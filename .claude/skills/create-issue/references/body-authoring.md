# Issue Body Authoring Reference

Issue 本文の生成・更新に関する共通参照ガイドライン。
`create-issue` skill（新規起票）と `issue-editor` SubAgent（既存 Issue 更新）が共通参照する。

> 旧 `issue-body-authoring` skill を本ドキュメントに統合した（独立 skill である必要がなく、共有参照は references/ に置くのが skill ベストプラクティス）。

## Machine-Readable Contract Block Guidance（契約ブロックの運用ガイド）

Issue 本文に `## Machine-Readable Contract` がある場合、author / caller は以下を守る。

- block 全体を削除・移動・prose 化しない。変更が必要なときは YAML key の値だけを更新する
- issue kind ごとの required key を維持する:
  - `parent`: `contract_schema_version`, `issue_kind`, `goal_ref`, `change_kind`, `parent_mode`, `closure_mode`
  - `implementation` / `research`: `contract_schema_version`, `issue_kind`, `parent_issue`, `goal_ref`, `change_kind`
- `parent_mode` は `delivery-rollup | quality-gate | routing-map | decision-log` の closed enum
- `closure_mode` は `child-complete | measurement-ready | quality-validated | routing-complete | decision-recorded` の closed enum
- `parent_mode` と `closure_mode` の互換: `delivery-rollup → child-complete` / `quality-gate → measurement-ready | quality-validated` / `routing-map → routing-complete` / `decision-log → decision-recorded`
- `<required: ...>` placeholder や enum 外値は missing と同様に invalid
- `## Required Skills` / `## Rules` は prose section を正本に維持し、block に二重化しない
- block 更新時は対応する prose（`## Outcome` / `## In Scope` / `## Parent Issue` 等）と矛盾していないか同編集で確認する

## Parent Issue Authoring Guidance（親 Issue 執筆ガイド）

- parent issue は `parent_mode` と `closure_mode` だけで close 契約を済ませず、`## Quality Decision Record` と `## Parent Closure Rule` を本文に置く
- `quality-gate` parent は child close 数ではなく Quality Decision Record の確定を close 判定に使う
- `closure_mode` と `Quality Decision Record.Status` は同一編集で更新する。不一致は invalid で close 不可
- `quality-gate + child-complete` のような互換しない組み合わせは invalid
- `routing-map` / `decision-log` parent でも close 条件を本文に prose で残し、本文だけで agent が close 契約を再解釈できるようにする

## Required Skills / Rules Authoring Guidance（Required Skills・Rules 執筆ガイドライン解説）

Issue 本文の `## Required Skills` は **ドメイン知識スキル** のみを列挙する（例: TypeScript / ECS / Canvas / Vitest BDD 等）。

- ワークフロー skill（`issue-contract-review` / `implement-issue` / `pr-review-judge` / `ssot-discovery` 等）はここに書かない。skill 間で関係性が定義されているため
- rule 参照は per-directory `CLAUDE.md`（Claude Code 自動ロード）と `ssot-discovery` skill で行うため、`## Rules` への列挙は最小限に
- spec / doc / path reference（`docs/adr/...`, `docs/product/requirements.md` 等）は skill ではない。`## Background` / `## In Scope` の適切な section に書く
- runtime dependency がない場合は `- なし` と明記するか、section 全体を省略する

## ワークフロー不具合検出時の修正方針起案ガイダンス（決定論的修正優先）

follow-up Issue 起票時、prompt 注記による workaround を即時起案するバイアスを避けるため、以下の 3 ステップで修正方針を検討する。

**ステップ 1: 根本原因の特定**
「どこ（スクリプト・コード・設定ファイル・運用ルール）の何が、どのように動作を変えたのか」を明記する。

**ステップ 2: 決定論的修正の検討**
スクリプト・設定・コード側の決定論的修正で解決可能か検討する。再現性・テスト可能な修正案を Outcome の第一候補として記載する。

**ステップ 3: workaround との明示比較**
決定論的修正が困難・過剰コスト・スコープ外の場合のみ workaround を採用する。「決定論的修正は検討したが、以下の理由で workaround を採用」と明示する。
prompt 注記による SubAgent 委譲・運用ルール追加は次点オプション（anti-pattern: workaround の即時起案）。

### 適用範囲

ワークフロー不具合を起票する follow-up Issue（post-merge-cleanup / issue-refinement-loop から自動抽出されるもの）に適用する。単純なバグ fix や軽微な改善 Issue では本ステップ 3 の明示比較を省略してよい。

## Issue-Editor Repair Contract by Blocker Category（Blocker 種別ごとの修復契約）

`issue-editor` が `REVIEW_ISSUE_RESULT_V1.structured_blockers` を受け取ったとき、`category` フィールドに応じて以下の修復手順を適用する。

### compound_command_disallowed

**検出条件**: VC コマンドに shell control operator（`&&`, `||`, `|`, `;`, `&`, `<<`, `>>`, `<`, `>`, `<<<`）が含まれる。

**修復方針（優先順）**:
1. VC を単一コマンドに分割する（AC を複数エントリに分割することを検討する）
2. compound が避けられない場合は `# preflight-scope: pr_review_only` または `# preflight-scope: runtime_only` マーカーをコマンド直前行に付与する

```bash
# 例: pnpm build && echo DONE → 分割
# AC1
$ pnpm build
# （DONE 確認は PR レビュー時に実施）
```

### unexpected_pass

**検出条件**: VC が実装前 baseline で exit 0 を返す（VC が緩すぎるか、すでに機能が存在する）。

**修復方針**:
- VC を「baseline で fail するコマンド」に変更する
- 存在確認 VC では `rg` / `test -f` で「対象が**存在しない**こと」を確認する形式にする
- 例: `rg -q "new_feature" file.py` → 実装前に機能が存在しないファイルを対象にする
- または `test -f /path/to/new/file` で未作成ファイルの不在を確認する（不在で exit 1）

### regression_gate fails（regression_gate 失敗時）

**検出条件**: `pnpm typecheck` / `pnpm lint` / `pnpm test` / `pnpm build` / `uv run pytest <existing>` が baseline で fail する。

**修復方針**:
1. まずコマンド自体の誤りか、tooling/env blocker かを切り分ける
2. 正しい回帰ゲートコマンドに修正する（パス誤り・設定誤りの場合）
3. 環境起因で baseline が壊れている場合は `human_judgment` として人間対応を依頼する
4. `pnpm build` が `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` / `Aborted removal of modules directory due to no TTY` で失敗した場合は、Issue body を `# preflight-scope: runtime_only` に書き換えて回避しない。これは `body_author_fixable=false` の tooling/env blocker であり、downstream bucket は `env_or_runtime` として扱う
5. `CI=true pnpm build` の shell env prefix や `env CI=true pnpm build` の wrapper で回避しない。`shell=False` 実行モデルと allowlist 境界を崩すため

### rva_immediate_field_missing

