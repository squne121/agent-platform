# Test Issue: Missing Outcome section

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "missing Outcome still fail-closes with contract present"
change_kind: research-only
```

## Parent Issue

none

## In Scope

- Feature implementation without outcome definition

## Acceptance Criteria

- AC1: Missing sections are detected
- AC2: Fail closed appropriately

## Verification Commands

```bash
uv run pytest tests/ -v
```

## Out of Scope

- 実装コード変更

## Stop Conditions

- Outcome / Scope / AC が検索可能な形で記載されていない場合は即停止
- Allowed Paths 外への書き込みを試みた場合は即停止
- 権限不足により操作が完了できない場合は即停止
- 成果物の書き込みに失敗した場合は即停止

## Allowed Paths

- なし

## Handoff Contract

- `Current Objective`
- `Bounded Current Context`
- `Open Questions`
- `Next Action`
- `Artifact Refs`
