# REFINEMENT_LOOP_PLAN_V1 出力ガイド

## 概要

`plan_refinement_loop.py` は Issue 本文、コメント、既知コンテキストを解析し、次の判断を表す決定論的な JSON plan を返します。

- 調査ステップが必要かどうか
- 外部仕様の検証が必要かどうか
- scope signal guard が発火しているかどうか
- delivery rollup の child materialization が残っているかどうか
- out-of-scope 作業を follow-up issue 候補として残すかどうか

## 入力 (`REFINEMENT_LOOP_PLANNER_INPUT_V1`)

```json
{
  "schema_version": "refinement_loop_planner_input/v1",
  "issue": {
    "number": 123,
    "title": "Issue title",
    "body": "Full markdown body",
    "labels": ["label1", "label2"]
  },
  "comments": null,
  "known_context": {
    "anchor_comment_url": "https://github.com/...",
    "parent_mode": "delivery-rollup",
    "closure_mode": "child-complete"
  }
}
```

## 出力 (`REFINEMENT_LOOP_PLAN_V1`)

```json
{
  "schema_version": "refinement_loop_plan/v1",
  "source": {
    "issue_number": 123,
    "issue_body_sha256": "...",
    "comments_sha256": null,
    "known_context_sha256": null,
    "generated_at": "2026-05-25T14:22:00Z"
  },
  "decisions": {
    "investigation_policy": {
      "required": true,
      "reason_code": "target_paths_present",
      "target_paths": [
        ".claude/skills/test/script.py",
        "src/components/Component.ts",
        "docs/dev/test.md"
      ],
      "repo_claims": ["Script command", "Path reference"],
      "evidence_spans": [
        {
          "source": "issue_body",
          "source_ref": null,
          "start_line": 1,
          "end_line": 50,
          "text_sha256": "..."
        }
      ],
      "confidence": "deterministic"
    },
    "web_research_policy": {
      "required": false,
      "reason_code": "no_critical_external_claim",
      "critical_external_claims": [],
      "evidence_spans": [],
      "confidence": "unknown"
    },
    "scope_signal_guard": {
      "triggered": false,
      "reason_code": "no_scope_signal",
      "excluded_by_anchor_reframe": false,
      "evidence_spans": []
    },
    "delivery_rollup": {
      "applicable": false,
      "unmaterialized_slots": [],
      "evidence_spans": []
    },
    "follow_up_materialization": {
      "candidates": []
    }
  },
  "fail_closed": {
    "required": false,
    "reason_codes": [],
    "human_message": ""
  }
}
```

## 各 decision field の意味

### `investigation_policy`

- `required`: codebase fact-checking が必要なら `true`
- `reason_code`: 調査が必要になった理由。不要な場合は `no_repo_fact_claim`
- `target_paths`: Outcome / In Scope / AC / VC から抽出した path
- `repo_claims`: repo 事実を主張している text span
- `evidence_spans`: Issue 本文やコメント内の根拠位置
- `confidence`: 判定が明確なら `deterministic`、曖昧なら `unknown`

`SKILL.md` ではこの値を `LOOP_STATE.investigation_policy` に反映し、`required == true` のときに Step 1 (`codebase-investigator`) を起動します。

### `web_research_policy`

- `required`: 外部仕様の検証が必要なら `true`
- `reason_code`: なぜ外部調査が必要かを表す理由コード
- `critical_external_claims`: 外部システムに関する主張を抽出した一覧。各 object は
  `{claim, affects, source_hint, role}` を持ち、`role` は `dispositive` または
  `non_dispositive`。planner は fail-closed の既定値 `dispositive` を出力し、Step 1
  の repository investigation が requested disposition を独立に決定した場合のみ、
  consumer が `non_dispositive` として join できる。
- `evidence_spans`: 根拠位置
- `confidence`: `deterministic` または `unknown`

`SKILL.md` ではこの値を `LOOP_STATE.web_research_policy` に反映し、`required == true` のときに Step 1b (`web-researcher`) を起動します。Step 1 の repository decision と Step 1b result の合流は `web_research.route` consumer が行い、transport failure を semantic verdict と混同しません。

### `scope_signal_guard`

