---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: Japanese text with path divergence fixture (warning expected)
---
<!-- SCOPE: ASCII / English natural-language tokens only. Japanese natural text alone yields 0 tokens.
However, bare ASCII paths embedded in Japanese text ARE detected by PATH_TOKEN_RE (path-only divergence).
This fixture verifies that case: Japanese prose + 1 bare ASCII path on each side, paths differ → warning fires. -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "japanese text with path divergence test"
change_kind: code
```

## Outcome

認証モジュールを改修して docs/auth-new.md を追加する。

## Current Validated Scope

- ユーザー認証モジュールを改修する（docs/auth-old.md を参照する）
- パスワードハッシュアルゴリズムを更新する

## In Scope

- データベーススキーマを再設計する（docs/schema-new.md を追加する）
- キャッシュレイヤーを実装する

## Acceptance Criteria

- [ ] AC1: `docs/auth-new.md` が存在する

## Verification Commands

```bash
# AC1
$ test -f docs/auth-new.md
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

- `docs/auth-new.md`
