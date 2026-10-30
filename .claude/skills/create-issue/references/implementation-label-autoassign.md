# Implementation Issue 標準ラベル自動付与

`create_issue_txn.py` は `--issue-kind implementation` が指定された場合、以下の標準ラベル 3 種を自動付与する。

## 標準ラベル一覧

| ラベル | 意味 |
|---|---|
| `phase/implementation` | 実装フェーズ |
| `agent/implementer` | AI implementer agent が担当 |
| `enhancement` | 新機能・改善要求 |

> **注意**: `state/queued` は標準ラベルから除外された（#211 対応）。
> `state/queued` は AI 着手可否の primary signal ではなく、着手可否は blocker/dependency の close 状態で判断する。
> `state/queued` は deprecated / legacy 扱いであり、AI 着手可否・VC・contract-review では参照禁止。

## 動作仕様

- `--issue-kind implementation` のときのみ自動付与される（`_resolve_labels` 関数）
- `research` / `parent` / `bug-report` など implementation 以外の種別、および種別未指定では付与されない
- 呼び出し元が `--label` で明示指定したラベルと重複なくマージされる（標準ラベルが先頭、呼び出し元ラベルが後続）
- `--issue-kind` を指定しない場合のデフォルトは空文字列（`""`）であり、自動付与は発動しない

## 使用方法

```bash
uv run python3 .claude/skills/create-issue/scripts/create_issue_txn.py \
  --repo owner/repo \
  --title "実装: <タイトル>" \
  --body-file issue_body.md \
  --issue-kind implementation \
  --parent-issue 40
```

上記実行時、`phase/implementation` / `agent/implementer` / `enhancement` の 3 ラベルが `gh issue edit --add-label` 経由で付与される。

## 背景

`.github/ISSUE_TEMPLATE/implementation.yml` の `labels:` フィールドは GitHub UI 経由起票時にのみ自動適用され、`gh issue create` 経由（`create_issue_txn.py`）では適用されない仕様になっている。そのため、スクリプト側でラベルを解決する必要がある。

本機能は Issue #61 の統合実装（PR #79 統合）として追加された。

## 関連

- `_resolve_labels()` — `create_issue_txn.py` 内のラベル解決関数
- `_IMPLEMENTATION_STANDARD_LABELS` — module-level 定数として定義されたラベルタプル
- `.claude/skills/create-issue/tests/test_create_issue_txn.py` — 単体テスト（AC8/AC9/AC10）
