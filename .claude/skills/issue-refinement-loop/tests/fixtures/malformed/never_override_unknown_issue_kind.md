# Test Issue: never_override_unknown_issue_kind

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: completely_unknown_xyz_that_is_not_in_allowlist
goal_ref: "Some goal"
```

## Outcome

This issue has an unknown issue_kind that cannot be overridden by human_decision_reframe.

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
