# Test Issue: New In Scope Area Detection

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "scope detection identifies multiple in-scope layers"
change_kind: research-only
```

## Outcome

This issue involves updates across multiple framework layers.

## In Scope

- Changes to `.claude/skills` framework layer
- Updates to `docs/product` specification layer
- Both layers require modifications

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Scope detection identifies multiple layers
- AC2: Evidence is properly captured

## Verification Commands

```bash
echo "verify scope detection"
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
