---
name: review-issue
description: GitHub Issue 本文を `check_issue_contract.py` で決定論的にレビューし、`REVIEW_ISSUE_RESULT_V1` を返す script-first skill。VC の動作検証はしない（pr-review-judge / test-runner の責務）。「Issue ◯◯ レビュー」「review issue」のトリガーで使う。
---

# Review Issue

Issue 本文の構造品質を `.claude/skills/review-issue/scripts/check_issue_contract.py` で機械的に判定し、JSON 結果を `REVIEW_ISSUE_RESULT_V1` として返す。

判定ロジックは checker を SSOT とする。LLM は checker JSON を整形・転送するだけで、独自の自然言語判定で結果を補完しない。

## Input（入力）

- `issue_number`（必須）
- `invoked_as_loop`（任意、bool）: `issue-refinement-loop` から呼ばれた場合 `true`、人間直起動なら `false`

## Producer I/O ownership（producer I/O 所有権の分離、Issue #2049）

`issue-refinement-loop` の canonical Step 2 は、`issue-reviewer` custom agent（`.claude/agents/issue-reviewer.md`）を経由せず、`.claude/skills/issue-refinement-loop/scripts/run_root_review_pipeline.py`（root-owned pipeline）を orchestrator（main thread）が直接呼び出し、`produce` が返す `compact_result.verdict` / `compact_result.next_action` / `verified_transport_artifact` を直接 consume する（Issue #2380）。routing 判定そのものは `produce` の stdout JSON トップレベル `canonical_step2_route` フィールド（`route_canonical_step2_result()` の戻り値。`produce` 自身が全出力経路で計算済み）を唯一の routing authority として読むだけであり、orchestrator は `status` / `compact_result.verdict` / `compact_result.next_action` から独自に再計算しない（Issue #2389。詳細は `issue-refinement-loop/SKILL.md` の Step 2 routing table を参照）。body fetch・temp file・checker 実行・artifact 保存・compact envelope 生成/永続化はすべてこの root-owned pipeline が行い、read-only な `issue-reviewer` agent はこれらの producer I/O を一切実行しない。`issue-reviewer` agent は canonical Step 2 では起動されず、legacy CLI（`run_root_review_pipeline.py classify-child-stdout` 等）・診断・回帰テスト用途でのみ引き続き利用可能である（下記の所有権表は、`issue-reviewer` agent 経由で legacy CLI/diagnostic 用途として本 skill 自体が起動される場合の handoff contract を示す。PR #2135 human REQUEST_CHANGES iteration-3 P0-1: 従来の narrative な説明だけでは「どの Procedure ステップを child が skip するか」が未規定で、実行可能な handoff contract が無いという指摘を受けて、下記の表で明示する。#2054 で V1 envelope から V2 envelope へ atomic cutover した）。

**invoked_as_loop による Procedure ステップの所有権表（executable handoff contract）**:

| Step | 内容 | `invoked_as_loop: true`（canonical: orchestrator が root-owned pipeline を直接呼ぶ。`issue-reviewer` agent 経由は legacy CLI/diagnostic 専用） | `invoked_as_loop: false`（人間直起動） |
|---|---|---|---|
| 1 | `gh issue view` で本文取得 | orchestrator（main thread）が `root_review_pipeline.produce` 経由で実行。`issue-reviewer` agent は実行しない | main thread がそのまま実行 |
| 2 | 一時ファイル保存・checker 3 コマンド実行 | 同上（`run_root_review_pipeline.py`, `produce` サブコマンド内部。invocation-private `TemporaryDirectory` を使い呼び出し完了時に自動削除） | main thread がそのまま実行（一時ファイルの cleanup 責務は呼び出し元） |
| 2.5 | compact envelope（`ISSUE_REVIEW_RESULT_COMPACT_V2`）の生成・`reviewer_transport.py`（V2 契約 SSOT）による永続化 | orchestrator が `produce` の戻り値 `compact_result.verdict` / `compact_result.next_action` / `verified_transport_artifact` を直接 consume する（root-owned、単一 producer、内部で `reviewer_transport.run_reviewer_transport()` に委譲）。canonical Step 2 では `issue-reviewer` agent へ `compact_result.stdout_lines` を relay しない（Issue #2380）。`issue-reviewer` agent が legacy CLI/diagnostic 用途で本 skill を起動された場合に限り、この `stdout_lines` を **read-only input** として受け取り、そのまま自身の最終出力として relay するだけで、producer スクリプトを自ら実行しない | main thread は `invoked_as_loop: false` では compact envelope を生成しない。Step 3 の `REVIEW_ISSUE_RESULT_V1` JSON をそのまま呼び出し元へ返す（compact envelope の生成・永続化は loop 経由（`invoked_as_loop: true`）専用の producer 配線であり、`compact_review_result.py` は retired、V1/V2 downgrade fallback はない） |
| 3 | checker JSON を `REVIEW_ISSUE_RESULT_V1` に整形 | Step 2 の一部として root-owned pipeline が実行済み（`merged_review_result`） | main thread がそのまま実行 |
| 4-5 | diff_proposal 提示・本文書き戻し判定 | orchestrator（`issue-refinement-loop`）が VERDICT を元に判定。`issue-reviewer` agent 自身は判定しない | main thread がそのまま実行 |

