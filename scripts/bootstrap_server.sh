#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash scripts/bootstrap_server.sh" >&2
  exit 1
fi

APP_USER="${APP_USER:-stocknotifier}"
APP_DIR="${APP_DIR:-/opt/stock-notifier}"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  caddy git python3 python3-pip python3-venv rsync sqlite3 ufw unattended-upgrades

if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${APP_USER}"
fi

install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 "${APP_DIR}"

# Keep OCI's VCN rules and the host firewall: both layers must allow the traffic.
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

systemctl enable --now caddy
echo "Bootstrap complete. Copy the repository to ${APP_DIR}, then continue SETUP_SOP.md."
