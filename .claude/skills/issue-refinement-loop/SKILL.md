---
name: issue-refinement-loop
description: >-
  Issue 本文の品質を調査・レビュー・改善ライトで反復改善するオーケストレーター。
  `plan_refinement_loop.py` が生成する `REFINEMENT_LOOP_PLAN_V1` を consume し、
  判定ロジックの再実装は行わない。「Issue ◯◯ を改善して」「refinement loop」で使う。
---

# Issue Refinement Loop

<!-- ISSUE_REFINEMENT_LOOP_THIN_ENTRYPOINT_V1
planner_ssot: REFINEMENT_LOOP_PLAN_V1
max_skill_lines: 500
no_prose_rejudgment: true
subagent_contract_mode: link_only
note_ja: 本ファイルは thin entrypoint 契約のメタデータであり、判定ロジックの再実装は行わない
-->

`issue-refinement-loop` は control-plane 専用の thin entrypoint である。詳細 procedure は `references/` を必要時だけ読む progressive disclosure とし、planner / reviewer / worker の判定ロジックをこのファイルへ再実装しない。

## 入力 (Inputs)
- `issue_number`（必須）: 改善対象の Issue 番号
- `max_iterations`（任意、既定 3）: review cycle の上限
- `anchor_comment_url`（任意）: snapshot 固定して扱う対象コメント URL。URL 単独から origin は推定せず、canonical command profile が human context / agent report / unlabeled を明示する。

## ループ方針 (Loop Policy)
```yaml
loop_policy:
  default_max_iterations: 3
  loop_iteration_approval_gate:
    default_required: false
    scope: repo_loop_iteration_only
    does_not_control:
      - Claude Code permissions.defaultMode
      - bypassPermissions
      - --dangerously-skip-permissions
      - --allow-dangerously-skip-permissions
      - --permission-mode
      - hooks PermissionRequest auto-approval
```

### loop_iteration_approval_gate
`loop_iteration_approval_gate.default_required: false`

ループの自動継続は「このリポジトリの loop policy 上の承認確認（過去に `--no-approval` と呼んでいた運用フラグ/指示）」であり、Claude Code の `--permission-mode`、`--dangerously-skip-permissions`、`permissions.defaultMode` は変更しない。loop policy は「何回まで自動で回すか」を制御し、Claude Code の permission mode は「ツール呼び出しの承認方式」を制御する。両者は直交する概念であり、loop policy の継続判断に permission mode を参照しない。

needs-fix を受け取ったとき:
- `iteration + 1 < max_iterations` → 自動継続（条件なし）
- `iteration + 1 >= max_iterations` → `human_escalation` で停止し、全 iteration 分の blocker summary を添付

## ループ構造 (Loop Structure)
```text
[Step 0: Preconditions / planner input assembly]
        ↓
[Step 0f: plan_refinement_loop.py]  → REFINEMENT_LOOP_PLAN_V1
        ↓
[Step 1: Investigation]      → codebase-investigator
[Step 1b: Web research]      → web-researcher (conditional)
        ↓
[Step 2: Review]             → run_root_review_pipeline.py produce（root-owned、直接 consume, #2380）
        ↓
 (status, verdict, next_action) の組で routing する（bounded decision, #1873/#2380。詳細・
 routing table は下記 Step 2 参照）
   ├─ ok / approve / proceed                       → Step 2.5 → Step 4.5 → Step 5
   ├─ ok / needs-fix / request_changes              → iteration += 1 → Step 4（rewrite。
   │                                                   max_iterations 到達時は Step 5）
   ├─ ok / needs-fix / human_judgment_required（exact な組合せのみ）→ Step 5 (human_review_required)
   └─ 上記以外の不整合な組（status != ok / input_or_runtime_error を含む）→ fail-closed で停止
```

canonical Step 2 は `issue-reviewer` SubAgent を起動せず、11 行 wire（`compact_result.stdout_lines`）
を relay しない（Issue #2380）。`issue-reviewer` SubAgent・`classify_child_stdout()`・
`retry_once_on_transport_failure()` は legacy CLI・診断・回帰テスト専用であり、canonical
routing の必須処理ではない。Step 2a（旧 Replay Arbitration、#1532 V2 契約）は #1873 で
撤去された。Step 3（adversarial review）と Step 1.5（spec document review）は採用しない。
Step 番号は履歴互換のため維持する。

## LOOP_STATE
`LOOP_STATE_V1` のフィールド定義・routing semantics・next action 決定手順は
`references/loop-state.md` を参照する（#1873: `schemas/loop_state.schema.json` の JSON
Schema ファイルと `build_loop_state.py` builder は撤去済み — orchestrator が planner /
review の結果から直接組み立てる plain dict である）。next action の決定は
`scripts/decide_next_loop_action.py` に委譲する（呼び出し手順は `references/loop-state.md`
を参照）。

routing-critical フィールド（`scope_rollup_decision`、`scope_signal_guard`、`delivery_rollup`、
`follow_up_materialization`、`superseded_decision`）の定義は `references/loop-state.md` が SSOT。
orchestrator はこれらのフィールドを直接 prose 再判定しない。

主要な consumer フィールドの例: `web_research:` (web-researcher 実行状態) と
`critical_claims`（planner の `critical_external_claims` を join 時だけ参照する互換名）。
これは新しい永続 ledger ではなく、current planner result に束縛された consumer input
である。未解決 external claim は、その decision dependency が `dispositive` で
repository evidence が disposition を独立に決定できない場合だけ human_escalation
候補になる。

## 手順 (Procedure)
### Step 0: 前提条件 (Preconditions)
1. Issue 本文と必要コメントを取得する。`state/needs-human` / `state/done` は presentation-only / non-authoritative metadata（#2084）であり、それら label の単独付与だけで hard stop としない。停止には別 authority — OWNER の明示指示、または `human_judgment_required`（scope-signal-guard 等の別 authority）— が必要である。`state/done` の代替として GitHub native Issue `closed` state（`gh issue view --json state`）を参照する。
2. `anchor_comment_url` がある場合は snapshot を固定し、対象 Issue 所属を検証する。
3. scope rollup preflight を mutation-free で実行し、`LOOP_STATE.scope_rollup_decision` を記録する。
4. Product/Spec routing signal を検知し、`LOOP_STATE.product_spec_context` を更新する。
5. 本 Issue への refinement 継続が確定した後に、stale な `state/blocked` / `state/queued` を hygiene として除去する。

### Step 0f: Planner 結果の消費 (Planner Consumption)

canonical bare `preflight.run` は `workflow_start_entry.py` を first hop とし、Step 0g mutation・SubAgent dispatch・GitHub mutation より前に workflow capability preflight を一度だけ実行する（`decision: blocked` なら以降を起動しない。Step 0 のこの preflight と Step 5 の preflight は独立した fresh call であり、Step 0 の結果を Step 5 の implementation-entry authorization として再利用しない）。`decision: ready`/`degraded` の場合のみ `workflow_start_entry.py` が `run_refinement_preflight.py` wrapper を trusted `uv` 経由で一度だけ起動し、Issue fetch・anchor comment 構造検証・planner stdin 組立・`REFINEMENT_LOOP_PLAN_V1` 生成を一括実行する（判断ロジックは `plan_refinement_loop.py` に委譲）。

コマンドの canonical な argv 定義は `ISSUE_REFINEMENT_COMMAND_REGISTRY_V1`（`scripts/command_registry.py`）に集約されている。SubAgent / main thread は手書き shell string を消費せず、registry entry を参照する。bare `preflight.run` の production 実行はこれらのゲートを迂回するため `run_refinement_preflight.py` を直接起動してはならず、canonical executor 経由の呼び出しのみを使う（PR #2320 review P0-3）。capability request（`planned_operations`）を宣言する場合は `LOOP_PLANNED_OPERATIONS_JSON` を永続 export せずこの1回の invocation にだけ設定する（`_sanitize_env()` は `command_id == "preflight.run"` の場合のみこの変数を child へ通す。未宣言時は producer を呼ばず `environment_failure` で fail-closed）。GPT-5.3-Codex-Spark delegation は退役済みのため（Issue #2651）、`LOOP_SPARK_MODE`/`LOOP_SPARK_FALLBACK` の宣言・export 手順はもはや存在しない（`_sanitize_env()` はこの2変数を一切 child へ通さない）:

```bash
LOOP_PLANNED_OPERATIONS_JSON='[{"phase":"<p>","actor_role":"<r>","operation":"<op>","requires_mutation":<bool>}]' \
uv run --locked python3 scripts/agent-guards/skill_runtime_exec.py \
  --command-id preflight.run --issue-number <N> --repo <owner/repo>
```

