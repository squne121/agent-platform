# Step 5: 判定 / 終了 / フィードバック循環

> **移管状況 (I-4-4D3)**: 本書は移管元 `squne121/loop-protocol` tag `pre-split-agent-platform-20261020` (commit `6fa07b6a0ab932a62f08db6a8d521c0dfded2075`) の同名文書を最小差分で移管したもの。差分は `docs/dev/i4-4d3-step4-step5-manifest.json` の `intentional_diff` に記録している。本書が参照する `SKILL.md`・Agent 定義 (`pr-reviewer` / `test-runner` / `implementation-worker`)・`implement-issue` / `open-pr` / `pr-review-judge`・他の steps・補助 script 等のうち、本 repository に未移管のものは後続の I-4-4D スライスの担当であり、現時点では存在しない。未移管の対象を起動できるものとして扱ってはならない。本書の記述だけで Step 4 / Step 5 / Final Gate が本番稼働していると解釈してはならない。

Step 2-4 の結果を統合して、ループを次イテレーションに進めるか終了するかを判定する。

## 終了条件マトリクス（Issue #1873、`route_loop_verdict_v2()` の `route` を正本とする）

### Human-history の投稿と PR head の安全性（Issue #1908）

Step 5 は machine-readable verdict comment を移動・更新・削除しない。route を確定した後、
new human-history だけを `context-protocol-and-guardrails.md#human-history-v1` の identity / template
contract で emit する。`publish_termination_report.py::publish_human_history()` が既存
`issue_comment.publish` lane に渡す target は次の matrix 固定値である。

| origin | phase / reason | target | reviewed_ref |
| --- | --- | --- | --- |
| pre PR review | `pre-PR-binding` / completed, needs_fix, human_judgment | source Issue | review 直前 Issue body SHA-256 |
| binding invalid | `binding-validation` / binding_missing, binding_ambiguous, binding_wrong_repo, binding_gone | source Issue | failure 時 Issue body SHA-256 |
| valid PR review | `post-PR-binding` / completed, needs_fix, human_judgment | bound PR | review 直前 `refs/pull/<pr>/head@<head>` |
| target drift | `post-PR-head-drift` / head_drift | bound PR | original stale PR snapshot |
| repeated same-iteration conflict | `conflict-resolution` / human_escalation | origin target | origin mapping の reviewed_ref |

post-PR では、snapshot → review → direct PR-head read の順で処理し、primary の create/PATCH/noop
の各 decision の直前に direct PR-head read を行う。不一致なら old primary identity を mutation
せず、既存の `head_drift` reconciliation を開始する。diagnostic の decision 前には direct-read head が
stale evidence と一致しなければならず、不一致なら latest head で**同じ** diagnostic identity を
再 reconcile する。accepted noop と全ての controlled write/readback の後にも head を再読し、latest
diagnostic の successful readback または valid noop reconciliation の後にだけ re-review する。
`conflict_hard_stop` 単独では history を emit しない。


reviewer_verdict（`verdict`/`reviewed_head_sha`/`blockers`/`warnings`）と live_mergeability
（`gh pr view` で取得した `mergeable`/`merge_state_status`）を `step5-terminal-gate` に渡し、出力の `route` で分岐する。
詳細な `route` 一覧と判定条件は `step-5-mergeability-handling.md` を参照。

| `route` | アクション |
|---|---|
| `approved` | `termination_reason: approved` を立て、終了処理へ |
| `route_to_update_branch` | 合成された `update_branch` action を worker に委譲し、検証・PR review を再実行（終了しない） |
| `route_scope_clean_reconciliation`（#2102） | main drift による base-bound evidence の選択的失効を実行してから Step 5 を resume する（終了しない。手順は下記「main drift の scope-clean reconciliation 再開（resume）手順」参照） |
| `route_stale_head_rereview` | 現在 head で PR review を再実行（終了しない） |
| body-only lane（`REQUEST_CHANGES` かつ `decide_body_only_repair` が eligible。`conflict_hard_stop` / `already_satisfied` / `route_to_update_branch` のいずれにも一致しなかった場合のみ評価。下記「body-only lane」節） | iteration を消費せず、`iteration` が `max_iterations` 未満・到達のどちらでも `continue_loop` に優先して body-only repair → fresh pr-review → `step5-terminal-gate`（終了しない） |
| `continue_loop` | LOOP_STATE.iteration += 1、Step 1 に戻る（blockers を fix_delta として渡す） |
| `already_satisfied`（#2607。`step-5-mergeability-handling.md` の「already_satisfied recovery route」参照） | `termination_reason: already_satisfied` を立て、終了処理へ。`decision.selected_action` の `result`/`recommendation` を報告するのみで、PR/Issue の close 等の mutation は実行しない |
| `route_human_escalation` | `termination_reason: human_escalation` を立て、即停止（`HUMAN_REVIEW_REQUIRED` verdict、または max iteration 到達・secret/protected-path gate 等の実 hard gate の場合のみ） |
| `conflict_hard_stop` | CONFLICTING PR Escalation Runbook 発動（actual conflict のみ。#1860 Owner Decision の唯一の hard stop） |
| `fail_closed`（`mergeability_unknown` / `merge_state_status_*_not_conflict_defer_to_ci_evaluator`） | warning として記録し、bounded retry または次サイクルでの current-head CI / branch-protection 再評価に委ねる（human escalation にはしない） |
| `fail_closed`（`LOOP_STATE.iteration >= LOOP_STATE.max_iterations`。`decide_body_only_repair` が eligible の body-only lane を除く） | `termination_reason: max_iterations` を立て、fail-close で人間判断 |
| `fail_closed`（`concurrent_base_churn_budget_exhausted`） | `evidence_epoch.drift_rebind_attempts` が上限（既定 2、#2039/#1023 の bounded no-progress budget と同じ考え方）を超過。drift 起因の再試行を打ち切り `termination_reason: human_escalation` ではなく機械的な fail-closed 停止として人間判断を仰ぐ（#2102） |

### terminal approval は `step5-terminal-gate` 経由に固定する（Issue #2837）

上表の `approved` は、`step5-terminal-gate` が exit 0 を返した場合にのみ成立する。

`termination_reason: approved` / `merge_ready: true` を確定する terminal approval は、`route_loop_verdict_v2()` を直接呼んだ結果ではなく、`adjudicate_vc_result.py step5-terminal-gate` の出力（exit 0 = approved）だけを根拠にする。この subcommand は既存の公開 wrapper `route_loop_verdict_v2_resolve_semantic_ambiguity()`（`main_drift` が `semantic_ambiguity` を省略している場合に実 git oracle で補完する現行の production 経路。caller が `semantic_ambiguity` を推測・固定値で渡してはならない）を呼んだうえで、route が `approved` の場合に限り次をすべて要求する。VC の再実行は行わず、新しい判定 schema・第二の分類器・新 route 定数も作らない。

1. `--dispatch-seq` が `loop_state["dispatch"]["seq"]` と一致する（不一致、または `dispatch` が欠落・不正: `route: continue_loop` / `reason_code: dispatch_seq_mismatch` / `rerun_required.pr_review: true`）。
2. `loop_state["dispatch"]["binding_key"]` が live の HEAD / Issue body SHA-256 / 順序付き command hashes から再計算した key と一致する（不一致: `continue_loop` / `binding_changed_since_dispatch` / `rerun_required` の `verification` と `pr_review` が true）。あわせて `--live-mergeability-file` の `head_sha` が `--expected-head-sha` と一致することも要求する（reviewer / mergeability 側の HEAD と VC 側の binding HEAD が食い違った split-head の合成承認を防ぐ。不一致や `head_sha` 欠落・非 object は fail-closed で同じ `binding_changed_since_dispatch`）。
3. 当該 binding の VC adjudication が `step4_gate_from_loop_state()` で有効（無効: `continue_loop` / `vc_gate_blocking` / `rerun_required.verification: true`）。

