---
name: pr-review-judge
description: implementation child issue に紐づく PR をレビューし、linked issue の contract と PR diff / 証跡を照合して APPROVE / REQUEST_CHANGES / HUMAN_REVIEW_REQUIRED を判定する。verdict は `verdict` / `reviewed_head_sha` / `blockers` / `warnings` の最小 convention で呼び出し元へ返す（Issue #1873）。verdict コメントの GitHub 投稿は生の `gh pr review` を呼ばず、control-plane が通常の `gh pr comment --body-file` で行う。self-authored PR でも常に `event: COMMENT`。
disable-model-invocation: true
---

# PR Review Judge（PRレビュー判定）

> **移管状況 (I-4-4D5)**: 本 Skill は移管元 `squne121/loop-protocol` tag `pre-split-agent-platform-20261020` (commit `6fa07b6a0ab932a62f08db6a8d521c0dfded2075`) の同名 Skill を最小差分で移管したもの。差分は `docs/dev/i4-4d5-pr-review-consumers-manifest.json` の `intentional_diff` に記録している。
>
> - **一時 disable**: frontmatter に `disable-model-invocation: true` を設定している（Claude Code 公式 Skill 仕様では、Claude の自動 invoke と SubAgent への preload が抑止される）。したがって `pr-reviewer.md` の `skills: [pr-review-judge]` による preload は **まだ有効ではない**。これは意図的な deferred であり、本番稼働の証明ではない。動作検証と下記の未移管依存が揃うまで解除しない。
> - **no-dispatch / no-publish / 手動 read-only 限定有効化 (I-4-4D13, #111)**: 手動 `/pr-review-judge` は disable では防げない。D13 の owner decision により、**ユーザーが対話的 Claude Code で明示的に `/pr-review-judge <PR>` を手動 invoke した場合に限り**、下記「Manual Read-only Review (D13)」で単一の既存 PR の read-only レビューを実行する。それ以外の経路（Claude 自身の自動 invoke、SubAgent への preload、loop からの dispatch）は引き続き **実行しない**（fail-closed）。Reviewer SubAgent の dispatch と、verdict の GitHub 投稿（`gh pr comment` 等の GitHub 書込）も **実行しない**。手動 read-only 経路が動いたことは、自動 loop・Final Gate・本番稼働の証明ではない。
> - **未移管の hard dependency**（実レビュー手順が参照するが本 repository には存在しない）: `pr-review-judge/scripts/ci_verdict_summary_v2.py` / `wait_pr_checks.py`、`implement-issue` Skill、`gemini-cli-headless-delegation` Skill、`docs/dev/workflow.md`、`docs/dev/schema-governance.md`、`.github/pull_request_template.md`。後続の runtime 結線工程の担当である（owner / rerun_condition は manifest の `unmigrated_references`）。`ci_verdict_summary.py` / `check_pr_review_gates.py`（D6, #99）、`validate_pr_body.py`（D7, #101）、`open-pr` Skill（D11, #108）、`scripts/allowed_paths_review_gate.py`、`impl-review-loop/scripts/wait_ci_checks.py` は移管済みで、手動 read-only 経路が使うのは `allowed_paths_review_gate.py` / `check_pr_review_gates.py` / `ci_verdict_summary.py` / `impl-review-loop/scripts/wait_ci_checks.py` / `validate_pr_body.py` のみ。 旧 Claude Code hook `.claude/hooks/pr_reviewer_guard.py` は I-4-4D8（#103）の owner decision により再移管しない（`pr-reviewer.md` の `hooks` は撤去済み）ため、未移管の依存としては扱わない。hook がなくても Bash を持つ Reviewer が変更コマンドを技術的に実行できないとは主張しない。
> - 現行の正本は `AGENTS.md` / `docs/dev/github-ops.md`。本文中の旧 repository の Issue・PR 番号、`docs/dev/workflow.md`、ゲーム固有の例は移管元の履歴記述であり、現行の実行認可や正本ではない。

## Manual Read-only Review (D13)

**適用条件**: ユーザー自身が対話的 Claude Code に明示的に `/pr-review-judge <PR番号|URL>` を入力した場合だけ、この節で単一の既存 PR を read-only でレビューする。
`disable-model-invocation: true` は維持する。Claude が自分でこの Skill を起動しない。SubAgent（`pr-reviewer` / `pr-reviewer-lite`）へは dispatch も preload もしない。
入力が無い・複数 PR・PR が存在しない場合は何もせず停止する。これは **運用指示** であり OS／権限による隔離ではない。CLI の tool permission だけで書込が技術的に拒否されるとは主張せず、実行した呼出しの観測で no-write を示す。

**禁止（reviewer 実行中）**: `gh pr comment|edit|review|create|merge|ready|close|reopen|update-branch`、`gh issue comment|edit|close|create`、
`gh api` の `-X`/`--method` POST|PATCH|PUT|DELETE と `-f`/`-F`/`--input`（暗黙 POST）、`git push|commit|merge|rebase|reset`、リポジトリのファイル編集、
Bot／Broker／controlled `pr_*` route、追加認証、verdict の GitHub 投稿。Procedure の「0) Self-authored PR ガード」と「6) verdict 投稿」は **この経路では実行しない**。
読取（`gh pr view|diff|checks`、`gh issue view`、`gh api` の GET、`gh repo view`、`git fetch`（ローカル ref のみ）、`git diff|log|merge-base`）だけを使う。