**検出条件**: `decision: immediate` の `## Runtime Verification Applicability` セクションに必須フィールドが不足している。

**必須フィールド（すべて必要）**:
- `applicable_acs`: 動作検証の対象 AC リスト
- `execution_environment`: 必要な CLI ツール名・認証方法・ネットワーク要件
- `skip_conditions`: 実行環境が整っていない場合の停止条件（exit 77 SKIP 規約）
- `fallback_policy`: フォールバック経由の成功を PASS としない旨の明示
- `artifact_requirements`: 証跡出力先・ファイル名パターン

**修復方針**:
- `fix_hint` に示された不足フィールドを補完する
- 各フィールドの詳細要件は `docs/dev/runtime-verification-policy.md` を参照する

## Runtime Verification Applicability

Issue 起票時に動作検証の適用判定セクションを記載する。`runtime-verification: true` タグ付き AC の起票は `decision: immediate` のときのみ必要。

```markdown
## Runtime Verification Applicability

- decision: not_applicable | immediate | deferred
- reason: <判定理由>
- if immediate: 対応 AC / VC / 証跡要件（policy.md の decision ごとの要求事項を参照）
- if deferred:
    - deferred_destination:
        - destination_type: issue | phase | milestone
        - destination_ref: <Issue番号 / フェーズ名 / マイルストーン名>
    - deferred_verification_condition: <検証が成立するために必要な条件の説明>
```

| decision | 意味 | 実動作検証 AC の要否 |
|---|---|---|
| `not_applicable` | 静的検証のみで完結 | 不要 |
| `immediate` | 本 Issue 内で動作検証が成立する | 必要 |
| `deferred` | 統合フェーズ・後続 Issue で初めて成立する | 不要（後続で提出） |

- `deferred` の場合は `deferred_destination`（destination_type + destination_ref）と `deferred_verification_condition` の **両方を必ず記載する**（`review-issue` C10 blocker）。
- 自由記述の「後続 Issue で検証する」だけでは不完全。機械的に検出できる半構造化フォーマットで記述すること。
- 適用判定の詳細基準は `docs/dev/runtime-verification-policy.md` の「Runtime Verification Applicability」を参照する。

### `actual`/`canonical` ランタイム受け入れと `fixture` 意味論との AC 分離規則（Issue #2807）

AC が **actual / canonical / default runtime selection**（例: current-head production launcher が接続する server（`ANTHROPIC_BASE_URL` の向き先）の診断結果）を要求する場合、fixture / mock server を明示注入する hermetic test（fixture-only VC）を、その AC の唯一の evidence にしてはならない。

少なくとも次の2種類の AC を区別して起票する。

1. **fixture semantics AC**: catalog mismatch / repair / precedence 等のロジックを hermetic fixture（fake binary injection 等）で検証する AC。
2. **canonical runtime acceptance AC**: current-head production launcher を external process として起動し、actual connected server の diagnosis（`launch_check_only.connected_server`）と `--check-only` 相当の結果を観測する AC（connected-server AC）。launcher は proxy を選択・起動せず、`ANTHROPIC_BASE_URL` が実際に向く running server だけを診断する。

fixture semantics AC の PASS は canonical runtime acceptance AC の代替にならない。**両方が必要な場合は、単一の AC/VC に混在させず、別々の AC と別々の VC に分離して起票する**。1つの AC に fixture-only VC と「real smoke は SKIP でよい」という記述を同居させ、それを canonical runtime acceptance の充足として扱わない。

**canonical runtime acceptance evidence の必須フィールド**（fixture / mock server に向けた check-only の evidence のみでは不十分）:

- `run_head_sha`
- `git_dirty`
- 実行 command identity（実際に起動したコマンド文字列 / invocation）
- launcher hash
- actual connected server の diagnosis（`connected_server` の `base_url` / `reachable` / `model_catalog_ok` / `classification`）

上記の必須フィールドと既存 producer `CLAUDE_GPT_SMOKE_RESULT_V1`（`scripts/claude-gpt/runtime_smoke_test.sh`）の実 schema との対応（新しい schema field は追加しない）:

- `run_head_sha` := `CLAUDE_GPT_SMOKE_RESULT_V1.sut.git_head`
- `git_dirty` := `CLAUDE_GPT_SMOKE_RESULT_V1.sut.git_dirty`
- launcher hash := `CLAUDE_GPT_SMOKE_RESULT_V1.sut.launch_sh_sha256`
- actual connected server の diagnosis := `CLAUDE_GPT_SMOKE_RESULT_V1.launch_check_only.connected_server`（`launch.sh --check-only` の `CLAUDE_GPT_LAUNCH_RESULT_V1.connected_server` と同一。**接続先 authority はこれのみ**）
- `launch_check_only.local_proxy_binary_auxiliary`（PATH 上の `claude-code-proxy` binary の path / version）は **非 authority の補助診断** であり、接続先 server が動かしている binary とは限らない。evidence 要件を満たす根拠にしない
- 接続先 server の version / hash は現行 interface から観測できないため **「未確認」** と記録する（`connected_server.version` は常に `"未確認"`。PATH 上の binary の version を server version として代用しない）
- 実行 command identity := authoritative evidence に束縛された literal command 文字列とその command SHA256

fixture / mock server に向けた check-only の evidence（`ANTHROPIC_BASE_URL` を fixture / mock server へ向けた結果や、PATH 上の binary の path / version のみを記載した evidence）は、canonical runtime acceptance AC の充足として **明示的に不十分** である。catalog / connected-server AC は `ANTHROPIC_BASE_URL` を fixture / mock server へ向けない current-head production `scripts/claude-gpt/launch.sh --check-only`（接続先 server の `connected_server` 診断。catalog 不足は `reason: model_alias_not_resolved` / `cause: connected_server_model_catalog_incomplete`）を canonical acceptance の最低限とし、`connected_server.model_catalog_ok` は `/v1/models` に required model が列挙されていることだけを示す（実 ChatGPT subscription request の受理や provider fallback の不在の証明ではない）。`CLAUDE_GPT_PROXY_BIN` は補助 binary path の解決にのみ影響し接続先 server の authority ではないため、fake `CLAUDE_GPT_PROXY_BIN` の有無を connected-server AC の充足判定に使わない。authenticated request / transport の意味論自体を AC が要求する場合に限り `scripts/claude-gpt/runtime_smoke_test.sh` の full smoke を追加要求する。full smoke の認証不足 SKIP は catalog-only AC を failure 扱いしないが、authenticated request / transport AC 自体を PASS にはしない。canonical runtime evidence の取得には、新しい harness を作らず既存の `scripts/claude-gpt/launch.sh --check-only` / `scripts/claude-gpt/runtime_smoke_test.sh` のような current-head production 実行資産を参照する。

### 実行時検証プロファイルの assertion binding 記法（`runtime_assertion_bindings`, Issue #2771）

