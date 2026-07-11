#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
. "${script_dir}/lib.sh"

eval "$(python3 "${script_dir}/configure_config.py" --emit-shell-runtime)"

workspace_dir="${tmp_dir}/prewarm-workspace"
mkdir -p "${workspace_dir}"

log_info "starting workat prewarm probe"

exec codex exec \
  -m "gpt-5.4-mini" \
  -c "model_reasoning_effort=low" \
  -c 'approval_policy="never"' \
  --dangerously-bypass-hook-trust \
  --ephemeral \
  --ignore-rules \
  --skip-git-repo-check \
  -C "${workspace_dir}" \
  "Just say Hi"
