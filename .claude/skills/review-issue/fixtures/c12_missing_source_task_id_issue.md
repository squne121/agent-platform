---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: Product Spec C12 fixture (missing source_task_id)
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#300"
goal_ref: "Generated task からの trace field 検証"
change_kind: code
product_spec_id: "features/game-core"
requirement_id: "REQ-001"
```

## Outcome

`tests/x.test.ts` に C12 fixture テストが追加され、`pnpm test` が通っている。

## Acceptance Criteria

- [ ] AC1: fixture が存在する
- [ ] AC2: `pnpm test` が PASS する

## Verification Commands

```bash
# AC1
$ test -f tests/x.test.ts

# AC2
$ pnpm test
```

## Stop Conditions

- テストが修正できない場合は停止
- 既存型と競合する場合は停止
- ビルドが壊れる場合は停止
- 別 Issue 起票が必要になった場合は停止
- 外部 API 利用が必要な場合は停止
- 権限昇格が必要な場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: fixture のみ。

## Allowed Paths

- `tests/x.test.ts`
