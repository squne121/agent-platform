# Test Issue: New Allowed Path Layer Detection

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "allowed path layer detection identifies top-level spread"
change_kind: research-only
```

## Outcome

This issue requires changes across different path layers.

## Parent Issue

none

## Allowed Paths

- `.claude/skills/my-skill/scripts/main.py`
- `src/components/MyComponent.tsx`

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Allowed paths layer detection works
- AC2: Multiple top-level directories are identified

## Verification Commands

```bash
echo "verify path layers"
```

## Stop Conditions

- Outcome / Scope / AC が検索可能な形で記載されていない場合は即停止
- Allowed Paths 外への書き込みを試みた場合は即停止
- 権限不足により操作が完了できない場合は即停止
- 成果物の書き込みに失敗した場合は即停止

## Handoff Contract

- `Current Objective`
- `Bounded Current Context`
- `Open Questions`
- `Next Action`
- `Artifact Refs`
