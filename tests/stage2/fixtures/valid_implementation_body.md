## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: "テスト用"
change_kind: code
```

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため



## Parent Issue

none

## Parent Goal Ref

- Goal: なし
- Desired Destination: N/A

## Current Validated Scope

- テスト実装

## Remaining Parent Gaps

なし

## Outcome

テストが PASS している状態

## In Scope

- テスト実装

## Out of Scope

- 本番実装

## Acceptance Criteria

- [ ] AC1 テストが PASS する
- [ ] AC2 型エラーがない

## Verification Commands

```bash
# AC1
$ uv run pytest tests/ -v
# AC2
$ pnpm typecheck
```

## Allowed Paths

- tests/test_example.py

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約（キー集合・スキーマ・型定義）の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合（スコープ分割が発生する場合）
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Required Design References

- docs/dev/agent-skill-boundaries.md

## Required Skills

- Python / pytest
