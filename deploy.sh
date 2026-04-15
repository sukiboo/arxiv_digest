#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="./.env"
SETTINGS_FILE="./settings.ini"

for f in "$ENV_FILE" "$SETTINGS_FILE"; do
  [[ -f "$f" ]] || { echo "ERROR: $f not found."; exit 1; }
done

CRON_SCHEDULE=$(python3 -c "import configparser; c=configparser.ConfigParser(); c.read('$SETTINGS_FILE'); print(c.get('deploy','cron_schedule').strip().strip('\"').strip(\"'\"))")

set -a; source "$ENV_FILE"; set +a

for var in SERVER_USER SERVER_HOST APP_PATH; do
  [[ -n "${!var:-}" ]] || { echo "ERROR: $var not set in $ENV_FILE"; exit 1; }
done

REMOTE="${SERVER_USER}@${SERVER_HOST}"
REPO_URL=$(git remote get-url origin | sed 's|git@github.com:|https://github.com/|')

echo "==> Syncing code to server"
ssh "$REMOTE" bash <<EOF
  set -euo pipefail
  if [[ -d ~/${APP_PATH}/.git ]]; then
    git -C ~/${APP_PATH} fetch --prune
    git -C ~/${APP_PATH} pull --ff-only
  else
    mkdir -p ~/${APP_PATH}
    git clone ${REPO_URL} ~/${APP_PATH}
  fi
EOF

echo "==> Copying config files"
scp "$ENV_FILE" "$REMOTE":~/"${APP_PATH}/.env"
scp "$SETTINGS_FILE" "$REMOTE":~/"${APP_PATH}/settings.ini"
ssh "$REMOTE" "chmod 600 ~/${APP_PATH}/.env"

echo "==> Setting up venv"
ssh "$REMOTE" bash <<EOF
  set -euo pipefail
  sudo apt-get install -y -qq python3-venv >/dev/null 2>&1
  cd ~/${APP_PATH}
  python3 -m venv --upgrade-deps venv
  venv/bin/pip install -q -r requirements.txt
EOF

echo "==> Installing cron job (${CRON_SCHEDULE})"
ssh "$REMOTE" bash <<EOF
  set -euo pipefail
  APPDIR=~/${APP_PATH}
  CMD="cd \$APPDIR && venv/bin/python arxiv_digest.py"
  # remove old entry if present, then add new one
  (crontab -l 2>/dev/null | grep -v "arxiv_digest" || true; echo "${CRON_SCHEDULE} \$CMD") | crontab -
EOF

echo "==> Done"