**`pr_review_only` の実行失敗を reviewer へ委譲した場合（Issue #2916）**: `step4-adjudicate --delegate-pr-review-only-nonpass` が exit 0 を返しても、それは reviewer の起動許可であり、AC 達成でも terminal approval でもない。永続化された adjudication は `overall_status: indeterminate` / `blocking: true` のままで、実行失敗の事実が `per_ac[].failure_keys` に残っている。terminal approval は、reviewer が失敗の事実・AC・diff を照合して APPROVE を返し、`route_loop_verdict_v2()` が `approved` を返し、かつ上記 1-3 を満たした場合にだけ成立する。reviewer が REQUEST_CHANGES、未判断（verdict が `APPROVE` でない値、または verdict file が不在・不正で `reviewer_verdict_malformed` / exit 2）の間は、`step5-terminal-gate` は exit 0 にならない。`step4_gate_from_loop_state()` は委譲された entry の形（non-pass の事実が整合し、`indeterminate` / `blocking: true` を保つこと）を検証し、事実が書き換えられた・欠落した永続化 adjudication では `vc_gate_blocking` で閉じる。この subcommand は reviewer の APPROVE が失敗の事実を踏まえた判断かどうかを機械的には検証できないため、委譲した場合は pr-reviewer への依頼に失敗の事実（`failure_keys`）を明示的に渡す。

評価順序と `reason_code` の優先順位は `dispatch_seq_mismatch` -> `binding_changed_since_dispatch` -> `vc_gate_blocking` で固定である。`approved` 以外の route はそのまま出力される（`RouteDecision` と同形の plain JSON）。exit code は `0=approved` / `1=approved でない`（route を出力。`continue_loop` 等の通常分岐へ進む）/ `2=malformed`（引数不備、または破損した `loop_state`）。

`--expected-head-sha` と `--live-mergeability-file` は、**同一の `gh pr view --json headRefOid,mergeable,mergeStateStatus` の 1 回の応答**から組み立てる。別々の時点・別の取得で作ったり、resume 後に残った古いファイルを混ぜたりしない（入力同士の取り違えが split-head の主因である）。`step4-adjudicate` 側の `--expected-head-sha` も同じ応答の `headRefOid` を使う。

```bash
LIVE_PR_JSON="$(gh pr view "$PR_NUMBER" --json headRefOid,mergeable,mergeStateStatus)"
LIVE_HEAD_SHA="$(jq -r .headRefOid <<<"$LIVE_PR_JSON")"
LIVE_MERGEABILITY="$REVIEW_RESULT_DIR/live_mergeability.json"
jq '{head_sha: .headRefOid, mergeable: .mergeable, merge_state_status: .mergeStateStatus}' <<<"$LIVE_PR_JSON" > "$LIVE_MERGEABILITY"

uv run python3 .claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py step5-terminal-gate \
  --loop-state-file "$LOOP_STATE_FILE" \
  --reviewer-verdict-file "$REVIEWER_VERDICT" \
  --live-mergeability-file "$LIVE_MERGEABILITY" \
  --expected-head-sha "$LIVE_HEAD_SHA" \
  --expected-contract-body-sha256 "$LIVE_BODY_SHA256" \
  --expected-command-hashes-file "$EXPECTED_COMMAND_HASHES" \
  --dispatch-seq "$(cat "$REVIEW_RESULT_DIR/dispatch_seq")"
```

`--dispatch-seq` には、reviewer を起動する **前** に root が reviewer 結果の保存先と同じ場所へ書き残した `seq`（`step-4-pr-review.md` 参照）を渡す。resume / compaction 後も `loop_state` の最新 `seq` を再読込して渡してはならない（再読込すると検査が空洞化する。reviewer 結果を dispatch に束縛する唯一の手段である）。新しい reviewer を起動し直した（`step4-adjudicate` が再び `invoke` を返した）場合は `seq` が進み、古い reviewer 結果は `dispatch_seq_mismatch` で拒否される。

dispatch 後に HEAD / Issue body / VC binding（command hashes）のいずれかが **実際に** 変化した場合は、古い reviewer 結果で終端承認してはならない（`binding_changed_since_dispatch`）。この場合は `step-4-pr-review.md` の手順で VC を再検証し、新しい reviewer を起動して `seq` を進めてから Step 5 を再実行する。

`binding_changed_since_dispatch` が `live_mergeability_head_sha_differs_from_expected_head_sha` を示している場合は、まず上記の 1 回の `gh pr view` 応答で `--expected-head-sha` と live mergeability file を取り直し、入力の取り違えかどうかを確認する。取り直した live HEAD が dispatch 時の binding HEAD と同一であれば、入力の取り違えにすぎないので full verification / pr-review を再実行せず、metadata を取り直して Step 5 を再実行するだけでよい。実際に HEAD（または Issue body / command hashes）が変わっていた場合のみ、その HEAD について Step 2 / Step 4 を fresh にやり直す。

### main drift の scope-clean reconciliation 再開（resume）手順（#2102）

`route: route_scope_clean_reconciliation` は Step 5 を終了させず、`decision.selected_action`
（`kind: scope_clean_reconciliation`、`evidence_epoch`、`reusable_evidence`）を使って以下を実行してから
同一サイクル内で Step 5 を再実行（resume）する:

1. `decision.rerun_required`（`snapshot`/`ci`/`review`）で `true` になっている証跡だけを再取得する。
   `reusable_evidence` が `null` になっているキーは再利用禁止（stale の同一 URL / SHA を使い回さない）。
2. `snapshot: true` の場合、issue-refinement-loop 側の contract-snapshot producer を
   `decision.selected_action.evidence_epoch.base_sha`（= current base）に束縛して再生成し、
   新しい trusted source（comment/artifact）の ID を取得する。
3. `ci: true` の場合、current head に対する required CI の再実行状態を再取得する（stale run の
   `GITHUB_SHA` を re-check の代わりに使わない）。
4. `review: true` の場合、Step 4（pr-review-judge）を current head に対して再実行し、新しい
   `reviewer_verdict` を取得する。
5. 上記が揃ったら、`live_mergeability.main_drift.evidence_base_sha` を
   `decision.selected_action.evidence_epoch.base_sha` に更新した上で Step 5 を再実行する。
   `evidence_epoch.implementation_iteration_delta` は `0` なので、この resume は
   `LOOP_STATE.iteration` を消費しない。
6. `evidence_epoch.drift_rebind_attempts` を 1 加算して LOOP_STATE 側に永続化する。上限超過時は
   `fail_closed`（`concurrent_base_churn_budget_exhausted`）に遷移する（上表参照）。

> **注意**: `verdict: APPROVE` 単独では `termination_reason: approved` に到達しない。
> `route_loop_verdict_v2()` が live mergeability（`CLEAN`/`HAS_HOOKS` かつ `MERGEABLE`）を確認し、
> `blockers == []` である場合にのみ `route: approved` を返す。

## human_review_required の扱い（#1869 fix_delta P0-4）

Step 1-4 の SubAgent が返す `human_review_required: true`（真偽値フィールドとしての自己申告）は、
semantic planning・overlap・contract snapshot・body SHA・artifact 異常に起因するものが大半であり、
それ自体には停止権限がない（#1860 Owner Decision）。以下に再定義する:

- `human_review_required: true` を受け取った場合、`termination_reason: human_escalation` を
  **自動では立てない**。理由・evidence を warning として LOOP_STATE / 終了報告コメントに記録し、
  iteration 余裕があれば Step 1 へ戻って継続する。
