#!/bin/bash
# Put the server (server/*.py and the web app built into static/dist) live on the deploy host, on every instance, and check it.
#   server/deploy.sh            # HEAD (a clean origin/main): build, back up, install, restart, check; the friends' first
#   server/deploy.sh check      # check what runs now (read only)
#   server/deploy.sh rollback   # put back what the last deploy replaced (code, web, columns it added), restart, check
# Instances are dir:unit; LR_INSTANCES replaces them (a throwaway test instance). Each deploy keeps a backup with a copy
# of live.db in <dir>/backup-deploy-<time>, <dir>/backup-last points at it; a deploy that passes its check keeps only its
# own and the one before.
set -euo pipefail
HOST=suyiiyii@203.0.113.10
INSTANCES=${LR_INSTANCES:-/opt/lr-prod:lr-prod /opt/lr-prod2:lr-prod2 /opt/live-record:live-record}

if [ "${1:-}" != remote ]; then  # on this Mac
  mode=${1:-deploy}
  case $mode in deploy | check | rollback) ;; *) sed -n 2,5p "$0" >&2; exit 2 ;; esac
  root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
  export SSH_AUTH_SOCK=${LR_DEPLOY_SSH_SOCK:-$SSH_AUTH_SOCK}
  if [ "$mode" = deploy ]; then
    git -C "$root" fetch -q origin main
    if [ -n "$(git -C "$root" status --porcelain -- server web)" ] || [ "$(git -C "$root" rev-parse HEAD)" != "$(git -C "$root" rev-parse origin/main)" ]; then
      echo "deploy only a clean checkout of origin/main" >&2; exit 1
    fi
    (cd "$root/web" && pnpm install --frozen-lockfile && pnpm exec vp run build)
    tmp=$(mktemp -d)
    git -C "$root" rev-parse --short HEAD > "$tmp/COMMIT"
    # explicit list, no excludes: bsdtar's * crosses /, an exclude once dropped dist/index.html; no ._ files
    (cd "$root/server" && COPYFILE_DISABLE=1 tar --no-xattrs -czf "$tmp/release.tgz" ./*.py pyproject.toml uv.lock static/dist -C "$tmp" COMMIT)
    tar -tzf "$tmp/release.tgz" | grep -qx static/dist/index.html || { echo "static/dist/index.html is not in the release" >&2; exit 1; }
    scp -q "$tmp/release.tgz" "$HOST:lr-deploy.tgz"
  fi
  ssh "$HOST" "wsl -u root -e bash -s -- remote $mode $INSTANCES" < "$0"
  exit
fi

# in WSL on the deploy host, as root: remote <mode> <dir:unit>...
mode=$2; shift 2
RUN=$(date +%Y%m%d-%H%M%S)
TGZ=/mnt/c/Users/me/lr-deploy.tgz  # where scp put it (the Windows home)
UV=/root/.local/bin/uv

env_of() { grep "^$2=" "$1/.env" | tail -1 | cut -d= -f2-; }  # dir key: the last one wins, as in systemd

get() {  # dir path: GET from the instance on loopback, with its token
  curl -sf -m 10 -H "Authorization: Bearer $(env_of "$1" AUTH_TOKEN)" "http://127.0.0.1:$(env_of "$1" PORT)$2"
}

started() {  # dir unit log-offset (after a restart): answering within 3 min, not restarted since, no errors logged
  local d=$1 u=$2 off=$3 pid
  pid=$(systemctl show -p MainPID --value "$u")
  for _ in $(seq 90); do  # until it answers or dies
    { get "$d" /api/status > /dev/null || [ "$(systemctl show -p MainPID --value "$u")" != "$pid" ]; } && break; sleep 2
  done
  sleep 20  # the pipelines load their models after the port is up (a torch hub check has failed there)
  [ "$(systemctl show -p MainPID --value "$u")" = "$pid" ] || { echo "  FAIL $d: $u restarted while starting"; return 1; }
  if tail -c +$((off + 1)) "$d/data/server.log" | grep -E 'Traceback|task .* ended'; then
    echo "  FAIL $d: errors in data/server.log since the restart"; return 1
  fi
}

check() {  # dir unit: active, running the commit in its COMMIT, serving the pages, each asset index.html names, the API
  local d=$1 u=$2 dist=$1/server/static/dist want got a p n=0 bad=
  want=$(cat "$d/server/COMMIT" 2> /dev/null || true)
  [ "$(systemctl is-active "$u")" = active ] || bad+=" $u-not-active"
  got=$(get "$d" /api/status | python3 -c 'import json, sys; print(json.load(sys.stdin)["commit"])') || got="(no /api/status)"
  [ "$got" = "$want" ] || bad+=" running:'$got'-not-'$want'"
  for p in / /conversations; do get "$d" "$p" | cmp -s - "$dist/index.html" || bad+=" $p"; done
  for a in $(grep -o '/assets/[^"]*' "$dist/index.html"); do
    get "$d" "$a" | cmp -s - "$dist$a" || bad+=" $a"; n=$((n + 1))
  done
  [ $n -gt 0 ] || bad+=" no-assets-in-index.html"
  for p in /api/client-config /api/days /api/conversations; do get "$d" "$p" > /dev/null || bad+=" $p"; done
  [ -z "$bad" ] || { echo "  FAIL $d:$bad"; return 1; }
  echo "  ok $d ($u): runs ${want:-(no COMMIT)}; /, /conversations, $n assets, API"
}

deploy() {  # dir unit: back up, install the release, restart, check
  local d=$1 u=$2 b off o
  b=$d/backup-deploy-$RUN
  mkdir -p "$b/old" "$b/new"
  tar -xzf "$TGZ" -C "$b/new" --no-same-owner
  cp -a "$d"/server/*.py "$b/old/"
  [ ! -f "$d/server/COMMIT" ] || cp -a "$d/server/COMMIT" "$b/old/"
  # the release's packages (its uv.lock) into the instance's venv first: added or changed, none removed (--inexact), so
  # the code running now and a rollback's run on them too; a failure stops here, nothing installed, nothing restarted
  (cd "$b/new" && UV_PROJECT_ENVIRONMENT="$d/server/.venv" "$UV" sync --frozen --inexact -q)
  for f in pyproject.toml uv.lock; do [ ! -f "$d/server/$f" ] || cp -a "$d/server/$f" "$b/old/"; done
  # the columns now (rollback drops those a release adds: old code inserts whole rows) and an online copy of the db
  "$d/server/.venv/bin/python" - "$(env_of "$d" LIVE_DATA)/live.db" "$b/old" << 'PY'
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
tables = [t for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
json.dump({t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")] for t in tables}, open(f"{sys.argv[2]}/columns.json", "w"))
c.backup(sqlite3.connect(f"{sys.argv[2]}/live.db"))
PY
  INSTALLED+=" $d"
  ln -sfn "$b" "$d/backup-last"  # once the backup is whole; rollback goes by it and by COMMIT, so COMMIT goes in first
  cp "$b/new/COMMIT" "$d/server/"
  cp "$b/new/"*.py "$b/new/pyproject.toml" "$b/new/uv.lock" "$d/server/"
  mv "$d/server/static/dist" "$b/old/dist"
  cp -a "$b/new/static/dist" "$d/server/static/dist"
  off=$(stat -c %s "$d/data/server.log")
  systemctl restart "$u"
  echo "  $d: $(cat "$b/old/COMMIT" 2> /dev/null || echo '(no COMMIT)') -> $(cat "$b/new/COMMIT"), backup $b"
  started "$d" "$u" "$off" && check "$d" "$u" || return 1
  for o in $(printf '%s\n' "$d"/backup-deploy-* | head -n -2); do rm -rf -- "$o"; echo "  removed ${o##*/}"; done  # time-named: sorted
}

