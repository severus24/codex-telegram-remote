#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_TEMPLATE="${PROJECT_DIR}/launchd/com.codex-telegram-remote.plist.example"
PLIST_TARGET="${HOME}/Library/LaunchAgents/com.codex-telegram-remote.plist"
EXECUTABLE="${CODEX_TELEGRAM_REMOTE_BIN:-$(command -v codex-telegram-remote || true)}"
CONFIG="${CODEX_REMOTE_CONFIG:-${HOME}/.config/codex-telegram-remote/config.env}"
DATA_DIR="${HOME}/Library/Application Support/codex-telegram-remote"
TOOL_PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}:/usr/bin:/bin:/usr/sbin:/sbin"

if [[ -z "${EXECUTABLE}" ]]; then
  print -u2 "codex-telegram-remote is not installed in PATH"
  print -u2 "Run: python3 -m pip install -e ${PROJECT_DIR}"
  exit 2
fi

"${EXECUTABLE}" check --env "${CONFIG}"

mkdir -p "${HOME}/Library/LaunchAgents" "${DATA_DIR}"
sed \
  -e "s|__EXECUTABLE__|${EXECUTABLE}|g" \
  -e "s|__CONFIG__|${CONFIG}|g" \
  -e "s|__HOME__|${HOME}|g" \
  -e "s|__DATA_DIR__|${DATA_DIR}|g" \
  -e "s|__PATH__|${TOOL_PATH}|g" \
  "${PLIST_TEMPLATE}" > "${PLIST_TARGET}"

launchctl bootout "gui/$(id -u)" "${PLIST_TARGET}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_TARGET}"
launchctl kickstart -k "gui/$(id -u)/com.codex-telegram-remote"

print "已安装并启动 com.codex-telegram-remote"
print "查看状态：launchctl print gui/$(id -u)/com.codex-telegram-remote"
print "查看日志：tail -f '${DATA_DIR}/codex-telegram-remote.log'"
