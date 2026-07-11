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

export CODEX_RESUME_SESSION_ID="${session_id}"
export CODEX_RESUME_TARGET_CWD="${target_cwd}"
export CODEX_AUTO_RESUME_SCRIPT_DIR="${script_dir}"

command_payload=$(cat <<'EOF'
cd "$CODEX_RESUME_TARGET_CWD"
eval "$(python3 "$CODEX_AUTO_RESUME_SCRIPT_DIR/configure_config.py" --emit-shell-runtime)"
eval "$(python3 "$CODEX_AUTO_RESUME_SCRIPT_DIR/resolve_session_resume.py" "$CODEX_RESUME_SESSION_ID" --emit-shell-runtime)"
codex exec resume \
  -m "$CODEX_RESUME_MODEL" \
  -c "model_reasoning_effort=$CODEX_RESUME_EFFORT" \
  -c 'approval_policy="never"' \
  --dangerously-bypass-hook-trust \
  --skip-git-repo-check \
  "$CODEX_RESUME_SESSION_ID" \
  "continue"
EOF
)

log_info "starting silent resume for session ${session_id} in cwd ${target_cwd}"

exec /bin/bash -c "${command_payload}"
