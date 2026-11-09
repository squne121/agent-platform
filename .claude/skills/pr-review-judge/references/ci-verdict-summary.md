# CI 証拠チェック

```bash
HEAD_SHA=$(gh pr view <PR> --json headRefOid --jq .headRefOid)
uv run python3 .claude/skills/pr-review-judge/scripts/ci_verdict_summary.py --pr <PR> --repo <owner>/<repo> --expected-head-sha "$HEAD_SHA"
```

Exit と意味:

- 0: all_pass（補助証拠として参照可）
- 10: failed / no_required_evidence（blocker。Issue #1856 AC11: required checks が
  0 件の場合も `no_required_evidence` として exit 10 になり、`all_pass` にはならない
  — fail-open の解消）
- 20: pending_or_queued（blocker）
- 30: stale_head_sha（stale blocker）
- 40: gh_error（blocker）

`ci_verdict_summary.py` が使えない場合のみ `gh pr checks` fallback を許容し、その旨を記録。

## canonical required-CI evaluator との関係（Issue #1856）

required check の存在有無・pass/fail 判定そのものは、pr-review-judge・pr-reviewer-lite・
impl-review-loop Step 4 の 3 経路とも `.claude/skills/impl-review-loop/scripts/wait_ci_checks.py`
（`gh pr checks --required --json` で live required set を取得し CheckRun / StatusContext
双方を評価対象に含む no-checks/skipped fail-closed 実装）を canonical evaluator とする。
`ci_verdict_summary.py` はそれに加えて log excerpt 抽出・artifact 保存などの詳細
provenance 収集を行う拡張レイヤーであり、required check が 0 件のときに `all_pass` を
返さない（`no_required_evidence`）という判定極性は `wait_ci_checks.py` の `no_checks`
判定と整合させてある。
