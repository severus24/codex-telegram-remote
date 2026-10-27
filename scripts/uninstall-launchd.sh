#!/bin/zsh
set -euo pipefail

PLIST_TARGET="${HOME}/Library/LaunchAgents/com.codex-telegram-remote.plist"
launchctl bootout "gui/$(id -u)" "${PLIST_TARGET}" 2>/dev/null || true
rm -f "${PLIST_TARGET}"
print "已停止并移除 com.codex-telegram-remote"
