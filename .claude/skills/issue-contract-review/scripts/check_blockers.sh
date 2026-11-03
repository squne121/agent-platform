#!/usr/bin/env bash
# check_blockers.sh — blocker/dependency 全 close 確認
# Usage: check_blockers.sh <issue_number> [<owner>/<repo>]
#
# Exit codes:
#   0 — blocker が 0 件、または全て closed（go 判定へ継続）
#   1 — blocker が 1 件以上 open、または native / fallback 不一致、
#       または native API 失敗 + fallback なし（human_escalation）
#
# 環境変数:
#   GH_BIN — gh コマンドのパス（デフォルト: gh）。fake gh に差し替え可能。

set -euo pipefail

GH_BIN="${GH_BIN:-gh}"

ISSUE_NUMBER="${1:?Usage: check_blockers.sh <issue_number> [<owner>/<repo>]}"
REPO="${2:-}"

if [[ -z "$REPO" ]]; then
  REPO=$(git remote get-url origin 2>/dev/null | sed 's/.*github.com[:/]//' | sed 's/\.git$//') || true
fi

if [[ -z "$REPO" ]]; then
  echo "ERROR: could not determine repository. Pass <owner>/<repo> as second argument." >&2
  exit 1
fi

# ---------- helper: get issue state ----------
get_issue_state() {
  local num="$1"
  "${GH_BIN}" issue view "$num" --repo "$REPO" --json state --jq '.state' 2>/dev/null || echo "UNKNOWN"
}

# ---------- helper: sort and deduplicate numeric list ----------
sort_unique() {
  printf '%s\n' "$@" | sed '/^$/d' | sort -n | uniq
}

# ---------- 1. native dependency API (primary) ----------
native_blockers=()
native_api_available=false
native_api_failed=false

native_api_err_file=$(mktemp)
trap 'rm -f "$native_api_err_file"' EXIT

if raw="$("${GH_BIN}" api "repos/${REPO}/issues/${ISSUE_NUMBER}/dependencies/blocked_by" --paginate 2>"$native_api_err_file")"; then
  native_api_available=true
  while IFS= read -r num; do
    [[ -z "$num" ]] && continue
    native_blockers+=("$num")
  done < <(echo "$raw" | jq -r '.[].number // empty' 2>/dev/null || true)
else
  native_api_failed=true
fi

# ---------- 2. Depends on #N fallback ----------
issue_body=$("${GH_BIN}" issue view "$ISSUE_NUMBER" --repo "$REPO" --json body --jq '.body' 2>/dev/null || echo "")

fallback_blockers=()

# Pattern 1: inline "Depends on #N" (case-insensitive)
while IFS= read -r num; do
  [[ -z "$num" ]] && continue
  fallback_blockers+=("$num")
done < <(echo "$issue_body" | grep -oP '(?i)Depends on #\K[0-9]+' || true)

# Pattern 2: "## Depends On" section — lines starting with "- #N" or "* #N"
in_depends_section=false
while IFS= read -r line; do
  if echo "$line" | grep -qiP '^\s*#{1,6}\s+Depends\s+On\s*$'; then
    in_depends_section=true
    continue
  fi
  # Stop at the next heading
  if $in_depends_section && echo "$line" | grep -qP '^\s*#'; then
    in_depends_section=false
  fi
  if $in_depends_section; then
    num=$(echo "$line" | grep -oP '(?<=[-*]\s+#)[0-9]+' || true)
    [[ -z "$num" ]] && continue
    fallback_blockers+=("$num")
  fi
done < <(echo "$issue_body")

# ---------- 3. native API 失敗時の処理 ----------
if $native_api_failed; then
  if [[ ${#fallback_blockers[@]} -eq 0 ]]; then
    echo "human_escalation: native dependency API unavailable and no 'Depends on #N' fallback found." >&2
    cat "$native_api_err_file" >&2
    exit 1
  else
    echo "WARNING: native dependency API unavailable. Falling back to 'Depends on #N' in issue body." >&2
    cat "$native_api_err_file" >&2
  fi
fi

# ---------- 4. mismatch check（双方向・集合一致） ----------
if $native_api_available && [[ ${#fallback_blockers[@]} -gt 0 ]]; then
  native_sorted=$(sort_unique "${native_blockers[@]+"${native_blockers[@]}"}")
  fallback_sorted=$(sort_unique "${fallback_blockers[@]}")

  if [[ "$native_sorted" != "$fallback_sorted" ]]; then
    echo "human_escalation: native dependency と 'Depends on #N' が不一致です。" >&2
    echo "  native blockers : $(echo "$native_sorted" | tr '\n' ' ')" >&2
    echo "  fallback (body) : $(echo "$fallback_sorted" | tr '\n' ' ')" >&2
    exit 1
  fi
fi

# ---------- 5. 判定対象 blocker リストを決定 ----------
if $native_api_available; then
  blockers=("${native_blockers[@]+"${native_blockers[@]}"}")
else
  blockers=("${fallback_blockers[@]+"${fallback_blockers[@]}"}")
fi

if [[ ${#blockers[@]} -eq 0 ]]; then
  echo "OK: blocker なし（Issue #${ISSUE_NUMBER} は着手可能）"
  exit 0
fi

# ---------- 6. 各 blocker の state を確認 ----------
open_blockers=()
for num in "${blockers[@]}"; do
  state=$(get_issue_state "$num")
  if [[ "$state" != "CLOSED" ]]; then
    open_blockers+=("$num (state=$state)")
  fi
done

if [[ ${#open_blockers[@]} -eq 0 ]]; then
  echo "OK: 全 blocker が closed（Issue #${ISSUE_NUMBER} は着手可能）"
  exit 0
else
  echo "human_escalation: blocker が open です。着手不可。" >&2
  for b in "${open_blockers[@]}"; do
    echo "  open blocker: #${b}" >&2
  done
  exit 1
fi
