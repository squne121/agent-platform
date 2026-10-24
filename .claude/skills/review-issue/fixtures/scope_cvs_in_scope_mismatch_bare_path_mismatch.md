---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: bare path mismatch fixture
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "bare path mismatch warning test"
change_kind: code
```

## Outcome

`src/alpha.py` を改修する。

## Current Validated Scope

- docs/old-design.md を参照する
- src/legacy.py を削除する
- src/old_util.ts を整理する

## In Scope

- docs/new-design.md を新規追加する
- src/feature/main.py を実装する
- src/feature/helper.ts を実装する

## Acceptance Criteria

- [ ] AC1: `src/feature/main.py` が存在する

## Verification Commands

```bash
# AC1
$ test -f src/feature/main.py
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

- `src/feature/main.py`
