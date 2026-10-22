---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: 古いファイルを削除する
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "古いファイル削除"
change_kind: code
```

## Outcome

古いファイル `src/old_feature.ts` が削除されている。

## Acceptance Criteria

- [ ] AC1: old_feature.ts が削除されている

## Verification Commands

```bash
# AC1: untracked file を除外するパターン
$ test ! -f src/old_feature.ts && git status --porcelain | grep -v "^?" | grep -c "src/old_feature.ts" || true
```

## Stop Conditions

- ファイルが完全に削除できない場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: deletion only.

## Allowed Paths

- deletion of src/old_feature.ts