- `triggered`: 新しい scope signal が検出されたら `true`
- `excluded_by_anchor_reframe`: anchor comment による reframe で signal が除外される場合は `true`
- `reason_code`: signal の種類。除外時は `anchor_reframe_exclusion`
- `evidence_spans`: delta-based provenance を保持します。現在は `source: known_context` に加えて、`body_version`、`coordinate_space: body_absolute_1_based`、対応する `source_ref` を含めます。

`SKILL.md` では、発火していて anchor で除外されていない場合に human escalation 候補として扱います。

### `delivery_rollup`

- `applicable`: parent issue に未 materialize の child slot があるとき `true`
- `unmaterialized_slots`: `{child_title_hint, marker, body_line}` の配列
- `marker`: `未起票`、`unmaterialized`、`TBD` などの slot marker

`SKILL.md` では child issue materialization の追跡に使います。

### `follow_up_materialization`

- `candidates`: out-of-scope work を follow-up issue 化できる候補一覧
- 各候補は `{dedupe_key, summary, source_evidence}` を持ちます
- `dedupe_key` は `sha256(summary)` の先頭 16 文字で、重複候補の統合に使います

`SKILL.md` では post-approval comment や補助ドキュメントで候補を参照します。

## `fail_closed` の扱い

`fail_closed.required == true` の場合は、planner が構造的な問題を検出しています。

1. malformed contract や Outcome 欠落など、plan をそのまま判断に使えない状態です。
2. 出力自体は valid JSON ですが、decision の根拠として使ってはいけません。
3. `fail_closed.reason_codes` と `human_message` を添えて human escalation が必要です。
4. orchestrator は不足している policy を推測して補完してはいけません。
5. `fail_closed.rewrite_constraints` に含まれる `FAIL_CLOSED_REWRITE_CONSTRAINTS_V1` を Rewrite ルートへそのまま forward する必要があります。

### `reason_codes`

- `malformed_machine_readable_contract`: YAML block に `contract_schema_version` がない
- `missing_required_section`: Outcome などの重要 section がない
- `missing_required_contract_key`: Machine-Readable Contract に必須 key がない
- `missing_scope_signal_delta_input`: hard-stop phase で immutable baseline 付き `scope_signal_delta_input` を供給できなかった
- `unknown_input_schema`: 入力が `REFINEMENT_LOOP_PLANNER_INPUT_V1` に一致しない
- `planner_internal_error`: planner 実行中に予期しない例外が起きた
- `unknown_issue_kind`: `issue_kind` が SSOT allowlist にない
- `issue_kind_policy_load_error`: ISSUE_KIND_POLICY_V1 を読めなかった
- `contract_schema_parse_error`: Machine-Readable Contract YAML を parse できなかった
- `template_resolution_error`: Issue template を解決または読込できなかった
- `checker_internal_error`: contract checker 側の内部エラー

## `FAIL_CLOSED_REWRITE_CONSTRAINTS_V1`

`fail_closed.required == true` のとき、planner は `fail_closed.rewrite_constraints` に次の schema を含めます。

```json
{
  "schema_version": "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1",
  "required_sections": ["Outcome", "Acceptance Criteria"],
  "required_contract_keys": ["contract_schema_version", "issue_kind"],
  "rewrite_constraints": {
    "must_add_sections": ["Outcome", "Acceptance Criteria"],
    "must_add_contract_keys": ["contract_schema_version", "issue_kind"],
    "freeform_rewrite_forbidden": true
  },
  "override_policy": {
    "allowed_reason_codes": ["missing_required_section", "missing_required_contract_key"],
    "never_override_reason_codes": [
      "unknown_issue_kind",
      "issue_kind_policy_load_error",
      "contract_schema_parse_error",
      "template_resolution_error",
      "checker_internal_error"
    ],
    "overridable_in_current_result": ["missing_required_section"],
    "non_overridable_in_current_result": []
  },
  "max_rewrite_attempts": 2,
  "no_progress_route": "human_judgment_required"
}
```

### field semantics（各 field の意味）

以下は各 field の実務上の意味づけです。

