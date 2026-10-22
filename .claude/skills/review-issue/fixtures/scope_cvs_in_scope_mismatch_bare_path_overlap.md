---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: bare path overlap fixture (no warning)
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "bare path overlap no warning test"
change_kind: code
```

## Outcome

`src/shared.py` と `docs/design.md` を更新する。

## Current Validated Scope

- src/shared.py を更新する
- docs/design.md を改訂する
- src/helper.ts を整理する

## In Scope

- src/shared.py の API を拡張する
- docs/design.md に図を追加する
- src/helper.ts のテストを追加する

## Acceptance Criteria

- [ ] AC1: `src/shared.py` が更新されている

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
