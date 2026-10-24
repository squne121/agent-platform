---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: Japanese only fixture (no warning)
---
<!-- KNOWN LIMITATION: This fixture contains Japanese natural-language text only (no bare path tokens,
no backtick-quoted tokens). The scope_cvs tokenizer is limited to ASCII / English natural-language
tokens. Japanese text without path/backtick tokens yields 0 tokens on both sides, so the mismatch
detector does not fire (guarded by the "if not cvs_tokens or not in_scope_tokens: return" check).
Japanese path-only divergence can be detected if a bare ASCII path is present — see
scope_cvs_in_scope_mismatch_japanese_path_divergence.md for that case. -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "japanese only no warning test"
change_kind: code
```

## Outcome

日本語テキストのみのセクションで警告が出ないことを確認する。

## Current Validated Scope

- ユーザー認証モジュールを改修する
- パスワードハッシュアルゴリズムを更新する
- セッション管理を改善する

## In Scope

- データベーススキーマを再設計する
- キャッシュレイヤーを実装する
- モニタリングダッシュボードを構築する

## Acceptance Criteria

- [ ] AC1: `src/auth.py` が存在する

## Verification Commands

```bash
# AC1
$ test -f src/auth.py
```

## Stop Conditions

- 1
- 2
- 3
- 4
- 5
- 6

## Runtime Verification Applicability

decision: not_applicable
reason: fixture only.

## Allowed Paths

- `src/auth.py`
