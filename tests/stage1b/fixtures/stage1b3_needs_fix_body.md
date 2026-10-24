## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2380 canonical delivery fixture (needs-fix branch)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
canonical Step 2 directly consumes `compact_result.verdict` (Issue #2380).

## Acceptance Criteria

AC1: fixture body intentionally omits the `- [ ]` checkbox prefix so the
real checker chain's C3 check fails, producing a genuine needs-fix verdict.

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true
```

## Allowed Paths

- fixture/e2e_produce_canonical_delivery_needs_fix.md
