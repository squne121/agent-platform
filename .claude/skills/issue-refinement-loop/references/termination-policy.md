# Termination Policy（終了ポリシー）

## Loop end conditions（loop 終了条件）

| condition | termination_reason |
|---|---|
| Step 2 returns `approve` AND latest `CONTRACT_REVIEW_RESULT_V1.status == "go"` confirmed | `approved` |
| Step 2 returns `approve` BUT latest `CONTRACT_REVIEW_RESULT_V1.status != "go"` | continue (re-run `issue-contract-review`) |
| Step 2 returns `needs-fix` and `iteration + 1 < max_iterations` | continue to next iteration |
| Step 2 returns `needs-fix` and `iteration + 1 >= max_iterations` | `human_escalation` (with full blocker summary) |
| Any step requires human review | `human_escalation` |
| `final_classification == superseded_by_decision` and close / replacement flow completed | `superseded_by_decision` |

## Contract Hygiene Repair Routing Predicate（契約 hygiene 修復ルーティング述語）

`ISSUE_AUTHOR_RESULT_V1.contract_hygiene_repair_applied` フラグによる iteration accounting ルール。

| `contract_hygiene_repair_applied` | `no_change` guard | routing |
|---|---|---|
| `true` | — | semantic iteration を消費しない。Step 2（reviewer）に戻す（iteration カウントを increment しない） |
| `false` | — | 通常通り iteration カウント（semantic iteration として処理） |
| `true` だが body_sha256 が前回と同一（`no_change`） | 同一 | 同一 lane に戻さない。通常 iteration カウントとして処理（無限ループ防止） |

**重要**: orchestrator は C4/C9 の具体修復知識を持たない。`contract_hygiene_repair_applied: true` フラグのみで routing を判断する。修復の詳細は `edit-issue` skill および `issue-editor` SubAgent の責務。

## Handoff State（引き渡し状態）: `refinement_approved_gate_pending` / `implementation_ready`

`issue-reviewer` が `approve` を返しただけでは `implementation_ready` とならない。  
以下の 2 段階を経て初めて handoff 状態が `implementation_ready` に遷移する:

| handoff 状態 | 条件 |
|---|---|
| `refinement_approved_gate_pending` | `issue-reviewer approve` を受け取ったが、`CONTRACT_REVIEW_RESULT_V1.status == "go"` がまだ確認されていない |
| `implementation_ready` | `approve` かつ `CONTRACT_REVIEW_RESULT_V1.status == "go"` かつ freshness 確認済み（`body_sha256` 一致、またはフォールバックとして `generated_at >= issue.updated_at`） |

**重要制約**: `issue-reviewer approve` のみを根拠に `implementation_ready` を返してはならない。  
`issue-contract-review` の `status: go` 確認なしに `impl-review-loop` へ handoff することは禁止（#561 型 handoff gap の防止）。

```yaml
HANDOFF_STATE_V1:
  refinement_approved_gate_pending:
    description: "issue-reviewer approve 済みだが CONTRACT_REVIEW_RESULT_V1.status == go 未確認"
    allowed_next: [run_issue_contract_review, human_escalation]
    forbidden_next: [impl_review_loop_handoff]
  implementation_ready:
    description: "approve かつ status: go かつ freshness 確認済み"
    allowed_next: [impl_review_loop_handoff]
```

## Final Gate — `CONTRACT_REVIEW_RESULT_V1.status == "go"` 必須

reviewer が `approve` を返しても、最新の `CONTRACT_REVIEW_RESULT_V1.status == "go"` が確認できるまで `approved` 終了としない。

- `approve` 後、`issue-contract-review` を実行し `CONTRACT_REVIEW_RESULT_V1.status == "go"` を確認してから完了とする
- `status: blocked` の場合は `approved` ではなく継続（blocker 解消後に `issue-contract-review` 再実行）とする
- `next_action: human_judgment` の場合は `human_escalation` とする（`CONTRACT_REVIEW_RESULT_V1.status` は `go | blocked` のみ。`human_judgment` は `next_action` フィールドで表現）
- 本ルールは `issue-refinement-loop/SKILL.md` が本ファイルを normative reference として消費するため、SKILL.md を変更せずとも実効性がある

### Final Gate 適用除外（issue_kind: parent + parent_mode: delivery-rollup, #1914）

`issue_kind: parent` かつ `parent_mode: delivery-rollup` の Machine-Readable Contract を持つ Issue（`## Verification Commands` セクションを持たない調査・整理系 delivery-rollup parent）には、本 Final Gate（`CONTRACT_REVIEW_RESULT_V1.status == "go"` 必須化）を適用しない。

- 適用除外の判定は `run_contract_review_once.py` が Step 2（`contract_readiness_check.py` 呼び出し、既存の strict resolver `resolve_existing_issue_validation_profile` による canonical parent 判定）の結果を再利用して行う。新しい YAML parser・allowlist は追加しない
- 適用除外の対象は `parent_mode: delivery-rollup` のみ（保守的な選択）。`quality-gate` / `routing-map` / `decision-log` へは拡大しない
- 適用除外に該当する Issue では、`run_contract_review_once.py` は `baseline_vc_preflight.py` を呼ばずに `CONTRACT_REVIEW_RESULT_V1.status` を `go` として確定する（`blocked` にはしない）
- `issue_kind: parent` かつ `parent_mode` が `delivery-rollup` 以外の場合、および `issue_kind: implementation` の場合は本適用除外の対象外であり、`## Verification Commands` セクションがなければ従来通り `status: blocked`（`VC001_NO_VERIFICATION_COMMANDS_SECTION`）のままとする
- **「Final Gate 非適用」と「Final Gate 成功」は区別する**: 適用除外に該当する delivery-rollup parent の `approved` 終了は「Final Gate 非適用（not applicable）」であり、`## Verification Commands` を実際に検証して `status: go` を得た通常経路の「Final Gate 成功（passed）」とは異なる。両者は終了要約（下記 `LOOP_HANDOFF_RESULT_V1` 節参照）で明確に区別する
- 発端: Issue #1890（delivery-rollup parent、`## Verification Commands` セクションなしを理由に `run_contract_review_once.py` が `status: blocked` を返し、`issue-reviewer approve` にもかかわらず `approved` 終了できなかった事故）に対する OWNER 決定の恒久反映

