## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: docs
```

## Outcome

テスト用 Outcome です。`tests/` にファイルを追加することで完了する。

## In Scope

- テストファイルの変更

## Out of Scope

- その他

## Acceptance Criteria

- [ ] AC1: テストが通る

## Verification Commands

```bash
# AC1
$ pnpm test
```

## Allowed Paths

- tests/
  （テスト用 fixture のため変更範囲はテストディレクトリのみに限定する）

## Stop Conditions

+ Allowed Paths 外の変更が必要と判明した場合
+ In Scope の固定契約の変更が必要になった場合
+ 新規 Issue の起票が必要と判断した場合
+ テストが継続的に失敗する場合
+ ビルドが壊れる場合

## Required Skills

なし

## Runtime Verification Applicability

decision: not_applicable
reason: テスト