- 本ループを停止する human veto は、以下のいずれかを **live Issue または live PR コメント上で
  直接確認できた場合に限定** する:
  - current owner による明示的な停止指示（例: PR/Issue コメントでの `REQUEST_CHANGES` や
    「停止してください」の明示発話）
  - `docs/dev/secret-policy.md` の Decision Gate 未通過（secret 関連）
  - `git conflict` / target PR の GitHub mergeability（`mergeable == CONFLICTING` または
    `merge_state_status == DIRTY`。`step-5-mergeability-handling.md` 参照）
- 上記に該当しない `human_review_required: true`（scope-rollup missing、contract snapshot
  invalid、overlap ambiguous 等）は、ループを止める理由にしない。
- これは pr-reviewer の `verdict` が第一級の `HUMAN_REVIEW_REQUIRED` 値である場合（上記
  終了条件マトリクスの `route_human_escalation`）とは別概念である。`verdict:
  HUMAN_REVIEW_REQUIRED` は reviewer の正式な判定結果であり、`route_loop_verdict_v2()` の
  routing に従って正当に human escalation する。

## update_branch の処理手順（Issue #1873: reviewer 自己申告を廃止、control-plane が合成）

`route_loop_verdict_v2()` が `route: route_to_update_branch` を返した場合、`decision.selected_action`
（`kind: update_branch` / `executor: implementation-worker` / `skill: implement-issue.update_branch` /
`mechanical: true` / `expected_head_sha`）を `implementation-worker` に委譲する。

1. `selected_action` を `implementation-worker` に委譲する
2. worker result の `status` で分岐する:
   - `failed` / `blocked` / `permission_blocked` → `termination_reason: human_escalation` で停止
   - `ok` → verification と PR review の両方を再実行する（`update_branch` は常に head SHA を変える）
3. `reviewed_head_sha` が現在 head と不一致の場合、dispatch 前に PR review を再実行する
4. 再実行後に得られた新しい `reviewer_verdict` / `live_mergeability` で再度 `route_loop_verdict_v2()` を評価する

body-only な自動修正（`update_pr_body_hygiene`、reference authority entrypoint が `closing_required` を返した場合に限る `ensure_closing_keyword` 追加等）が必要な場合も、reviewer は
自己申告せず、具体的な内容を `blockers[]`/`warnings[]` に記載する。
control-plane は blocker 文面を自由に解釈せず、body-only 修正を行うかどうかを `decide_body_only_repair`
（`.claude/skills/impl-review-loop/scripts/body_only_repair_plan.py`）の結果だけで判断する。eligible なら
下記「body-only lane」節の手順で `implementation-worker` の `update_pr_body_hygiene` mode に委譲し、ineligible なら
`REQUEST_CHANGES` として `continue_loop` 経路で次イテレーションへ回す。`ensure_closing_keyword`
（entrypoint が `closing_required` を返した場合のみ）は引き続き #2878 の single evaluator が所有し、
`decide_body_only_repair` の eligibility 対象に含めない。これらの body-only 対応は head SHA を変えないため、`update_branch`
と異なり verification の再実行は不要で、PR review のみ再実行する。`blockers` が非空のまま
`verdict == APPROVE` を返した reviewer 結果は `route_loop_verdict_v2()` が `fail_closed`
（`approve_with_blockers_inconsistent`）として扱う。

### body-only lane（`iteration ≥ max_iterations` の fail-close に対する例外、Issue #2971）

current PR HEAD の verification / runtime evidence / required CI が完了済みで、pr-reviewer の blockers が **PR 本文に既にある evidence の同期だけ**（`## Runtime Verification Evidence` section 欠落・stale な件数・pending 文言の closed な 3 kind）に限られる場合に限り、`iteration ≥ max_iterations` でも implementation iteration を消費せず body-only repair を 1 回だけ行える。eligibility は `decide_body_only_repair`（pure な単一 authority。新規 route / schema / registry / lock は作らない）だけが決め、`SKILL.md` の「body-only lane」節と同一の意味論に従う。 実行は production CLI（`body_only_repair_plan.py` の `plan` / `record` / `guard` / `ci-freshness`）と既存 `IMPLEMENTATION_WORKER_REQUEST_V2`（`update_pr_body_hygiene` mode）だけで行い、dry-run 専用経路とは別である。

**適用条件（bounded 規則）**:

- `REQUEST_CHANGES` で `decide_body_only_repair` が eligible の場合のみ、`iteration` が `max_iterations` 未満・到達のどちらでも `continue_loop → Step 1` に優先して適用する。`iteration ≥ max_iterations` であること自体は eligibility を妨げない。
- 終了条件表の `conflict_hard_stop`（`mergeable == CONFLICTING` または `merge_state_status == DIRTY`、verdict に関係なく最優先）・`already_satisfied`・`route_to_update_branch`（`merge_state_status == BEHIND`）のいずれにも一致しなかった場合にのみ評価する。mergeability が `CONFLICTING` / `DIRTY` / `BEHIND` の場合は lane は適用不能で既存 routing に従う。mergeability の gate は control-plane が呼出し前に行い、`decide_body_only_repair` に mergeability の引数は追加しない。
- eligible な blocker は blocker 全文が closed grammar に完全一致する 3 kind（`runtime_evidence_section_missing` / `stale_count` / `pending_wording`）だけで、未知の substantive 句が残る blocker・code / test / Issue contract / branch の変更を要する blocker が 1 件でもあれば ineligible となり、従来どおり iteration を消費する（`max_iterations` 到達時は fail-close）。
- lane の消費は、`record` が `--loop-state-file`（`step4-adjudicate` / `step5-terminal-gate` と同じ file）の既存 `LOOP_STATE.blockers_history[]` へ書く二段階 entry で数える。entry の field は closed set `{lane, outcome}` だけで、worker 起動の **直前**に `{lane: body_only_repair, outcome: dispatched}` を 1 件追記し、worker が guard で拒否して mutation を行わなかった（`status: blocked` かつ `wrapper_used: false`）場合だけ、その entry を `outcome: no_mutation` へ更新する。次回の `prior_body_only_repairs` は `outcome != no_mutation` の entry 件数（crash / resume で `dispatched` のまま残った entry、`update_pr.py` に到達した成否不問の mutation は消費済み）で、`prior_body_only_repairs >= 1` は ineligible となるため、mutation を伴う lane は同一 PR で最大 1 回である。`LOOP_STATE` のキー集合は変更せず、新規 ledger / lock も作らない。JSON の手編集はしない（`record` は `blockers_history[]` だけを触る canonical writer）。
- guard 拒否（`no_mutation`）後の再評価・再 dispatch は、`outcome: no_mutation` の entry が **ちょうど 1 件**の間だけ 1 回許される。`no_mutation` が 2 件になった時点で lane は終了し通常 routing（`continue_loop` / `max_iterations` fail-close）に戻る。件数は同じ `--loop-state-file` から数えるため、compaction / resume 後も再評価上限はリセットされない。
- `plan` が `stale_body_rebuild_required` / `ineligible_head_changed` 相当（`expected_head_sha_mismatch` / `live_body_hash_mismatch`）で ineligible を返した場合は mutation を行わず（`no_mutation` としてカウントしない）、fresh な live 状態で eligibility を最大 1 回だけ再評価する。再び ineligible または stale なら通常 routing へ戻る。

**実行順序（canonical fresh review path）**:

control-plane は次の production CLI（`body_only_repair_plan.py` の `plan` / `record` / `guard` / `ci-freshness`。いずれも JSON-in / JSON-out で、dry-run 専用 path とは別）だけを使い、Python import・ad-hoc JSON・未定義 prompt field を即興で作らない。exit code は 0 = 肯定（eligible / proceed / fresh / recorded）、1 = 否定、2 = runtime error。

