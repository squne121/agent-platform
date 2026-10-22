# VC Preflight（baseline_vc_preflight.py の説明）

## 前提

`## Verification Commands` は fenced bash block 形式で記述する規約とする。

- コマンド行: `AC` マーカー直前に `# ACn`
- 1 行あたり 1 コマンド
- VC でない書式（インラインなど）は無視

## 実行

実行コマンド例（Issue 番号と repo を指定して実行する）:

```bash
# 実行例
uv run python3 .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py \
  --issue <番号> --repo <owner>/<repo>
```

## 判定

判定結果の対応関係は次の通りとする。

- `status: pass` → OK（合格）
- `status: blocked` → BLOCKED（阻止）
- `status: human_judgment` → `human_escalation`（人間判断への委譲）

## Scope Classes（分類の種類）

- `baseline_fail_expected`: 基本想定。`expected_fail` を go、想定外 pass は `blocked`
- `regression_gate`: `pnpm ...` / `uv run pytest` 等。pass は go、fail は blocked
- `pr_review_only`: skipped/go（`verification_owner` + `deferred_reason`）
- `runtime_only`: skipped/go（`verification_owner` + `deferred_reason`）

`pnpm build` は regression gate のまま扱うが、runner 側で fixed env delta `{CI:"true"}` を付けて `shell=False` 実行する。Issue body 側で `CI=true pnpm build` や `env CI=true pnpm build` を書いて回避しない。

## 主要カテゴリ

`expected_baseline_fail`, `unexpected_pass`, `env_missing_dep`, `command_not_allowed`, `unsupported_syntax`, `compound_command_disallowed`, `file_not_found_*`, `trivially_pass`, `regression_gate`, `package_manager_no_tty_prompt` など。

`package_manager_no_tty_prompt` は `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` / `Aborted removal of modules directory due to no TTY` を検出したときの dedicated category。body-author-fixable ではなく tooling/env blocker として扱う。

| category | body_author_fixable | downstream_bucket | expected route |
|---|---:|---|---|
| package_manager_no_tty_prompt | false | env_or_runtime | stop rewrite; tooling/human triage |

`scope_class` / `classification` / `decision` / `category` を別々に解釈。

## UV allowlist（実運用固定形）

`uv` の allowlist は次の形のみ許可する。

- `uv lock --check`
- `uv run --isolated --locked --no-default-groups python scripts/ci/runtime_dependency_smoke.py`
- `uv run --isolated --locked --no-default-groups python3 scripts/ci/runtime_dependency_smoke.py`

拒否対象:

- `uv lock` / `uv lock --upgrade` / `uv sync` / `uv run uv lock --check`
- `uv run --isolated --locked python ...` など option が不足する runtime smoke
- `uv run --isolated --locked --no-default-groups --with ... python scripts/ci/runtime_dependency_smoke.py`
- `uv run ... --group ...`, `--with`, `--all-groups`, `--extra`, `--python`, `--project`, `--directory`, `--env-file`, `--upgrade`, `--env-file`
- `uv run --isolated --locked --no-default-groups python -c ...`
- `uv run --isolated --locked --no-default-groups python -m ...`
- `uv run --isolated --locked --no-default-groups python ../runtime_dependency_smoke.py`
- `uv run --isolated --locked --no-default-groups python /tmp/runtime_dependency_smoke.py`
- `uv run --isolated --locked --no-default-groups python scripts/ci/other.py`

## preflight-scope marker（プリフライト範囲マーカー）

マーカーの記述例:

```bash
# AC1
# preflight-scope: pr_review_only
$ <command>
```

- `pr_review_only` / `runtime_only` のみ有効
- 不正値は `classification: human_judgment`。

## github_metadata_assert（GitHub milestone metadata の exit code による検証機能）

GitHub milestone metadata（特に `description`）の forbidden phrase の有無を **exit code** で検証したい場合は、raw `gh api` を VC に書かず、first-class な `github_metadata_assert` を使う。

許可される形:

許可される記述例:

```bash
# AC1
$ github_metadata_assert not_contains description <literal> repos/<owner>/<repo>/milestones/<number>
```