`issue-reviewer` agent が legacy CLI/diagnostic 用途で本 skill を起動された場合の INPUT_CONTRACT は、root-owned pipeline の `produce` コマンド出力に含まれる `compact_result.stdout_lines`（11 行の `ISSUE_REVIEW_RESULT_COMPACT_V2`、既に `reviewer_transport.build_compact_v2()` で render・artifact 束縛済み）を明示フィールドとして受け取ることである。この agent は値を再計算・再フォーマットせず、そのまま relay する。canonical Step 2 はこの INPUT_CONTRACT を経由しない（Issue #2380）。

人間が本 skill を直接起動する場合（`invoked_as_loop: false`）は、以下の Procedure（Step 1-5）を main thread がそのまま実行する（read-only 制約は適用されない）。

## Procedure（`invoked_as_loop: false` — 人間直起動時、または root-owned pipeline 自体の実装時の手順）

1. `gh issue view <番号> --json title,body,labels` で本文を取得する。
2. 本文を一時ファイルに保存し、以下のスクリプトを**決定論的に**順に実行する（LLM は JSON を整形・転送するだけで、合成ロジック自体を実行しない。Issue #1791 review remediation, Critical #1）:
   - `uv run python3 .claude/skills/review-issue/scripts/check_issue_contract.py --file <tmp> --json > <review_result.json>`
   - `uv run python3 .claude/skills/issue-contract-review/scripts/contract_readiness_check.py --body-file <tmp> --mode execute > <readiness_result.json>`
   - `uv run python3 .claude/skills/review-issue/scripts/check_issue_contract.py --mode merge_readiness --review-result-file <review_result.json> --readiness-result-file <readiness_result.json> --readiness-artifact-path <readiness_result.json の実パス> --iteration-id <iteration_id> --output-file <merged_review_result.json>`

   `check_issue_contract.py` は内部で `check_product_spec_contract.py --body-file <tmp>` を同一 body snapshot に対して実行し、`body_sha256` mismatch / malformed output / pair invariant violation / timeout を fail-closed に合成する。

   3 番目のコマンド（`--mode merge_readiness`）が `ISSUE_CONTRACT_READINESS_RESULT_V1.errors[]` を `REVIEW_ISSUE_RESULT_V1` へ合成する唯一の producer であり、この合成は `merge_readiness_into_review_result()`（`check_issue_contract.py`）が決定論的に行う。本 skill / LLM は合成規則を再実装せず、上記コマンドを実行し `<merged_review_result.json>` を Step 3 以降の `REVIEW_ISSUE_RESULT_V1` として扱う。合成規則の要点（実装の詳細規則は `check_issue_contract.py` の docstring に集約し、ここでは重複記載しない）:
   - `blocking_issues` には各 error の `fix_hint` 文字列が追記される（人間向け要約）。
   - `structured_blockers` には `errors[]` の構造体は**そのまま転写されない**。`code` / `message` / `finding_kind` / `deterministic_domain_key` / `blocking` / `checker_evidence`（`source_check` / `rule_id` / `category` / `artifact_path` / `artifact_schema` / `body_sha256` / `iteration_id` / `line_start` / `line_end` を含む）を備えた**変換後の形状**に限り追記される。生の readiness error 構造体が `structured_blockers` へ直接コピーされることはない。
   - `status: needs_fix` の errors → `verdict: needs-fix` に反映される。
   - `status: human_judgment` の errors（`env_missing_dep` / `timeout` / unknown 分類など）は `structured_blockers` に追加されない（deterministic blocker として扱うと Issue 本文書き換えで解決しない事象を「修復対象」に誤分類するため）。代わりにトップレベルの `REVIEW_ISSUE_RESULT_V1.failure_class: contract_readiness_human_judgment` が設定される。`verdict` はスキーマ上 `approve | needs-fix` のみを許可するため `needs-fix` のまま据え置かれる。compact V2 wire（`reviewer_transport.py` の `build_compact_v2()` / `validate_compact_v2()`）はこの `failure_class` を一切読まず、`NEXT_ACTION` は `proceed | request_changes` の二値契約のまま変更しない（Issue #2054 / Issue #2397 Out of Scope）。loop 経由（`invoked_as_loop: true`）では、root-owned canonical route（`run_root_review_pipeline.route_canonical_step2_result()`）が、この producer 呼び出しが自身の stdout JSON に含める verified `merged_review_result.failure_class`（トップレベル）を直接参照し、`status: ok` + `verdict: needs-fix` + `next_action: request_changes` かつ `failure_class == "contract_readiness_human_judgment"` の場合に限り `step_5_operator_intervention_required` route へ送る（Issue #2397。詳細は `issue-refinement-loop/SKILL.md` の Step 2 routing table を参照）。
   - 判定ロジックは `contract_readiness_check.py` に集約し、本 skill では再実装しない。合成ロジックは `check_issue_contract.py` の `merge_readiness_into_review_result()` に集約し、SKILL.md には合成規則の詳細（フィールド対応表・regex 等）を重複記載しない。

   Note: `--mode execute` は `compound_command_disallowed`（静的検出）と `unexpected_pass`（VC 実行結果）の両方を検出する。`shell=True` は導入しない（既存の `shell=False` 前提を維持）。