root checkout（canonical main root / default branch）から anchor comment を指定して preflight を実行する場合は、上記の直接 wrapper 呼び出しではなく、origin lane を明示する正規の privileged executor profile を使う。`preflight.run.with_anchor` は URL だけを扱う unlabeled profile であり、human origin を推定せず fail-closed にする。direct human context は `preflight.run.with_human_context`、agent report は read-only の `preflight.run.with_agent_report` を使う。以下は direct human context 用の厳密な token 列（`--locked` を含まない）そのものであり、`uv run --locked` governance policy の対象ではない:

<!-- policy-example --><!-- 以下は方針の例を示すコメントであり、実行対象のコマンド構文には影響しない -->
```bash
uv run --locked python3 scripts/agent-guards/skill_runtime_exec.py \
  --command-id preflight.run.with_human_context \
  --issue-number <N> \
  --repo <owner/repo> \
  --anchor-comment-url <canonical GitHub issue comment URL>
```

`--anchor-comment-url` は `https://github.com/<owner>/<repo>/issues/<N>#issuecomment-<M>` の canonical shape のみを受け付け、`--issue-number` / `--repo` と URL 内の owner/repo/issue 番号が一致しない場合は拒否される（context-binding）。同一 URL を human / agent の両 lane に渡すこと、または unlabeled URL を human origin として扱うことは fail-closed である。`preflight.run` 自体の argv / placeholders / execution_class はこの sibling profile の追加によって一切変更されない。

#### Step 0g: trusted contract update（main control-planeを実行する親control-plane限定）

`preflight.run.with_human_context` が trusted contract patch plan を得た後だけ、main control-plane は canonical main root / default branch から次の明示phaseを実行できる。agent report / unlabeled anchor はこの mutation phase に到達できない。preflight entryの `mutation: false` は変更せず、このphase以外に `--consume-contract-patch-plan` を渡してはならない。

```bash
uv run --locked python3 scripts/agent-guards/skill_runtime_exec.py \
  --command-id contract_update.run.with_human_context \
  --issue-number <N> \
  --repo <owner/repo> \
  --anchor-comment-url <canonical GitHub issue comment URL>
```

このphaseは既存patch planをtransaction-localにconsumerへ渡し、candidate static readiness、controlled transaction、final readback、fresh preflight/review/readiness入力までを一続きに実行する。subagent / isolation worktree はこのcommandを直接実行しない。新しい永続schema、receipt、publisher、state storeは作らない。

**investigation evidence transport（Issue #2678、#2086 AC3 の mutation phase 拡張。詳細・primary-root 内部生成・write-zero 保証は `references/anchor-comment-handling.md` の「mutation phase（#2678）」節を正本として参照）:** read-only `preflight.run.with_human_context` が `--investigation-evidence-transport-path` で `contract_update_required` decision を得た場合、その read-only invocation で使用した**同一の manifest path**を、直後の別の（command ID が異なる）`contract_update.run.with_human_context` invocation に再利用してそのまま渡す（manifest を再生成しない。例: `skill_runtime_exec.py --command-id contract_update.run.with_human_context --issue-number <N> --repo <owner/repo> --anchor-comment-url <canonical URL> --investigation-evidence-transport-path <repo-relative manifest path>`）。`optional_flag_pair`（省略時は byte-identical）。`--investigation-evidence-primary-root` は caller 指定不可（executor 内部生成）。検証失敗時は mutation consumer 呼出し・GitHub 更新要求ともに 0 回（write-zero）。対象外 boundary flag はこの緩和の対象外。

wrapper の出力フィールドを確認する（**canonical stdout フィールド（機械可読）**）:
- `STATUS: pass | warn | needs_fix | blocked | environment_failure` — 常に出力される
- `NEXT_ACTION: proceed | proceed_with_notes | apply_deterministic_repair | apply_deterministic_structural_repair | human_judgment_required | fix_environment | issue_editor_required` — 常に出力される（`issue_editor_required` は `--consume-contract-patch-plan` 経路限定。下記「`NEXT_ACTION: issue_editor_required` 受信時」参照）
- `MUST_READ:` — 読むべきパス一覧（空の場合は省略）
- `COMMANDS_JSON:` — full command spec objects（canonical machine-consumable、空の場合は省略）
- `COMMANDS_DISPLAY:` — human-readable display（display_only=true、空の場合は省略）
- `BLOCKERS:` — ブロッカーコード一覧（空の場合は省略）
- `ARTIFACT:` — 書き込まれた artifact の key: path 一覧（空の場合は省略）。`STATUS: needs_fix` の場合は `repair_diagnostics` / `repair_candidate_body` も含まれる（Issue #2016 iteration-3 P1-1。`repair_action.diagnostics_artifact` / `.candidate_body_artifact` と同一パスを canonical artifact map からも参照可能にする）
- `REPAIR_ACTION:` — versioned `repair_action` disposition（Issue #2016。`STATUS: needs_fix` の場合のみ出力される。`disposition: auto_apply_safe` と diagnostics/candidate body artifact パス・original/repaired SHA を含む）

**`NEXT_ACTION: issue_editor_required`（Issue #2048 Scope Delta、Issue #2620 で拡張）:**
`contract_update.run.with_human_context` 経由の `--consume-contract-patch-plan` 実行が `full_rewrite_required` disposition を検出した場合に返る。到達経路は (1) 承認済み trusted anchor scope reframe（構造化 `ANCHOR_SCOPE_REFRAME_V1`、非空 `allowed_path_deltas`）で派生 `CONTRACT_PATCH_PLAN_V1.operations[]` が空（#2048）、または (2) 構造化 scope reframe が存在せず section-bound patch derivation でも表現不能な explicit trusted `human_review_directive`（`authority_category == human_review_directive` かつ explicit confidence かつ `with_human_context` lane かつ trusted principal かつ same-target/anchor binding valid、#2620）の 2 つ。(2) は `decide_rewrite_route.decide_human_review_directive_editor_route()`（routing SSOT）が判定し、`consume_trusted_anchor_contract_patch_plan()` がその結果だけを consume する（新規 classifier 不使用）。この内部 consumer 結果が持つ `rewrite_route.reviewer_feedback_url`（canonical anchor comment URL、raw snapshot 本文ではない）は `_bounded_contract_update_handoff()` の canonical `contract_update` projection（`status`/`disposition`/`writes`/`iterations`/`final_readback`/`fresh_preflight`/`fresh_review`/`fresh_readiness`/`reason_code` のみ）には含まれず、`preflight.run` / `contract_update.run.with_human_context` の stdout・ARTIFACT を通じて main/root thread へ新規伝搬されるフィールドではない（PR #2623 review fix、finding 4: 新規 schema key は追加しない）。`NEXT_ACTION: issue_editor_required` を受信した main/root thread は、このphaseを起動した際に自身が渡した `--anchor-comment-url`（= `--human-context-comment-url`、canonical anchor comment URL）をそのまま Step 4 `issue-editor` の `reviewer_feedback_url` として再利用する。raw snapshot 本文は転送しない。いずれの経路でも:
- `contract_update.run.with_human_context` を再実行しない（no-progress な同一 mutation の再試行は禁止）
- scope-reframe comment を再投稿しない（trusted anchor は既に Issue 上に存在するため新規 comment は不要）
- 既存の `issue-editor` / `edit-issue` controlled transaction route（Step 4 相当）へ handoff し、Issue 本文の完全な rewrite を行う
- handoff 後の mutation が完了しても、body/title readback・fresh `preflight.run` 再実行・fresh Step 2 review・fresh readiness が全て完了するまで implementation を許可しない（`_bounded_contract_update_handoff()` の既存 post-update gate と同じ 6-gate 基準を満たすまで `NEXT_ACTION: proceed` 相当の implementation authorization に進まない）

