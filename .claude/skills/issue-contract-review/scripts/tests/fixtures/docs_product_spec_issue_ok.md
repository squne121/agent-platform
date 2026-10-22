# Spec: Update Game Logic Documentation

Update `docs/product/game-logic.md` with new balance parameters discovered in playtest phase.

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: spec
change_kind: spec-delta
diff_rationale: |
  Playtest feedback revealed movement speed imbalance.
  Updated MOVE_SPEED values to match playtested metrics.
```

## Outcome

`docs/product/game-logic.md` updated with playtest findings and new balance parameters.

## Acceptance Criteria

- [ ] AC1: Movement balance section updated
- [ ] AC2: Change logged with playtest reference

## Verification Commands

```bash
$ grep -n "playtest" docs/product/game-logic.md
$ grep -n "MOVE_SPEED" docs/product/game-logic.md
```

## Allowed Paths

- `docs/product/game-logic.md`

## Stop Conditions

- None
