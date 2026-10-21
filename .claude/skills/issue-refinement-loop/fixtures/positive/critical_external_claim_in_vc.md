# Test Issue: critical_external_claim_in_vc

## Outcome

Add support for the official GitHub CLI API version 2.0 authentication.

## In Scope

- Update to latest GitHub API specification
- Ensure compatibility with current CLI auth behavior

## Acceptance Criteria

- [ ] AC1: Implementation follows official GitHub documentation
- [ ] AC2: Authentication matches current migration behavior

## Verification Commands

```bash
$ test -f .claude/skills/verify/scripts/auth_test.py
```

## Out of Scope

- Backward compatibility with legacy authentication
