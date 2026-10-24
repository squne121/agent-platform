---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: Jaccard boundary jaccard=0 fixture (warning expected)
---
<!-- Jaccard boundary fixture: jaccard ≈ 0 (complete disjoint).
CVS tokens and In Scope tokens have no overlap → warning fires. -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "jaccard boundary 0 test"
change_kind: code
```

## Outcome

スコープ完全不一致のケース。

## Current Validated Scope

- src/alpha.py を改修する
- src/beta.ts を更新する
- src/gamma.json を整理する

## In Scope

- docs/delta.md を追加する
- docs/epsilon.md を整備する
- docs/zeta.md を作成する

## Acceptance Criteria

- [ ] AC1: `docs/delta.md` が存在する

## Verification Commands

```bash
# AC1
$ test -f docs/delta.md
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

- `docs/delta.md`
