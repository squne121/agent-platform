# Tasks.md Direct Implementation with source_task_id

## Allowed Paths
- src/systems/movement.ts

## Machine-Readable Contract
```yaml
source_task_id: REQ-001-T001
issue_kind: implementation
```

## Summary
This issue mentions tasks.md as direct implementation source with explicit "Implementation source: tasks.md" text.

Even though it has source_task_id in the contract, the direct implementation source language makes this fail PS002.

Implementation source: tasks.md

The tasks.md file contains our implementation targets that we will directly implement from.
