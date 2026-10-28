---
name: edit-issue
description: 既存 GitHub Issue 本文更新を transaction helper に集約する手順。reviewer フィードバックや人間判断結果を反映し、controlled executor lane を使って body/comment mutation を 1 transaction として実行する。`issue-editor` SubAgent や main session が「Issue ◯◯ の本文を修正して」「Issue 本文を更新して」「edit issue」などのトリガーで使う。新規起票は `issue-creator` の create route を使う。
---

# Issue 編集

既存 Issue body/comment mutation の本番経路を
`.claude/skills/edit-issue/scripts/edit_issue_txn.py` に集約する。
呼び出し側は candidate body と readiness context を用意し、helper が返す
JSON result を次のルーティング判断に使う。本文の書き戻し authority は
`issue_content.update` / `issue_comment.publish` の controlled executor command id
だけに限定する。

## 依存ポリシー

```yaml
dependency_policy:
  required_for_txn_helper: "#1284 / PR #1295"
  required_for_end_to_end_raw_mutation_removal: "#1291 / PR #1298 (merged)"
```

- `required_for_txn_helper` は transaction helper 自体の前提条件。
- `required_for_end_to_end_raw_mutation_removal` は local main guard 側の allowlist 整理を含む別 dependency。
- 本 skill の success は helper consumer への移行を意味し、repo 全体で raw mutation 経路の排除が完了したことまでは意味しない。

## 入力

- `issue_number`（必須）
- `reviewer_feedback_url` または `reviewer_feedback_text`（任意）
- `readiness_forwarding_payload`（必須）: `READINESS_FORWARDING_PAYLOAD_V1`
- `new_body_file`（必須）: candidate issue body を保存した repo-relative file
- `comment_mode`（任意）: success comment を controlled publish するかどうかの指定
- `title_update`（任意）: `required: true` のとき non-empty の `proposed_title` と `reason` を必須とし、title/body を `issue_content.update` の単一 PATCH で更新する

`READINESS_FORWARDING_PAYLOAD_V1.readiness_result.status` は
`status: go | needs_fix | human_judgment | input_or_runtime_error`
だけを受け付ける。`status: go` の場合は pre-author static readiness blocker なし、
`status: needs_fix` の場合は `errors[]` と `readiness_result_ref` をcandidate body 修正の正本に使う（`resolution_evidence` も併せて参照する）。
`status: human_judgment | input_or_runtime_error` の場合は fail-closed で helper の mutation 段へ進めない。

## 入力契約

`docs/dev/agent-skill-boundaries.md` の `ISSUE_EDIT_TXN_INPUT_V1` を正本とする。
呼び出し側は以下のような JSON を repo 配下に書き、helper に渡す。

```json
{
  "schema": "ISSUE_EDIT_TXN_INPUT_V1",
  "issue_number": 1287,
  "repo": "squne121/agent-platform",
  "new_body_file": "tmp/issue_1287_new.md",
  "readiness_forwarding_payload": {
    "readiness_result": {
      "status": "go",
      "body_sha256": "sha256:...",
      "source_checks": ["contract_readiness_check.py --mode static"],
      "errors": [],
      "readiness_result_ref": "artifacts/.../readiness.json",
      "resolution_evidence": "artifacts/.../resolution_evidence.json"
    }
  },
  "comment_mode": {
    "mode": "skip"
  },
  "expected_previous_body_sha256": "sha256:...",
  "expected_previous_updated_at": "2026-07-03T10:40:51Z",
  "title_update": {
    "required": false,
    "proposed_title": null,
    "reason": null
  }
}
```

### native_relationships（任意、Issue #1883 additive 拡張）

> **agent-platform Stage 2-4 では未承認**: `native_relationships` を含む入力（noop 形を含む）は
> `edit_issue_txn.py` が gh の read/auth/mutation より前に拒否し、`failed_no_mutation` を返す。
> 以下は source（Issue #1883）の契約記録であり、現時点で実行可能な手順ではない。

native `parent`／`blockedBy`／`blocking` を同期する場合は `native_relationships` を追加する。
省略時は pre-#1883 の body/comment-only 挙動と完全に同一。

```json
{
  "native_relationships": {
    "expected_before": {"parent": null, "blocked_by": [], "blocking": []},
    "parent": {"action": "set", "issue_number": 1860},
    "add_blocked_by": [],
    "remove_blocked_by": [],
    "add_blocking": [],
    "remove_blocking": []
  }
}
```

- `expected_before` は省略可（省略時は helper 自身の pre-readback 値を採用する）。指定した場合は live 値との完全一致が mutation の前提条件になる（drift があれば mutation を一切実行せず `failed_no_mutation`）
- `parent.action` は `unchanged`／`set`／`remove` のいずれか。`replace` semantics は提供しない
- `add_blocked_by`／`remove_blocked_by`／`add_blocking`／`remove_blocking` は明示的な delta 集合として指定する（空集合への全解除も明示的な `remove_*` で表現する）
- native relationship mutation は title/body content mutation より **先に** 実行され、失敗した場合は content mutation を一切開始しない（AC1/AC2）
- 自然言語（`Part of #N`、`Related`、コメント等）からは parent／blocked_by／blocking を一切推測しない。structured input（この `native_relationships` フィールド）だけが source of truth

