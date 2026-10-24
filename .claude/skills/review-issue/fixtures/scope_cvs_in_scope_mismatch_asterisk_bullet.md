---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: asterisk bullet fixture (no warning)
---
<!-- Fixture 2: asterisk bullet marker "* src/foo.py" (Blocker 2).
Same path appears on both CVS and In Scope sides (via * bullets) → no warning. -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "asterisk bullet no-warning test"
change_kind: code
```

## Outcome

src/foo.py と src/bar.ts を両側で一致させる。

## Current Validated Scope

* src/foo.py を改修する
* src/bar.ts を更新する

## In Scope

* src/foo.py のロジックを追加する
* src/bar.ts のバグを修正する

## Acceptance Criteria

- [ ] AC1: `src/foo.py` が存在する

## Verification Commands

```bash
# AC1
$ test -f src/foo.py
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

- `src/foo.py`
