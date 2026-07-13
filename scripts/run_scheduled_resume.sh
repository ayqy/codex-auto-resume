#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
. "${script_dir}/lib.sh"

if [[ $# -lt 2 ]]; then
  log_info "missing session id or cwd"
  exit 1
fi

session_id="$1"
target_cwd="$2"
shell_bin="${SHELL:-/bin/bash}"
post_resume_shell="${CODEX_AUTO_RESUME_INTERACTIVE_SHELL:-${shell_bin}}"

export CODEX_RESUME_SESSION_ID="${session_id}"
export CODEX_RESUME_TARGET_CWD="${target_cwd}"
export CODEX_AUTO_RESUME_SCRIPT_DIR="${script_dir}"
export CODEX_AUTO_RESUME_POST_SHELL="${post_resume_shell}"

command_payload=$(cat <<'EOF'
if ! cd "$CODEX_RESUME_TARGET_CWD"; then
  printf 'failed to cd into %s\n' "$CODEX_RESUME_TARGET_CWD" >&2
  if [[ -x "$CODEX_AUTO_RESUME_POST_SHELL" ]]; then
    exec "$CODEX_AUTO_RESUME_POST_SHELL" -il
  fi
  exec /bin/bash -il
fi
eval "$(python3 "$CODEX_AUTO_RESUME_SCRIPT_DIR/configure_config.py" --emit-shell-runtime)"
eval "$(python3 "$CODEX_AUTO_RESUME_SCRIPT_DIR/resolve_session_resume.py" "$CODEX_RESUME_SESSION_ID" --emit-shell-runtime)"
set +e
codex resume -m "$CODEX_RESUME_MODEL" -c "model_reasoning_effort=$CODEX_RESUME_EFFORT" --yolo "$CODEX_RESUME_SESSION_ID" "continue"
resume_status=$?
set -e
cd "$CODEX_RESUME_TARGET_CWD" || true
if [[ -x "$CODEX_AUTO_RESUME_POST_SHELL" ]]; then
  exec "$CODEX_AUTO_RESUME_POST_SHELL" -il
fi
exec /bin/bash -il
EOF
)

log_info "starting resume for session ${session_id} in cwd ${target_cwd}"

if command -v "${shell_bin}" >/dev/null 2>&1; then
  exec "${shell_bin}" -il -c "${command_payload}"
fi

exec /bin/bash -il -c "${command_payload}"