3. checker の JSON をそのまま `REVIEW_ISSUE_RESULT_V1` に整形する（`verdict` / `deterministic_checks` / `blocking_issues` / `non_blocking_improvements` / `diff_proposal` を保持）。
   `findings[]` / `checker_evidence[]` / `body_sha256` / producer schema version も lossless に保持し、compact consumer が provenance を失わないようにする。
4. `verdict: needs-fix` の場合のみ `diff_proposal` を呼び出し元に提示する。本文書き戻しは Step 5 の条件分岐に従う。
5. 本文書き戻し条件:

| Verdict | invoked_as_loop | アクション |
|---|---|---|
| `approve` | * | レビュー結果のみ返して終了 |
| `needs-fix` | `true` | `diff_proposal` を返し、本文更新は呼び出し元（`issue-refinement-loop`）に委ねる。本 skill では本文を直接書き換えない |
| `needs-fix` | `false` | ユーザーに「この差分を Issue 本文に適用しますか？（yes/no）」と明示確認。承認時のみ `edit-issue` skill の controlled executor（`issue_content.update`）経由でのみ本文を更新する |

## Checker contract（チェッカー契約、C1〜C12）

決定論的判定の詳細仕様は `scripts/check_issue_contract.py` に集約する。本 SKILL.md には判定表・regex・Step 4 自然言語評価を重複記載しない。checker は以下を返す:

- `C1_required_sections` 〜 `C13_vc_preflight_decision_consistency` の 13 件の `pass | fail | warn | n/a | legacy_missing_applicability` 値
- `blocking_issues`: 各 fail の説明 string 配列
- `non_blocking_improvements`: 各 warning が `{code, severity, evidence, details?, suggested_action}` の dict として配列（`evidence` は `list[str]`、`details` は warning 固有の構造化情報 dict で省略可）
- `diff_proposal.add`: C1 fail 時の `missing_section_skeleton` 等、機械的に挿入可能な skeleton 配列
- `verdict`: いずれか fail があれば `needs-fix`、それ以外は `approve`

