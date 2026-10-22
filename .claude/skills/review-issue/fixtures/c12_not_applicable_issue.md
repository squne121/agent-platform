---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: 一般的なバグ修正
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "一般的なバグ修正"
change_kind: code
```

## Outcome

`src/utils.ts` の bug が修正され、`pnpm test` でテストが通っている。

## Acceptance Criteria

- [ ] AC1: bug が修正されている
- [ ] AC2: `pnpm test` が PASS する

## Verification Commands

```bash
# AC1
$ grep -r "fixed_bug" src/utils.ts

# AC2
$ pnpm test
```

## Stop Conditions

- テストが修正できない場合は停止
- ビルドが壊れる場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: runtime に影響しない。

## Allowed Paths

- `src/utils.ts`
