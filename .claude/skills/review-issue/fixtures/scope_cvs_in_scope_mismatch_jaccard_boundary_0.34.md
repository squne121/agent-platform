---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: Jaccard boundary jaccard=0.34 fixture (no warning expected)
---
<!-- Jaccard boundary fixture: jaccard ≈ 0.34 (> 0.3 threshold → no warning).
Design (path tokens only):
  CVS path tokens:      src/apex.py, src/beta.ts, docs/guide.md
  In Scope path tokens: src/apex.py, docs/guide.md, docs/extra.md
  overlap = {src/apex.py, docs/guide.md} = 2
  |CVS|=3, |InScope|=3, union = 3+3-2 = 4
  jaccard = 2/4 = 0.5 — well above 0.3, no warning.
Note: prose is kept minimal to avoid extra significant tokens that shift jaccard. -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "jaccard boundary 0.34 no-warning test"
change_kind: code
```

## Outcome

頂点モジュールとガイドを更新する。

## Current Validated Scope

- src/apex.py を改修する
- src/beta.ts を更新する
- docs/guide.md を改訂する

## In Scope

- src/apex.py のロジックを追加する
- docs/guide.md のサンプルを更新する
- docs/extra.md を新規追加する

## Acceptance Criteria

- [ ] AC1: `src/apex.py` が存在する

## Verification Commands

```bash
# AC1
$ test -f src/apex.py
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

- `src/apex.py`