`docs/dev/extension-surface-runtime-policy.yaml` の risk-trigger rule に **hard** で一致する Allowed Paths を宣言した Issue は、その rule が要求する `verification_profile` の `assertions[]`（証明すべき postcondition）を、どの runtime AC が担うかを `runtime_assertion_bindings` として明示する。`enforcement == advisory` のみで到達した profile / assertion は required set に含まれない（PR #2370 の advisory non-blocking 方針を継続する）。

**canonical wire format**（entry ごとに `disposition` を選ぶ。`vc:` やコマンド本文をここに複製しない — VC 本体は既存の `## Verification Commands` の `# AC<N>` 参照で紐づける）。`disposition` は閉じた enum（`dispositive` / `non_dispositive_readiness_compat` / `not_applicable`）で、disposition ごとに使える key 集合も閉じている。集合外の key を足した entry は malformed として拒否される:

```yaml
## Runtime Verification Applicability

decision: immediate
applicable_acs:
  - AC3
runtime_assertion_bindings:
  # (1) 今回の runtime AC が検証する（key 集合: profile / assertion / ac / disposition）
  - profile: skill-invocation-runtime-smoke
    assertion: procedure_steps_executed_in_declared_order
    ac: AC3
    disposition: dispositive
  # (2) 別の既存 AC が substantive verification を所有する（key 集合: profile / assertion / disposition / demonstrated_by / reason。ac は書かない）
  - profile: hook-chain-runtime-smoke
    assertion: sibling_side_effect_inventory_complete
    disposition: non_dispositive_readiness_compat
    demonstrated_by: AC18
    reason: "deny 経路の fault-injection pytest は AC18 が所有する"
  # (3) 今回の change surface に対象の振る舞い自体が存在しない（key 集合: profile / assertion / disposition / reason。ac / demonstrated_by は書かない）
  - profile: subagent-lifecycle-causal-evidence-smoke
    assertion: subagent_start_stop_causal_evidence_correlated
    disposition: not_applicable
    reason: "scratch から SubAgent への handoff が production path に存在しない"
```

- `disposition` を省略した `profile` / `assertion` / `ac` の旧 3 field 形式は読み取り互換として受理され、内部では `dispositive` 相当（`disposition_source: legacy_default`）に扱われる。ただし「実証済み」の意味は付与されないため、新規に起票する Issue は `disposition` を明示する。
- `demonstrated_by` は **実在する既存 AC の参照（`AC<N>`）のみ**を書く。当該 AC は Acceptance Criteria に実在し、`## Verification Commands` に `# AC<N>` canonical reference を持たなければならない（`applicable_acs` / runtime-verification タグへの追加は不要）。test path / pytest node-id は書かない。
- `reason` の機械検証は非空文字列のみである。`: ` や `#` を含む理由は YAML の誤解釈を避けるためダブルクォートで囲む。

**生成規則（この順序で判断する）**:

1. policy から candidate assertion を得る: Allowed Paths が hard selector に一致する rule ごとに、その `verification_profile` の `assertions[]` 全件（`docs/dev/extension-surface-runtime-policy.yaml` の `verification_profiles.<profile_id>.assertions[].id`）を洗い出す。同一 `verification_profile` が複数 selector（enforcement が異なるものを含む）でマッチした場合でも、profile 単位でいずれか 1 つが hard なら全 assertions が candidate になる。複数 profile が同時に要求される場合（Issue #2467 のような同時適用）は、各 profile の assertions を漏れなく列挙する。
2. candidate ごとに、今回の change surface に対する applicability を分類する（その振る舞いが今回の変更に存在するか）。
3. applicable なものだけ、substantive verification の宛先（`ac`）を結ぶ（`disposition: dispositive`）。宛先の AC は `applicable_acs` に含まれ、`<!-- runtime-verification: true -->` タグを持ち、`## Verification Commands` に `# AC<N>` canonical reference がある runtime AC とする。
4. 別の既存 AC が substantive verification を所有する場合に限り `non_dispositive_readiness_compat`（`demonstrated_by` + `reason`）を使う。compat は runtime PASS ではなく、通ったからといって実証済みとは扱われない。
5. 対象の振る舞い自体が今回の change surface に無い場合は、`not_applicable` と理由（`reason`）を必ず書く。
6. 構造上の完全性を作るためだけに、dummy AC（ダミー AC）・人工的な SubAgent・空の runtime log・無関係な marker へ押し込んで bind してはならない。

`not_applicable` の `reason` に使えない理由: 「runner が無い」「認証できない」「test が未実装」は `not_applicable` の理由にならない。対象の振る舞いが今回の変更に関係するのに未観測の assertion は unverified であり、`not_applicable` でも PASS でもない。実証できない事情がある場合は、その事情を Stop Condition / Scope 分割として扱い、`not_applicable` で隠さない。これらの理由の妥当性は既存 semantic review の責務で、machine 検証は非空 `reason` と AC の実在・VC 参照までである（機械的に強制されるとは主張しない）。

**識別子の組み合わせ（composite identity）と 1 key = 1 binding の制約**:

- 各エントリの識別子は `(profile, assertion)` の組（composite identity）である。`assertion` の `id` は policy 上 profile 内でのみ一意性が保証されるため、`profile` を含めない `assertion` 単独を識別子にしてはならない。
- **1つの `(profile, assertion)` key は高々1つの binding にのみ宣言できる**。同じ key を2回以上宣言してはならない（disposition の組合せを問わず duplicate として拒否される）。policy が candidate としない key への `not_applicable` も unknown として拒否される。
- 一方、**複数の異なる key が同一の `ac` を指すことは許容される**。1つの AC/VC が複数 assertion の evidence を兼ねてよい。ただし、その assertion を実際に実証できているかの判断は semantic review の責務であり、実証できない assertion を手順実行を証明する AC に同居させて structural completeness を作ってはならない（上記 6）。

**structural completeness と semantic sufficiency の責務境界**:

`runtime_assertion_bindings` の機械判定（`scripts/agent-guards/extension_surface_policy_matcher.py` の `evaluate_runtime_assertion_binding_coverage()`）が保証するのは、宣言漏れ・未知宣言・重複宣言の不在と binding 先 AC の referential integrity（実在確認 / `applicable_acs` 包含 / runtime-verification タグ整合 / VC canonical reference 存在）という **structural completeness** のみである。binding された VC が assertion の意味を実際に証明しているか（**semantic sufficiency**）は判定しない。この境界は Issue 起票時にも維持し、「binding が揃っている」ことを「動作検証の意味的な十分性が保証された」と混同しないこと。`dispositive` の宣言は「実行済み / 観測済み」ではなく、`non_dispositive_readiness_compat` / `not_applicable` は runtime PASS ではない（成功ではなく、別 AC への参照または対象外の宣言である）。`disposition` 省略（旧 3 field 形式）も「実証済み」を意味しない。`reason` / `demonstrated_by` の妥当性も判定しない。分類は `review-issue` / `issue-contract-review` の公開結果に非 blocking の `runtime_assertion_disposition_classification` として現れるが、これも実証の根拠ではなく分類の記録である。

