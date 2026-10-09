#!/bin/bash
# Put a release APK at the main instance's /app.apk: the Android app offers it to every phone on an older build
# (/api/app/latest reads the version from the file). The APK it replaces stays beside it as live-record.apk.prev-<version>.
#   server/apk_release.sh <apk>     # e.g. app/build/app/outputs/flutter-apk/app-release.apk; its build number must be higher
set -euo pipefail
HOST=suyiiyii@203.0.113.10
DIR=/opt/live-record  # the main instance only (the friends' keep the APK they were set up with)

if [ "${1:-}" != remote ]; then  # on this Mac
  [ -f "${1:-}" ] || { sed -n 4p "$0" >&2; exit 2; }
  new=$(uv run --no-project python "$(dirname "$0")/apk.py" "$1")
  echo "$1: $new"
  export SSH_AUTH_SOCK=${LR_DEPLOY_SSH_SOCK:-$SSH_AUTH_SOCK}
  scp -q "$1" "$HOST:lr-app.apk"
  ssh "$HOST" "wsl -u root -e bash -s -- remote $DIR $new" < "$0"
  exit
fi

# in WSL on the deploy host, as root: remote <dir> <versionName+versionCode>
d=$2 new=$3
TMP=/mnt/c/Users/me/lr-app.apk  # where scp put it (the Windows home)
env_of() { grep "^$1=" "$d/.env" | tail -1 | cut -d= -f2-; }
apk=$(env_of LIVE_DATA)/live-record.apk
if [ -f "$apk" ]; then
  old=$("$d/server/.venv/bin/python" "$d/server/apk.py" "$apk")
  [ "${old#*+}" -lt "${new#*+}" ] || { echo "the server has $old: $new is not newer, nothing changed" >&2; rm -f "$TMP"; exit 1; }
  cp -a "$apk" "$apk.prev-$old"
fi
install -m 644 "$TMP" "$apk.new" && mv "$apk.new" "$apk"  # a rename: a download under way goes on reading the old file
rm -f "$TMP"
# what the phones now get (the token through stdin: not in the process list)
got=$(echo "Authorization: Bearer $(env_of AUTH_TOKEN)" | curl -sf -m 10 -H @- "http://127.0.0.1:$(env_of PORT)/api/app/latest")
echo "$got" | grep -q "\"versionCode\":${new#*+}[,}]" || { echo "FAIL: /api/app/latest says $got" >&2; exit 1; }
echo "ok: /app.apk is $new${old:+ (was $old, kept as ${apk##*/}.prev-$old)}"
