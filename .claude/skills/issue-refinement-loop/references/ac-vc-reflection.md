# AC / VC の反映確認

## 目的

review 段階で本文品質を評価するとき、baseline 未実装状態と実装後 gate を取り違えないための reference。

## 判定規則（SubAgent 所有）

AC/VC の baseline fail 判定、reflection guard、および rewrite 時の期待動作に関する詳細は、既存 Issue を更新する `.claude/agents/issue-editor.md` の **FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 の rewrite payload 契約** セクションを参照すること。`issue-creator` は新規起票専用であり、この rewrite route には使用しない。

orchestrator はこれらの判定ロジックを再実装せず、SubAgent 側の自律的判断に委譲する。

## 書き換え時の防御（統括層）

- `reviewer_feedback_text` は不透明な転送用 payload として扱う。
- anchor comment が絡む場合も、生の snapshot ではなく正規化済み `anchor_comment_feedback` だけを `issue-editor` へ渡す。

## FAIL_CLOSED_REWRITE_CONSTRAINTS_V1 の転送

`fail_closed.required == true` の場合、orchestrator は `fail_closed.rewrite_constraints`（`FAIL_CLOSED_REWRITE_CONSTRAINTS_V1` スキーマ）を `issue-editor` への rewrite 入力に含める必要がある。

- `issue-editor` は `required_sections` / `required_contract_keys` / `rewrite_constraints` フィールドを受け取り、不足セクション・不足契約キーの補完を優先する
- `rewrite_constraints.freeform_rewrite_forbidden == true` の場合、`issue-editor` は自由文形式の改変を拒否する
- `human_decision_reframe` による override が許可されていても、`FAIL_CLOSED_REWRITE_CONSTRAINTS_V1` を無視してよいわけではない

### rewrite 後の自動再検証（2 段構成）

`issue-editor` による rewrite 完了後に以下の検証を順に実施する:

1. **pre-mutation dry-run checker（変更前の試行検査）**: mutation 前に新本文の静的検証（`check_issue_contract.py` 相当）を dry-run で実施する
2. **post-mutation fresh checker（変更後の再取得検査）**: `gh issue edit` 実行後に GitHub から本文を再取得し、静的検証を再実施する

`post-mutation fresh checker` が exit 0 以外の場合、Review に進まず Rewrite を継続する。この再検査は、変更後にGitHubから取得した本文だけを対象にし、変更前の結果で成功扱いにしない。

### max_rewrite_attempts / no-progress 検出

- `FAIL_CLOSED_REWRITE_CONSTRAINTS_V1.max_rewrite_attempts: 2` — この回数を超えて同一 fail_closed を解消できない場合は `human_judgment_required` へ遷移する
- **no-progress detection（進捗なしの検出）**: 連続する 2 rewrite で `checked_body_sha256` が変化しない場合、前進がないと判定し `human_judgment_required` を返す

## 禁止事項

- verification owner が異なる VC を refinement loop 側で再分類しない
- SubAgent 側の reflection 判定を orchestrator 側で再解釈しない
- `human_decision_reframe` を validation bypass として扱わない（`FAIL_CLOSED_REWRITE_CONSTRAINTS_V1` は必ず転送する）
- `never_override_reason_codes`（`unknown_issue_kind` / `issue_kind_policy_load_error` / `contract_schema_parse_error` / `template_resolution_error` / `checker_internal_error`）に対して override を許可しない