1. `plan` を実行する（live の head / PR body は CLI 自身が `gh pr view` で取得し、`check_body_freshness` が照合する plan 束縛値 `expected_head_sha` / `expected_live_body_sha256` を返す）。
   必須引数は `--repo` / `--pr-number` / `--issue-number`（`update_pr.py --linked-issue` と同一） / `--worktree`（対象 PR の implementation worktree。changed paths 解決の cwd） / `--reviewer-result-file`（verdict / `reviewed_head_sha` / `blockers` を持つ reviewer result） / `--test-verdict-file`（`TEST_VERDICT_MACHINE/v2` report。`$TEST_RUNNER_REPORT` と同一） / `--wait-ci-output`（`wait_ci_checks.py --required` の出力 file） / `--loop-state-file` / `--expected-contract-body-sha256` / `--expected-command-hashes-file`（この 3 つは既存 `step4-gate` の期待値と同一。VC の current-head 判定は第二の分類器を作らず既存 `adjudicate_vc_result.py step4-gate` に委ねる） / `--body-out`（completed body の書き出し先）。任意引数は `--runtime-summary-file`（`run_worktree_agent_runtime_smoke.py` の `summary.md`）。`--expected-contract-body-sha256` には既存 `step4-gate` と同じく `sha256:` 付き（`sha256:<64 桁 hex>`）の値を渡す（`plan` が返す `expected_live_body_sha256` / `body_file_sha256` は `sha256:` なしの hex で、別の値である）。

   ```bash
   uv run --locked python3 .claude/skills/impl-review-loop/scripts/body_only_repair_plan.py plan \
     --repo <owner/repo> --pr-number <N> --issue-number <ISSUE> \
     --worktree <PR の implementation worktree> \
     --reviewer-result-file <reviewer result file> \
     --test-verdict-file <TEST_VERDICT report file> \
     --wait-ci-output <wait_ci_checks.py 出力 file> \
     [--runtime-summary-file <summary.md>] \
     --loop-state-file <LOOP_STATE file> \
     --expected-contract-body-sha256 <live Issue 本文の SHA-256> \
     --expected-command-hashes-file <step4-gate の期待 command hash file> \
     --body-out <completed body の出力 path>
   ```

   stdout は JSON 1 件 `{eligible, reason_codes, expected_head_sha, expected_live_body_sha256, body_file_path, body_file_sha256}`。exit 0 = eligible、1 = ineligible（lane を消費しない）。ineligible または exit 非 0 の場合は lane を使わず通常 routing に従う。
2. eligible の場合だけ、worker 起動の **直前**に `record` で dispatch を記録する（この呼出しが lane 消費の唯一の writer）。

   ```bash
   uv run --locked python3 .claude/skills/impl-review-loop/scripts/body_only_repair_plan.py record --loop-state-file <LOOP_STATE file> --state dispatched
   ```

3. `plan` の返した値を **そのまま**、既存 `IMPLEMENTATION_WORKER_REQUEST_V2` の `update_pr_body_hygiene` mode の明示 field として `implementation-worker` へ渡す。cwd は対象 PR の implementation worktree。

   ```yaml
   IMPLEMENTATION_WORKER_REQUEST_V2:
     mode: update_pr_body_hygiene
     required_auto_action: {kind: update_pr_body_hygiene}
     pr_number: <N>
     issue_number: <ISSUE>                          # 必須（update_pr.py --linked-issue へ渡す）
     expected_head_sha: <plan の expected_head_sha>      # 必須
     body_file_path: <plan の body_file_path>
     body_file_sha256: <plan の body_file_sha256>
     expected_live_body_sha256: <plan の expected_live_body_sha256>
   ```

   worker は mutation 直前に `guard`（live の head / body を CLI 自身が取得して照合）を実行し、`proceed` の場合だけ body file を **改変せず** `open-pr/scripts/update_pr.py --body-file ... --linked-issue ...` wrapper へそのまま渡す（`gh pr edit` の直接呼出し禁止）。`expected_head_sha` 不一致は `reason_code: expected_head_sha_mismatch`、live body hash または body file hash の不一致は `reason_code: live_body_hash_mismatch` で、overwrite せず `status: blocked` / `wrapper_used: false` を返す。`guard` の手順と限界は `implementation-worker.md` の `update_pr_body_hygiene` mode を参照する。worker が実行する `guard` の invocation は次のとおり（control-plane が mutation 前にこれを代行・省略してはならない）。

   ```bash
   uv run --locked python3 .claude/skills/impl-review-loop/scripts/body_only_repair_plan.py guard \
     --repo <owner/repo> --pr-number <N> \
     --expected-head-sha <expected_head_sha> \
     --expected-live-body-sha256 <expected_live_body_sha256> \
     --body-file <body_file_path> --body-file-sha256 <body_file_sha256>
   ```
4. worker が `status: blocked` かつ `wrapper_used: false`（guard 拒否）を返した直後に限り、`record --state no_mutation` で直近の `dispatched` entry を更新し、上記の再評価規則（`no_mutation` がちょうど 1 件の間だけ再評価 1 回、2 件で通常 routing）に従う。`update_pr.py` に到達した場合は成否を問わず `dispatched` のまま消費済みとする。

   ```bash
   uv run --locked python3 .claude/skills/impl-review-loop/scripts/body_only_repair_plan.py record --loop-state-file <LOOP_STATE file> --state no_mutation
   ```

5. 書込み後に control-plane が readback する。`verify_body_readback` には CLI が無いため、同じ判定（HEAD が不変、かつ canonicalize 後（CRLF→LF、末尾改行除去）の live body が completed body と一致）を `guard` で等価に表現する。`--expected-head-sha` に不変の head（`plan` の `expected_head_sha`）、`--expected-live-body-sha256` に **`body_file_sha256`**（completed body の canonical hash）を渡し、live の head / body は CLI 自身が `gh pr view` で取得して照合させる。この `guard` が exit 0 で通ることが readback `ok` の条件で、exit 非 0 の場合は terminal approval に進まず通常 routing に従う。

   ```bash
   uv run --locked python3 .claude/skills/impl-review-loop/scripts/body_only_repair_plan.py guard \
     --repo <owner/repo> --pr-number <N> \
     --expected-head-sha <不変の head（plan の expected_head_sha）> \
     --expected-live-body-sha256 <body_file_sha256> \
     --body-file <body_file_path> --body-file-sha256 <body_file_sha256>
   ```

   readback が `ok` の時点で `gh pr view <N> --repo <owner/repo> --json updatedAt --jq .updatedAt` の値を body edit の causality watermark（`body_edit_updatedAt`）として **1 回だけ**取得する。
6. post-edit required CI の fresh 判定: PR body の `edited` で同一 head の required check が再起動されるため、unchanged head で `wait_ci_checks.py --required` を再実行し、その出力を `ci-freshness` で判定する。この 2 つを **間隔 15 秒・合計 1800 秒の deadline** までの loop として実行する（固定 sleep で済ませず、`wait_ci_checks.py` の既存 polling を使う）。

   ```bash
   uv run --locked python3 .claude/skills/impl-review-loop/scripts/wait_ci_checks.py \
     --repo <owner/repo> --pr <N> --head-sha <expected_head_sha> --required > <wait-ci-output file>
   uv run --locked python3 .claude/skills/impl-review-loop/scripts/body_only_repair_plan.py ci-freshness \
     --wait-ci-output <wait-ci-output file> \
     --body-edit-updated-at <body_edit_updatedAt> \
     --workflow-dir .github/workflows --expected-head-sha <expected_head_sha>
   ```

   `ci-freshness` は各 required check の `workflow` 名を `.github/workflows/*.yml` の `name:` と突き合わせ、`on.pull_request.types` に `edited` を含む workflow の check は `startedAt >= body_edit_updatedAt` の pass だけを fresh とし（`startedAt < body_edit_updatedAt` の pass は `stale_pre_edit`）、`edited` を含まない workflow の check は head 一致の pass を `head_bound` として受理し、workflow を特定できない check は `unknown_workflow` で fail-closed とする。exit 0（全 required check が `fresh` / `head_bound`）の場合だけ live mergeability を取得して次へ進む。exit 非 0 かつ出力 `retryable: true` の間だけ 15 秒間隔で再実行し、deadline（1800 秒）超過・`retryable: false` は terminal approval に進まず通常 routing に戻る。`wait_ci_checks.py` は変更しない。watermark は readback 直後の同一の値を使い続けるため、PR comment 等で `updatedAt` が進んだ場合の永続的な `stale_pre_edit` も deadline で終端する。これは verification の再実行ではない。
