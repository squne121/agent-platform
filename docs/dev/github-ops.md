# GitHub Ops 運用ルール

`gh` CLI を介した GitHub 操作の共通規約。AI エージェント・人間レビュアー双方が参照する。
個別の skill / SubAgent はこのルールに従って Issue / PR / コメントを更新する。

> **GitHub Milestone 操作の正本**: GitHub Milestone の作成・割当・close・rollup に関する運用規約は
> `docs/dev/milestone-ops.md` が正本である。本ドキュメントは `gh` CLI の共通規約を扱い、
> Milestone 固有の判断基準・命名規則・AI 操作フローは `docs/dev/milestone-ops.md` を参照すること。

## ISSUE_KIND_POLICY_V1

Issue の `issue_kind` taxonomy の正本。`plan_refinement_loop.py` / `check_issue_contract.py` はここを SSOT として参照する。

```yaml
ISSUE_KIND_POLICY_V1:
  schema_version: "1"
  canonical_kinds:
    - implementation
    - research
    - parent
  aliases:
    design: research      # deprecated alias — design は research として正規化する
    tracking: parent      # label legacy alias — tracking は parent として正規化する
  unknown_kind_policy: block  # allowlist / aliases に存在しない kind は silent fallback 禁止
  unknown_kind_reason_code: unknown_issue_kind
  consumer_requirements:
    - plan_refinement_loop.py
    - check_issue_contract.py
```

### SSOT 利用規約

- **canonical_kinds**: template 検索・section 検証に使う確定 kind 集合。
- **aliases**: 入力 kind がここにあれば対応する canonical kind に正規化して扱う。
- **unknown_kind_policy: block**: canonical_kinds にも aliases にも存在しない kind を受け取った場合、`implementation` への silent fallback は禁止。`unknown_issue_kind` reason_code で fail_closed を返すか、呼び出し元に block を返す。
- ローカル allowlist 定義を consumer script 内に持つことは禁止（SSOT 二重管理防止）。

## Body File Guidance（`gh issue edit` / `gh pr edit` / `gh issue comment` 共通）
本文ファイル経由の更新手順に関する共通ガイダンスをまとめる。

Issue / PR 本文の長文・多行更新時は **必ず body-file 経由** で操作する。inline `--body "..."` はクォート崩壊・HEREDOC 由来エスケープ混入を招くため使わない。

```bash
mkdir -p tmp
BODY_FILE="tmp/<task>-<番号>-body.md"
# 本文全体を $BODY_FILE に書き出してから:
gh issue edit <番号> --body-file "$BODY_FILE"
gh pr edit <番号> --body-file "$BODY_FILE"
gh issue comment <番号> --body-file "$BODY_FILE"
```

### Pre-edit guard
本文ファイルを渡す直前の事前確認手順。

`--body-file` を渡す直前に以下を実行し、空ファイル・1 byte ファイル・HEREDOC 由来エスケープ混入を検知して停止する:

```bash
wc -c "$BODY_FILE"
if [ "$(wc -c < "$BODY_FILE")" -le 1 ]; then
  echo "body-file が空または 1 byte です: $BODY_FILE" >&2
  exit 1
fi
if grep -Pn '\\(?:\"|\$)' "$BODY_FILE"; then
  echo "HEREDOC 由来エスケープ混入の可能性。該当行を確認して再実行" >&2
  exit 1
fi
```

### HEREDOC 内のコードフェンス

HEREDOC サンプルにコードフェンスを含める場合は ``` ではなく `~~~` を使う（HEREDOC 終端の `EOF` 直前にコードフェンスが解釈されて崩れる事故を防ぐ）:

```markdown
~~~yaml
key: value
~~~
```

## Parent Issue の Machine-Readable Contract
親 Issue が持つ機械可読な契約定義を扱う節。

parent Issue の `## Machine-Readable Contract` は以下の closed enum を使い、placeholder のまま確定しない。

`parent_mode`:
- `delivery-rollup`: child implementation の完了を rollup して close する
- `quality-gate`: child 完了と quality decision を分離、Quality Decision Record の確定まで close しない
- `routing-map`: canonical destination の地図を維持する
- `decision-log`: 意思決定記録と next action の固定を主目的にする

`closure_mode`:
- `child-complete` / `measurement-ready` / `quality-validated` / `routing-complete` / `decision-recorded`
（子 Issue 完了・測定準備完了・品質検証済み・ルーティング完了・意思決定記録のいずれかを示す列挙値）

`parent_mode` と `closure_mode` の互換:

