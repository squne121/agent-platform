# claude-gpt shared runtime(Issue #57 移管候補)

> **状態: isolated migration candidate / OFF / 未配備。**
> 本ファイルと `scripts/claude-gpt/` は、loop-protocol から agent-platform へ shared runtime を移すための
> **候補**である。どの consumer もまだ本 launcher に切り替えておらず、live service・Remote Ops・Queue には
> 一切配備していない。公式サポートや production readiness を主張するものではない。

## 1. 出典(provenance)

| 項目 | 値 |
| --- | --- |
| source repository | `squne121/loop-protocol` |
| source HEAD | `f90a79927c176669a2d9fc5cac023ea20d34aefc` |
| agent-platform base HEAD | `8421f910b11bee4dd5a11654daa177de0d4f163f` |

shell 3 本は source から **byte-identical** で移す(本移管では一切編集しない)。

| file | SHA256(source commit の blob と candidate の両方で照合済み) |
| --- | --- |
| `scripts/claude-gpt/launch.sh` | `48d817ed8a58b50d402adaa7210e57d78c4e221ffa47b87667efd9e2aa89a26f` |
| `scripts/claude-gpt/lib.sh` | `99d0beff95b40b3b5b1473d3783bd531f9338d637f6dc077f7d55bfc79676166` |
| `scripts/claude-gpt/repair_proxy.sh` | `453647c1ab582433355851d87f58b9972b15e0e0f1813d25245ab6213e3930fe` |

- 2026-11-01、supervising process が上記 source commit の各 blob を `git show` で読み出し、
  candidate と byte comparison / SHA256 comparison を行った。3 本とも一致した。
- `launch.sh` / `repair_proxy.sh` は実行 bit 付き(`100755`)で追跡すること(`test_repair_proxy.py` が
  `repair_proxy.sh` を直接実行する)。
- shell 内コメントは source の文脈(source 側 Issue 番号、未移管の `preflight.sh` / `runtime_smoke_test.sh`、
  loop-protocol の hook 名など)をそのまま含む。byte-identity を優先して書き換えていない。

### test の移管差分(Python 側のみ)

- `tests/_launcher_harness.py` / `tests/test_connected_proxy_diagnostics.py` / `tests/test_repair_proxy.py`:
  全 regression test 関数を保持(skip / 削除なし)。変更は `ruff format`、`subprocess.run(..., check=False)` の明示、
  `__enter__` の `Self` 注釈、未使用 `noqa` 削除のみ。
- `test_repair_proxy.py` が読み込む fixture helper `tests/_proxy_model_compat_fixture_helpers.py` も
  dependency closure として同じ source commit から移管した。supervising process が source blob と
  candidate の byte identity を確認済み。SHA256:
  `df22feb448ce372fa99a104ca0f893ebf07e39da1614ec310ab9542299810674`。
- 新規 `tests/test_runtime_migration.py`: 本 Issue の移管 contract を subprocess で固定する(§6)。

## 2. 所有境界

| 所有者 | 範囲 |
| --- | --- |
| agent-platform | shared runtime(`scripts/claude-gpt/` の launcher・診断・repair helper・model alias 定義) |
| loop-protocol | project の Skills / Hooks / SubAgents(`LOOP_TASK_CONTEXT_RUNTIME_VARIANT` などを読む側) |
| Remote Ops | HOME / queue / session / worker の準備と lifecycle |

- 採用後の launcher と model mapping の正本は agent-platform の `lib.sh` / `launch.sh` とする。
  現在は未マージの候補であり、稼働中 consumer の正本はまだ切り替わっていない。Remote Ops 側や
  生成 profile に launcher・model mapping を**複製しない**。
- 現時点では loop-protocol 側に旧 source が残る。consumer の切り替えと旧 source の削除は、将来の
  **1 つの coordinated PR** で同時に行う。2 つの permanent owner を並存させる運用はしない。
- 既存の Claude native 起動経路は本移管で何も変わらない。

## 3. launcher contract(移管後も不変)

`launch.sh` は、既に起動している loopback `claude-code-proxy` へ Claude Code を向けるための env だけを追加し、
指定された `claude` を `exec` する薄い wrapper である。

