#!/usr/bin/env bash
# Thin wrapper - delegates all logic to wait_ci_checks.py
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$SCRIPT_DIR/wait_ci_checks.py"

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$PY" "$@"
elif command -v uv >/dev/null 2>&1; then
  exec uv run python3 "$PY" "$@"
else
  echo 'CI_WAIT_RESULT_V1_JSON={"schema":"CI_WAIT_RESULT_V1","status":"gh_error","repo":"","pr_number":0,"head_sha":"","current_head_sha":"","required_only":true,"checks":[],"elapsed_seconds":0,"interval_seconds":0,"timeout_seconds":0,"error_code":"runtime_unavailable","message":"python runtime not found"}'
  exit 2
fi
