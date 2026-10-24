---
name: issue-contract-review
description: 人間承認後・実装着手直前に、Issue contract（作業計画・コンテクスト）が指定通りで開発フローに沿って AI Agent が安全に着手できるかを **決定論的**に preflight する skill。VC が baseline で fail することと AC が決定論的に検証可能であることを確認する。Issue 内容・文脈レビューは review-issue / issue-refinement-loop の責務で、本 skill では扱わない。
---

# Issue Contract Review

実装開始前の **preflight skill**。Issue 本文・コメント・VC・AC を読んで `implement-issue` へ渡せるかを判定する。

## Input（入力）

- `Issue番号` または `Issue URL`（必須）

## Procedure（最小化版）

### 1) Issue contract の取得

```bash
# issue 本文・コメント・ラベルを取得
gh issue view <番号> --json title,body,labels,comments
```

### 2) 開発フロー適合性チェック

template 準拠、`Allowed Paths`、`Verification Commands`、Stop Conditions（implementation の場合）を確認する。

GitHub Issue label（`state/needs-human` を含む全 label）は presentation-only の
non-authoritative metadata であり、`CONTRACT_REVIEW_RESULT_V1.status`（go/blocked/human_judgment）
の算出には一切使わない（#2084 OWNER directive）。readiness authority は Issue native
open/closed state、Machine-Readable Contract、GitHub native dependency close 状態、
explicit operator/OWNER directive、review/test/CI 結果である（#2084 Outcome の
Readiness Plane 定義を参照）。label を `checks.state_label` として observational
telemetry に残す場合も、それは presentation 用の観測情報に限定し `status` の判定に
寄与させない。

- Allowed Paths/VC が空のままなら BLOCKED
- Stop Conditions は implementation のみ必須

詳細は `references/contract-compliance.md`

### 3) blockers / dependency を決定論的検証

```bash
bash .claude/skills/issue-contract-review/scripts/check_blockers.sh <issue_number> <owner>/<repo>
```

- native dependency API が主情報源
- `Depends on #N` fallback は native 取得不可時のみ
- native と fallback 不一致は human escalation

### 3.5) Product Spec Preflight（必要時のみ）

トリガー条件（docs/product / tasks.md / .specify / generated task / `Product Spec Context`）で `check_product_spec_contract.py` を実行。

### 4) VC preflight（検証コマンド事前確認）

`baseline_vc_preflight.py` で AC 別 VC を AC1..N に抽出・実行し、scope/カテゴリ/判定を記録。

`pnpm build` regression gate は `shell=False` のまま runner-side fixed env delta `{CI:"true"}` で実行する。Issue body 側で `CI=true pnpm build` や `env CI=true pnpm build` のような shell/env prefix workaround を許可しない。

`## Verification Commands` では ` ```bash ... ``` ` の **fenced bash** を **canonical VC format** とし、各コマンドを
`$ <command>` + `# ACn` 形式で記載する。

VC 実行前に静的に弾くカテゴリ:

- `unsupported_shell_syntax` : `$(...)` / backtick / `${...}` を含む場合
- `unsafe_command` : `rm` / `git push` / `curl` など危険コマンド
- `command_not_allowed` : allowlist 外コマンド
- `package_manager_no_tty_prompt` : pnpm no-TTY prompt 由来の tooling/env blocker。`body_author_fixable=false` / downstream bucket `env_or_runtime`

`uv` コマンドの allowlist は、`issue #1195` の scope で次を許可する（これ以外は blocked）。

- `uv lock --check`（`--check` 以外の `uv lock` 系は不許可）
- `uv run --isolated --locked --no-default-groups python scripts/ci/runtime_dependency_smoke.py`
- `uv run --isolated --locked --no-default-groups python3 scripts/ci/runtime_dependency_smoke.py`

上記 runtime smoke は `python -c` / `python -m` / 追加 `uv` オプション / 非 `scripts/ci/runtime_dependency_smoke.py` script / `../` / 絶対パスを伴う場合は `command_not_allowed`。

