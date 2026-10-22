# Implement: malformed PSC without docs/product scope

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: code
```

## Outcome

Malformed Product Spec Context is rejected even for code-only scope.

## Product Spec Context

```yaml
product_spec_id: game-v1
product_spec_id: game-v2
```

## Acceptance Criteria

- [ ] AC1: duplicate-key PSC blocks

## Verification Commands

```bash
$ test -f src/game.py
```

## Allowed Paths

- `src/game.py`

## Stop Conditions

- None
