#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="$(cd "${script_dir}/.." && pwd)"
tmp_dir="${app_dir}/tmp"
log_dir="${tmp_dir}/logs"

mkdir -p "${log_dir}"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

log_info() {
  printf '[%s] %s\n' "$(timestamp)" "$*"
}
