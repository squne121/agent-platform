---
name: create-issue
description: ユーザーの要求を Terminal AI Agent が再現可能に作業できる GitHub Issue に整形するときに使う。要求分析・Scope 判定・Issue 本文生成・即時起票を行う。blocking stop（Scope 分割採否・Scope Overlap 3 択）以外は人間承認なしで `.claude/skills/create-issue/scripts/create_issue_txn.py` を実行する。「Issue 起票」「Issue 作って」「create issue」などの短文トリガーで使う。
---

# Create Issue

ユーザーの要求を分析し、Terminal AI Agent が安全・再現可能に着手できる GitHub Issue を生成するスキル。

## agent-platform 承認境界（Stage 2-5 / Issue #35。以下の本文より優先する）

この節は agent-platform への移管で追加した。下記の手順は source の記述を保持しているが、**記述があること自体は実行承認を意味しない**。

- 起票の write は `create_issue_txn.py` 経由のみ。`create_issue_txn.py` は `--repo` を `scripts/agent-guards/repo_identity.py`（`config/repository_identity.v1.json`）の trusted slug と完全一致で検査し、不一致・不明な設定では `gh` を 1 回も呼ばずに拒否する。
- **label 付与 / native sub-issue（parent）/ blocked-by / blocking は未承認の live 操作**。`--label`、`--parent-issue`、`--blocked-by`、`--dependency`、`--blocking`、body 由来の parent、`--issue-kind implementation`（標準 implementation label の自動付与）、`reconcile` サブコマンドの同入力は、すべて最初の `gh` 呼び出しより前に `unapproved-operation` で拒否される。起票だけ成功して後続の label 操作で失敗する部分適用は起こさない。
- **`--label-profile triage_only` は無ラベル起票ではない**。`_resolve_labels` が `triage-required` を必ず追加するため、同様に拒否される。label・parent・dependency なしの正規経路は `--label-profile standard`（既定）+ `--issue-kind` 無指定（kind は body の Machine-Readable Contract で宣言）+ 明示 label なし。
- 下の「sub-issue 登録手順（手動実行が必要な場合）」「Blocker / Blocked-by 設定手順」「label-readback 失敗後の reconcile 手順」の raw `gh api` / `gh issue edit` は承認されていない。agent-platform では実行しない。承認が出るまで、親子・依存関係は Issue 本文に書く。
- create の失敗（タイムアウト・receipt 喪失・gh 失敗後の書き込み済み）は `failure_stage: issue-create-outcome-unknown` を返し、authoritative な title readback の結果を `failure_message` に含める。「書き込みなし」とは断定しない。再実行前に readback で確認する。
- `check_issue_overlap.py` / `plan_child_materialization.py` / `materialize_child_issues.py` / `mutation_route_matrix.py` は Stage 2-6（#36）所有で未移管。未移管の間、4a・4b と overlap preflight は実行できない（`docs/dev/stage2-5-create-txn-manifest.json` の `dangling_references` に owner_stage と再実行条件を記録）。

## Procedure（手順）

### 0. テンプレートを読み込む（Issue Template Guard）

- Issue 種別（`parent` / `research` / `implementation`）を判定する
- タイトル先頭に `調査:` / `実装:` / `導入:` のどれが必要かを先に確認し、種別を誤らない
- 対応するテンプレートファイル `.github/ISSUE_TEMPLATE/{種別}.yml` を読み、Issue Forms の各 `textarea` の `label` を必須セクション一覧として取得する（テンプレ更新時に自動追従）
- 以降の本文生成はこの必須セクション一覧を基準にする

### 1. 要求を分析する

- ユーザーの要求から Outcome（達成したい状態）を抽出する
- **anchor 主張を含む Issue**: Issue 本文で「既存ファイルの行番号・セクション見出し・関数名」を anchor として主張する場合は、起票前に [`references/body-authoring.md`](references/body-authoring.md) の Anchor Verification Preflight を参照し、以下のスクリプトで一括検証してから起票する:
  ```bash
  # anchor_list.txt に 1 行 1 anchor を記載（# コメント行は無視される）
  .claude/skills/create-issue/scripts/verify-anchors.sh anchor_list.txt
  ```
  スクリプトは `git grep -lF -- <anchor>` を配列形式で実行し、PASS/FAIL を出力する。exit 1 が返った場合は起票しない
- **follow-up Issue の場合（post-merge-cleanup / issue-refinement-loop から委譲）**: [`references/body-authoring.md`](references/body-authoring.md) の「ワークフロー不具合検出時の修正方針起案ガイダンス」セクションを参照し、決定論的修正と workaround を明示比較してから Outcome を起案する
- 実装案だけでなく、運用で解決できる案も比較し、採用方針を明示する
- 要件が曖昧な場合は Issue を確定させず、`## Notes for Reviewer` に記載するか blocking stop として扱う（推測で埋めない）
- **タイトル prefix と AC の性質のセルフチェック**: `research` / `調査` を名乗る Issue に `src/` や `tests/` の実装変更が AC として入っていないか確認する。入っている場合は `implementation` / `実装` に切り替えるか、Scope を分割して別 Issue にする
- 不確実性が残る場合は `phase/research` / `state/needs-human` ラベルの付与要否を先に決める。implementation に昇格できる場合は `docs/dev/workflow.md` の implementation issue canonical contract を正本とし、`実装:` prefix + `phase/implementation` の consumer ready contract に沿って起票する（`state/queued` は deprecated のため付与しない）

#### desired destination handoff guard（委譲先ガード）

orchestrator（`issue-refinement-loop` / `post-merge-cleanup` 等）から follow-up 候補を受ける場合:

1. 各候補の `desired_destination` と `validated_scope_delta` を必須とする。どちらか欠落していれば `failure_reason: destination mapping required` で委譲元へ返す
2. 必須フィールドが揃っている場合は、Issue 本文の `## Background` または `## In Scope` に反映する
3. `desired_destination` をそのまま write-capable claim や scope 拡張に昇格させず、repo reality で validated な範囲だけを `## In Scope` に入れる

#### sweep / cleanup issue 起票前ガード

タイトル / Outcome / VC のいずれかに `sweep` / `cleanup` / `残存` / `repo 全体` のような repo-wide inventory シグナルがある場合は、起票前に **dry-run** を必須化する:

1. Issue 本文へ入れる予定の VC または同値の read-only コマンドを実行し、`remaining_count` と `sample_hits`（最大 3 件）を取得する
2. `remaining_count == 0` の場合は **新規 issue を起票しない**。「対象 0 件のため起票不要」と報告して終了
3. dry-run でヒットがあっても、同じ concern を扱う既存 open issue が見つかった場合は新規起票せず canonical destination（Issue 番号と comment URL）を返す
4. 起票中止時の報告は `decision` / `remaining_count` / `sample_hits` / `inspected_commands` / `canonical_destination` を含める

### 1.5. 類似 Issue のキーワードベース重複チェック（起票前検索）

- ステップ 1 で抽出した Outcome・タイトル案から代表キーワード（2〜3 語）を選ぶ
- 以下のコマンドで OPEN Issue を検索する:
  ```bash
  gh issue list --search "<keyword>" --state open --json number,title,url
  ```
- 結果の解釈:
  - **重複あり（同一・極めて近い Outcome の OPEN Issue がある）**: 即座に新規起票を中止し、人間に提示:
    ```
    [類似 Issue 検出] 以下の OPEN Issue が同一・類似の Outcome を持つ可能性があります:
    - #<number>: <title> (<url>)
    1. 既存 Issue (#<number>) に追記して対応する
    2. 既存 Issue を重複クローズ候補として指定し、新規 Issue に統合する
       → 新規 Issue に「Closes #<number>（重複）」を記載して起票
    3. scope が異なることを確認し、新規 Issue を起票する（理由を明記）
    ```
  - **canonical destination が確定している場合**: 3 択提示せず、`decision: route_to_existing_issue` と `canonical_destination` を返して停止
  - **false positive の除外**: GitHub Full-Text Search はトークン分割するため hit に false positive が含まれうる。タイトル・本文を目視確認して無関係のものは除外してから判定

### 2. Scope を判定する

「良い PR スコープ」は以下の基準で判断する。最初の 3 つは決定論的に判定可能、4 番目は人間判断が残る。

| 基準 | 判定方法 |
|---|---|
| **単一意図** | 変更ファイル群が 1 つの Outcome のためだけに必要 / 別 Outcome 用の修正が混入していない |
| **アーキテクチャ層のまとまり** | Allowed Paths が 1 つの層（`src/state` / `src/render` / `src/systems` / `src/data` のいずれか、または同一機能領域）に閉じている。複数層をまたぐ場合は層境界の変更そのものが Outcome である |
| **ロールバック単位** | 1 PR を revert すれば Outcome が完全に元に戻る（部分 revert が不要） |
| **AC の独立性** | 各 AC が他の AC に依存せず、相互に独立に検証可能 |

これらを満たさない場合は分割を提案する。複数に分かれる場合は、分割案と各 Issue の Outcome を提示して人間に確認する。

AC は検証可能な記述にし、実装か調査かを自分で確認する。研究寄りなら `調査:`、実装寄りなら `実装:` をタイトル先頭に置く。

### 2.5. `proposal_only` で Issue 本文案を受ける場合の境界

- Gemini CLI に下書きだけを委譲したい場合は、`gemini-cli-headless-delegation` の wrapper へ `tool_profile: proposal_only` と `output_sections: ["issue_authoring_draft"]` を明示する
- 返却された `issue_authoring_draft` は **proposal text** として扱い、そのまま GitHub に投稿済みの本文や確定済み outcome とみなさない
- final file edit / shell edit / GitHub mutation は Claude 側 worker または main thread が保持する
- request に `post_to_issue_url`、direct file edit、shell execution、GitHub mutation 指示が混ざる場合は fail-closed とし、caller 側で request を修正してから再実行する

### 2.6. Web 調査が必要な場合の経路

- 公式仕様・モデル既定値・provider 側の既知挙動などの grounded research は `gemini-cli-headless-delegation`（`tool_profile: "grounded_research"`、`timeout_sec >= 300`）を default 経路とする
- Claude 直接生成は fallback 経路とし、preflight 失敗 + 明示承認・既存コンテクスト充足のみで結論できる場合のいずれかに該当する場合のみ採用する
- repo 実体確認で確定可能な論点は `rg` / `find` で先に確認し、grounded_research を起動しない（トークン抑制）

### 3. Issue 本文を生成する

ステップ 0 で取得した必須セクション一覧に従い、implementation 種別では以下をすべて含める:

- `## Parent Issue` — 親 Issue 番号（なければ「なし（単独改善）」と明記）
- `## Machine-Readable Contract` — issue 種別ごとの required key を持つ YAML block
  - implementation / research: `contract_schema_version` / `issue_kind` / `parent_issue` / `goal_ref` / `change_kind`
  - parent: `contract_schema_version` / `issue_kind` / `goal_ref` / `change_kind` / `parent_mode` / `closure_mode`
  - 本文更新時は block 全体を削除せず、値だけを必要最小限で更新する
  - parent issue では `parent_mode` を `delivery-rollup` / `quality-gate` / `routing-map` / `decision-log` から選ぶ
  - `closure_mode` は `child-complete` / `measurement-ready` / `quality-validated` / `routing-complete` / `decision-recorded` の closed enum から選び placeholder のまま確定しない
  - `parent_mode` と `closure_mode` の互換は `delivery-rollup → child-complete` / `quality-gate → measurement-ready | quality-validated` / `routing-map → routing-complete` / `decision-log → decision-recorded` に固定する
  - `quality-gate` parent では `## Quality Decision Record` と `## Parent Closure Rule` を本文に残す
  - `<required: ...>` placeholder や enum 外値は missing と同様に invalid とし Issue 本文を確定させない
- `## Parent Goal Ref` — 親 Issue の `## Goal` または `## Outcome` を 1 回の読解で追える参照面
- `## Current Validated Scope` — parent tracker で narrow 済みの validated な作業範囲（今回の child issue で write-capable に扱う範囲だけ）
- `## Remaining Parent Gaps` — この child issue 完了後も parent に残る gap、または follow-up で追う項目
- `## Outcome` — 達成したい状態（1 文で明確に）
- `## In Scope` — 今回の PR で行うこと
- `## Out of Scope` — 今回の PR では行わないこと
- `## Acceptance Criteria` — チェックボックス形式の検証可能な条件
- `## Verification Commands` — **必須**。各 AC に対応するターミナルで実行可能なコマンドを列挙。`grep -n` / `rg -n` / `pnpm typecheck` / `pnpm test` 等の単一コマンドを AC ごとに 1 エントリで記載する（[`references/body-authoring.md#VC_SINGLE_COMMAND_GUARDRAIL`](references/body-authoring.md#VC_SINGLE_COMMAND_GUARDRAIL) 参照）。コマンドが 1 つも記載されていない Issue は不完全とみなす
- `## Allowed Paths` — 変更してよいファイル・ディレクトリの完全パス
- `## Stop Conditions` — **必須**。`.github/ISSUE_TEMPLATE/implementation.yml` の Stop Conditions セクションに記載された 6 定型項目をプレースホルダを埋めて記載
- `## Scope Delta（該当時のみ記載）` — Allowed Paths と実作業の乖離が生じた場合のみ
- `## Required Skills` — **ドメイン知識スキル**のみを記載する（例: TypeScript / ECS / Canvas / Vitest BDD 等）。ワークフロー skill（`issue-contract-review` / `implement-issue` / `pr-review-judge` / `ssot-discovery` 等）は書かない。runtime dependency がない場合は「なし」と明記するか省略
- `## Delivery Rule` — `1 Issue = 1 PR`、worktree 指定、Draft PR 既定など

#### VC 作成の決定論的ルール

> **VC_SINGLE_COMMAND_GUARDRAIL**: VC は shell control operator（&&, ||, |, ;, &）を使わない単一コマンドで記述する。詳細は [`references/body-authoring.md#VC_SINGLE_COMMAND_GUARDRAIL`](references/body-authoring.md#VC_SINGLE_COMMAND_GUARDRAIL) を参照。違反検出は `uv run python3 .claude/skills/create-issue/scripts/verify_vc_single_command_guardrail_docs.py --strict` で行う。

- **削除確認パターン**: `rg --files-without-match --fixed-strings -- "<literal>" <file>` を使い、literal が file に存在しなければ exit 0 を成功とする（`--` は option parsing の終端で、`--settings` のように `-` で始まる literal を pattern として扱うために必須。1 command = 1 concrete file、`-q` は併用しない。`-c` / `--count` は no-match で exit 1 を返すため削除確認に使わない。`grep ... && echo PASS || echo FAIL` 形式も禁止。詳細は [`references/body-authoring.md`](references/body-authoring.md) の「削除確認パターン」を参照）
- **marker 単位の独立確認**: 1 つの AC に複数の marker を使う場合、marker ごとにヒット件数を個別検証
- **決定論的判定**: `grep` / `rg` の exit code、`diff` の exit code、`pnpm test` の exit code、`test -f` / `test -d`、ファイルサイズ・行数の数値比較
- **意味的評価は VC に書かない**: 「コード品質の正当性」「算出値の妥当性」等は PR レビュアーの責務

#### Issue Template Guard（テンプレート検証、fail-closed）

本文ドラフト完成後、ステップ 0 の必須セクション一覧と照合する。不足セクションがあれば `[Issue Template Guard] Missing sections: <セクション名一覧>` を出力して Issue 生成を中断する。Stop Conditions セクションが空欄・1 項目のみの場合も不完全とみなす。

#### Machine-Readable Contract Guard（契約検証、fail-closed）

`## Machine-Readable Contract` がない、または issue kind ごとの required key が欠ける場合は `[Issue Template Guard] Machine-Readable Contract keys are incomplete` を出力して中断する。

#### Required Skills Guard（必須スキル検証、fail-closed）

`## Required Skills` を書いた場合、各 bullet を以下の順で分類し、1 つでも違反があれば中断する:

1. **暗黙ワークフロースキル検出**: `issue-contract-review` / `implement-issue` / `pr-review-judge` / `ssot-discovery` などが含まれていたら `[Issue Template Guard] Required Skills contains implicit workflow skills` を出力して中断
2. **document / path reference 検出**: `docs/` / `.md` / `/` を含む repo path が含まれていたら `[Issue Template Guard] Required Skills contains document or path references; move them to ## Background or ## In Scope` を出力して中断
3. **canonical skill 名の照合**: current skill inventory にある canonical skill 名（bare の system skill と namespaced plugin skill の両方）はそのまま許容
4. **repo-local skill existence 確認**: bare skill 名を書く場合は `.claude/skills/<skill-name>/SKILL.md` の実在を確認。存在しない場合は中断
5. **runtime dependency なしの明示**: なければ「なし」へ正規化

### 3.5. Outcome Quality Guard（成果物形式・完了条件確認）

生成した Outcome が以下 2 要素を含むか確認する:

1. **成果物形式**: 何が出来上がるか（更新されたファイル、追加されたコミット、作成された PR、close 済み Issue など）
2. **完了条件**: 何をもって完了とするか（検証可能な状態）

**不適合パターン**（動作状態のみで成果物形式欠落）:
- 「〜が決定される」「〜が整理される」「〜が完了する」「〜が明確になる」「〜を検討する」「〜を改善する」

**判定基準**:
- **適合**: 成果物形式と完了条件の両方が明確。または受動的状態記述であっても具体的な成果物（ファイルパス・Issue 番号・PR 番号・コミット・リリース等）への参照が伴う
- **不適合**: 動作状態のみで「何が出来上がるか」「何をもって完了とするか」が曖昧

**不適合時**: `[Outcome Quality Guard] Outcome に成果物形式・完了条件が不足しています: <抽象表現>` を出力して中断、人間に具体的な書き換え案を提示する。

### 3.6. Allowed Paths ベースの類似 Issue 重複チェック（scope 重複チェック）

確定した `## Allowed Paths` の各パスについて OPEN Issue が存在するか確認する:

```bash
gh issue list --search "<file_path> is:open" --state open --json number,title,url
```

- OPEN Issue が 1 件以上 → false positive 除外を先に行い、本文への literal 含有を確認:
  ```
  gh issue view <N> --json body | uv run python3 -c "import json,sys; b=json.load(sys.stdin)['body']; print('found' if '<file_path>' in b else 'not found')"
  ```
- literal 含有ありで scope 重複あり → 3 択（分割/依存明記/統合）を Issue コメントまたは起票時の本文注記として提示した上で起票を継続する（#1860 Owner Decision: declared Allowed Paths の文字列重複は起票停止理由にしない。実 Git conflict は worktree/PR 作成時に別途検出される）
- scope 重複なし → 重複なし旨を記録して次へ

上記の gh ベース手動チェックに加え、title keyword search だけに依存しない決定論的判定として overlap preflight helper `.claude/skills/create-issue/scripts/check_issue_overlap.py` を使う。

実行例（#1869 fix_delta P0-3: canonical invocation から `--fail-on-unsafe` を除去。全 decision が advisory であるため、shell 側を hard-fail させるフラグを canonical command に含めない）:

```bash
uv run --locked python3 .claude/skills/create-issue/scripts/check_issue_overlap.py \
  --repo <owner/repo> --title "<起票予定 title>" \
  --goal "<goal_ref>" --allowed-paths-file <paths.txt> \
  [--label <l> ...] [--parent-ref <#N> ...] [--depends-on <#N> ...]
```

`--fail-on-unsafe` フラグ自体はスクリプトに残すが（他の呼び出し元の後方互換性のため）、本 SKILL.md の canonical invocation では使用しない。`set -euo pipefail` 下で本コマンドを実行しても、`overlap_requires_comment` / `ambiguous_requires_human` / `duplicate` の decision でシェルが中断しない。

helper は `ISSUE_OVERLAP_PREFLIGHT_RESULT_V1`（`decision` / `reason_code` / `policy_class` / `source_status` / `candidates[].matched_fields` / `comment_template`）を返す。`decision` 別の対応（#1860 Owner Decision により全 decision advisory。完全一致タイトルの exact dedupe のみ実質的な採否判断に使う）:

- `safe_new_issue` → 起票続行。
- `overlap_requires_comment`（C1/C2a）→ `comment_template` を新規 Issue に記録して起票。
- `ambiguous_requires_human`（C2b/C3 / source degraded）→ warning として記録し起票を継続する。完全一致タイトルの既存 OPEN Issue を実際に発見した場合のみ、それを再利用しリンクする（重複起票を避ける）。
- `duplicate` → 完全一致または明確な重複と判断できる場合は既存 Issue を再利用しリンクする。断定できない場合は warning として記録し起票を継続する（統合要否は人間へ提案するに留め、起票自体は止めない）。

GitHub source（search / read-back）失敗・partial・saturation は `ambiguous_requires_human` として記録されるが、起票を止めない（#1860 Owner Decision: 検索失敗・saturation は起票停止理由にしない）。証跡として helper の JSON 出力（`ISSUE_OVERLAP_PREFLIGHT_RESULT_V1`）を Issue comment または PR 本文に残す。

本 helper は preflight advisory であり、`create_issue_txn.py` の mutation hard gate には配線しない。child overlap は fixture-only であり #946 の child materialization gate ではない。

#### 同一 Allowed Paths への複数 Issue 集約ガイドライン（マージコンフリクト回避）

scope 重複チェックの結果にかかわらず、以下を強く推奨する:

1. **1 PR への集約**: 同一ファイルへの複数の小変更 Issue を起票する場合は 1 つの実装 Issue にまとめて 1 PR で処理
2. **直列依存の設定**: 集約困難な場合は `## Delivery Rule` に依存関係を明記（例: `Depends on #<N> merge`）
3. **並行起票の禁止**: 同じファイルを Allowed Paths に含む複数の Implementation Issue を同時に OPEN 状態で並行起票・実装しない

### 4. 起票を実行する

Issue Template Guard / Outcome Quality Guard / Scope 重複チェックを全て通過したら、Issue 本文を一時ファイルに書き出し、**書き込み前 validator** を実行する（fail-closed）:

```bash
# 書き込み前 validator — exit 1 なら起票しない
# --kind を指定すると ISSUE_TEMPLATE/<kind>.yml から必須セクション・Stop Conditions を動的取得する（LP001/LP017）
# --title を指定すると LP031 で implementation kind の title prefix を検証する
uv run --locked python3 .claude/skills/create-issue/scripts/validate_issue_body.py --body-file /tmp/issue_body.md --kind implementation --title "$TITLE"
```

`validate_issue_body.py` が exit 1 を返した場合は **起票を中止**し、JSON 出力の `errors` を人間に提示して修正を求める。

validator が exit 0 を返した後、人間承認なしで即座に `.claude/skills/create-issue/scripts/create_issue_txn.py` を実行し、transaction として起票する。

helper は `--title` / `--body-file` / `--label` / `--parent-issue` / `--blocked-by` / `--blocking` を受け取り、labels / sub-issue / dependency（両方向）の read-back を同一 transaction で実施する。`--blocked-by` が正式名称（新規 Issue が指定 Issue にブロックされる方向）で、`--dependency` は legacy alias（後方互換のため同じ destination に吸収される）。`--blocking` は新規 Issue が指定 Issue を **ブロックする**（前提Issue/hard predecessor として起票する）方向で、`--blocked-by`/`--dependency` とは別 destination を持つ（#1946）。

**post-create ready tuple validation（起票後の状態検証、implementation issue のみ）**:

`create_issue_txn.py` 実行後、implementation issue の場合は GitHub 上の最終状態を read-back して ready tuple を検証する。pre-create の body 検証と異なり、GitHub に実際に付与された title / labels を確認する。

**手順（ステップ分割）**:

1. GitHub から title/labels を read-back する:

```bash
gh issue view "$CREATED_ISSUE_NUMBER" --repo "$REPO" --json title,labels > /tmp/readback.json
```

2. `guard-issue-body.py` に `--readback-json` を渡して ready tuple を検証する:

```bash
uv run --locked python3 .claude/skills/edit-issue/scripts/guard-issue-body.py /tmp/issue_body.md \
  --issue-kind implementation \
  --check-ready-tuple \
  --readback-json /tmp/readback.json
```

3. guard の exit code を変数に保存する:

```bash
uv run --locked python3 .claude/skills/edit-issue/scripts/guard-issue-body.py /tmp/issue_body.md \
  --issue-kind implementation \
  --check-ready-tuple \
  --readback-json /tmp/readback.json \
  --format json > /tmp/guard_result.json
GUARD_EXIT=$?
```

4. `GUARD_EXIT` が 0 以外の場合は failure comment を投稿して処理を停止する（成功扱いにしない）:

```bash
gh issue comment "$CREATED_ISSUE_NUMBER" --repo "$REPO" \
  --body "[create-issue] post-create ready tuple validation FAILED: title prefix or phase/implementation label missing. Manual fix required."
```

上記の 2 ステップ構成（guard 実行 + exit code 保存 → 失敗時のみ comment 投稿）により、compound shell operator（`||` / `;`）を使わず exit code を確実に検出する。

検証失敗時は上記 comment 投稿後に処理を停止し、成功扱いにしない（起票自体は完了しているが、ready tuple が canonical でないことを明示的に記録する）。

**blocking stop**:
1. Scope が複数に分かれる場合（分割採否は人間判断）
2. Scope Overlap Detected で 3 択のアクション選択が必要な場合

上記以外の確認事項（調査で解決できる技術的事実・フラグ名・コマンド引数・ファイルパス等）は人間確認にせず、Issue 本文の `## Notes for Reviewer` セクションとして記録する。

`create_issue_txn.py` 実行後（および post-create ready tuple validation 通過後）、起票した Issue URL（`issue_url`）を Output として提示する。

### 4a. Delivery-rollup parent の child materialization（子 Issue 生成、`CHILD_MATERIALIZATION_PLAN_V2` 経由）

`delivery-rollup` parent が持つ child issue を起票する場合、`plan_child_materialization.py` が生成した `CHILD_MATERIALIZATION_PLAN_V2` を入口として使う。LLM が parent body 全体を都度読む必要をなくし、トークン消費を抑えるための標準フロー。

**フロー**:

1. plan を取得する（read-only — GitHub Issue は変更しない）:
   ```bash
   uv run --locked python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
     --repo <owner>/<repo> \
     --issue <parent_issue_number>
   ```

2. `CHILD_MATERIALIZATION_PLAN_V2.children` から `action: create_issue` のエントリのみを対象にする（`no_op` / `human_escalation` / `reuse_and_update_parent` は本 step でスキップ）。

3. 各 `action: create_issue` の child について `create_issue_txn.py` を実行する:
   - `--parent-issue <parent_issue_number>` を必ず渡す
   - body に `parent_issue: "<parent_issue_number>"` と `child_id: <child_id>` / `dedupe_key: <dedupe_key>` を含める
   - `create_issue_txn.py` の dedupe 機構により重複起票を防止する

4. `action: reuse_and_update_parent` の child は **起票せず** `edit-issue` skill に `CHILD_MATERIALIZATION_PLAN_V2.parent_body_updates` を渡して parent body の placeholder を修正する。

5. `action: human_escalation` の child は自動処理せず `warnings` として記録し、人間判断を仰ぐ。

**制約**:
- `plan_child_materialization.py` は read-only（GitHub Issue を変更しない）
- plan が存在しない / 取得できない場合は通常の create-issue フロー（ステップ 1〜4）にフォールバック
- `CHILD_MATERIALIZATION_PLAN_V2` は `docs/dev/agent-skill-boundaries.md` が schema 正本

### 4b. `materialize_child_issues.py`（決定論的 executor / `CHILD_MATERIALIZATION_RESULT_V2` producer）

ステップ 4a の手作業フローを **決定論的スクリプト**として正規化したのが `materialize_child_issues.py` である。LLM が child plan を逐次解釈して `create_issue_txn.py` を呼ぶ代わりに、closed-schema バリデーション・canonical body render・起票・parent patch・結果集約を 1 つの helper に集約する。

```bash
uv run --locked python3 .claude/skills/create-issue/scripts/materialize_child_issues.py \
  --plan-file <CHILD_MATERIALIZATION_PLAN_V2 互換 JSON>
```

**責務と保証**:

1. **closed-schema 検証（fail-closed）**: unknown key / duplicate `child_id` / `issue_lookup.complete: false` / 不正 `action` / 非整数 `depends_on` / 空 `allowed_paths` / AC↔VC の AC set 不一致を exit 2 で拒否する。非 JSON 入力は YAML fallback せず拒否する。
2. **canonical body render（spec-driven）**: `ISSUE_TEMPLATE/<kind>.yml` の required label order（`validate_issue_body._load_required_section_labels`）に従って body を生成し、`validate_issue_body.py --kind <kind> --title <title>` を通過した body だけを起票に回す。
3. **起票は `create_issue_txn.py` 経由のみ**: `materialize_child_issues.py` は `gh issue create` を直接呼ばない。`--label-profile standard|triage_only` も `create_issue_txn.py` に転送する。
4. **`depends_on` 写像 + read-back**: 各依存を `create_issue_txn.py --blocked-by <n>`（正式名称。`--dependency` は legacy alias）に写像し、GitHub dependency read-back（`_readback_dependencies`）まで `create_issue_txn.py` が確認する。`depends_on` は「この child が既存 Issue にブロックされる」方向（child が後続）であり `--blocking`（hard-predecessor 方向）ではない。#1946 AC7 inventory の結果、本 Issue 起票時点では新規 Issue を既存 Issue の hard predecessor として `--blocking` で配線する自動 caller は存在しない（producer は直接 CLI 起票のみ。詳細はスクリプト内コメントを参照）。
5. **parent checklist 安全 patch**: parent body の `## Child Issues` section 内で `body_sha256` 一致 / exact `old_line` / `expected_match_count == 1` / post-edit read-back を満たす場合のみ patch する。`partial_failure` では parent patch を一切行わない。
6. **overlap gate**: overlap preflight（#948）が未導入（`overlap.status: not_run|deferred_to_issue`）の場合は各 child が #948 を `depends_on` に明記していることを要求し、満たさなければ `human_escalation`。`overlap.status: undeterminable` は無条件で `human_escalation`。
7. **出力**: `CHILD_MATERIALIZATION_RESULT_V2`（schema 正本は `docs/dev/agent-skill-boundaries.md`）を stdout に JSON で返す。exit code は `ok`=0 / `human_escalation`=3 / それ以外=1。

設計境界の詳細は `docs/dev/agent-skill-boundaries.md` の「child materialization executor」を参照。

## Output（出力）

1. **Issue タイトル**: `<type>(<scope>): <description>` 形式で 1 案を決定（ユーザーへの選択肢提示は不要）
2. **Issue 本文案**: ステップ 3 の項目を含む完全な本文
3. **起票した Issue URL**（必須）
4. **分割案**（Scope が複数の場合のみ）
5. **Notes**（あれば）: blocking stop に該当しない補足事項

## 親子 Issue 構造ルール

サブ Issue を起票するときの不変条件:

**親 Issue = 共通文脈コンテナ**
- 背景・目的・調査結果・共有コンテキストのみを保持する
- 作業タスク（実装・修正・検証手順）は親 Issue 本文に書かない
- 既存の親 Issue 本文にタスクがある場合は、サブ Issue を追加する前にそれらをサブ Issue へ移行する

**サブ Issue = 作業タスク単位**
- `1 Issue = 1 PR` で完結する単一の作業タスクを持つ
- 親子の階層関係を表したい場合は GitHub native `sub-issues` を使う
- sibling issue 間で「どちらを先にマージしないと次へ進めないか」を表したい場合は `issue dependencies` を使う
- 単なる参考参照・closed precedent・将来候補の destination mapping だけで十分な場合は `sub-issues` / `issue dependencies` を増やさず、Issue 本文または comment に routing を残す

**`create_issue_txn.py` による自動 sub-issue 登録（スクリプト正本）**:

`.claude/skills/create-issue/scripts/create_issue_txn.py` が sub-issue 登録の **script 正本** である。
以下の規約に従って親子登録を自動実行する:

1. **body-derived parent 抽出**: `--body-file` で渡された body の以下パターンから parent issue number を抽出する:
   - `parent_issue: "#N"` / `parent_issue: "N"` / `parent_issue: N`（YAML front-matter 形式）
   - `## Parent Issue\n\n#N` / `## Parent Issue\n\nN`（Markdown 見出し形式）
   - `parent: #N` / `parent: N`（短縮形）
   - `none` / `null` / `なし` / `N/A` は「親なし」として None を返す

2. **`Depends on #N` は parent として解釈しない**: `Depends on #N` は dependency/blocker 表現であり、
   MUST NOT parent issue として解釈すること。dependency 登録には `--blocked-by`（正式名称。`--dependency` は
   legacy alias で同じ destination に吸収される）を使う。前提Issue（hard predecessor）として新規 Issue を
   起票する場合、つまり新規 Issue が既存 Issue を **ブロックする** 方向にしたい場合は `--blocking` を使う
   （`--blocked-by`/`--dependency` とは逆方向。混同すると #1946 で修正した native dependency 方向逆登録
   バグを再現するので注意）。

3. **body と `--parent-issue` の照合（fail-closed）**: `--parent-issue` と body 内 parent が **矛盾した場合**、
   issue 作成・dedupe reconcile の前に `TransactionError(stage="parent-arg-body-mismatch")` で **fail-closed** する。
   矛盾 = 両方が指定されており、かつ番号が異なる場合。

4. **422 idempotency（read-back 同一 parent のみ PASS）**: sub-issue POST が 422 を返した場合は、
   `GET repos/{owner}/{repo}/issues/{child}/parent` で read-back を行い、
   read-back の parent number == requested parent number の場合のみ `already_registered` として PASS。
   それ以外（404・別 parent・empty）は `partial_failure` とする。
   `replace_parent=true` は使わない。

5. **dedupe 経路でも body 由来 parent を使用**: dedupe（同一タイトルの既存 issue が見つかった場合）でも、
   body 由来 parent に基づく sub-issue 登録（idempotent）と parent read-back / reconcile が実行される。

**sub-issue 登録手順（手動実行が必要な場合）**:

```bash
# 1. child issue の整数 databaseId を GraphQL で取得
CHILD_DB_ID=$(gh api graphql -f query='
{
  repository(owner: "{owner}", name: "{repo}") {
    issue(number: {child_number}) {
      databaseId
    }
  }
}' --jq '.data.repository.issue.databaseId')

# 2. parent issue に sub-issue として登録
gh api repos/{owner}/{repo}/issues/{parent_number}/sub_issues --method POST -F sub_issue_id=$CHILD_DB_ID
```

- `databaseId` 取得失敗時は POST に進まず fail-closed で停止
- 既存 child issue の親が一致しているかを read-back で確認:
  ```bash
  gh api repos/{owner}/{repo}/issues/{child_number}/parent --method GET -H "Accept: application/vnd.github+json"
  ```
- read-back が別の親を返した場合は `replace_parent=true` を使わず fail-closed で停止

**責務分担**:
- sub-issue の「登録手順」と「body/arg 照合・422 idempotency」は本セクション（`create-issue`）が正本
- sub-issue として「登録済みかを確認する方法（read-back 確認）」は `issue-contract-review` が正本

## Blocker / Blocked-by 設定手順

### blocker 検出基準

- 対象機能の実装 PR が未マージ
- 依存 spec / 設計書が未確定
- 同一ファイルを変更する別 PR がオープンでコンフリクト可能性がある
- 本 Issue の AC が別 Issue の完了を条件としている

### 設定方法（`--blocked-by`: 新規 Issue が既存 Issue にブロックされる方向）

`--blocked-by` が正式名称、`--dependency` は legacy alias（同じ destination に吸収される。後方互換のため残存）。

```bash
uv run --locked python3 .claude/skills/create-issue/scripts/create_issue_txn.py \
  --repo <owner>/<repo> \
  --title "実装: <タイトル>" \
  --blocked-by <blocker_issue_number> \
  --blocked-by <another_blocker_number>
```

### 検証コマンド

```bash
# blocked-by 関係が登録されたことを GraphQL で確認
gh api graphql -f query='
query {
  repository(owner:"<owner>", name:"<repo>") {
    issue(number: <child_issue_number>) {
      blockedBy(first: 10) {
        nodes { number title }
      }
    }
  }
}'
```

`state/blocked` ラベルを付与し、blocker が解除されるまでキューに入れないことを推奨する。

### 前提Issue（hard predecessor）を起票する場合: `--blocking`

新規 Issue を、既存 Issue の **前提Issue（hard predecessor）** として起票したい場合（＝新規 Issue が既存
Issue を **ブロックする** 方向にしたい場合）は `--blocking` を使う。`--blocked-by`/`--dependency` と混同して
逆方向に登録すると #1946 で修正した native dependency 方向逆登録バグ（`#1943` が `#1842` の hard
predecessor として起票されるはずが、逆方向 `#1943 blocked by #1842` として誤登録された事例）を再現するので
注意すること。

```bash
uv run --locked python3 .claude/skills/create-issue/scripts/create_issue_txn.py \
  --repo <owner>/<repo> \
  --title "実装: <タイトル>" \
  --blocking <existing_issue_number_this_new_issue_blocks>
```

```bash
# blocking 関係（新規 Issue 側）が登録されたことを GraphQL で確認
gh api graphql -f query='
query {
  repository(owner:"<owner>", name:"<repo>") {
    issue(number: <new_issue_number>) {
      blocking(first: 10) {
        nodes { number title }
      }
    }
  }
}'
# 逆方向確認（既存 Issue 側の blockedBy に新規 Issue が存在すること）
gh api graphql -f query='
query {
  repository(owner:"<owner>", name:"<repo>") {
    issue(number: <existing_issue_number>) {
      blockedBy(first: 10) {
        nodes { number title }
      }
    }
  }
}'
```

hard-successor（`--blocking` で配線すべき自動 caller）の inventory は #1946 AC7 で確認済み。本 Issue
起票時点では該当する自動 caller は存在せず、follow-up hard-successor Issue の起票は不要と判断した
（詳細は `create_issue_txn.py` モジュール docstring を参照）。

## Partial-failure Recovery 手順

`create_issue_txn.py` がいずれかのステージで失敗した場合、Issue に partial-failure audit comment が自動投稿される。

### sub-issue-readback の bounded retry 契約

`create_issue_txn.py` は parent issue への sub-issue 登録 POST 成功後に parent readback（`GET repos/{owner}/{repo}/issues/{child}/parent`）を行う。GitHub Sub-issues API の eventual-consistency ラグにより、POST 直後の GET が期待する parent 番号を返さない transient failure が発生することがある。

この transient failure を吸収するため、`_readback_parent_issue_with_retry` helper が **bounded semantic retry** を行う:

- **成功条件（retry 期間内に確認できた場合）**: transaction は `success` または `dedupe` を返す
- **全試行失敗条件（全試行で親を確認できなかった場合のみ）**: transaction は `partial_failure`、`failure_stage = "sub-issue-readback"` を返す
- **retry 遅延**: `_PARENT_READBACK_RETRY_DELAYS`（module-level 定数 `tuple[float, ...]`）で定義。合計待機時間は常に 2 秒以下
- **sleep_fn 注入**: helper は `sleep_fn` を引数で受け取り、単体テストでは fake sleep を渡すことで実時間 `time.sleep` を発生させない
- **両経路で共有**: 新規 Issue 作成経路（`run_transaction` 内）と dedupe reconcile 経路（`_reconcile_issue_links` 内）の双方が同一 helper・同一定数を使う

`partial_failure` stage = `sub-issue-readback` を受け取った場合は、実際には parent 登録済みの可能性が高い。下記 Recovery 手順で関係を確認してから manual 対応する。

### 失敗ステージ別の補正手順

| failed_stage | 補正手順 | idempotent |
|---|---|---|
| `sub-issue-readback` | readback で関係確認 → 未登録なら `sub_issues` 登録 | 既存関係 readback 後に再実行 |
| `dependency-readback` / `dependency-register` | blockedBy readback → 未登録なら GraphQL mutation | 既存関係 readback 後に再実行 |
| `label-readback` | `uv run python3 .claude/skills/create-issue/scripts/create_issue_txn.py reconcile --repo <owner/repo> --issue <N> --label <labels>` を実行して script 経由で復旧する。raw direct mutation は禁止（script 正本経由のみ）。 | yes |
| `dedupe-search` / `dedupe-race-detection` | 自動補正不可。手動で同タイトル open issue を確認・クローズ後に再実行 | no |

comment の "Recovery hint:" 以降に stage 固有の補正コマンドと idempotency 情報が記載されている。

### label-readback 失敗後の reconcile 手順

`label-readback` ステージで `partial_failure` が返された場合、**以下の手順で復旧する**:

```bash
uv run --locked python3 .claude/skills/create-issue/scripts/create_issue_txn.py reconcile \
  --repo <owner/repo> \
  --issue <issue_number> \
  --label <label1> --label <label2>
```

**禁止事項**: `reconcile` サブコマンドを使わずに `gh issue edit <N> --add-label <labels>` を直接実行すること。
script 正本経由のみで復旧すること（audit trail・idempotency の保証のため）。
`gh api` 直接 mutation も同様に禁止する。

## Guardrails（安全境界）

- 曖昧な要件を推測で埋めて Issue を確定させない
- `1 Issue = 1 PR` を超える Scope の Issue を単独で作成しない。黙って広げず、分割案を提示してから人間に確認する
- Acceptance Criteria に検証不可能な条件（「適切に動作すること」など）を含めない
- Verification Commands に実際に存在しないコマンド・ファイルを記載しない
- サブ Issue を起票・追加するとき、親 Issue 本文にタスクが残っていれば停止して移行案を提示する
- Outcome が動作状態のみで成果物形式を欠く Issue を確定させない（Outcome Quality Guard 参照）
- 未解決の追加調査が残る状態で、「確認する」「決める」だけを Outcome にした Issue を起票しない
- **scripts entrypoint 経由統一**: anchor preflight は必ず `.claude/skills/create-issue/scripts/verify-anchors.sh` 経由で実行する
- **inline `gh` / `jq` / `grep` / `awk` / heredoc 使用禁止**: anchor preflight での inline bash パイプラインは使用しない
- **スクリプトは配列形式の `git grep` のみ実行**: `eval` 禁止、入力 anchor は `^[A-Za-z0-9._/: #-]+$` で validation 済み

## visual_impact フィールド（canonical schema owner）

Issue 本文が `visual_impact` 関連フィールド（`VISUAL_IMPACT_DECLARATION_V1` 等）に触れる場合、
単一の canonical schema ファイル [`docs/dev/visual-impact.schema.json`](../../../docs/dev/visual-impact.schema.json)
を参照する（詳細は [`references/body-authoring.md`](references/body-authoring.md) の該当節）。
本 SKILL.md では独自にキー集合・enum を再定義しない（Issue #2019 AC26）。

## Related（関連資料）

- [`references/body-authoring.md`](references/body-authoring.md) — 本文編集の共通参照（schema 定義・VC 作成ガイダンス・Anchor Verification・Blocker 検出）
- `.claude/skills/review-issue/SKILL.md` — Issue 品質レビュー
- `.claude/skills/issue-contract-review/SKILL.md` — 着手前 contract 確認
- `.claude/skills/ssot-discovery/SKILL.md` — 関連 SSOT の探索
- `.github/ISSUE_TEMPLATE/implementation.yml` / `parent.yml` / `research.yml` — Issue Forms 正本

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
