---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: サンプル機能を追加する（C9 fail: Runtime Verification Applicability セクションなし）
---
## Outcome

`src/sample.ts` に SampleFeature クラスが追加される。

## Background

サンプル機能が未実装のため追加する。

## Parent Goal Ref

- Goal: サンプル機能の実装
- Desired Destination: SampleFeature が動作すること

## Current Validated Scope

- `src/sample.ts` に SampleFeature クラスを追加

## Remaining Parent Gaps

なし

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: `src/sample.ts` に `SampleFeature` クラスが存在する

## Verification Commands

```bash
# AC1
$ grep -r "class SampleFeature" src/sample.ts
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Allowed Paths

- `src/sample.ts`

NOTE: このフィクスチャは C9 チェック（Runtime Verification Applicability セクション不在）を fail させるためのもの。
`## Runtime Verification Applicability` セクションが存在しない。
