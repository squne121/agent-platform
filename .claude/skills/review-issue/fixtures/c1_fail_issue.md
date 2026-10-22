---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: サンプル機能を追加する（C1 fail: 必須セクション不足）
---
## Outcome

`src/sample.ts` に SampleFeature クラスが追加される。

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

decision: not_applicable
reason: ゲームの runtime 動作に影響しない。

NOTE: このフィクスチャは C1 チェック（必須セクション不足）を fail させるためのもの。
必須セクション "Allowed Paths" が存在しない。