rollback() {  # dir unit: undo the newest deploy run, where it got to installing and is still what is installed
  local d=$1 u=$2 b off
  b=$(readlink "$d/backup-last" || true)
  if [ "${b##*/}" != "$NEWEST" ] || ! cmp -s "$b/new/COMMIT" "$d/server/COMMIT"; then
    echo "  skip $d: not on the release of ${NEWEST:-any deploy}"; return 0
  fi
  off=$(stat -c %s "$d/data/server.log")
  systemctl stop "$u"
  "$d/server/.venv/bin/python" - "$(env_of "$d" LIVE_DATA)/live.db" "$b/old/columns.json" << 'PY'
import json, sqlite3, sys
c, before = sqlite3.connect(sys.argv[1]), json.load(open(sys.argv[2]))
for t, cols in before.items():
    for col in [r[1] for r in c.execute(f"PRAGMA table_info({t})")]:
        if col not in cols:
            print(f"  drop column {t}.{col}")
            c.execute(f"ALTER TABLE {t} DROP COLUMN {col}")
PY
  cp -a "$b/old/"*.py "$d/server/"
  for f in pyproject.toml uv.lock; do [ ! -f "$b/old/$f" ] || cp -a "$b/old/$f" "$d/server/"; done  # the venv keeps what came
  if [ -f "$b/old/COMMIT" ]; then cp -a "$b/old/COMMIT" "$d/server/"; else mv "$d/server/COMMIT" "$b/rolled-back-COMMIT"; fi
  if [ -d "$b/old/dist" ]; then  # the deploy got as far as replacing it
    mv "$d/server/static/dist" "$b/rolled-back-dist"
    cp -a "$b/old/dist" "$d/server/static/dist"
  fi
  systemctl start "$u"
  echo "  $d: back to $(cat "$b/old/COMMIT" 2> /dev/null || echo '(no COMMIT)') from $b"
  started "$d" "$u" "$off" && check "$d" "$u"
}

# deploy and rollback stop at the first failure (called under || a function would run on past one); check goes on
stopped() {  # on exit: what a failed deploy left
  [ "$1" != 0 ] && [ "$mode" = deploy ] || return 0
  if [ -n "${INSTALLED:-}" ]; then echo "stopped; installed on$INSTALLED, undo with: server/deploy.sh rollback"; else echo "stopped; nothing installed"; fi
}
trap 'stopped $?' EXIT
NEWEST=$(for i in "$@"; do readlink "${i%%:*}/backup-last" || true; done | sed 's|.*/||' | sort | tail -1)
rc=0
for i in "$@"; do
  if [ "$mode" = check ]; then check "${i%%:*}" "${i#*:}" || rc=1; else "$mode" "${i%%:*}" "${i#*:}"; fi
done
exit $rc