### implement-issue Handoff Gate（引き渡しゲート）

| `CONTRACT_REVIEW_RESULT_V1` フィールド | handoff 判定 |
|---|---|
| `status: go` | `impl-review-loop` へ handoff 可 |
| `status: blocked` AND `next_action: propose_refinement_loop` | 継続（blocker 解消後に `issue-contract-review` 再実行） |
| `status: blocked` AND `next_action: human_judgment` | `human_escalation` で停止 |

`CONTRACT_REVIEW_RESULT_V1.status` の有効値は `go | blocked`。`human_judgment` は `next_action` フィールドに現れる（`status` フィールドには存在しない）。

### Contract Snapshot Idempotency（スナップショットの冪等性）

- contract-review snapshot comment は Issue body の `body_sha256` を含む
- `body_sha256` が現在の Issue body と一致しない場合（stale result）、その snapshot は無効とする
- stale snapshot を `go` 判定として使用してはならない（`issue-contract-review` を再実行すること）
- Issue body が 1 文字でも変更された場合は `body_sha256` が変化するため、prior snapshot は自動的に stale となる

**Note（policy-only — follow-up 依存）**: `body_sha256` フィールドの producer-side 実装（`issue-contract-review/SKILL.md` の `CONTRACT_REVIEW_RESULT_V1` 出力への追加）は本 Issue のスコープ外。現時点では本セクションは policy constraint として機能し、実装は follow-up Issue で対応する（`issue-contract-review` の out-of-scope 修正として別 Issue を起票すること）。
それまでの間、consumer 側は `CONTRACT_REVIEW_RESULT_V1.generated_at` と Issue の `updated_at` の比較を用いた暫定的な stale 検知を行う。

## Human Escalation on max_iterations（イテレーション上限時のエスカレーション）

`iteration + 1 >= max_iterations` かつ approve なしの場合は `human_escalation` で停止し、全 iteration 分の blocker summary を終了コメントに添付する。`max_iterations=3` 既定では、3 回目の `needs-fix` で停止する。

### termination summary の正規化（normalization、正規化処理、#1873）

#1873: `TERMINATION_REPORT_INPUT_V1` を検証する renderer（`render_termination_report.py`）
は撤去された。orchestrator は以下のルールに従って plain markdown の termination summary
を直接組み立てる（構造化 JSON payload の正規化ではなく、markdown 本文の組み立て規則）:

- `termination_reason: human_escalation` かつ `termination_cause` が未確定の場合、summary
  本文に `Cause: none` を出さず `human_judgment_required` を fallback cause として書く
- `decide_next_loop_action.py` が明示した `TERMINATION_CAUSE`（例: `max_iterations_exceeded`）
  がある場合はそれを使う（fallback で上書きしない）
- blockers は summary 本文に箇条書きで列挙する（`decide_next_loop_action.py` の BLOCKERS 行を
  そのまま反映する）

human_escalation の summary 例（markdown）:

```markdown
## issue-refinement-loop: human_escalation

- Cause: human_judgment_required
- Issue: #829
- Iteration: 3
- Blockers:
  - オーナー判断が必要
  - スコープの矛盾が未解決
```


## scope_signal_guard 停止時の termination payload 正規化

`scope_signal_guard.triggered=true` かつ `excluded_by_anchor_reframe=false` のとき、orchestrator は以下の規則に従って termination payload を組み立てる。

### 呼び出しタイミング前提条件（#1873）

termination payload を組み立てる前に、orchestrator が `decide_next_loop_action.py` を
呼ぶタイミングそのものが hard-stop 対象かどうかを決める（#1873 で
`ISSUE_REFINEMENT_PHASE_STATE_V1` の formal phase-gate は撤去された。判定は
`references/loop-state.md` の「フロー上の位置」表を参照）。

| フロー上の位置 | `decide_next_loop_action.py` を呼ぶか | scope_signal_guard.triggered 時の動作 |
|---|---|---|
| `preflight` / `investigation` | 呼ばない | シグナルは investigation/review へ進む合図として扱われるのみ |
| `review`（pre-rewrite） | 呼ばない | VERDICT に基づき直接ルーティングする |
| rewrite 後 / next-action 決定時 | 呼ぶ | 無条件で `human_escalation` → termination payload を組み立てる |

`decide_next_loop_action.py` は呼ばれた時点で常に hard-stop 判定を行う（phase 概念を
持たない）。呼び出しタイミングの制御は orchestrator の責務である。

### termination_cause 正規化ルール

#1873: `render_termination_report.py`（`TERMINATION_REPORT_INPUT_V1` を検証する
renderer）は撤去された。orchestrator は plain markdown の termination summary を
直接組み立てて `publish_termination_report.py` に渡す（`--body-file` / stdin）。
組み立て時に守る正規化ルールは変わらない:

| 項目 | 使用する値 | 出所 |
|---|---|---|
| termination reason（summary 内の見出し等） | `human_escalation` | 固定 |
| termination cause（summary 本文） | `human_judgment_required` | **常に `human_judgment_required` に正規化する** |
| blockers 要約 | `scope_signal_guard_triggered` / `scope_signal_guard_reason_code:<reason_code>` | `decide_next_loop_action.py` の BLOCKERS 出力 |

**重要**: `scope_signal_guard.reason_code`（例: `new_allowed_path_layer`、`new_in_scope_area`）
は termination cause としてそのまま使用しない（`scope_signal_guard_triggered` という
raw トリガー名自体も cause ではない）。summary 本文には常に正規化済みの
`human_judgment_required` を書き、reason_code は blockers 要約側にのみ残す。