**手順（M1→M8 の順に実行し、省略・並べ替えをしない）**。`<owner>/<repo>` は M1 で確定した値のみを使う。

1. **M1 repo／PR identity**: `gh repo view --json nameWithOwner` と `gh pr view <PR> --json number,url,state,isDraft,isCrossRepository,headRefName,headRefOid,baseRefName,baseRefOid,body,closingIssuesReferences` を取得する。PR URL の repo が現在の repo と異なる、`isCrossRepository: true`、`state != OPEN`、取得失敗はいずれも blocker（`foreign_repository` 等）。以降 `headRefOid` を `reviewed_head_sha` とする。入力の `reviewed_head_sha` と異なれば stale として blocker。
2. **M2 linked Issue**: PR 本文の `Closes #N`／`Refs #N`（または `closingIssuesReferences`）から候補 `N` を取り、`gh issue view <N> --json number,state,body` を fresh 取得する。同一 repo の Issue か、`Outcome`／`Acceptance Criteria`／`Allowed Paths`／`Verification Commands` を持つ実装 Issue かを確認する。特定不能・不正・foreign は blocker。
3. **M3 reference evaluator**: 「1) Linked Issue を特定」の `validate_pr_body.py --evaluate-reference-policy` を実際に実行し `decision`／`body_verdict`／`reason_code` だけで判定する（reviewer が本文の文字列から推測しない）。`nonclosing_required` かつ A1/A2 のときは native auto-close risk check も実行する。facts を組み立てられない場合（`docs/dev/workflow.md` は未移管）、実行不能・出力不正・`fail_closed`・`body_verdict: block|repair` は blocker。
4. **M4 Allowed Paths**: live Issue の `## Allowed Paths` の path pattern を **そのまま** JSON 配列にし（省略形・説明文中の path を補完しない。解釈不能なら blocker）、`git fetch --no-tags origin "+refs/pull/<PR>/head:refs/remotes/pr-review/<PR>"` で head をローカルに取得した上で次を実行する。`status != ok`（`fail_closed`／`indeterminate`）は blocker。
   `uv run --locked python3 .claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py --pr-number <PR> --base-ref <baseRefName> --current-base-sha <baseRefOid> --head-sha <HEAD_SHA> --reviewed-head-sha <HEAD_SHA> --allowed-paths '<JSON>' --issue-number <N>`
5. **M5 head-scoped CI**: 「4) CI 証拠」の `wait_ci_checks.py`（節 4 の引数 `--required --interval 1 --timeout-seconds 1`、待機なし）を `--head-sha <HEAD_SHA>` で実行し、`CI_WAIT_RESULT_V1.status == passed` かつ `current_head_sha == <HEAD_SHA>` のときだけ pass。`pending_timeout`／`no_checks`／`skipped_only`／`failed`／`cancelled`／`head_sha_changed`／`gh_error`／出力不正はすべて CI 不明または失敗として blocker（待って APPROVE に変えない）。続けて `ci_verdict_summary.py` を実行し、`exit != 0` は blocker。
6. **M6 deterministic gate**: `check_pr_review_gates.py --rule all --pr-body <PR本文> --pr-head-sha <HEAD_SHA> --local-head-sha <HEAD_SHA> --issue-body <Issue本文>` を実行する（単独 rule は schema 検証で失敗する）。出力が得られない（exit 2）は blocker。`g4` の fail は blocker。`g1`／`g2`／`g6` が旧 CI artifact・旧 findings block・旧 `allowed_paths_gate` block を要求して欠落しただけの fail は legacy 入力の欠落であり、新しい判定器は作らず `warnings[]` に `legacy_gate_input_missing:<gate_id>` として記録する。旧 block が存在して内容が矛盾している場合は欠落ではなく blocker のままとする。M4／M5 が canonical な判定を担う。
7. **M7 diff と AC／evidence**: `gh pr diff <PR>` と変更ファイル一覧で実差分を読み、Issue の各 AC を PR 本文の達成状況と実差分・CI 証跡に照合する（「5) PR Evidence / AC の一致」「4.6b」の規則）。本文の自己申告だけを根拠にしない。scope 混入は blocker。immediate runtime AC の実 evidence が無い場合は未検証として `warnings[]` に残し、**このレビュー自身の出力をその PR の runtime evidence として数えない**。
8. **M8 head 再確認と出力**: `gh pr view <PR> --json headRefOid` を再取得し `reviewed_head_sha` と異なれば stale として APPROVE せず、最初から再レビューする（再レビューは 1 回まで。再び変われば `HUMAN_REVIEW_REQUIRED` で停止する）。続いて結果を呼び出し元の会話に返す。

