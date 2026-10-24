---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: punctuation normalization fixture (no warning)
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "punctuation normalization no warning test"
change_kind: code
```

## Outcome

Update shared utilities across the codebase.

## Current Validated Scope

- src/utils/helper.py, src/utils/formatter.ts を更新する
- docs/api-guide.md, docs/user-guide.md を改訂する
- src/config.json を更新する

## In Scope

- src/utils/helper.py のメソッドを追加する
- src/utils/formatter.ts のバグを修正する
- docs/api-guide.md に新しいエンドポイントを追記する
- docs/user-guide.md のサンプルを更新する
- src/config.json の設定値を更新する

## Acceptance Criteria

- [ ] AC1: `src/utils/helper.py` が更新されている

## Verification Commands

```bash
# AC1
$ test -f src/utils/helper.py
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

- `src/utils/helper.py`
