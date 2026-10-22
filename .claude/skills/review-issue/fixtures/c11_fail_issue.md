---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: サンプル機能を追加する（C11 fail: decision と runtime-verification タグ不整合）
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

## Runtime Verification Applicability

decision: immediate
reason: ゲームの runtime 動作に影響する。

## Allowed Paths

- `src/sample.ts`

NOTE: このフィクスチャは C11 チェック（decision と runtime-verification タグの不整合）を fail させるためのもの。
`decision: immediate` なのに AC に `<!-- runtime-verification: true -->` タグが一つもない。