`decide_next_loop_action.py` は `scope_signal_guard.triggered=true` のとき
`TERMINATION_CAUSE: human_judgment_required` を stdout に出力する。orchestrator は
この値を summary の termination cause として使用する。

## `canonical_step2_route: step_5_operator_intervention_required` 発生時の termination payload 正規化（#2397）

`run_root_review_pipeline.route_canonical_step2_result()` が
`canonical_step2_route: step_5_operator_intervention_required` を返した場合
（`status: ok` + `compact_result.verdict: needs-fix` +
`compact_result.next_action: request_changes` かつ verified
`merged_review_result.failure_class == "contract_readiness_human_judgment"`。
上記「canonical Step 2 routing table」節参照）、orchestrator は既存の
`step_5_human_judgment_required` handler をそのまま再利用して停止処理を行う
（新設の別 handler は追加しない）が、以下の点で `step_5_human_judgment_required`
と区別する:

| フィールド | `step_5_human_judgment_required`（genuine semantic / owner ambiguity） | `step_5_operator_intervention_required`（#2397） |
|---|---|---|
| `termination_reason` | `human_escalation` | `human_escalation`（同一） |
| `termination_cause`（summary 本文） | `human_judgment_required` | `operator_intervention_required` |
| 意味 | Issue 本文の書き換えでは解消しない意味論的・owner 判断が必要な状態 | `env_missing_dep` / `timeout` / unknown 分類 / regression gate failure / `package_manager_no_tty_prompt` / validator timeout・internal error・JSON decode error / body retrieval failure など、Issue 本文の書き換えでは解消しない環境・tool・runtime 起因の readiness 状態（`contract_readiness_check.py` の `human_judgment` 分類、exit code 2） |

`termination_cause: operator_intervention_required` は、`termination_reason:
human_escalation` の summary 本文における cause 記述としてのみ現れる
（`termination_reason` 自体を新設の値に変えるものではない）。

**scope_signal_guard の termination_cause 正規化ルールとの独立性**: 上記
「scope_signal_guard 停止時の termination payload 正規化」節は、
`scope_signal_guard.triggered=true` かつ `excluded_by_anchor_reframe=false`
というトリガー経路に対して、summary 本文の termination cause を常に
`human_judgment_required` へ正規化するルールである。`canonical_step2_route:
step_5_operator_intervention_required` は `route_canonical_step2_result()`
起因の別トリガー経路であり、`scope_signal_guard` の正規化ルールの対象外
（scope_signal_guard 側のルールを流用・適用しない）。本 route の
termination_cause は常に `operator_intervention_required` を使用し、
`human_judgment_required` へ正規化してはならない。

## Additional stop rules（追加の停止規則）

- anchor comment fact-check が未完了のまま stale approval を使おうとした場合
- scope change signal が新規追加された場合
- required external research が critical claim を unresolved のまま残した場合

### #2086: trusted operator-selected directive による scope expansion は単独では停止理由にしない

上記「scope change signal が新規追加された場合」は、`scope_signal_guard_decision_v2.
scope_delta_authority.route.action == "contract_update_required"`（`with_human_context`
lane の trusted OWNER/MEMBER/COLLABORATOR directive、`references/anchor-comment-handling.md`
の「Operator-Selected Human-Context 継続」参照）が成立するケースには適用しない。
`decide_next_loop_action.py` はこのケースを `NEXT_ACTION: proceed_with_contract_update`
として優先的に処理し（`scope_signal_guard` hard stop より高優先）、
`termination_reason` を変更しない（loop は継続する）。停止理由として `scope change signal`
が残るのは、evidence が untrusted / ambiguous / conflicting、または destructive・
permission・external-service・issue-split boundary を伴う場合など、
`classify_scope_delta_authority()` が `human_escalation` を返すケースに限られる。

## Must not（禁止事項）

- `approve` 以外を success 扱いして silently finish しない
- `max_iterations` を超えて自動ループしない
- hard stop 条件（`state/needs-human`、scope change 等）をスキップしない

## Termination Result Schema（終了結果スキーマ, LOOP_TERMINATION_RESULT_V1）

`human_escalation` 終了時は以下の構造で終了コメントを出力する:

```yaml
LOOP_TERMINATION_RESULT_V1:
  termination_reason: human_escalation
  max_iterations: 3
  blockers_history:
    - iteration: 0
      blockers: []
    - iteration: 1
      blockers: []
    - iteration: 2
      blockers: []
```

## Termination Comment（終了コメント。全 termination reason 共通）

すべての termination reason（`approved` / `human_escalation` / `superseded_by_decision`）で、終了コメントに `FOLLOW_UP_MATERIALIZATION_RESULT_V1` を含める。follow-up が存在しない場合も空配列で出力する（`follow_up_issues: []` / `note_only_observations: []`）。

```yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: issue-refinement-loop
  follow_up_issues: []   # 起票済み / reuse / skip 結果。空の場合も省略しない
  note_only_observations: []  # 起票せず記録のみ。空の場合も省略しない
```

詳細 schema は `docs/dev/agent-skill-boundaries.md` の `FOLLOW_UP_MATERIALIZATION_RESULT_V1` を参照。`issue-refinement-loop` は thin orchestrator として raw context を保持せず、materialization 結果のみを報告する（`docs/dev/agent-skill-boundaries.md` の `ORCHESTRATOR_IO_BOUNDARY_V1` 参照）。

## Loop Policy（loop 動作方針の定義, LOOP_POLICY_V1）

```yaml
LOOP_POLICY_V1:
  max_iterations_default: 3
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
  routes:
    - when: hard_stop_triggered
      action: human_escalation
    - when: "verdict == 'approve' and contract_review.status == 'go' and contract_review.body_sha256 == issue.body_sha256"
      action: done
    - when: "verdict == 'approve' and contract_review.status != 'go'"
      action: rerun_issue_contract_review
    - when: "contract_review.body_sha256 != issue.body_sha256"
      action: rerun_issue_contract_review
    - when: "verdict == 'needs-fix' and iteration_plus_one < max_iterations"
      action: continue
    - when: "verdict == 'needs-fix' and iteration_plus_one >= max_iterations"
      action: human_escalation
  hard_stops:
    - state/needs-human
    - state/done
    - scope_change_signal
    - contract_malformation
    - required_external_research_unresolved
    - unsafe_mutation
```

