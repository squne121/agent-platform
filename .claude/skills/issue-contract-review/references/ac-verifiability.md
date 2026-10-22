# AC 検証可能性チェック

| 項目 | 判定 |
|---|---|
| AC チェックボックス | `- [ ]` 形式 |
| AC ⇔ VC 整合 | `# ACn` と VC 対応 |
| 決定論的 | `grep` / `test -f` / `pnpm test` 等 |
| 主観文言回避 | 「適切」など意味的表現なし |

`match-ssot.*` 系で `--keywords` + `--paths` の同時指定を使う場合、`trivially_pass` となるため別途 AC を追加で対応できるか確認。
