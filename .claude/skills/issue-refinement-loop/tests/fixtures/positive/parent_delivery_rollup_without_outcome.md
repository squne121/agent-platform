# Test Issue: Parent delivery-rollup tracker (no Outcome section)

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: "導入 plan_refinement_loop.py のテンプレート駆動バリデーション"
change_kind: workflow
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Summary

plan_refinement_loop.py が `.github/ISSUE_TEMPLATE/*.yml` を正本として読み込み、
issue_kind: parent / parent_mode: delivery-rollup の Issue では Outcome 欠落だけでは
fail_closed を返さないようにする parent tracker。

## Goal

issue-refinement-loop planner のバリデーションをテンプレート駆動に移行する。

## Desired Destination

スクリプト内にセクション名がハードコードされず、テンプレート YAML の required label が変われば
スクリプト変更なしで検出対象が変わる状態。

## Current Validated Scope

- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py`
- `.claude/skills/issue-refinement-loop/tests/`

## Decisions Fixed

- 2026-05-31: parent delivery-rollup は Outcome 欠落で fail_closed しない

## Quality Decision Record

- `Status`: N/A
- `Decision Date`: 未記録
- `Does this prove the parent goal complete?`: N/A
- `Reason`: N/A
- `Evidence`: N/A
- `Next Action`: child issue #544 を実装する

## Parent Closure Rule

- `delivery-rollup`: child issue #544 がマージされたら close する
- `quality-gate`: N/A
- `routing-map`: N/A
- `decision-log`: N/A

## Child Issues

- [ ] #544 — plan_refinement_loop.py parent delivery-rollup バリデーション修正

## Remaining Parent Gaps

- [ ] なし

## Phase Handoff Contract

- `Desired Destination`
- `Current Validated Scope`
- `Allowed Paths`

## Acceptance Criteria

- [ ] parent delivery-rollup の Issue で Outcome 欠落でも fail_closed しない
- [ ] implementation Issue では Outcome 欠落で fail_closed する