`C12_product_trace_fields_structure` は Product Spec / task-lineage Issue に限って適用され、`product_spec_id` / `requirement_id` / `source_task_id` の構造欠落・placeholder・形式不正を fail にする。非該当 Issue では `n/a` を返し verdict を変えない。

## Output（出力、REVIEW_ISSUE_RESULT_V1）

```yaml
REVIEW_ISSUE_RESULT_V1:
  schema: REVIEW_ISSUE_RESULT_V1
  schema_version: review_issue_result/v1
  status: ok | failed
  body_sha256: <sha256>
  generated_at: <ISO 8601>
  generated_by: review-issue
  issue_url: https://github.com/<owner>/<repo>/issues/<番号>
  verdict: approve | needs-fix
  findings: []
  deterministic_checks: <checker JSON deterministic_checks をそのまま>
  blocking_issues: <checker JSON blocking_issues をそのまま>
  non_blocking_improvements: <checker JSON non_blocking_improvements をそのまま>
  diff_proposal: <checker JSON diff_proposal をそのまま>
  structured_blockers: <merge_readiness_into_review_result() で変換後の形状（機械処理用・code/finding_kind/deterministic_domain_key/blocking/checker_evidence を保持。status:human_judgment の readiness error はここに含まれない）>
  failure_class: <readiness status:human_judgment のときのみ contract_readiness_human_judgment、それ以外は省略またはnull>
  update_applied: true | false
  comment_url: <変更経緯コメント URL、適用時のみ>
```

## Contract（契約）

- 判定ロジックは checker を SSOT とする
- LLM は checker 結果を補完・再判定・上書きしない
- SKILL.md には C1〜C12 の詳細実装条件（regex・閾値・パターン）を重複記載しない
- 本 SKILL.md と checker の出力 schema が乖離した場合は checker を正とする

## Guardrails（安全策）

- VC を実装後の動作確認に使わない（baseline fail の構造を見るのみ。動作検証は `pr-review-judge` / `test-runner` の責務）
- 本文更新は `edit-issue` skill の controlled executor（`issue_content.update`）経由でのみ行い、本 skill から直接 Issue 本文を書き換えるコマンドを実行しない
- `approve` 判定時は `invoked_as_loop` の値に関わらず本文更新へ進まない
- `needs-fix` + `invoked_as_loop: true` の場合は `diff_proposal` だけ返し、本文更新を呼び出し元に委ねる
- 人間の明示的承認なく本文を書き換えない

## visual_impact フィールド（canonical schema owner）

レビュー対象 Issue 本文が `visual_impact` 関連フィールド（`VISUAL_IMPACT_DECLARATION_V1` 等）に触れる場合、
単一の canonical schema ファイル [`docs/dev/visual-impact.schema.json`](../../../docs/dev/visual-impact.schema.json)
を参照する。本 SKILL.md では独自にキー集合・enum を再定義しない（Issue #2019 AC26）。

## Related（関連ファイル）

- `.claude/skills/review-issue/scripts/check_issue_contract.py` — 決定論的判定エンジン（C1〜C12 の SSOT）
- `.claude/skills/review-issue/tests/test_check_issue_contract.py` — C1〜C11 の fixture-driven test
- `.claude/skills/review-issue/tests/test_check_issue_contract_c12.py` — C12 / warning 群 / C1 skeleton の fixture-driven test
- `.claude/skills/issue-contract-review/SKILL.md` — 着手直前の preflight（本 skill の次段）
- `.claude/skills/edit-issue/SKILL.md` — `needs-fix` 結果を本文に反映する手順
- `.claude/skills/issue-refinement-loop/SKILL.md` — Issue 改善ループ（本 skill を中で呼ぶ）
- [`.claude/skills/create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) — VC 作成 / Anchor Verification 等の共通ガイドライン
- `.github/ISSUE_TEMPLATE/implementation.yml` / `research.yml` / `parent.yml` — 必須セクションの SSOT

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`REVIEW_ISSUE_RESULT_V1` の全フィールドは必ず含める（routing 必須フィールド）。
