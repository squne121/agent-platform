# Claude-GPT ローカル実機受入(Issue #64)

非秘密の UAT 報告。実施: 2026-11-03(WSL host)。Claude Code 2.1.316 / CLIProxyAPI 8.0.51(commit 7cad1cb7)。
認証は #63 で付与済みの専用 SIWC 領域を CLIProxyAPI 自身が使用(資格情報の読取・複製・再ログインなし)。
proxy は 127.0.0.1 の専用 UAT process(PID・port 照合済み)。consumer は隔離 HOME と小さな fixture repo。

証跡区分: **[mock]** = offline dummy key の pytest、**[real]** = 実 SIWC・実 GPT 推論。

| AC | 内容 | 区分 | 結果 |
|---|---|---|---|
| AC1 | 認証なし=401/exit 7、正 key=200、誤 key=401、非 loopback 拒否、秘密非出力 | mock | PASS |
| AC1 | 実 CPA に対する `--check-only`(正 key で HTTP 200・catalog 確認)、匿名 `/v1/models`=401 | real | PASS |
| AC1 | 起動後の `/v1/responses` `response.completed` | real | PASS(#63 + 本 UAT の `claude -p` 成功)。API 課金キー/旧 backend への fallback は設定していない |
| AC2 | 明示 `launch.sh` 経路と native(`ANTHROPIC_*` 未設定)の区別、tmux 対話 TTY で 2 ターン(MANGO-64 を記憶→想起、a.txt Read) | real | PASS。global settings/credentials の size・mtime は不変 |
| AC3 | Read / Write / Edit / Bash(`-p`, `--allowedTools` 明示、bypass なし)。a.txt 変更・b.txt 作成・PostToolUse hook 4 回 | real | PASS |
| AC4 | Skill → Agent(`uat-child`)起動・子の Read・親が `CHILD:HELLO-fixture` を受領(stream-json で `parent_tool_use_id` 付きイベント確認)。modelUsage は GPT のみ | real | PASS(並行 2 SubAgent は未実施) |
| AC5 | Skill(token 取得)・Hook 発火 | real | PASS |
| AC5 | local MCP | - | UNVERIFIED(必要な既存 local MCP なし) |
| AC6 | `-p --output-format json`: exit 0 / `subtype=success`。`stream-json --include-partial-messages`: delta 113 件。key 出力 0 件 | real | PASS |
| AC7 | `--session-id` → `--resume` で記憶・cwd 継承。native 会話の暗黙 resume はしていない | real | PASS |
| AC8 | pytest / ruff / hygiene / 独立レビュー | - | PR 本文参照 |

## モデル
- `gpt-5.6-luna`(`CLAUDE_GPT_MODEL_*` で明示): AC3〜AC7 の実推論。modelUsage=`gpt-5.6-luna`。
- 既定 `gpt-6-sol[1m]`: `claude -p` 1 回成功(modelUsage=`gpt-6-sol[1m]`)。
- catalog 掲載のみで利用権限 PASS とは扱わない。

## 注意
- TUI ヘッダーの「API Usage Billing」表示は `ANTHROPIC_AUTH_TOKEN` 使用時の Claude Code 側ラベルで、実バックエンドは CPA→SIWC。ChatGPT 側の使用量計上は UNVERIFIED(恒久 gate にしない)。
- Claude Code の非 Claude gateway 利用は Anthropic 公式サポート対象外。本 PASS は独自互換性の実証。
