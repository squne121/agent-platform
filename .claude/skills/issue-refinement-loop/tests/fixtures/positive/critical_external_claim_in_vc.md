# Test Issue: Critical external claims in VC

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "planner detects critical external claims in verification commands"
change_kind: research-only
```

## Outcome

Need to verify official CLI API behavior and authentication flow against published documentation.

## In Scope

- Analysis of current official API specification
- Validation of auth migration behavior

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: External claims are detected
- AC2: Web research policy is triggered appropriately

## Verification Commands

The following command requires external verification:
```bash
# Verify against official API documentation: https://api.example.com/docs
uv run pytest .claude/skills/issue-refinement-loop/tests/ -v
```

Additional requirement: Verify current CLI authentication flow matches the official spec documentation.

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
