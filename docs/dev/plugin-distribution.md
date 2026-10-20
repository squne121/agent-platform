# Stable Plugin 配布経路（I-2 pilot）

実測日 2026-10-20 / claude 2.1.304 / Issue #3。結果区分: **pass** / **fail** / **conditional** / **SKIP**(理由あり) / **未検証**。

## 構成
- Marketplace `ap-stable`（`.claude-plugin/marketplace.json`）。Stable の `ap-pilot`（skill `ap-ping`）だけを載せる
- `plugins/ap-pilot`: version は `plugin.json` のみで定義（entry には書かない）
- `plugins/ap-pilot-exp`: Experimental。**marketplace に載せない**。manifest name は `ap-pilot-exp`（Stable と別名）
- entry は `git-subdir` + **https URL** + 40 桁 `sha`。`owner/repo` 短縮形は SSH で clone されるため、SSH 鍵のない consumer で失敗する（実測）

## 版固定・更新・rollback
| 版 | sha | 内容 |
|---|---|---|
| 0.1.0 | `d7a9d2d74fcc78f5ada602b90ee39e99e65be642` | 初版 |
| 0.2.0 | `c9eff0631e1f3adb494c6fd40ac4adb6a129db29` | 現行 pin |

- 更新: `marketplace.json` の `sha` を新 commit に変更 → consumer で `claude plugin marketplace update ap-stable` → `claude plugin update ap-pilot@ap-stable --scope project`（反映は再起動）
- rollback: `sha` を旧 commit に戻し、同じ 2 コマンドを実行する（0.2.0→0.1.0 を実測）
- 緊急停止: `claude plugin disable ap-pilot@ap-stable --scope project`

## consumer 手順（project scope）
```bash
cd <consumer repo>
claude plugin marketplace add squne121/agent-platform --scope project     # 開発中は squne121/agent-platform#<branch>
claude plugin install ap-pilot@ap-stable --scope project
claude plugin list --json     # id=ap-pilot@ap-stable, scope=project, enabled=true, version 非空
```
検証は `env -i PATH=$PATH HOME=<tmp>/home CLAUDE_CONFIG_DIR=<tmp>/cfg claude ...` で実環境から隔離した。

## 実測結果
| # | 項目 | 結果 | 根拠 |
|---|---|---|---|
| 1 | `validate --strict`（root と各 plugin dir） | pass | exit 0 |
| 2 | Public marketplace 取得（認証なし、clean HOME、`#<branch>`） | pass（`#<branch>` 指定）/ **conditional**（ref なし） | clone 成功、credential 非使用。ref なし（main）は marketplace.json が main に入る merge 後でないと検証できない |
| 3 | project scope install と list | pass | `ap-pilot@ap-stable` 0.2.0 / project / enabled。`.claude/settings.json` に `enabledPlugins` と `extraKnownMarketplaces` |
| 4 | 通常 session: skill 解決（`claude -p` の init event） | pass | `skills` に `ap-pilot:ap-ping`、plugin path が cache/…/0.2.0、`plugin_errors` null |
| 5 | skill 本文の実行（model 呼び出し） | **SKIP** | 隔離 config は未ログイン（`apiKeySource: none`）。user の認証情報は複製しない |
| 6 | Experimental は Stable consumer で無効 | pass | list に `ap-pilot-exp` 無し。`--plugin-dir` 指定時のみ `ap-pilot-exp@inline` が session 限定で load |
| 7 | 同名 `--plugin-dir` の shadow | pass（挙動確認） | 無通知で `ap-pilot@inline` 9.9.9 が優先 |
| 8 | `ap-pilot@inline: false`（`.claude/settings.local.json`）で shadow 抑止 | pass | Stable 0.2.0 が load される |
| 9 | 更新 / rollback | pass | 0.1.0→0.2.0→0.1.0（`plugin list` で確認） |
| 10 | git worktree（consumer の linked worktree） | pass（条件付き） | consumer の \`.claude/settings.json\` が **commit 済み**なら同じ版・path が load される。未 commit のまま worktree を作ると \`enabled=false\`（再現） |
| 11 | `claude --bg` と Agent View（2 経路とも） | **SKIP** | 隔離 config は未ログインで session を起動できない。共通 Agent View に `--plugin-dir` は注入しない |
| 12 | `claude plugin list` の session plugin | 注意 | `--plugin-dir` を付けない list には `@inline` は出ない。shadow は init event で確認する |

経路別: 通常 session = 4、worktree = 10、`claude --bg` / Agent View = 11（SKIP）。

## 版固定の範囲と merge 方式
- `sha` が固定するのは **plugin 本体の commit** のみ。`marketplace.json` 自体は consumer の `extraKnownMarketplaces`（ref）で浮動するため、rollback は marketplace repo 側で `sha` を戻して push する操作が必要
- pin 先 commit は main の祖先として残す必要がある。**squash merge すると pin 済み commit が履歴から外れ**、将来取得できなくなる恐れがあるため、merge commit か rebase merge を使う（設計上の懸念で、取得失敗は未再現）
- `sha` と `plugin.json` の version の対応は上の表で人手管理（静的 test は 40 桁 hex までしか見ない）

## 未検証
- 本文の実行結果（5）、`--bg`・Agent View への継承（11）、auto-update、workspace trust ダイアログ経由の初回承認（`-p` では project plugin が load された）
- main に merge した後の `marketplace add squne121/agent-platform`（ref なし）

## 制約
- 実 skill / agent / hook の移行は対象外（I-3 以降）。permissionMode 依存 agent は Plugin では維持できない可能性がある
- Public のため marketplace / plugin にユーザー固有パス・token・内部 URL を含めない（`scripts/check_public_hygiene.sh`）
