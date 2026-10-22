# Implement: Game Balance Updates

Implement game balance updates using .specify/game-balance.yaml as the canonical SSOT.

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: enhancement
```

## Outcome

Game balance updated by reading from .specify/game-balance.yaml (canonical spec source), overriding docs/product.

## Acceptance Criteria

- [ ] AC1: Implement all balance changes from .specify/game-balance.yaml as the source of truth
- [ ] AC2: .specify/ is the SSOT for game parameters (canonical source)
- [ ] AC3: docs/product values are derived from .specify/ (not the reverse)

## Verification Commands

```bash
$ test -f src/data/balance.ts
$ grep -n ".specify" README.md
```

## Allowed Paths

- `src/data/balance.ts`
- `.specify/game-balance.yaml`

## Stop Conditions

- None

## Notes

The canonical game specification is .specify/ (SSOT), which takes priority over docs/product.
