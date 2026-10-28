# Agent / Skill 責務境界(Issue #34 限定抜粋)

> **出典と範囲**: 本ファイルは `squne121/loop-protocol` tag `pre-split-agent-platform-20261020`
> (peeled `6fa07b6a0ab932a62f08db6a8d521c0dfded2075`)の `docs/dev/agent-skill-boundaries.md`(3,223 行)から、
> Issue #34 の限定承認(2026-10-27 JST)に基づき **次の 2 節のみ** を原文のまま抜粋したもの(source 行 1307-1361 と 2154-2454)。
> 全文移管ではなく、他節は意図的に除外している。除外節・差分の詳細は `docs/dev/stage2-4-edit-txn-manifest.json` に記録する。
>
> - `OUTPUT_BUDGET_V1`(Agent / Skill が参照する出力制約)
> - `ISSUE_EDIT_TXN_INPUT_V1` / `ISSUE_EDIT_TXN_RESULT_V1`(`edit_issue_txn.py` の canonical YAML Schema)
>
> **本 repo での運用注記(schema 本体は無改変)**: schema の `native_relationships` は source 互換のため定義に残るが、
> agent-platform の Stage 2-4 entrypoint では `native_relationships` を含む入力(noop 形を含む)を未承認として
> gh 呼び出し前に拒否する。

## OUTPUT_BUDGET_V1

全 SubAgent / Skill に適用する出力制約の定義。目的は「ルーティングに必要なスキーマフィールドを削除せず、人間向けサマリとエビデンスの再掲を削減すること」。

### 定義

```yaml
OUTPUT_BUDGET_V1:
  intent: "reduce completion/output bloat without removing routing-critical schema fields"
  max_human_summary_lines: 30
  max_human_summary_chars: 2400
  prohibit_full_body_reprint: true
  prohibit_full_diff_reprint: true
  machine_yaml:
    required_schema_fields: must_include_all
    optional_arrays_max_items: 5
    overflow: count_and_refs_only
  evidence:
    refs_only_by_default: true
    allowed_ref_forms: [url, "path:line-line", command_exit_code, artifact_id]
    short_quote_max_words: 25
  patch:
    minimal_delta_only: true
  escape_hatch:
    when_budget_blocks_blocking_finding: "emit NEEDS_EXPANSION with refs"
```

### 適用判定基準

本制約は `.claude/agents/*.md` と `.claude/skills/*/SKILL.md` の全ファイルに適用する。

| 対象 | 適用方法 |
|---|---|
| SubAgent の人間向けサマリ出力 | `max_human_summary_lines: 30` / `max_human_summary_chars: 2400` を遵守する |
| 機械可読 YAML 出力 | `required_schema_fields: must_include_all`（routing 必須フィールドは削らない）、オプション配列は 5 件まで（超過分は件数+参照のみ） |
| エビデンス・証跡 | 原則 `url` / `path:line-line` / `command_exit_code` / `artifact_id` の参照形式で示す。短い引用は 25 語以内を許容 |
| パッチ・diff | minimal delta のみ（前後全文の再掲禁止） |
| Issue / PR 本文の再掲 | 禁止（`prohibit_full_body_reprint: true` / `prohibit_full_diff_reprint: true`） |

### `escape_hatch` の条件

budget 制約の適用によりブロッキングな知見が伝達不能になる場合は、以下の形式で `NEEDS_EXPANSION` を emit して人間または orchestrator に判断を委ねる。

```
NEEDS_EXPANSION: <topic>
refs: [<url-or-path>]
```

`NEEDS_EXPANSION` は制約の例外ではなく、「参照を示した上で詳細展開を要求する」プロトコル。詳細を展開する責務は受け取り側が負う。

### 適用除外（non-goals）

- `machine_yaml` の `required_schema_fields` — routing に必要なフィールドは削らない
- `escape_hatch` 経由の `NEEDS_EXPANSION` — ブロッキング知見の隠蔽禁止
- VC や証跡で必須の出力 — OUTPUT_BUDGET_V1 適用により既存 VC が FAIL する場合は Stop Condition に該当

## edit-issue 向け transaction helper の契約定義（Issue #1287）

既存 Issue body/comment mutation の consumer contract は `edit_issue_txn.py` に集約する。
権限境界は helper input/result schema と controlled executor command id で固定する。

### ISSUE_EDIT_TXN_INPUT_V1

