# Test Issue: delivery_rollup_unmaterialized

## Outcome

Create a delivery rollup parent issue with child issues.

## In Scope

- Child issue: "Implement feature X" (未起票)
- Child issue: "Add tests for feature X" (unmaterialized)
- Child issue: "Update documentation" (TBD)

## Acceptance Criteria

- [ ] AC1: All child issues are materialized
- [ ] AC2: Each child passes its own acceptance criteria

## Verification Commands

```bash
$ test -f .claude/worktrees/issue-392-test/scripts/check.py
```

## Out of Scope

- Performance optimization
