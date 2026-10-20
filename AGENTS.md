# agent-platform

AI 開発基盤の Public repository。

## 開発規約
- Python 環境は uv で管理する: `uv sync --locked`
- テスト: `uv run --locked pytest -q tests/smoke`
- lint: `uv run --locked ruff check . && uv run --locked ruff format --check .`
- Public 衛生検査: `bash scripts/check_public_hygiene.sh`
- 絶対ホームパス・token・session ID・内部 URL を commit しない。
- 変更は Issue 単位の branch / PR で行い、primary checkout では編集しない。