## LOOP_HANDOFF_RESULT_V1 — Terminal Contract（終端契約, SSOT）

`issue-refinement-loop` が `approved` 終了するとき、終了コメントに `LOOP_HANDOFF_RESULT_V1` marker を出力する。  
本セクションが `LOOP_HANDOFF_RESULT_V1` の唯一の SSOT である。

**適用範囲（implementation Issue 専用、#1914）**: `LOOP_HANDOFF_RESULT_V1` marker は `issue_kind: implementation` の Issue（`impl-review-loop` への handoff を伴う通常実装 Issue）専用の終端契約であり、`issue_kind: parent`（delivery-rollup parent を含む）の Issue には出力しない。

`issue_kind: parent` かつ `parent_mode: delivery-rollup` の Issue が `approved` 終了する場合:

- `LOOP_HANDOFF_RESULT_V1` marker は出力しない（`impl-review-loop` への handoff は発生しないため）。marker が出力されない以上、marker 内の `status: refinement_approved`（legacy reader compatibility 専用の旧値: `impl_ready`）/ `routing_action: none`（legacy reader compatibility 専用の旧値: `run_impl_review_loop`）フィールドも一切出力されない（#1940 review: marker 非出力の帰結として明示する。#2740: producer 語彙更新後もこの帰結は不変）
- plain Markdown の終了要約（fenced YAML marker を伴わない）のみを投稿する
- 既存の `FOLLOW_UP_MATERIALIZATION_RESULT_V1`（child issue materialization 結果）は変更なく併記する
- 終了要約本文に `Final Gate: not applicable` と reason code（例: `delivery_rollup_parent_without_verification_commands`）を明記し、上記「Final Gate 適用除外」節の「Final Gate 成功」（`status: go` を実際に確認した通常経路）と区別する

### Output Format（出力形式）

出力は HTML comment と fenced YAML block の 2 要素で構成する:

````
<!-- LOOP_HANDOFF_RESULT_V1 -->
```yaml
LOOP_HANDOFF_RESULT_V1:
  status: refinement_approved | human_judgment_required | blocked  # legacy reader-only alias: impl_ready
  ...
```
````

`<!-- LOOP_HANDOFF_RESULT_V1 -->` HTML comment が marker の開始行を示す。  
fenced YAML ブロックが marker の内容を保持する。

### Schema（スキーマ定義。#1873: `schemas/loop_handoff_result_v1.json` の JSON Schema ファイルは撤去済み — 本セクションの YAML 定義が唯一の SSOT）

```yaml
LOOP_HANDOFF_RESULT_V1:
  status: refinement_approved | human_judgment_required | blocked  # refinement_approved は #2740 以降の producer 契約。legacy reader-only alias: impl_ready（新規 producer はもう出力しない）
  routing_action: none | ask_human | blocked  # none は #2740 以降の producer 契約。legacy reader-only alias: run_impl_review_loop（新規 producer はもう出力しない）
  contract_review:
    status: go | blocked          # CONTRACT_REVIEW_RESULT_V1.status を echo（衝突回避のため separated）
    gate_result: fresh_go | missing_go | stale_go | invalidated_by_request_changes | blocked
    latest_comment_url: string
    generated_at: ISO-8601
    body_sha256: string
  metadata:
    title_prefix_ready: bool
  label_sync_observation:
    # #2084: presentation_sync — non-authoritative telemetry only. GitHub
    # Issue labels are presentation-only metadata (SSOT: docs/dev/workflow.md,
    # docs/dev/github-ops.md) and MUST NOT participate in refinement_approved
    # (legacy reader-only alias: impl_ready) / status / routing_action
    # authority. This block records the
    # best-effort presentation sync outcome for observability only.
    kind: presentation_sync
    result: applied | noop | failed
    detail: string
  auto_fixes:
    result: auto_fixed | human_judgment_required | blocked
    required:
      - kind: template_hygiene | metadata_hygiene | known_marker_fix | stale_state_label_cleanup | contract_snapshot_materialization
        executor: implementation-worker
        result: applied | skipped | failed
        evidence:
          before: string
          after: string
          comment_url: string
    skipped: []
  blockers:
    - kind: string
      description: string
  permissions:
    unavailable: []
  generated_at: ISO-8601
  # --- AC11 フィールド（SSOT: 本セクション） ---
  # 以下 4 フィールドは issue-contract-review の終了チェックスクリプトが生成・消費する。
  # この 4 フィールド + attempt counter に対する runtime enforcement（max_rewrite_attempts /
  # no-progress detection）は #664 で decide_rewrite_route.py として実装済み。下記
  # 「Rewrite Loop Runtime Router」セクションが orchestrator invocation 手順の SSOT。
  checked_body_sha256: string    # チェック対象の Issue body の SHA-256 ハッシュ
  checker_exit_code: int         # チェックスクリプトの終了コード（0: pass, 1: fail）
  missing_sections: []           # 不足しているセクション名のリスト（pass 時は空）
  missing_contract_keys: []      # 不足している contract キーのリスト（pass 時は空）
```

**Note**: 上記 4 フィールド（`checked_body_sha256` / `checker_exit_code` / `missing_sections` / `missing_contract_keys`）はスキーマの SSOT として本セクションが定義する。これら 4 フィールド + attempt counter に対する runtime enforcement（`max_rewrite_attempts` 制限・no-progress detection）は #664 で `decide_rewrite_route.py` として実装済みであり、その orchestrator からの invocation 手順は直下の「Rewrite Loop Runtime Router」セクションが normative SSOT となる。

