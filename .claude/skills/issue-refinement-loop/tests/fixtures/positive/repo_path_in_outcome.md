# Test Issue: Extract target_paths from Outcome

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "planner extracts target paths from Outcome"
change_kind: research-only
```

## Outcome

This Issue requires updates to the following repository paths:
- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py`
- `src/components/Button.tsx`
- `tests/unit/button.test.ts`

## In Scope

- Modification of refinement loop logic
- Addition of new schema validation

## Parent Issue

none

## Out of Scope

- なし

## Acceptance Criteria

- AC1: The planner extracts all target_paths correctly
- AC2: Paths are sorted and deduplicated
- AC3: Exit code is 0 on success

## Verification Commands

```bash
uv run pytest .claude/skills/issue-refinement-loop/tests/ -v
```

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