- 追加する env(全て非 secret。`ANTHROPIC_AUTH_TOKEN` は固定 placeholder `unused`):
  `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` /
  `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` / `CLAUDE_CODE_AUTO_MODE_SERVER=0` /
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW` / `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` /
  `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1` / `LOOP_TASK_CONTEXT_RUNTIME_VARIANT=claude_gpt` /
  `CLAUDE_GPT_CLAUDE_BIN`。
- model alias(`gpt-6-sol[1m]` / `gpt-6-luna[1m]` の役割割当)は source の既存値を**そのまま保持**しているだけで、
  本文書は model の推奨をしない。
- しないこと: caller の cwd 変更、HOME / XDG / `CLAUDE_CONFIG_DIR` / `GH_CONFIG_DIR` / `SSH_AUTH_SOCK` の隔離・変更、
  `--settings` / permission mode / hook / MCP の注入、caller argv の加工、proxy の起動・停止・再起動。
- `--check-only` が成功しても、それは「接続先 loopback server に到達でき、`/v1/models` に必要な model ID が
  載っている」ことだけを示す。**認証・課金・account entitlement・推論可否の証明ではない。**
  catalog 上の alias(GPT-6 を含む)は account の entitlement を検証しない。

### opt-in 起動(provider qualification 後のみ)

```sh
cd <consumer>
sh "$PLATFORM/scripts/claude-gpt/launch.sh" --claude-bin <real-claude-binary> -- --effort high
```

- provider / route の qualification(§4 の未確認事項の解消)が済むまでは使わない。既定経路にはしない。
- `--claude-bin` には実 Claude binary を渡す。permission を緩める指定(bypass 系)を案内・追加しない。
  caller が `--` 以降に渡した引数はそのまま転送される。

### repair helper

`repair_proxy.sh` は source 互換のため保持するが、upstream の route / auth review が終わるまでは、
**採用(adoption)目的で実行しない**。実行した場合も binary を入れ替えるだけで、running server には触れない。

## 4. 確認済み事項と未確認事項(provider / route)

以下は supervising process(ChatGPT)が 2026-11-01 に公式資料で確認した内容の要約である。本移管 session では
GPT 経路の login・consent・推論・usage 計測を**一切行っていない**。
移管作業の coding agent は既存の Claude native subscription session で実行した。

- OpenAI の OSS / local 向け Sign in with ChatGPT(SIWC)は、ChatGPT plan による Responses 利用を
  `offline_access resource.invoke chatgpt.tokens.use.direct` で認可できる。利用側は granted scope に direct が
  含まれることを確認し、`https://api.openai.com/v1/responses` に `store:false` / `stream:true` で要求し、
  `response.completed` を待つ必要がある。公式 flow は ChatGPT backend-api へ向けないよう明記している。
- upstream proxy v0.1.48 / commit `49cf15220b2efa94b3d21c1a560d3762e961628d` の
  `src/providers/codex/auth/constants.rs` は `https://chatgpt.com/backend-api/codex/responses` を使用する。
  同 commit の `auth/pkce.rs` が要求する scope は `openid profile email offline_access` であり、
  `resource.invoke` / `chatgpt.tokens.use.direct` を要求していない。
  **この source は新 SIWC direct-inference flow を実装していない**。既存 proxy を移管するだけでは
  公式 SIWC 適合の受入を満たさない。これは「native Codex の利用が一律に禁止されている」という断定ではない。
- Anthropic は Claude 以外の model を LLM gateway 経由で使う構成をサポートしない。
- native Codex CLI が自身の login で動くことは別の公式経路であり、その auth file を抽出・再利用してよいという
  意味ではない。
- Codex app-server は orchestration / tools / agents を置き換えるため、Claude harness の drop-in 代替ではない。
  公開されている SIWC 設定は app 自身が認可した access token、`env_key`、`requires_openai_auth=false`、
  `supports_websockets=false`、refresh / restart / thread-resume を前提とする。
- 採用条件は API key / 従量課金への自動 fallback を行わないことである。現候補の launcher は
  ambient config を維持するため、任意の downstream proxy の課金方式まで強制・証明するものではない。
  実配備前に認証経路・利用枠・課金条件を別途検証する。

## 5. Remote Ops 統合に向けた制約(将来作業)

- Remote Ops の実 launcher `claude-remote` は専用 HOME を設定した後、`ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` を unset する。したがって `claude-remote` の path を
  本 launcher の `--claude-bin` に渡すと gateway 設定が失われる。
- 将来の統合では: Remote Ops が native 環境準備(HOME / session / worker)を行った**後に** shared platform
  launcher を再利用し、実 Claude binary を exec する。session / runtime identity を保持し、runtime は
  **新規 job ごとに明示選択**する。provider をまたぐ暗黙の resume / failover はしない。
- rollback は「native Claude で新しい session を開始する」ことであり、cross-provider resume を前提にしない。
- 本 Issue では Remote Ops の live launcher・runner を変更しない。Queue E2E は実施しておらず、その成立も主張しない。

