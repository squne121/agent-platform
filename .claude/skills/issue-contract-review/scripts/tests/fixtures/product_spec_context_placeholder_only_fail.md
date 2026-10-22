# Implement: Product spec placeholder context

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: enhancement
```

## Outcome

docs/product update is wired into review-issue.

## Product Spec Context

```yaml
product_spec_id: "<required: product_spec_id>"
requirement_ids:
  - TODO
diff_rationale: "<required: rationale>"
affected_sections:
  - "<required: section>"
```

## Acceptance Criteria

- [ ] AC1: docs/product scope is validated

## Verification Commands

```bash
$ test -f docs/product/game-logic.md
```

## Allowed Paths

- `docs/product/game-logic.md`

## Stop Conditions

- None
