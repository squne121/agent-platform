#!/usr/bin/env bash
# Thin wrapper — delegates all logic to get_ci_failed_log.py
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$SCRIPT_DIR/get_ci_failed_log.py"

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$PY" "$@"
elif command -v uv >/dev/null 2>&1; then
  exec uv run python3 "$PY" "$@"
else
  echo "CI_FAILED_LOG_RESULT_V1_JSON: {\"status\":\"runtime_unavailable\",\"run_id\":null,\"attempt\":null,\"head_sha\":\"\",\"workflow_name\":null,\"failed_jobs\":[],\"retrieval_method\":null,\"redaction_applied\":false,\"truncated\":false}"
  exit 2
fi
