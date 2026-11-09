# Step 5: LOOP_VERDICT ルーティング（Mergeability Handling）

> **移管状況 (I-4-4D3)**: 本書は移管元 `squne121/loop-protocol` tag `pre-split-agent-platform-20261020` (commit `6fa07b6a0ab932a62f08db6a8d521c0dfded2075`) の同名文書を最小差分で移管したもの。差分は `docs/dev/i4-4d3-step4-step5-manifest.json` の `intentional_diff` に記録している。本書が参照する `SKILL.md`・Agent 定義 (`pr-reviewer` / `test-runner` / `implementation-worker`)・`implement-issue` / `open-pr` / `pr-review-judge`・他の steps・補助 script 等のうち、本 repository に未移管のものは後続の I-4-4D スライスの担当であり、現時点では存在しない。未移管の対象を起動できるものとして扱ってはならない。本書の記述だけで Step 4 / Step 5 / Final Gate が本番稼働していると解釈してはならない。

Issue #1873 以降、pr-reviewer SubAgent の返す `reviewer_verdict`（`verdict` / `reviewed_head_sha` /
`blockers` / `warnings` の最小 convention）と、control-plane が `gh pr view` で直接取得する
`live_mergeability`（`mergeable` / `merge_state_status`）を突き合わせて次アクションを決定する。
PR コメントとして投稿された YAML を再パースして判定を再構築することはしない
（コメント投稿は監査用の記録であり、production 判定の入力ではない）。

## reviewer_verdict の取得

pr-reviewer SubAgent の Step 4 呼び出し結果から直接受け取る:

```yaml
verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED
reviewed_head_sha: "<pr-reviewer がレビューした時点の PR head SHA>"
blockers:
  - "<具体的な blocker>"
warnings:
  - "<任意>"
```

`verdict` が `_VALID_VERDICTS`（APPROVE / REQUEST_CHANGES / HUMAN_REVIEW_REQUIRED）以外、
または `reviewed_head_sha` が空の場合は `route_loop_verdict_v2()` が `fail_closed` を返す
（`schema_invalid_verdict_value` / `schema_invalid_reviewed_head_sha_empty_or_missing`）。
`merge_ready` / `mergeability` / `required_auto_actions` / `allowed_paths_gate` / `test_verdict` を
`reviewer_verdict` に含めてはならない（含まれていた場合 `schema_invalid_legacy_field_present` で fail-closed）。

## live_mergeability の取得

```bash
LIVE_MERGEABILITY_JSON=$(gh pr view "$PR_NUMBER" --repo "$REPO" \
  --json headRefOid,mergeable,mergeStateStatus)
```

`route_loop_verdict_v2()` に渡す `live_mergeability` は以下の形にマップする:

```yaml
head_sha: <headRefOid>
mergeable: MERGEABLE | CONFLICTING | UNKNOWN
merge_state_status: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN | DRAFT | HAS_HOOKS
```

## main drift の live facts を必ず注入する

Step 5 control-plane は、**毎回の通常 production routing 前**に live main-drift facts を
作成し、`build_step5_live_mergeability()` 経由で router に渡す。`BEHIND` だけの
補助情報ではない。`CLEAN`、`HAS_HOOKS`、`BLOCKED`、`DRAFT`、`UNSTABLE` のいずれでも、
base-bound evidence が stale なら scope-clean reconciliation を先に選ばなければならない。

同一 decision cycle で、次を fresh source から取得する:

```yaml
main_drift_facts:
  current_base_sha: <live PR baseRefOid または live main HEAD>
  evidence_base_sha: <LOOP_STATE の evidence epoch base SHA>
  allowed_paths_snapshot_base_sha: <scope snapshot に記録された base SHA>
  allowed_paths: <live linked Issue 本文の Allowed Paths>
  latest_main_net_diff: <evidence_base_sha..current_base_sha の read-only net diff>
  expected_old_sha: <reconciliation 前に保存した old ref>
  observed_old_sha: <同じ decision cycle で再読した old ref>
  semantic_ambiguity: false
  behind_fast_path_eligible: <merge_state_status == BEHIND のときだけ評価>
```

`current_base_sha == evidence_base_sha` の場合も、同じ shape を渡して evidence epoch の
一致を確認する。必須 key が欠けたら drift なしと推定せず fail-closed とする。control-plane は
raw snapshot / 過去 CI / 過去 review URL をこの facts の代替にしてはならない。

### `semantic_ambiguity` は control-plane が実 git 検査で算出する（Issue #2102 fix_delta iteration 2）