### producer 語彙更新（#2740）

`issue-refinement-loop` が `approved` 終了するとき、`LOOP_HANDOFF_RESULT_V1` の新規 producer 値は `status: refinement_approved` / `routing_action: none`（または同義の非-dispatch値）である。`status: refinement_approved` は「`issue-refinement-loop` が `approved` で終了した」という audit telemetry を表すのみであり、`impl-review-loop` Step 1 の implementation dispatch を authorize しない（#2740、本セクション冒頭「適用範囲」節および `## Root-Owned Synchronous Entry Transition` 節参照）。旧値 `status: impl_ready` / `routing_action: run_impl_review_loop` は legacy reader compatibility のためだけに残り、新規 producer はもう出力しない。既存の readiness-invariant 前提条件（`contract_review.gate_result == fresh_go` / hygiene auto-fix evidence 完備 / `blockers` 空）は `refinement_approved` 発行条件としてそのまま維持する。

**Task Context workflow-signal との別概念であることの明示**: `scripts/task-context/task_context_workflow_signals.py`（Issue #2565 由来）が発行する Task Context の `signal_kind: "refinement_approved"` と、本 `LOOP_HANDOFF_RESULT_V1.status: refinement_approved` は、同じ文字列 `refinement_approved` を共有するが**別スキーマ・別概念**である。前者は Task Context 側の workflow-signal パイプライン（`publish_termination_report.py::_emit_refinement_approved_signal()`）が発行する human-history 相当の signal であり、後者は本ファイルが定義する `LOOP_HANDOFF_RESULT_V1` marker のフィールド値である。両者は独立に発行され、一方の値が他方の authority や producer 契約を変更しない（本 Issue #2740 は Task Context workflow-signal pipeline 自体を変更しない — Out of Scope）。

### Rewrite Loop Runtime Router（リライトループの実行時ルーター, #664 / #814）

Step 4（Rewrite）の rewrite ループにおいて、orchestrator は **checker を実行するたびに** `decide_rewrite_route.py` を呼び出し、その出力に従って routing する。これにより `max_rewrite_attempts` 超過・body hash 変化なし・missing set の単調減少なしを runtime で確定的に強制する（planner payload の値を宣言するだけでなく、実経路で enforcement する）。

#### checker approve が全 stop guard より優先される（#814 AC1）

`decide_rewrite_route` の判定優先順は以下の通り:

1. `checker_exit_code == 0` → **即座に `proceed_to_review / checker_passed`**（`max_rewrite_attempts` 超過・`body_hash_unchanged`・`missing_contract_no_progress` よりも優先）
2. `checker_exit_code != 0` かつ `rewrite_attempt_count >= max_rewrite_attempts` → `human_judgment_required / max_attempts_exceeded`
3. `checker_exit_code != 0` かつ body hash 変化なし → `human_judgment_required / body_hash_unchanged`
4. `checker_exit_code != 0` かつ missing set が strictly decrease しない → `human_judgment_required / missing_contract_no_progress`
5. `checker_exit_code != 0` かつ budget 内 → `continue_rewrite / checker_failed_rewrite`

checker が approve（exit 0）を返した場合、budget 超過中であっても・body hash が変化していなくても **`proceed_to_review`** を返す。

#### wrapper スクリプト（`route_after_rewrite.py`）

Step 4 の各反復で `route_after_rewrite.py` wrapper を呼び出すことで手順 1〜6 を一括実行できる。

```bash
uv run python3 .claude/skills/issue-refinement-loop/scripts/route_after_rewrite.py \
  --issue <ISSUE_NUMBER> \
  --repo <OWNER/REPO> \
  --state-path <path/to/state.json> \
  --max-rewrite-attempts <N>
```

wrapper は checker stdout JSON のみを parse し、stderr を JSON に混入しない（AC4）。
checker exit 1（needs-fix）はインフラ障害でなく正常系として routing に渡す（AC4b）。
`LOOP_REWRITE_ROUTER_STATE_V1` schema allowlist 外のキーを router state に混入しない（AC4c）。
`load_rewrite_router_state()` / `save_rewrite_router_state()` を使い attempt counter を silent reset しない（AC4d）。

**invocation 手順**（直接 `decide_rewrite_route.py` を呼ぶ場合）:

1. **persisted state の復元（replay-safe）**: `load_rewrite_router_state(state_path, current_source_body_sha256)` で前回の attempt counter / `previous_*` を復元する。
   - file 不在 → `None`（attempt 0 で新規開始）
   - 破損 / schema 違反 → `RewriteRouterStateError`（fail-closed。attempt counter を **silent reset しない**）
   - source issue body が人間により変更（sha 不一致）→ `source_body_reset: true` の reset state（attempt 0）。reset 事実は route result に残る
2. **checker を実行**して当該反復の `checked_body_sha256` / `checker_exit_code` / `missing_sections` / `missing_contract_keys` を得る。
3. **attempt counter を increment** し、復元した state と当該反復の checker 結果を合成して `LOOP_REWRITE_ROUTER_STATE_V1` を組み立てる（前回の missing set は `previous_missing_*` に入れる）。
4. **router を呼ぶ**:

   ```bash
   echo '<LOOP_REWRITE_ROUTER_STATE_V1 JSON>' | \
     python3 .claude/skills/issue-refinement-loop/scripts/decide_rewrite_route.py
   ```

   exit 0 で `RouteResult` JSON（`route` / `reason_code` / 端末フィールド）を返す。exit 2 は schema 違反入力（fail-closed）。
5. **route に従って分岐**:

   | `route` | orchestrator のアクション |
   |---|---|
   | `continue_rewrite` | issue-editor に rewrite を委譲（Step 4 継続）。次反復へ |
   | `proceed_to_review` | rewrite ループを抜けて Review / handoff 判定へ進む |
   | `human_judgment_required` | `termination_reason: human_escalation` で停止。`reason_code`（`max_attempts_exceeded` / `body_hash_unchanged` / `missing_contract_no_progress`）を終了コメントに添付 |

