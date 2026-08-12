#!/usr/bin/env bash
set -euo pipefail

LABEL="com.ayqy.codex-auto-resume.reset-credit"
DOMAIN="gui/$(id -u)"
STATE_DIR="${CODEX_RESET_RUNTIME_DIR:-$HOME/Library/Application Support/codex-auto-resume/reset-credit/state}"
STATUS_PATH="$STATE_DIR/status.json"
MONITOR_LOG="$STATE_DIR/monitor.log"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
umask 077

while true; do
  NOW="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  if [[ -s "$STATUS_PATH" ]] && /usr/bin/grep -Eq '"status"[[:space:]]*:[[:space:]]*"(completed|expired|disabled)"' "$STATUS_PATH"; then
    printf '%s terminal-state; monitor-exit\n' "$NOW" >>"$MONITOR_LOG"
    exit 0
  fi
  LAUNCH_STATUS="$(/bin/launchctl print "$DOMAIN/$LABEL" 2>/dev/null || true)"
  if ! /usr/bin/grep -q 'state = running' <<<"$LAUNCH_STATUS"; then
    printf '%s launch-agent-not-running; kickstart\n' "$NOW" >>"$MONITOR_LOG"
    /bin/launchctl kickstart -k "$DOMAIN/$LABEL" >>"$MONITOR_LOG" 2>&1 || true
  elif [[ ! -s "$STATUS_PATH" ]]; then
    printf '%s status-missing; kickstart\n' "$NOW" >>"$MONITOR_LOG"
    /bin/launchctl kickstart -k "$DOMAIN/$LABEL" >>"$MONITOR_LOG" 2>&1 || true
  else
    printf '%s healthy\n' "$NOW" >>"$MONITOR_LOG"
  fi
  /bin/sleep 30
done