| parent_mode | 許容される closure_mode |
|---|---|
| `delivery-rollup` | `child-complete` |
| `quality-gate` | `measurement-ready` または `quality-validated` |
| `routing-map` | `routing-complete` |
| `decision-log` | `decision-recorded` |

### Fail-close 条件

- `parent_mode` 欠落 → 自動推定で確定しない（提案までで止める）
- `closure_mode` と `Quality Decision Record.Status` 不一致 → invalid、close 判定不可
- `<required: ...>` placeholder や enum 外値 → missing 扱い

## Issue / PR コメントへの記録プロトコル

オーケストレーター skill / SubAgent が Issue / PR にコメントを残す際の構造化テンプレ。

### イテレーション開始時

```markdown
## <skill 名>: iteration <N> 開始 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- Inputs: <要約>
- 前 iteration からの差分: <ある場合>
```

### イテレーション完了時

```markdown
## <skill 名>: iteration <N> 完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- 結果サマリ: <verdict / status>
- 次イテレーション: <Yes / No (理由)>
- 詳細: <REVIEW_ISSUE_RESULT_V1 / LOOP_VERDICT 等の構造化出力要約>
```

### ループ終了時

```markdown
## <skill 名>: 完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- 最終 iteration: <N>
- termination_reason: <approved | max_iterations | human_escalation>
- LOOP_STATE 最終値: <主要フィールド>
- 次アクション: <人間レビュー / マージ / 追加 iteration 等>
```

## 認証・リポジトリ指定

- `gh auth status` で認証状態を確認。認証エラー時は人間に再認証を依頼
- スクリプト・skill 内で `gh` を呼ぶ際は `--repo <owner>/<name>` を明示（worktree 内の origin が変わっても安全）
- 対話的にユーザーが叩く想定のコマンドでは `--repo` を省略してよい

## ラベル運用

- 状態: `state/in-progress` / `state/needs-human` / `state/blocked` / `state/done` / ~~`state/queued`~~（deprecated / legacy — 詳細は「state ラベル意味論定義」参照）
- 種別: `phase/implementation` / `phase/research`
- カテゴリ: `bug` / `enhancement` / `chore` / `docs` / `tracking` 等
- triage: `triage-required`（後述）

詳細は `.github/ISSUE_TEMPLATE/*.yml` の labels 定義を SSOT として参照する。

### triage-required ラベル

**目的**: AI エージェントが自動起票した follow-up Issue を人間または AI が triage するまでの間、未評価であることを明示する。

**付与タイミング**: `impl-review-loop` Step 5 / `post-merge-cleanup` の main thread が follow-up Issue を自動起票する際に **必ず付与** する。手動起票の場合は任意。

**解除タイミング**: triage 完了後（`state/needs-human` / close のいずれかに移行した時点、または単に `triage-required` を除去して `phase/` ラベルを付与した時点）。

**state ラベルとの関係**: `triage-required` は state ラベルと競合しない補助ラベル。triage 完了時に `state/queued` は付与しない（`state/queued` は deprecated / legacy 扱い — AI 着手可否・VC・contract-review では参照禁止）。

**運用フロー**:

```
自動起票（triage-required 付与）
        ↓
triage セッション（人間または AI エージェント）
        ↓
  有効 → triage-required 除去 + phase/ ラベル
  重複 → duplicate ラベル + close
  不要 → not planned で close
  保留 → state/needs-human
```

**注意**: `triage-required` は state ラベル体系（`state/queued` 等）とは独立した補助ラベルであり、AI 着手可否判定の primary signal として使わない（着手可否は blocker / dependency の close 状態で判断する）。

### state ラベル意味論定義

| ラベル | 意味 |
|---|---|
| `state/queued` | **deprecated / legacy** — AI 着手可否・VC・contract-review では参照禁止。着手可否の source of truth は blocker/dependency の close 状態である。triage フローでの自動付与も廃止（#211）。 |
| `state/in-progress` | 担当者（AI エージェントまたは人間）が現在作業中。 |
| `state/blocked` | blocker / dependency のうち少なくとも 1 つが open のため、着手できない状態。**補助・派生表示**であり、AI 着手可否の primary signal ではない（後述）。 |
| `state/needs-human` | 仕様判断・ライセンス確認・インフラ操作など、AI が単独で進められない判断を人間に求めている状態。 |
| `state/done` | Issue が完了し close された状態。 |

### AI 着手可能性の source of truth

**AI が Issue に着手してよいかどうかの source of truth は、GitHub native dependency（`depends on` リンク）がすべて close されているかどうかである。native dependency が利用できない場合は Issue 本文の `Depends on #N` fallback で補完する。**

