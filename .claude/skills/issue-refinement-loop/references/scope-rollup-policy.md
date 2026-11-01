---
title: スコープ統合方針（Scope Rollup Policy）
schema_version: 1
related_script: ../scripts/plan_issue_scope_rollup.py
related_issues: ["#316", "#550"]
status: active
canonical_policy: docs/dev/workflow.md#execution-planning-policy-canonical-ssot
description: この文書はスコープ統合の判断手順を定義する。
---

# スコープ統合方針（Scope Rollup Policy）

この文書は `docs/dev/workflow.md#execution-planning-policy-canonical-ssot` の canonical policy を、`plan_issue_scope_rollup.py` の判定ロジックと
`issue-refinement-loop` / `impl-review-loop` における統合アクションの選択基準を定義する。
普遍的な planning state、freshness、collision、merge readiness、quality/safety gate の設計正本ではない。
canonical `ISSUE_EXECUTION_DECISION_V1` の field/enum/semantic validator は #1677 が所有し、本書は
normalised decision を再解釈しない。legacy graph adapter の equivalence digest が不一致なら
`migration_required: yes` として fail-closed にする。

## 目的

AI エージェントが同一ファイル・同一 skill family の修正を別 PR に分散させることを防ぐ。
orchestrator は `ISSUE_SCOPE_ROLLUP_PLAN_V2` を受け取り、このポリシーに従って統合可否を判断する。

---

## 1. 統合可能な条件（mergeable conditions）

以下のいずれかの条件を満たす場合、統合の検討対象となる。

| 条件 | シグナル名 |
|---|---|
| 同一 dedupe_key を持つ Issue / PR が存在する | `shared_dedupe_key` |
| 完全一致する Allowed Paths を持つ Issue / PR が存在する | `exact_allowed_path_overlap` |
| 同一 skill family（`.claude/skills/<family>/`）に属する変更である | `same_skill_family` |
| 同一 parent_issue を持つ Issue / PR が存在する | `same_parent_issue` |
| 同一 failure_mode_marker を持つ Issue / PR が存在する | `same_failure_mode_marker` |

---

## 2. 統合不可能な条件（non-mergeable conditions）

以下のいずれかに該当する場合は、統合してはならない。

1. **docs-only 変更と runtime 変更の混在**
   - `docs/` のみを変更する Issue と、`src/` / テスト / スクリプト等の runtime 変更 Issue は同一 PR に統合しない。
2. **構造的衝突（同一 sub-file anchor への競合変更）**
   - 同一ファイル内の同一 anchor（JSON Pointer / プロパティ名）を双方が変更する場合は統合しない。
   - `scope_context.escalation_required == true`（`conflict_type: same_anchor_conflicting_operation`）のとき `suggested_action: human_review_required` を設定し、人間が判断する。
   - `scope_context.conflict_type == uncertain` のとき `suggested_action: proceed_with_coordination` を設定し、coordination evidence を残したうえで進行する。ただし「安全に並行可能」「semantic conflict がない」ことは保証しない。
   - **重要（#550）**: security / auth / permission / sandbox などの語彙が本文に一致するだけでは escalation しない。語彙一致は `scope_context.domain_flags` と `security_match_evidence` に audit 目的で記録するのみで、判定の主因にしない（誤エスカレーション抑止）。
3. **Allowed Paths 超過**
   - 統合によって Allowed Paths の範囲を超える変更が生じる場合は統合しない。
4. **異なるアーキテクチャ層（`src/state` vs `src/render` 等）の混在**
   - CLAUDE.md の「不変のアーキテクチャ原則」で分離が要求されている層を跨ぐ変更は統合しない。
5. **closed / not_planned Issue との統合**
   - closed または not_planned になった Issue の変更を open Issue に取り込んではならない。

---

## 3. 統合アクション（5 種類）

`ISSUE_SCOPE_ROLLUP_PLAN_V2.candidates[].suggested_action` に設定される値。

### 3.1 `merge_into_current_pr`（現在の PR へ統合）

同一 PR に統合する。
適用条件: `confidence: high` かつ `shared_dedupe_key` シグナルあり、または高い信頼度で同一変更セットと判断できる場合。

### 3.2 `amend_current_issue`（現在の Issue を補正）

現在の Issue 本文（Allowed Paths / Outcome / AC / VC 等）を修正して対象を吸収する。
適用条件: `confidence: high` または `confidence: medium` で `exact_allowed_path_overlap` + `same_parent_issue` の組み合わせ。

### 3.3 `create_parent_rollup_issue`（親ロールアップ Issue を作成）

