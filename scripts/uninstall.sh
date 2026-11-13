#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Please run as root or with sudo." >&2
  exit 1
fi

systemctl disable --now telegram-relay-bot.service 2>/dev/null || true
systemctl disable --now telegram-relay-verify.service 2>/dev/null || true
rm -f /etc/systemd/system/telegram-relay-bot.service
rm -f /etc/systemd/system/telegram-relay-bot-init.service
rm -f /etc/systemd/system/telegram-relay-verify.service
rm -f /opt/telegram-relay-bot/bot.py
rm -f /opt/telegram-relay-bot/verification.py
rm -f /opt/telegram-relay-bot/web/verify.html /opt/telegram-relay-bot/web/verify.css /opt/telegram-relay-bot/web/verify.js
rmdir /opt/telegram-relay-bot/web 2>/dev/null || true
rmdir /opt/telegram-relay-bot 2>/dev/null || true
systemctl daemon-reload

echo "Program and services removed. Credentials and database were preserved."
echo "To delete all retained data manually:"
echo "  sudo rm -rf /etc/telegram-relay-bot /var/lib/telegram-relay-bot"
echo "  sudo userdel telegram-relay-bot"