### rewrite_lane（任意、Issue #2296 追加）

`issue-refinement-loop` の Step 4（rewrite）がどちらの rewrite 契約を使って
呼び出しているかを示す discriminator フィールド。省略時は `fail_closed_repair`
として扱う（既存挙動と完全互換）。

```json
{
  "rewrite_lane": "fail_closed_repair"
}
```

- `fail_closed_repair`: `.claude/agents/issue-editor.md` の `FAIL_CLOSED_REWRITE_CONSTRAINTS_V1`
  契約（必須セクション/契約キーの補完のみ、`freeform_rewrite_forbidden: true` が既定）
- `semantic`: 同エージェントの `SEMANTIC_REWRITE_CONSTRAINTS_V1` 契約（Step 2.5 semantic
  design review の finding を解消するための AC/VC/architecture 判断の書き換え）

## 手順

### 1. candidate body と readiness context を準備する

- `reviewer_feedback_url` / `reviewer_feedback_text` と `readiness_forwarding_payload` を使って candidate body を生成する
- candidate body は repo-relative file に保存する
- `title_update.required == true` のときは current title、current body hash、current updatedAt を同一 pre-readback に束縛する。title-only は candidate body に current body を渡す。
- body authoring rule は [`../create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) を参照する

### 2. helper を起動する

```bash
uv run --locked python3 .claude/skills/edit-issue/scripts/edit_issue_txn.py \
  --input-file tmp/issue_<N>_txn_input.json
```

helper は以下の固定順序で進む。

1. current issue を readback する
2. （`native_relationships` 指定時）capability preflight → pre-readback → `expected_before` drift 確認 → graph invariant 検証 → `issue_relationship.update` controlled executor で native parent／blockedBy／blocking を同期し、全ページ post-readback を確認する（失敗時は以降の全ステップを実行せず終了する。Issue #1883）
3. candidate body を load する
4. stale precondition を確認する
5. guard を実行する
6. hygiene autofix を適用する
7. static readiness check を実行する
8. `issue_content.update` 用 input file を生成する
9. title/body を固定 endpoint の単一 PATCH で更新する
10. title と body の final readback を確認する
11. 必要な場合だけ `issue_comment.publish` を実行する
12. bounded result を出力する（`native_relationships` ブロックを含む）

### 3. 失敗時ルーティング

- stale precondition / guard / readiness が失敗した場合 → `failed_no_mutation`
- content update 後に title/body final readback が失敗した場合 → `failed_after_mutation`
- body update 成功後に comment publish が失敗した場合 → `failed_after_mutation`
- `readiness_forwarding_payload.readiness_result.status` が `human_judgment` または `input_or_runtime_error` → `human_judgment`
- native relationship の capability preflight 失敗／`expected_before` drift／graph invariant 違反 → `failed_no_mutation`（content mutation は一切実行しない。AC1）
- native relationship mutation 自体が失敗（部分実行含む）した場合 → `failed_after_mutation`（content mutation は一切実行しない。AC2）

### 4. 出力

`docs/dev/agent-skill-boundaries.md` の `ISSUE_EDIT_TXN_RESULT_V1` を返す。
helper stdout は最後の 1 JSON object のみとし、old/new issue body や child stdout/stderr を含めない。

## visual_impact フィールド（canonical schema owner）

編集対象 Issue 本文が `visual_impact` 関連フィールド（`VISUAL_IMPACT_DECLARATION_V1` 等）に触れる場合、
単一の canonical schema ファイル [`docs/dev/visual-impact.schema.json`](../../../docs/dev/visual-impact.schema.json)
を参照する。本 SKILL.md では独自にキー集合・enum を再定義しない（Issue #2019 AC26）。

## ガードレール

- existing issue body/comment mutation の本番経路は `edit_issue_txn.py` 経由に限定する
- helper は `issue_content.update` / `issue_comment.publish` 以外の mutation command id を使わない
- `issue_content.update` は title/body だけを固定 endpoint へ一度だけ PATCH し、曖昧な PATCH failure は remote readback で `already_applied` または失敗に分類する。自動再試行・rollback・CAS は行わない
- executor input は `artifacts/{issue_number}/issue-metadata/{command-id}/` 配下だけに生成する
- helper は `capture_output=True, text=True, shell=False` で子プロセスを起動し、bounded diagnostics だけを result に残す
- native relationship mutation は `issue_relationship.update` controlled executor 固定 operation 経由に限定する。caller が任意の GraphQL query／REST path／hostname／argv を渡す経路は存在しない（AC13）
