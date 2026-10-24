---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: sentence-final punctuation fixture (warning expected)
---
<!-- Fixture 1: bare paths with sentence-final punctuation (Blocker 3).
CVS has "src/foo.py." (trailing dot), In Scope has "docs/bar.md." (trailing dot).
PATH_TOKEN_RE lookahead allows trailing punctuation; rstrip normalizes to path tokens.
Paths differ → warning fires. -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "sentence-final punctuation test"
change_kind: code
```

## Outcome

既存モジュールを docs 整備に移行する。

## Current Validated Scope

- src/foo.py.
- src/old_helper.ts.

## In Scope

- docs/bar.md.
- docs/new-guide.md,

## Acceptance Criteria

- [ ] AC1: `docs/bar.md` が存在する

## Verification Commands

```bash
# AC1
$ test -f docs/bar.md
```

## Stop Conditions

- 1
- 2
- 3
- 4
- 5
- 6

## Runtime Verification Applicability

decision: not_applicable
reason: fixture only.

## Allowed Paths

- `docs/bar.md`
