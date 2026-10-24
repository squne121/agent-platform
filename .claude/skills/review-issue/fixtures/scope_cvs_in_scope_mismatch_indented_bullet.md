---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: indented bullet fixture (no warning)
---
<!-- Fixture 4: indented bullet "  - src/foo.py" (Blocker 2).
Same path on both sides via indented bullet (2 leading spaces) → no warning. -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "indented bullet no-warning test"
change_kind: code
```

## Outcome

src/foo.py の両側での同期を確認する。

## Current Validated Scope

  - src/foo.py を改修する
  - src/baz.json を更新する

## In Scope

  - src/foo.py のメソッドを追加する
  - src/baz.json の設定を更新する

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