- assertion: `contains` / `not_contains` のみ
- field は `description` のみ（allowlist 外・typo は reject）。コマンドは4引数ちょうどで flags / 余分な positional は reject
- 内部実行は固定 argv `gh api --method GET repos/<owner>/<repo>/milestones/<number>`（method GET 固定・非 mutating）
- endpoint は `repos/<owner>/<repo>/milestones/<number>` のみ（絶対 URL・query string `?`・path traversal `../`・placeholder `<...>` は reject）
- `contains` は present→exit 0 / absent→non-zero、`not_contains` は absent→exit 0 / present→non-zero
- gh 不在 / auth 失敗 / 404 / rate limit / timeout / invalid JSON / 未知の gh 失敗 / response に field 不在（schema drift）は `github_metadata_assert_environment_error` として `human_judgment` に分類され、assertion の pass/fail（false pass）と区別される

禁止例（VC に raw `gh api` を書かない）:

```bash
# 不可: raw gh api は allowlist で block される。jq は出力するだけで assertion にならない
$ gh api repos/owner/repo/milestones/1 --jq '.description'
```

## Command-level timeout budget（Issue #2233、コマンド単位の実行時間上限、fix_delta 反映済み）

canonical VC plan（`compute_canonical_vc_plan()`、schema `canonical_vc_plan/v2`）は、`body_sha256` / `parser_contract_version` / `command_occurrence_count` / `command_occurrences`（occurrence 順の command_hash/is_pure 一覧）などと並ぶ一級のデータとして `command_budgets[]`（`command_timeout_budget/v1` スキーマ）・`aggregate_timeout_seconds`・`plan_digest`（`estimator_evidence_digest` はその alias）を持つ。

この plan は単一の root-owned producer artifact であり、`baseline_vc_preflight.py` 自身の executor（`_main_impl()`）・`contract_readiness_check.py`・`run_contract_review_once.py`・`run_root_review_pipeline.py` の 4 箇所すべてが、同じ pinned Issue body から同一関数 `compute_canonical_vc_plan()` を呼び出す（または、subprocess 境界を跨ぐ場合は同じ body から再計算し `plan_digest` を照合する）ことで、独立に per-command scalar を再導出しない契約になっている（AC2）。

`command_timeout_budget/v1` の各フィールド:

- `command_hash` / `command_identity_hash`: 対象コマンド**テキスト**の識別子（`compute_command_hash()` と同一値）。fix_delta で `execution_key_hash` から改名した。既存の `compute_execution_key_hash()`（argv/cwd/env/timeout/state epoch を含む、実行単位の識別子）とは意図的に別物であり、混同しない。
- `timeout_seconds` / `cleanup_tail_seconds`: 実効 timeout と post-SIGTERM cleanup tail（`CLEANUP_TAIL_SECONDS`、既定 15 秒）
- `source`: `explicit_override`（`--timeout-seconds` の hard global override）/ `static_policy`（`STATIC_PER_COMMAND_TIMEOUT_POLICY` に一致する trusted かつ hand-curated な既知の低速コマンド）/ `static_fallback`（既定値、`DEFAULT_PER_COMMAND_TIMEOUT_SECONDS`）/ `history_estimate`（Issue #2254 で再導入。ローカル per-repository SQLite に永続化された genuine な実測 evidence に基づき、`static_base` を **strictly** 上回る場合のみ名乗る raise-only source。詳細は下記「History-based estimator（Issue #2254）」節）。
- `estimator_version` / `estimator_input_digest`: provenance の再現性を保証する識別子
- `sample_count` / `observed_p95_ms`: `source: history_estimate` のときのみ非ゼロ/非 null（history snapshot の `eligible_sample_count` / `observed_p95_ms` をそのまま反映）。それ以外の source では常に `0` / `null`。
- `policy_clamped`: `static_fallback` / `static_policy` / `history_estimate` の値が `MIN_PER_COMMAND_TIMEOUT_SECONDS` の floor まで引き上げられた場合に `true`（fix_delta で実装。以前は常に `false` だった）
- （Issue #2254 additive）`command_group_key` / `history_store_status` / `history_backoff_applied` / `timeout_backoff_floor_seconds`: history-based estimator の identity・診断用フィールド。`history_snapshot` 未指定時は順に `null` / `"snapshot_absent"` / `false` / `null`。

### Override precedence（AC4、優先順位）