`route_loop_verdict_v2.py` 自身は subprocess を呼ばない設計制約を保つ（router を純粋な決定論的関数として保つため）。したがって `semantic_ambiguity` は router 内部で算出されず、**呼び出し元（この Step 5 手順を実行する control-plane）が `main_drift_facts` を組み立てる前に実 git 検査を実行して算出しなければならない（MUST）**。caller が推測や固定値 `false` を渡すことは禁止する。

`scripts/agent-ops/pr_head_replay_publish_exec.py::_merge_tree_conflicts()` が採用している decision と同じ oracle を、worktree／index を汚さない read-only 呼び出しとして使う:

```bash
# expected_old_sha は main_drift_facts.expected_old_sha、current_base_sha は同 facts の current_base_sha
git merge-tree --write-tree "$EXPECTED_OLD_SHA" "$CURRENT_BASE_SHA"
MERGE_TREE_EXIT=$?
# 非 0 終了 = 実 conflict あり -> semantic_ambiguity: true
# 0 終了      = clean merge   -> semantic_ambiguity: false
```

この呼び出しは `git merge-tree --write-tree` の 2-ref 形式であり、作業ツリー・index を変更しない。control-plane はこの exit code をそのまま `main_drift_facts.semantic_ambiguity` へ写し、上書きしない。

**適用範囲の明示（Safety Claim の正確化）**: この検査は `pr_head_replay_publish_exec.py` の opt-in `main_drift_reconciliation` 経路だけでなく、この Step 5 BEHIND fast path を含む **通常の main drift routing 全体で MUST** とする。

**production wiring の現況（Issue #2102 fix_delta iteration 5, Blocker B 解消）**: 上記 bash 呼び出しを手動で実行し `main_drift_facts.semantic_ambiguity` を組み立てる従来手順に加えて、`route_loop_verdict_v2.py` は `route_loop_verdict_v2_resolve_semantic_ambiguity(reviewer_verdict, live_mergeability, cwd=...)` という薄い convenience wrapper を公開する。この wrapper は `main_drift["semantic_ambiguity"]` が明示的に渡されていない場合にのみ、Allowed Paths の姉妹 exact file である `scripts/agent-ops/pr_head_replay_publish_exec.py::compute_semantic_ambiguity()`（`_merge_tree_conflicts()` の public export、同じ `git merge-tree --write-tree` oracle）を呼び出して補完してから `route_loop_verdict_v2()` 本体へ委譲する。`route_loop_verdict_v2()` 自体は module docstring の Design Invariant どおり subprocess を呼ばない pure 関数のまま変更していない -- purity の緩和はこの wrapper 1 箇所に限定する（Issue #2102 の contract が許容する option (a)）。control-plane はどちらの経路（手動 bash 配線、または wrapper 呼び出し）を使ってもよいが、`semantic_ambiguity` を caller が推測・固定値で渡すことは引き続き禁止する。

```python
from route_loop_verdict_v2 import build_step5_live_mergeability, route_loop_verdict_v2

live_mergeability = build_step5_live_mergeability(
    {
        "head_sha": live_pr["headRefOid"],
        "mergeable": live_pr["mergeable"],
        "merge_state_status": live_pr["mergeStateStatus"],
    },
    main_drift_facts,
)
decision = route_loop_verdict_v2(reviewer_verdict, live_mergeability)
```

`main_drift_facts["semantic_ambiguity"]` を control-plane がまだ算出していない場合は、
`route_loop_verdict_v2()` の代わりに `route_loop_verdict_v2_resolve_semantic_ambiguity()`
を呼んで実 git oracle による補完を委譲できる:

```python
from route_loop_verdict_v2 import (
    build_step5_live_mergeability,
    route_loop_verdict_v2_resolve_semantic_ambiguity,
)

live_mergeability = build_step5_live_mergeability(
    {
        "head_sha": live_pr["headRefOid"],
        "mergeable": live_pr["mergeable"],
        "merge_state_status": live_pr["mergeStateStatus"],
    },
    {k: v for k, v in main_drift_facts.items() if k != "semantic_ambiguity"},
)
decision = route_loop_verdict_v2_resolve_semantic_ambiguity(
    reviewer_verdict, live_mergeability, cwd=repo_root
)
```

この注入は reviewer verdict dispatch より前に router が評価する。実 conflict
（`CONFLICTING` / `DIRTY`）だけは従来どおり最優先 hard stop とする。

