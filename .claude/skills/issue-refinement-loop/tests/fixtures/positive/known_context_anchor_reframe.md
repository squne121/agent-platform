# Test Issue: Known context anchor reframe

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "known_context anchor reframe keeps scope stable"
change_kind: research-only
```

## Outcome

Internal refactoring that was reviewed and reframed in context of existing scope.

## In Scope

- Internal module reorganization
- Existing paths remain in scope

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Known context anchor reframe is applied
- AC2: Evidence sources include known_context

## Verification Commands

```bash
uv run pytest tests/ -v
```

Reference: Previously reviewed with scope confirmation that this doesn't expand beyond module boundaries.

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