## VC 作成ガイダンス

> 動作検証 AC（`runtime-verification: true`）を含む VC の設計は `docs/dev/runtime-verification-policy.md` を参照すること。
> SKIP 規約（exit 77）・証跡保存フォーマット・テストシナリオ最小セット・Stop Condition 連動が定義されている。

### pytest baseline-fail VC の正規形（canonical form, marker: pytest_baseline_fail_canonical_rules）

pytest で baseline-fail（`expected_baseline_fail`）を表す VC は、原則として**まだ存在しないテストファイルへの node-id** で記述する（`missing_new_test_file.py::test_name`）。既存ファイルに対する `-k <未作成関数>` は禁止形であり、`vc_baseline_shape_compiler.py`（`.claude/skills/issue-contract-review/scripts/vc_baseline_shape_compiler.py`）が machine-readable な rewrite hint を返す。

既存の Allowed Paths 内テストファイルに新しい test class/function を追加する意図を明示する場合だけは、既存ファイルの missing node-id の直前に `# baseline-expect: fail` を置く。この annotation は `baseline_vc_preflight.py` の既存 escape hatch を選択し、compiler も元のファイル・node-id を維持する。annotation なしの existing-file missing node-id は従来どおり禁止形である。

| 禁止形 | 推奨形（canonical） | 理由 |
|---|---|---|
| `pytest existing_test_file.py -k test_new_name` | `pytest missing_new_test_file.py::test_new_name` | 既存ファイルへの `-k` 新規テスト名指定は pytest exit 5（no tests collected）で `vc_no_tests_collected` として hard block される |
| `pytest existing_test_file.py::test_missing_name`（annotation なし） | `pytest missing_new_test_file.py::test_missing_name` | annotation なしでは既存ファイルへの missing node-id は判定不能。意図的な追加は直前の `# baseline-expect: fail` で明示する |
| `pytest existing_test_file.py -k "test_a or test_b"`（boolean / class selector / parametrized selector） | 対象 AC を分割し、それぞれ missing-file node-id で記述する | 複合 `-k` 式は `not_autofixable`（compiler が安全に書き換えられない） |

- `-k` の書き換え対象は単一 bare `test_*` identifier のみ。boolean 式・class selector・parametrized selector は書き換えない。
- 既存ファイルの missing node-id 判定は `ast.parse()` による静的解析（top-level `def test_*` のみ）に限定する。class/method や parametrization は対象外。
- 生成される missing-file node-id は Allowed Paths に含まれる場合のみ提案される。安全な候補が無い場合は書き換えない。

### 実装後に検証する新規 pytest VC は `pr_review_only` を使う（deferred を pr_review_only の代用にしない）

baseline に存在しない新規テストを実装後の独立検証（Step 4 / `pr-review-judge`）で確認する pytest VC には、コマンド直前行に正規の `# preflight-scope: pr_review_only` を付ける。`# baseline-expect: deferred` は baseline で実行を省略する点が似ているだけで、producer の出力契約と Step 4 の `step4-adjudicate` での受理可能性は同じではない（`unsupported_baseline_classification` で拒否される）。**deferred を pr_review_only の代用にしない**。

```
# AC2
# preflight-scope: pr_review_only
$ uv run --locked pytest <Allowed Paths 内テストファイル> -q -k "<新規テストの識別子>"
```

- 正規 `preflight-scope: pr_review_only` / `runtime_only` が付いた VC は、`vc_baseline_shape_compiler.py` と hygiene の書換え対象にならず、コマンドは保持される。
- 正当な `baseline-expect: pass|fail` の用法（既存テストの回帰確認・既存ファイルへの意図的な新規追加の `fail`）は変更しない。
- 誤って `deferred` を選んだ pytest VC は `repair_issue_contract.py` が限定条件下で注釈行のみ `pr_review_only` へ修復する。不正値の `baseline-expect` は別 Issue（#2760）の対象である。

### lint VC の対象 coverage（列挙と実検査の区別）

Markdown / prose の lint VC は、exit 0 であることと、要求した変更対象が実際に検査されたことを区別して設計する。lint の exit 0 は対象ファイルの coverage を保証しない。config の ignore / glob / gitignore により、対象が 1 件も検査されないまま exit 0 になる false-green を避けるための、新規 gate を伴わない authoring 上の指針である。

#### 集合の定義（E ⊆ P）

- E: VC が検査すると主張する期待対象集合（Issue の Allowed Paths 内で変更する Markdown など）
- P: lint が実際に処理した集合
- PASS に必要な条件は `E ⊆ P` である。ゼロ件、一部欠落、処理確認不能は PASS にしない。対象を限定した検証例では、可能な限り P = E にする。
- E と P のパスは、同じ cwd / checkout を基準にした絶対パス、または repo-relative のどちらかに正規化してから比較する。
- exit code は lint の verdict であり、E ⊆ P の証拠とは別の項目として評価する。exit 0 でも P が空なら未検査であり、exit 1 でも別ファイルの診断だけが原因なら対象の検査を意味しない。

#### 列挙は実検査の証拠ではない

`showFound` の `Found:` 一覧や `Linting: N file(s)` は列挙の証拠にすぎず、実検査の証拠ではない。次は、以下に記す installed version で隔離環境（repo 外の `mktemp` ディレクトリ）に再現した例である。実測できた範囲のみを記載し、未確認の挙動は断定しない。

