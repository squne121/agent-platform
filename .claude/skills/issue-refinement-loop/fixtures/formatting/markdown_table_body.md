# Test Issue: markdown_table_body

```yaml
contract_schema_version: v1
issue_kind: implementation
goal_ref: "Test goal"
```

## Outcome

Test issue with markdown tables in body containing file paths.

## In Scope

- Path extraction from tables

## Allowed Paths

| Path | Description |
|---|---|
| `.claude/skills/test/` | Test skill |
| `src/components/` | Components |

## Acceptance Criteria

- [ ] AC1: Table paths `.claude/skills/test/` are extracted
- [ ] AC2: Table paths `src/components/` are extracted

## Verification Commands

Table extraction tests
