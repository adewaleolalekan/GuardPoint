#!/usr/bin/env bash
#
# Provision GuardPoint on a fresh Ubuntu 24.04 LTS (arm64) instance.
#
# Run as a user with sudo, from the repository root:
#     sudo bash deploy/provision.sh
#
# Idempotent: safe to re-run after a git pull. It does not touch .env if
# that file already exists, so an existing API key is never clobbered.

set -euo pipefail

APP_USER=guardpoint
APP_DIR=/opt/guardpoint
REPO_URL="https://github.com/adewaleolalekan/GuardPoint.git"

log() { printf '\n==> %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "error: run with sudo (needs to create users and install units)" >&2
    exit 1
fi

log "Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl

log "Creating service account ${APP_USER}"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "${APP_USER}"
fi

log "Fetching application into ${APP_DIR}"
if [[ -d "${APP_DIR}/.git" ]]; then
    git -C "${APP_DIR}" fetch --quiet origin
    git -C "${APP_DIR}" reset --hard origin/main
else
    git clone --quiet "${REPO_URL}" "${APP_DIR}"
fi
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

log "Building virtualenv"
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    sudo -u "${APP_USER}" python3 -m venv "${APP_DIR}/.venv"
fi
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

log "Writing .env"
if [[ -f "${APP_DIR}/.env" ]]; then
    echo "    .env already exists — leaving it untouched"
else
    # Empty POSTCODE_API_KEY means the app runs against its built-in
    # simulator: no network calls, no credits consumed. Set a real
    # nipost_test_... or nipost_live_... key to hit the live gateway.
    cat > "${APP_DIR}/.env" <<'EOF'
POSTCODE_API_KEY=
POSTCODE_MAX_LEVEL=3
RISK_BLOCK_THRESHOLD=70
RISK_REVIEW_THRESHOLD=40
EOF
    chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
    # Readable only by the service account.
    chmod 600 "${APP_DIR}/.env"
fi

log "Installing systemd unit"
install -m 0644 "${APP_DIR}/deploy/guardpoint.service" /etc/systemd/system/guardpoint.service
systemctl daemon-reload
systemctl enable --now guardpoint

log "Waiting for the service to answer"
for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
        echo "    healthy"
        curl -fsS http://127.0.0.1:8000/api/health; echo
        break
    fi
    sleep 1
done

cat <<EOF

Done. GuardPoint is listening on 127.0.0.1:8000 and is NOT reachable
from the internet yet. To publish it:

  1. Install cloudflared (see deploy/README.md).
  2. cloudflared tunnel login
     cloudflared tunnel create guardpoint
     TUNNEL_ID=\$(basename ~/.cloudflared/*.json .json)
  3. sudo cp deploy/cloudflared.yml /etc/cloudflared/config.yml
     sudo cp ~/.cloudflared/"\${TUNNEL_ID}".json /etc/cloudflared/
     sudo sed -i "s|<TUNNEL-ID>|\${TUNNEL_ID}|g" /etc/cloudflared/config.yml
     sudo chmod 600 /etc/cloudflared/*.json
  4. Test in the foreground first — a bad credentials path is the usual
     reason "service install" reports exit code 1:
       sudo cloudflared --config /etc/cloudflared/config.yml tunnel run
  5. sudo cloudflared service install
     cloudflared tunnel route dns guardpoint guardpoint-test.waletech.org
  6. Add a Cloudflare Access policy — this app has NO authentication
     of its own.

Logs: journalctl -u guardpoint -f
EOF
