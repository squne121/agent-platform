# Implement: Product spec duplicate key

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: enhancement
```

## Outcome

docs/product update is validated with strict parsing.

## Product Spec Context

```yaml
product_spec_id: game-logic-v1
diff_rationale: "first"
diff_rationale: "second"
affected_sections:
  - Movement Balance
requirement_ids:
  - REQ-001
```

## Acceptance Criteria

- [ ] AC1: duplicate key is rejected

## Verification Commands

```bash
$ test -f docs/product/game-logic.md
```

## Allowed Paths

- `docs/product/game-logic.md`

## Stop Conditions

- None