複数の関連 Issue を束ねる parent rollup Issue を新規作成し、それぞれを child として管理する。
適用条件: `confidence: medium` で `same_parent_issue` シグナルあり、または同一 skill family の Issue が 3 件以上ある場合。

### 3.4 `keep_separate_with_reason`（理由付きで分離を維持）

統合せず別 Issue / PR として維持する。理由を `ISSUE_SCOPE_ROLLUP_DECISION_V2` に記録すること。
適用条件: `confidence: low`、または統合不可能な条件に該当する場合。

### 3.5 `human_review_required`（人間レビューが必要）

AI による自動判断を行わず、人間が統合可否を判断する。
適用条件（#550 で語彙一致ベースから構造化コンテキストベースへ変更）:

- `scope_context.escalation_required == true`（同一 sub-file anchor への競合変更 = `conflict_type: same_anchor_conflicting_operation`）

`scope_context.conflict_type == uncertain` は `human_review_required` ではなく `proceed_with_coordination` にルーティングする。`proceed_with_coordination` は「coordination evidence を残したうえで進行可能」であり、「安全に並行可能」「semantic conflict がない」ことを保証しない。

security / auth / permission / sandbox 等の語彙一致のみでは `human_review_required` にしない。

---

## 3.5b `scope_context` / `ordering_constraint`（#550 で追加）

各候補は `suggested_action` とは独立した構造化フィールドを持つ。

### `scope_context`

変更の構造的関係を記述する（additive、既存フィールド非削除）。

```yaml
scope_context:
  anchor_paths: []          # 重複するファイルパス（構造的位置）
  conflicting_anchors: []   # 双方が変更する共有 sub-file anchor（JSON Pointer / プロパティ名）
  domain_flags: []          # 意味カテゴリ（security/schema/metadata/workflow/runtime/docs）— audit 専用、判定の主因にしない
  conflict_type: none | same_anchor_conflicting_operation | same_file_disjoint_anchor | uncertain
  escalation_required: true | false
```

**衝突種別の判定**:

| conflict_type | 条件 | escalation |
|---|---|---|
| `none` | パス重複なし | false |
| `same_anchor_conflicting_operation` | 同一ファイル + 共有 sub-file anchor | **true** |
| `same_file_disjoint_anchor` | 同一ファイルだが anchor が disjoint（例: #547 が phase_instance_id、#549 が /required） | false |
| `prefix_overlap_uncertain` | prefix 重複（親子パス関係）のみ。`anchor_paths` に `parent -> child` を記録。共有 anchor があれば escalate | 条件付き |
| `uncertain` | 完全同一ファイルだが anchor を抽出できず disjoint を証明できない | **false**（proceed_with_coordination） |

`none` は「パス重複なし」のみに限定する。prefix 重複（`.claude/skills/foo` と `.claude/skills/foo/SKILL.md` 等）は `none` にせず `prefix_overlap_uncertain` として `anchor_paths` に親子関係を記録する（#550 Blocker 4）。

#### sub-file anchor 抽出の限界（RFC 6901 非準拠 — #550 Blocker 5）

sub-file anchor は本文中の以下から **軽量テキスト抽出** する:

- JSON Pointer 風トークン: `/required`、`/properties/foo/pattern`
- バックティック付きプロパティ名 / JSON Pointer: `` `phase_instance_id` ``、`` `/required` ``

これは **厳密な RFC 6901 実装ではない**:

- `~0`（`~`）/ `~1`（`/`）のエスケープ解除を行わない
- array index（`/items/0`）の意味論を扱わない
- 構文検証を行わず、正規表現マッチのみ

precise な anchor 解決（AST / tree-sitter による構文木ベースの抽出）は本ポリシー対象外であり、follow-up とする。現状の軽量 extractor は「同一ファイル内の独立変更（#547/#549）を disjoint と判定できる」最小限の精度を目的とし、その境界をここに明文化する。

#### scope rollup は security gate ではない（#550 / 12:00 人間指示）

scope rollup は **汎用の scope collision 判定** であり、security gate ではない。
secret value のログ出力、GitHub Actions secrets、auth token、credential / permission / access-control などの **real-security-risk escalation は専用 skill の責務** とし、本スクリプトでは判定しない。
security 語彙の一致は `domain_flags` / `security_match_evidence` に audit 目的で記録するのみ（判定の主因にしない）。

### `ordering_constraint`

時間的順序の推奨。`suggested_action`（何をするか）とは別概念（順序）。

```yaml
ordering_constraint: current_first | candidate_first | parallel_ok | sequential_required
```

---

## 4. confidence_rules（信頼度ルール）

### high（高信頼度）

以下の条件のいずれかを満たす場合のみ `high` を設定する。