**`STATUS: needs_fix` / `NEXT_ACTION: apply_deterministic_repair`（Issue #2016）:**
`repair_issue_contract.py` が既知の safe な deterministic repair（disposition: `auto_apply_safe`）を1件以上検出した場合、`run_refinement_preflight.py` は generic blocker を追加せず `STATUS: needs_fix` / `NEXT_ACTION: apply_deterministic_repair` を返す。この `NEXT_ACTION: apply_deterministic_repair`（`repair_action.disposition: auto_apply_safe`）は、`command_registry.py` の `ISSUE_REFINEMENT_COMMAND_REGISTRY_V1` に登録済みの controlled consumer `repair_action.apply`（Issue #2039 / PR #2202、2026-08-16 マージ済み）を `skill_runtime_exec.py` 経由で `uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id repair_action.apply --issue-number <N> --repo <owner/repo> --apply-repair-action <preflight_result_path>` として起動し実処理する（`<preflight_result_path>` は canonical main root からの repo-relative path でなければならない。`preflight.run` の `ARTIFACT:` は絶対パス（`absolute_path`）で出力するため、そのまま渡すと `skill_runtime_command_policy.py` の repo-relative path 検証（`_is_safe_repo_relative_fixture_path()`）に拒否される — canonical main root に対する相対パス（例: `.claude/artifacts/issue-refinement-loop/<N>/refinement_preflight_result_v1.json`）へ正規化してから渡す。加えて `preflight.run` が生成した対象 Issue 自身の `.claude/artifacts/issue-refinement-loop/<N>/` 配下の fresh artifact パスに限られ、他 Issue の artifact や絶対パスを含む任意の不正な repo-relative path は拒否される。内部は `edit_issue_txn.py` 経由の controlled transaction のみで、raw `gh issue edit` は呼ばない）。`repair_action.apply` は `required_cwd: canonical_main_root` / `required_branch: default_branch` で canonical main root から実行するが、`ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS` には含まれないため、対象 Issue 番号を `LOOP_ISSUE_NUMBER` 環境変数に設定した上で、`select_issue_worktree()` の命名規約（branch short name は `(worktree-)?issue-<N>-*`、path basename は `issue-<N>-*`。selector 自体は親ディレクトリを条件にしないが、bootstrap による標準配置先は `.claude/worktrees/` 配下）に合致する単一の既存 worktree が存在することも同時に要求する（未作成、または命名規約に合致しない場合は手作業でリネームせず `worktree_bootstrap_exec.py` による worktree bootstrap を先に実行する。canonical main root からの実行と active-issue-bound worktree の存在は独立した別要件であり両方を満たす必要がある）。戻り値は `mutation_outcome`（`applied` / `no_change` / `unknown` / `not_attempted` の4値）に加え `phase` / `failure_code` を持ち、後続処理はこの3軸の組み合わせで分岐する。fresh preflight（`preflight.run` の再実行）を行い Step 2 review を再開できるのは、プロセス終了コード0 かつ `phase: complete` かつ `failure_code: null` かつ `mutation_outcome` が `applied`（`repair_action.apply` 内部の1回限りの authoritative readback で確認済み）または `no_change` の場合に限る。`applied` かつ `phase: fresh_validation`（`failure_code: source_lane_mismatch` または `fresh_validation_failed`）や consumer 自体の非ゼロ終了は、GitHub 側の mutation が既に起きた可能性を保持したまま停止し、Step 2 review 再開の implementation authorization を出さない。`unknown`（readback 自体が `repair_action.apply` 内部で既に一度だけ試行済みのため）は `repair_action.apply` を盲目的に再試行せず、bounded な report を添えて停止し人間判断を仰ぐ。`not_attempted` も再試行せず `failure_code` で分岐する: `secure_open_rejected` / provenance mismatch 各種（例: `cross_issue_provenance_mismatch`）/ `second_body_drift` / `transaction_execute_error` のような secure-open・provenance・transaction・環境系の `failure_code` は Step 4 rewrite へ送らず環境/control-plane recovery とし、それ以外（例: repair candidate 単独では別の既存 readiness gate を解消できない）の場合のみ通常の Step 2 review → Step 4 rewrite サイクルへフォールバックする。disposition が `auto_apply_safe` 以外（`human_review_required` / `informational` / `invalid_payload`）の場合はこの consumer の対象外であり、引き続き fail-closed で `STATUS: blocked` 相当の human judgment route のままである。
**`STATUS: needs_fix` / `NEXT_ACTION: apply_deterministic_structural_repair`（Issue #2402）:** `structural_repair_action.disposition_summary: auto_apply_safe` の canonical next action は fresh preflight artifact に限り canonical main/root thread が `skill_runtime_exec.py` を通じ `structural_repair_action.apply` へ dispatch する。実行形は `uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id structural_repair_action.apply --issue-number <N> --repo <owner/repo> --apply-structural-repair-action <preflight_result_path>` である。`<preflight_result_path>` は absolute `ARTIFACT:` 表示のままではなく canonical main root からの repo-relative `.claude/artifacts/issue-refinement-loop/<N>/refinement_preflight_result_v1.json` に正規化し、対象 Issue artifact root、canonical root/default branch、`LOOP_ISSUE_NUMBER=<N>` に対応する active Issue worktree を要求する。generic `apply_deterministic_repair → repair_action.apply → --apply-repair-action` とは独立し、structural command + generic flag、generic command + structural flag、両 flag、extra argv、other-Issue/absolute/traversal/symlink path は dispatch 前に reject する。root は command ID / outer flag / repo / Issue / path / cwd / branch / active-worktree context だけを検証し、nested structural payload、disposition、digest、provenance、body drift は既存 consumer の責務として再解析しない。fresh preflight と fresh Step 2 review の再開は **exit 0** かつ **`phase: complete`** かつ **`failure_code: null`** かつ **`mutation_outcome` が `applied` または `no_change`** の場合だけであり、`unknown` / `not_attempted` / incomplete / nonzero では implementation authorization を出さず盲目的に再試行しない。
**非 canonical / 抑制フィールド:**
- `SUMMARY` — 人間向け prose、オーケストレーターは consume しない
- `DO_NOT_READ` — 予約済み（現在は常に空）、consumers は欠如に依存してはならない
- `EVIDENCE` — raw issue body / comments は stdout に出力されない（artifact のみ）

**warn (exit 1) の定義:** planner exit 0 かつ `fail_closed.required == false` かつ `decisions.*.confidence` に `"unknown"` が 1 つ以上含まれる場合、`STATUS: warn` / exit 1 を返す。human note が必要だが blocking ではない。`NEXT_ACTION: proceed_with_notes` に従って継続できる。

- `NEXT_ACTION:` に従って後続ステップを決定する
- `ARTIFACT:` の `refinement_preflight_result_v1` パスから `fail_closed` / `decisions` を参照する
- `ARTIFACT:` の `planner_input` パスで planner へ渡した stdin JSON を確認できる

`STATUS: blocked` または `STATUS: environment_failure` の場合は停止し、人間判断へ送る。ここでの `STATUS: environment_failure` は Step 0f preflight の repository/environment failure であり、Step 1b の external evidence provider transport status とは別である。`investigation_policy` / `web_research_policy` / `scope_signal_guard` / `follow_up_materialization` の判定は planner を SSOT とし、このファイルで prose 再判定しない。

`scope_signal_guard.triggered: true` が含まれる場合でも、preflight 完了直後は
`decide_next_loop_action.py` を呼ばない（#1873: `ISSUE_REFINEMENT_PHASE_STATE_V1`
の formal phase-gate は撤去された。呼び出しタイミングの制御は orchestrator が
Step 順序に従って行う）。planner の `investigation_policy` / `web_research_policy`
に従って Step 1 / Step 1b / Step 2 へ進む。

参照:

- `references/refinement-loop-plan-output.md`
- `references/scope-signal-guard.md`

#### Parent-owned preflight（isolation worktree agent への委譲時）

`preflight.run`（`skill_runtime_command_policy.py` が要求する `required_cwd: canonical_main_root` / `required_branch: default_branch` invariant）と、Agent tool の `isolation: "worktree"` で生成される汎用 worktree の cwd は衝突する。この衝突を解決するため、isolation worktree agent へ Step 0f 相当の作業を委譲する場合は **parent-owned preflight** 方針を採用する: parent（orchestrator 自身、canonical main root・default branch で稼働する 主スレッド）が `preflight.run` を実行し、bounded な結果（`NEXT_ACTION` / `MUST_READ` / `ARTIFACT` 等の canonical stdout フィールドのみ）を isolation agent へ渡す。isolation agent はその bounded な結果を入力として consume するだけであり、preflight 実行そのものは行わない。

isolation agent は `skill_runtime_exec.py`（exact executor）を自ら実行しない。isolation agent は `run_refinement_preflight.py`（direct wrapper）も自ら実行しない。いずれも parent が canonical main root で実行し、その出力のみを isolation agent へ引き渡す。

`agent-*` 等の未保証な isolation worktree 命名パターンを `preflight.run` の認可根拠として追加しない。`skill_runtime_command_policy.py` の `required_cwd` / `required_branch` invariant は緩和せず、認可判定は canonical main root / default branch の実行コンテキストのみを根拠とする。

既存の Step 0f 直接実行 bash block（canonical executor 経由で `preflight.run` を呼ぶ例）は、orchestrator 自身が canonical main root もしくは canonically-named issue worktree から直接実行する場合に限定される。isolation worktree agent からは直接実行しない — isolation agent への委譲時は必ず上記の parent-owned preflight 方針に従い、parent が実行した結果のみを渡す。

### Step 1: 事前調査 (Investigation)