`unsupported_shell_syntax` は `run_command()` を呼ばない前提の必須カテゴリ。

- コマンドは「`$ <command>` + `# ACn`」形式（verbatim）
- baseline fail が期待される項目は `go` 可
- `preflight-scope` marker は `baseline_fail_expected / pr_review_only / runtime_only` を制御

### 4.5) 動作検証 AC 前提チェック

`Runtime Verification Applicability` の有無と実装前提（対象 AC、環境要件、SKIP 規約、証跡要件）を確認。

### 5) AC 検証可能性チェック

- AC はチェックボックス `- [ ]`
- AC 番号と VC 番号の一致
- VC は決定論的（exit code / 数値比較）
- `AC <ACN>` と VC 内 `# ACn` 一致

### 6) Worktree / Branch preflight（作業ツリー・ブランチ事前確認）

`git worktree list` と branch 競合をチェック（`implement-issue` が使う worktree/branch の命名衝突確認のみ。本 skill 自身は worktree を作らない）。`worktree_branch_collision`（命名衝突）は `declared_path_overlap`（OPEN PR の changed-file 名重複）とは別 check であり、混同しない。

### 6.5) declared_path_overlap（OPEN PR changed-file 名重複、advisory のみ、#1680）

`scripts/declared_path_overlap.py` の `compute_declared_path_overlap_for_issue()` が、対象 Issue の Allowed Paths と OPEN PR の changed-file 名（`gh pr diff --name-only` 相当）の単純な重なりを **advisory のみ**で記録する。この check は実 Git merge 競合（3-way merge・hunk 競合・rename/delete 競合）を評価・証明しない。単独では `blocked` にしない — `declared_path_overlap` の結果は `run_contract_review_once.py` の `status`（go/blocked/human_judgment）に一切影響しない。

OPEN PR 一覧の取得は完全性契約（completeness contract）を持つ:

- `state`: 常に `open` 固定
- `base_ref`: 各 PR エントリが自身の base branch を明示的に保持する
- `draft_policy`: `include_drafts`（draft PR も inventory に含める）
- fork PR は `head_repository_owner` / `head_repository_name` / `head_ref_oid` / `is_cross_repository` で識別する
- GraphQL cursor pagination で `hasNextPage` が `false` になるまで取得し、`totalCount` と `fetched_count` を cross-check する
- `complete: true|false` フラグを持ち、safety cap（`limit`）に到達しても `hasNextPage` が残る場合は `saturated: true` として `complete: false` に倒す（全件性を証明できない場合は complete を騙らない）

実 Git merge 競合の blocking 判定（`pairwise_merge_conflict`）は本 skill の責務ではない。commit/PR 作成後にのみ評価可能なタイミングで呼ばれる独立 producer（Issue #1792、`PAIRWISE_MERGE_OBSERVATION_V1`）と、その呼び出し元配線（Issue #1793）に分離されている。`issue-contract-review` は worktree/branch/PR を作らず `go` で `implement-issue` に処理を渡して終了する preflight であるため、commit/PR 作成後にのみ評価可能な blocking 判定をこの preflight に混ぜることはできない。

OPEN Issue 重複そのものの再判定は行わない。重複判定は `issue-refinement-loop` の責務であり、`issue-contract-review` は relation / priority を**再判定しない**。OPEN Issue duplicate 候補が存在するだけでは `blocked` にしない。

### 7) 実行結果の出力

`CONTRACT_REVIEW_RESULT_V1` を `go | blocked` で返却し、`go` の場合のみ `implement-issue` へ handoff。`generated_at` を含む freshness 判定（contract snapshot / body hash / generated_at）は本 Issue（#1680）で変更しない — 既存挙動のまま維持する。

## Output Contract（最小）

### status: `go`（判定: go）