7. verification は再実行しない（HEAD が不変のため）。binding（head / Issue body SHA-256 / 順序付き command hashes）が不変なら `step4-adjudicate --reuse-stored` で `dispatch.seq` を +1 し、reviewer を起動する **前** に `<review-result-dir>/dispatch_seq` へ保存する。binding が変化した場合は `--reuse-stored` を使わず通常の `step4-adjudicate`（再検証あり）へ戻り、lane は消費済みとして通常 routing に従う。
8. 新規 `pr-reviewer` を dispatch し、fresh reviewer completion を得る。
9. 保存済み `dispatch_seq` と同一 binding を上記の `step5-terminal-gate` へ渡す。`APPROVE` かつ `blockers == []` かつ live mergeability が適格な場合のみ `approved`。fresh reviewer が code / test / contract change を要求した場合は通常 routing（iteration 消費・`max_iterations` fail-close）へ戻る。

**guard の所在と禁止事項**:

- worker は body file field を伴う `update_pr_body_hygiene` で `expected_head_sha` を強制する。mutation 直前に `guard` を実行し、live head が `expected_head_sha` と異なる、live body の canonical hash が `expected_live_body_sha256` と異なる、または body file の canonical hash が `body_file_sha256` と異なる場合は overwrite せず `status: blocked`（`wrapper_used: false`）を返す。control-plane は `plan` 時点の `check_body_freshness` 相当の判定と、書込み後の `verify_body_readback` の判定（readback）で HEAD / body を再確認する。`update_pr.py` 自体に head / body の freshness 検査や readback は無い。
- **best-effort の限界**: GitHub に PR body の compare-and-swap は無く、`guard` は best-effort の optimistic guard である。`guard` 通過後から `update_pr.py` の全置換までの race window は残り、その間に他 actor が行った編集は失われ得る（residual risk）。lost update の絶対防止は主張せず、lock / approval layer / persistent coordination は追加しない。
- **artifact の来歴は best-effort**: `plan` が evidence ref に使う `summary.md` / pytest 出力は head field を持たないため、worktree HEAD が live head と一致し artifact の mtime が当該 HEAD の commit 時刻以降であることを pre-filter にしている。これは artifact の head 束縛に関する best-effort の来歴確認であり、artifact の真正性は証明しない（residual risk）。`TEST_VERDICT` / CI 出力は明示の head field で束縛する。
- 次を禁止する: terminal gate bypass（`step5-terminal-gate` exit 0 以外での `approved` 確定）、古い reviewer result の carry-forward（lane 前の reviewer 結果の流用）、reviewer の直接呼出しのみでの fresh review 成立扱い（`step4-adjudicate --reuse-stored` による `dispatch_seq` の +1 と保存、`step5-terminal-gate` への受け渡しを省略する経路）。
- `adjudicate_vc_result.py` / `route_loop_verdict_v2.py` / `step5-terminal-gate` に max-iteration の例外は入れない。terminal approval は引き続き `step5-terminal-gate` だけが authority である。

### `ensure_closing_keyword` の適用条件（reference authority entrypoint、Issue #2878）

`ensure_closing_keyword`（`Closes #N` 追記による body-only repair）は無条件の自動修復ではない。`Refs #N` が妥当な PR
（post-merge live evidence を待つ #2842 型の OPEN Issue 等）に `Closes` を追記すると、merge が Issue を live evidence より先に close する。
repair 判定は `open-pr` の単一 evaluator の entrypoint 結果（`decision` / `body_verdict` / `body_reason`）だけで行い、
control-plane は facts（linked Issue の state / 本文、A1 の comment 事実）を gh で fresh 取得して次を実行する（grammar を再実装しない。`action` 名・schema は変更しない）:

```bash
uv run --locked python3 .claude/skills/open-pr/scripts/validate_pr_body.py --evaluate-reference-policy --body-file <PR本文ファイル> --linked-issue <N> --linked-issue-body-file <Issue本文ファイル> --reference-facts-file <facts JSON>
```

出力 JSON の `reason_code` は `issue_closed` / `a1_explicit_decision` / `a1_decision_invalid` / `a1_decision_ambiguous` / `a2_contract_deferred` / `a3_close_ready` / `runtime_applicability_unresolved` / `facts_invalid` のいずれか。
step-5 の扱いは次の表が正本で、表に無い組合せは `blocker` として扱う:

| decision | body_verdict | body_reason | step-5 の扱い |
|---|---|---|---|
| closing_required | repair | closing_missing | repair（`ensure_closing_keyword`: 根拠のない Refs-only に `Closes #N` を追記） |
| closing_required | block | reference_missing | repair（`ensure_closing_keyword`: reference が無い本文に `Closes #N` を追記） |
| closing_required | valid | ok | none（repair 不要） |
| nonclosing_required | valid | ok | none（`Refs` が正しい reference。`Closes` へ戻さない） |
| nonclosing_required | block | closing_forbidden | blocker（closing keyword は auto repair しない） |
| nonclosing_required | block | reference_missing | blocker（auto repair しない） |
| nonclosing_required | block | closing_for_other | blocker（番号違いの closing keyword は auto repair しない） |
| closing_required | block | closing_for_other | blocker（番号違いの closing keyword は auto repair しない） |
| fail_closed | block | not_evaluated | stop（auto repair しない。`reason_code` を blocker に記載して人間判断へ） |

`nonclosing_required` / `valid` / `ok` の `none` は **PR 本文の判定**であり、本文以外の自動 close 経路（GitHub の手動 closing relation、採用される squash message の closing keyword）が無いことまでは保証しない。
`nonclosing_required`（A1 / A2）の PR は merge 前に、`validate_pr_body.py --evaluate-native-auto-close-risk`（native auto-close risk check、手順と facts の exact key は `docs/dev/workflow.md` が正本）を
**merge 直前の final message / final native relation に対して**再実行する（または `adopted_message_sha256` が一致する検証済み message を変更せず使う）。`status: blocked` / `fail_closed` の間は merge せず、`blockers[]` に `reason_code` と具体的な矛盾（relation の解除、message の修正）を記載して `REQUEST_CHANGES` とする。これは body-only repair の対象ではなく auto repair しない。PR / Issue 本文の hash だけでは自動 close の不在を保証しない。

`fail_closed`（A1 invalid / ambiguous、authority 不明、facts 不正）は常に停止し、`ensure_closing_keyword` を発行しない。
CLOSED の linked Issue（`level: CLOSED`）は常に `nonclosing_required` で、`Refs` が valid、closing keyword は `blocker`。

### worker_status_result_routing（`implementation-worker` 結果の routing）

`implementation-worker` へ `update_branch` action（またはその他の機械的修正）を委譲した結果の `status` は
以下の table で分岐する:

```yaml
worker_status_result_routing:
  worker_status_failed:
    route: human_escalation
  worker_status_blocked:
    route: human_escalation
  worker_status_permission_blocked:
    route: human_escalation
  worker_status_stale_verdict:
    route: rereview
    note: "reviewed_head_sha が変わっており verdict が stale。人間判断を仰がず Step 4（pr-review-judge）を re-review してから Step 5 を再実行する（step-5-mergeability-handling.md と整合。stale は route_loop_verdict_v2() 自身も route_stale_head_rereview として扱い、human_escalation にはしない）"
  worker_status_forbidden:
    route: human_escalation
    note: "403 Forbidden — 権限確認が必要"
  worker_status_validation_failed:
    route: human_escalation
    note: "422 Validation failed（expected_head_sha 不一致等）"
  worker_status_timeout:
    route: human_escalation
    note: "タイムアウト"
  worker_status_ok_rerun_required_true:
    route: "rerun verification and pr_review"
    note: "ok でも rerun_required: true の場合は即終了しない"
```

