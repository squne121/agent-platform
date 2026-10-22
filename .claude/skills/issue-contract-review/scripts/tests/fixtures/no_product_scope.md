# Feature: New Movement System

Implementation of basic character movement and projectile mechanics.

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: feature
```

## Outcome

Movement and projectile system implemented and tested.

## Acceptance Criteria

- [ ] AC1: Movement logic in `src/systems/movement.ts`
- [ ] AC2: Projectile collision detection working

## Verification Commands

```bash
$ pnpm typecheck
$ pnpm test -- movement
```

## Allowed Paths

- `src/systems/movement.ts`
- `src/systems/projectile.ts`
- `tests/movement.test.ts`

## Stop Conditions

- None