`REFINEMENT_LOOP_PLAN_V1.decisions.investigation_policy.required == true` の場合のみ `codebase-investigator` を起動する。返却される構造化結果を受け取り、`final_classification` の確定責務は main thread が保持する。SubAgent は mutation してはいけない。この Step が **local-asset advisory** を選択したときだけ、outer delegation に literal `CALLER_ROUTE: agent/issue-refinement-loop.local-advisory` を付ける。これは routing instruction であって controller stdin の field ではない。caller は planner から導出した nonempty repository-relative regular-file `target_paths` と nonempty `purpose` で exact `AGY_ADVISORY_INVOCATION_REQUEST_V1` を構築する。stdin object は `schema`、`schema_version: 1`、`mode: "codebase_local_asset"`、`purpose`、`target_paths`、optional `context_paths`、`agy_investigation_requirement: advisory|explicitly_required` だけとし、global fields、route marker、raw prompt、wrapper path/result、failure class、provenance を入れない。legacy `agy_advisory_native_fallback_allowed` はこの named pre-controller ingress だけで requirement へ one-way map して削除する（requirement と legacy が両方あれば fail-close）。named route はその exact JSON を `run_codebase_investigator_agy_advisory.py` の stdin に渡し、stdout decision と stderr success sidecar を別 stream として capture する。exact exit-0 `ok/continue_agy_result` + strict sidecar、又は exact exit-0 `degraded/native_non_mutating_fallback` だけを受理する。後者だけが bounded non-mutating native investigation を許可し、`explicitly_required`、pre-AGY、malformed/unknown result、exit 1 の `failed/fail_closed`、exit 2、欠落/余計な bytes/duplicate key/不正 pairing は fail-close である。それ以外の investigation（global `target_path` / `target_symbol`、`keywords` / `issue_body`、agent-retrospective の arbitrary `CALLER_TASK_DATA.task` を含む）は global `codebase-investigator` contract のままとする。controller を起動せず、`local_asset_research` 又は `github_research` canonical builder → existing wrapper/result/evidence flow を使う。caller-supplied request/result/evidence destinations は verbatim で使用し、expected actual result/evidence/hash が materialize されなければ成功を主張しない。`root_entry_router.py::capability_preflight_result()` / `workflow_start_entry.py` への新配線は行わない。

anchor comment の fact-check 契約、`ANCHOR_COMMENT_CONTEXT_V1`、`ANCHOR_COMMENT_FACT_CHECK_RESULT_V1`、`REPO_EVIDENCE_REF_V1` の扱いは `references/anchor-comment-handling.md` を参照する。

### Step 1b: 外部Web調査 (Web Research)

`REFINEMENT_LOOP_PLAN_V1.decisions.web_research_policy.required == true` の場合のみ `web-researcher` を起動する。外部 claim のハルシネーションまたは repository evidence だけでは解消できないエビデンス不足を切り分けるため、orchestrator は `WEB_RESEARCH_RESULT_V1` を opaque に扱い、consumer field だけを `LOOP_STATE.web_research` へ反映する。retry / fallback / raw grounding state は保持しない。

Step 1（codebase-investigator）と Step 1b（web-researcher）は、両方が required の場合に並列実行できる。ただし両 SubAgent の結果を Step 2 前に合流させること。

web-researcher は AGY-first / native Web fallback を内部で完結する。critical claim に一次資料エビデンスを示せず、両 route でも検証不能またはハルシネーション疑いと判定した場合だけ `human_escalation` に倒す（Step 5）。provider telemetry / provenance trace の不足だけを escalation 理由にしてはならない。

Step 1 と Step 1b の完了後、main thread は current repository state、実 diff、実 test
等の Step 1 evidence から `repository_decision` を組み立て、planner の
`critical_external_claims[].role` と `WEB_RESEARCH_RESULT_V1` を
`web_research.route` registry entry で join する。`non_dispositive` は repository
evidence が requested disposition を独立に決定すると確認できた場合だけ使う。

join result が `next_action: proceed` または `proceed_with_notes` なら Step 2 へ進む。
後者は `transport_status: environment_failure` / `semantic_verdict: null` と
unresolved risk を記録するが、それだけで Step 5 に進めない。dispositive claim が
未解決、または repository decision が inconclusive の場合だけ
`human_judgment_required` として Step 5 へ進む。web-researcher の retry/fallback、
grounding trace、citation/provider provenance をこの consumer が再実装・保存しては
ならない。consumer は `verification_route` の個々の値（`native_web` 等の producer 追加
route を含む）を re-validate せず、`status: ok` かつ `verification_route` が非空なら
transport/grounding 成功と扱う（詳細は `references/web-research-routing.md`）。

詳細は `references/web-research-routing.md` を参照する。

### Step 2: レビュー (Review)

**producer I/O は root-owned、compact envelope 生成も含め単一 producer（Issue #2049 / merged PR #2135、AC8 SSOT を #2054 が拡張）**: live body fetch・body SHA 固定・`check_issue_contract` / `contract_readiness_check` / merge_readiness の実行・`ISSUE_REVIEW_RESULT_COMPACT_V2` compact envelope の生成・full artifact / compact artifact の canonical artifact directory への保存は、すべて orchestrator（main thread）が
`.claude/skills/issue-refinement-loop/scripts/run_root_review_pipeline.py produce --issue-number <N> --repo <owner/repo>`
（`root_review_pipeline.produce`, command_registry.py 登録済み、外部 CLI 契約は #2054 でも変更しない）を実行して行う。`produce` の内部実装は #2054 で `.claude/skills/issue-refinement-loop/scripts/reviewer_transport.py`（V2 契約 SSOT）へ委譲し、`ISSUE_REVIEW_RESULT_COMPACT_V1`（9行、`EVIDENCE` あり）ではなく `ISSUE_REVIEW_RESULT_COMPACT_V2`（`ATTEMPT_ID`/`ARTIFACT_SHA256` を含む exact 11行 grammar）を生成・永続化する。V1 producer（`compact_review_result.py` の compact 生成関数）は retired であり、同一 commit での atomic cutover に downgrade fallback はない。**Step 2 実行契約: 明示的 background launch + mandatory completion join（Issue #2610 AC6、OWNER REQUEST_CHANGES 反映）**: Claude Code main thread の Bash tool から上記 `root_review_pipeline.produce` を呼び出す際は、`run_in_background: true` を明示的に指定して background 実行し、その background task の完了を示す task notification を join してから（＝実際に完了した JSON 結果を取得してから）canonical Step 2 routing（下記 `canonical_step2_route` の読み取り）へ進む。background起動した時点でStep 2完了扱いにしない — `run_in_background: true` の呼び出しそのものは Step 2 の完了を意味せず、完了 JSON の取得（join）までが本 Step 2 実行契約の必須手順である。この background + join 契約は inner の `ReviewBudget`（invocation-local な per-attempt/total deadline、`reviewer_transport.run_reviewer_transport()` が実際に強制する SIGTERM→bounded grace→SIGKILL→reap semantics）を置き換えるものではなく、outer 側（Claude Code 自体の background task 実行モデル）の責務として inner budget と独立に維持する。`command_registry.py` に宣言された `root_review_pipeline.produce.timeout_seconds` は、これを実際に読んで強制する repository-owned consumer が現状存在しないため unenforced / informational な値であり、本契約（background + join）が実際の execution model である（詳細は `command_registry.py` 内の当該コメントを参照）。

