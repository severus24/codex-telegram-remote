#!/usr/bin/env bash
set -euo pipefail

systemctl --user disable --now codex-telegram-remote.service 2>/dev/null || true
rm -f "${HOME}/.config/systemd/user/codex-telegram-remote.service"
systemctl --user daemon-reload
echo "Removed codex-telegram-remote.service"
