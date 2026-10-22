---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: trailing dot normalization fixture (no warning)
---
<!-- Fixture 5: trailing dot normalization (Blocker 3).
CVS has "config/settings.yaml." (trailing dot), In Scope has "config/settings.yaml" (no dot).
rstrip normalizes both to "config/settings.yaml" → same token → no warning. -->
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "trailing dot normalization no-warning test"
change_kind: code
```

## Outcome

config/settings.yaml を更新する。

## Current Validated Scope

- config/settings.yaml.
- src/app.ts.

## In Scope

- config/settings.yaml
- src/app.ts

## Acceptance Criteria

- [ ] AC1: `config/settings.yaml` が更新されている

## Verification Commands

```bash
# AC1
$ test -f config/settings.yaml
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

- `config/settings.yaml`
