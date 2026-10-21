# Test Issue: path_only_in_fenced_code

## Outcome

Improve code examples in documentation.

## In Scope

- Better examples for feature X
- Clear usage patterns

## Acceptance Criteria

- [ ] AC1: Examples are correct and helpful
- [ ] AC2: No syntax errors in examples

## Verification Commands

```bash
# Example: the following path should NOT be extracted as target_path
$ cat src/example/not_real.ts
```

This is just a code example showing what the file might contain:

```typescript
// src/components/ExampleComponent.ts
import { utilities } from './.claude/skills/internal/utils';
```

## Out of Scope

- Performance optimization