6. **state を永続化**: `save_rewrite_router_state(state, state_path)` で attempt counter を atomic write（tmp + fsync + `os.replace`）で保存する。これにより session 再起動 / CI rerun を跨いで attempt counter が 0 に戻らない。

**routing の正準性**: rewrite ループの停止判断は `decide_rewrite_route` の `route` を SSOT とする。orchestrator は prose で attempt 数や no-progress を再判定しない（thin entrypoint 原則）。`route: human_judgment_required` は本ファイルの `human_escalation` 経路と連動する。

### `refinement_approved` 定義（legacy reader-only alias: `impl_ready`）

`status: refinement_approved`（legacy reader-only alias: `impl_ready`。新規 producer はもう `impl_ready` を出力しない、#2740）を出力できるのは以下のすべてが真のときのみ:

1. `contract_review.gate_result == fresh_go` — 最新の `CONTRACT_REVIEW_RESULT_V1.status == "go"` が存在し、現 Issue body hash に対して fresh
2. `contract_review.status == go` が後続の `request_changes` / `blocked` により無効化されていない
3. `metadata.title_prefix_ready == true` または `auto_fixes.required` に `metadata_hygiene` / `template_hygiene` の `result: applied` エントリが存在する
4. `auto_fixes.required` が空（または全 applied 済み）かつ `auto_fixes.skipped` が空
5. `blockers` が空
6. `routing_action == none`（legacy reader-only alias: `run_impl_review_loop`）

**Title prefix 不在のみを理由に `refinement_approved` を拒否してはならない** — implementation-worker (repair mode) が auto-fix evidence を添付していれば `refinement_approved` は許可される。`phase/implementation` label（および他の presentation label）の有無は `refinement_approved` 判定条件から除外する（#2084）。`label_sync_observation`（presentation_sync）の `result`（`applied | noop | failed`）を変えても `status` / `routing_action` / `implementation_allowed` は変化しない。`auto_fixes.required` に triage label 遷移（`triage-required` remove、`phase/implementation` / `agent/implementer` add）を追加してはならない — それらは `label_sync_observation` として readiness decision 後に best-effort で実行される非authoritative な presentation sync である。

`auto_fixes.required` / `auto_fixes.skipped` の各エントリは `kind` / `executor` / `result` / `evidence`（`before` / `after` / `comment_url`）を含む。`result: skipped` または `evidence` 欠如 → `refinement_approved` 禁止。

**注（#2740）**: `status: refinement_approved` は `issue-refinement-loop` が `approved` 終了したという audit telemetry を表すのみであり、単独で `impl-review-loop` Step 1 の implementation dispatch を authorize しない。`impl_ready` / `run_impl_review_loop` の旧語彙は legacy reader compatibility（過去に投稿済みの comment を読む consumer）のためだけに残る。

### Routing Rules（ルーティング規則）

| 条件 | `status` | `routing_action` |
|---|---|---|
| 全 invariant 満足（上記 1〜6） | `refinement_approved`（legacy: `impl_ready`） | `none`（legacy: `run_impl_review_loop`） |
| `contract_review.gate_result` が `missing_go` / `stale_go` | `blocked` | `blocked` |
| `request_changes` / `blocked` が `go` を後続で無効化 | `blocked` | `blocked` |
| scope / goal / AC に semantic change が検出された | `human_judgment_required` | `ask_human` |
| `blockers` に 1 件以上 | `blocked` | `blocked` |
| fixer unavailable かつ title prefix 不在 | `human_judgment_required` | `ask_human` |
| `auto_fixes.skipped` に 1 件以上 | `human_judgment_required` | `ask_human` |

### `human_judgment_required` 停止条件

scope / goal / AC への semantic change が検出されたとき、`issue-refinement-loop` は `LOOP_HANDOFF_RESULT_V1.status: human_judgment_required` / `routing_action: ask_human` で停止し、人間の判断を仰ぐ。Semantic change の検出は `references/scope-signal-guard.md` の guard 定義を参照する。

### Hygiene Delegation Contract（委譲契約, routing 定義のみ）

以下の 5 種 hygiene は implementation-worker (repair mode) に委譲する:

| kind | 委譲先 | 委譲条件 |
|---|---|---|
| `template_hygiene` | implementation-worker (repair mode) | 既定テンプレートセクション欠落 |
| `metadata_hygiene` | implementation-worker (repair mode) | title prefix 不在（phase label は presentation_sync として別扱い、#2084） |
| `known_marker_fix` | implementation-worker (repair mode) | 既知の壊れた marker 形式を検出 |
| `stale_state_label_cleanup` | implementation-worker (repair mode) | stale `state/blocked` / `state/queued` を検出 |
| `contract_snapshot_materialization` | implementation-worker (repair mode) | contract snapshot comment 未作成 |

各委譲は `auto_fixes.required` エントリとして記録し、`result: applied` かつ `evidence` 完備のものだけが `refinement_approved`（legacy: `impl_ready`）に貢献する。


## Root-Owned Synchronous Entry Transition（root が単独で所有する同期的な実装着手への遷移経路、#2272 正本）

`issue-refinement-loop` の `approved` 終了（`LOOP_HANDOFF_RESULT_V1.status: refinement_approved`、
legacy reader-only alias: `impl_ready`）は `impl-review-loop` Step 1 起動の **authority ではない**
（#2740）。root/main thread が Issue review 開始から `impl-review-loop` 起動判断までを単一の連続した control flow
として実行する invocation（同一 root invocation）の中でのみ、以下の process-local な
戻り値 `ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1` を生成・消費する。durable authorization
packet ではなく、保存・再ロード用の API を持たない。GitHub comment・artifact・
digest・invocation ID のいずれも単独では Step 1 起動を authorize しない。

