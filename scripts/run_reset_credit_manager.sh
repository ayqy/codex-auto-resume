#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$APP_DIR/tmp/reset-credit"
mkdir -p "$RUNTIME_DIR"

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.cargo/bin"
CODEX_BIN="${CODEX_BIN:-/usr/local/bin/codex}"
if [[ ! -x "$CODEX_BIN" ]]; then
  CODEX_BIN="$(command -v codex)"
fi

PYTHON_BIN="${PYTHON_BIN:-$HOME/.venv/bin/python3}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="/usr/bin/python3"
fi

eval "$("$PYTHON_BIN" "$APP_DIR/scripts/configure_config.py" --emit-shell-runtime)"

exec /usr/bin/caffeinate -dimsu \
  "$PYTHON_BIN" "$APP_DIR/scripts/reset_credit_manager.py" watch --codex-bin "$CODEX_BIN" \
  >>"$RUNTIME_DIR/launch-agent.log" 2>&1
