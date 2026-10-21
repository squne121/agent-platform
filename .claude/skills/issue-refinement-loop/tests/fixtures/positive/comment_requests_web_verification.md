# Test Issue: Comment requests web verification

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "comments can trigger web research policy"
change_kind: research-only
```

## Outcome

Need to implement feature with external verification.

## In Scope

- Feature implementation
- Internal updates

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Comments trigger web research appropriately
- AC2: Evidence sources include comments

## Verification Commands

```bash
uv run pytest tests/ -v
```

Note: Reviewers have requested webで確認 in comments - verify behavior externally before merging.

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
