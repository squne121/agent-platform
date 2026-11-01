# Stage 2-7 → Pilot-1 引継ぎ（#29 S2-AC6）

Issue #37（Stage 2-7）は Step 4 → 4.5 → 5 の terminal path を fake `gh` の実 subprocess で検証するまでを扱う。
**実 GitHub の Pilot-1 は本変更では実施しない（実施しない理由: 別工程、人間承認が必要）。** 以下は #29 へ引き継ぐ実施条件である。

## 前提（Stage 2-7 で確定したこと）

- capability の mutation route は `issue_comment` / `issue_edit` / `issue_create` のみ `available`。`permission` は常に `unverified`
  で、route の存在は GitHub 書き込み権限の証明ではない。
- `pr_*`、label / state、native parent / dependency / relationship、未登録 operation は `unavailable`。
- 証跡は fixture（fake `gh`）であり、live GitHub 証跡ではない。

## Pilot-1 実施条件

1. **dedicated_docs_only_issue**: Pilot-1 専用に `squne121/agent-platform` へ作成した docs-only Issue 1 件だけを対象にする。
   実装 Issue・親 Issue・既存の進行中 Issue は対象にしない。対象 Issue 番号は #29 に事前記録する。
2. **before_after_digest**: 実行前（before）と実行後（after）の本文 SHA-256 と `updatedAt` を記録する。Step 4 の
   `expected_previous_body_sha256` / `expected_previous_updated_at` は before 値と一致させる（stale なら write-zero で停止）。
3. **readback**: 本文更新後と終了コメント投稿後に、それぞれ fresh な読み戻しで要求内容との一致を確認する。
   gh の終了コードだけを成功根拠にしない。コメントは marker 付きで 1 件だけであること（duplicate は失敗）。
4. **status_decision**: 各 Step の `status`（`ok` / `no_change` / `failed*`）と、Step 4.5 gate の `decision`
   （`proceed` / `block_retryable` / `block_persistent`）を記録し、`proceed` 以外では Step 5 の承認終了を投稿しない。
5. 依存関係（native `blockedBy`）を持たない経路だけを対象にする。native relationship 経路は #46 の承認が下りるまで
   fail-closed のままであり、Pilot-1 の対象外。

## 未解決の人間判断（Pilot-1 の前提を変えうるもの）

- #46: native relationship（native parent / dependency）の承認可否と deferred 37 件の扱い。
- #45: `ISSUE_EDIT_TXN_RESULT_V1` の canonical schema と producer 出力の正本選択。
- #44: hygiene C9 が parent 種別本文で exit 2 を返す件（parent 種別 Issue は Pilot-1 の対象にしない）。
- #50: create-issue Skill の未割当 dangling 依存の owner 決定（Pilot-1 は create-issue を使わない）。
