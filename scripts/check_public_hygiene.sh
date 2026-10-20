#!/usr/bin/env bash
# Public 衛生検査。git grep の exit code を区別する:
#   1 = 禁止パターン無し(成功) / 0 = 検出(失敗) / 2以上 = 実行エラー(失敗)
set -u
cd "${1:-.}" || exit 2

excludes=(':!uv.lock' ':!scripts/check_public_hygiene.sh' ':!tests/smoke/test_smoke.py')
# 実際に grep される(テキスト、内容あり)ファイル数
files=$(git grep -Il '' -- . "${excludes[@]}" | wc -l)
if [ "$files" -lt 1 ]; then
  echo "hygiene: no tracked files were checked" >&2
  exit 2
fi

# 除外: uv.lock(生成物)、本スクリプト(pattern を含む)、smoke test(禁止文字列 fixture を分割して持つ)
pattern='(/home/[A-Za-z0-9_-]+|/root/|/Users/[A-Za-z0-9_-]+/|[A-Za-z]:\\Users\\|gh[pos]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[bp]-[A-Za-z0-9-]{10,}|session[_-]?id.?[=:]|https?://[A-Za-z0-9.-]+\.(internal|corp|local)([:/]|$))'
git grep -nIiE "$pattern" -- . "${excludes[@]}"
rc=$?
case "$rc" in
  1) echo "hygiene: OK ($files files checked)"; exit 0 ;;
  0) echo "hygiene: forbidden pattern found" >&2; exit 1 ;;
  *) echo "hygiene: search error (exit $rc)" >&2; exit 2 ;;
esac
