#!/bin/bash
# Each instance's tenants.json (#200 B1: who a server serves, server/tenant.py), made once from its .env around the first
# deploy of a server that reads it. In order:
#   server/tenants.sh migrate    # before that deploy: <dir>/tenants.json (600) with the instance's own tenant, from its
#                                # .env: id LR_INSTANCE (else friend or main by its LIVE_DATA path, as the metrics had it),
#                                # token AUTH_TOKEN (copied, never printed), data LIVE_DATA, recordings RECORDINGS_DIR. The
#                                # .env is kept as .env.pre-tenants. Nothing restarts: the server now running ignores the file.
#   server/deploy.sh             # the server that reads tenants.json
#   server/tenants.sh cleanup    # once it checks out: AUTH_TOKEN and LR_INSTANCE out of .env (tenants.json has them)
# Back: server/tenants.sh rollback (the .env as it was, AUTH_TOKEN with it), then server/deploy.sh rollback. In that
# order: the server before tenants.json takes its token from .env and runs without auth when there is none (deploy.sh
# refuses to go back to it then). tenants.json stays: that server does not read it.
# Instances are dir:unit as in deploy.sh; LR_INSTANCES replaces them.
set -euo pipefail
HOST=suyiiyii@203.0.113.10
INSTANCES=${LR_INSTANCES:-/opt/lr-prod:lr-prod /opt/lr-prod2:lr-prod2 /opt/live-record:live-record}

if [ "${1:-}" != remote ]; then  # on this Mac
  mode=${1:-}
  case $mode in migrate | cleanup | rollback) ;; *) sed -n 2,14p "$0" >&2; exit 2 ;; esac
  export SSH_AUTH_SOCK=${LR_DEPLOY_SSH_SOCK:-$SSH_AUTH_SOCK}
  ssh "$HOST" "wsl -u root -e bash -s -- remote $mode $INSTANCES" < "$0"
  exit
fi

# in WSL on the deploy host, as root: remote <mode> <dir:unit>...
mode=$2; shift 2
for i in "$@"; do
  d=${i%%:*}
  case $mode in
    migrate)
      if [ -e "$d/tenants.json" ]; then echo "  skip $d: it has tenants.json"; continue; fi
      [ -e "$d/.env.pre-tenants" ] || cp -p "$d/.env" "$d/.env.pre-tenants"
      python3 - "$d" << 'PY'
import json, os, sys
d = sys.argv[1]
env = {}
for line in open(f"{d}/.env"):  # as systemd reads an EnvironmentFile: the last one wins, quotes around a value go
    k, eq, v = line.strip().partition("=")
    if eq and not k.startswith("#"):
        env[k.strip()] = v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "'\"" else v
data = env["LIVE_DATA"]
t = {"id": env.get("LR_INSTANCE") or ("friend" if "friend" in data else "main"), "token": env.get("AUTH_TOKEN", ""),
     "data": data, "recordings": env["RECORDINGS_DIR"]}
fd = os.open(f"{d}/tenants.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, (json.dumps([t], indent=1) + "\n").encode())
os.close(fd)
print(f"  {d}: tenants.json (600): tenant {t['id']}, data {data}, recordings {t['recordings']}, token {'copied' if t['token'] else 'EMPTY'}")
PY
      ;;
    cleanup)
      # only when tenants.json's token is the one in .env (then .env's is a copy, not the only one)
      python3 - "$d" << 'PY'
import json, sys
d = sys.argv[1]
env = [l for l in open(f"{d}/.env") if l.startswith("AUTH_TOKEN=")]
token = env[-1].strip().partition("=")[2].strip("'\"") if env else None
if token is not None and token not in [t["token"] for t in json.load(open(f"{d}/tenants.json"))]:
    sys.exit(f"  stop {d}: tenants.json has not .env's AUTH_TOKEN")
PY
      grep -v -E '^(AUTH_TOKEN|LR_INSTANCE)=' "$d/.env" > "$d/.env.new" || true
      chmod --reference="$d/.env" "$d/.env.new" && mv "$d/.env.new" "$d/.env"
      echo "  $d: AUTH_TOKEN and LR_INSTANCE out of .env (in tenants.json); the server reads it at its next start"
      ;;
    rollback)
      if [ ! -e "$d/.env.pre-tenants" ]; then echo "  skip $d: no .env.pre-tenants"; continue; fi
      cp -p "$d/.env" "$d/.env.tenants-rolled-back" && cp -p "$d/.env.pre-tenants" "$d/.env"
      echo "  $d: .env as before tenants.json (the one replaced kept as .env.tenants-rolled-back); now server/deploy.sh rollback"
      ;;
  esac
done