**verdict**: blocker があれば `REQUEST_CHANGES`。blocker が無く、semantic な判断や未検証の runtime AC が残る場合は `HUMAN_REVIEW_REQUIRED`。`APPROVE` は M1〜M8 がすべて完了し、blocker が 0 件で、CI が current head で `passed`、reference／Allowed Paths が肯定的に確認でき、M8 で head が不変のときに限る。不明・stale・foreign・欠落証跡・script 未接続・予期しない出力から `APPROVE` を出さない。

**出力（会話内のみ）**: 人間可読の要約、M1〜M8 を実行順に並べた手順ログ（各 step で実行したコマンドの種類と結果）、実行した read-only 呼出しの一覧、最小 YAML ブロック（`verdict`／`reviewed_head_sha`／`blockers`／`warnings`）。新しい schema field・wrapper・script は追加しない。GitHub にもファイルにも書かない。

## Input（入力）

- `PR番号` または `PR URL`（必須）
- `reviewed_head_sha`（任意）

## Procedure（最小化版）

### 0) Self-authored PR ガード

`PR author == 実行アカウント` の場合でも、投稿は常に通常の `gh pr comment --body-file`・`event: COMMENT` 相当固定（`--approve` / `--request-changes` を意味する formal review event は生成しない。専用 semantic publisher は使用しない。詳細は「6) verdict 投稿」参照）。

### 1) Linked Issue を特定（reference の判定は entrypoint の結果で決める、Issue #2878）

linked Issue は PR 本文の `Closes #N` だけでなく `Refs #N` からも特定する。ただし「この reference が妥当か」「`Closes` が必要か」は reviewer が PR 本文の文字列から推測せず、
`open-pr` の単一 evaluator（`validate_pr_body.py`）の entrypoint 結果だけで決める。候補 Issue 番号 `N` は PR 本文が参照する `Closes #N` / `Refs #N`（または `closingIssuesReferences`）から取り、facts を gh で fresh 取得して実行する
（reviewer は Bash でファイルを書けないため、入力は process substitution で渡す。`decision_comment` は PR 本文に `Reference-Decision:` 行がある場合のみ `gh api repos/<owner>/<repo>/issues/comments/<id>` の `html_url` / `id` / `issue_url` / `author_association` / `body` を `url` / `id` / `issue_url` / `author_association` / `body` として渡し、無ければ `null`）。

```bash
uv run --locked python3 .claude/skills/open-pr/scripts/validate_pr_body.py --evaluate-reference-policy --body-file <(gh api repos/<owner>/<repo>/pulls/<PR番号> --jq '.body // ""') --linked-issue <N> --linked-issue-body-file <(gh api repos/<owner>/<repo>/issues/<N> --jq '.body // ""') --reference-facts-file <(jq -n --arg repo <owner>/<repo> --arg state <OPEN|CLOSED> --argjson pr <PR番号> --argjson comment <decision_comment JSON | null> '{repo:$repo,issue_state:$state,pr_number:$pr,decision_comment:$comment}')
```

出力は JSON object 1 つ（decision によらず exit 0）。読む field は `decision`（`closing_required` / `nonclosing_required` / `fail_closed`）、`level`、`reason_code`、`body_verdict`（`valid` / `repair` / `block`）、`body_reason` のみ:

- `reason_code`: `issue_closed` / `a1_explicit_decision` / `a1_decision_invalid` / `a1_decision_ambiguous` / `a2_contract_deferred` / `a3_close_ready` / `runtime_applicability_unresolved` / `facts_invalid`
- `body_verdict: valid` → linked Issue を `Closes` または `Refs` から特定したものとして扱い、`Closes` 不在だけを理由に `REQUEST_CHANGES` にしない（`nonclosing_required` では `Refs` が正しい reference）。Refs 経由でも linked Issue の `Outcome` / `Acceptance Criteria` / `Allowed Paths` / `Verification Commands` を取得し、AC / evidence を通常どおり評価する
- `decision: closing_required` かつ `body_verdict: repair`（`closing_missing`。根拠のない Refs-only）→ `Closes` 不足を blocker として `REQUEST_CHANGES`
- `body_verdict: block`（`closing_for_other` / `closing_forbidden` / `reference_missing`）→ blocker として `REQUEST_CHANGES`
- `decision: fail_closed`（A1 invalid / ambiguous、authority 不明、facts 不正）→ fail-closed。`blockers[]` に `reason_code` を記載して `REQUEST_CHANGES`（auto repair 前提にしない）
- entrypoint を実行できない、候補 Issue 番号が特定できない場合も fail-closed（`REQUEST_CHANGES`）

**native auto-close risk check（`decision: nonclosing_required`、level A1 / A2 のときだけ追加で実行する）**: `Refs` の本文は「本文は close しない」ことしか示さず、
GitHub の手動 closing relation（Development 欄）や、採用される merge message の closing keyword では Issue が close され得る。そこで本文の判定とは別に、同じ `validate_pr_body.py` の次の entrypoint を実行する
（evaluator の結果を再利用する pure 関数で、grammar は複製しない。facts は gh で fresh 取得して `<native facts JSON>` に渡す: `closing_relations` は PR の `closingIssuesReferences` の `number` と `repository`（`owner/name`）の list、
`closing_relations_complete` は取り切れたか、`merge_settings` は `gh api repos/<owner>/<repo>` の live な `allow_squash_merge` / `allow_merge_commit` / `allow_rebase_merge` / `squash_merge_commit_title` / `squash_merge_commit_message`、
`merge_method` は採用予定の方式（未定なら `null`）、`pr_title` / `pr_body` / `commit_messages`（`gh api repos/<owner>/<repo>/pulls/<PR番号>/commits --paginate` の full message の list）、`final_squash_message` は確定済みの title / body（無ければ `null`）。
exact key は `docs/dev/workflow.md` の「native auto-close risk check」が正本）:

```bash
uv run --locked python3 .claude/skills/open-pr/scripts/validate_pr_body.py --evaluate-native-auto-close-risk --body-file <PR本文ファイル> --linked-issue <N> --linked-issue-body-file <Issue本文ファイル> --reference-facts-file <facts JSON> --native-close-facts-file <native facts JSON>
```

- `status: clear` → 本文以外の自動 close 経路が無いと確認できた状態。このときに限り「`Refs` なので Issue は OPEN のまま維持される」とみなす
- `status: blocked`（`native_relation_present` / `effective_message_closing_keyword`）→ blocker として `REQUEST_CHANGES`。`blockers[]` に `reason_code` と `findings` の具体的な矛盾を記載する
- `status: fail_closed`（facts / 設定 / relation 取得が不完全）→ fail-closed。`blockers[]` に `reason_code` を記載して `REQUEST_CHANGES`
- `status: not_applicable`（A3 / CLOSED）→ 追加の判定なし
- この結果は review 時点のものである。人間は merge 画面で final squash message や relation を変更できるため、merge 直前に final message / final native relation に対して再実行する（または検証済み message を変更せず使う）ことを merge の最終 precondition とする。PR / Issue 本文の hash だけでは保証にならない（`docs/dev/workflow.md`）

`pr-reviewer-lite` の allow-list は `Closes #N` の存在を要求するため、`Refs`-bound PR（`nonclosing_required`）は lite の適用対象外であり、この `pr-review-judge`（Sonnet `pr-reviewer`）で扱う（`pr-reviewer-lite.md` 自体は変更しない）。decision table と grammar の正本は `docs/dev/workflow.md` の「PR reference と Issue close の分離」。

### 2) Mergeability 取得

**Issue #1856（evidence authority cutover, Phase 1）**: `gh pr view --json mergeable,mergeStateStatus` を authoritative source として直接利用する（TEST_VERDICT_MACHINE の有無に依存しない）。BEHIND ルーティングは `merge_state_status == "BEHIND"` のみで判定し、`route_loop_verdict_v2()` は TEST_VERDICT の `branch_behind_main` を参照しない。

判定（review criteria 自体は変更しない。Issue #1873 で変わるのは transport のみ）:

- `mergeable == CONFLICTING` または `merge_state_status == DIRTY` → actual conflict。reviewer の verdict に
  関係なく `route_loop_verdict_v2()` が conflict hard stop する（#1860 Owner Decision）。reviewer 自身は
  この状態でも通常どおり判定してよい（reviewer の verdict は routing の入力の一つに過ぎず、conflict
  hard stop は live mergeability から独立に決まる）