```yaml
type: object
additionalProperties: false
required:
  - schema
  - issue_number
  - repo
  - new_body_file
  - readiness_forwarding_payload
  - comment_mode
  - expected_previous_body_sha256
  - expected_previous_updated_at
  - title_update
properties:
  schema:
    const: ISSUE_EDIT_TXN_INPUT_V1
  issue_number:
    type: integer
  repo:
    type: string
  new_body_file:
    type: string
  readiness_forwarding_payload:
    type: object
    additionalProperties: false
    required: [readiness_result]
    properties:
      readiness_result:
        type: object
        additionalProperties: false
        required:
          - status
          - body_sha256
          - source_checks
          - errors
          - readiness_result_ref
        properties:
          status:
            enum: [go, needs_fix, human_judgment, input_or_runtime_error]
          body_sha256:
            type: string
          source_checks:
            type: array
            items:
              type: string
          errors:
            type: array
            items:
              type: object
          readiness_result_ref:
            type: string
          resolution_evidence:
            type: [string, "null"]
  comment_mode:
    type: object
    additionalProperties: false
    required: [mode]
    properties:
      mode:
        enum: [skip, publish]
      comment_body_file:
        type: [string, "null"]
      marker:
        type: [string, "null"]
  expected_previous_body_sha256:
    type: string
  expected_previous_updated_at:
    type: string
  title_update:
    type: object
    additionalProperties: false
    required: [required, proposed_title, reason]
    properties:
      required:
        type: boolean
      proposed_title:
        type: [string, "null"]
      reason:
        type: [string, "null"]
  native_relationships:
    # Issue #1883: additive, optional. Omitting the field entirely preserves
    # pre-#1883 body/comment-only transaction behaviour exactly.
    type: [object, "null"]
    additionalProperties: false
    properties:
      expected_before:
        type: [object, "null"]
        additionalProperties: false
        properties:
          parent:
            type: [integer, "null"]
          blocked_by:
            type: array
            items:
              type: integer
          blocking:
            type: array
            items:
              type: integer
      parent:
        type: object
        additionalProperties: false
        required: [action]
        properties:
          action:
            enum: [unchanged, set, remove]
          issue_number:
            type: [integer, "null"]
      add_blocked_by:
        type: array
        items:
          type: integer
      remove_blocked_by:
        type: array
        items:
          type: integer
      add_blocking:
        type: array
        items:
          type: integer
      remove_blocking:
        type: array
        items:
          type: integer
  rewrite_lane:
    # Issue #2316: additive, optional discriminator. Omitting the field
    # entirely is equivalent to fail_closed_repair (preserves pre-#2316
    # behaviour exactly). join_review_results.py sets this to "semantic"
    # when an open blocker/high semantic finding routes to rewrite.
    enum: [fail_closed_repair, semantic]
  semantic_rewrite_constraints:
    # Issue #2316: additive, optional. Only accepted when rewrite_lane ==
    # "semantic" (presence-correlation invariant); forbidden otherwise
    # (including when rewrite_lane is omitted). The nested payload is a
    # versioned opaque envelope produced by join_review_results.py --
    # edit_issue_txn.py validates only schema_version and does not deep
    # -validate source_artifact / checked_body_sha256 / findings /
    # max_rewrite_attempts / no_progress_route.
    type: object
    required: [schema_version]
    additionalProperties: true
    properties:
      schema_version:
        const: SEMANTIC_REWRITE_CONSTRAINTS_V1
allOf:
  - if:
      required: [rewrite_lane]
      properties:
        rewrite_lane:
          const: semantic
    then:
      required: [semantic_rewrite_constraints]
    else:
      not:
        required: [semantic_rewrite_constraints]
```

- `title_update.required == true` は non-empty の `proposed_title` と `reason` を必須とし、
  helper が current title/body hash/updatedAt を同一 pre-readback に束縛した
  `issue_content.update` request を生成する。title-only は current body を `new_body` に渡す。
- `title_update.required == false` のとき `proposed_title` と `reason` はともに null とする
- helper が生成する executor authority file は
  `artifacts/{issue_number}/issue-metadata/{command-id}/` 配下のみ

### ISSUE_EDIT_TXN_RESULT_V1

```yaml
type: object
additionalProperties: false
required:
  - schema
  - status
  - issue_number
  - repo
  - mutation_started
  - rollback_attempted
  - body_update
  - comment_publish
  - errors
properties:
  schema:
    const: ISSUE_EDIT_TXN_RESULT_V1
  status:
    enum: [ok, no_change, failed_no_mutation, failed_after_mutation, human_judgment]
  issue_number:
    type: [integer, "null"]
  repo:
    type: [string, "null"]
  mutation_started:
    type: boolean
  rollback_attempted:
    const: false
  body_update:
    type: object
    additionalProperties: false
    required:
      - attempted
      - status
      - previous_body_sha256
      - new_body_sha256
      - remote_current_body_sha256
      - artifact_ref
    properties:
      attempted:
        type: boolean
      status:
        enum: [not_run, ok, failed]
      previous_body_sha256:
        type: [string, "null"]
      new_body_sha256:
        type: [string, "null"]
      remote_current_body_sha256:
        type: [string, "null"]
      artifact_ref:
        type: [string, "null"]
  comment_publish:
    type: object
    additionalProperties: false
    required:
      - attempted
      - status
      - comment_id
      - comment_url
      - comment_body_sha256
      - artifact_ref
    properties:
      attempted:
        type: boolean
      status:
        enum: [not_run, ok, failed]
      comment_id:
        type: [string, "null"]
      comment_url:
        type: [string, "null"]
      comment_body_sha256:
        type: [string, "null"]
      artifact_ref:
        type: [string, "null"]
  errors:
    type: array
    items:
      type: object
      additionalProperties: false
      required: [code, message]
      properties:
        code:
          type: string
        message:
          type: string
  native_relationships:
    # Issue #1883: additive result block. Always present -- {"status":
    # "not_run", "attempted": false, ...} when the transaction input omitted
    # native_relationships or when a pre-relationship-gate failure (readiness
    # gate / stale content precondition) short-circuited the transaction
    # before the relationship saga ever ran.
    type: object
    additionalProperties: false
    required:
      - attempted
      - status
      - before
      - desired
      - after
      - completed_operations
      - pending_operations
      - errors
    properties:
      attempted:
        type: boolean
      status:
        enum: [not_run, no_op, applied, failed_no_mutation, failed_after_mutation]
      before:
        type: [object, "null"]
      desired:
        type: [object, "null"]
      after:
        type: [object, "null"]
      completed_operations:
        type: array
        items:
          type: string
      pending_operations:
        type: array
        items:
          type: string
      errors:
        type: array
        items:
          type: object
```

- stdout は exactly one bounded JSON object とし、old/new issue body や raw child stdout/stderr を含めない
- body/comment mutation authority は `issue_body.update` / `issue_comment.publish` / `issue_relationship.update` に限定する
- native relationship mutation（parent / blockedBy / blocking）は title/body content mutation より先に実行し、失敗時は content mutation を一切開始しない（Issue #1883 AC1/AC2）