`--timeout-seconds` を明示的に指定した場合、それは **hard global override** としてすべてのコマンドの budget に優先適用される（`source: explicit_override`）。明示指定がない場合、`STATIC_PER_COMMAND_TIMEOUT_POLICY` に一致するコマンドは `static_policy`、それ以外は `DEFAULT_PER_COMMAND_TIMEOUT_SECONDS`（`static_fallback`）が使われる。

### 271.31 秒問題の解消（fix_delta P0-2）と Policy ceiling（AC5）

Issue #2233 の元々の障害（`uv run --locked pytest .claude/skills/issue-refinement-loop/tests -v` が実測 271.31 秒であるにもかかわらず固定 150 秒キャップで誤って kill される）は、`STATIC_PER_COMMAND_TIMEOUT_POLICY` に当該コマンドの exact command text を trusted static policy entry として 420 秒（実測値の 1.5 倍以上のマージン）で登録することで解消した。history-based estimator（本 Issue の Out of Scope）ではなく、人間がレビューし version control された固定表であり、実測値の根拠をコメントに記載する。

`MAX_PER_COMMAND_TIMEOUT_SECONDS` は fix_delta で 150 秒から 600 秒に引き上げた（`STATIC_PER_COMMAND_TIMEOUT_POLICY` の全エントリを常に上回るよう import 時に assert される）。この値を超える解決値は、`source`（override / static_policy / fallback のいずれか）を問わず `CommandTimeoutExceedsPolicyError`（`command_timeout_exceeds_policy`、non-retryable）として subprocess 起動前に reject される。`resolved_seconds <= 0` は `source` を問わず `CommandTimeoutNonPositiveError`（`command_timeout_non_positive`）として同様に reject される（fix_delta で追加）。`baseline_vc_preflight.py` の CLI では、これらの違反を `status: blocked` / `failure_class: <error_code>` の JSON として出力し、対象 VC の subprocess は一切起動しない。

`aggregate_timeout_seconds`（plan 全体の per-command budget 合計）が `MAX_TOTAL_VERIFICATION_BUDGET_SECONDS` を超える場合も、`compute_canonical_vc_plan()` 自体が `AggregateTimeoutExceedsPolicyError`（`aggregate_timeout_exceeds_policy`）を **plan を返す前に** raise する（fix_delta P1-1: 以前は informational constant に留まり production では未使用だった）。これは #2207/PR #2221 の aggregate timeout budget 計算式（`effective_n * (150 + 15) + margins`、`contract_readiness_check.derive_review_budget()`）とは別の、command-level budget 自身に対する独立したハード上限であり、#2207 の計算式自体は本 Issue のスコープ外のまま変更していない。

### Consumers と outer wrapper timeout（AC2、消費者側と外側の待機時間、fix_delta P0-1/P0-2）

`contract_readiness_check.py` / `run_contract_review_once.py` / `run_root_review_pipeline.py` は、`DEFAULT_TIMEOUT_SECONDS`（per-command fallback）と `CLEANUP_TAIL_SECONDS`（cleanup tail）を `baseline_vc_preflight.py` からの import のみで参照し、独立した scalar を再定義しない。3 箇所すべてが `baseline_vc_preflight.compute_canonical_vc_plan()` を直接呼び出し（re-export のみ）、同一の canonical plan を消費する。

- `contract_readiness_check.run_baseline_vc_preflight()` は、`baseline_vc_preflight.py` を subprocess 起動する際に `--expected-plan-digest` を明示的に渡し、子プロセスが自分自身の `--body-file` から再計算した plan の digest と照合する（不一致は `vc_plan_digest_mismatch` として subprocess 起動前に reject）。
- `run_contract_review_once.py` は Step 5 で `--timeout-seconds` を**無条件には**渡さなくなった（fix_delta P0-1: 以前は常に `explicit_override` を強制し、`static_policy` budget を握りつぶしていた）。同様に `--expected-plan-digest` を渡す。
- `contract_readiness_check.effective_review_budget()`（fix_delta P0-2 で追加）は、`derive_review_budget()`（#2207 の計算式そのものは不変）の結果を、同じ plan の `aggregate_timeout_seconds` を下限として底上げする。この下限は `static_policy` 由来の budget が無い通常のケースでは常に no-op（マージン定数を #2207 formula 側と一致させているため）であり、`static_policy` が実際に per-command 150 秒超を要求する場合にのみ outer wrapper（`run_baseline_vc_preflight()` の supervisor timeout、`run_root_review_pipeline.py` が `reviewer_transport` へ渡す `per_attempt_deadline`/`total_deadline` を含む）を底上げする。

