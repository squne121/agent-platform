---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: natural English overlap fixture (no warning)
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "natural English overlap no warning test"
change_kind: code
```

## Outcome

extend the tokenizer with meaningful keyword matching.

## Current Validated Scope

- extend tokenizer with keyword extraction logic
- implement meaningful overlap detection between sections
- validate matching accuracy against known benchmarks

## In Scope

- extend tokenizer with additional pattern matching
- implement meaningful overlap detection for bullet lists
- validate matching performance against benchmarks

## Acceptance Criteria

- [ ] AC1: tokenizer extension is implemented

## Verification Commands

```bash
# AC1
$ test -f src/tokenizer.py
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

- `src/tokenizer.py`