- markdownlint-cli2 0.17.2（markdownlint 0.37.4）の root `ignores` に含まれるファイルを `:path` 形式のリテラルで指定すると、`Found:` にも `Linting: N file(s)` にも数えられる。しかし MD040 の違反を含んでいても `Summary: 0 error(s)` かつ exit 0 になる。これが negative control（違反を含む ignore 対象が報告されない例）である。同じ内容を ignore 外へ置くと違反が報告されて exit 1 になる。
- サブディレクトリに置いた `.markdownlint-cli2.yaml` の `ignores` は列挙の後に適用される。そのため、`Found:` に載り `Linting:` に数えられても、そのサブディレクトリのファイルは 0 error・exit 0 になる。
- `gitignore: true` では、git-ignored のファイルを通常のパス / glob 形式で指定すると、`Finding:` 行に除外の表示が出ないまま `Found:` からも黙って外れる（同じファイルを `:path` 形式で指定したときは検査され、違反が報告された）。
- CLI で指定した対象に config の `globs` が合成されるため、別ファイルの違反で exit 1 になることがある。このとき診断は指定した対象とは無関係であり、対象が検査された証拠にならない（P ≠ E）。
- `--config <file>` は base として読まれ、cwd の `.markdownlint-cli2.*` が後から merge される。`globs`・`ignores`・`gitignore` のような同名の最上位プロパティは cwd 側の値で置き換えられ、base 側の値は残らない。`config` 内の規則は規則ごとに merge される。そのため、cwd の設定が `ignores` を持つ場合は、`--config` で `ignores` を渡すだけでは除外を外せない。
- `--config <file>` のファイル名は、末尾の suffix によって読み込み方が決まる（根拠は markdownlint-cli2 0.17.2 の `readOptionsOrConfig()`）。末尾が `.markdownlint-cli2.jsonc` / `.yaml` / `.cjs` / `.mjs` なら CLI2 options として、`.markdownlint.jsonc` / `.json` / `.yaml` / `.yml` / `.cjs` / `.mjs` なら markdownlint の規則設定として読まれる（後者は内部で `{ config }` に包まれる）。末尾が対応する suffix であれば prefix 付きの名前も受理される（例: `rules.markdownlint.json`、`base.markdownlint-cli2.yaml`）。両形式は中身の形式が異なるため、ファイル名だけを変えて交換してはならない。対応する suffix を持たない名前は設定ファイルの読込エラーになる。これは、対応する suffix の設定ファイルを読み込めたうえで lint が返す結果（違反があれば exit 1）とは別の事象であり、混同しない。設定ファイルを読み込めたことは、lint 全体が exit 0 になることを意味しない。以下は実測した範囲のみで、ほかの exit code へは一般化しない（隔離ディレクトリ、markdownlint-cli2 0.17.2 / markdownlint 0.37.4、cwd は一時ディレクトリ）。
  - `rules.markdownlint.json`（`{"default": true}`）と違反のない Markdown では exit 0（規則設定として受理）。`opts.markdownlint-cli2.jsonc`（`{"config": {"default": true}}`）でも exit 0（options として受理）。
  - 対応する suffix を持たない `bad.config.json` では、実測では exit 2 だった。
  - MD040 違反を含む Markdown に `{"MD040": false}` を `off.markdownlint.json` で渡すと MD040 が無効化されて exit 0 だった。同じ内容を `off.markdownlint-cli2.jsonc` に改名すると options として読まれ、`MD040` キーは規則設定にならないため MD040 が報告されて exit 1 だった。
- textlint 14.8.4 では、`--ignore-path` を指定すると既定の `.textlintignore` は適用されず置き換えられる（併用にならない）。リテラルの明示パスは `.textlintignore` で除外されず処理されるが、glob 経由の指定は除外され、全件が除外されると出力は `[]`・exit 0 になる。存在しないパスも `[]`・exit 0 になりうる。
- textlint の `--output-file` を指定すると、違反があっても exit 0 になる。exit code だけで判定しない。

#### P の根拠

- textlint: `--format json` の出力にある `filePath` の集合を P とする。`jq -r '.[].filePath'` などで抽出し、E ⊆ P と exit code を別々に評価する。
- markdownlint-cli2: 処理した集合を直接出力する機能に頼らず、canary を主たる根拠にする。canary は、意図的な違反（例: 言語指定のないコードフェンス MD040）を含む一時ファイルを、検証対象と同じディレクトリ・同じ設定スコープ・同じ指定経路（glob か literal path か）に置き、その違反が報告されることで、その指定経路が実検査に到達したことを示す補助証拠である。実行後に canary は削除する。
- 別の ignore 範囲や別ファイルに置いた canary の検出は、実対象が lint された証拠にならない。ファイル単位の除外まで排除したい場合は、対象ファイルそのものに一時的な意図的違反を入れる。その前に作業前の内容を保存して `sha256sum` を記録し、検証後に元の内容へ byte-identical に復元して `sha256sum` と `diff` で確認する。違反を commit に残さない。そして、その対象のパスに対する診断が出力されることを確認する。E に複数の対象があれば、対象ごとに行う。
- canary は、その対象が検査に到達したことを示すが、E 内のほかの clean なファイルが検査されたことまでは示さない。
- `showFound` の一覧から per-directory の `ignores` を差し引く導出は補助の推論にすぎない。root の `ignores`・`gitignore`・`--config` の merge 結果の取りこぼしを検出できないため、単独で P の証拠にしない。

#### cwd と revision の合わせ方

- cwd は、検証したい探索範囲と対象 revision に合わせる。通常の変更ファイル検査は、レビュー対象の変更内容が存在する worktree / checkout で実行する。
- #217 / PR #215 は、main checkout 配下の worktree traversal と ESLint ignore を検証した個別の限定事例であり、すべての lint VC を main checkout で実行する規則ではない。#217 の実装や merge を前提にしない。

#### baseline の切り分けと境界

- 既存違反と変更由来の違反を区別する。例えば textlint を `.claude/` 配下の Markdown へリテラル指定すると、変更前から存在する `prh` 違反で exit 1 になることがある。同様に、`.claude/**` の ignore を外した隔離環境で markdownlint-cli2 を実際に走らせると、変更前から存在する MD038 / MD040 の違反が報告されうる。これらの非ゼロ終了を「PASS」と書き換えない。変更前の revision と変更後の revision で診断を比較し、変更由来の違反が増えていないことだけを示す。
- 差分外ファイルの大規模な改変や、全体の ignore 設定の廃止は要求しない。
- この指針を守るために、新規の gate・hook・wrapper・schema・CI job は不要である。VC の作者が証拠を設計し、reviewer が照合する。

#### 掲載する限定 lint 例の要件

この節または Issue の VC に限定 lint 例を載せる場合は、実行確認済みのものに限る。未確認の例は載せない。実行確認の証拠として、次の項目を記録する。

- installed version（例: `pnpm exec markdownlint-cli2 --help` の先頭行、`pnpm exec textlint --version`）
- 対象 revision（Git commit SHA / PR HEAD）
- 実行した cwd と、検査した対象の実内容
- 期待対象集合 E
- 実検査集合 P の根拠（textlint の `filePath` 集合、markdownlint-cli2 の canary 結果）
- 実際の exit code
- diagnostics（negative control / canary の結果を含む）
- 既存違反と変更由来の違反の区別

`pnpm run lint:docs` の一般結果は、`.claude/**` が ignore されているため、本ファイルのような対象の検査証拠として扱わない。限定 lint の実測証拠は、一般結果とは別の欄に記載する。

## VC_SINGLE_COMMAND_GUARDRAIL

Issue body の VC（Verification Commands）は **shell control operator に依存しない単一コマンド** で記述する。

### 禁止 operators

以下の shell control operators を VC コマンド行で使用してはならない:

| Operator | 禁止理由 |
|---|---|
| `&&` | 前段の exit code に依存した条件実行。失敗判定が不完全になる |
| `||` | `A && B || C` は if-then-else と等価ではない（SC2015）。A が成功して B が失敗した場合に C が実行される |
| `|` | パイプの exit code は最終コマンドに依存し、前段の失敗を隠蔽する |
| `;` | 逐次実行。前段の失敗を無視する |
| `&` | バックグラウンド実行。exit code が非同期になる |

> **リダイレクト演算子（`<<`, `<`, `>`, `>>`, `<<<`）について**: VC 例示での使用は避けることを推奨するが、`<file>` や `<pattern>` 等の placeholder との誤判定リスクがあるため、`verify_vc_single_command_guardrail_docs.py` は機械的な enforce を行わない。

