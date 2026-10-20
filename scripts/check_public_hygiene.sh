#!/usr/bin/env bash
# Public 衛生検査。git grep の exit code を区別する:
#   1 = 禁止パターン無し(成功) / 0 = 検出(失敗) / 2以上 = 実行エラー(失敗)
set -u
cd "${1:-.}" || exit 2

files=$(git ls-files -- . ':!uv.lock' ':!scripts/check_public_hygiene.sh' ':!tests/smoke/**' | wc -l)
if [ "$files" -lt 1 ]; then
  echo "hygiene: no tracked files were checked" >&2
  exit 2
fi

pattern='(/home/[A-Za-z0-9_-]+/|/Users/[A-Za-z0-9_-]+/|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|session[_-]?id[=:])'
git grep -nIE "$pattern" -- . ':!uv.lock' ':!scripts/check_public_hygiene.sh' ':!tests/smoke/**'
rc=$?
case "$rc" in
  1) echo "hygiene: OK ($files files checked)"; exit 0 ;;
  0) echo "hygiene: forbidden pattern found" >&2; exit 1 ;;
  *) echo "hygiene: search error (exit $rc)" >&2; exit 2 ;;
esac
