---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: Jaccard boundary jaccard=0.25 fixture (warning expected)
---
<!-- Jaccard boundary fixture: jaccard ≈ 0.25 (< 0.3 threshold → warning fires).
Design: CVS = {shared.py, only_cvs_a.ts, only_cvs_b.ts}  (path tokens)
        In Scope = {shared.py, only_in_a.md}
        overlap = {shared.py} = 1
        union = {shared.py, only_cvs_a.ts, only_cvs_b.ts, only_in_a.md} = 4
        jaccard = 1/4 = 0.25 < 0.3 → warning -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "jaccard boundary 0.25 test"
change_kind: code
```

## Outcome

共有モジュールを部分的に更新する。

## Current Validated Scope

- src/shared.py を改修する
- src/only_cvs_a.ts を更新する
- src/only_cvs_b.ts を整理する

## In Scope

- src/shared.py のメソッドを追加する
- docs/only_in_a.md を新規追加する

## Acceptance Criteria

- [ ] AC1: `src/shared.py` が存在する

## Verification Commands

```bash
# AC1
$ test -f src/shared.py
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

- `src/shared.py`