**canonical Step 2 routing は `produce` の JSON 出力を直接 consume する（Issue #2380、routing authority は #2389 で `canonical_step2_route` へ一本化）**: `produce` の JSON 出力には root-verified な `compact_result.verdict` / `compact_result.next_action`（`reviewer_transport.run_reviewer_transport()` が内側で行う deterministic checker transport の bounded retry、および `_cmd_produce()` 自身が行う `verify_artifact()` / `verify_wire_matches_artifact()` による artifact 整合性検証を経て確定した値）と `verified_transport_artifact`（`REVIEWER_COMPACT_ARTIFACT_V2`）に加えて、トップレベル `canonical_step2_route` フィールド（`route_canonical_step2_result()` の戻り値そのもの。`_cmd_produce()` 自身が全 stdout JSON 出力経路——成功時 / body fetch 失敗時 / VC budget エラー時 / transport failure 時 / artifact readback failure 時のすべて——で計算済み）が含まれる。canonical Step 2 は、この `canonical_step2_route` を routing の唯一の入力（唯一の routing authority）として直接使用し、`status` / `compact_result.verdict` / `compact_result.next_action` から独自に再計算しない。`issue-reviewer` SubAgent（`.claude/agents/issue-reviewer.md`）は canonical Step 2 では起動されない（routing authority を持たない）。`compact_result.stdout_lines`（11行 wire）を issue-reviewer に relay することも行わない。これは Issue #2049 の root-owned pipeline アーキテクチャ（producer I/O は root-owned pipeline のみが行う）を維持したまま、#2054 が compact envelope の wire format を V1 から V2 へ atomic cutover した変更を引き継ぐ。`run_root_review_pipeline.py` は #2054 が新設する第二の producer pipeline ではなく、既存の `classify_child_stdout()` / `retry_once_on_transport_failure()` / `readback_persisted_artifact()` の内部実装を `reviewer_transport.py` へ委譲する形で単一の V2 契約実装へ統合されている（AC8）。**#2380 以降、`classify_child_stdout()` / `retry_once_on_transport_failure()` / `run_root_review_pipeline.py classify-child-stdout` CLI と issue-reviewer SubAgent の起動は canonical Step 2 routing の必須処理から外れた。これらは legacy CLI・診断・回帰テスト専用として引き続き利用可能であり、exact 11-line grammar を強制する strict compatibility boundary としての `validate_review_compact_output.py` / `reviewer_transport.verify_wire_matches_artifact()` 自体は変更しない（AC3）。** ステップ単位の所有権表は `.claude/skills/review-issue/SKILL.md` の「Producer I/O ownership」節を参照する。
**`produce` 自体が transport failure を返した場合の扱い（AC2/AC4/AC5、OWNER PR #2391 レビュー P0-2 反映）**: `reviewer_transport.run_reviewer_transport()` が内側で行う deterministic checker transport の bounded retry（spawn / timeout / signal / empty output / malformed output を含む）、artifact verification（repo/issue/body SHA/invocation/attempt/raw-byte SHA binding）、final review readback gate は変更しない。`status: input_or_runtime_error` は、`error_code` が既知（`reviewer_transport_environment_failure` / `artifact_readback_failed`）であるか未知であるかを問わず、`canonical_step2_route: fail_closed_environment_or_integrity_failure` を返す。これは Issue #2054 が確立した「transport/artifact 障害は `transport_status: environment_failure` / `semantic_verdict: null` の environment failure であり、`human_judgment_required`（意味論的判断）と混同しない」という分離契約を維持するためであり、Issue #2389 はこの分離契約を supersede せず preserve する（`step_5_human_judgment_required` へは分類しない）。symlink／root escape／attempt mismatch／hash mismatch を含む artifact 整合性検証は `produce` 内部で完結しており、この整合性検証ロジック自体は変更しない。消費側契約 (consumer contract): `ISSUE_REVIEW_RESULT_COMPACT_V2`（正本 (SSOT): `.claude/skills/issue-refinement-loop/scripts/reviewer_transport.py`）。V1（`ISSUE_REVIEW_RESULT_COMPACT_V1`、`compact_review_result.py`）は retired であり、partial deployment・downgrade fallback は禁止する。

**canonical Step 2 routing table（AC1/AC2、追加の classify / relay ステップなしに直接評価する。Issue #2380 P0-2: `(status, verdict, next_action)` の組で判定し、`verdict` 単独では判定しない。Issue #2389: `route_canonical_step2_result()` の実行は `produce`（`_cmd_produce()`）内部で完結しており、その戻り値は `produce` の stdout JSON のトップレベル `canonical_step2_route` フィールドとしてすでに配線済みである。canonical Step 2 はこの `canonical_step2_route` を唯一の routing authority として読むだけであり、`route_canonical_step2_result()` を自ら再度呼び出したり、`status` / `verdict` / `next_action` から独自に再計算したりしない）**: `canonical_step2_route` は次のいずれかの値を取る:

- `canonical_step2_route: step_2_5`（`status: ok` + `compact_result.verdict: approve` + `compact_result.next_action: proceed` に対応）→ Step 2.5（セマンティック設計レビュー applicability 判定）へ進む。Issue #2389: deterministic approve は Step 2.5 の判定を必ず経由し、Step 2.5 を迂回して直接 Step 4.5 へは進まない（`semantic_review_applicable: false` の場合のみ Step 2.5 自身が直接 Step 4.5 へ進める。下記「Step 2.5」節参照）
- `canonical_step2_route: step_4`（`status: ok` + `compact_result.verdict: needs-fix` + `compact_result.next_action: request_changes` に対応。同一 payload の verified `merged_review_result.failure_class` が欠落・`None` の場合はこちら）→ `decide_next_loop_action.py`（rewrite 後 / next-action 決定時に呼ぶ、下記「LOOP_STATE」参照）へ VERDICT をそのまま渡す（**#1873: 旧 Step 2a Replay Arbitration は撤去された** — orchestrator は VERDICT を独立に再計算せず直接信頼する）。`command_not_allowed`（VC が preflight allowlist 外のコマンド形を使っている。body 書き換えで解消可能）はこちらへ分類され、下記 `step_5_operator_intervention_required` へは分類されない（Issue #2397 Scope Delta AC9/AC10、OWNER PR #2398 レビュー P0-1）
- `canonical_step2_route: step_5_operator_intervention_required`（Issue #2397。`status: ok` + `compact_result.verdict: needs-fix` + `compact_result.next_action: request_changes` と**同一 triple かつ同一 payload**の verified `merged_review_result.failure_class == "contract_readiness_human_judgment"` の場合のみ。`contract_readiness_human_judgment` は `env_missing_dep` / `timeout` / unknown 分類など Issue 本文書き換えでは解消しない operator 起因の readiness 状態で、genuine semantic / owner ambiguity 用の下記 `step_5_human_judgment_required` とは独立した5番目の分岐として分離する。compact V2 wire の `NEXT_ACTION: proceed | request_changes` 二値契約は変更しない）→ 既存の Step 5 handler を再利用し、`termination_cause: operator_intervention_required` として区別して停止する（詳細は `issue-refinement-loop/references/termination-policy.md` 参照）。**この Step 5 operator intervention route の終了処理は、`produce` の stdout JSON に同梱される `canonical_step2_disposition` の `termination_reason` / `termination_cause` フィールドをそのまま使用し、Step 5 終了処理側で独自に再導出しない（Issue #2397 Scope Delta iteration 3 P1-1）**
- `canonical_step2_route: step_5_human_judgment_required`（`status: ok` + `compact_result.verdict: needs-fix` + `compact_result.next_action: human_judgment_required` の **exact な組合せのみ**に対応。この組合せは現行 Compact V2 producer（`build_compact_v2()`）からは生成されない defensive/legacy route であり — `build_compact_v2()` の `NEXT_ACTION` は `proceed | request_changes` の二値のみを生成する — genuine semantic / owner ambiguity の実経路は Step 2.5 semantic design review が別途所有する（Issue #2397 Scope Delta iteration 3 P1-2）。この行は `review-issue/SKILL.md` の「Producer I/O ownership」節が説明する既存 compact schema 上の値域を defensive に受理するものである。OWNER PR #2391 レビュー P1-1: `verdict` を確認しない緩い一致（`approve` + `human_judgment_required` 等）は下記 `fail_closed_environment_or_integrity_failure` に分類する）→ Step 5 (human_review_required) へ。genuine semantic / owner ambiguity 用であり、operator 起因の readiness 状態を扱う上記 `step_5_operator_intervention_required` とは別の停止理由として扱う
- `canonical_step2_route: fail_closed_environment_or_integrity_failure`（上記いずれにも一致しない組。`status != ok`（`input_or_runtime_error` は既知/未知の `error_code` を問わず常にこちら。OWNER PR #2391 レビュー P0-2: Issue #2054 の environment_failure 分離契約を維持するため `step_5_human_judgment_required` へは分類しない）、`verdict`/`next_action` の不整合な組合せ、`result` / `compact_result` が `Mapping` 型でない場合を含む）→ environment/integrity failure として fail-closed で停止する（`decide_next_loop_action.py` を呼ばず、通常の rewrite loop へも進めない）
- full structured data は `produce` が返す `verified_transport_artifact`（`REVIEWER_COMPACT_ARTIFACT_V2`、`compact_result.artifact_path` と同一パス）または `merged_review_result` から取得する。`merged_review_result` は `_cmd_produce()` の成功 JSON のトップレベルに含まれ、通常の tool result 経由で main context に実際に返る（Issue #2380 P2: 過去の記述「main context には返らない」は実際の stdout 出力と矛盾していたため訂正）。issue-reviewer 経由の relay は経由しない

anchor comment により stale approval を無効化する場合も、raw snapshot は Step 4 に渡さず、正規化済み `anchor_comment_feedback` だけを渡す。

**重要**: `review` phase（rewrite 前）では `decide_next_loop_action.py` を呼んではならない。
`review` phase での routing は `produce` の stdout JSON トップレベル `canonical_step2_route` にそのまま従う（承認なら Step 2.5、要修正なら Step 4、operator-only readiness（`contract_readiness_human_judgment`, Issue #2397）なら Step 5 operator intervention、genuine semantic / owner ambiguity の human_judgment_required なら Step 5 human judgment、不整合なら fail-closed で停止）。`step_5_operator_intervention_required` は `step_5_human_judgment_required` とは独立した5番目の分岐であり、両者を同一の Step 5 停止として丸めない（詳細は上記 routing table および `references/termination-policy.md` 参照）。