### SC2015 問題（A && B || C パターン）

`A && B || C` は "if A then B else C" と等価ではない:

- A が成功し B が失敗した場合: C が実行される（意図しない PASS）
- 例: `grep -q pattern file && echo PASS || echo FAIL`
  - `grep` が成功（pattern 存在）しても `echo PASS` が非 0 を返すと `echo FAIL` が実行される

### compound shell が避けられない場合の AC 分割方針

1 つの VC コマンドが複数の条件チェックを行う必要がある場合は、AC を複数エントリに分割する。

### checker script entrypoint（チェッカースクリプトの入口）

compound shell 違反の自動検出は以下のスクリプトで行う:

```bash
uv run python3 .claude/skills/create-issue/scripts/verify_vc_single_command_guardrail_docs.py --strict
```

違反があれば file:line で報告して exit 1。成功時は exit 0。

### 決定論的 VC と意味的評価 AC の分離

VC はすべて **決定論的（deterministic）** な形式で作成する。意味的評価は PR レビュアーの責務であり、test-runner が実行可能な VC 内で行わせない。

**決定論的判定（test-runner が実行可能）**:
- `grep` / `rg` の exit code（パターンの存在）
- `diff` の exit code（ファイル一致）
- `pnpm typecheck` / `pnpm lint` / `pnpm test` / `pnpm build` の exit code（各 AC ごとに 1 コマンド）
- ファイル存在確認（`test -f` / `test -d`）
- ファイルサイズ・行数の数値比較

**意味的評価（PR レビュアー判定）**:
- コード品質の正当性
- 算出値の妥当性
- ドメイン固有の正当性

### TypeScript 関数スコープ限定 VC

AC に「特定の関数内」で何かを確認する VC を書く場合、`grep`（ファイル全体）ではなく以下のいずれかを使う:

1. **`pnpm test path/to/specific-test`**: Vitest など実行系で確かめられる場合、最も決定論的
2. **TypeScript Compiler API（AST ベース）**: 関数スコープを厳密に取りたい場合は `typescript` パッケージを使うスクリプトを書く
3. **関数境界の正規表現**: 軽量だが偽陽性に注意

### 削除確認パターン

削除されたことを確認するには、literal が file に存在しないことを `--files-without-match` で単一コマンドとして確認する。成功は exit 0 とする。

```bash
rg --files-without-match --fixed-strings -- "<literal>" <file>
```

- `--` は option parsing の終端であり、`--settings` / `--foo` のように `-` で始まる literal を option ではなく pattern として扱うために必須とする。`--fixed-strings` を付けても `--` がなければ dash-leading literal は option と解釈されて rg が exit 2（option parse error）になる。
- 成功は exit 0（literal が file に存在しない）。literal が存在する間は exit 1 になるため、削除前の baseline では fail、削除後は pass という遷移を VC で実証できる。
- 1 command = 1 concrete file とする。複数 file を一度に渡すと、1 file でも literal を含まなければ exit 0 になり「全 file から消えた」と誤読できるため、file ごとに VC を分ける。
- `-q` / `--quiet` を併用しない。ripgrep 14.1.0 には `--files-without-match` との併用で exit status が反転する upstream bug（#3108）があり、15.0.0 で修正された。バージョン依存を避けるため plain `rg` を使う。
- rg の `-c` / `--count` は no-match で exit 1 を返す（`--include-zero` 付きでも同じ）ため、「0 件を成功」とする削除確認には使えない。存在確認や正の件数確認（count が 1 以上であることを成功とする用途）とは区別する。
- 否定演算子 `!` を前置した rg / grep 検索は、repo の VC verifier が静的に分類して実行しないため、削除確認の canonical な例として使わない。

> `A && echo PASS || echo FAIL` 形式の compound shell は使用しない。`VC_SINGLE_COMMAND_GUARDRAIL` セクションを参照。

### GitHub milestone metadata の readback assertion パターン

GitHub milestone metadata（`description` 等）の forbidden phrase の有無を VC で検証する場合は、raw `gh api` を VC に書かず、first-class な `github_metadata_assert` を使う。raw `gh api` は preflight の allowlist で block され、`gh api ... --jq` は値を出力するだけで exit code による assertion にならない。

許可される形（method GET 固定・endpoint は milestone のみ）:

```bash
# AC1: milestone description に forbidden phrase が含まれないことを exit code で確認する
github_metadata_assert not_contains description "<literal>" repos/<owner>/<repo>/milestones/<number>
```

- assertion は `contains` / `not_contains` のみ。`contains` は present→exit 0、`not_contains` は absent→exit 0
- field は `description` のみ（typo・未知 field は reject）。コマンドは4引数ちょうどで flags / 余分な positional は受け付けない
- endpoint は `repos/<owner>/<repo>/milestones/<number>` のみ（絶対 URL・query string・path traversal・placeholder は reject）
- gh 不在 / auth 失敗 / 404 / rate limit / timeout / invalid JSON は environment error として `human_judgment` 分類になり、false pass にならない

禁止例:

```bash
# 不可: raw gh api は block される。jq は出力するだけで assertion にならない
gh api repos/owner/repo/milestones/1 --jq '.description'
```

### AC/VC 番号一致制約

Verification Commands 内の `# AC<N>` コメント番号は、Acceptance Criteria の AC 番号と必ず一致させる。

```
## Acceptance Criteria
- [ ] AC1: ファイルが存在する
- [ ] AC2: 特定のフィールドが含まれている

## Verification Commands
# AC1: ファイル存在確認
test -f <file>

# AC2: フィールド確認
rg -q "<field>" <file>
```

### rg 構文チェック

VC に `rg` が含まれる場合、`rg "foo\|bar"` の `\|` は `|` に修正する（rg の OR 演算子）。

**Issue #589 追記**: `baseline_vc_preflight.py` は `\|` を含む rg / egrep / grep -E コマンドを
`regex_literal_pipe_suspected` として `blocked` に分類する。`\|` は ripgrep / ERE では alternation を
意図するなら `|` のみで十分であり、`\|` は literal pipe 文字のため。

VC を修正できない正当な理由（例: BRE モードで literal pipe が必要）がある場合は、
コマンド行の直前行に以下の annotation を付与することで exempt できる:

```bash
# vc-regex-intent: literal-pipe-ok reason="BRE mode: \| is literal pipe (intentional)"
$ grep "foo\|bar" file.txt
```

annotation 構文:
- 形式: `# vc-regex-intent: literal-pipe-ok reason="<理由>"`
- 位置: VC コマンド行の直前行（bash ブロック内）
- `reason` フィールドは任意だが強く推奨（レビュー時の根拠として使用）
- annotation がない場合: `baseline_vc_preflight` は `decision: blocked` を返す

### rg を用いた VC 作成コマンド構築

