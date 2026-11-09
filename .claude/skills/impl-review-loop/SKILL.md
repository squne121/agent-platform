---
name: impl-review-loop
description: >-
  implementation child issue を **実装→検証→PR レビュー** の 3 ステップループで自律完了させるオーケストレーター。
  Issue 番号を受け取り、pr-reviewer の LOOP_VERDICT が APPROVE になるまで反復する。
  移管元では `/impl-review-loop <N>` または「Issue ◯◯ をループで実装して」で使う想定だったが、
  I-4-4D4 の暫定移管では未移管依存が残るため Claude による自動起動を禁止している（下記の移管状況を参照）。
  着手前に `AGENTS.md` と `docs/dev/github-ops.md` の運用境界を確認する。
disable-model-invocation: true
---

# Impl Review Loop

> **移管状況 (I-4-4D4)**: 本書は移管元 `squne121/loop-protocol` tag `pre-split-agent-platform-20261020` (commit `6fa07b6a0ab932a62f08db6a8d521c0dfded2075`) の同名 Skill を最小差分で移管したもの。差分は `docs/dev/i4-4d4-skill-agents-readonly-manifest.json` の `intentional_diff` に記録している。
>
> **一時的な自動起動禁止（fail-closed）**: frontmatter の `disable-model-invocation: true` は Claude による自動起動を抑止するが、ユーザーによる `/impl-review-loop` の手動 invoke は妨げない（Claude Code 公式 Skill 仕様）。そのため手動で invoke された場合も、本書の Step 1 / Step 2 / Step 4 / Step 5 の通常 dispatch を行ってはならない。`implement-issue` / `open-pr` / `pr-review-judge` Skill、`pr-reviewer-lite`、`body_only_repair_plan.py` 等の補助 script、`references/` 配下の文書等の未移管参照、Final Gate・`LOOP_HANDOFF_RESULT_V1` の本番接続は未移管であり、後続の I-4-4D スライスの担当である。未移管の対象を起動できるものとして扱わず、必要になった時点で停止して人間へ報告する。本書の `docs/dev/workflow.md` 等の旧 repo 固有の参照は履歴であり、現行の正本は `AGENTS.md` / `docs/dev/github-ops.md`（uv / pytest / ruff）である。この制限の解除は、後続スライスで完全な結線と動作検証を終えた後にのみ検討する（恒久的な gate ではない）。

## body-only lane dry-run（引数 `body-only-lane-dry-run <fixture>` を受けた場合の限定的な起動手順、Issue #2971）

最初の引数が `body-only-lane-dry-run` の場合は、下記の手順 **だけ** を実行する。
事前準備（preparation）・worktree 作成・Step 1〜5 の loop・GitHub 操作は一切実行しない。
dry-run は実 PR を変更せず、`gh` / `update_pr.py` / `step4-adjudicate` / `step5-terminal-gate` を実行しない。

1. 第 2 引数の `<fixture>`（repo 相対 path の JSON file）を取得する。
2. 次の command をそのまま 1 回だけ実行する。

```bash
uv run --locked python3 .claude/skills/impl-review-loop/scripts/body_only_repair_plan.py --dry-run-fixture <fixture>
```

3. その stdout を一切の加筆・要約・並べ替え・追加なしに verbatim で最終回答として報告する。出力の各行は
   `decide_body_only_repair` の実出力から導出された判定結果であり、自作・補完してはならない。
   command が失敗した場合は stdout を捏造せず、失敗した事実だけを報告する。

## Advisory artifact policy（助言的 artifact の方針、Issue #1830）

scope-rollup、overlap、contract snapshot、body SHA、launch ledger、
session manifest、publish context、controlled-executor receipt は観測情報であり、
存在・freshness・identity・digest の欠落や不正を routing の停止条件にしない。
`SUBAGENT_LAUNCH_LEDGER_V1` は warning のみで、PASS、承認、merge readiness の
証拠として使用禁止とする。routing は live Issue、linked worktree の
cwd/branch/HEAD/dirty state、Allowed Paths、実テスト、CI、PR review に基づける。

implementation child issue を **実装 → 検証 → PR レビュー** の 3 ステップループで自律完了させるオーケストレーター skill。各ステップを SubAgent に委譲し、メインの control-plane（state tracking + routing）に責務を限定する。

## Root-Owned Synchronous Entry Transition（Step 1 起動契約, #2272 正本、root-direct 再設計）

