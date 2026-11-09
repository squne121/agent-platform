# Context Protocol（作業手順）

> **移管状況 (I-4-4D4)**: 本書は移管元 `squne121/loop-protocol` tag `pre-split-agent-platform-20261020` (commit `6fa07b6a0ab932a62f08db6a8d521c0dfded2075`) の同名文書を最小差分で移管したもの。差分は `docs/dev/i4-4d4-skill-agents-readonly-manifest.json` の `intentional_diff` に記録している。本書が参照する `implement-issue` / `open-pr` / `pr-review-judge`・`references/`・補助 script 等のうち、本 repository に未移管のものは後続の I-4-4D スライスの担当であり、現時点では存在しない。未移管の対象を起動できるものとして扱ってはならない。本書の記述だけで通常の `/impl-review-loop`・Step 4 / Step 5 / Final Gate が本番稼働していると解釈してはならない。

## LOOP_STATE 更新タイミング

**各 Step 完了直後** に LOOP_STATE YAML を会話履歴へ明示記録する。次イテレーション開始時に最新値を読み戻す。

## Human-history v1（人間向け作業履歴、Issue #1908）

既存の machine-readable comment（target / marker / payload / consumer）は変更しない。
new human-history だけを既存 `issue_comment.publish` controlled lane 経由で投稿する。
`publish_termination_report.py` の `publish_human_history()` は別 publisher ではなく、この既存 lane
の strict marker mode である。

1. review phase の直前に SSOT を snapshot する。pre-PR / binding-invalid / issue-refinement は
   source Issue body の SHA-256（`sha256:` prefix なし）、post-PR は
   `refs/pull/<pr>/head@<40-lowercase-sha>` である。
2. matrix に従い identity の target を選ぶ。`issue-refinement-loop/review-complete`、
   `impl-review-loop/pre-PR-binding`、`binding-validation` は source Issue、valid
   `post-PR-binding` / `post-PR-head-drift` は bound PR、`conflict-resolution` は origin の
   target を維持する。
3. `loop_kind, phase, source_issue_number, target_kind, target_number,
   route_or_termination_reason, reviewed_ref` **だけ**を RFC 8785 JCS UTF-8 JSON object として
   SHA-256 し、`<!-- loop-protocol/human-history:v1:sha256:<lowercase-hex> -->` を最終 non-empty
   line に置く。timestamp / run ID / rendered body は identity に入れない。
4. public-safe 日本語で実施内容、推奨 action / reason / impact、evidence refs を `publish_human_history()`
   へ渡す。raw transcript、credential、local absolute path、full tool output は拒否する。
5. controlled executor は raw comment 全体から namespace occurrence を先に検査する。malformed /
   duplicate / foreign marker は diagnostic failure。one owned marker だけで same digest=noop、
   changed digest=PATCH、zero=create とし、create/PATCH/noop の全てで author / marker / canonical
   content digest を readback する。

post-PR primary result の create/PATCH/noop decision 直前には direct PR-head read を行い、
reviewed_ref の snapshot と等しい時だけ primary を受理する。不一致時は old primary を new head
用に PATCH せず `head_drift` diagnostic identity を同じ bound PR に reconcile する。diagnostic は
original stale snapshot を reviewed_ref に維持し、decision-time direct read と stale evidence が違えば
latest head で同じ diagnostic identity を再reconcileする。accepted noop / controlled write/readback の後も
head を再読し、latest diagnostic の readback/noop reconciliation 成功後にだけ latest head を re-review
する。

controlled executor が `status: stale_head` または `applied_but_head_drift` と
`head_drift.route` を返した場合、Step 5 は成功として終了してはならない。
`reconcile_head_drift_then_rereview` は current_head_sha で既存 `head_drift` identity の
reconciliation を開始し、`reconcile_same_head_drift_identity_then_rereview` は stale evidence だけを
latest current_head_sha に更新して同じ identity を再reconcileする。後者の successful readback または
valid noop reconciliation を確認してから、その latest head にだけ Step 4 re-review を enqueue する。

`conflict_hard_stop` は history event ではない。同一 iteration で resolve/revalidate 後にも連続する
conflict だけが `conflict-resolution` / `human_escalation` を emit する。

## SubAgent 出力の取扱い

各 SubAgent は構造化フォーマット（YAML / KEY=VALUE）で結果を返す。orchestrator はそれを parse して LOOP_STATE に反映する:

| SubAgent | 出力契約 | 受け取り方 |
|---|---|---|
| `implementation-worker` | `IMPLEMENT_RESULT_V1` YAML | `status` / `pr_url` / `verification` を LOOP_STATE へ |
| `test-runner` | `TEST_VERDICT_MACHINE v2` マーカー付き read-only report（呼び出し元への直接返却。test-runner は PR へコメントを投稿しない、Issue #1648, #88） | `spawn_agent` / `list_agents` の final result から直接受け取る（`gh pr view --json comments` からのTEST_VERDICT 抽出は normal routing として扱わない）。current-head binding tuple の照合結果である`VC_ADJUDICATION_RESULT_V1` を LOOP_STATE へ反映し、Step 4 起動可否の gate に使う（diagnostics-only の TEST_VERDICT_MACHINE 自体は APPROVE/REQUEST_CHANGES 判定の必須 blocking inputとしては扱わない。authoritative evidence は `CI_CHECK_RUN_SCOPED` と束縛済み独立実行 Issue VC。`.claude/skills/pr-review-judge/references/evidence-policy.md` 参照） |
| `pr-reviewer` | `LOOP_VERDICT` YAML（verdict コメント内） | step-5-mergeability-handling.md の抽出手順を使う |

## 外部仕様調査の判定根拠記録

外部仕様調査（`gemini-cli-headless-delegation`）のスキップ・実施判断は LOOP_STATE.external_research_skip_basis に記録する:

```yaml
external_research_skip_basis: "internal-only change in src/systems; no external spec dependency (iteration 0)"
```
