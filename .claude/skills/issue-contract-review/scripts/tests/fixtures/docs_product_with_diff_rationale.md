# Implement: Game Logic Specification Update

Update `docs/product/game-logic.md` with playtest findings.

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: spec-update
```

## Outcome

`docs/product/game-logic.md` updated with new movement balance parameters.

## Product Spec Context

```yaml
change_type: update
target_paths:
  - docs/product/game-logic.md
product_spec_id: game-logic-v1
requirement_ids:
  - REQ-movement-001
  - REQ-movement-002
diff_rationale: "Playtest phase 1 showed movement speed too slow at 200ms; increased to 250ms based on 15 player feedback data points"
affected_sections:
  - Movement Balance
  - Combat Timing
```

## Acceptance Criteria

- [ ] AC1: Movement balance section updated with new values
- [ ] AC2: Playtest reference documented
- [ ] AC3: Affected game-logic sections consistent

## Verification Commands

```bash
$ grep -n "diff_rationale\|affected_sections" <(gh issue view --json body)
$ grep -n "250ms" docs/product/game-logic.md
```

## Allowed Paths

- `docs/product/game-logic.md`

## Stop Conditions

- None