```yaml
high_conditions:
  - signals: [shared_dedupe_key]          # 同一 dedupe_key は最強シグナル
  - signals: [exact_allowed_path_overlap, same_parent_issue]
  - signals: [exact_allowed_path_overlap, same_failure_mode_marker]
```

**重要制約**: `same_skill_family` のみのシグナルは `high` を禁止する（後述の AC6 要件）。

### medium（中信頼度）

```yaml
medium_conditions:
  - signals: [exact_allowed_path_overlap]  # 単体でも medium
  - signals: [same_parent_issue]           # 単体でも medium
  - signals: [same_skill_family, <any_other_signal>]  # 追加シグナルがあれば medium
  - signals: [same_failure_mode_marker]    # 単体でも medium
```

### low（低信頼度）

```yaml
low_conditions:
  - signals: [same_skill_family]  # same_skill_family のみ -> low（high 禁止）
  - signals: []                   # シグナルなし（candidates に含まれない）
```

---

## 5. `same_skill_family` only は `high` 禁止（adversarial 防止ルール）

`same_skill_family` シグナル単体では `confidence: high` を設定してはならない。

**理由**: 同一 skill family に属するだけでは、変更内容の重複・競合を確定的に判断できない。
Allowed Paths の実際の重複や dedupe_key の一致がなければ、統合は安全ではない可能性がある。

```yaml
adversarial_rule:
  condition: signals == [same_skill_family]
  prohibited: confidence: high
  required: confidence: low
  rationale: "skill family 共有のみでは Allowed Paths 重複・dedupe_key 一致の証拠にならない"
```

---

## 6. `ISSUE_SCOPE_ROLLUP_DECISION_V2` 常時記録要件

統合を実施した場合・しなかった場合を問わず、orchestrator は必ず
`ISSUE_SCOPE_ROLLUP_DECISION_V2` を LOOP_STATE に記録しなければならない。

```yaml
ISSUE_SCOPE_ROLLUP_DECISION_V2:
  schema_version: 2
  recorded_at: "<ISO8601>"
  rollup_plan_ref:
    body_sha256: "<plan の body_sha256>"
    generated_at: "<plan の generated_at>"
  decision: executed | skipped | deferred | human_review_required
  executed_actions: []           # 実施したアクションのリスト（decision: executed の場合）
  skipped_reason: null           # decision: skipped の場合の理由
  candidates_reviewed:
    - kind: "issue|pr"
      number: <int>
      confidence: "high|medium|low"
      suggested_action: "<action>"
      final_decision: "accepted|rejected|deferred|human_review_required"
      rejection_reason: null      # final_decision: rejected の場合のみ設定
```

**記録タイミング**:
- `issue-refinement-loop`: Step 0d 完了後、Step 0-hygiene 実行前に記録する。
- `impl-review-loop`: Step 2.5 完了後、worktree 作成（Step 3）前に記録する。

**記録先**: `LOOP_STATE.scope_rollup_decision` フィールド（LOOP_STATE に追加）。

---

## 7. orchestrator の判断ルール

```yaml
orchestrator_rules:
  confidence_high:
    action: "候補ごとに suggested_action を LOOP_STATE に記録し、統合実施前に orchestrator が判断する"
    escalation_override: "scope_context.escalation_required==true の候補は suggested_action を human_review_required に上書きする（#550: 語彙一致ではなく構造的衝突で判定）。uncertain は proceed_with_coordination を維持し、human_review_required に上書きしない。"
    auto_execute: false   # high でも自動実行しない。orchestrator が明示的に判断する

  confidence_medium:
    action: "LOOP_STATE に記録し、推奨アクションを提示する。自動実行はしない"
    auto_execute: false

  confidence_low:
    action: "LOOP_STATE に記録するが、アクション不要（keep_separate_with_reason）"
    auto_execute: false

  structural_conflict_candidates:
    action: "scope_context.escalation_required==true の候補は human_review_required を設定して即時停止。uncertain は proceed_with_coordination で継続する。AI による自動統合禁止"
    note: "#550: security 語彙一致のみでは停止しない。domain_flags / security_match_evidence は audit 記録のみ"
    auto_execute: false
```

---

## 関連資料（Related）

- `../scripts/plan_issue_scope_rollup.py` — このポリシーを実装するスクリプト
- `.claude/skills/issue-refinement-loop/SKILL.md` — Step 0d でこのポリシーを適用
- `.claude/skills/impl-review-loop/steps/preparation.md` — Step 2.5 でこのポリシーを適用
- `.claude/skills/impl-review-loop/steps/step-5-feedback-and-termination.md` — DECISION_V2 記録セクション