Step 1（Implementation）の起動は、root/main thread が単一の継続した invocation
（in-process 呼び出しなら同一 call stack、CLI subprocess 経由なら同一 continuous
turn。詳細は `issue-refinement-loop/references/termination-policy.md` の
production carrier `delivery` 定義参照）の中で、
capability preflight → live Issue fetch → 同一呼び出し内での current-run
`issue-contract-review`（`.claude/skills/issue-refinement-loop/scripts/root_entry_router.py`
の `run_root_transition()` が既存 `run_once()` を関数として直接呼び出す。
`.claude/skills/issue-contract-review/**` 自体は変更しない）→ 直後の live 再取得
→ routing 決定 → 条件成立時のみ同じ呼び出しの中で Step 1 を直接起動する、という
一続きの手順を自ら実行した場合にのみ許可される。producer/consumer を跨いで
再提示可能な `invocation_token` は撤去済みで、`ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1`
は authorization packet ではなく非永続の process-local route result であり、
GitHub comment・artifact・digest・invocation ID のいずれも単独では Step 1 起動を
authorize しない（`issue-refinement-loop/references/termination-policy.md` の
「Root-Owned Synchronous Entry Transition」節が normative SSOT）。旧
`implementation_entry_decision`（authorization packet 型）および producer/consumer
subprocess 分離方式（`invocation_token` による再提示）は撤回済み。

## Inputs（入力）

- `issue_number`（必須）: implementation child issue 番号
- `contract_snapshot_url`（任意）: 参照用 telemetry。欠落時に materialize を要求せず routing を継続する。
- `max_iterations`（任意、デフォルト 3）: 上限回数。超過時は fail-close で人間判断を仰ぐ
- `human_context_comment_urls`（任意、repeatable, #1950 AC6）: root/main thread が「人間が投稿した自然言語コンテキスト」として明示的に渡す Issue comment URL のリスト。origin はコメント本文・投稿アカウント・`author_association`・構造化 marker の有無から推測せず、**この引数として渡されたこと自体だけ**を origin 判定根拠にする。
- `agent_report_comment_urls`（任意、repeatable, #1950 AC6）: root/main thread が「SubAgent が返した構造化 report」として明示的に渡す Issue comment URL のリスト。同様に本引数として渡されたことだけが origin 判定根拠であり、投稿アカウントが human_context 側と同一でも構わない（例: `create-issue transaction partial-failure` のような機械生成コメントと人間コメントが同一アカウントから混在する場合がある）。
- 同一 URL が `human_context_comment_urls` と `agent_report_comment_urls` の両方に渡された場合は provenance conflict として fail-closed にする（`build_intake_capsule.py` の `--human-context-comment-url` / `--agent-report-comment-url` 参照）。

## Loop Structure（ループ構造）

```
[Step 1: Implementation]  → implementation-worker SubAgent (implement-issue skill)
        ↓
[Step 2: Verification]    → test-runner SubAgent
        ↓ (current-head gate: VC_ADJUDICATION_RESULT_V1.blocking == false のときのみ通過。Issue #88)
[Step 4: PR Review]       → pr-reviewer SubAgent (pr-review-judge skill)
        ↓
[Step 5: Judgment]        → reviewer_verdict + live_mergeability を route_loop_verdict_v2() で解析
        ↓
    route: approved → 終了（PR は人間がマージ判断）
    route: route_to_update_branch → worker 委譲（update_branch）→ 検証・PR review 再実行
    route: route_stale_head_rereview → 現在 head で PR review 再実行
    route: body-only lane（REQUEST_CHANGES かつ decide_body_only_repair が eligible。iteration を消費せず continue_loop → Step 1 に優先。下記「body-only lane」節） → body-only repair → fresh pr-review → step5-terminal-gate
    route: continue_loop（REQUEST_CHANGES） → Step 1 に戻る（fix_delta を渡す）
    route: already_satisfied（REQUEST_CHANGES かつ base_ac_satisfied かつ meaningful_pr_delta なし、#2607） → 終了。recommendation を構造化して報告（自動 close は実行しない）
    route: route_human_escalation（verdict: HUMAN_REVIEW_REQUIRED） → 人間判断を仰ぐ
    route: conflict_hard_stop（actual conflict のみ） → CONFLICTING PR Escalation Runbook
    route: fail_closed（schema 不正 / UNKNOWN / BLOCKED / UNSTABLE / DRAFT 等） → warning 記録、次サイクルで再評価（自動 human escalation ではない）
    上限超過（body-only lane が eligible の場合を除く） → 人間判断を仰ぐ
```

Issue #1873 以降、pr-reviewer は `verdict` / `reviewed_head_sha` / `blockers` / `warnings` の最小 convention のみを返す。`merge_ready` / `required_auto_actions` / `mergeability` / `allowed_paths_gate` / `test_verdict` は reviewer の自己申告として受け取らない。mergeability は control-plane が `gh pr view` で直接取得し、`route_loop_verdict_v2()`（`.claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py`）の `live_mergeability` 引数として渡す。`update_branch` action は reviewer から受け取らず `route_loop_verdict_v2()` が合成する。Step 1-4 の SubAgent が返す `human_review_required: true`（真偽値の自己申告）自体には停止権限がない（#1860 Owner Decision）。詳細は `step-5-feedback-and-termination.md` の「human_review_required の扱い」を参照。

> Step 3（adversarial review）と Step 1.5（spec document review）は LOOP_PROTOCOL では採用しない（PR #12 / #20 方針）。Step 番号は履歴互換のため 1 → 2 → 4 → 5 のまま保持する。

