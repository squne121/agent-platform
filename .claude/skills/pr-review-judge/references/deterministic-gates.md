# Deterministic Gates (G1–G5)

- **G1** ci_test_selection
- **G2** evidence_binding（self-report 単独 APPROVE 禁止）
- **G3** implementation_oracle（実装検証）
- **G4** head_sha_consistency（review head の整合）
- **G5** fixture_guard_path_coverage

判定実装: `.claude/skills/pr-review-judge/scripts/check_pr_review_gates.py`

失敗時は `PR_REVIEW_GATE_RESULT_V1` が error>=1 なら `REQUEST_CHANGES`。