## branch publish の deterministic retry / safety stop（決定的な再試行と安全停止）

branch publish が hook / approval 境界または remote head drift で止まった場合、`gh pr create` の再試行前に次の read-only preflight を必須とする。これは独立した Git push safety 境界であり、semantic review verdict とは無関係に維持する。

1. `git ls-remote --refs --exit-code origin refs/heads/<branch>` または GitHub Branch API で live remote head を読む
2. local remote-tracking ref を使う場合は、同一 decision cycle 内の fetch 成功を `remote_readback_source: fetch_then_show_ref` として記録する
3. `expected_remote_head`、`current_remote_head`、`local_head`、`verified_head`、`declared_publish_head`、`allowed_paths_gate_status`、`remote_readback_source`、`decision_inputs_complete` を `PUBLISH_LANE_DECISION_V1` で照合する
4. `status: allow_retry` の場合だけ bounded publish command を再試行する
5. 不一致時は `PUBLISH_SAFETY_STOP_REPORT_V1` を残し、manual remote update や force update に暗黙フォールバックしない

strict publish lane を Codex hook 側で再利用する場合、terminal command に env binding を付けない。
repo-approved `codex-hook-adapter.mjs` が hook process と fresh git probe から
`CONTROLLED_PUBLISH_CONTEXT_V1` を生成し、policy CLI へ invocation-scoped JSON として注入する。
canonical existing-branch push は policy の controlled transaction が 1 回だけ実行して readback を確認し、
外側の shell command は deny して二重実行を防ぐ。`env LOOP_PUBLISH_...=... rtk git push ...` は
`context_invalid` として fail-closed に扱う。

```yaml
CONTROLLED_PUBLISH_CONTEXT_V1:
  repository: "squne121/loop-protocol"
  issue_number: "<issue-number>"
  active_branch: "<active-branch>"
  head: "<local-head-sha>"
  remote: "origin"
  allowed_paths_digest: "sha256:<digest>"
```

```yaml
PUBLISH_LANE_DECISION_V1:
  status: allow_retry | safety_stop
  publish_failure_reason:
    boundary_layer: worktree_scope_guard_denied
    reason_code: remote_write_requires_approval | branch_mismatch | stale_remote_head | local_head_mismatch | remote_fast_forward_by_same_scope | remote_head_scope_contamination | non_fast_forward_remote_rewrite | allowed_paths_gate_not_ok | publish_guard_context_missing | publish_guard_context_invalid
  remote_readback_source: ls_remote | github_branch_api | fetch_then_show_ref
  decision_inputs_complete: true | false
  allowed_command: null | "<bounded publish command>"
  postcondition: "remote branch head == local_head"
```

## live mergeability の routing 分類（Issue #1873: merge_ready 自己申告を廃止）

`merge_ready` は reviewer_verdict の一部として受け取らない。live mergeability
（`gh pr view` の `mergeable` / `merge_state_status`）から `route_loop_verdict_v2()` が
直接分類する。分類の詳細と根拠は `route_loop_verdict_v2.py` の `_APPROVABLE_STATUSES` /
`_DEFER_TO_CI_EVALUATOR_STATUSES` を正本とする:

| merge_state_status | route（`mergeable: MERGEABLE` 前提） | 備考 |
|---|---|---|
| `CLEAN` | `approved` | |
| `HAS_HOOKS` | `approved` | merge hooks があるが merge 可能。conflict ではない |
| `DRAFT` | `fail_closed`（defer to CI evaluator） | Draft PR は人間が ready にする。単独では human escalation にしない（#1860 Owner Decision / #1873 Delivery Rule） |
| `UNSTABLE` | `fail_closed`（defer to CI evaluator） | branch protection テスト失敗の可能性。conflict でも自動 human escalation でもない |
| `BLOCKED` | `fail_closed`（defer to CI evaluator） | branch protection 設定待ち。conflict でも自動 human escalation でもない |
| `BEHIND` | `route_to_update_branch` | `step-5-mergeability-handling.md` の BEHIND 分岐参照 |
| `DIRTY`（または `mergeable: CONFLICTING`） | `conflict_hard_stop` | CONFLICTING PR Escalation Runbook 発動。verdict に関係なく最優先 |
| `UNKNOWN`（`mergeable` または `merge_state_status`） | `fail_closed`（`mergeability_unknown`） | 5 秒待機 × 最大 3 回 retry 後も UNKNOWN なら warning として記録し、最終 merge-ready 判定のみ保留する（`human_escalation` はしない） |

`BLOCKED`/`UNSTABLE`/`DRAFT` の実 blocker（required checks が未充足、branch protection 未達成など）
は current-head required-CI / branch-protection evaluator（`wait_ci_checks.py` / `gh pr checks
--required`）の live evidence で判定する。次の test-runner / review サイクルで再評価し、
`route_loop_verdict_v2()` 自身はこれらを human escalation の理由にしない。

### draft_pr_ready

`IMPL_REVIEW_LOOP_RESULT_V1.status: draft_pr_ready` はループの終端ステータス（PR を人間マージ判断のまま
残す）であり、GitHub の Draft PR フラグとは別概念。`merge_state_status: DRAFT` は単独では
human escalation の理由にならない（`route: fail_closed`、defer to CI evaluator）。

## REQUEST_CHANGES 時の fix_delta 構築

LOOP_VERDICT.blockers と TEST_VERDICT の失敗内容から fix_delta を生成し、Step 1 の implementation-worker に渡す:

```yaml
fix_delta:
  iteration: <次の iteration 番号>
  blockers:
    - "<LOOP_VERDICT.blockers から抽出>"
  test_failures:
    - "<TEST_VERDICT.result が FAIL の場合の失敗詳細>"
  pr_review_comment_url: <pr-reviewer が投稿した verdict コメントの URL>
```

## AUTONOMY_POLICY_V1 validator gate（termination_reason: approved 前に必須）

`termination_reason: approved` を立てる前に、`validate_autonomy_policy_result.py` を実行する。
非ゼロ終了（exit 1）の場合は `termination_reason: approved` を禁止し、`human_escalation` として停止する。
この validator は agent の read-only 宣言（Edit/Write/MultiEdit tool 非付与）と終了報告 YAML の
必須フィールド充足を確認する独立した安全境界であり、semantic な review verdict の内容そのものは
検証しない。

```bash
# validate_autonomy_policy_result.py は、ループが生成した実際の終了報告ファイルを受け取る。
# $RESULT_FILE は、終了報告コメント本文を gh issue comment で投稿する前に
# 一時ファイルとして書き出したものを指す（自己生成のダミー入力ではない）。
#
# 終了報告コメント本文の例（$RESULT_FILE に書き込む実際の内容）:
#   ## impl-review-loop: 完了 (2024-01-01T00:00:00Z)
#
#   <!-- IMPL_REVIEW_LOOP_RESULT_V1 -->
#   ```yaml
#   IMPL_REVIEW_LOOP_RESULT_V1:
#     schema_version: 1
#     status: draft_pr_ready
#     termination_reason: approved
#     merge_ready: true
#     pr_url: "https://github.com/..."
#   ```
#
# RESULT_FILE は既にループの終了フローで生成されているファイルへのパスを参照する。

uv run python3 .claude/skills/impl-review-loop/scripts/validate_autonomy_policy_result.py \
  --policy docs/dev/autonomy-policy.md \
  --agent-dir .claude/agents \
  --terminal-output-file "$RESULT_FILE"

VALIDATOR_EXIT=$?