## Procedure（手順）

各 Step の詳細は `steps/` 配下に分割。実行時は下記の順で読む:

1. [事前準備（state 初期化・worktree 確認）](steps/preparation.md)
2. [Step 1: Implementation](steps/step-1-implementation.md)
3. [Step 2: Verification](steps/step-2-verification.md)
4. [Step 4: PR Review](steps/step-4-pr-review.md)
5. [Step 5: 判定・終了・フィードバック循環](steps/step-5-feedback-and-termination.md)
6. [Step 5: LOOP_VERDICT ルーティング（mergeability handling）](steps/step-5-mergeability-handling.md)
7. [CONFLICTING PR Escalation Runbook](steps/conflicting-pr-escalation-runbook.md)
8. [Context Protocol / Guardrails](steps/context-protocol-and-guardrails.md)

## LOOP_STATE YAML（state tracking の正本）

ループ実行中は以下の構造で state を保持する。orchestrator がイテレーションごとに更新し、次のイテレーションへ持ち越す:

```yaml
LOOP_STATE:
  issue_number: <int>
  contract_snapshot_url: <URL>
  contract_snapshot_source: provided | detected_existing | materialized_by_issue_contract_review
  iteration: <int, 0-indexed>
  max_iterations: 3
  worktree: .claude/worktrees/issue-<番号>-<slug>
  branch: worktree-issue-<番号>-<slug>
  last_step: implementation | verification | pr_review | judgment
  last_loop_verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED | null
  blockers_history: []
  external_research_skip_basis: "<理由 or null>"
  termination_reason: null | approved | max_iterations | human_escalation | intake_gate_failed | already_satisfied
  product_spec_preflight:
    source: contract_snapshot.checks.product_spec_check
    applicability: applicable | not_applicable | missing
    decision: pass | fail | human_judgment | missing
    blocked_rule_ids: []
    contract_snapshot_url: "<url>"
    body_sha256: "<sha256>"
    routing_action: continue | stop_human | refresh_contract_snapshot
  contract_materialization:
    attempted: bool
    source: existing_go | materialized_go | latest_blocked | readiness_blocked | human_judgment | stale_conflict
    result_schema: CONTRACT_SNAPSHOT_ENSURE_RESULT_V1
    contract_snapshot_url: null
    artifact_path: artifacts/contract-snapshot/...
  vc_adjudication:
    # Issue #88 fix_delta Blocker 1/2: Step 2 が独立検証で得た
    # VC_ADJUDICATION_RESULT_V1 を Step 4 の current-head gate が再利用するための
    # 永続化フィールド（`Step4AdjudicationCache` という in-memory object の代替）。
    # key は `.claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py` の
    # `step4_binding_key()` が (head_sha, contract_body_sha256, ordered command_hashes)
    # から導出する文字列。Step 4 は `step4_gate_from_loop_state()`
    # （CLI: `adjudicate_vc_result.py step4-gate`）でこの mapping を live binding に
    # 再照合するだけで、test-runner を再起動しない。stale/invalid なら Step 2 へ
    # 戻り新規 binding key で再度書き込む。
    "<binding_key>": <VC_ADJUDICATION_RESULT_V1>
```

## 終了条件

| 条件（`route_loop_verdict_v2()` の `route`） | アクション |
|---|---|
| `approved`（`verdict: APPROVE` かつ live mergeability が `CLEAN`/`HAS_HOOKS` かつ `blockers == []`） | 終了。`IMPL_REVIEW_LOOP_RESULT_V1.status: draft_pr_ready` を emit。PR は人間がマージ判断 |
| `route_to_update_branch`（live `merge_state_status == BEHIND`） | 終了しない。合成された `update_branch` action を worker に委譲し、検証・PR review を再実行する |
| `route_stale_head_rereview`（`reviewed_head_sha` が現在の PR head と不一致） | 終了しない。現在 head で PR review を再実行する |
| body-only lane（`verdict: REQUEST_CHANGES` かつ `decide_body_only_repair` が eligible。`conflict_hard_stop` / `already_satisfied` / `route_to_update_branch` のいずれにも一致しなかった場合のみ評価） | 終了しない。iteration を消費せず、`iteration` が `max_iterations` 未満・到達のどちらでも `continue_loop → Step 1` に優先して適用する（詳細は下記「body-only lane」節） |
| `continue_loop`（`verdict: REQUEST_CHANGES`、actual conflict がない場合） | 終了しない。Step 1 に戻り blockers を fix_delta として渡す |
| `already_satisfied`（`verdict: REQUEST_CHANGES` かつ `already_satisfied_evidence.base_ac_satisfied == true` かつ `meaningful_pr_delta == false` かつ `evidence_base_sha` が current main HEAD と一致、#2607） | 終了。`termination_reason: already_satisfied` を LOOP_STATE に記録。recommendation を構造化して報告する（詳細は下記「Already-Satisfied Recommendation Structure」）。PR/Issue の close は本 route 自身では実行しない |
| `iteration ≥ max_iterations`（body-only lane が eligible の場合を除く。例外は下記「body-only lane」節のみ） | fail-close。`termination_reason: max_iterations` を LOOP_STATE に記録、人間判断を仰ぐ |
| `route_human_escalation`（`verdict: HUMAN_REVIEW_REQUIRED`、actual conflict がない場合） | 即停止、人間判断を仰ぐ |
| Step 1-4 のいずれかで `human_review_required: true`（真偽値の自己申告）を SubAgent が返した | #1860 Owner Decision により即停止しない。warning として記録し、iteration 余裕があれば継続する（`step-5-feedback-and-termination.md` の「human_review_required の扱い」参照）。ループを止める human veto は live Issue/PR コメント上の明示的な停止指示、または実 Git conflict／target PR mergeability に限定する |
| `conflict_hard_stop`（`mergeable == CONFLICTING` または `merge_state_status == DIRTY`。**verdict に関係なく最優先で評価**） | CONFLICTING PR Escalation Runbook 参照（`merge_state_status == CONFLICTING` は無効な enum 値であり schema 不正として扱う。`BLOCKED` は required checks/review 未充足であり Git conflict ではないため本 runbook の対象にしない） |
| `fail_closed`（schema 不正、`APPROVE` かつ `blockers` 非空、mergeability `UNKNOWN`、`BLOCKED`/`UNSTABLE`/`DRAFT`） | `reason_code` を warning として記録。`UNKNOWN` は bounded retry（最大 3 回）後も warning のまま継続。`BLOCKED`/`UNSTABLE`/`DRAFT` は current-head required-CI / branch-protection evaluator の判定に委ね、human escalation にはしない |