- `state/blocked` ラベルは補助・派生表示にすぎず、AI 着手可否の primary signal ではない。
  - `state/blocked` が残存しているだけで BLOCKED 判定しない（ラベルの更新遅れ・付け忘れが生じうるため）。
- `state/queued` ラベルは deprecated / legacy 扱いであり、AI 着手可否・VC・contract-review での参照は禁止する（legacy cleanup 目的の削除のみ許容）。blocker / dependency がすべて close されていれば着手可であり、`state/queued` の有無は判定に関与しない。
- `state/ready` ラベルは採用しない（判断根拠は後述）。

### state/ready 不採用の根拠

`state/ready` ラベルを追加しても「ready かどうか」を判断するには結局 blocker / dependency の close 状態を確認する必要がある。ラベルはその確認結果の複製にすぎず、二重管理によるズレ（ラベルは ready だが blocker が open のまま等）を招く。したがって `state/ready` は採用しない。着手可否は blocker / dependency の close 状態を直接参照することで判断する。

### GitHub Issue labels の presentation-only SSOT 原則（全ラベル種別への一般化, #2084）

上記の `state/blocked` / `state/queued` に関する「補助・派生表示」原則を、全ラベル種別（`phase/implementation` / `state/needs-human` / `state/done` / `triage-required` / `agent/implementer` を含む）へ一般化する。

**GitHub Issue labels は presentation-only / non-authoritative metadata である。** labels は人間の認知・分類・検索補助に限定され、AI の readiness・quality・implementation permission・handoff・stop condition の authority にしてはならない（OWNER directive、#2084 comment #5249582962 / #5249734344）。

#### Readiness Plane（readiness の authority）と Presentation Plane（label の位置づけ）の分離

- **Readiness Plane（readiness authority）**: GitHub native issue open/closed state、Machine-Readable Contract、GitHub native dependency（`Depends on #N` fallback 含む）close 状態、explicit operator/OWNER directive、`CONTRACT_REVIEW_RESULT_V1` / review / test / CI 結果。
- **Presentation Plane（label の位置づけ）**: GitHub labels は人間の認知・分類・検索補助に限定する。label mutation の失敗は warning/telemetry にはできるが、readiness failure にしてはならない。

#### 不変条件

`label_sync.result`（`applied | noop | failed`）の値を変えても `status` / `routing_action` / `implementation_allowed` が変化してはならない。`triage-required` remove、`phase/implementation` / `agent/implementer` add 等の label mutation は、readiness decision 後にのみ実行される best-effort presentation sync として扱う。

### blocker / dependency の本文表現規約

Issue 本文で依存関係を表現する場合は、以下の優先順位に従う。

1. **GitHub native dependency（primary）**: GitHub の "Add a dependency" 機能（`depends on` リンク）を使う。GitHub UI および API から依存関係を直接参照できる。
2. **`Depends on #N` テキスト表現（fallback）**: native dependency が利用できない場合、または補足的に明示する場合は Issue 本文に `Depends on #N` の形式で記載する。

### blocker 判定の優先順位

AI エージェントが着手可否を判定する際の blocker 確認順序:

1. GitHub native dependency API でリンクされた dependency Issue がすべて close か確認する（primary）。
2. Issue 本文に `Depends on #N` パターンが存在する場合、該当 Issue `#N` がすべて close か確認する（fallback / 補完）。

### native dependency と `Depends on #N` が不一致の場合

native dependency が示す依存関係と、本文の `Depends on #N` 記述が矛盾または不一致（例: native では依存なし、本文では `Depends on #N` が open を指している）の場合は、**自動判断せず human escalation とする**。不一致を Issue コメントに記録し、人間による確認・修正を依頼する。

### closed になった stale native dependency の解除（`issue_dependency.remove`、Issue #1632）

closed になった blocker への GitHub native `blockedBy` relationship が本文の依存記述を持たないまま残存すると、
implementation overlap preflight が `human_review_required` に倒れ続ける（例: #1523 と closed #1403）。
このケースは `CONTROLLED_SKILL_MUTATION_COMMAND_POLICY` の `issue_dependency.remove` command id で解除する
（詳細は `docs/dev/agent-skill-boundaries.md#issue_dependencyremove--github-native-blockedby-解除-controlled-executorissue-1632`
を参照）。

- 対象は **closed blocker への native `blockedBy` 一件のみ**。open blocker、期待する `blockedBy` 集合が
  一致しない候補、複数 dependency の一括解除は対象外（out of scope）。
- 固定 host（`github.com`）・固定 GraphQL query/mutation・全ページ pre/post readback・
  node-ID/number/state/set binding・trusted actor 権限確認をすべて満たした場合のみ一回だけ
  `removeBlockedBy` を実行する。network/GraphQL error や postcondition mismatch は自動再試行しない。
