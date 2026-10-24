---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: scope mismatch fixture
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test scope mismatch warning"
change_kind: code
```

## Outcome

`src/a.ts` を編集して `src/b.ts` のテストも通す。

## Current Validated Scope

- `src/old_file_a.ts` を改修する
- `src/old_file_b.ts` のテストを通す
- `src/old_file_c.ts` の型を整理する

## In Scope

- `src/totally_new_x.ts` を新規追加する
- `src/totally_new_y.ts` のテストを書く
- `src/totally_new_z.ts` のスキーマ定義

## Acceptance Criteria

- [ ] AC1: `src/totally_new_x.ts` が存在する

## Verification Commands

```bash
# AC1
$ test -f src/totally_new_x.ts
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
reason: fixture のみ。

## Allowed Paths

- `src/totally_new_x.ts`
