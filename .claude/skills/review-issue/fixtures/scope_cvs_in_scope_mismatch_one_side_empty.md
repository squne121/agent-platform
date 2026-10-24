---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: one side empty fixture (no warning)
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "one side empty no warning test"
change_kind: code
```

## Outcome

Add initial implementation for the new module.

## Current Validated Scope

- (なし)
- まだ検証されたスコープはありません

## In Scope

- src/newmodule.py を新規実装する
- docs/newmodule.md を追加する

## Acceptance Criteria

- [ ] AC1: `src/newmodule.py` が存在する

## Verification Commands

```bash
# AC1
$ test -f src/newmodule.py
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

- `src/newmodule.py`
