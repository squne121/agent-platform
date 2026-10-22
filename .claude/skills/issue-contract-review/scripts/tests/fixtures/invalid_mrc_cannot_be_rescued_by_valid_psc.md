# Implement: invalid MRC cannot be rescued

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: code
change_kind: docs
```

## Outcome

Malformed Machine-Readable Contract blocks even if PSC is valid.

## Product Spec Context

```yaml
product_spec_id: movement-v1
diff_rationale: Valid PSC must not rescue invalid MRC.
requirement_id: REQ-123
affected_sections:
  - Movement
```

## Acceptance Criteria

- [ ] AC1: invalid MRC blocks

## Verification Commands

```bash
$ test -f src/movement.py
```

## Allowed Paths

- `src/movement.py`

## Stop Conditions

- None
