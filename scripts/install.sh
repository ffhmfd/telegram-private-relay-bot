#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Please run as root or with sudo." >&2
  exit 1
fi

for command_name in python3 systemctl systemd-run useradd install; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Missing required command: ${command_name}" >&2
    exit 1
  }
done

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/telegram-relay-bot
CONFIG_DIR=/etc/telegram-relay-bot
SERVICE_USER=telegram-relay-bot
FIRST_INSTALL="$(python3 "${PROJECT_DIR}/scripts/install_preflight.py" "${CONFIG_DIR}" "${APP_DIR}" /var/lib/telegram-relay-bot)"
BOT_TOKEN=
OWNER_CHAT_ID=

cleanup() {
  unset BOT_TOKEN OWNER_CHAT_ID
}
trap cleanup EXIT

if ! getent passwd "${SERVICE_USER}" >/dev/null; then
  useradd --system --home-dir /var/lib/telegram-relay-bot --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o root -g root -m 0755 "${APP_DIR}"
install -d -o root -g root -m 0700 "${CONFIG_DIR}"
install -d -o root -g root -m 0755 "${APP_DIR}/web"
for file in bot.py verification.py; do
  install -o root -g root -m 0644 "${PROJECT_DIR}/${file}" "${APP_DIR}/${file}.new"
  mv -f "${APP_DIR}/${file}.new" "${APP_DIR}/${file}"
done
for file in verify.html verify.css verify.js; do
  install -o root -g root -m 0644 "${PROJECT_DIR}/web/${file}" "${APP_DIR}/web/${file}.new"
  mv -f "${APP_DIR}/web/${file}.new" "${APP_DIR}/web/${file}"
done
install -o root -g root -m 0644 \
  "${PROJECT_DIR}/deploy/telegram-relay-bot.service" \
  /etc/systemd/system/telegram-relay-bot.service
install -o root -g root -m 0644 \
  "${PROJECT_DIR}/deploy/telegram-relay-bot-init.service" \
  /etc/systemd/system/telegram-relay-bot-init.service
install -o root -g root -m 0644 \
  "${PROJECT_DIR}/deploy/telegram-relay-verify.service" \
  /etc/systemd/system/telegram-relay-verify.service
install -o root -g root -m 0644 \
  "${PROJECT_DIR}/deploy/config.example" "${CONFIG_DIR}/config.example"

if [[ ${FIRST_INSTALL} -eq 1 ]]; then
  read -r -s -p "Telegram Bot Token: " BOT_TOKEN
  echo
  read -r -p "Administrator Telegram User ID: " OWNER_CHAT_ID

  if [[ ! ${BOT_TOKEN} =~ ^[0-9]{6,}:[A-Za-z0-9_-]{20,}$ ]]; then
    echo "The Bot Token format is invalid." >&2
    exit 1
  fi
  if [[ ! ${OWNER_CHAT_ID} =~ ^-?[0-9]+$ ]]; then
    echo "The administrator User ID must be numeric." >&2
    exit 1
  fi

  umask 077
  printf '%s\n' "${BOT_TOKEN}" >"${CONFIG_DIR}/token"
  printf '%s\n' \
    "OWNER_CHAT_ID=${OWNER_CHAT_ID}" \
    "MAPPING_RETENTION_DAYS=30" \
    "RATE_LIMIT_COUNT=20" \
    "RATE_LIMIT_WINDOW_SECONDS=60" \
    "LOG_LEVEL=INFO" >"${CONFIG_DIR}/config"
  chown root:root "${CONFIG_DIR}/token" "${CONFIG_DIR}/config"
  chmod 0600 "${CONFIG_DIR}/token" "${CONFIG_DIR}/config"
else
  echo "Keeping existing credentials and configuration in ${CONFIG_DIR}."
fi

systemctl daemon-reload
systemd-analyze verify \
  /etc/systemd/system/telegram-relay-bot.service \
  /etc/systemd/system/telegram-relay-bot-init.service \
  /etc/systemd/system/telegram-relay-verify.service

if [[ ${FIRST_INSTALL} -eq 1 ]]; then
  systemctl stop telegram-relay-bot.service 2>/dev/null || true
  systemctl start telegram-relay-bot-init.service
fi

run_verify_check() {
  # Let systemd parse its own EnvironmentFile syntax, including whitespace,
  # quoting and continuations. Never source the file as a shell program.
  systemd-run --quiet --wait --pipe --collect \
    --property="EnvironmentFile=${CONFIG_DIR}/config" \
    /usr/bin/python3 -E -s -B "${APP_DIR}/verification.py" "$1"
}

VERIFY_MODE="$(run_verify_check --check-config)"
if [[ "${VERIFY_MODE}" == enabled ]]; then
  systemctl enable telegram-relay-verify.service
  systemctl restart telegram-relay-verify.service
  run_verify_check --check-ready
  systemctl is-active --quiet telegram-relay-verify.service
fi

systemctl enable telegram-relay-bot.service
systemctl restart telegram-relay-bot.service
systemctl --no-pager --full status telegram-relay-bot.service

echo
echo "Installation complete. Send /start to your bot from the administrator account."