- `required_sections`: Issue 本文に追加しなければならない section
- `required_contract_keys`: Machine-Readable Contract block に必須の key
- `rewrite_constraints.freeform_rewrite_forbidden`: 既存 Issue 更新専用の `issue-editor` は自由記述の全面改稿を受け入れてはいけません
- `override_policy.allowed_reason_codes`: `human_decision_reframe` が override できる reason code
- `override_policy.never_override_reason_codes`: 人間指示があっても override できない reason code
- `max_rewrite_attempts`: loop router が強制する rewrite 上限
- `no_progress_route`: rewrite しても前進しないときの遷移先

## `scope_signal_guard_decision_v2` の schema 注記

`scope_signal_guard_decision_v2` は任意 object ではなく、`raw_signal` / `scope_context` / `scope_delta_approval` / `security_sensitive` / `route` を必須とする strict schema です。`scope_delta_authority` を含む場合も nested object は `additionalProperties: false` で拘束され、unknown enum や欠落 field は schema validation で reject されます。

## `issue_execution_decision`（`ISSUE_EXECUTION_DECISION_V1`、Issue #1677 で追加）

plan 出力の `issue_execution_decision` は `ISSUE_EXECUTION_DECISION_V1`（`schemas/issue_execution_decision_v1.schema.json`）の versioned relation graph です。`identity` / `nodes` / `relations`（`relation_type`: `depends_on` / `duplicate` / `absorb` / `supersedes` / `coordinates`）/ `execution`（`state`: `selected` / `deferred` / `blocked` / `duplicate`、`predecessors`、`defer_reason`）/ `downstream_policy` / `completeness` を持ちます。`known_context.scope_rollup_result`（`run_refinement_preflight.py` が `ISSUE_SCOPE_ROLLUP_PLAN_V2` artifact から結合）が与えられない場合は、対象 Issue 単独の `selected` 最小形を返します。cross-field invariant（順序・一意性・endpoint・cycle・state semantics）は `plan_refinement_loop.validate_issue_execution_decision()` が検証し、違反時は `execution.state: deferred` に自己修復されます。詳細な downstream handoff 契約は `SKILL.md` の「ISSUE_EXECUTION_DECISION_V1 ハンドオフ契約」節を参照してください。

## `human_decision_reframe` override contract（人間判断 override 契約）

この section では override の許可条件と禁止条件を整理します。

`human_decision_reframe` は `fail_closed` verdict を無視する bypass ではありません。`FAIL_CLOSED_REWRITE_CONSTRAINTS_V1` に従った structured rewrite を続行してよい、という人間判断です。

### override が許可される条件

1. `fail_closed.reason_codes` が `override_policy.allowed_reason_codes` のみで構成されている
2. 人間の anchor comment が、不足 section や不足 key を明示的に認識している
3. rewrite が `FAIL_CLOSED_REWRITE_CONSTRAINTS_V1.rewrite_constraints` に制約されている

### override が禁止される条件

- `fail_closed.reason_codes` に `override_policy.never_override_reason_codes` が 1 つでも含まれる
- `unknown_issue_kind`、`issue_kind_policy_load_error`、`contract_schema_parse_error`、`template_resolution_error`、`checker_internal_error` は常に blocking

### override 後の rewrite contract（override 後の rewrite 契約）

override 後も rewrite の手順は制約付きで進めます。

1. orchestrator は `FAIL_CLOSED_REWRITE_CONSTRAINTS_V1` を `issue-editor` に forward する
2. `issue-editor` は不足 section / key の structured repair だけを行う
3. repair 後は contract checker を自動再実行する
4. post-mutation checker が non-zero の場合は、`max_rewrite_attempts` の範囲で rewrite loop を継続する
5. 上限超過または no progress の場合は `human_judgment_required` に遷移する

### terminal result fields (`AC11`)（終端結果 field）

handoff や terminate 時に残す必須 field をここで確認します。

terminal または handoff result には次を必ず含めます。

- `checked_body_sha256`: 検査対象 Issue body の SHA256
- `checker_exit_code`: post-mutation checker の exit code
- `missing_sections`: rewrite 後も不足している section 一覧
- `missing_contract_keys`: rewrite 後も不足している contract key 一覧

## 冪等性の保証

同じ入力、つまり同じ Issue body・comments・known_context に対しては、`generated_at` を除いて常に同じ JSON が生成されます。

multi-value field である `target_paths` や `repo_claims` などは安定順序で sort されるため、実行ごとの差分が発生しない設計です。
