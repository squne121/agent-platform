# Test Issue: fenced_code_in_outcome

```yaml
contract_schema_version: v1
issue_kind: implementation
goal_ref: "Test goal"
```

## Outcome

This issue includes `src/real/file.ts` as a target path.

Here's an example of what not to match (in fenced code):

```typescript
// This path should be ignored: src/example/NotReal.ts
import { utils } from 'src/utils/Real.ts';
```

Real target path: `src/utils/Real.ts`

## In Scope

- Real implementation in `docs/implementation.md`

## Acceptance Criteria

- [ ] AC1: Real paths extracted correctly
- [ ] AC2: Fenced code paths ignored

## Verification Commands

```bash
$ test -f src/real/file.ts
```
