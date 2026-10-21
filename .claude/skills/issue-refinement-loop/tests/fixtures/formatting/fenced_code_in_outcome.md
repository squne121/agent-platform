# Test Issue: Fenced code in outcome

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "real paths stay extractable while fenced examples are ignored"
change_kind: research-only
```

## Outcome

Implementation requires updates to actual files, excluding examples:

Real files:
- `src/real/file.ts`
- `src/utils/Real.ts`

Code examples (should be excluded):
```typescript
// Example paths that should NOT be extracted:
src/example/NotReal.ts
import { helper } from 'src/example/NotReal';
```

The actual implementation files are listed above without code fencing.

## In Scope

- Real source file updates
- Exclude example code paths

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Fenced code paths excluded
- AC2: Real paths included

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