- native dependency と本文の `Depends on #N` が不一致な場合の human escalation 境界（上記節）は
  この executor によって変更されない。`issue_dependency.remove` は closed-blocker-only の一件解除の
  みを扱い、本文記述の自動修復は行わない。mutation 応答（`removeBlockedBy` の
  `issue`/`blockingIssue` node ID・number・`clientMutationId`）の一致確認、mutation 前後どちらの
  write-root 外 tracked changes 確認、`GH_TOKEN`/`GITHUB_TOKEN` の環境除去、mutation 直前の
  attempt marker 記録は PR #1667 レビュー fix_delta で追加された安全策である。

## 既存の Issue に対して native な parent・blockedBy・blocking の関係性を同期する仕組み（`issue_relationship.update`、Issue #1883）

`edit-issue` transaction（`edit_issue_txn.py`）が既存 Issue を更新する際、本文の
`parent_issue`／`Part of`／`Depends on #N` を書き換えても GitHub native
`parent`／`blockedBy`／`blocking` が自動的に追従する保証はない（#1679 で実際に
不一致が発生した）。native relationship を変更する場合は、本文更新だけで完了と
判定せず、`ISSUE_EDIT_TXN_INPUT_V1.native_relationships`（additive、任意）に
explicit structured な desired state を渡し、`issue_relationship.update`
controlled executor 経由で native graph を同期・全ページ readback してから
本文を更新する。

- native relationship 変更は本文 mutation より **先に** 実行する。native
  mutation が失敗した場合、本文 mutation は一切開始しない。
- `blocked_by`／`blocking` の変更は常に明示的な `add_*`/`remove_*` 集合で表現
  する。`replace` semantics は提供しない。destructive な `remove_*` を実行する
  前に、pre-readback で取得した live state と `expected_before` の完全一致を
  確認し、drift があれば mutation を一切実行しない。
- `Related`／`Part of`／URL mention／コメント等の自然言語からは parent／
  blocked_by／blocking を一切推測しない。structured input のみが source of
  truth である。
- 親 Issue と blocked-by Issue は意味が異なるため、自動的に相互変換しない
  （`native_relationships.parent` と `add_blocked_by`/`add_blocking` は独立
  したフィールドであり、同一 issue number を両方に同時指定することは graph
  invariant 違反として拒否される）。
- 詳細な transaction order・実行系統・スキーマは
  `docs/dev/agent-skill-boundaries.md#issue_relationshipupdate-による-github-native-関係性parent--blockedby--blockingの同期実行系統issue-1883`
  を参照する。

## scripts 集約による permission 削減パターン

オーケストレーション skill（edit-issue / post-merge-cleanup / create-issue）の inline bash は `.py` / `.sh` script に集約し、`subprocess.run([...])` 配列形式 + 外部入力 allowlist validation を必須とする。Bash allowlist は scripts entrypoint パターン（`Bash(uv run python3 .claude/skills/<name>/scripts/*.py *)`）に絞ることで permission prompt を 1 ループあたり 1 回に削減する。

スクリプト呼び出しは **必ず `uv run python3 ...` 形式** を使う（bare `python3` 直起動は禁止）。`uv` は dependency lock を共有し、再現性のある実行を保証する。Bash allowlist にも `python3` 直起動パターンを置かないこと。

一時ファイル（バックアップ・中間 body・コメント本文等）は **リポジトリルート配下の `tmp/` に作成** する。システム `/tmp/` は使わない（プロジェクト外への副作用回避 / cleanup の追跡性確保のため）。バックアップ等の一時ファイルは `.gitignore` で除外する。

## .claude/settings.json permissions 方針
権限設定ファイルにおける許可方針の全体像を示す。

Claude Code の `.claude/settings.json` は `permissions.allow` / `permissions.ask` / `permissions.deny` の 3 セクションで構成される。
公式ドキュメント: https://docs.anthropic.com/claude-code/permissions（確認日: 2026-05-21）

### permissions.ask の動作

`permissions.ask` に列挙されたコマンドは、Claude Code がそのコマンドを実行しようとする際に **実行前確認プロンプトを表示する**。
人間が「許可（Allow）」を選ぶと実行される。「拒否（Deny）」を選ぶとキャンセルされる。

> 参照: https://docs.anthropic.com/claude-code/permissions#permission-modes

### GitHub 操作（Issue / PR）→ allow 方針

**方針**: GitHub Issue / PR 操作はすべて `allow` に置く。

