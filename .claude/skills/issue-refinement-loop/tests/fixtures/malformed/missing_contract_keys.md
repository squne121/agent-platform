# Test Issue: missing_contract_keys

## Machine-Readable Contract

```yaml
goal_ref: "Some goal without required keys"
change_kind: workflow
```

## Outcome

The issue has a Machine-Readable Contract block but it is missing required keys
(`contract_schema_version` and `issue_kind`).

## In Scope

- Some scope item

## Acceptance Criteria

- [ ] AC1: Test passes

## Verification Commands

```bash
$ test -f file.txt
```

## Allowed Paths

- `.claude/skills/test/`