```yaml
ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1:
  route: invoke_impl_review_loop | rerun_contract_review | rerun_base_preflight | stop
  reason:
  issue_number:
  reviewed_body_sha256:
  observed_live_body_sha256:
  reviewed_base_sha:
  observed_base_sha:
  resume_from:
  retry_count:
```

### Routing 優先順位

| 優先度 | 条件 | route |
|---|---|---|
| 1 | capability／live fetch／identity が検証不能 | stop |
| 2 | Issue body または Allowed Paths drift | rerun_contract_review |
| 3 | base SHA のみ drift | rerun_base_preflight |
| 4 | verdict が blocked / request_changes | stop |
| 5 | current-run go かつ live equality 成立 | invoke_impl_review_loop |

数値が小さい優先度を優先する。複数条件が同時成立する場合は最小番号の route を採用する。

body／Allowed Paths drift（review subject 自体の変更）は常に full `issue-contract-review`
を再実行する（`rerun_contract_review`）。base SHA drift のみは `rerun_base_preflight`
route で exact base SHA を pin し、その SHA から worktree を作成する。
`rerun_base_preflight` の再試行上限は 3 回（`MAX_BASE_PREFLIGHT_RETRIES`）。上限到達後も
drift が解消しない場合は `route: stop` / `reason: base_preflight_retry_exhausted` を
terminal result として返す。

### production carrier（送り手・受け手・呼び出し口を固定する運搬経路の定義、root-direct 再設計）

OWNER REQUEST_CHANGES
（https://github.com/squne121/loop-protocol/pull/2282#issuecomment-5371853364 ）
を受け、producer/consumer を別プロセス・別 script に分離し `invocation_token` の
再提示で authorize する方式は撤回した。root/main thread は単一の継続した invocation
（in-process 呼び出しなら同一 call stack、CLI subprocess 経由なら同一 continuous
turn — 後者は subprocess がこの turn の tool call を直接駆動できないための区別。
下記 delivery 参照）の中で以下を自ら実行する（`.claude/skills/issue-refinement-loop/scripts/root_entry_router.py`
の `run_root_transition()`）。

```yaml
carrier:
  entry_point: .claude/skills/issue-refinement-loop/scripts/root_entry_router.py::run_root_transition
  encoding: strict JSON（CLI 経由で呼ぶ場合の stdout。in-process 呼び出しの場合は plain dict）
  delivery: 同一 call stack 内の直接関数呼出し。プロセス境界を越える必要がある場合
    （例: root/main thread から独立した CLI 実行）も、CLI の標準出力は SAME
    continuous turn 内で直ちに読み取られ次の判断に使われるのみで、再提示可能な
    形で保存・再利用されない。
  forbidden:
    - GitHub comment discovery
    - ambient environment lookup
    - stale artifact lookup
    - transcript scraping
    - re-presentable authorization token（invocation_token 相当のもの一切）
```

`run_root_transition()` は以下を1回の呼び出しの中で順に実行する: capability
preflight → live Issue fetch → 同一呼び出し内での current-run
`issue-contract-review`（既存 `run_once()` を関数として直接呼び出す。呼び出し元が
`review_verdict` / `reviewed_body_sha256` / `reviewed_base_sha` を供給することは
できない — これらのパラメータは公開 API に存在しない）→ 直後の live 再取得 →
`decide_root_entry_route()` によるルーティング決定（base drift のみ、関数内部の
ループでの bounded retry。retry_count は呼び出し元へ公開されない local state） →
route が `invoke_impl_review_loop` の場合のみ、同じ呼び出しの中で
`invoke_step1()` コールバックを直接実行する。

### root invocation の識別（プロセスローカルな相関用ラベルであり認可根拠ではない）

payload 内に自己申告の `root_invocation_id` を含めない。host から `prompt_id` が
取得可能な場合は audit ログ相関にのみ利用し（`generate_root_invocation_nonce(prompt_id)`）、
取得不能な場合は process-local nonce を生成する（`generate_root_invocation_nonce()`）。
この nonce / prompt_id 由来の correlation id は **いかなる authorization 判断にも
関与しない**（比較・照合されることが一切ない）。nonce を payload や audit comment
から再取得することもない。process restart 後は nonce を復元せず、常に fresh
review（capability preflight → live fetch → current-run review）からやり直す。

`ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1` を受理する別 consumer は存在しない。
GitHub コメントのみから再構成された route や、process 再開後に古い route を
再利用しようとする試みは、`run_root_transition()` が route/envelope を一切
外部入力として受理しない設計そのものによって構造的に不可能である。

### fingerprint の再利用範囲（AC13 の明確化）

既存 `issue-contract-review` の comment-based fingerprint validator
（`contract_source_kind == issue_comment` を要求し、実 comment ID・trusted author
provenance を検証する既存 parser）は、本節の direct result には適用しない。
`reviewed_body_sha256` と `observed_live_body_sha256`、`reviewed_base_sha` と
`observed_base_sha` を直接比較する live equality check として実装する。SHA256
算出・正規化方式は既存 `issue-contract-review` の `contract_body_sha256` /
`allowed_paths_normalized_sha256` と同一の canonicalization ロジック
（`"sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()`）を独立実装として
流用するが、comment provenance 検証は要求しない。既存 comment-based fingerprint
parser 本体（`.claude/skills/issue-contract-review/**`）は変更対象に含めない。

### audit comment の位置付け（非 authoritative）

`impl-review-loop` 起動判断（`route` の決定）は GitHub audit comment の publish
成否を条件にしない。audit comment publish は route 決定後の best-effort 処理とし、
失敗時は warning として記録するのみで route を変更しない
（`publish_audit_comment_best_effort()`）。restart 時は audit comment を authority
として扱わず、常に fresh review からやり直す。

## Dependency Materialization Gate（終了前必須ゲート、Issue #2435 P0 — OWNER レビュー PR #2447 反映）

