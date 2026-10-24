---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: サンプル機能を追加する
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "サンプル機能の実装"
change_kind: code
```

## Parent Issue

none

## Outcome

`src/sample.ts` に SampleFeature クラスが追加され、`pnpm test` でテストが通っている。

## Background

サンプル機能が未実装のため追加する。

## Parent Goal Ref

- Goal: サンプル機能の実装
- Desired Destination: SampleFeature が動作すること

## Current Validated Scope

- `src/sample.ts` に SampleFeature クラスを追加

## Remaining Parent Gaps

なし

## In Scope

- `src/sample.ts` に SampleFeature クラスを追加
- `tests/sample.test.ts` にテストを追加

## Out of Scope

- その他のファイルの変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: `src/sample.ts` に `SampleFeature` クラスが存在する
- [ ] AC2: `pnpm test` が PASS する

## Verification Commands

```bash
# AC1
$ grep -r "class SampleFeature" src/sample.ts

# AC2
$ pnpm test
```

## Stop Conditions

* Allowed Paths 外の変更が必要な場合は停止
* テストが修正できない場合は停止
* 既存の型定義と競合する場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: ゲームの runtime 動作に影響しない。

## Allowed Paths

- `src/sample.ts`
- `tests/sample.test.ts`
