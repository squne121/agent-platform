# Test Issue: Path extraction from fenced code

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "fenced-code paths are ignored"
change_kind: research-only
```

## Outcome

Need to implement path extraction, but don't extract paths from code examples.

## In Scope

- Documentation updates

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Fenced code paths are excluded
- AC2: Regular text paths are included

## Verification Commands

```bash
# Example of a path that should NOT be extracted in fenced code
# These are just examples in code blocks
uv run pytest .claude/skills/test_runner/tests/ -v
```

Code examples are not extracted paths.

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