`issue-refinement-loop` の Step 5 が `approved` 終了（`LOOP_HANDOFF_RESULT_V1` marker の投稿
および `publish_termination_report.py` の呼び出し）を行う **前に**、当該サイクルで hard
dependency が確定していた場合（trusted human context / trusted anchor / controlled reframe /
#2406 confirmed-hard-predecessor のいずれか）は、以下の gate を必ず実行する。

Issue #2435 は `dependency_materializer.py` の producer/materializer choke point を追加した
が、当初の実装はこの Step 5 経路のどこからも呼ばれていなかった（OWNER REQUEST_CHANGES,
https://github.com/squne121/loop-protocol/pull/2447#issuecomment-5468370232 ）。choke point
自体が存在しても実際の終了経路が一度も呼ばなければ、#2424 型の body-only false-green は merge
後も再発可能なままだった。本節はその欠落配線を Step 5 手順として確定する。

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/dependency_materializer.py gate \
  --target-issue <ISSUE_NUMBER> --repo <owner/repo> \
  --current-decision-file <path/to/current_ISSUE_EXECUTION_DECISION_V1.json> \
  [--previous-decision-file <path/to/previous_ISSUE_EXECUTION_DECISION_V1.json>] \
  --body-file <path/to/current_issue_body.md>
```

`--current-decision-file` は当該サイクルで確定した `ISSUE_EXECUTION_DECISION_V1`（本ファイル
上部「ISSUE_EXECUTION_DECISION_V1 ハンドオフ契約」節、SKILL.md 参照）をそのまま渡す。
`--previous-decision-file` はこの loop の前回 iteration で確定した decision snapshot があれば
渡す（初回確定時は省略してよい）。`TERMINATION_DEPENDENCY_GATE_RESULT_V1` の schema・decision
定義・body-only false-green との統合は `references/dependency-materialization.md` の
「Step 5 termination gate」節を SSOT とする。

### Routing（gate の decision に応じた Step 5 分岐）

| `decision` | Step 5 の扱い |
|---|---|
| `proceed` | 確定済み hard dependency が存在しない、または native materialization が独立検証済み。通常どおり `approved` 終了（`LOOP_HANDOFF_RESULT_V1` 投稿 + `publish_termination_report.py`）を継続する |
| `block_retryable` | `auth-or-environment-failure` / `controlled-executor-failure` の一時的障害。bounded retry（gate を再実行）する。この decision 単独を理由に `human_escalation` へは倒さない |
| `block_persistent` | `readback-mismatch` / `semantic-human-judgment-required` / `native-capability-unavailable`、または body-only false-green の検出。`approved` 終了を投稿してはならない。`termination_reason: human_escalation` / `termination_cause: dependency_materialization_blocked` として停止し、gate の `reason` / `materialization_result.failure_class` / `body_only_reason` を blockers 要約に含める |

`termination_cause: dependency_materialization_blocked` は、上記「scope_signal_guard 停止時の
termination payload 正規化」節・「`canonical_step2_route: step_5_operator_intervention_required`」
節のいずれの正規化ルールの対象にもならない、独立した trigger 経路である（本節が単独の SSOT）。

## Termination Summary Publish Flow（終了サマリー投稿フロー, #1873）

### 新規 human-history のみ（Issue #1908）

`issue-refinement-loop` が review-complete を得た時は、existing machine-readable termination
summary を変更せず、source Issue を target とする new human-history を一件 reconcile する。
`loop_kind: issue-refinement-loop`、`phase: review-complete`、reason は `completed` / `needs_fix` /
`human_judgment` に限定し、review phase 直前の source Issue body SHA-256（prefix なし）を
`reviewed_ref` にする。`publish_termination_report.py::publish_human_history()` に public-safe
日本語の実施内容・推奨 action/reason/impact・evidence refs を渡す。stable marker は RFC 8785 JCS
identity の SHA-256 であり、同じ identity は create / PATCH / noop のいずれでも一 event と数える。
controlled readback が失敗した場合は成功扱いにせず diagnostic failure とする。

この scoped quality rule は本 Issue が生成する new human-history だけに適用する。PR/Issue 全体を
finalize する仕組み、finalizer SubAgent、新 publisher / ledger / run-id store / hook は導入しない。


#1873（bounded review loops）で `render_termination_report.py`（`TERMINATION_REPORT_INPUT_V1` ->
`TERMINATION_REPORT_RENDER_RESULT_V1` の renderer/validator パイプライン、attempt/guard/
dynamic-fence を含む）は撤去された。orchestrator は次の手順で終了時のコメントを投稿する。

1. orchestrator が `decide_next_loop_action.py` の出力（`STATUS` / `NEXT_ACTION` /
   `TERMINATION_CAUSE` / `BLOCKERS`）と loop の経緯から、短い plain markdown の summary
   を直接組み立てる（見出し・termination reason/cause・issue 番号・iteration・
   blockers の箇条書き程度の最小構成。テンプレート/guard/attempt ロジックは持たない）。
2. `publish_termination_report.py`（`--issue-number` / `--repo` / `--body-file` または stdin）
   に summary をそのまま渡す。この script は body の空チェックのみ行い（`empty_body` で
   fail-closed）、`issue_comment.publish` controlled mutation lane（Issue #1633）経由で
   投稿する。raw `gh issue comment` を直接呼ぶことはない。
3. `scope_signal_guard.triggered=true` の termination では、`scope_signal_guard_route` /
   `missing_approval_field` / `suggested_contract_patch`（#1090 AC6、`scope_signal_guard_
   decision_v2.scope_delta_approval` 由来）を summary の blockers 箇条書きに含める。
   `scope_signal_guard_decision_v2` は orchestrator が `plan_refinement_loop.py` の出力
   からそのまま抽出し、summary 組み立て時に自ら参照する（`decide_next_loop_action.py`
   への sidecar 引数としての用途とは別に、summary 本文の材料として使う）。

markdown 本文には引き続き `` ``` `` を含む blockers 等が構造を破壊しないよう配慮すること
（動的 fence 生成のような自動防御機構はないため、orchestrator 自身が内容を検証する）。