VC 内でファイルのパターン存在確認を行う際は `grep` より `rg` を優先する。`grep` は GNU 拡張差や Perl 互換構文の扱いが環境によって分かれるため、決定論性が低い。

基本形式（行番号付きで 1 行以上マッチすれば exit 0）:

```bash
# 特定の見出しが存在することを確認する例
rg -n "^## VC 作成ガイダンス" .claude/skills/create-issue/references/body-authoring.md

# 特定のコマンド例が記述されていることを確認する例
rg -n "uv run --with" .claude/skills/create-issue/references/body-authoring.md
```

見出し配下のコンテキストを含めて確認する例（見出し + 20 行以内に内容が存在）は 2 段階の確認が必要なため、2 つの独立した VC に分割する:

```bash
# VC1: 見出しが存在することを確認
rg -nA 20 "^## VC 作成ガイダンス" .claude/skills/create-issue/references/body-authoring.md
```

```bash
# VC2: 見出し配下に rg -n が存在することを確認（VC1 通過後に実行）
rg -n "rg -n" .claude/skills/create-issue/references/body-authoring.md
```

Perl 互換正規表現（後読み / 先読み / Unicode property 等）が必要な場合は `-P` フラグを優先する。
`grep -P` は GNU grep 限定だが `rg -P` は ripgrep 組み込みのため移植性が高い:

```bash
# アンチパターンを含む行を検出（Perl 互換後読み）
rg -Pn "アンチパターン|anti.pattern" .claude/skills/create-issue/references/body-authoring.md

# cd + python3 パターンを Perl 互換で検出
rg -Pn "cd .+python3 -m pytest" .claude/skills/create-issue/references/body-authoring.md
```

**VC 作成時の rg チェックリスト**:
1. パターンが Perl 互換構文（`\K` / `(?<=...)` / `\p{...}` 等）を含む → `-P` を付ける
2. OR 演算子は `|`（`\|` ではない）
3. 見出し配下の存在確認は `rg -nA <N> "^## <見出し>" <file> | rg "<内容>"` の 2 段パイプを使う

### Python テスト系 VC の依存明示

Python テストを VC に含める場合、実行時の依存パッケージが不在で fail しないよう `uv run --with` で依存を明示する。

**推奨パターン**（依存を明示して実行）:

```bash
uv run --with pytest --with pyyaml python -m pytest tests/
```

**アンチパターン 1**: 依存未明示（パッケージ不在時に ImportError で fail）

```bash
# BAD: pytest / pyyaml がプロジェクト依存に含まれていない環境で fail する
uv run python -m pytest tests/
```

**アンチパターン 2**: `cd` + `python3` 直接実行

```
# BAD: PATH 依存・venv 未整備・cwd 前提が絡み合い環境依存が高い
cd .claude/skills/some-skill && python3 -m pytest tests/
```

`uv run` を使う理由:
- プロジェクトの `.python-version` / `pyproject.toml` を自動参照する
- 仮想環境を自動作成・再利用する
- `--with` で追加依存をインライン指定できる（グローバル汚染なし）

### pytest 実行 VC の推奨パターン

pytest を VC に含める場合は以下の順で選択する。

**第一推奨**: `uv run --with pytest` で依存明示

```bash
uv run --with pytest python -m pytest .claude/skills/some-skill/tests/
```

**第二推奨**: `uv run python -m pytest`（プロジェクト依存に pytest が含まれる場合）

```bash
uv run python -m pytest .claude/skills/some-skill/tests/
```

**アンチパターン**: `cd <dir> && python3 -m pytest`

```
# BAD: 以下の理由で決定論性が低い
# - cwd 変更が後続コマンドに副作用を与える
# - python3 コマンドの PATH は環境依存
# - venv が有効化されていない場合、依存パッケージが見つからず fail
cd .claude/skills/some-skill && python3 -m pytest tests/
```

### SubAgent frontmatter YAML 検証（構文検証の手順）

SubAgent / Skill ファイルの frontmatter（YAML ブロック）が正しいフィールドを持つかを VC で確認する場合、`yaml.safe_load` を使うスクリプトを推奨する。

**推奨パターン**: `yaml.safe_load` で parse して値を検証

```bash
# name フィールドが存在し空でないことを確認
python3 -c "
import yaml, sys
with open('.claude/agents/some-agent.md') as f:
    content = f.read()
# frontmatter は --- で囲まれたブロック
fm_text = content.split('---')[1]
fm = yaml.safe_load(fm_text)
assert fm.get('name'), 'name field missing or empty'
print('PASS: name field exists')
"

# uv run --with pyyaml で依存を明示する場合
uv run --with pyyaml python3 -c "
import yaml
with open('.claude/agents/some-agent.md') as f:
    fm = yaml.safe_load(f.read().split('---')[1])
assert fm.get('description'), 'description field missing'
print('PASS')
"
```

**アンチパターン**: `grep "^<field>:"` による擬似マッチ

```bash
# BAD: 以下の問題がある
# 1. コメントアウトされた行 (# name: foo) にも誤マッチする
# 2. YAML の multiline value や anchor を正しく扱えない
# 3. フィールド値の内容（空文字 / null）を検証できない
grep "^name:" .claude/agents/some-agent.md
```

## 必須セクション列挙手順

Issue 本文を起票・更新する前に、対象テンプレートの `required: true` ラベルを動的に列挙する。ハードコードした列挙は ISSUE_TEMPLATE の変更追従が遅れるため、以下の動的取得手順を使う。

### yq を用いた列挙

```bash
# 例: improvement テンプレートの required ラベルを列挙
yq '.body[] | select(.validations.required == true) | .attributes.label' \
  .github/ISSUE_TEMPLATE/improvement.yml
```

### Python を用いた列挙

`yq` が利用できない環境では `python3` で代替する:

```python
import yaml

with open(".github/ISSUE_TEMPLATE/improvement.yml") as f:
    template = yaml.safe_load(f)

required_labels = [
    item["attributes"]["label"]
    for item in template.get("body", [])
    if item.get("validations", {}).get("required") is True
]
print("\n".join(required_labels))
```

`uv run` で実行する場合:

```bash
uv run --with pyyaml python3 -c "
import yaml
with open('.github/ISSUE_TEMPLATE/improvement.yml') as f:
    t = yaml.safe_load(f)
required = [i['attributes']['label'] for i in t.get('body',[]) if i.get('validations',{}).get('required')]
print('\n'.join(required))
"
```

### 利用タイミング

- 新規 Issue 起票前（`create-issue`）: 必須セクションのもれを防ぐ
- 既存 Issue 更新前（`edit-issue`）: Template Guard 通過を事前確認する
- template を参照せずにハードコードした列挙は、ISSUE_TEMPLATE 変更追従が遅れるため使わない

## AC ⇔ VC 番号整合手順

AC の番号（`- [ ] AC<n>`）と VC の行末コメント（`# AC<n>`）が一致しているかを起票・更新前に照合する。

### awk による AC 件数カウント

