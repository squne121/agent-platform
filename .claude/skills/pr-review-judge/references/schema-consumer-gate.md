# Schema Consumer Inventory Gate

## schema_change_applicability 判定

- `schema_change`: schema 境界に影響を与える変更
- `not_schema_change`: consumer 変更なし
- `uncertain`: 判定不能 → fail-closed 扱い

`PR body` に以下の存在が必要。

- `Schema Change Applicability`
- `Schema Consumer Inventory`（schema_change/uncertain の場合）

## Approval 禁止条件

- `schema_change|uncertain` で Inventory 不在
- consumer 列挙結果の欠落 / 未対応（未対応が残る）
- `Compatibility Decision` が breaking/uncertain なのに followup がない

`not_schema_change` が明示され diff と整合していれば、Inventory は不要。