`decide_next_loop_action.py` は rewrite 後 / next-action 決定時にのみ呼ぶ:

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py \
  --loop-state-file <loop_state_path> \
  --review-result-verdict <approve|needs-fix>
```

`decide_next_loop_action.py` は呼ばれた時点で `scope_signal_guard.triggered: true` を
常に hard-stop 判定する（呼び出しタイミングの制御が orchestrator 側の責務であり、
スクリプト自身は phase 概念を持たない。AC4 / #919 回帰維持）。

### Step 2.5: セマンティック設計レビュー (Semantic Design Review)

deterministic `VERDICT: approve` 確定後、`semantic_review_trigger.py` で `semantic_review_applicable` を判定する。`true` の場合のみ `semantic_review_transport.py` → `join_review_results.py` を実行する。`join_review_results.py` が返す `effective_verdict` は `approve` | `needs-fix` | `retry` | `human_judgment_required` の 4 値（#2296 fix_delta iteration 6, P0-3/P1-5）。`retry` は本 Step 2.5 の内部ループでのみ消費する（transport を一度だけ再実行し `retry_already_attempted: true` で再度 join する）中間状態であり、`decide_next_loop_action.py` には一切渡さない。`retry` 以外の 3 値（approve | needs-fix | human_judgment_required）だけを通常の VERDICT として上記 routing にそのまま渡す（approve→Step4.5 / needs-fix→Step4 / human_judgment_required→Step5）。`needs-fix` が semantic finding 由来の場合、結果には追加で `rewrite_lane: "semantic"` と `semantic_rewrite_constraints`（`SEMANTIC_REWRITE_CONSTRAINTS_V1`）が含まれ、Step 4 はこれを再構築せずそのまま `issue-editor` へ転送する（P0-4）。`semantic_review_applicable: false` の場合は本 Step をスキップし直接 Step 4.5 へ 進む。詳細手順は `references/semantic-design-review.md` を参照する。`decide_next_loop_action.py` の既存呼び出し規約は一切変更しない。

### Step 4: 書き換え (Rewrite)

`issue-editor` SubAgent に opaque forwarding payload を渡して本文を更新する（Issue #1734: `issue-author` は `issue-creator` / `issue-editor` に分割済み。既存 Issue の書き換えは `issue-editor` が担う）。AC/VC の baseline fail expectation と review 時の扱いを取り違えないこと。詳細な reflection guard は `references/ac-vc-reflection.md` を参照する。

issue-editor 起動前に、現在本文に対して pre-edit static readiness check を実行する。

```bash
uv run --locked python3 .claude/skills/issue-contract-review/scripts/contract_readiness_check.py \
  --mode preflight-static \
  --body-file <current_body_file>
```

実行コマンド例 (inline form): `contract_readiness_check.py --mode preflight-static --body-file <current_body_file>`

生成側契約 (producer contract): `READINESS_FORWARDING_PAYLOAD_V1`

```yaml
READINESS_FORWARDING_PAYLOAD_V1:
  readiness_result:
    status: go | needs_fix | human_judgment | input_or_runtime_error
    body_sha256: <sha256>
    source_checks:
      - contract_readiness_check.py --mode preflight-static
    errors: []
    readiness_result_ref: <artifact-or-path>
```

`preflight-static` は static body/readiness の事前確認専用であり、execute-mode の `unexpected_pass` 検出は扱わない。

readiness 結果に応じた分岐処理 (readiness routing):

```yaml
exit_code_0:
  status: go
  action: invoke_issue_editor
  readiness_errors: []
exit_code_1:
  status: needs_fix
  action: invoke_issue_editor_with_readiness_result
exit_code_2:
  status: human_judgment
  action: skip_issue_editor_and_go_step5
exit_code_3:
  status: input_or_runtime_error
  action: human_escalation
```

消費側契約 (consumer contract): `ISSUE_AUTHOR_RESULT_COMPACT_V1`（正本 (SSOT): `.claude/skills/issue-refinement-loop/scripts/compact_author_result.py`）

- `STATUS: ok` / `BODY_HASH: <sha256>` → 更新成功、`NEXT_ACTION: proceed` で Step 2 に戻る
- `STATUS: no_change` → 変更なし、`NEXT_ACTION: proceed` で Step 2 に戻る

**final review gate（Issue #2049 AC10）**: `STATUS: ok` / `BODY_HASH` readback により本文更新が確認できた場合にのみ Step 2 の "final review"（この body revision に対する terminal VERDICT を決める review pass）を実行する。readback が未確認、または `run_root_review_pipeline.py readback` が `verdict_identity: false` を返す（persisted artifact が regular file でない / strict JSON でない / repo・issue・body_sha256・invocation_id・attempt のいずれかの binding が一致しない / `semantic_result` が REVIEW_ISSUE_RESULT_V1 スキーマとして不正 / `verdict` が一致しない、のいずれか）間は final review を実行しない。gate-final-review が読む canonical artifact は `produce` の `compact_result.artifact_path`（= `verified_transport_artifact`、`REVIEWER_COMPACT_ARTIFACT_V2`）であり、トップレベルの `artifact_path`（= `full_review_artifact`、`REVIEW_ISSUE_RESULT_V1` フル成果物）ではない（Issue #2242 OWNER adversarial review Blocker 1, https://github.com/squne121/loop-protocol/pull/2246#issuecomment-5328161000）。`run_root_review_pipeline.py gate-final-review --artifact-root <root> --artifact-relative <relative> --expected-repo <repo> --expected-issue <issue> --expected-body-sha256 <sha> --expected-invocation-id <id> --expected-attempt <n> --expected-artifact-sha256 <sha256> --expected-verdict <verdict> --remote-update-ok` が `final_review_allowed: true` を返した場合にのみ次段階（Step 4.5）へ進める。expected-* はすべて呼び出し側（orchestrator）が既知の repo/issue/invocation/attempt から独立に供給し、artifact 自身から再導出しない（Issue #2242 Blocker 2）。
- `STATUS: failed` → 修正失敗、`NEXT_ACTION: human_judgment_required`、Step 5 human_escalation へ
- `partial_failure` は廃止。issue-editor は `ok` / `no_change` / `failed` の 3 値のみを返す。
- full mutation result は `ARTIFACT:` パスから取得する（main context には返らない）

rewrite ループの反復ごとに、checker 実行後に `scripts/decide_rewrite_route.py` を呼び出して `max_rewrite_attempts` 超過・body hash 変化なし・missing set 単調減少なしを runtime で強制し、`route`（`continue_rewrite` / `proceed_to_review` / `human_judgment_required`）に従って routing する。invocation 手順・state 永続化・`human_judgment_required` 連動は `references/termination-policy.md` の「Rewrite Loop Runtime Router（#664）」セクションを SSOT とする。orchestrator は attempt 数や no-progress を prose で再判定しない。

**#2048 regression（承認済み scope reframe が empty operations[] のケース、Scope Delta で production reachability を修正）**: 承認済み trusted anchor scope reframe（`scope_delta_status: approved_by_trusted_anchor` かつ `allowed_path_deltas` 非空）から派生した `CONTRACT_PATCH_PLAN_V1.operations[]` が空の場合、`decide_rewrite_route.py` の `decide_scope_reframe_contract_route()` が `contract_update` ではなく `issue_editor_required`（reason_code: `approved_scope_requires_full_contract_rewrite`）へ route する。

唯一の production 呼び出し元は `run_refinement_preflight.py::consume_trusted_anchor_contract_patch_plan()`（`contract_update.run.with_human_context` の `--consume-contract-patch-plan` 実行がこの関数を呼ぶ）である。この consumer 境界が `known_context["scope_delta_decision"]`（`_classify_anchor_scope_reframe()` が生成する trusted anchor 分類結果）を fail-closed に再検証（型・binding・anchor 一致）した上で `allowed_path_deltas` を抽出し、`scope_signal_delta.run_trusted_anchor_iteration_zero()` の `allowed_path_deltas` / `already_reflected_in_body` 引数へ実際に渡す。`operations[]` の producer が存在しない場合（freeform directive に exact path literal がなく `classify_scope_delta_authority()` が human_escalation へ倒れる場合等）は、`consume_trusted_anchor_contract_patch_plan()` の呼び出し元が empty-operations の `CONTRACT_PATCH_PLAN_V1` を合成し consumer へ渡す。

**#2086 AC3**: `expands_allowed_paths` boundary が理由で `human_escalation` に倒れた operator-selected human-context directive（trusted OWNER/MEMBER/COLLABORATOR、`with_human_context` lane）に対しては、mutation を行わない前に `codebase-investigator` SubAgent（read-only）へ委譲し、directive が意味論的に指す exact repository-relative path を current main から導出する。導出できた場合は `classify_scope_delta_authority()` の `investigation_derived_path_literals` キーワード引数へ渡して再判定する（`references/anchor-comment-handling.md` の「Operator-Selected Human-Context 継続」参照）。導出できない場合、または導出結果でも `contract_patch_plan.operations` が空のままの場合は、上記の `NEXT_ACTION: issue_editor_required` full-rewrite lane へ進む。人間へ手書き `ANCHOR_SCOPE_REFRAME_V1` の再入力を要求してはならない。

empty operations は無条件に `issue_editor_required` を意味しない。承認済み `allowed_path_deltas` が現在の Issue 本文の `## Allowed Paths` セクションに既に全て反映済みの場合は `proven_no_change`（通常の `no_change` no-op、rewrite_route は付与されない）として扱われる。型不正（`operations` が list でない等）や binding 不一致（`anchor_comment_url` / `anchor_comment_hash` が今回の呼び出しと一致しない）は fail-closed に `None` 扱いとなり、同様に `issue_editor_required` へは昇格しない。

