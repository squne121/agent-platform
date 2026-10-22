# Implement: Movement Balance Updates

Implementation of updated movement speeds based on playtest feedback.

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: enhancement
```

## Outcome

Movement balance parameters updated in code and documented in product spec.

## Acceptance Criteria

- [ ] AC1: Movement speed constants updated in `src/data/movement-config.ts`
- [ ] AC2: `docs/product/game-logic.md` updated with new parameters
- [ ] AC3: Tests pass with new values

## Verification Commands

```bash
$ test -f src/data/movement-config.ts
$ grep -n "MOVE_SPEED" src/data/movement-config.ts
$ test -f docs/product/game-logic.md
```

## Allowed Paths

- `src/data/movement-config.ts`
- `docs/product/game-logic.md`
- `tests/movement-balance.test.ts`

## Stop Conditions

- None