```
# Acceptance Criteria セクションの AC 件数を数える（awk + wc -l の参照例）
awk '/^## Acceptance Criteria/{flag=1; next} /^## /{flag=0} flag && /- \[ \] AC[0-9]/' issue_body.md | wc -l
```

### rg による VC の # AC<n> コメント件数カウント

```bash
# VC セクション内の # AC<n> コメント件数を数える
rg -c "# AC[0-9]" issue_body.md
```

### 照合の判定ルール

AC 件数と VC の `# AC<n>` 件数が一致しなければ起票・更新しない。
`edit-issue` の guard-issue-body.py も同様の整合チェックを実施するが、起票前に手動照合しておくと手戻りを減らせる。

```
# 照合の参照例（各コマンドを個別に実行する）
# AC 件数カウント
AC_COUNT=$(awk '/^## Acceptance Criteria/{flag=1; next} /^## /{flag=0} flag && /- \[ \] AC[0-9]/' issue_body.md | wc -l)
# VC の # AC<n> コメント件数カウント
VC_AC_COUNT=$(rg -c "# AC[0-9]" issue_body.md)
# 件数比較（個別に実行）
[ "$AC_COUNT" -eq "$VC_AC_COUNT" ]
```

> 上記を連結した `[ ... ] && echo PASS || echo FAIL` 形式は使用しない。`VC_SINGLE_COMMAND_GUARDRAIL` セクションを参照。

## 単独 implementation Issue 向け Parent 系セクション scaffold

単独改善（parent Issue を持たない）の implementation Issue を起票する場合、review-issue C1（必須セクション存在チェック）と整合させるために ISSUE_TEMPLATE の `validations.required: true` 全セクションを埋める必要がある。Parent Issue / Parent Goal Ref / Current Validated Scope / Remaining Parent Gaps / Scope Delta の各セクションも required に含まれる場合は以下の placeholder で埋める。

> review-issue C1 は「必須セクションが存在すること」を判定する。Parent 系セクションを省略すると C1 fail となるため、単独改善でも必ず scaffold を記述する。

### 推奨 scaffold

```markdown
## Parent Issue

なし（単独改善）

## Parent Goal Ref

- Goal: <この Issue が解決しようとしている目標を 1 文で>
- Desired Destination: N/A（単独改善 Issue のため）

## Current Validated Scope

- <実装・変更の対象ファイル/機能を箇条書きで列挙>

## Remaining Parent Gaps

なし（単独改善 Issue のため）

## Scope Delta

- 追加: <今回追加する内容>
- 削除: なし
- 変更: <今回変更する内容>
```

### Machine-Readable Contract の parent_issue フィールド

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: "<この Issue の goal を 1 文で>"
change_kind: workflow
```

`parent_issue: none` は「parent Issue が存在しない単独改善」を明示する値。空文字や省略は invalid。

### Scope Delta セクションとの関係

`## Scope Delta` セクションは `pr-review-judge` が「この PR が当初スコープからどう変化したか」を判定するために使う。単独改善でも省略しないこと。

### 関連 Issue を複数 close する場合

`Closes #25 Closes #42 ...` のように複数 linked Issue がある PR では、PR 本文に各 Issue の AC を列挙し、全件を検証したことを `pr-review-judge` が確認できるようにする。

## Anchor Verification Preflight（アンカー検証プリフライト）

Issue 本文で「既存ファイルの行番号・セクション見出し・関数名」を anchor として主張する場合、起票前にプリフライトを実施する。

```bash
git grep -n "<anchor文字列>" <対象ファイルパス>
# または
rg -n "<anchor文字列>" <対象ファイルパス>
```

0 hit の場合は「該当箇所は存在しないため、更新ではなく新規追加」と本文に明記するか、anchor 主張を修正する。

## Blocker 検出フロー（Issue 起票前）

Issue を起票する前に、関連 OPEN Issue を列挙し、依存関係を判定する。

### ステップ 1: 関連 OPEN Issue の列挙

```bash
gh issue list --search "<関連語>" --state open --json number,title,url
```

### ステップ 2: 前提条件かどうかを判定する

以下のいずれかに該当すれば blocker:

1. 対象機能の実装 PR が未マージ（依存 API / ライブラリの提供元が未完了）
2. 本 Issue の実装に必要な spec / 設計書が未確定
3. 同一ファイルへの変更が競合する可能性がある open PR が存在する
4. 本 Issue の AC が別 Issue の完了を明示的に条件としている

### ステップ 3: blocked-by として登録する場合

`create_issue_txn.py --blocked-by` で blocker を指定して起票する（コマンド詳細は `create-issue/SKILL.md` の Blocker / Blocked-by 設定手順を参照）。

## visual_impact フィールド（canonical schema owner, Issue #2019）

`create-issue` / `edit-issue` / `issue-contract-review` / `review-issue` の4スキルが Issue 本文で扱いうる
`visual_impact` 関連フィールド（`VISUAL_IMPACT_DECLARATION_V1` 等）は、単一の canonical schema ファイル
[`docs/dev/visual-impact.schema.json`](../../../../docs/dev/visual-impact.schema.json) への参照として共有する。
4 スキルのいずれも、このフィールドのキー集合・enum を独自に再定義しない（重複定義禁止）。

- `docs/dev/visual-impact.schema.json` の `$defs.VISUAL_IMPACT_DECLARATION_V1` が Issue/PR body 上で人間または
  PR author が記載する untrusted declaration の正本スキーマ
- `docs/dev/visual-impact.schema.json` の `$defs.VISUAL_IMPACT_DECISION_V1` が CI が独立生成する trusted decision
  の正本スキーマ（Issue/PR 本文執筆時には直接扱わない）
- registry 側の正本は `docs/dev/visual-surfaces.yml`（`docs/dev/visual-surfaces.schema.json` で検証）

## Required Design References Authoring Guidance（Required Design References セクションの執筆ガイダンス・設計文書参照ガイド）

Issue 本文の `## Required Design References` は、本 Issue の実装が前提とする **設計ドキュメント（SSOT）への repo 相対パス参照** を列挙するセクションである。

- 空・`N/A` のみ・`none` のみで済ませない。少なくとも 1 件の repo 相対パス（例: `docs/dev/agent-skill-boundaries.md`）を含める
- 参照は `docs/`, `.claude/`, `src/`, `scripts/` 配下の実在ファイルへの相対パスで書く。抽象的な説明文だけでは `contract_readiness_check.py` の静的検査（`RDR001`）で `needs_fix` になる
- 見出しアンカー（例: `docs/dev/agent-skill-boundaries.md#Parallel Agent Runtime Safety`）を付けてもよい
- `## Required Skills`（ドメイン知識スキル）とは別概念。`## Required Design References` は SSOT ドキュメントへの参照専用

## 関連

- `.claude/skills/create-issue/SKILL.md` — 新規起票でこの参照を使う
- `.claude/agents/issue-editor.md` — 既存 Issue 更新でこの参照を使う