- `BLOCKED` / `UNSTABLE` / `DRAFT` / `UNKNOWN` は Git conflict ではなく、reviewer が単独でこれらを理由に
  `REQUEST_CHANGES` にする必要はない（#1860 Owner Decision / PR #1871 P0-3）。required checks・branch
  protection の未充足自体は current-head required-CI evaluator（4) 参照）が独立に評価する
- `BEHIND` は衝突 blocker としない。`BEHIND` の update_branch 対応は reviewer が
  自己申告せず、control-plane が live mergeability から
  直接検出して `route_loop_verdict_v2()` で `update_branch` action を合成する（`step-5-mergeability-handling.md` 参照）
- `MERGEABLE` で `CLEAN`/`HAS_HOOKS`/`UNSTABLE`/`BLOCKED`/`BEHIND` → 次ステップ（PR review 自体の
  APPROVE/REQUEST_CHANGES 判定は AC/evidence 品質で決める。mergeability だけを理由に判定を変えない）

### 3) VC 証拠ポリシー（PR_REVIEW_JUDGE_VC_EVIDENCE_POLICY）

**Issue #1856（evidence authority cutover, Phase 1）**: authoritative な証拠は以下の2系列のみ。`TEST_VERDICT_MACHINE` は advisory（non-authoritative）に降格した（詳細は `references/evidence-policy.md`）。

1. `CI_CHECK_RUN_SCOPED`（current-head, `expected_head_sha` / `check_run_id` 束縛）
2. exact head SHA + literal command SHA256 に束縛された独立実行 Issue VC
3. 補助報告は `PR_BODY_SELF_REPORT`（単独では APPROVE 不可）
4. `TEST_VERDICT_MACHINE`（advisory のみ。APPROVE の必須条件にも REQUEST_CHANGES 回避の根拠にもしない）

- APPROVE 禁止条件
  - `verification_skipped_count > 0`
  - `SKIP:` / `exit 77`
  - `_*_fallback: true` など fallback/偽装成功
  - `CI_CHECK_RUN_SCOPED` head が stale、または missing / skipped / neutral / cancelled / unknown-classification

### 4) CI 証拠

**Issue #1856（evidence authority cutover, Phase 1 / AC11・AC12）**: required
check の存在有無・pass/fail 判定の canonical evaluator は
`.claude/skills/impl-review-loop/scripts/wait_ci_checks.py`
（`gh pr checks --required --json` で live required set を取得し、CheckRun /
StatusContext 双方を評価対象に含め、no-checks/skipped を fail-closed にする）
であり、pr-review-judge・pr-reviewer-lite・impl-review-loop Step 4 の 3 経路が
これを共有する。`ci_verdict_summary.py` はこれに加えて log excerpt 抽出・
artifact 保存等の詳細 provenance 収集を行う拡張レイヤーとして使用する
（raw `gh pr checks` は原則不使用）。

```bash
HEAD_SHA=$(gh pr view <PR番号> --json headRefOid --jq .headRefOid)

# canonical required-CI evaluator（3経路共通）
uv run --locked python3 .claude/skills/impl-review-loop/scripts/wait_ci_checks.py \
  --repo <owner>/<repo> --pr <PR番号> --head-sha "$HEAD_SHA" --required \
  --interval 1 --timeout-seconds 1

# 詳細 provenance（log excerpt / artifact）
uv run --locked python3 .claude/skills/pr-review-judge/scripts/ci_verdict_summary.py --pr <PR番号> --repo <owner>/<repo> --expected-head-sha "$HEAD_SHA"
```

`wait_ci_checks.py` の `CI_WAIT_RESULT_V1.status` が `no_checks` / `skipped_only` /
`failed` / `cancelled` / `pending_timeout` / `head_sha_changed` のいずれかであれば
fail-closed で `REQUEST_CHANGES` blocker とする。

`ci_verdict_summary.py` の exit code:

- `exit 0`: 補助証拠可
- `exit 10`: blocker（required checks が 0 件の場合の `no_required_evidence` を含む。
  Issue #1856 AC11 により 0 件を `all_pass` として通過させない）
- `exit 20`: CI 未確定 blocker
- `exit 30`: stale_head_sha blocker
- `exit 40`: gh error blocker

いずれのスクリプトも不可用時は `gh pr checks --required` fallback を明記した上で停止判断。

### 5) PR Evidence / AC の一致

- AC coverage: linked issue の各 AC が PR本文の `## 受け入れ条件の達成状況` に `[x]/[ ] + 根拠`
- Allowed Paths 遵守: 変更ファイルが contract の Allowed Paths 内
- 検証コマンド結果: 証拠付きで結果記載
- scope 混入（無関係修正）: blocker
- immediate runtime AC: `## Runtime Verification Evidence` と artifact/log 証跡