### Result item provenance（AC6、実行結果の来歴情報）

`baseline_vc_preflight.py` の各 VC result item は `timeout_provenance`（`timeout_seconds` / `cleanup_tail_seconds` / `source` / `estimator_version` / `estimator_input_digest`）を持ち、その VC 自身の `command_timeout_budget/v1` エントリから導出される（他コマンドの budget のコピーではない）。

### 履歴ベース estimator（History-based estimator、Issue #2254 で追加）

`history_estimate` source は `.claude/skills/issue-contract-review/scripts/vc_runtime_history.py`（新規モジュール、`baseline_vc_preflight.py` に詰め込まない）が所有する、**ローカル per-repository** SQLite store（`$XDG_STATE_HOME/loop-protocol/vc-runtime-history/<sha256(realpath(git-common-dir))>.sqlite3`、`XDG_STATE_HOME` 未設定時は `~/.local/state`）に永続化された過去実測 execution time evidence から算出する。repo にコミットされる state ではなく、Allowed Paths guard の対象外の runtime state として扱う（worktree に関わらず同一 repository の全 worktree が同一 store を共有する — `git rev-parse --git-common-dir` で解決）。

**Identity 分離**（PR #2245 iteration で `history_estimate` が一度 reject された反省を踏まえた設計）:

- `command_group_key`（`vc_runtime_history.compute_command_group_key()`）: 生のコマンドテキスト（shell 構文を正規化しない生テキストそのもの。`shlex.split()` は POSIX クォート除去のため option 順序保持の根拠にはならないと fix_delta で判明し、raw text 方式へ変更）・repo-relative cwd・command family から算出する history **グルーピング**専用のキー。`applied_timeout_ms` 等の実行パラメータを含まない（timeout が変化しても同じ history bucket を参照し続ける）。repo-relative cwd は `baseline_vc_preflight.resolve_repo_root_for_history()`（`git rev-parse --show-toplevel`）で解決した `repo_root` を渡すことで実現し、同一 repository の複数 worktree（絶対パスは異なるが `show-toplevel` は各々の cwd 自身）が同一 history bucket を共有できるようにする（fix_delta P0 blocker 2）。
- `environment_fingerprint`（`vc_runtime_history.compute_environment_fingerprint()`）: platform/arch/runner class/command family + lock digest/tool version のみを対象とする sample 互換性判定キー。全環境変数・token・credential・temporary path・`GITHUB_RUN_ID`・PID・日時等の volatile 値は含めない。
- `execution_id`（`vc_runtime_history.new_execution_id()`）: 1 回の実 subprocess launch を一意識別する UUID。SQLite の `sample_id` 主キーそのものであり、同一 `execution_id` での 2 回目以降の `record_sample()` 呼び出しは `INSERT OR IGNORE` により無視される（1 launch = 1 sample の保証）。
- 上記いずれも、実行パラメータ（timeout・env delta・state epoch）を含む既存の `compute_execution_key_hash()`（dedup-replay 用の識別子）を流用しない。

**`nearest_rank_v1` percentile 契約（固定）**: eligible sample は exit 0 の `success` サンプルのみ（`failed`/`cancelled` は診断用に保存されるが percentile から除外）、同一 `environment_fingerprint` かつ `observed_at_utc` が 30 日 TTL 以内、新しい順で最大 50 件の window、minimum success samples は 5（未満は候補なし）。`P95 = sorted_samples[ceil(0.95 * n) - 1]`、`candidate_seconds = ceil(observed_p95_ms * 1.5 / 1000)`（**整数演算のみ**で実装。`(observed_p95_ms * 3 + 1999) // 2000` という `ceil(a/b) == (a + b - 1) // b` の整数除算恒等式で計算し、`math.ceil()` の浮動小数点丸めを一切経由しない — fix_delta P0 blocker 7）。

