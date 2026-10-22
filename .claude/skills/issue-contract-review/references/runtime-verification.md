# Runtime Verification Applicability

`## Runtime Verification Applicability` の `decision` により判定。

- `not_applicable`: 本スキルではスキップ
- `deferred`: フェーズ分割。後続 Issue/フェーズで確認
- `immediate`: 以下を確認し不足あれば blocked
  - applicable ACs が明示
  - 実行環境前提（CLI・認証・ネットワーク）
  - SKIP/exit 77 規約
  - fallback 成功を PASS にしない明示
  - artifact 証跡要件（出力先・ファイル名）

存在しない場合の実装 issue:

- `BLOCKED` または `human_judgment`（fail-closed）