if [ "$VALIDATOR_EXIT" -ne 0 ]; then
  echo "AUTONOMY_POLICY_V1 validation failed (exit $VALIDATOR_EXIT). termination_reason: approved is prohibited."
  echo "termination_reason: human_escalation"
  exit 1
fi
```

validator が exit 0 を返した場合のみ、次の終了処理（approved）に進む。
詳細スキーマ: `docs/dev/autonomy-policy.md` の AUTONOMY_POLICY_VALIDATION_RESULT_V1 マーカースキーマ参照。

`merge_ready`（終了報告 YAML の必須フィールド）は `route_loop_verdict_v2()` への **入力**（reviewer
self-report）としては禁止されているが、終了報告の **出力**フィールドとしては live mergeability から
computed する（`merge_state_status in {CLEAN, HAS_HOOKS}` かつ `mergeable == MERGEABLE` の場合 `true`）。
reviewer からの自己申告値をそのまま転記してはならない。

## 終了処理（approved）

```bash
# LOOP_STATE を最終 YAML として会話履歴に記録
# PR は人間がマージ判断（orchestrator はマージしない）

# Issue コメントで終了報告（機械可読フィールドを含む）
gh issue comment <issue_number> --body "## impl-review-loop: 完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- iteration: <最終 iteration 数>
- verdict: APPROVE
- route: approved
- PR: <PR URL>
- 次アクション: 人間レビュー → マージ → post-merge-cleanup

\`\`\`yaml
IMPL_REVIEW_LOOP_RESULT_V1:
  schema_version: 1
  status: draft_pr_ready
  pr_url: \"<PR URL>\"
  head_sha: \"<HEAD SHA>\"
  issue_number: <ISSUE NUMBER>
  termination_reason: approved
  merge_ready: <live merge_state_status in {CLEAN, HAS_HOOKS} and mergeable == MERGEABLE>
  iteration: <最終 iteration 数>

FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: impl-review-loop
  follow_up_issues: # 空の場合も省略しない
    - request_dedupe_key: \"...\"
      status: created | reused_open
      issue:
        number: 123
        url: \"https://github.com/...\"
      reason: null
    - request_dedupe_key: \"...\"
      status: skipped_closed_duplicate | skipped_closed_not_planned | skipped_closed_completed
      issue: null
      reason: \"<skipped の理由>\"
  note_only_observations: # 空の場合も省略しない
    - dedupe_key: \"...\"
      source_url: \"...\"
      source_note_id: \"...\"
      summary: \"...\"

# 空の場合の形式（省略禁止）
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: impl-review-loop
  follow_up_issues: []
  note_only_observations: []
\`\`\`"
```

### APPROVE 時の follow-up Issue 自動起票

Issue #1873 以降、follow-up Issue 提案は専用の構造化フィールドでは受け渡さない。pr-reviewer は
follow-up 候補を `reviewer_verdict.warnings[]`（non-blocker の場合）または `blockers[]`（APPROVE を
妨げる場合）のテキストとして記載する。control-plane（main thread）はそのテキストを読み、以下の
優先度で判断する:

- APPROVE を妨げない改善提案として明記されたテキストで、独立した follow-up Issue 化が妥当と main thread が
  判断したもの → APPROVE 確定後に `issue-creator` SubAgent に委譲して `create-issue` 経由で起票する
- APPROVE 前に materialize が必須と reviewer が明記したもの（`severity: mandatory_follow_up` 相当の記述）
  → APPROVE 確定**前**に create/reuse する。未 materialize の状態で APPROVE してはならない
- 単なる観察・記録のみでよいもの → 終了報告コメントの本文に記載するのみで起票しない

新規の構造化 schema field（`follow_up_issue_requests[]` 相当）は追加しない（AC13）。

**mandatory_follow_up の処理タイミング**: `severity: mandatory_follow_up` のリクエストは APPROVE 確定**前**に create/reuse する。未 materialize の状態で APPROVE してはならない。

**delivery-rollup parent の残り child 起票（mandatory_follow_up）**:

linked issue の parent が `parent_mode: delivery-rollup` の場合、APPROVE 確定前に以下を実行する:

1. `plan_child_materialization.py` を実行して parent の残り child を確認する（read-only）:
   ```bash
   uv run python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
     --repo <owner>/<repo> \
     --issue <parent_issue_number>
   ```

2. `CHILD_MATERIALIZATION_PLAN_V2.children` に `action: create_issue` のエントリがある場合:
   - 各エントリを `severity: mandatory_follow_up` の候補として main thread が in-memory で保持する follow-up 候補リストに追加する（`FOLLOW_UP_ISSUE_REQUEST_V1` 形式の値は使うが、reviewer_verdict の schema field としては受け渡さない）
   - dedupe_key は `CHILD_MATERIALIZATION_PLAN_V2.children[*].dedupe_key` を使用する

3. `action: reuse_and_update_parent` のエントリがある場合:
   - `edit-issue` skill の `delivery-rollup-parent-update` mode に委譲して parent body の placeholder を修正する

4. `action: human_escalation` のエントリがある場合:
   - `human_review_required: true` で停止し、人間判断を仰ぐ

スキーマ: `CHILD_MATERIALIZATION_PLAN_V2` の正本は `docs/dev/agent-skill-boundaries.md` を参照。

pr-reviewer が `reviewer_verdict.warnings[]`（non-blocker の改善提案・観察事項）に記載したテキストから
main thread が follow-up 候補を抽出する場合も、上記と同じ in-memory リストに追加する
（構造化 field としては受け渡さない。値の形は参考として `docs/dev/agent-skill-boundaries.md` の
`FOLLOW_UP_ISSUE_REQUEST_V1` を流用してよい）。

```
for each req in <main thread が保持する follow-up 候補リスト>:
  - severity: mandatory_follow_up → APPROVE 前に必ず起票（dedupe_key チェック後）
  - severity: optional_follow_up → APPROVE 後に dedupe_key チェック後、重複なければ起票
  - severity: note_only → 起票せず、終了報告コメントの note_only_observations に記録

  dedupe チェック（severity: mandatory_follow_up / optional_follow_up）:
    gh issue list --repo squne121/loop-protocol --state all \
      --search '"<req.dedupe_key>"' --json number,title,url,state,stateReason,labels
    重複あり（open）→ スキップ（既存 Issue 番号を記録、status: reused_open）
    重複あり（closed / not_planned）→ 起票せずスキップ（status: skipped_closed_not_planned）
    重複あり（closed / completed）→ 起票せずスキップ（status: skipped_closed_completed）
    重複あり（closed / duplicate）→ 起票せずスキップ（status: skipped_closed_duplicate）
    重複なし → 起票（## Source セクションに dedupe_key を含める）
    ※ closed Issue を open に差し戻す場合は human escalation が必要（自動起票不可）
```

起票・スキップした follow-up Issue の情報を終了報告コメントの `follow_up_issues` フィールドに列挙する。

follow-up の起票判断そのものは advisory な提案整理であり、`termination_reason: approved` を
ブロックする独立安全境界ではない（mandatory_follow_up の materialize 完了確認を除く）。

## 終了処理（max_iterations）

```bash
gh issue comment <issue_number> --body "## impl-review-loop: max_iterations 到達 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- 上限 iteration: <max_iterations>
- 最終 blockers: <LOOP_STATE.blockers_history の最新>
- PR: <PR URL>
- 人間判断を仰ぎます: 追加 iteration を許可するか、別アプローチを検討するか"
```

## 終了処理（human_escalation）

```bash
gh issue comment <issue_number> --body "## impl-review-loop: 人間判断要請 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- 発生 step: <last_step>
- 詳細: <実 hard gate（conflict / secret / max_iteration / 明示的な停止指示）の理由>
- PR: <PR URL>
- 人間の確認後、ループ再開または別アプローチを選択してください"
```

## 終了処理（no-diff / superseded termination）

