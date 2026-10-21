# Test Issue: Broken machine-readable contract

## Machine-Readable Contract

## Outcome

This issue has a malformed machine-readable contract.

## In Scope

- Feature implementation

```yaml
# Missing required contract_schema_version field
feature_id: test-feature
status: planning
```

## Acceptance Criteria

- AC1: Malformed contracts are detected
- AC2: Appropriate fail_closed response

## Verification Commands

```bash
uv run pytest tests/ -v
```

## Allowed Paths

- なし