### 4.5) `Schema Consumer Inventory` の有無と妥当性を判定

PR が schema を変更しうると判断される場合:

- `Schema Change Applicability`（`schema_change/not_schema_change`）を確認
- `Schema Consumer Inventory` の存在・consumer 差分・compatibility 決定を確認

`schema_change` / `uncertain` だが表記不足なら `REQUEST_CHANGES`。

### 4.6) Safety Claim Gate（Safety Claim の判定ゲート）

`.claude/skills/**`, 権限・サンドボックス系差分、または安全ワード/本文条件を満たすと safety-sensitive。

`Safety-sensitive` 判定になった場合は `Safety Claim Matrix` を必須とする。

`Not controlled` 列が非空の際は bounded な主張であること、証跡一致、必要 follow-up があることを確認。

### 4.6b) Enumerated / Exhaustive Claim Evidence Coverage（有限列挙・全称 Claim の証拠基準、Issue #2765）

AC または Safety Claim Matrix の Claim が有限集合について all / each / 全対応 / 非回帰などの exhaustive completeness を主張する場合（単なる例示列挙は対象外）、aggregate な suite/class の PASS 件数のみを直接 coverage evidence として受理せず、各列挙ケースについて **case identity AND relevant executable assertion が PASS していること** を確認する（詳細: `references/ac-evidence-checks.md`）。skip / xfail / 未実行は AC が明示的に許可しない限り direct evidence にしない。Safety Claim Matrix は claim-to-evidence coverage とし、Evidence が test の場合のみ同 rule を適用する（詳細: `references/safety-claim-gate.md`）。1ケースでも欠落する場合は current-head CI が green でも `REQUEST_CHANGES` blocker とする。この判定ステップへ実際に到達した場合、出力に `PR_REVIEW_JUDGE_EVIDENCE_COVERAGE_RULE_APPLIED` を含める。

### 4.7) Clean-Room Review（grounded_research / 認証 surface 変更）

grounded_research 関連（`.claude/skills/gemini-cli-headless-delegation/**` の
provider 呼び出し・fan-out・grounding evidence 検証を含む差分）または AGY / OAuth
等の認証 surface に関する変更の PR は、**security-boundary reviewer** と
**experimental-validity reviewer** の 2 名独立 parallel review を要求する
（Issue #1776; #1494 敵対的再監査 follow-up）。

- **security-boundary reviewer**: 認証境界・権限境界・secret 取り扱いの安全性を
  判定する。security-boundary reviewer には実装者（implementation-worker）の
  raw transcript を渡さない。渡してよいのは Issue contract、PR diff、
  test result manifest、experiment manifest、public-safe artifact hash の
  みに限定する（clean-room 制約）。実装者の推論過程・自己申告コメントを
  そのまま信頼材料にしない。
- **experimental-validity reviewer**: 実験手順・evidence binding・causal claim
  の妥当性を判定する。`validate_agy_grounding_evidence.py` の
  `AGY_GROUNDING_EVIDENCE_VERDICT_V1`（`unsupported_claims[]` を含む）を
  一次情報源として使う。
- 2 名の判定が食い違う場合は `REQUEST_CHANGES` を優先する（fail-closed）。
- clean-room 制約（raw transcript 非共有）の遵守は verdict コメント内に
  明記し、merge-blocking な監査証跡として扱う。

### 5) verdict 決定

- blocker あり → `REQUEST_CHANGES`
- blocker なし → `APPROVE`

Issue #1873 以降、機械的に対応可能な不備（entrypoint が `closing_required` / `body_verdict: repair` を返した場合の `Closes` 不足、PR body hygiene 欠陥等。`nonclosing_required` の `Refs` は不備ではない）を
`required_auto_actions` という専用構造化フィールドで自己申告しない。これらは具体的な内容を
`blockers[]` に記載した上で `REQUEST_CHANGES` を返す（`references/required-auto-actions.md` 参照）。
`BEHIND` の update_branch 対応も reviewer が申告せず、control-plane が live mergeability から
直接検出する。

`Safety Claim Matrix` と `Schema Consumer Inventory` の欠落は blocker として扱う。

mergeability（`mergeable` / `merge_state_status`）は verdict に含めない。control-plane が
`gh pr view` で直接取得し、`route_loop_verdict_v2()` の判定に使う。`verdict == APPROVE` は
reviewer 側の判定であり、実際にループを終了できるかどうか（旧 `merge_ready` 相当）は
`route_loop_verdict_v2()` が live mergeability から決定する終端条件である。

