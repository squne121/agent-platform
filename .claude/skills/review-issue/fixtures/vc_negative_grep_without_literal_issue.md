---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: 削除対象が不明な場合
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "削除対象を確認する"
change_kind: code
```

## Outcome

不要なコードが削除されている。

## Acceptance Criteria

- [ ] AC1: 削除完了を確認する

## Verification Commands

```bash
# AC1: 削除対象を明記せず、否定 grep だけで確認している
$ ! grep -r "old_pattern" src/
```

## Stop Conditions

- 削除できない場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: deletion only.

## Allowed Paths

- src/
