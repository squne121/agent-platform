# Test Issue: Internal design patterns update

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "internal docs should not trigger external research"
change_kind: research-only
```

## Outcome

Update the internal design patterns guide in our docs.

## In Scope

- docs/dev/design-patterns.md updates
- Internal maintainance

## Parent Issue

none

## Out of Scope

- 外部仕様調査

## Acceptance Criteria

- AC1: Updates don't require external verification
- AC2: Changes are internal only

## Verification Commands

```bash
uv run pytest tests/ -v
```

This issue is about our internal docs and doesn't require external research.

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
