---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: Product Spec 由来のテスト追加
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

`tests/game.test.ts` に core game logic テストが追加され、`pnpm test` でテストが通っている。

## Acceptance Criteria

- [ ] AC1: core game logic テストが存在する
- [ ] AC2: `pnpm test` が PASS する

## Verification Commands

```bash
# AC1
$ grep -r "describe.*game" tests/game.test.ts

# AC2
$ pnpm test
```

## Stop Conditions

- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- ビルドが壊れる場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: ゲームの runtime 動作に影響しない。

## Allowed Paths

- `tests/game.test.ts`
