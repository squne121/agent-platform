# Test Issue: Markdown table body paths

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "markdown table paths remain extractable"
change_kind: research-only
```

## Outcome

Extract paths from markdown formatted tables.

| Component | Path | Status |
|-----------|------|--------|
| Skills | `.claude/skills/refinement` | Done |
| Source | `src/components/Button.tsx` | WIP |
| Tests | `tests/unit/button.test.ts` | TBD |

## In Scope

- Multi-path extraction from tables
- Format handling

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Paths in tables are extracted
- AC2: Table markdown doesn't break extraction

## Verification Commands

```bash
uv run pytest tests/ -v
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
