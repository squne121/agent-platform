# Change Kind Update Only - Not Product Spec Context

## Allowed Paths
- docs/product/features/movement.md
- src/systems/movement.ts

## Machine-Readable Contract
```yaml
change_kind: update
issue_kind: implementation
```

## Outcome
Updating movement features and src with generic "update" change_kind.

## Acceptance Criteria
- [ ] AC1: Updated

## Verification Commands
```bash
grep -n "update" docs/product/features/movement.md
```

## Summary
This has docs/product/features in allowed_paths, but change_kind is just "update" without product-spec context.

This should fail PS004 because docs/product is being updated but no diff_rationale / changed_requirement_id / affected_sections is provided, and it's not explicitly a spec delta.