- `Draft PR` 自体は blocker ではない。`DRAFT` 状態は `route_loop_verdict_v2()` が
  `fail_closed`（defer to current-head CI evaluator。人間 escalation は自動で発生しない）として扱い、
  impl-review-loop はループを終了しない（#1860 Owner Decision / #1873 Delivery Rule: PR は Draft の
  まま人間の最終マージ判断へ残す設計であり、Draft 自体を human escalation の理由にしない）。

### 6) verdict 投稿

pr-reviewer（本 SubAgent）は `Edit`/`Write`/`MultiEdit` を持たず、Bash 経由のファイル書き込みも禁止されている（`disallowedTools`）。

- pr-reviewer は verdict 本文（人間可読 Markdown + 最小 YAML ブロック）と `verdict` / `reviewed_head_sha` / `blockers` / `warnings` を構造化出力として **呼び出し元（impl-review-loop control-plane）に返すのみ**。
- 呼び出し元（Write ツールを持つ trusted orchestrator）が、pr-reviewer の返した本文テキストをそのまま通常の `gh pr comment --body-file` で投稿する（専用 semantic publisher は使用しない）:

```bash
gh pr comment <PR番号> --repo <owner>/<repo> --body-file <本文テキストのパス>
```

- 投稿前後に `gh pr view --json headRefOid` で head をリードバックする。投稿後に head が変化していた場合は
  その投稿を stale note として扱い、fresh review を実行する（Safety Invariants）。
- self-authored でも常に `event: COMMENT` 相当の通常コメント投稿（`gh pr review --approve` / `--request-changes` は使わない）
- 生の `gh pr review` を直接呼び出してはならない（reviewer と呼び出し元の間の behavioral workflow contract。`local_main_branch_guard.sh` は現在 `.claude/settings.json` の `hooks.PreToolUse` に wiring されておらず（PR #1691, commit `f971a95d`）、この契約の technical enforcement authority ではない。command/routing/permission/prohibition の詳細は本 SKILL.md が owner — #1732）

## Output Contract（出力契約、Issue #1873 最小 convention）

最小に必要な fields:

- verdict 値: `verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED`
- レビュー対象 head: `reviewed_head_sha`
- blocker 一覧: `blockers[]`
- 任意の補足: `warnings[]`

`merge_ready` / `mergeability` / `required_auto_actions` / `allowed_paths_gate` は出力に含めない
（新規の同等 schema field も追加しない。AC13）。`recommendations`（camelCase）も出さない。

### consumer_inventory（消費先一覧）

- `impl-review-loop` が本 `pr-review-judge` の出力（`reviewer_verdict`）を受け取り、
  control-plane が別途取得した `live_mergeability` と合わせて `route_loop_verdict_v2()` に渡す。
  `route: approved` の場合にループを終了する。
- Allowed Paths / contract 監査結果は `LOOP_VERDICT_V2.allowed_paths_gate` という専用フィールド
  ではなく、`status != ok` の場合に具体的な違反内容を `blockers[]` へ記載する形で受け渡す
  （`references/allowed-paths-gate.md` 参照）。
- production consumer（`route_loop_verdict_v2(reviewer_verdict, live_mergeability)`、2引数のみ。
  action は router 自身が単一合成する）の挙動は、待機条件（stale な issue 参照）ではなく、
  実装（`.claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py`）の以下 10 分岐で固定する
  （AC11。production fixture: `impl-review-loop/tests/fixtures/step5_routing_consumer/
  test_route_loop_verdict_v2.py` および `test_route_loop_verdict_v2_merge_state_status_only.py`）:

  1. `mergeable: CONFLICTING` → `conflict_hard_stop`（reviewer verdict に関係なく最優先）
  2. `merge_state_status: DIRTY` → `conflict_hard_stop`（同上）
  3. `verdict: HUMAN_REVIEW_REQUIRED` → `route_human_escalation`
  4. `verdict: REQUEST_CHANGES` → `continue_loop`
  5. `verdict: APPROVE` かつ `blockers` 非空 → `fail_closed`
     （reason_code: `approve_with_blockers_inconsistent`）
  6. `verdict: APPROVE` かつ `reviewed_head_sha` が live head と不一致 → `route_stale_head_rereview`
  7. `verdict: APPROVE` かつ `mergeable: UNKNOWN` または `merge_state_status: UNKNOWN`
     → `fail_closed`（reason_code: `mergeability_unknown`）
  8. `verdict: APPROVE` かつ `merge_state_status: BEHIND` → `route_to_update_branch`
     （`update_branch` action を router が合成、`rerun_required` は verification/pr_review 双方 true）
  9. `verdict: APPROVE` かつ `merge_state_status` が `BLOCKED`/`UNSTABLE`/`DRAFT`
     → `fail_closed`（current-head required-CI / branch-protection evaluator へ defer。
     人間 escalation は自動発生しない）
  10. `verdict: APPROVE` かつ `merge_state_status` が `CLEAN`/`HAS_HOOKS` かつ `mergeable: MERGEABLE`
      → `approved`