> **重要**: `verdict: APPROVE` 単独では終了しない。live mergeability が `CLEAN`/`HAS_HOOKS` かつ `blockers == []` の両条件が必要（`route_loop_verdict_v2()` が判定する）。

## body-only lane（`iteration ≥ max_iterations` の fail-close に対する例外、Issue #2971）

current PR HEAD の verification / runtime evidence / required CI が完了済みで、pr-reviewer の blockers が **PR 本文に既にある evidence の同期だけ**（`## Runtime Verification Evidence` section 欠落・stale な件数・pending 文言）に限られる場合に限り、`iteration ≥ max_iterations` でも通常の implementation iteration を消費せず body-only repair を 1 回だけ行える。判断は impl-review-loop の scripts 配下の `body_only_repair_plan` module の `decide_body_only_repair`（pure な単一 authority。新規 route / schema / registry / lock は作らない）だけが行い、control-plane が blocker 文面を自由に解釈して eligible と見なしてはならない。 実行は production CLI（`body_only_repair_plan.py` の `plan` / `record` / `guard` / `ci-freshness`）と既存 `IMPLEMENTATION_WORKER_REQUEST_V2`（`update_pr_body_hygiene` mode）だけで行い、dry-run 専用経路とは別である。

### 適用条件（bounded 規則）

- `REQUEST_CHANGES` で `decide_body_only_repair` が eligible の場合のみ、`iteration` が `max_iterations` 未満・到達のどちらでも `continue_loop → Step 1` に優先して適用する。`iteration ≥ max_iterations` であること自体は eligibility を妨げない。
- 終了条件表の `conflict_hard_stop`（`mergeable == CONFLICTING` または `merge_state_status == DIRTY`、verdict に関係なく最優先）・`already_satisfied`・`route_to_update_branch`（`merge_state_status == BEHIND`）のいずれにも一致しなかった場合にのみ評価する。mergeability が `CONFLICTING` / `DIRTY` / `BEHIND` の場合は lane は適用不能で既存 routing に従う。mergeability の gate は control-plane が呼出し前に行い、`decide_body_only_repair` に mergeability の引数は追加しない。
- eligible な blocker は blocker 全文が closed grammar に完全一致する 3 kind（`runtime_evidence_section_missing` / `stale_count` / `pending_wording`）だけで、未知の substantive 句が残る blocker・code / test / Issue contract / branch の変更を要する blocker が 1 件でもあれば ineligible となり、従来どおり iteration を消費する（`max_iterations` 到達時は fail-close）。
- lane の消費は、`record` が `--loop-state-file`（`step4-adjudicate` / `step5-terminal-gate` と同じ file）の既存 `LOOP_STATE.blockers_history[]` へ書く二段階 entry で数える。entry の field は closed set `{lane, outcome}` だけで、worker 起動の **直前**に `{lane: body_only_repair, outcome: dispatched}` を 1 件追記し、worker が guard で拒否して mutation を行わなかった（`status: blocked` かつ `wrapper_used: false`）場合だけ、その entry を `outcome: no_mutation` へ更新する。次回の `prior_body_only_repairs` は `outcome != no_mutation` の entry 件数（crash / resume で `dispatched` のまま残った entry、`update_pr.py` に到達した成否不問の mutation は消費済み）で、`prior_body_only_repairs >= 1` は ineligible となるため、mutation を伴う lane は同一 PR で最大 1 回である。`LOOP_STATE` のキー集合は変更せず、新規 ledger / lock も作らない。JSON の手編集はしない（`record` は `blockers_history[]` だけを触る canonical writer）。
- guard 拒否（`no_mutation`）後の再評価・再 dispatch は、`outcome: no_mutation` の entry が **ちょうど 1 件**の間だけ 1 回許される。`no_mutation` が 2 件になった時点で lane は終了し通常 routing（`continue_loop` / `max_iterations` fail-close）に戻る。件数は同じ `--loop-state-file` から数えるため、compaction / resume 後も再評価上限はリセットされない。
- `plan` が `stale_body_rebuild_required` / `ineligible_head_changed` 相当（`expected_head_sha_mismatch` / `live_body_hash_mismatch`）で ineligible を返した場合は mutation を行わず（`no_mutation` としてカウントしない）、fresh な live 状態で eligibility を最大 1 回だけ再評価する。再び ineligible または stale なら通常 routing へ戻る。

