# Implement: Product spec tilde-fenced decoy

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: enhancement
```

## Outcome

docs/product update is validated with section-bound parsing.

## Notes

~~~markdown
## Product Spec Context
product_spec_id: fake
diff_rationale: fake
affected_sections:
  - fake
~~~

## Acceptance Criteria

- [ ] AC1: code-fence decoy is ignored

## Verification Commands

```bash
$ test -f docs/product/game-logic.md
```

## Allowed Paths

- `docs/product/game-logic.md`

## Stop Conditions

- None
