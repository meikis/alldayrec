#!/bin/bash
# Build the web app and put it live on the deploy host: no Node there, no service restart.
#   web/deploy.sh            # rollback on the server: mv dist dist.bad && mv dist.prev dist
set -euo pipefail
cd "$(dirname "$0")"
JUMP=root@example.com HOST=suyiiyii@203.0.113.10 APP=/opt/live-record/server/static
export SSH_AUTH_SOCK=${LR_DEPLOY_SSH_SOCK:-$SSH_AUTH_SOCK}

pnpm exec vp run build
COPYFILE_DISABLE=1 tar --no-xattrs -czf /tmp/live-record-web.tgz -C ../server/static dist  # no ._ files: they broke data/ once
scp -q -J $JUMP /tmp/live-record-web.tgz $HOST:live-record-web.tgz
S="set -e; cd $APP; rm -rf dist.new && mkdir dist.new && tar -xzf /mnt/c/Users/me/live-record-web.tgz -C dist.new --strip-components=1
rm -rf dist.prev; [ -d dist ] && mv dist dist.prev; mv dist.new dist; ls dist/assets | wc -l"
ssh -J $JUMP $HOST "wsl -u root -e bash -c \"echo $(echo "$S" | base64) | base64 -d | bash\""
echo "deployed: $(git rev-parse --short HEAD)"