### 実行順序（canonical fresh review path）

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
9. 保存済み `dispatch_seq` と同一 binding を `step5-terminal-gate` へ渡す。`APPROVE` かつ `blockers == []` かつ live mergeability が適格な場合のみ `approved`。fresh reviewer が code / test / contract change を要求した場合は通常 routing（iteration 消費・`max_iterations` fail-close）へ戻る。

### guard の所在と禁止事項

- worker は body file field を伴う `update_pr_body_hygiene` で `expected_head_sha` を強制する。mutation 直前に `guard` を実行し、live head が `expected_head_sha` と異なる、live body の canonical hash が `expected_live_body_sha256` と異なる、または body file の canonical hash が `body_file_sha256` と異なる場合は overwrite せず `status: blocked`（`wrapper_used: false`）を返す。control-plane は `plan` 時点の `check_body_freshness` 相当の判定と、書込み後の `verify_body_readback` の判定（readback）で HEAD / body を再確認する。`update_pr.py` 自体に head / body の freshness 検査や readback は無い。
- **best-effort の限界**: GitHub に PR body の compare-and-swap は無く、`guard` は best-effort の optimistic guard である。`guard` 通過後から `update_pr.py` の全置換までの race window は残り、その間に他 actor が行った編集は失われ得る（residual risk）。lost update の絶対防止は主張せず、lock / approval layer / persistent coordination は追加しない。
- **artifact の来歴は best-effort**: `plan` が evidence ref に使う `summary.md` / pytest 出力は head field を持たないため、worktree HEAD が live head と一致し artifact の mtime が当該 HEAD の commit 時刻以降であることを pre-filter にしている。これは artifact の head 束縛に関する best-effort の来歴確認であり、artifact の真正性は証明しない（residual risk）。`TEST_VERDICT` / CI 出力は明示の head field で束縛する。
- 次を禁止する: terminal gate bypass（`step5-terminal-gate` exit 0 以外での `approved` 確定）、古い reviewer result の carry-forward（lane 前の reviewer 結果の流用）、reviewer の直接呼出しのみでの fresh review 成立扱い（`step4-adjudicate --reuse-stored` による `dispatch_seq` の +1 と保存、`step5-terminal-gate` への受け渡しを省略する経路）。
- `adjudicate_vc_result.py` / `route_loop_verdict_v2.py` / `step5-terminal-gate` に max-iteration の例外は入れない。terminal approval は引き続き `step5-terminal-gate` だけが authority である。

## Evidence-Based Landing Disposition（実装済み scope の pre-Step-1 duplicate-dispatch 判定、Issue #2699）

`preparation.md` の「0-a-0. Evidence-Based Landing Disposition」は、worker dispatch・worktree 作成・new PR 作成のいずれよりも前に一度だけ評価する pre-Step-1 choke point であり、`implementation_landed_evidence.py` の strict producer（`collect_candidate_inputs()` → `resolve_landing_disposition_with_freshness_rebind()` → `derive_landing_disposition()`）が返す disposition を、呼び出し元が自己解釈せずそのまま消費する。

| disposition | data-plane action |
|---|---|
| `implementation_already_landed` | worker / worktree / new PR を開始せず、no-op disposition を記録する |
| `existing_pr_resume` | linked open/draft PR を resume し、duplicate PR を作成しない |
| `already_satisfied` | 下記「Disposition Precedence」の合成結果。`already_satisfied` route（既存 #2607、`already_satisfied_decision` を含む）と同じ扱いで、worker / worktree / new PR を開始せず recommendation を構造化して報告する（上記「Already-Satisfied Recommendation Structure」参照） |
| `ordinary_dispatch_or_explicit_recovery` | 通常の Step 1 へ進む |
| `reconciliation_required` | worker / worktree / new PR を開始せず、fresh evidence の reconciliation を要求する |