AI エージェントと人間のコミュニケーションは **Issue / PR をインターフェース** として行う設計のため、
`gh issue *` / `gh pr *` / `gh api *` コマンドを `ask`（確認プロンプト）にすると
エージェントの自律的なワークフロー（issue コメント投稿、PR 起票、ラベル更新等）が毎回中断される。

Issue / PR はそれ自体が人間可視の監査ログであり、エージェントの行動は GitHub 上で追跡・取消可能なため、
追加の確認プロンプトは不要と判断した。（Issue #48 調査・Issue #114 実装、方針確定 2026-05-21）

### git commit / push → allow 方針
コミットとプッシュ操作を許可する方針の説明。

**方針**: `git commit *` / `git push *` も `allow` に置く。

`main` ブランチへの直接 push は **GitHub の branch protection rules**（require PR reviews / restrict pushes）で防ぐ。
Claude Code の `ask` プロンプトで防ぐのではなく、インフラ層のガードレールを使う（workaround 最小化方針）。

branch protection が設定されている限り、エージェントが `git push` を実行しても
`main` への直接マージは不可能なため、`ask` を置く必要はない。

### git worktree / checkout / stash → allow 方針
worktree・checkout・stash 操作を許可する方針の説明。

**方針**: `git worktree *` / `git checkout *` / `git stash *` も `allow` に置く。

`implement-issue` skill および SubAgent が worktree ベースで実装を進めるため、
これらのコマンドが毎回確認プロンプトを要求すると worktree 作成フローが中断される。
副作用リスクは低く（ローカル git 操作のみ）、`allow` で問題ない。

## main ブランチ branch protection 設定状況

確認日時: 2026-05-21

確認コマンド:

```bash
gh api repos/squne121/agent-platform/branches/main/protection
```

設定済み項目:

| 項目 | 値 |
|---|---|
| `required_pull_request_reviews.dismiss_stale_reviews` | `true` |
| `required_pull_request_reviews.required_approving_review_count` | `0` |
| `allow_force_pushes.enabled` | `false` |
| `required_status_checks` | `typecheck` / `lint` / `test` / `build`（strict: true） |
| `allow_deletions.enabled` | `false` |

`git push --force` は GitHub 側でブロックされる。これは `.claude/settings.json` の `git push *` を allow 設定しても、GitHub リモートが force push を拒否することを意味する。AI エージェントが誤って force push を試みた場合でも、リポジトリ保護が維持される。

## .claude/settings.local.json 個人用設定例

`.claude/settings.local.json` は **個人ローカル設定** であり、リポジトリには含めない（`.gitignore` で除外済み）。下記を個人用に配置することで Bash 系の追加許可を適用できる。

```json
{
  "permissions": {
    "allow": [
      "Read(//home/<USER>/.gemini/**)",
      "Bash(env)",
      "Bash(gemini --version)"
    ]
  }
}
```

組織全体で許可したいパターンは `.claude/settings.json`（repo-tracked / 共有）に置く。個人ローカル特化（端末固有のパス、テスト用環境変数等）は `.claude/settings.local.json` に置く。

## Codex Runtime Reference
Codex ランタイムに関する参照情報の窓口。

Codex local runtime の install / PATH / runtime recovery / permission profile / rules / instruction surface は
[agent-runtime-ops.md](agent-runtime-ops.md) を正本とする。
この文書では GitHub 操作に直接関係するルールだけを扱う。

## GitHub Trust Boundary
GitHub 操作における信頼境界の定義。

- Codex session での GitHub 操作は `rtk gh` を入口にそろえる
- 長文更新は上の Body File Guidance を使い、Codex session では `rtk gh issue edit --body-file ...` / `rtk gh pr edit --body-file ...` / `rtk gh issue comment --body-file ...` を使う
- この文書中の `gh ...` 表記は GitHub CLI の概念説明であり、Codex session から direct `gh` を実行してよいという意味ではない
- `rtk gh` の documented subcommand 範囲と GitHub write policy が変わった場合は、この節と runtime 側の運用文書を同時に見直す
- この文書は GitHub trust boundary と body-file guidance の窓口であり、runtime 復旧手順の詳細は持たない

### GitHub Trust Boundary Smoke Check
信頼境界が維持されているかを確認する簡易チェック手順。

`rtk gh` の入口が想定どおり維持されているか、最低限以下を確認する。

```bash
rtk gh --help
```

Issue #2161（native Codex CLI retirement）以前は上記に加えて `codex execpolicy check --pretty --rules
.codex/rules/default.rules -- rtk gh issue view 1` 等の execpolicy smoke check も行っていたが、
`.codex/**` の撤去に伴い当該チェックは廃止された。
