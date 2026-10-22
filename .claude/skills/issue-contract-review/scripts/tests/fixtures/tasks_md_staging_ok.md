# Implement: Movement Task Group A

Implementation of first movement task group materialized from tasks.md staging artifact.

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: feature
source_task_id: movement-task-001
requirement_id: REQ-movement-001
```

## Outcome

Movement task group A implemented with traceability to source tasks.md.

## Acceptance Criteria

- [ ] AC1: Movement controllers implemented
- [ ] AC2: Tests for movement input handling pass
- [ ] AC3: source_task_id / requirement_id preserved in PR

## Verification Commands

```bash
$ grep -n "source_task_id: movement-task-001" <(gh pr view --json body)
$ pnpm test -- movement
```

## Allowed Paths

- `src/systems/movement.ts`
- `tests/movement.test.ts`

## Stop Conditions

- None

## Notes

- tasks.md is a staging artifact; GitHub Issues are the tracking SSOT
- This Issue was materialized via taskstoissues equivalent