`plan_refinement_loop.py` 側にも同じ判定を `decisions.scope_delta_decision.rewrite_route` として echo する opt-in 経路（`known_context.scope_delta_decision.operations` が渡された場合限定）があるが、これは診断用の echo であり canonical な mutation-phase routing は上記 consumer 境界が担う。

`full_rewrite_required` が検出された場合、`run_refinement_preflight.py` の wrapper は通常の `STATUS` 由来 `NEXT_ACTION` 判定を上書きし `NEXT_ACTION: issue_editor_required` を stdout・result artifact の両方に反映する（`contract_update.status` は `applied`/`no_change`/`rebased`/`failed`/`handoff_required` の 5 値を使う。`handoff_required` は `full_rewrite_required` disposition 専用の値であり、`no_change` を full-rewrite-required の意味に流用しない。ImportError・classifier 例外・fresh readback 失敗のような integrity/environment failure は新しい 6 番目の値を追加せず、既存の `status: "failed"` / `disposition: "invalid"` 語彙を reason_code だけ変えて再利用する）。この受信後の orchestrator 手順は Step 0g の「`NEXT_ACTION: issue_editor_required` 受信時」を参照する。#2620 の explicit `human_review_directive` route（priority 3、既存 route を上書きしない）は `decide_rewrite_route.decide_human_review_directive_editor_route()` が SSOT。詳細は `references/loop-state.md` の「explicit trusted human_review_directive の issue_editor_required 拡張（#2620）」参照。

### Step 4.5: 子Issue/follow-up の実体化 (Materialization)

delivery-rollup parent の child materialization gate と、approve 後の follow-up 起票候補は `references/follow-up-materialization.md` を参照する。dedupe は title ではなく `dedupe_key` で行う。

### Step 5: 終了処理 (Termination)

終了条件、`human_escalation` 経路、scope change signal 停止、loop termination table は `references/termination-policy.md` を参照する。

`approved` 終了時、`issue_kind: implementation` の Issue（`impl-review-loop` への handoff を伴う通常実装 Issue）は `LOOP_HANDOFF_RESULT_V1` marker を終了コメントに出力する（形式・routing rules は `references/termination-policy.md#LOOP_HANDOFF_RESULT_V1` 参照。このコメントは audit telemetry のみで Step 1 起動の authority ではない）。出力は `<!-- LOOP_HANDOFF_RESULT_V1 -->` HTML comment と fenced YAML block の 2 要素。**Issue #2435 P0 必須ゲート**: 当該サイクルで hard dependency が確定していた場合（trusted human context / trusted anchor / controlled reframe / #2406 confirmed-hard-predecessor のいずれか）、`LOOP_HANDOFF_RESULT_V1` 投稿・`publish_termination_report.py` 呼び出しの前に `dependency_materializer.py gate`（`scripts/dependency_materializer.py::evaluate_termination_dependency_gate()`）を実行し、`decision: proceed` の場合のみ `approved` 終了を継続する（`block_retryable` は bounded retry、`block_persistent` は `termination_cause: dependency_materialization_blocked` の `human_escalation` とする。詳細は `references/termination-policy.md#Dependency Materialization Gate` を参照）。ゲートが `decision: proceed` を返した後、`issue-refinement-loop` は `run_root_transition()` を呼び出さず（#2740）、`impl-review-loop` の起動・implementation worker の起動・実装用 worktree の作成・production code の実装・commit/push/PR 作成のいずれも行わずにそのまま正常終了する。**`approved` は実装開始の代替承認ではない**（#2740。follow-up materialization / refinement / contract review の完了も実装依頼と同一視しない）。`impl-review-loop` の起動は、ユーザーが実装を明示的に依頼した invocation（例: `/impl-review-loop <N>` の直接呼び出し、または実装依頼を伴う自然文プロンプト）を通じて `impl-review-loop` 自身の Root-Owned Synchronous Entry Transition エントリゲート（`.claude/skills/impl-review-loop/steps/preparation.md`）が `run_root_transition()`（CLI: `root_entry_router.py --issue-number <N> --repo <owner/repo>`、#2272 root-direct 正本）を呼び出したときにのみ行われる（詳細は `references/termination-policy.md`）。

**適用範囲外（`issue_kind: parent`、delivery-rollup parent を含む、#1914）**: `issue_kind: parent` の Issue が `approved` 終了する場合、本 marker は出力しない（`impl-review-loop` への handoff が発生しないため）。plain Markdown の終了要約のみを投稿し、delivery-rollup parent の場合は要約に `Final Gate: not applicable` と reason code を明記する。詳細は `references/termination-policy.md` の `LOOP_HANDOFF_RESULT_V1` セクション（適用範囲節）を SSOT とする — 本ファイルはそれを矛盾なく要約するのみで、独自の無条件規則を持たない。

#### scope_signal_guard 停止時の termination_cause 正規化手順

`scope_signal_guard.triggered=true` かつ `excluded_by_anchor_reframe=false` のとき、orchestrator は以下の手順で termination payload を組み立てる:

1. `decide_next_loop_action.py` の出力から `TERMINATION_CAUSE:` 行を読み取る（`human_judgment_required` が出力される）
2. `termination_cause: human_judgment_required` を termination payload に設定する
3. `BLOCKERS:` 行の値（`scope_signal_guard_triggered`、`scope_signal_guard_reason_code:<code>` 等）を `blockers_summary` に転記する
4. `publish_termination_report.py` に渡す

`scope_signal_guard.reason_code` を `termination_cause` に直接渡してはならない。`VALID_TERMINATION_CAUSES` に含まれない diagnostic code は renderer が reject する（#919 回帰防止）。

詳細は `references/termination-policy.md` の「scope_signal_guard 停止時の termination payload 正規化」セクションを参照する。

## 終了レポート投稿フロー (Termination Report Publish Flow)

終了レポートの GitHub 投稿は `publish_termination_report.py` を経由して行う。

#1873（bounded review loops）: `render_termination_report.py`（`TERMINATION_REPORT_INPUT_V1` を
検証する renderer）は撤去された。orchestrator は `decide_next_loop_action.py` の出力
（`STATUS` / `NEXT_ACTION` / `TERMINATION_CAUSE` / `BLOCKERS`）と loop の経緯から短い
plain markdown の summary を直接組み立て、`--body-file` または stdin で渡す。

```bash
# summary.md に組み立て済みの plain markdown を書いてから渡す
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py \
  --issue-number 42 --repo <owner/repo> --body-file summary.md
```

canonical な approved termination/handoff publish の場合のみ、永続化済みの承認 Issue 本文の
SHA-256 を計算し、`--termination-reason approved --approved-body-sha256 <64-lowercase-hex>` を
渡す。publisher は controlled comment publish 成功後に限り `refinement_approved` を emit し、
自然言語 summary を完了証跡として使用しない。
origin が missing/unbound であることは diagnostic であり、この publish をロールバックしない。

`human_escalation` の summary では、termination cause が未確定の場合 `human_judgment_required`
へ正規化する（詳細は `references/termination-policy.md` の「termination_cause 正規化ルール」）。

`publish_termination_report.py` は以下の責務を持つ:

1. body が空でないことを確認する（空文字列は `empty_body` で fail-closed）
2. `issue_comment.publish` controlled mutation lane（Issue #1633）経由で GitHub issue comment を投稿する（raw `gh issue comment` を直接呼ばない）
3. gh 呼び出しが失敗またはタイムアウトした場合は fail-closed で終了し、reason_code / timestamp をローカル artifact に記録する

詳細な publisher 仕様は `.claude/skills/issue-refinement-loop/scripts/publish_termination_report.py` を参照する。

