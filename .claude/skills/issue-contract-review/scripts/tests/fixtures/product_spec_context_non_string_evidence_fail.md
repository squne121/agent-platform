# Implement: non-string product spec evidence

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: enhancement
```

## Outcome

Boolean and numeric evidence is rejected.

## Product Spec Context

```yaml
product_spec_id: false
diff_rationale: false
requirement_id: 0
affected_sections:
  - Movement
```

## Acceptance Criteria

- [ ] AC1: non-string evidence blocks

## Verification Commands

```bash
$ test -f docs/product/game-logic.md
```

## Allowed Paths

- `docs/product/game-logic.md`

## Stop Conditions

- None