- Contract Snapshot（issue body 参照情報）をコメント投稿。**Issue #1284**: この投稿は issue-specific worktree を作らず、root/default branch から controlled metadata executor（`scripts/agent-guards/controlled_skill_mutation_exec.py --command-id contract_snapshot.publish`）経由で実行する。raw `gh issue comment` / `gh issue edit` は本 skill の production path では使わない
- `implement-issue` へ handoff 可能

### status: `blocked`（未達で差し戻す判定）

- 不足理由・不足項目を列挙してコメント
- `issue-refinement-loop` へ委譲

`contract_snapshot` には少なくとも次を含める。

- `Outcome`
- `Acceptance Criteria`
- `Verification Commands`
- `Allowed Paths`
- `Worktree / Branch`
- `VC preflight` 集約

## Programmatic Entry Points (scripts)（プログラム経由の入口）

`run_contract_review_once.py`（#817 追加）

- `--mode static|execute`
- `CONTRACT_REVIEW_ONCE_RESULT_V1` を返す

`contract_review_result_parser.py`（#817 追加）

- `CONTRACT_REVIEW_RESULT_V1` をコメントから解析（issue-comment / issue-body）

## Output Contract 実体

`CONTRACT_REVIEW_RESULT_V1` の全フィールドは残す。

`checks` には少なくとも以下を含める。

- `template_compliance`（テンプレ準拠）
- `state_label`（状態ラベル。observational telemetry のみ。`status` の go/blocked/human_judgment
  判定には寄与させない。#2084）
- `allowed_paths_present`（許可パスの有無）
- `vc_present`（検証コマンドの有無）
- `stop_conditions_complete`（停止条件の充足）
- `vc_preflight`（検証コマンド事前確認）
- `ac_verifiability`（受け入れ条件の検証可能性）
- `product_spec_check`（プロダクト仕様確認）
- `worktree_branch_collision`（作業ツリー・ブランチ衝突）
- `declared_path_overlap`（OPEN PR の changed-file 名重複、advisory のみ。単独では blocking にしない。#1680）
- `next_action`（次のアクション）
- `blocked_reasons`（ブロック理由）

## Schema Change Applicability（スキーマ変更適用性）

本 skill の `CONTRACT_REVIEW_RESULT_V1.checks` / `CONTRACT_REVIEW_ONCE_RESULT_V1.checks` へのフィールド追加は、原則として Stop Conditions（「In Scope の固定契約（キー集合・スキーマ・型定義）の変更が必要になった場合」）により一度停止する。ただし Issue #1680 は `declared_path_overlap` の追加を `additive_fields` として明示的に許可する（`unknown_fields_allowed_by_legacy_consumers: true`）。additive schema change は次を満たす場合に限り Stop Conditions と矛盾しない。

- 追加フィールドが既存フィールドの意味・型を変更しない
- 追加フィールドが `advisory` のみで `status`（go/blocked/human_judgment）の決定に影響しない
- 追加フィールドを消費しない既存 consumer が unknown field を無視できる（後方互換）

`pairwise_merge_conflict`（実 Git merge 競合、blocking）は本 Issue のスキーマ変更対象に含めない。別スキーマ（`PAIRWISE_MERGE_OBSERVATION_V1`、Issue #1792）に分離されており、本 skill の checks へは追加しない。

`vc_preflight` は各 classification を保持し、`scope_class`（baseline_fail_expected / regression_gate / pr_review_only / runtime_only）を含める。

## Handoff to implement-issue（go 時、実装 skill への引き継ぎ）

必須項目（最小）:
- Issue 番号
- `contract_snapshot_url`
- Outcome（成果物） / Acceptance Criteria（受け入れ条件） / Verification Commands（検証コマンド） / Allowed Paths（許可パス） / Required Skills（必要スキル）
- Worktree/Branch 命名（preflight で確定）

## Guardrails（安全策）

