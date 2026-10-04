#!/bin/bash
# End-to-end test of the iPhone app's BLE link without a board. This Mac plays the recorder (fake.swift: the record
# stream of firmware/devkit/src/lr_store.h over CoreBluetooth), server.py stands in for the server behind a cloudflared
# quick tunnel (HTTPS: no ATS exception, no local-network prompt on the phone), and the app on a wirelessly paired
# iPhone gets its settings from the launch environment (LR_E2E, Recorder.launch()): bound to the fake, BLE source.
# The phone must stay unlocked until the app has found the fake (about 15 s: it scans for its name, which iOS sees only
# in the foreground); after that it may lock (the app goes on in the background). A launch that is refused or does not
# connect is tried again at the next unlock.
#   app/ios/e2e/run.sh [--install] [--minutes N] [--kill]
#     --install    build the release app (debug Flutter builds do not run on their own) and install it
#     --minutes N  how long the app follows the fake (default 5); then server.py check
#     --kill       half way, SIGKILL the app: iOS should relaunch it in the background for the recorder (state
#                  restoration) and the audio go on
# Needs Xcode (devicectl), flutter, ffmpeg with libopus, cloudflared, python3; LR_DEVICE picks the iPhone (default: the
# first paired one within reach). iOS launches an app only while unlocked: the script waits for that (twice: the test,
# then putting the app's own settings back, backed up in the work dir before the first run). The fake advertises only
# while the script runs. Work dir: $TMPDIR/lr-e2e (logs: fake.log, app.log, up/server.jsonl).
set -euo pipefail
umask 077  # the backup holds the app's token
cd "$(dirname "$0")"
W=${TMPDIR:-/tmp}/lr-e2e
APP=com.suyiiyii.liverecord
NAME=LR-MAC-BEEF
MINUTES=5 INSTALL= KILL=
while [ $# -gt 0 ]; do
  case $1 in --install) INSTALL=1 ;; --minutes) MINUTES=$2; shift ;; --kill) KILL=1 ;; *) echo "unknown $1"; exit 2 ;; esac
  shift
done
mkdir -p "$W"
say() { echo "$(date +%T) $*"; }
json() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print($1)" "$2"; }

xcrun devicectl list devices --json-output "$W/devices.json" >/dev/null
DEV=${LR_DEVICE:-$(json "next(x['identifier'] for x in d['result']['devices'] if x['hardwareProperties'].get('deviceType') == 'iPhone' and x['connectionProperties'].get('tunnelState') in ('connected', 'disconnected'))" "$W/devices.json")}
say "iPhone $DEV"

if [ -n "$INSTALL" ]; then
  (cd ../.. && flutter build ios --release)
  xcrun devicectl device install app --device "$DEV" ../../build/ios/iphoneos/Runner.app >/dev/null
fi

# the fake recorder and the audio it replays: tones a second each, as 10 ms Opus frames
swiftc -O -o "$W/fake" fake.swift
[ -f "$W/tone.ogg" ] || ffmpeg -v error -f lavfi -i "aevalsrc='0.3*sin(2*PI*(300+100*mod(floor(t)\,10))*t)':s=16000:d=120" \
  -ac 1 -c:a libopus -frame_duration 10 -b:a 24k "$W/tone.ogg"

# the app's own settings, once: a later run would back up the test's
if [ ! -d "$W/backup" ]; then
  xcrun devicectl device copy from --device "$DEV" --domain-type appDataContainer --domain-identifier $APP \
    --source Library/Preferences --destination "$W/backup" >/dev/null
  say "app settings backed up in $W/backup"
fi

unlocked() {
  say "waiting for the iPhone to be unlocked"
  while xcrun devicectl device info lockState --device "$DEV" 2>/dev/null | grep -q "passcodeRequired: true"; do sleep 10; done
}
launch() {  # the app, its settings from $1 (JSON)
  local env
  env=$(python3 -c 'import json,sys; print(json.dumps({"LR_E2E": sys.argv[1]}))' "$1")
  xcrun devicectl device process launch --device "$DEV" --terminate-existing --console --environment-variables "$env" $APP >> "$W/app.log" 2>&1 &
}
pid() {
  xcrun devicectl device info apps --device "$DEV" --bundle-id $APP --json-output "$W/apps.json" >/dev/null
  xcrun devicectl device info processes --device "$DEV" --json-output "$W/procs.json" >/dev/null
  python3 -c "
import json,sys; u=json.load(open(sys.argv[1]))['result']['apps'][0]['url']
print(next((p['processIdentifier'] for p in json.load(open(sys.argv[2]))['result']['runningProcesses'] if p.get('executable','') == u + 'Runner'), ''))" \
    "$W/apps.json" "$W/procs.json"
}