> **`mergeable` と `merge_state_status` の分離（#1869 fix_delta P0-1）**: `mergeable` の有効値は
> `CONFLICTING` / `MERGEABLE` / `UNKNOWN`。`merge_state_status` の有効値は `BEHIND` / `BLOCKED` /
> `CLEAN` / `DIRTY` / `DRAFT` / `HAS_HOOKS` / `UNKNOWN` / `UNSTABLE`。`merge_state_status ==
> CONFLICTING` という値は GitHub の実 enum に存在しないため、production router
> （`route_loop_verdict_v2.py`）はこれを **schema 不正**として扱い、conflict としては扱わない。

## ルーティング呼び出し

```python
from route_loop_verdict_v2 import route_loop_verdict_v2

decision = route_loop_verdict_v2(
    reviewer_verdict,       # {verdict, reviewed_head_sha, blockers, warnings}
    live_mergeability,      # {head_sha, mergeable, merge_state_status}
)
```

Issue #1870（#1856）: `route_loop_verdict_v2()` は `test_verdict` を一切受け付けない（渡すと
`TypeError`）。BEHIND 判定は `live_mergeability.merge_state_status == "BEHIND"` のみで確定する。
#1856 が確立した protected TEST_VERDICT producer/publisher lane 自体はこの routing とは独立して
存続する（ordinary review routing の入力ではなくなっただけ）。

`decision.route` の値と orchestrator の対応:

| `route` | 意味 | 次アクション |
|---|---|---|
| `approved` | `APPROVE` かつ mergeable/merge_state_status が `MERGEABLE`/`CLEAN` または `MERGEABLE`/`HAS_HOOKS` | 終了（approved）。`step-5-feedback-and-termination.md` の残り gate を確認 |
| `continue_loop` | `REQUEST_CHANGES`（actual conflict がない場合。`already_satisfied_evidence` 未提供、または下記 `already_satisfied` の 4 条件のいずれかが不成立） | 次イテレーションへ（blockers を fix_delta に） |
| `already_satisfied` | `REQUEST_CHANGES` かつ `live_mergeability.already_satisfied_evidence.base_ac_satisfied == true` かつ `meaningful_pr_delta == false` かつ `evidence_base_sha` が `live_mergeability.main_drift.current_base_sha` と一致（#2607。下記「already_satisfied recovery route（既存 PR に対する Step 5 回復ルート）」参照） | 終了。`decision.selected_action` の `result`/`recommendation` を `SKILL.md` の Already-Satisfied Recommendation Structure に従って報告。PR/Issue の close は control-plane・router のいずれも実行しない |
| `route_stale_head_rereview` | `APPROVE` かつ `reviewed_head_sha != live head_sha`（actual conflict がない場合） | Step 4 を現在の head で再委譲し、Step 5 を最初からやり直す |
| `route_to_update_branch` | `APPROVE` かつ `merge_state_status == BEHIND` | 下記「BEHIND 分岐 routing」参照 |
| `route_human_escalation` | `HUMAN_REVIEW_REQUIRED`（actual conflict がない場合） | 人間判断を仰いで停止（`termination_reason: human_escalation`）。max iteration 到達や secret/protected-path 等の実 hard gate と合わせて、正当な human stop 理由の一つ |
| `conflict_hard_stop` | `mergeable == CONFLICTING` または `merge_state_status == DIRTY`（**verdict に関係なく最優先で評価**） | CONFLICTING PR Escalation Runbook 発動（actual conflict のみ hard stop。#1860 Owner Decision） |
| `fail_closed` | schema 不正、`APPROVE` かつ `blockers` 非空、`mergeability_unknown`、`BLOCKED`/`UNSTABLE`/`DRAFT` | `decision.reason_code` を warning として記録。`mergeability_unknown` は bounded retry（最大 3 回、5 秒間隔）後も `fail_closed` のままなら warning に格下げして継続。`BLOCKED`/`UNSTABLE`/`DRAFT` は current-head required-CI / branch-protection evaluator の判定に委ね、human escalation にはしない |

`UNKNOWN`、`BLOCKED`、`BEHIND`、`UNSTABLE`、`DRAFT`、`HAS_HOOKS` は Git conflict として扱わない
（Safety Invariants）。actual Git conflict として hard stop するのは `mergeable == CONFLICTING` または
`merge_state_status == DIRTY` の場合のみであり、この判定は reviewer verdict（`REQUEST_CHANGES` /
`HUMAN_REVIEW_REQUIRED` を含む）より必ず先に評価される。`DRAFT` は Issue #1873 の Delivery Rule が
Draft PR を要求しているため、単独では human escalation の理由にしない。

## already_satisfied recovery route（既存 PR に対する Step 5 回復ルート、#2607）

`already_satisfied` は `preparation.md` の `0-a-1. Already-Satisfied Early-Exit choke point` とは責務が分離した **別経路**（recovery route）である。両者の呼び出し順序・責務分離は以下のとおり:

| 経路 | 発火位置 | 前提 | `recommendation.pr.action` |
|---|---|---|---|
| early-exit（`preparation.md` 0-a-1） | Step 1 起動前（implementation-worker dispatch 前） | PR 未作成 | `none`（`reason: no_pr_created`） |
| recovery route（本節、`route_loop_verdict_v2()`） | Step 5（PR 既存、`REQUEST_CHANGES` verdict 受信後） | PR 既存 | `close`（`reason: no_meaningful_delta`） |

recovery route は early-exit が不発だった（PR が既に作成された）場合にのみ到達しうる後続の safety net であり、early-exit の再実行ではない。両者は同一サイクル内で二重に発火しない（early-exit は PR 未作成時のみ評価され、recovery route は PR 既存時のみ評価されるため、条件が排他的）。

control-plane は Step 5 の通常 `live_mergeability` 構築（`build_step5_live_mergeability()`）に加えて、`already_satisfied_evidence` を独立に構築する場合のみ以下を `live_mergeability` へ追加する（未構築なら追加しない。追加しなければ `route_loop_verdict_v2()` は従来どおり `continue_loop` を返す）。`already_satisfied_evidence` の3値は自由に組み立てず、`route_loop_verdict_v2.py` の production pure function `build_already_satisfied_evidence(base_test_verdict, pr_head_test_verdict, live_main_sha, live_pr_head_sha)`（Issue #2607 AC13。canonical producer）にそのまま渡して導出する:

```yaml
already_satisfied_evidence:
  base_ac_satisfied: <bool>     # current main HEAD に対する独立 TEST_VERDICT_MACHINE/v2 が全 AC PASS
  meaningful_pr_delta: <bool>   # base 実行と直近 PR head 実行の runtime_ac_results PASS 集合比較
  evidence_base_sha: <sha>      # base 実行時点の current main HEAD SHA
```

`evidence_base_sha` の freshness は `main_drift.current_base_sha`（Issue #2102、既存の main-drift facts）と比較される。`main_drift` を省略した場合、`already_satisfied_evidence` を渡しても freshness 判定不能として `already_satisfied` は成立しない（`continue_loop` へフォールバック）。

## BEHIND 分岐 routing

`decision.route == "route_to_update_branch"` の場合、`decision.selected_action` に
`route_loop_verdict_v2()` が合成した action が入っている（reviewer からは受け取らない）:

```yaml
kind: update_branch
executor: implementation-worker
skill: implement-issue.update_branch
mechanical: true
expected_head_sha: <reviewed_head_sha>
```

1. この action から `UPDATE_BRANCH_REQUEST_V1` を組み立てる:

   ```yaml
   UPDATE_BRANCH_REQUEST_V1:
     repo: <REPO>
     pr_number: <PR_NUMBER>
     expected_head_sha: <selected_action.expected_head_sha>
     update_method: merge_only
     caller: impl-review-loop.step-5
   ```

2. `implementation-worker` に `UPDATE_BRANCH_REQUEST_V1` を渡して委譲する。`implementation-worker` は
   `.claude/skills/implement-issue/scripts/update_branch.py` の canonical invocation を実行する
   worker 実行経路である。REST 呼び出し・202 poll・403 / 422 分岐は `update_branch.py` wrapper 内部の
   責務であり、orchestrator は `UPDATE_BRANCH_RESULT_V1` のみを消費する。詳細は `implement-issue`
   SKILL.md の `## update_branch Contract` セクションを参照。

3. `UPDATE_BRANCH_RESULT_V1` を受け取り、`status` で分岐する:

   | status | 次アクション |
   |---|---|
   | `ok` | Step 2（test-runner）→ Step 4（pr-review-judge）→ Step 5 再実行 |
   | `stale_verdict` | Step 4（pr-review-judge）re-review → Step 5 再実行 |
   | `forbidden` | `termination_reason: human_escalation` を記録して停止 |
   | `validation_failed` | `termination_reason: human_escalation` を記録して停止 |
   | `timeout` | `termination_reason: human_escalation` を記録して停止 |
   | `human_escalation` | 停止して人間判断を仰ぐ |

4. 更新後に `mergeable=CONFLICTING` または `merge_state_status=DIRTY` を検出した場合: `CONFLICTING PR Escalation Runbook` を発動する

## 出力

`route_loop_verdict_v2()` の `RouteDecision` を LOOP_STATE の代わりに control-plane が in-memory で保持し、
`step-5-feedback-and-termination.md` の判定マトリクスに従って次アクションを決定する。
resume/compaction 後は保存された decision を再利用せず、PR head と reviewer 結果を再取得してから再実行する。