## ISSUE_EXECUTION_DECISION_V1 ハンドオフ契約 (#1677)

**現在 production 接続済みの契約**（PR #1767 owner review + Scope Delta 反映後、#1873 でコンシューマ集合を更新）: `scripts/validate_issue_execution_decision.py` が standalone canonical module（`validate_schema()` / `validate_semantics()` を明示的に分離、schema-first の `validate_issue_execution_decision()`、`project_issue_execution_decision_ref()`、legacy adapter・migration envelope helper）として存在し、`run_refinement_preflight.py`（downstream consumer）が同一モジュールを import して呼び出す。`plan_refinement_loop.py` は `build_issue_execution_decision()` の producer だが、自身の出力を自己検証しない（#1873: Replay Arbitration 撤去に伴い内部整合性チェックを削除。downstream consumer が独立に検証する）。import 失敗は fail-closed（silent skip 禁止）。

**未接続（follow-up）**: `REFINEMENT_LOOP_PLAN_V1`/`LOOP_STATE_V1`/`LOOP_HANDOFF_RESULT_V1` の required 化（現状は additive/optional）。genuine な `depends_on`/`duplicate`/`absorb` を GitHub native dependency・明示的 duplicate marker・方向付き Machine-Readable Contract から直接構築する結線（現状は `ISSUE_SCOPE_ROLLUP_PLAN_V2` からの derivation を撤去した保守的な `deferred` fallback のみ）。

downstream skill（impl-review-loop・implement-issue・issue-contract-review・open-pr）は `downstream_policy`（`semantic_reclassification: forbidden` / `freshness_validation: required` / `stale_action: rerun_issue_refinement`）に従い、semantic relation を再分類しない。stale 検出時は issue-refinement-loop を再実行する。

**semantic relation の情報源について**: `ISSUE_SCOPE_ROLLUP_PLAN_V2` は coordination plan であり semantic dependency graph ではない。方向未確定の `sequential_required` や `merge_into_current_pr` 等の suggested_action は、depends_on/duplicate/absorb relation へ自動変換しない（誤変換は PR #1767 owner review で指摘され撤去済み）。`proceed_with_coordination` のみ非committal な `coordinates` relation に変換する。それ以外の曖昧な signal は `execution.state: deferred` にして human 判断を要求する。

**Canonical schema の digest provenance / legacy compatibility metadata**（AC10）: `schemas/issue_execution_decision_v1.schema.json` に additive/optional な `provenance`（`policy_version`・producer/collector provenance・`canonicalization_id`・`digests.{source_manifest_sha256,semantic_decision_sha256,artifact_sha256}`・`legacy_compatibility.{legacy_schema_identifiers,supported_consumer_versions}`）と `migration`（`phase: dual_write | equivalence | dual_read | new_authoritative | legacy_removed`・`legacy_digest`・`new_digest`・`equivalence_result`・`producer_version`・`consumer_capability`）ブロックを追加した。`equivalence` phase の digest 不一致は `validate_issue_execution_decision.validate_migration()` が fail-closed で拒否する（AC13）。legacy `graph.nodes/graph.edges` + `execution.target_state/predecessor_issue_numbers/reason_codes` からの adapter は `validate_issue_execution_decision.adapt_legacy_graph_to_v1()` が実装し、canonical output は常に V1 へ正規化する。詳細は `references/refinement-loop-plan-output.md` の `issue_execution_decision` 節を参照する。hard dependency の SSOT は GitHub native `blockedBy`; body prose は mirror/fallback である（Issue #2435 dependency-materialization choke point、`references/dependency-materialization.md` 参照）。

## 参照マップ (Reference Map)

| topic | primary reference |
|---|---|
| native dependency materialization | `references/dependency-materialization.md` — `scripts/dependency_materializer.py` |
| anchor comment schema | `schemas/anchor_comment.schema.json`（Issue #1873: `loop_state.schema.json` から抽出） |
| loop state field definitions（historical） | `references/loop-state.md` |
| anchor comment handling | `references/anchor-comment-handling.md` |
| owner reaction decision（read-only。`_classify_heavy_mutation_gate_with_fresh_owner_reaction()` 経由で `not_planned` category のみ heavy mutation gate へ fresh-subprocess 限定接続済み — `_is_approved_owner_reaction_not_planned_decision()`、#2689） | `scripts/owner_reaction_decision.py` — `references/anchor-comment-handling.md`（#1975） |
| scope signal guard | `references/scope-signal-guard.md` |
| AC/VC reflection | `references/ac-vc-reflection.md` |
| follow-up materialization | `references/follow-up-materialization.md` |
| web research routing | `references/web-research-routing.md` |
| termination policy | `references/termination-policy.md` |
| planner output contract | `references/refinement-loop-plan-output.md` |
| scope rollup preflight | `references/scope-rollup-policy.md` |
| command registry | `scripts/command_registry.py` — `ISSUE_REFINEMENT_COMMAND_REGISTRY_V1` |
| semantic design review (Step 2.5) | `references/semantic-design-review.md` |
| architecture review / design 判断・failure mode の詳細（`derived_design_note`。本 entrypoint と矛盾する場合は本 entrypoint が正本、#1876） | `docs/dev/workflows/issue-refinement-loop-design.md` |

## 安全策 (Guardrails)

- thin entrypoint を維持し、判定ロジックは planner / reviewer / worker の SSOT を consume するだけに留める
- control-plane のみを担当し、Issue/PR mutation や final judgment の一部を SubAgent に委譲しすぎない
- raw anchor comment snapshot を reviewer feedback や title rewrite 入力へ直接流さない
- `WEB_RESEARCH_RESULT_V1` の retry/fallback/attempt log は link-only とし、`#394` の責務へ越境しない
- `max_iterations` 超過時は fail-close する

## スコープ変更時の停止条件 (Scope Change Stop Conditions)

iteration 中に以下が新規追加された場合は `termination_reason: human_escalation` で停止する。

- `## In Scope` に新規の機能領域が追加された
- `## Allowed Paths` に新規ディレクトリや別アーキテクチャ層が追加された
- `## Acceptance Criteria` に新規の低検証可能 AC が追加された

詳細な signal semantics は `references/scope-signal-guard.md` を参照する。

## Out of Scope

- planner script の判定ロジック追加や schema 変更
- `web-researcher` / `gemini-cli-headless-delegation` の retry / fallback / attempt log 設計変更
- `.claude/agents/*.md` の責務移動
- Agent SDK 化

## Verification Commands

```bash
# AC2 / AC5
rg -n "ISSUE_REFINEMENT_LOOP_THIN_ENTRYPOINT_V1|REFINEMENT_LOOP_PLAN_V1|plan_refinement_loop.py|schema validation|fail_closed" .claude/skills/issue-refinement-loop/SKILL.md

# AC3
test "$(wc -l < .claude/skills/issue-refinement-loop/SKILL.md)" -le 500

# AC4 / AC6 / AC10
rg -n "references/anchor-comment-handling\.md|references/web-research-routing\.md|references/follow-up-materialization\.md|references/termination-policy\.md|references/ac-vc-reflection\.md|references/scope-signal-guard\.md" .claude/skills/issue-refinement-loop/SKILL.md
for f in index anchor-comment-handling web-research-routing follow-up-materialization termination-policy ac-vc-reflection scope-signal-guard; do test -f .claude/skills/issue-refinement-loop/references/$f.md; done
rg -nq "\| topic \| file \| loaded_when \| owner \| moved_from \| must_not \|" .claude/skills/issue-refinement-loop/references/index.md

# AC8 / AC9
test -f .claude/skills/issue-refinement-loop/tests/test_thin_entrypoint.py
uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_thin_entrypoint.py -v

# AC7
pnpm typecheck
pnpm lint
pnpm test
pnpm build
uv run --locked pytest .claude/skills/issue-refinement-loop/tests/ -v

# Issue #1507 AC11 / AC23
rg -n "validate_review_compact_output" .claude/skills/issue-refinement-loop/SKILL.md
rg -n "checker_artifact_inconsistency" .claude/skills/issue-refinement-loop/SKILL.md
rg -n "validator 完了前に" .claude/skills/issue-refinement-loop/SKILL.md
```

## 関連資料 (Related)

- `.claude/skills/review-issue/SKILL.md` — レビュー手順の正本
- `.claude/skills/edit-issue/SKILL.md` — 本文編集の正本
- `.claude/skills/gemini-cli-headless-delegation/SKILL.md` — 外部調査委譲の正本
- `docs/dev/agent-skill-boundaries.md` — オーケストレーター境界の設計原則
- `docs/dev/workflow.md` — 開発フロー全体の正本
- `docs/dev/agent-run-report.md` — run report finalize / posting handoff 規約

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドを削らず、人間向け説明・証跡の再掲のみを削減する。