## 6. test と CI

`scripts/claude-gpt/tests/` は fake loopback proxy、argv / env / cwd を JSON に書き出す fake `claude`、
`file://` の fake installer を使い、GPT login / 推論 / provider API 呼び出しを試験しない。
ただし、全 test が完全に親環境から隔離されているわけではない。

- 新規 `test_runtime_migration.py` と `_launcher_harness.py` の HOME / XDG / config は `tmp_path` の
  sentinel であり、新規 test の `GH_CONFIG_DIR` / `SSH_AUTH_SOCK` も sentinel である。
- 移管元の repair tests は親環境を継承し、主に `CLAUDE_GPT_HOME` を一時ディレクトリへ向ける。
  repair helper の `curl` には `-q` がないため、実行環境の `.curlrc` が影響し得る。
- launcher diagnostics は PATH 上の `claude-code-proxy --version` を実行し得る(上限 2 秒)。
  実 binary が入った開発機では、この補助診断まで fake とは限らない。proxy 起動や認証・推論ではない。
- CI / 開発試験では専用の一時 HOME と管理された PATH を使うこと。既存 test の構造を保持した今回の
  移管だけでは、任意の開発機における完全な hermeticity を保証しない。

`test_runtime_migration.py` が固定する contract:

- caller の cwd(platform root と異なる consumer directory)がそのまま `claude` に渡る。
- ambient の HOME / XDG_* / `CLAUDE_CONFIG_DIR` / `GH_CONFIG_DIR` / `SSH_AUTH_SOCK` と、launcher 所有以外の全変数が
  不変。sentinel を置いた user / project config が書き換わらない。
- launcher が追加する env は §3 の集合と完全一致し、`--dry-run` の `launch_env` とも一致する。
- caller argv(`--resume` を含む)が完全一致で転送され、`--settings` / permission mode / MCP / hook 系 flag が
  注入されない。
- fake `claude` の exit status がそのまま返る。
- catalog 不完全・HTTP error・到達不能・non-loopback・不正 URL のいずれでも fake `claude` は起動しない。
- `--check-only` と通常起動のどちらも fake server を所有・停止しない。
- catalog は返すが他の要求を全て拒否する server でも check が通る(= reachability のみで、認証・entitlement
  ではない)。launcher は `/v1/models` の GET 以外を送らない。

CI: 既存 `python-test` job に `uv run --locked pytest -q scripts/claude-gpt/tests` を 1 行追加しただけである
(新 job・新 gate・wrapper なし)。

## 7. 受入停止と未確認(UNVERIFIED)/ 残作業

**既存 Stage 1a テストとの衝突: FAIL。** 本候補では focused tests 83 件は成功したが、既存 `tests/` 全体は
1,243 件成功 / 1 件失敗だった。失敗は
`tests/stage1a/test_stage1a_2_canonical_preflight.py::test_no_launcher_or_proxy_dependency_in_migrated_chain`
の `scripts/claude-gpt` 自体が存在しないことを要求する末尾 assertion。移管前 base HEAD では同 test は成功する。
chain の launcher 非依存を検証する preceding assertions は維持・通過している。

本 Issue の現在の変更範囲には上記既存 test は含まれず、既存契約変更も承認されていないため削除・skip・迂回は
行わない。最小の解消候補は、Stage 1a の「chain は launcher に依存しない」保証を保ちつつ、repository 全体の
物理的不在という前提を、独立 runtime が併存できる検証へ置き換えること。これは承認後の follow-up とし、
現候補は **merge-ready ではない**。GitHub CI 成功や canonical loop の terminal approval を主張しない。

- upstream proxy 経路(ChatGPT backend-api)と SIWC の互換性、および auth / billing の扱い。
- 実 Claude binary + 実 proxy での起動、推論、usage 計測(いずれも未実施)。
- Remote Ops 統合(§5)と Queue E2E。
- consumer 切り替え・旧 source 削除の coordinated PR。

## 参考資料

- https://developers.openai.com/siwc/token-sharing-open-source
- https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
- https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
- https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server
- https://code.claude.com/docs/en/llm-gateway
- https://claude-code-proxy.raine.dev/providers/codex/

- https://github.com/raine/claude-code-proxy/blob/49cf15220b2efa94b3d21c1a560d3762e961628d/src/providers/codex/auth/constants.rs
- https://github.com/raine/claude-code-proxy/blob/49cf15220b2efa94b3d21c1a560d3762e961628d/src/providers/codex/auth/pkce.rs
