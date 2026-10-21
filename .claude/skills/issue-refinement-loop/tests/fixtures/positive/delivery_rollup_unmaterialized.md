# Test Issue: Extract unmaterialized child slots

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: "delivery rollup identifies unmaterialized child slots"
change_kind: workflow
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Outcome

This is a parent tracker Issue with delivery rollup tracking.

## Summary

delivery-rollup parent として未具体化 child slot を抽出できることを確認する。

## Goal

未起票・TBD・unmaterialized の child slot を正しく拾い、親トラッカーの残件として扱える状態にする。

## Desired Destination

delivery-rollup parent から child issue 起票候補を安定して洗い出せる状態。

## Current Validated Scope

- issue-refinement-loop planner の親トラッカー判定
- unmaterialized child slot 抽出

## Decisions Fixed

- 2026-06-20: delivery-rollup parent では child slot 抽出を継続する

## Quality Decision Record

- `Status`: N/A
- `Decision Date`: 未記録
- `Does this prove the parent goal complete?`: N/A
- `Reason`: N/A
- `Evidence`: N/A
- `Next Action`: N/A

## Parent Closure Rule

- `delivery-rollup`: child issue 群が materialize されたら close する
- `quality-gate`: N/A
- `routing-map`: N/A
- `decision-log`: N/A

## In Scope

- #100 (未起票) - Initial planning document
- #101 (unmaterialized) - Implementation phase  
- #102 (TBD) - Verification and testing
- Code change tracking with (#103) (未起票) first sub-issue

## Acceptance Criteria

- AC1: Unmaterialized slots are correctly extracted
- AC2: Multiple marker types are supported

## Child Issues

- [ ] #100 - Initial planning document
- [ ] #101 - Implementation phase
- [ ] #102 - Verification and testing
- [ ] #103 - Code change tracking

## Remaining Parent Gaps

- [ ] 未具体化 child slot の起票

## Verification Commands

```bash
uv run pytest .claude/skills/issue-refinement-loop/tests/ -v
```

## Phase Handoff Contract

- `Desired Destination`
- `Current Validated Scope`
- `Remaining Parent Gaps`
- `Current Objective`
- `Bounded Current Context`
- `Allowed Paths`
- `Required Skills`
- `Validation Commands`
- `Stop Conditions`
- `Next Action`

## Notes

なし