`contradictory`（materialization failure を含む）、insufficient、stale、identity mismatch、malformed durable marker は lifecycle（open/draft/merged）に関わらず常に最優先で `reconciliation_required` にする。`implementation_already_landed` は、merged candidate、verified main ancestry、durable marker による exact current-scope coverage がすべて成立したときに限り、`IMPLEMENTATION_SCOPE_COVERAGE_V1` marker を持たない legacy merged candidate（例: #2119 に対する #2137）は現在の body から merge-time scope を推測せず `implementation_already_landed` を導出しない。open / draft candidate は、marker 付きなら exact coverage、markerless なら現在の `## Allowed Paths` 全エントリのカバレッジ検証の両方が揃った場合に限り `existing_pr_resume` となる。

**Bounded candidate exclusions（要約。詳細な成立条件は `steps/preparation.md` の「Bounded candidate exclusions」節が正本）**: `derive_landing_disposition()` は conflict counting / authority selection 用の local set に限り、別分類の 2 種の除外を適用する。(a) Issue #2750 の irrelevant sibling cross-reference（`scope_coverage_issue_identity_mismatch` 1 件のみの candidate）と、(b) Issue #2893 の historical merged `later_scope_expansion`（除外後に fresh な current exact open/draft candidate がちょうど 1 件残る場合のみ）であり、(b) は (a) の identity mismatch の許容ではない。いずれも closing candidate が存在しない場合に限られ、適用順は closing candidate なし → (a) → (b) である。main ancestry の verified / reachable は (b) 固有の必須条件であり (a) には適用しない（(a) は candidate-local marker / provenance の限定条件のみ）。`evidence["candidates"]` は保持され、fail-closed の既存 semantics（複数の current exact open/draft は `qualified_candidate_conflict`、malformed / stale / markerless legacy は非緩和）は変わらない。長い条件式はここへ複製せず `preparation.md` を参照する。

**closing precedence**: `closing_relation` candidate が1件でも存在する場合は bounded candidate exclusions を一切適用せず、structured closing authority を優先する。

**decision-time semantic refresh**: live PR body / `closingIssuesReferences` から scope coverage / closing authority の意味を再導出するのは、収集時点で (a) と認定済みの irrelevant sibling candidate と、(b) が成立し得る状況での carve-out 参加 candidate に限る。それ以外の candidate は head / merge OID の identity 再取得に留まり、全 candidate への一般的な PR body semantic refresh は保証しない（正確な射程は `steps/preparation.md` を参照）。PR body 全体の byte equality は gate にしない（prose や verification-result のみの更新で不要停止しない）が、Issue body sha256 / candidate head-or-merge-oid / current main sha に対する既存の bounded freshness rebind は維持する。新しい API call や freshness classifier は導入せず、producer が既に取得する live 値の既存経路を説明するものである。

呼び出し元は producer が返す canonical な `start_data_plane` boolean と `action` を消費し、prose から判定を再導出しない。`existing_pr_resume` は `start_data_plane: false` / `resume_existing_pr` であり、duplicate worker / worktree / new PR を抑止する正常系（failure ではない）である。

**Disposition Precedence（新しい enum を追加しない #2607 との合成）**: `derive_landing_disposition()` が landing authority を確立できなかった場合（`ordinary_dispatch_or_explicit_recovery` かつ `reason_codes` に `no_qualified_candidate` または `closed_unmerged_candidate` を含む）に限り、`implementation_landed_evidence.py::apply_already_satisfied_precedence()` が既存 `route_loop_verdict_v2.py::resolve_already_satisfied_early_exit_decision()` をそのまま呼び出して再評価し、`early_exit: true` なら結果を `already_satisfied` に差し替える。`reconciliation_required` / `implementation_already_landed` / `existing_pr_resume`、および他の理由による `ordinary_dispatch_or_explicit_recovery` はこの合成の対象外であり無条件で優先される（新しい判定ロジックの再実装ではなく、既存関数の再利用）。

**AC9 Freshness / Decision-Time Rebind**: `resolve_landing_disposition_with_freshness_rebind()` は、disposition 確定直前に issue body sha256 / candidate head-or-merge-oid / current main sha を live 再取得し、collection 開始時の値と不一致なら bounded に 1 回だけ discovery + evaluation をやり直す。再試行後も不一致なら `reconciliation_required`（`reason_codes: ["freshness_rebind_failed"]`）にする。単純な `observed_at` TTL のみを authority にしない。

## Already-Satisfied Recommendation Structure（要求が既に充足済みの場合の推奨構造。`already_satisfied` の recommendation 構造、Issue #2607 AC8）

`already_satisfied` は 2 つの経路（`preparation.md` の early-exit、`route_loop_verdict_v2()` の Step 5 recovery route）のいずれから到達しても、以下と同じ `result` / `recommendation` 構造で報告する。`route_loop_verdict_v2()` 側は `RouteDecision.selected_action` の `result` / `recommendation` キーとしてこの構造をそのまま返す（新規 top-level schema は新設しない）。

