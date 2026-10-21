# Test Issue: Ambiguous scope signal

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "ambiguous prose does not become schema failure"
change_kind: research-only
```

## Outcome

This issue has ambiguous scope markers that don't match any known pattern.

## In Scope

- Ambiguous area that might be scope expansion
- Area with unclear boundaries

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Ambiguous signals are detected
- AC2: Handled appropriately with fail_closed

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