**Timeout の right-censored 扱い**: `timed_out` サンプルは「真の実行時間は不明だが `applied_timeout_ms` より大きい」という下限のみ判明したデータとして扱い、percentile 計算からは除外する一方、`timeout_backoff_floor = min(MAX_PER_COMMAND_TIMEOUT_SECONDS, previous_applied_timeout_seconds * 2)`（直近の `timed_out` サンプルから算出）を別途の raise-only 候補として扱う。

**Raise-only precedence（AC4）**: `resolved = explicit_override if explicit_override is not None else min(MAX_PER_COMMAND_TIMEOUT_SECONDS, max(static_base, history_candidate, timeout_backoff_floor))`。`history_candidate` / `timeout_backoff_floor` が `static_base`（`static_policy` または `static_fallback` の解決値）を **strictly** 上回った場合のみ `source: history_estimate` を名乗る。同値以下の場合は `source` を `static_policy` / `static_fallback` のまま維持し、`history_estimate` を name-only で名乗らない（history は既存 trusted static policy を決して引き下げない）。

**Immutable snapshot 方式（AC1）**: `baseline_vc_preflight.produce_immutable_history_snapshot()` が root-owned producer として history store を実行開始時に一度だけ読み、`history_snapshot/v1`（`snapshot_as_of_utc` / `store_status` / `records`（`command_group_key` ごとの estimate + backoff）/ `snapshot_digest` を含む canonical JSON）を確定する。`compute_command_timeout_budget()` / `compute_canonical_vc_plan()` はこの snapshot dict を読むだけで SQLite に一切触れない -- 同一 body + 同一 snapshot からは、snapshot 確定後に store が更新されても、あるいは TTL 境界を wall-clock 上通過しても、同一 `plan_digest` が再現される。4 consumer（`baseline_vc_preflight.py` 自身の executor・`contract_readiness_check.py`・`run_contract_review_once.py`・`run_root_review_pipeline.py`）は、それぞれ自分の invocation の先頭でこの snapshot を一度だけ生成し、同一 invocation 内のすべての `compute_canonical_vc_plan()` 呼び出しへ同じ snapshot オブジェクトを渡す。subprocess 境界（`run_root_review_pipeline.py` の `run-checker-attempt` 子プロセス起動、`run_contract_review_once.py` の Step 2 (`contract_readiness_check.py`) と Step 5 (`baseline_vc_preflight.py`) 両方の子プロセス起動、`contract_readiness_check.py` 自身が `run_baseline_vc_preflight()` 経由で `baseline_vc_preflight.py` を子プロセス起動する経路）では、`vc_runtime_history.write_history_snapshot_file()`（temp file + `os.replace()` によるアトミック書き込み。fix_delta P1 blocker 5 item 3）で一時ファイルへ書き出し、`--history-snapshot-file` で子プロセスへ渡す（子は自前で snapshot を再生成せず、渡された snapshot をそのまま使う）。retry が発生する場合も同一の一時ファイルパスを再利用し、途中の store 更新を見ない（fix_delta P0 blocker 1: 修正前は `run_root_review_pipeline.py` の `run-checker-attempt` および `run_contract_review_once.py` の Step 2 がこの伝播経路の外にあり、それぞれ独立に snapshot を再生成していた）。

各 producer・consumer は `baseline_vc_preflight.resolve_repo_root_for_history()` で解決した `repo_root` を、その invocation が使う **同一の実効 cwd**（`run_contract_review_once.py` では current-head mode の `--cwd` と一致する `_effective_cwd_for_digest`）から一度だけ解決し、snapshot 生成・plan 計算・sample 記録のすべてに共有する（fix_delta P0 blocker 2/3: 修正前は producer 側が常に `cwd="."` で snapshot を作る一方、current-head mode の plan 計算は別の明示的 `--cwd` を使っており、`command_group_key` が不一致になり得た）。

