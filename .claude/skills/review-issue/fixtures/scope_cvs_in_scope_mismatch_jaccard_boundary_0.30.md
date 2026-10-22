---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: Jaccard boundary jaccard=0.30 fixture (no warning expected)
---
<!-- Jaccard boundary fixture: jaccard = 0.30 (== threshold, NOT < threshold → no warning).
Design (path tokens only, lowercase after normalization):
  CVS path tokens:       src/shared_alpha.py, src/shared_beta.ts, src/shared_gamma.json,
                         src/only_cvs_p.ts, src/only_cvs_q.ts, src/only_cvs_r.py, src/only_cvs_s.ts
  In Scope path tokens:  src/shared_alpha.py, src/shared_beta.ts, src/shared_gamma.json,
                         docs/only_in_u.md, docs/only_in_v.md, docs/only_in_w.md
  overlap = 3, |CVS|=7, |InScope|=6, union = 3+4+3 = 10
  jaccard = 3/10 = 0.30 — not < 0.3 → no warning.
Note: significant tokens (from prose) may shift the exact value slightly.
The fixture prose is kept minimal to avoid adding significant tokens. -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "jaccard boundary 0.30 no-warning test"
change_kind: code
```

## Outcome

共有モジュールを更新する。

## Current Validated Scope

- src/shared_alpha.py を改修する
- src/shared_beta.ts を更新する
- src/shared_gamma.json を整理する
- src/only_cvs_p.ts を削除する
- src/only_cvs_q.ts を削除する
- src/only_cvs_r.py を削除する
- src/only_cvs_s.ts を削除する

## In Scope

- src/shared_alpha.py のメソッドを追加する
- src/shared_beta.ts のバグを修正する
- src/shared_gamma.json の設定を更新する
- docs/only_in_u.md を追加する
- docs/only_in_v.md を追加する
- docs/only_in_w.md を追加する

## Acceptance Criteria

- [ ] AC1: `src/shared_alpha.py` が存在する

## Verification Commands

```bash
# AC1
$ test -f src/shared_alpha.py
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

- `src/shared_alpha.py`
