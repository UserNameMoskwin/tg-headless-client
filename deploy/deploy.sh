#!/usr/bin/env bash
# Push the code to the server and restart the service.
# Run from the repo root: bash deploy/deploy.sh [ssh-host]
# Needs only tar + ssh locally (Git Bash works). The code is staged on the
# server, then synced into place with rsync --delete; data/, logs/, .env and
# .venv/ are excluded, so they're never touched.
set -euo pipefail

HOST="${1:-moskwin}"
DEST=/opt/tgclient
STAGE=/tmp/tgclient-release

tar -cz \
  --exclude='./.git' --exclude='./.venv' --exclude='__pycache__' \
  --exclude='./.pytest_cache' --exclude='*.egg-info' --exclude='./.claude' \
  --exclude='./data' --exclude='./logs' --exclude='./.env' \
  --exclude='./.projecttray.json' \
  -f - . \
  | ssh "$HOST" "rm -rf $STAGE && mkdir -p $STAGE && tar -xz -C $STAGE --no-same-owner"

ssh "$HOST" "set -e
  # code is owned by root and read-only for the service user
  rsync -rlt --delete --chmod=D755,F644 \
    --exclude /data/ --exclude /logs/ --exclude /.env --exclude /.venv/ \
    --exclude '*.egg-info/' --exclude '__pycache__/' \
    $STAGE/ $DEST/
  rm -rf $STAGE
  mkdir -p $DEST/data $DEST/logs
  chown tgclient:tgclient $DEST/data $DEST/logs
  [ -d $DEST/.venv ] || python3 -m venv $DEST/.venv
  $DEST/.venv/bin/pip install -q -e $DEST
  if systemctl cat tgclient >/dev/null 2>&1; then
    systemctl restart tgclient
    sleep 5
    systemctl --no-pager --lines=20 status tgclient
  else
    echo 'tgclient.service is not installed yet (see README: Server deployment)'
  fi"
