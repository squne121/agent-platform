# Implement: Movement Specification via Spec Kit

Implement movement system using Spec Kit specifications from .specify/ derived workbench.

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: feature
```

## Outcome

Movement system implemented using .specify/ as a derived workbench artifact, with docs/product as the SSOT.

## Acceptance Criteria

- [ ] AC1: Movement system implemented per spec
- [ ] AC2: .specify/ used as working artifact during development
- [ ] AC3: docs/product/game-logic.md is the primary reference

## Verification Commands

```bash
$ test -f src/systems/movement.ts
$ pnpm typecheck
```

## Allowed Paths

- `src/systems/movement.ts`
- `.specify/game-rules.yaml`

## Stop Conditions

- None

## Notes

.specify/ is a derived workbench artifact. docs/product/** is the SSOT (canonical source).
Updates should flow from docs/ → .specify/, not the reverse.