```yaml
result:
  status: no_change_required
  termination_reason: already_satisfied
  merge_ready: false
recommendation:
  pr:
    action: none | close   # none: PR 未作成の early-exit経路 / close: PR 既存の Step 5 recovery 経路
    reason: no_pr_created | no_meaningful_delta
  issue:
    action: close
    state_reason: completed
    reason: requirement_already_delivered
```

- `already_satisfied` route 自身は PR/Issue の close 等の mutation を直接実行しない（recommendation の構造化のみ。`route_loop_verdict_v2.py` の module docstring が維持する "no gh, git, network, or subprocess calls" 不変条件は本 route でも変更しない）。
- `meaningful_pr_delta` の判定（`recommendation.pr.action: close` の根拠）は **対象 Issue 自身の AC/VC 範囲（`runtime_ac_results` が被覆する範囲）に限定した比較**であり、PR diff 全体の監査ではない。`recommendation.pr.action: close` は常にこの限定つきの判定であることを consumer 向けに明記する。`#2041` 等の将来の consumer は、本 recommendation だけを根拠に無条件で PR close を実行してはならず、独立した diff review を経由すべきである（対象 Issue の AC/VC 範囲外で PR が独立した価値を持つ場合、この比較だけでは no-op PR と区別できないため）。

## 外部仕様調査の取扱い

外部仕様調査が必要な場合は `gemini-cli-headless-delegation` skill を default 経路として使い、結果を LOOP_STATE の `external_research_skip_basis` に記録する。LOOP_PROTOCOL は internal-only 変更が多い前提のため、デフォルトはスキップで構わない（スキップ時も判定根拠を記録する）。

## Allowed Paths Gate Routing（許可パスゲートのルーティング、Issue #1873）

pr-reviewer が実行する `ALLOWED_PATHS_GATE_RESULT_V1`（決定論的スクリプト、正本は pr-review-judge 配下）の
`status` は専用フィールドとして `reviewer_verdict` に自己申告されず（`LOOP_VERDICT_V2` は #1873/#1875 で完全撤去済みであり本節が参照する対象は存在しない）、
`status != ok` の場合は具体的な違反内容が `reviewer_verdict.blockers[]` にテキストとして含まれる
（`references/allowed-paths-gate.md` 参照）。gate の pattern source は常に **live linked Issue 本文**
であり、contract snapshot / `expected_contract_fingerprint` は advisory telemetry に過ぎない
（欠落・不一致のみを理由に block しない。`allowed-paths-gate.md` 参照）。gate 自体（path が
Allowed Paths 外か、rename/copy provenance が確定できるか）は hard safety boundary のまま維持する。

gate `status` の値は `ok` / `fail_closed` / `indeterminate` のみ（`stale_snapshot` は `status` を
占有しない -- contract fingerprint drift は `warnings[]` の advisory annotation としてのみ表れ、
live 本文で評価した `status` が canonical のまま変わらない。詳細は `references/allowed-paths-gate.md`
参照）。

| gate `status` | reviewer_verdict への反映 | 結果としての `verdict` | routing |
|---|---|---|---|
| `ok` | blocker を追加しない（fingerprint drift があれば `warnings[]` に advisory 記載） | `APPROVE` 可 | `route_loop_verdict_v2()` の通常判定へ |
| `fail_closed` | 違反内容を `blockers[]` に記載 | `REQUEST_CHANGES` | `continue_loop`。next iteration で修正 |
| `indeterminate` | 理由（path が Allowed Paths 外と確定できない等）を `blockers[]` に記載 | `REQUEST_CHANGES` または `HUMAN_REVIEW_REQUIRED` | `continue_loop` または `route_human_escalation` |
| malformed（スクリプト実行不能） | 実行不能である旨を `blockers[]` に記載 | `HUMAN_REVIEW_REQUIRED` | `route_human_escalation` |

`verdict: APPROVE` かつ `blockers` が非空は inconsistent な reviewer 結果として `route_loop_verdict_v2()` が
`fail_closed`（`approve_with_blockers_inconsistent`）を返す。つまり Allowed Paths 違反が確定した状態のまま
`APPROVE` を出す reviewer 結果は production 経路で自動的に拒否される。

## Contract Snapshot 参照ルール

preparation step で取得した contract snapshot 内の以下の情報を Step 1-4 で参照する:

### VC Preflight Reference（検証コマンド事前確認の参照）

`vc_preflight` JSON（`baseline_vc_preflight.py` が生成）を参照し、impl-review-loop 側で `baseline_vc_preflight.py` を重複実行しない。VC 分類の正本は contract snapshot の `vc_preflight.classifications[]` に従う。

### Product Spec Check Reference（プロダクト仕様確認の参照, Issue #333）

`checks.product_spec_check` を contract snapshot から読み取り、Step 1 delegation 前に `LOOP_STATE.product_spec_preflight` に正規化して格納する。以下のルールに従う（#1869 fix_delta P0-4: `stop_human` / `refresh_contract_snapshot` は advisory warning へ改訂。product-spec snapshot は semantic planning artifact であり、それ自体には停止権限がない）:

