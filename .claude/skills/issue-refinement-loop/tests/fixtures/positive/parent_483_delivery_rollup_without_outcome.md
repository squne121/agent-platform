# 導入: M2: Combat MVP Gate (v0.2.x) として 1 sortie を開始→操作→戦闘結果まで通す

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: "M2: Combat MVP Gate (v0.2.x) として 1 sortie を開始→操作→戦闘結果まで通す"
change_kind: mixed
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Summary

M2: Combat MVP Gate（v0.2.x）の parent tracker。1 sortie を開始→操作→戦闘結果まで通すことが目標。

## Goal

1 sortie を開始→操作→戦闘結果まで通すための combat MVP 実装を child issue 群で進める。

## Desired Destination

v0.2.x として sortie 開始・操作・戦闘結果の E2E フローが動作する状態。

## Current Validated Scope

M2 Combat MVP として以下のスコープを確定:
- LoopE2ESnapshot 型定義
- E2E spec（Playwright）
- playtest doc

## Decisions Fixed

- 2026-05-20: parent_mode を delivery-rollup、closure_mode を child-complete に固定
- 2026-05-20: combat MVP のスコープを M2 として切り出す

## Quality Decision Record

- `Status`: N/A
- `Decision Date`: 未記録
- `Does this prove the parent goal complete?`: N/A
- `Reason`: delivery-rollup parent のため Quality Decision Record は N/A
- `Evidence`: N/A
- `Next Action`: child issue 完了後に parent を close

## Parent Closure Rule

- `delivery-rollup`: child issue が全て完了した時点で close する
- `quality-gate`: N/A（このトラッカーは delivery-rollup）
- `routing-map`: N/A
- `decision-log`: N/A
- `quality-gate` parent では `closure_mode` と `Quality Decision Record` を同一編集で更新する
- 非 `quality-gate` parent の `Quality Decision Record` は `N/A` 扱いでよく、close 判定の必須材料にしない

## Child Issues

- [x] #490 — M2 combat MVP playtest — LoopE2ESnapshot + E2E spec + playtest doc

## Remaining Parent Gaps

- [ ] なし（#490 完了で M2 達成）

## Phase Handoff Contract

- `Desired Destination`
- `Current Validated Scope`
- `Remaining Parent Gaps`
- `Current Objective`
- `Bounded Current Context`
- `Normative References`
- `Allowed Paths`
- `Required Skills`
- `Validation Commands`
- `Stop Conditions`
- `Open Questions`
- `Next Action`
- `Artifact Refs`
- `Handoff Prompt Draft`

## Acceptance Criteria

- [ ] #490 がマージされ M2 Combat MVP が動作する
- [ ] 1 sortie の E2E フローが Playwright で PASS する

## Notes

#483 相当の delivery-rollup parent fixture。Outcome セクションは意図的に存在しない。