PR を作成しない no-diff / superseded termination の呼び出し規約（Issue #1116）。

no-diff / superseded の判断自体（`completed` か `not_planned` か、supersedes 対象は
どれか）は既存運用（#107/#36 の実例）どおり root（main agent/orchestrator）が行う。
本セクションは新しい自動判定ブランチを追加しない — root が既に確定した終了判断を、
PR を経由せず `finalize_no_diff_issue.py` へ接続するための呼び出し規約のみを定義する。
`implement-issue`（Step 1 worker）の `IMPLEMENT_RESULT_V1` はこの呼び出し規約による
変更を受けない（no-diff 検出フィールドを追加しない）。

root は、対象 Issue が実装差分不要（no-diff）または他 Issue に置き換えられた
（superseded）と判断した場合、空の PR や架空の PR 情報を作らず、次の呼び出しで
`finalize_no_diff_issue.py` を直接実行する:

```bash
uv run --locked python3 \
  .claude/skills/impl-review-loop/scripts/finalize_no_diff_issue.py \
  --issue-number <issue_number> \
  --repo squne121/loop-protocol \
  --reason completed|not_planned \
  --ac-results-file <ac_results.json> \
  --evidence-body-file <evidence.md> \
  [--supersedes <other_issue_number> ...] \
  [--run-id <stable-run-id>]
```

`finalize_no_diff_issue.py` はこの呼び出し規約に従い、渡された終了判断（close 理由・
AC ごとの検証結果・証跡本文・supersedes 対象）の意味を再評価せず、証跡コメントの
冪等 upsert（`issue_comment.publish` 経由）・対象 Issue の close・supersedes 対象への
not_planned close＋コメント・close 後の read-back 検証を実行し、
`ISSUE_FINALIZE_RESULT_V1`（`applied | no_op | failed_no_mutation |
failed_after_mutation | result_unknown` の対象ごとの状態を含む）を返す（詳細スキーマは
`finalize_no_diff_issue.py` 本体のモジュール docstring 参照）。途中失敗しても自動
rollback は行わず、再実行時は GitHub の現在状態を読み直して未完了の操作だけを補完する。
このルートは通常の PR-based termination（`termination_reason: approved` 等）とは独立
しており、PR review や live mergeability 評価を経由しない。

## Publish Failure Safety Lane（publish 失敗時の安全レーン）

implementation-worker / open-pr が branch publish 境界で停止した場合、CI 結果や手動 remote 更新の事後成功だけで安全扱いしてはならない。
以下の順序で readback し、`PUBLISH_LANE_DECISION_V1` または `PUBLISH_SAFETY_STOP_REPORT_V1` を残す。
これも独立した Git push safety 境界であり、semantic review verdict とは無関係に維持する。

1. live readback: remote branch head と local worktree HEAD を読み取る。`ls_remote` / `github_branch_api` / `fetch_then_show_ref` の source を記録する。
2. expected/current/local head comparison: `expected_remote_head`、`current_remote_head`、`local_head`、`verified_head`、`declared_publish_head` を比較する。
3. allowed publish lane decision: `remote == origin`、`active_branch == target_branch`、`expected_remote_head == current_remote_head`、`local_head == declared_publish_head`、`local_head == verified_head`、`allowed_paths_gate_status == ok`、`decision_inputs_complete == true` が全て真の場合だけ `allow_retry` とする。
4. post-publish readback: retry が許された場合も、実行後に remote branch head が `local_head` と一致することを読み戻す。
5. safety stop report: いずれかの比較が崩れた場合は force update / reset を実行せず停止する。

```yaml
PUBLISH_LANE_DECISION_V1:
  status: allow_retry | safety_stop
  publish_failure_reason:
    boundary_layer: worktree_scope_guard_denied | git_remote_rejected | codex_permission_request_no_decision
    reason_code: remote_write_requires_approval | hook_policy_denied | branch_mismatch | stale_remote_head | local_head_mismatch | remote_fast_forward_by_same_scope | remote_head_scope_contamination | non_fast_forward_remote_rewrite | allowed_paths_gate_not_ok | publish_guard_context_missing | publish_guard_context_invalid
  expected_remote_head: "<sha>"
  current_remote_head: "<sha>"
  local_head: "<sha>"
  verified_head: "<sha>"
  declared_publish_head: "<sha>"
  allowed_paths_gate_status: ok | fail_closed | indeterminate
  remote_readback_source: ls_remote | github_branch_api | fetch_then_show_ref
  decision_inputs_complete: true | false
  allowed_command: null | "<bounded publish command>"
  postcondition: "remote branch head == local_head"
  required_human_decision: []
```

```yaml
PUBLISH_SAFETY_STOP_REPORT_V1:
  status: safety_stop
  redacted_command: "<command>"
  boundary_layer: "<layer>"
  reason_code: "<reason>"
  expected_remote_head: "<sha>"
  current_remote_head: "<sha>"
  local_head: "<sha>"
  verified_head: "<sha>"
  declared_publish_head: "<sha>"
  allowed_paths_gate_status: ok | fail_closed | indeterminate
  remote_readback_source: ls_remote | github_branch_api | fetch_then_show_ref
  decision_inputs_complete: true | false
  required_decision:
    - "PR branch を linked issue 専用 head へ戻す"
    - "混入 commit を別 PR / 別 branch へ退避する"
```

## ISSUE_SCOPE_ROLLUP_DECISION_V2 の常時記録（advisory）

`ISSUE_SCOPE_ROLLUP_DECISION_V2` は、統合を実施した場合・しなかった場合を問わず、
**ループの全終了経路（approved / max_iterations / human_escalation）で記録を試みる**advisory な
audit trail である。この記録の有無や内容は `termination_reason` の決定に影響しない
（semantic termination authority ではない）。

終了報告コメントに以下を含める:

```yaml
ISSUE_SCOPE_ROLLUP_DECISION_V2:
  schema_version: 2
  recorded_at: "<ISO8601>"
  rollup_plan_ref:
    body_sha256: "<preparation Step 2.5 で生成した plan の body_sha256>"
    generated_at: "<plan の generated_at>"
  decision: executed | skipped | deferred | human_review_required
  executed_actions: []           # 統合を実施した場合のみ設定
  skipped_reason: null           # decision: skipped の場合の理由（例: "no high-confidence candidates"）
  candidates_reviewed:
    - kind: "issue|pr"
      number: <int>
      confidence: "high|medium|low"
      suggested_action: "<action>"
      final_decision: "accepted|rejected|deferred|human_review_required"
      rejection_reason: null
```

**記録の原則**:
- preparation Step 2.5 で scope rollup preflight を実行しなかった場合でも `decision: skipped` として記録を試みる。
- `candidates_reviewed` は空配列（`[]`）でも記録する（候補なしの場合）。
- この advisory 記録が欠落・invalid であっても、`termination_reason` の決定（approved / max_iterations /
  human_escalation）をブロックしない。

## Output（出力）

各終了条件に応じた状態を、main thread の一時情報として会話履歴に記録する。永続 artifact としての
replay を必須にしない（resume/compaction 後は live Issue/PR/diff/CI を再取得してから再実行する）:

```yaml
LOOP_STATE:
  target_pr: <PR番号>
  current_head: <現在の head SHA>
  iteration: <最終 iteration 数>
  max_iterations: <上限>
  last_step: judgment
  termination_reason: approved | max_iterations | human_escalation | already_satisfied
  last_route: approved | continue_loop | already_satisfied | route_to_update_branch | route_scope_clean_reconciliation | route_stale_head_rereview | route_human_escalation | conflict_hard_stop | fail_closed | null
  unresolved_blockers: []
  scope_rollup_decision: <ISSUE_SCOPE_ROLLUP_DECISION_V2、advisory>
```

その後、orchestrator は次のユーザー入力を待つ（自動で次イテレーションに進む決定済みなら Step 1 を再呼び出し）。
