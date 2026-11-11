---
name: open-pr
description: 対話的 Claude Code から手動で Draft PR を作成する最小 caller。D7 の単一 reference evaluator で Closes/Refs を決め、同一 repo/head/base の OPEN PR を再利用する。
disable-model-invocation: true
---

# Open PR（最小 caller, I-4-4D11 / #108）

`.claude/skills/open-pr/scripts/open_pr.py` を **人間が必要時に手動 invoke** する。モデルの自動 invoke と SubAgent への preload は `disable-model-invocation: true` で抑止する。

## 認可の境界

- 使うのは **呼び出した対話的 Claude Code セッションの既存 `gh` 認証だけ**。secret のコピー、権限変更、permission bypass はしない。認証が無ければ最初の read で停止し、write は 0。
- Stage 2 の controlled `pr_create` / `pr_edit` / `pr_comment` / `pr_update_branch` は `unavailable`、Bot/Broker の mutation permission は `unverified` のまま。この caller はそれらの代替でも登録でもない。
- 実 PR を作成できたことは、その session の権限の事実であり、control-plane の `pr_*` capability の検証ではない。
- merge / auto-merge / `gh pr edit` / `gh pr comment` / `gh pr update-branch` / force push はこの caller の範囲外。

## 実行

```bash
# 読み取りのみの確認（write 0）
uv run --locked python3 .claude/skills/open-pr/scripts/open_pr.py \
  --pr-title "<title>" --linked-issue <N> --pr-body-file <body.md> --dry-run

# 公開（明示承認後のみ）
uv run --locked python3 .claude/skills/open-pr/scripts/open_pr.py \
  --pr-title "<title>" --linked-issue <N> --pr-body-file <body.md> --publish yes
```

結果は stdout の JSON 1 件（`status` = `dry_run` / `created` / `reused` / `stopped`）。exit 0 は dry_run / created / reused、1 は停止、2 は引数不正。

## 手順（すべて fail-closed）

1. `--publish yes`（dry-run を除く）が無ければ `E_APPROVAL_MISSING`。
2. 対象 repo は `config/repository_identity.v1.json` の trusted slug だけ（`scripts/agent-guards/repo_identity.py`）。`origin` の URL、現在 branch（`main`・detached は不可）、clean な作業ツリー、origin 上の同名 branch が local HEAD と一致、origin に `main` があること、を read-only で照合する。すべて `--repo <trusted>` を明示して `gh` を呼ぶ。
3. linked Issue の state / 本文を fresh に取得する。取得できなければ `E_LINKED_ISSUE_STATE_UNKNOWN`。
4. 同一 repo / head の OPEN PR を検索する。**`existing` / `absent` と、`error-or-ambiguous` を区別する**。検索失敗・非 JSON・件数上限到達・複数件・別 repository 由来の同名 head・base 違いは `absent` とみなさず停止（write 0）。
5. 既存 PR が 1 件なら URL と実 PR の head / head SHA / base / 本文を独立に readback し、本文を D7 evaluator で再評価して `valid` なら **再利用**（create 0、本文は更新しない）。別 Issue を参照する PR・不正な reference の PR は報告して停止する。
6. 無ければ、D7 の `validate_pr_body.py --evaluate-reference-policy` に facts（`repo` / `issue_state` / `pr_number`=`null` / `decision_comment`）を渡す。`closing_required` は `Closes #N`、`nonclosing_required` は `Refs #N` が必要な場合に限り末尾へ最小追記し、**最終本文で再評価**する。`decision` が `fail_closed`、または `body_verdict` が `valid` でなければ停止する。evaluator の exit 0 は許可を意味しない。親 Issue への `Refs #<親>` のような合法な非 closing 参照は保持し、`Closes` 化しない。
7. `gh pr create --repo <trusted> --head <branch> --base main --title <title> --body-file <検証済みfile> --draft` を 1 回だけ実行し、作成後に PR を fresh に readback（repo / OPEN / head / head SHA / base / Draft / 本文 hash / reference）して確認する。
8. create の失敗・timeout・URL 不明は **再 create しない**。read-only の再検索結果（`probe`）だけを報告して非 0 で終了する。

## PR 本文の形式

呼び出し側が用意した現行の日本語見出し構成（例: `## 概要`、`## 検証` など）をそのまま使う。旧 PR template の 9 見出し、日本語比率チェック、LP050–LP058 の一般 lint はこの caller では強制しない。必要な reference 行以外は本文を書き換えない。

## 含まないもの

旧 `open_pr.py` の全面移管、`update_pr.py`、Task Context 通知、旧 Reviewer hook、Reviewer dispatch、Final Gate、live E2E。`validate_pr_body.py` の native auto-close risk（`--evaluate-native-auto-close-risk`）は merge 直前の別 owner の確認であり、起票時には実行しない。

PR 作成後の競合（race）を完全に排除するとは主張しない。