> **注意**: `refresh_contract_snapshot` は **route only; no auto-run** の warning である（AI が `issue-contract-review` を自動実行することはない）。人間へ「再実行を推奨する」旨を記録するに留め、Step 1 continuation は妨げない。

- `checks.product_spec_check` が snapshot に存在しない場合は stale / incomplete snapshot として warning を記録し（`routing_action: refresh_contract_snapshot`）、Step 1 へ継続する
- `applicability == not_applicable && decision == pass` の場合、無関係 Issue として `continue` へ継続
- `applicability == not_applicable && decision != pass` は inconsistent snapshot として warning を記録し（`routing_action: refresh_contract_snapshot`）、Step 1 へ継続する
- `decision == fail` → warning として記録し（`routing_action: stop_human` は advisory 表示のみ）、Step 1 へ継続する。live Issue/PR コメント上の明示的な人間の停止指示がある場合のみ実際に停止する
- `decision == human_judgment` → 同上（warning として記録し継続。明示的な人間の停止指示がある場合のみ停止）
- `decision == pass` かつ `applicability == applicable` → 続行、`routing_action: continue`
- 不正な enum 値 → stale / invalid snapshot として warning を記録し（`routing_action: refresh_contract_snapshot`）、Step 1 へ継続する

**実装例**: `.claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py` が mutation-free CLI として `PRODUCT_SPEC_GATE_DECISION_V1` を出力する（routing_action: continue | stop_human | refresh_contract_snapshot。いずれも advisory であり `continue` 以外も Step 1 continuation を妨げない）。

## Guardrails（安全策）

- loop policy（何回まで自動で回すか）と Claude Code permission mode（ツール呼び出しの承認方式）は直交する概念であり、loop policy の継続判断に `--permission-mode` / `permissions.defaultMode` / `--dangerously-skip-permissions` を参照しない
- control-plane だけを担い、data-plane 操作（push / `gh pr edit` / マージ等）は SubAgent に委譲する
- LOOP_STATE をイテレーションごとに更新し、人間がループの全履歴を読めるようにする
- `max_iterations` 超過時は必ず fail-close（無限ループ防止）。唯一の例外は「body-only lane」節の `decide_body_only_repair` が eligible の場合で、同一 PR で最大 1 回に bounded される
- adversarial review は採用しないため `LOOP_VERDICT` 判定は pr-review-judge の APPROVE 一本で完結
- 全 SubAgent 出力は構造化フォーマット（YAML / KEY=VALUE）で受け取り、散文サマリで上書きしない
- **contract snapshot advisory routing**（#1851）: `contract_snapshot.normalized_status` が `go` / `missing_go` / `stale` / `runtime_error` のいずれかであれば、本節冒頭の advisory artifact policy に従い `next_action.route` は無条件で `proceed_to_step_1` を返し、live Issue の Allowed Paths と実テスト・CI・PR review に基づいて routing を継続する。`missing_go` / `stale` を検出した場合は参考情報として `ensure_contract_snapshot.py` による再 materialize を試みてよいが、その成否や `status: human_judgment` / `blocked_needs_refinement` / `stale_or_conflicting_snapshot` は routing の停止条件にしない。`latest_blocked`（trusted author による明示 blocked/request_changes）のみ人間判断（`run_contract_blocker_triage`）へ route する human veto 境界として維持する。

## Related（関連ファイル）

- `.claude/skills/implement-issue/SKILL.md` — Step 1 で使う実装手順
- `.claude/skills/pr-review-judge/SKILL.md` — Step 4 で使うレビュー判定手順
- `.claude/skills/open-pr/SKILL.md` — Step 1 内で PR 起票に使う
- `.claude/skills/issue-refinement-loop/SKILL.md` — Issue 本文改善のループ（本 skill とは別）
- `.claude/agents/implementation-worker.md` / `test-runner.md` / `pr-reviewer.md` — Step 1-4 で委譲する SubAgent
- `docs/dev/agent-skill-boundaries.md` — オーケストレーター設計原則（control-plane / LOOP_STATE / 人間承認原則）
- `docs/dev/github-ops.md` — GitHub 運用ルール（body-file guard / コメントテンプレ）
- `docs/dev/agent-run-report.md` — run report finalize / posting handoff 規約
- `docs/dev/agent-retro-index.md` — retro index 更新規約
- `docs/dev/workflows/impl-review-loop-design.md` — 設計判断・failure mode の詳細（`derived_design_note`。本 entrypoint と矛盾する場合は本 entrypoint が正本、#1876）

## Loop Policy 参照

impl-review-loop は `.claude/skills/issue-refinement-loop/references/termination-policy.md` の `LOOP_POLICY_V1` と同一の routing policy を採用する。`max_iterations` 既定値 3、loop iteration approval gate は repo_loop_iteration_only スコープ、Claude Code permission mode は変更しない。

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