### ALLOWED_PATHS_GATE_RESULT_V1（Allowed Paths 判定結果、正本移譲先: references/allowed-paths-gate.md）

PR review 後に `allowed_paths_review_gate.py` を使って changed files の契約違反を再計算する。
canonical source（live linked issue 本文の扱い）・`status`（`ok | fail_closed | indeterminate`）の
意味・changed files source hierarchy・rename/copy provenance（`previous_filename`、Issue #1300）の
判定手順は `references/allowed-paths-gate.md` を正本とし、本節では複製しない。

## Output Constraint（OUTPUT_BUDGET_V1 出力制約）

出力上限は `docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` を遵守。

最小 convention のフィールド（`verdict` / `reviewed_head_sha` / `blockers` / `warnings`）は全て維持。

## Deterministic Processing Script の禁止事項（AC6）

`.claude/skills/pr-review-judge/scripts/` 配下の deterministic processing script（request
builder／validator／classifier／executor／renderer）は以下を行わない:

- semantic findings（コード品質・設計判断等の主観的知見）の自動生成
- `gh pr review` / `gh issue edit` 等の GitHub mutation の直接実行
- publisher（`open-pr` skill）が所有する hash／identity／TOCTOU gate の再実装
- test 専用の shadow implementation（pytest 検出による分岐等）

script が計算するのは G1–G5（`references/deterministic-gates.md`）等の決定論的 gate 判定のみであり、
`verdict` 自体（APPROVE/REQUEST_CHANGES の意味判断）は reviewer_verdict（本 SKILL.md の Procedure）が
決める。

**semantic findings 禁止と gate 集約の区別**: `check_pr_review_gates.py` の `finalize_verdict()`
（G1–G5 の機械的 gate boolean から `verdict: APPROVE/REQUEST_CHANGES` を決定論的に集約する既存関数）
は、上記で禁止する「semantic findings（コード品質・設計判断等の主観的知見）の自動生成」の対象では
ない。`finalize_verdict()` は LLM 呼び出しや外部 mutation を一切含まない純粋な boolean 集約
（`any(gate.status == FAIL for gate in gates)` の要約）であり、決定論的 gate 判定の範囲内に留まる。

## Reference Loading Map（読取条件）

- `references/evidence-policy.md`: 証拠優先度、`PR_BODY_SELF_REPORT_ONLY_APPROVE_PROHIBITED`、APPROVE 禁止条件。
- `references/ci-verdict-summary.md`: `ci_verdict_summary.py` の判定規則。
- `references/ac-evidence-checks.md`: AC coverage、Allowed Paths、runtime evidence、placeholder 判定、finite/exhaustive claim の case-level evidence coverage（Issue #2765）。
- `references/schema-consumer-gate.md`: schema_change_applicability と `Schema Consumer Inventory` 判定。
- `references/safety-claim-gate.md`: safety-sensitive 判定と `Safety Claim Matrix` 要件、claim-to-evidence coverage（Issue #2765）。
- `references/loop-verdict-v2-schema.md`: 最小 convention の必須フィールド。
- `references/allowed-paths-gate.md`: `ALLOWED_PATHS_GATE_RESULT_V1` の再計算手順。
- `references/required-auto-actions.md`: 機械的に対応可能な不備の blockers への反映方針。
- `references/verdict-output-template.md`: コメントテンプレート。
- `references/deterministic-gates.md`: G1–G5 の重要 gate。

## Verdict コメントテンプレート

````markdown
## Verdict: REQUEST_CHANGES

### Mergeability
- mergeable=<...>, merge_state_status=<...>

### Blockers
- "<issue summary>"

```yaml
verdict: REQUEST_CHANGES
reviewed_head_sha: "<HEAD_SHA>"
blockers:
  - "<issue summary>"
warnings: []
```
````

## Related（関連資料）

- 実装フロー: `.claude/skills/implement-issue/SKILL.md`
- 反復フロー: `.claude/skills/impl-review-loop/SKILL.md`
- reviewer agent: `.claude/agents/pr-reviewer.md`
- test runner agent: `.claude/agents/test-runner.md`
- PR template: `.github/pull_request_template.md`
- schema governance: `docs/dev/schema-governance.md`
