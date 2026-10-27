#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="${PROJECT_DIR}/systemd/codex-telegram-remote.service.example"
TARGET_DIR="${HOME}/.config/systemd/user"
TARGET="${TARGET_DIR}/codex-telegram-remote.service"
CONFIG="${CODEX_REMOTE_CONFIG:-${HOME}/.config/codex-telegram-remote/config.env}"
DATA_DIR="${CODEX_REMOTE_DATA_HOME:-${XDG_STATE_HOME:-${HOME}/.local/state}/codex-telegram-remote}"
EXECUTABLE="$(command -v codex-telegram-remote || true)"

if [[ -z "${EXECUTABLE}" ]]; then
  echo "codex-telegram-remote is not installed in PATH" >&2
  exit 2
fi

mkdir -p "${TARGET_DIR}" "${DATA_DIR}"
"${EXECUTABLE}" check --env "${CONFIG}"

TOOL_PATH="${PATH}:/usr/local/bin:/usr/bin:/bin"
sed \
  -e "s|__EXECUTABLE__|${EXECUTABLE}|g" \
  -e "s|__CONFIG__|${CONFIG}|g" \
  -e "s|__HOME__|${HOME}|g" \
  -e "s|__DATA_DIR__|${DATA_DIR}|g" \
  -e "s|__PATH__|${TOOL_PATH}|g" \
  "${TEMPLATE}" > "${TARGET}"

systemctl --user daemon-reload
systemctl --user enable --now codex-telegram-remote.service
systemctl --user status codex-telegram-remote.service --no-pager