**Non-blocking degradation（AC8）**: store が missing / locked（busy_timeout 0.25〜1 秒で non-blocking に諦める）/ corrupt / unknown schema version / invalid row（型・range・重複 `sample_id`・`command_group_key`/`environment_fingerprint` 不一致）のいずれの場合も、`build_history_snapshot()` は例外を投げず `store_status != "ok"` の空 snapshot に degrade する。この場合、対象コマンドの budget は常に `static_policy` / `static_fallback` へフォールバックし、history 障害だけを理由に review 全体が block されることはない。cross-process の snapshot file 伝播（`load_history_snapshot_file()`）も、schema・`records` の nested type/range・`snapshot_digest` の再計算一致まで含めて全構造検証し、不正な transport は同じ `store_status: "corrupt"` の空 snapshot へ degrade する（fix_delta P1 blocker 5 item 1 — 修正前は top-level dict と schema 名のみの検証で、budget lookup 中の例外や `vc_plan_digest_mismatch` を誘発し得た）。

**SQLite atomic write（AC7）**: 1 execution につき 1 回の短い `BEGIN IMMEDIATE` トランザクションで `INSERT OR IGNORE`。既定の rollback journal mode（WAL は使わない）。busy_timeout は 0.25〜1 秒の範囲でクランプし、lock 発生時は待たず non-blocking に `store_status: locked` を返す。schema バージョン行の初期化（`_ensure_schema()`）自体も `INSERT OR IGNORE` で行い、2 プロセスが同時に「fresh store」を観測してもどちらか一方の seed が unique violation で `store_status: corrupt` に落ちない（fix_delta P1 blocker 6/AC7 — 修正前は plain `INSERT` で、2 プロセス並行時に cold-start race が起き得た。AC7 のテストも 2 スレッドではなく実 `multiprocessing` プロセス境界で検証する）。`build_history_snapshot()` の読み取りも 1 回の明示的な `BEGIN DEFERRED` 〜 `COMMIT` の read transaction で全 SELECT を包み、同一 snapshot 内の複数 record が異なる commit 時点を混在して観測しない。

**Test 環境での安全装置**: `produce_immutable_history_snapshot()` / `record_command_execution_sample()` は、`PYTEST_CURRENT_TEST` が設定されておりかつ `VC_RUNTIME_HISTORY_STORE_PATH`（store path のテスト用 override 環境変数）が未設定の場合、実際の store には一切触れず no-op で degrade する。これは、`baseline_vc_preflight.py` を呼び出す既存の 30 以上のテストファイルが、開発者/CI マシンの実際の `$XDG_STATE_HOME` 配下へ意図せず書き込むことを防ぐための test-safety guard であり、`VC_RUNTIME_HISTORY_STORE_PATH` を明示的に設定するテスト（Issue #2254 自身の新規テストを含む）では通常どおり実 store の read/write path を検証できる。

### `ci_runtime_baseline_v1` との scope boundary（対象範囲の切り分け、AC10）

`docs/dev/ci-performance.md` が正本の `ci_runtime_baseline_v1` は GitHub Actions の **job/step 粒度**で elapsed_ms を記録する CI cross-run baseline スキーマであり、bootstrap-3-runs / decision-baseline-20-runs という別のポリシーで運用される。本 Issue の `history_estimate` は **ローカル per-command 粒度**で、CI run をまたがない、単一開発者マシン上の実行履歴のみを扱う。両者は以下の理由で意図的に schema・store を共有しない独立実装とした:

1. **粒度が異なる**: `ci_runtime_baseline_v1` は 1 job/step あたり 1 記録だが、`history_estimate` は 1 VC コマンドテキストあたり 1 記録であり、単一 job の中で複数の VC コマンドが実行される（集約前後で粒度が一致しない）。
2. **永続化スコープが異なる**: `ci_runtime_baseline_v1` は GitHub Actions run をまたぐ CI 側の永続化（cache/artifact 等）を前提とするが、本 Issue は明示的に CI cross-run persistence を Out of Scope とする（CI 実行では history store が存在しないため常に `static_policy` / `static_fallback` へ non-blocking に劣化する）。ローカル開発者マシンの `$XDG_STATE_HOME` は CI runner 間で共有されない。
3. **更新方法が異なる**: `ci_runtime_baseline_v1` は特定のポリシー（3-run bootstrap、20-run 決定）に基づく batch 更新だが、`history_estimate` は 1 execution = 1 sample の継続的な incremental 更新である。

CI cross-run history（GitHub Actions run をまたぐ永続化）や `ci_runtime_baseline_v1` の schema 変更が将来必要になった場合は、実需要を確認した上で別 Issue に分離する（本 Issue の Stop Condition / Out of Scope）。