- Issue 本文の品質評価をしない（review-issue が担当）
- code edit は行わない（preflight 専用）
- branch / PR / worktree を本 skill で作らない
- `pnpm test` を含む回帰失敗は blocked
- `.claude/skills/**` や `.codex` を触る場合は Stop 条件で一度停止
- **Issue #1284**: Issue body / comment / Contract Snapshot の metadata mutation は
  issue-specific worktree ではなく controlled metadata executor lane
  （`scripts/agent-guards/controlled_skill_mutation_exec.py`、command id
  `contract_snapshot.publish` / `issue_body.update` / `issue_comment.publish`）から
  root/default branch で実行する。raw `gh issue edit` / `gh issue comment` を本 skill
  の production path で直接呼ばない。`local_main_branch_guard.py` の既存 raw gh
  allowlist は本 skill の成功経路の認可根拠として使わない（
  `worktree_scope_guard.py` の controlled executor exact command class allow が根拠）

## github_metadata_assert（GitHub metadata の安全な readback assertion）

GitHub milestone metadata（`description` 等）の forbidden phrase の有無を VC で検証する場合は、raw `gh api` を VC に書かず first-class な `github_metadata_assert` を使う。

- 許可される形: `github_metadata_assert <contains|not_contains> description <literal> repos/<owner>/<repo>/milestones/<number>`
- コマンドは4引数ちょうど（assertion_type field literal endpoint）。field は `description` のみ（typo・未知 field は reject）。余分な positional / flags は一切受け付けない
- 内部実行は固定 argv `gh api --method GET repos/<owner>/<repo>/milestones/<number>`（method GET 固定・非 mutating）
- endpoint は milestone のみ（絶対 URL・query string・path traversal・placeholder は reject）
- 危険な flags（`-f` / `-F` / `--field` / `--raw-field` / `--input` / `--header`(`-H`) / `--include`(`-i`) / `--paginate` / `--slurp` / `--cache` / `--template` / `--preview` / `graphql`）と mutating method（`--method POST/PATCH/PUT/DELETE`、`-X`）は block
- gh 不在 / auth 失敗 / 404 / rate limit / timeout / invalid JSON は environment error として `human_judgment` 分類になり、assertion の pass/fail（false pass）と区別される
- 禁止例: `gh api repos/owner/repo/milestones/1 --jq '.description'`（raw `gh api` は allowlist で block。jq は出力するだけで assertion にならない）
- 詳細は `references/vc-preflight.md` を参照。

## Reference Loading Map（読取条件）

- `references/contract-compliance.md`: テンプレ準拠 / state label / blocker / stop conditions の判定詳細。
- `references/product-spec-preflight.md`: PS001–PS006 テーブルと停止ルール。
- `references/vc-preflight.md`: baseline preflight スコープ、分類カテゴリ、分類ロジック。
- `references/runtime-verification.md`: immediate/deferred/not_applicable の判定と必須項目。
- `references/ac-verifiability.md`: AC/VC 検証可能性の詳細。
- `references/output-schema.md`: `CONTRACT_REVIEW_RESULT_V1` フィールド定義。

## visual_impact フィールド（canonical schema owner）

対象 Issue 本文が `visual_impact` 関連フィールド（`VISUAL_IMPACT_DECLARATION_V1` 等）に触れる場合、
単一の canonical schema ファイル [`docs/dev/visual-impact.schema.json`](../../../docs/dev/visual-impact.schema.json)
を参照する。本 SKILL.md では独自にキー集合・enum を再定義しない（Issue #2019 AC26）。

## Related（関連ファイル）

以下を参照する:
- `.claude/skills/issue-contract-review/scripts/*`
- `.claude/skills/review-issue/SKILL.md`
- `.claude/skills/implement-issue/SKILL.md`
- `.claude/skills/issue-refinement-loop/SKILL.md`
- `.github/ISSUE_TEMPLATE/implementation.yml`

## Output Constraint (OUTPUT_BUDGET_V1)（出力制約）

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約を遵守。
