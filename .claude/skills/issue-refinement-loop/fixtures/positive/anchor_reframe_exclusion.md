# Test Issue: anchor_reframe_exclusion

## Outcome

Test scope signal exclusion when anchor comment provides reframe decision.

## In Scope

- Original scope item A
- Original scope item B

## Acceptance Criteria

- [ ] AC1: Scope signal is triggered by anchor comment
- [ ] AC2: Exclusion is properly recorded

## Verification Commands

```bash
$ test -f .claude/worktrees/test/scripts/anchor_test.sh
```

## Out of Scope

- Items that should be excluded based on anchor reframe