# the server, reachable from the phone over HTTPS; up before the wait, as the phone may stay unlocked only briefly
SERVER= TUNNEL= FAKE=
cleanup() { kill ${SERVER:-} ${TUNNEL:-} ${FAKE:-} 2>/dev/null || true; }
trap cleanup EXIT
PORT=$((40000 + RANDOM % 20000))
TOKEN=$(openssl rand -hex 16)
rm -rf "$W/up"
python3 server.py serve 127.0.0.1 "$PORT" "$TOKEN" "$W/up" > "$W/server.log" 2>&1 &
SERVER=$!
answers() { curl -sf -m 10 -H "Authorization: Bearer $TOKEN" "$URL/api/client-config" >/dev/null; }
tunnel() {  # (re)starts the quick tunnel until it answers
  kill ${TUNNEL:-} 2>/dev/null || true
  : > "$W/tunnel.log"
  cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > "$W/tunnel.log" 2>&1 &
  TUNNEL=$!
  for _ in $(seq 60); do
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$W/tunnel.log" | head -1 || true)
    [ -n "$URL" ] && answers && { say "server $URL"; return; }
    sleep 2
  done
  say "the tunnel does not come up"; exit 1
}
tunnel

# the app launched while the phone is unlocked, and running: iOS refuses a launch once the phone has locked again, and
# the wait starts over (the fake advertises only while the app is meant to follow it)
start() {  # $1: the app's settings (JSON); $2: run the fake for it
  for try in 1 2 3 4 5; do
    unlocked
    answers || tunnel
    if [ -n "${2:-}" ]; then
      rm -rf "$W/card"
      (cd "$W" && exec ./fake $NAME > fake.log 2>&1) &
      FAKE=$!
      sleep 2
    fi
    launch "$1"
    sleep 8
    P=$(pid)
    if [ -n "$P" ] && [ -n "${2:-}" ]; then  # and it found the fake: by its name, which iOS sees only in the foreground
      for _ in $(seq 30); do grep -q "subscribed 7C3E0004" "$W/fake.log" && return; sleep 2; done
      say "the app runs but did not connect to the fake in 60 s (locked before its scan?)"
    elif [ -n "$P" ]; then
      return
    else
      say "the app did not start (locked again?)"
    fi
    say "waiting for the next unlock (try $try of 5)"
    if [ -n "${2:-}" ]; then kill $FAKE; FAKE=; fi
    while xcrun devicectl device info lockState --device "$DEV" 2>/dev/null | grep -q "passcodeRequired: false"; do sleep 10; done
  done
  say "giving up: the app never ran with the fake"; exit 1
}

# the test
start "$(python3 -c 'import json,sys; s,t,n=sys.argv[1:]; print(json.dumps({
  "server": s, "token": t, "device": "iphone-e2e", "source": "ble", "active": True, "archive": False, "foreground": 1, "background": 20,
  "bleName": n, "bindPending": True, "bindForce": True, "bleId": None, "bleKicked": None, "bleModel": None, "bleFw": None,
  "flutter.server": s, "flutter.token": t, "flutter.source": "ble", "flutter.archive": False}))' "$URL" "$TOKEN" $NAME)" fake
say "app running (pid $P), following $NAME for $MINUTES min"
if [ -n "$KILL" ]; then
  sleep $((MINUTES * 30))
  xcrun devicectl device process signal --device "$DEV" --pid "$P" --signal 9 >/dev/null || true
  say "killed the app (pid $P)"
  Q=
  for _ in $(seq 30); do sleep 5; Q=$(pid); [ -n "$Q" ] && [ "$Q" != "$P" ] && break; Q=; done
  say "after the kill: ${Q:+relaunched by iOS in the background (pid $Q)}${Q:-not relaunched within 150 s}"
  sleep $((MINUTES * 30))
else
  sleep $((MINUTES * 60))
fi
kill $FAKE; FAKE=
say "fake stopped; the uploads left drain for a minute"
sleep 60
python3 server.py check "$W/up" $NAME && OK=0 || OK=1

# the app's settings back: nothing of the test may go to the real server, so the queue is drained (still to the test
# server) first; then the backed-up values, the test's keys removed
start '{"source": "mic", "active": false, "flutter.source": "mic"}'
LEFT=
for _ in $(seq 10); do
  sleep 30
  rm -rf "$W/left"
  xcrun devicectl device copy from --device "$DEV" --domain-type appDataContainer --domain-identifier $APP \
    --source "Library/Application Support/chunks" --destination "$W/left" >/dev/null 2>&1 || true
  LEFT=$(find "$W/left" -name '*.ogg' 2>/dev/null | wc -l | tr -d ' ')
  say "pieces left in the app's queue: $LEFT"
  [ "$LEFT" = 0 ] && break
done
[ "$LEFT" = 0 ] || { say "the queue does not drain: settings NOT restored (backup in $W/backup)"; exit 1; }
python3 - "$W/backup" > "$W/restore.json" <<'EOF'
import json, plistlib, sys
from pathlib import Path
d = {k: None for k in ["source", "active", "bleName", "bleId", "bindPending", "bindForce", "bleKicked", "bleModel", "bleFw",
                       "foreground", "background", "archive", "server", "token", "device", "flutter.source", "flutter.server",
                       "flutter.token", "flutter.archive"]}
for f in Path(sys.argv[1]).glob("*.plist"):
    d.update(plistlib.load(open(f, "rb")))
print(json.dumps(d))
EOF
start "$(cat "$W/restore.json")"
xcrun devicectl device process terminate --device "$DEV" --pid "$P" >/dev/null 2>&1 || true
say "app settings restored"
exit $OK
