# Test Issue: Anchor reframe with scope signal exclusion

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "known anchor reframe excludes false scope expansion signals"
change_kind: research-only
```

## Outcome

Reviewed in #123456 with scope reframe: scope is not expanding beyond the explicitly designed boundaries even though internal structure changes.

## In Scope

- Changes to `.claude/skills` layer (refactoring)
- Updates to `docs/product` layer (coordination)
- API surface remains stable

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Scope signals are correctly excluded by anchor reframe
- AC2: Both manual and API changes are in scope

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
