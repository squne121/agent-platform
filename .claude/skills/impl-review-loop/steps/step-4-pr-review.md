# Step 4: PR Review

> **移管状況 (I-4-4D3)**: 本書は移管元 `squne121/loop-protocol` tag `pre-split-agent-platform-20261020` (commit `6fa07b6a0ab932a62f08db6a8d521c0dfded2075`) の同名文書を最小差分で移管したもの。差分は `docs/dev/i4-4d3-step4-step5-manifest.json` の `intentional_diff` に記録している。本書が参照する `SKILL.md`・Agent 定義 (`pr-reviewer` / `test-runner` / `implementation-worker`)・`implement-issue` / `open-pr` / `pr-review-judge`・他の steps・補助 script 等のうち、本 repository に未移管のものは後続の I-4-4D スライスの担当であり、現時点では存在しない。未移管の対象を起動できるものとして扱ってはならない。本書の記述だけで Step 4 / Step 5 / Final Gate が本番稼働していると解釈してはならない。

Step 2 の `VC_ADJUDICATION_RESULT_V1.blocking == false` で完了したら、`pr-reviewer` SubAgent に PR レビューを委譲する。Step 2 が `blocking == true`（FAIL 相当）の場合は本ステップをスキップして Step 5 に直行（REQUEST_CHANGES 確定）。

## current-head gate（Issue #88、Step 4 起動直前の再照合）

linked Issue に Verification Commands がある場合、`pr-reviewer` の `spawn_agent` を送信する **直前** に、Step 2 が保持している `VC_ADJUDICATION_RESULT_V1` を current-head binding tuple
（PR の現在の head SHA、linked Issue の現在の body SHA256、Verification Commands の
literal command SHA256 一覧）に対して再照合する。この再照合は
`.claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py` の
`evaluate_step4_vc_gate()` を使い、以下のいずれかに該当する場合は
`pr-reviewer` を起動しない（fail-closed）。

- `VC_ADJUDICATION_RESULT_V1` が欠落・破損（malformed）
- `blocking == true`（FAIL / SKIP / fallback 検出を含む）
- head SHA が current PR head と不一致（stale head）
- body SHA256 が現在の Issue 本文と不一致（stale body）
- literal command SHA256 の集合が現在の Verification Commands と不一致（stale command）

上記いずれにも該当しない場合のみ `pr-reviewer` を起動する。
同一 binding tuple（head/body/command が全て一致）の有効な adjudication が
`LOOP_STATE.vc_adjudication`（Step 2 が `step4_persist_vc_adjudication()` で
書き込む、plain YAML/JSON シリアライズ可能なマッピング）に既に存在する場合は
`step4_gate_from_loop_state()` がそれを再利用し、test-runner を再実行しない
（binding tuple の head/body/command のいずれかが変われば別 key となり、
旧 adjudication は自動的に stale として扱われ再実行される。旧 `Step4AdjudicationCache`
はプロセス内メモリのみに存在し CLI を複数回起動する構成では前回状態が残らなかったため、
Issue #88 fix_delta で LOOP_STATE ベースの永続化へ置き換えられた）。

TEST_VERDICT comment/artifact（存在する場合）は diagnostics-only であり、
この gate の判定入力にはならない。TEST_VERDICT だけを与えても Step 4 の gate は開かない。

### canonical 手順（独立 VC の consumer 入口、Issue #2837）

独立 VC の `adapt -> adjudicate -> persist -> gate` は `adjudicate_vc_result.py step4-adjudicate` の **単一 process** が実行する。永続化の CLI 入口もこの subcommand であり、`LOOP_STATE`（`--loop-state-file`）の手組み・手動編集・別 process での再現を正規経路として使わない。`step4-adjudicate` は完全な（compact 化前の）結果を中間ファイルなしで persist に渡し、結果が gate を開かない場合は同一 binding の既存 PASS を失効させる。

#### 必須入力と取得元

| 入力 | 必須 field | 取得元 |
|---|---|---|
| `--test-verdict-file` | `step-2-verification.md` の委譲契約どおりの `TEST_VERDICT_MACHINE/v2`（`generated_at` と per-command `command_hash` を含む。`pr_review_only` の独立経路では GitHub 固有 trust marker のキーを含めない） | Step 2 で取得した test-runner の final result（read-only report）。final result が不在なら存在しないファイルのまま渡してよい（失効して exit 1） |
| `--contract-snapshot-file` | `status: go`、`body_sha256`（`sha256:` 付き）、baseline の VC classification（`results[]` または `checks.vc_preflight.classifications[]`）。または `ensure_contract_snapshot.py --artifact-dir` が保存した `CONTRACT_SNAPSHOT_ENSURE_RESULT_V1` envelope をそのまま（加工せず）渡す | `ensure_contract_snapshot.py` が trusted source に保存した `CONTRACT_REVIEW_RESULT_V1`（または baseline producer の `baseline_vc_preflight/v1`）。envelope の場合は次節「`ensure_contract_snapshot.py` の出力の渡し方（Issue #2996）」に従い `--producer-exit-code` / `--repo` を添える。`body_sha256` は live Issue 本文の digest と一致させる |
| `--producer-exit-code` / `--repo` | envelope を渡すときの `--producer-exit-code`（必須）。`status: ok` の envelope では `--repo`（`owner/repo`）と `--expected-issue-number` も必須 | `ensure_contract_snapshot.py` プロセスの実際の終了コード（`$?`）と、呼び出し元が独立に決めた `owner/repo`。envelope 内の `repo` 値は信用の根拠にしない |
| `--diff-summary-file` | `head_sha`（PR current head）、`pr_number`、changed paths（`changed_paths[]`） | `gh pr view <pr_number> --json headRefOid,number,files` による独立取得（`gh pr diff --name-only` でも可） |
| `--allowed-paths-file` | live Issue の `## Allowed Paths` を並べた JSON 配列 | `gh issue view <issue_number> --json body` で取得した本文の Allowed Paths |
| `--expected-head-sha` | PR current head SHA | `gh pr view <pr_number> --json headRefOid,mergeable,mergeStateStatus` の 1 回の応答の `headRefOid`。Step 5 の `--expected-head-sha` と live mergeability file も同じ応答から作る（`step-5-feedback-and-termination.md` 参照）。HEAD を別々に取得して混ぜない |
| `--expected-contract-body-sha256` | live Issue 本文の SHA-256（`sha256:` 付き） | `gh issue view <issue_number> --json body \| jq -j .body \| sha256sum`（`ensure_contract_snapshot.py` の `sha256_of(body)` と同じ digest） |
| `--expected-command-hashes-file` | literal Verification Command の SHA-256 を宣言順に並べた JSON 配列 | live Issue 本文を独立取得して保存したファイルから `adjudicate_vc_result.py extract-vc-metadata --body-file <live body>` で再導出した `command_hashes[]`（parse-only で VC を実行しない。`baseline_vc_preflight.py` は executor のため hash 取得に使わない。contract snapshot の値を使い回さない） |
| `--expected-issue-number` / `--expected-pr-number` | runtime_only または pr_review_only VC を含む場合は必須（未指定は拒否される。下記の単文を参照） | 呼び出し元が `gh issue view` / `gh pr view` で独立取得した live 値 |
| `--delegate-pr-review-only-nonpass` | 任意の opt-in flag（値なし）。`pr_review_only` の実行失敗を reviewer へ委譲したい場合のみ付ける（下記「`pr_review_only` の実行失敗を reviewer へ委譲する条件（Issue #2916）」） | root が Step 4 で `pr_review_only` VC の executed non-pass を reviewer 判断へ回すと決めたときに指定する。未指定の既定動作は fail-closed のまま |

`--expected-issue-number` / `--expected-pr-number` は runtime_only または pr_review_only VC を含む場合は必須である（未指定は `runtime_only_expected_*_number_missing` / `pr_review_only_expected_*_number_missing` で拒否され、reviewer dispatch は開かない）。

`step4-adjudicate` が current evidence として consume するのは `--test-verdict-file`（内部で adapter を通す）と `--contract-snapshot-file` / `--diff-summary-file` / `--allowed-paths-file` / `--expected-*` である。`--current-vc-result-file` は `adjudicate` / `step4-gate` 用であり、`step4-adjudicate` は読まない。

手組みの入力（上記の取得元を経ない JSON）は正規経路ではなく、保存済み PASS の根拠として扱わない。

#### `ensure_contract_snapshot.py` の出力の渡し方（Issue #2996）

`ensure_contract_snapshot.py --artifact-dir <dir>` は全 status で `<dir>/contract-snapshot-<issue_number>.json` に `CONTRACT_SNAPSHOT_ENSURE_RESULT_V1` envelope 全体を保存する。envelope は `CONTRACT_REVIEW_RESULT_V1` ではないため、手作業の `jq`（例: `.contract_review_once_result` の抽出）や手組み JSON といった前置 normalizer を経由して `--contract-snapshot-file` に渡してはならない。前置で加工すると、変換に失敗した時点で `step4-adjudicate` に到達せず、同一 binding の保存済み PASS が失効しないまま `--reuse-stored` で再利用されうる。envelope は保存されたファイルをそのまま `--contract-snapshot-file` に渡し、解決（unwrap・trusted comment の再検証）は `step4-adjudicate` の入力処理の内側で一度だけ行わせる。

producer の終了コードは stdout の JSON からは復元できないため、root は producer を起動した直後の `$?` をその場で保存し、`--producer-exit-code` として渡す。

```bash
# 1) envelope を保存する（終了コードは 0 / 10 / 20 / 40 / 50 / 60 になりうるので、その場で保存する）
set +e
uv run --locked python3 .claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py \
  --issue-number "$ISSUE_NUMBER" --repo "$REPO" --mode auto --post \
  --evidence-mode baseline --artifact-dir "$SNAPSHOT_DIR" > /dev/null
PRODUCER_EXIT_CODE=$?
set -e
CONTRACT_SNAPSHOT="$SNAPSHOT_DIR/contract-snapshot-$ISSUE_NUMBER.json"

# 2) 上の「呼び出し」の step4-adjudicate に次の 2 引数を追加する（envelope を渡すときのみ）
#    --producer-exit-code "$PRODUCER_EXIT_CODE" --repo "$REPO"
```

受理する `(status, source, producer 終了コード, nested result, contract_snapshot_url)` の組は次の 3 つだけで、これ以外は fail-closed で拒否する。

| status | source | `--producer-exit-code` | evidence の authority |
|---|---|---|---|
| `dry_run_would_post` | `materialized_go` | `20`（`contract_snapshot_url` は null、nested `contract_review_once_result.status: go`） | nested result。**候補 evidence** であり、投稿済み trusted snapshot でも implementation authorization でもない。`--repo` は使わない |
| `ok` | `existing_go` | `0`（`contract_review_once_result` は baseline mode では null が正常。非 null でも authority にしない） | `contract_snapshot_url` の comment |
| `ok` | `materialized_go` | `0`（`--post` 成功。`contract_snapshot_url` あり） | `contract_snapshot_url` の comment |

- `dry_run_would_post` の終了コード 20 と `human_judgment` の終了コード 20 は同じ値だが、`status` で区別する。`human_judgment` は終了コードが 20 でも常に拒否される。`blocked_needs_refinement`（10）、`runtime_error`（40）、`stale_or_conflicting_snapshot`（50）、`controlled_publisher_binding_failed`（60）、未知の status も常に拒否される。終了コードの欠落・非整数（桁数が過大な値を含む）・status との不一致も拒否される。`status` / `source` が文字列でない型不正な envelope も未処理例外にならず、構造化された `contract_snapshot_errors` として拒否され、既存の persist・失効経路を通る。入力ファイル（`--contract-snapshot-file` を含む）が非 UTF-8 のバイト列、過大なネストの JSON、4300 桁を超える整数リテラルを含む JSON の場合も、未処理例外にならず `input_read_error:` / `input_json_error:` の構造化エラー（例外型名を含む）として拒否され、同じ persist・失効経路を通る。
- `ok` の envelope は `contract_snapshot_url` の comment を shared parser（`contract_review_result_parser.py`）で再検証する。producer（`ensure_contract_snapshot.py`）と同じ優先順位で選択する。最新の trusted result が `blocked` の場合は拒否し（URL の go より新しい trusted blocked comment がある場合も同じ）、そうでなければ URL の comment id が `find_latest_go(trusted_only=True, fingerprint_ready_only=True)` の返す最新の fingerprint-ready な trusted go と一致することを要求する（より新しい fingerprint-ready な go があり URL が古い場合は拒否）。fingerprint 未完成の provisional / orphan な go は authoritative な GO として扱わず、それだけでは既存の fingerprint-ready go（producer が `existing_go` として返した URL）を失効させない（最新 trusted result の comment id と URL の comment id の一致は要求しない）。最新 trusted result が `go` / `blocked` 以外の status の場合も fail-closed で拒否する。さらに author が trusted publisher であること、`body_sha256` が `--expected-contract-body-sha256` と一致することを確認する。repo は envelope ではなく `--repo` から取り、envelope の `issue_number` と URL の issue を `--expected-issue-number` に束縛する（別 repo / 別 Issue の comment は拒否）。
- 解決後の object（`status` / `body_sha256` / `checks.vc_preflight.classifications` を持つ `CONTRACT_REVIEW_RESULT_V1` 相当）は一度だけ構築され、baseline classification・source integrity・current-head binding のすべてが同じ object を読む。head / Issue body digest / 宣言順の command hash / `pr_review_only` / `runtime_only` の既存 binding は緩まない。
- envelope 以外の snapshot に `--producer-exit-code` を指定した場合、および `--reuse-stored` と `--producer-exit-code` / `--repo` を併用した場合は拒否される。`adjudicate` / `step4-gate` は envelope を扱わない。

handoff が失敗した場合（producer の status / 終了コード不整合、trusted comment の再検証失敗、body digest 不一致、malformed envelope、outer failure の envelope に inner go が残っているだけの場合を含む）、`step4-adjudicate` は部分的な snapshot を渡さず `contract_snapshot=None` と構造化 errors（stdout の `adjudication.errors`、例: `producer_exit_code_mismatch:status=ok:exit_code=20`）で通常の adjudicate -> persist 経路を通り、`exit 1` を返して同一 binding の保存済み PASS を失効させる。是正は producer の再実行（または Step 2 の再実行）であり、envelope・`LOOP_STATE` の手編集ではない。

`--test-verdict-file` の `runtime_ac_results[].ac` は、contract snapshot の baseline classification（`results[].ac`。fallback リテラル `AC_UNKNOWN` とカンマ連結ラベルを含む）と逐語で一致しなければならない。root は Step 2 の委譲時にその `(ac label, literal command)` の組を test-runner へ渡し（`step-2-verification.md` の「`ac` label の出所と verbatim echo 規則」参照）、受領後に label を付け替えない。`(ac, command_hash)` 集合が baseline と異なる report は `baseline_current_mapping_mismatch`（exit 1）で gate を閉じる。是正は正確な label での test-runner 再起動であり、report の手編集ではない。

#### 呼び出し

```bash
# 0) live PR state を 1 回だけ取得する（Step 5 の mergeability と同じ応答から HEAD を取る）
LIVE_PR_JSON="$(gh pr view "$PR_NUMBER" --json headRefOid,mergeable,mergeStateStatus)"
LIVE_HEAD_SHA="$(jq -r .headRefOid <<<"$LIVE_PR_JSON")"

# 0b) literal command hash（宣言順）を、VC を実行しない parse-only subcommand で live Issue 本文から導出する
gh issue view "$ISSUE_NUMBER" --json body --jq .body > "$LIVE_ISSUE_BODY"
uv run python3 .claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py extract-vc-metadata \
  --body-file "$LIVE_ISSUE_BODY" > "$VC_METADATA" && jq '.command_hashes' "$VC_METADATA" > "$EXPECTED_COMMAND_HASHES"

# 1) 通常起動 / 再検証: adapt -> adjudicate -> persist -> gate を単一 process で実行する
#    $CONTRACT_SNAPSHOT が ensure_contract_snapshot.py の envelope（CONTRACT_SNAPSHOT_ENSURE_RESULT_V1）の場合のみ、
#    末尾に --producer-exit-code "$PRODUCER_EXIT_CODE" --repo "$REPO" を追加する（前節「ensure_contract_snapshot.py の出力の渡し方」）
uv run python3 .claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py step4-adjudicate \
  --loop-state-file "$LOOP_STATE_FILE" \
  --test-verdict-file "$TEST_RUNNER_REPORT" \
  --contract-snapshot-file "$CONTRACT_SNAPSHOT" \
  --diff-summary-file "$DIFF_SUMMARY" \
  --allowed-paths-file "$ALLOWED_PATHS" \
  --expected-head-sha "$LIVE_HEAD_SHA" \
  --expected-contract-body-sha256 "$(gh issue view "$ISSUE_NUMBER" --json body | jq -j .body | sha256sum | awk '{print "sha256:" $1}')" \
  --expected-command-hashes-file "$EXPECTED_COMMAND_HASHES" \
  --expected-issue-number "$ISSUE_NUMBER" \
  --expected-pr-number "$PR_NUMBER"

# 2) binding 不変で再検証が無い場合の新規 reviewer 起動: test-runner の report を読まず、
#    保存済み PASS を再評価して dispatch を記録する
uv run python3 .claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py step4-adjudicate \
  --reuse-stored \
  --loop-state-file "$LOOP_STATE_FILE" \
  --expected-head-sha "$LIVE_HEAD_SHA" \
  --expected-contract-body-sha256 "$LIVE_BODY_SHA256" \
  --expected-command-hashes-file "$EXPECTED_COMMAND_HASHES"

# 3) 診断用（read-only。LOOP_STATE を書き換えない、dispatch も記録しない）
uv run python3 .claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py step4-gate \
  --loop-state-file "$LOOP_STATE_FILE" \
  --expected-head-sha "$LIVE_HEAD_SHA" \
  --expected-contract-body-sha256 "$LIVE_BODY_SHA256" \
  --expected-command-hashes-file "$EXPECTED_COMMAND_HASHES"
```

`step4-adjudicate` は単一 JSON を stdout に出力する（`invoke_pr_reviewer` / `reason_code` / `binding_key` / `seq`）。exit code は `0=invoke`（pr-reviewer 起動可。`loop_state["dispatch"]` を更新し、`seq` が 1 進む）/ `1=rerun`（Step 2 の test-runner 再実行が必要。同一 binding の既存 PASS は失効済み）/ `2=malformed` または state 保存失敗（`reason_code` で区別する。`loop_state_malformed` 等: `--loop-state-file` / `--expected-command-hashes-file` が破損、または `LOOP_STATE` が object でなく、何も書き込まない。`state_persist_failed_invalidation_unconfirmed`: 下記「state 保存失敗の回復契約」）。`--loop-state-file` は read-modify-write（同一 directory の temp file から rename で原子的に書込み、不在なら空 mapping から作成）。`exit 1` の場合は `pr-reviewer` を起動せず Step 2 に戻る。`step4-gate` は従来どおり read-only で、`0=invoke` / `1=rerun` / `2=malformed` を返す診断用 subcommand である。

#### state 保存失敗の回復契約（`state_persist_failed_invalidation_unconfirmed`）

`--loop-state-file` への原子的書込み（temp file + `os.replace`）が失敗した場合、`step4-adjudicate` は traceback ではなく構造化出力（`invoke_pr_reviewer: false` / `reason_code: state_persist_failed_invalidation_unconfirmed` / `seq: null` / `stale_pass_invalidated_best_effort` / `errors`）を出し、**exit 2** で終了する。通常の再検証失敗（exit 1。同一 binding の既存 PASS は失効して永続化済み）とは区別すること。保存に失敗した invocation では、メモリ上の失効も新しい dispatch も永続化されていないため、**以前に保存された PASS が `--loop-state-file` に残っている可能性がある**。`stale_pass_invalidated_best_effort: true` は同一 binding の entry を 1 回だけ best-effort で削除できたことを示すが、確定的な保証ではない。

- この出力を受けたら pr-reviewer を起動せず、`seq` ファイルも更新しない。
- 失効の永続化が確認できるまで、`--reuse-stored` を呼ばず、`step4-gate` の stored PASS も根拠にしない。
- 保持している test-runner の final result（read-only report）があれば、ディスク / 権限などの保存失敗の原因を解消した後、同じ report で通常の `step4-adjudicate`（`--reuse-stored` なし）をやり直して adjudicate + persist を完了させてから再開する。report が無い場合は Step 2 の test-runner 再実行に戻る。
- WAL・ledger・lock・追加の state file は導入しない。回復は canonical な adjudicate + persist のやり直しだけである。

「reviewer dispatch 許可」とは `step4-adjudicate` が `invoke`（exit 0）を返したことである。reviewer の実起動は LLM 手順であり、dispatch 関数は存在しない。

#### exit 0 の意味と terminal approval の入口（Issue #2912）

`step4-adjudicate` の exit 0 は reviewer dispatch の許可であり、terminal approval ではない。AC 達成の証明でもなく、reviewer の意味判断（実行事実・AC・diff の照合）は pr-reviewer が行う。terminal approval（`termination_reason: approved` / `merge_ready: true`）の確定は `step5-terminal-gate` の exit 0 のみを根拠とする。`route_loop_verdict_v2()` を直接呼んだ結果から終端承認を確定してはならない（`step-5-feedback-and-termination.md` の terminal gate 節参照）。

`pr_review_only` VC を含む場合、`step4-adjudicate` は baseline の scope 認可（producer skip envelope）と current-head の実行事実（test-runner の独立 report）を別の状態として扱う。経路の判定（独立経路 / legacy 経路）、独立経路が受理する executed PASS item、`pr_review_only_*` の reason code と fail-closed の扱いは `step-2-verification.md` の「`pr_review_only` AC の current-head 証跡の取得と経路判定」節を正本とする。独立経路は GitHub 固有 trust marker を省略した report を入力とし、`--require-producer-receipt` 指定時と marker のキーが 1 つでも存在する report は legacy 経路で検証される。`pr_review_only` AC の実行失敗（FAIL / SKIP / fallback / 非 0 exit）は、既定では `pr_review_only_current_execution_not_pass:<AC>` で fail-closed になり、reviewer を起動せず dispatch seq も記録しない。root が `--delegate-pr-review-only-nonpass` を指定した場合に限り、実行失敗の事実を保持したまま reviewer へ委譲できる（次節、Issue #2916）。

#### `pr_review_only` の実行失敗を reviewer へ委譲する条件（Issue #2916）

`step4-adjudicate --delegate-pr-review-only-nonpass` は、独立経路の `pr_review_only` item が **実行済みの non-pass** だったときに、その事実を失わずに reviewer を起動できる状態へ進める opt-in である。次の 3 つを別々の状態として扱い、混同しない。

1. **実行事実**: `exit_code` / `status` / `fallback_detected` / `human_review_required` / `stop_condition_triggered` を、永続化される adjudication の `per_ac[].failure_keys`（既存 field。`kind: pr_review_only_current_execution_fact`、`key: <name>=<JSON 値>` の行）へ逐語で残す。PASS へ書き換えず、skip metadata で覆わない。新しい schema / ledger / artifact は作らない。
2. **reviewer dispatch 許可**: `step4-adjudicate` の `invoke_pr_reviewer: true`（exit 0）と `seq` の記録。該当 entry は `status: indeterminate` / `blocking: true` / `reason_code: pr_review_only_nonpass_delegated_to_reviewer` のままで、adjudication 全体も `overall_status: indeterminate` / `blocking: true` / `rerun_required: false` である。つまり dispatch 許可は **AC 達成ではない**。stdout の `adjudication.pr_review_only_nonpass_delegated` に委譲された AC が列挙されるので、root は reviewer へ「この AC は実行失敗の事実を保持したまま意味判断を委ねる」と伝える。
3. **terminal approval**: reviewer の verdict を `step5-terminal-gate` に渡して exit 0 になった場合のみ。reviewer が REQUEST_CHANGES / 未判断 / verdict なしの間は到達しない（`step-5-feedback-and-termination.md` 参照）。

委譲が許可される条件（すべて必要）:

- `pr_review_only` の独立経路であり、skip envelope の echo ではなく executed item であること。
- 事実が整合した non-pass であること: `status: fail` かつ `exit_code != 0`、`status: skip` かつ `exit_code != 0`、または `status: fail` かつ `exit_code: 0` で `fallback_detected: true`（test-runner の分類規則どおり、fallback 経由の成功は FAIL であり PASS ではない）。
- `human_review_required` / `stop_condition_triggered` が false であること（明示的な human / stop 信号は reviewer へ委譲しない）。
- report 全体の `result` が `FAIL` または `PARTIAL` であること（`PASS` や未知の値が失敗 row の隣にある入力は `pr_review_only_nonpass_report_result_inconsistent` で fail-closed）。
- head / Issue body digest / Issue 番号 / PR 番号 / changed paths の binding が、通常の独立経路と同じく完全に一致していること。

次は引き続き fail-closed で、reviewer を起動せず dispatch seq も記録しない: skip envelope の echo（`pr_review_only_independent_requires_executed_item:<AC>`）、整合しない事実（`status: pass` の全形態（`fallback_detected: true` を伴う `pass` を含む）、`fallback_detected: false` で `status: fail` / `skip` かつ exit 0、`status: skip` で exit 0、未知の `status`、非 bool の flag。`pr_review_only_current_execution_not_pass:<AC>`）、GitHub 固有 trust marker のキーが 1 つでもある report（legacy 経路になり `pr_review_only_current_authorization_mismatch:<AC>`）、binding 不一致、通常 AC の失敗が混在して `rerun_required` になる場合、委譲対象の `(ac, command_hash)` 以外の row が `fallback_detected: true` を持つ場合（委譲時は report 全体の `fallback_detected` 集約値を使わず、委譲対象以外の全 row を行単位で検査する。違反は委譲なしと同じ `pr_review_only_fallback_detected` で fail-closed になり、他 row の委譲では免除されない）。永続化済み adjudication の entry が改ざんされて事実が non-pass として整合しない、あるいは `pass` / `blocking: false` を名乗る場合も、`evaluate_step4_vc_gate()` が gate を開かない（`adjudication_ac_not_resolved` / `adjudication_blocking_true`）。`runtime_only` AC と混在する report では、report 全体の status が `fail` / `partial` でも、その原因が検証済みの委譲対象 `pr_review_only` non-pass だけであれば runtime_only 側の binding を拒否しない（runtime_only AC 自体の executed PASS 要件、および委譲対象以外の全 row の行単位 fallback 検査は維持する。runtime_only の FAIL / SKIP / fallback は委譲されず従来どおり fail-closed）。`--delegate-pr-review-only-nonpass` なしの既定動作、通常 VC、runtime_only の挙動は変わらない。

#### reviewer 起動前の `seq` の保存

root は reviewer を起動する **前** に、`step4-adjudicate` が返した `seq`（stdout JSON の `seq`）を、reviewer 結果の保存先と同じ場所のファイル（例: `<review-result-dir>/dispatch_seq`）へ書き残す。reviewer 結果を Step 5 へ渡すときはそのファイルの値を `--dispatch-seq` として添える（`step-5-feedback-and-termination.md` 参照）。resume / compaction 後に `loop_state` の最新 `seq` を再読込して渡してはならない（検査が空洞化する）。

#### 通常起動・resume・既存結果の再利用・Stage A/B recovery が通る gate

すべての経路は同じ gate 関数（`step4_gate_from_loop_state()` -> `evaluate_step4_vc_gate()`）を通る。

| 経路 | 呼ぶ入口 | `seq` の扱い |
|---|---|---|
| 通常起動（新規 reviewer 起動） | `step4-adjudicate`（再検証あり）または `step4-adjudicate --reuse-stored`（再検証なし） | `invoke` のたびに +1。起動前に保存する |
| resume / compaction 後に reviewer 結果が既にある（既存 reviewer 結果の再利用） | `step4-adjudicate` を呼ばない。保存済み `seq` ファイルと reviewer 結果を持って `step5-terminal-gate` へ直行 | 元の `seq` のまま |
| 既に dispatch 済み reviewer の Stage A / Stage B recovery（`--agent <name>` による回復を含む） | `step4-adjudicate` を呼ばない。得られた verdict と保存済み `seq` で `step5-terminal-gate` へ | 元の `seq` のまま |
| 新しい reviewer を起動する（再 dispatch、head / Issue body / VC binding が変わった場合を含む） | `step4-adjudicate`（再検証なしなら `--reuse-stored`）。その後に得た reviewer 結果だけを使い、`seq` ファイルを書き直す | +1 される。古い reviewer 結果は `dispatch_seq_mismatch` で拒否される |

`--agent` による reviewer 回復は main-session persona binding であり、子 task を spawn しない。したがって回復経路に子 task の `completed` を一律に要求しない（起動方式固有の完了確認は、本ファイルの Common Completion Protocol と Stage B のハンドオフ規則の現行仕様を維持する）。

Codex CLI では `pr-reviewer` custom agent を起動し、root thread は file edit / test 実行 / commit / push / review judgment を直接行わない。

## Discovery-Failure Recovery Routing（agent 定義変更後の delegation 失敗時の回復手順）

SubAgent 定義（`.claude/agents/*.md`）変更後に、その agent への delegation が
`Agent(subagent_type: "<変更対象>")` の `not found` 等で失敗しても、それを
即座に停止条件とせず、回復可能な runtime discovery failure（YAML parse
error、一時的な watch 遅延等の個別具体的原因を含みうる）の可能性を疑う。

### Stage A（bounded live-reload retry、短時間の再試行）

対象の `.claude/agents/` directory が既に session 開始時から存在し、
`--add-dir` 経由でも `--disable-slash-commands` でもない通常ケースでは、
agent 定義の変更は数秒以内に自動検出され次の delegation から使われるのが
公式の正常系である。`not found` が発生した場合、新しい retry framework を
作らず、短い bounded grace（例: 数秒程度）の後に現在 session で1回だけ
再 dispatch を試みる。

### Stage B（candidate-head fresh direct invocation、通常の第一候補 recovery）

Stage A で解決しない場合、candidate worktree を cwd とした fresh Claude
runtime で、対象 agent を `--agent <name>` として直接起動する。この
「fresh Claude runtime」は、呼び出し元 session が使っている
runtime/adapter をそのまま継承する（例: 呼び出し元 session が
`scripts/claude-gpt/launch.sh` 経由の `claude-gpt` adapter で
`--claude-bin` / `--claude-adapter claude-gpt` を指定して動作している
場合、Stage B もその同じ adapter 指定を保ったまま実行し、ambient な
plain `claude` バイナリへ黙って fallback しない）。そうしないと、実運用で
使われているものと異なる runtime 下で recovery が見かけ上成功してしまい、
production 固有の real failure を覆い隠しうる。

Stage B の成功条件は、達成したい保証の強さに応じて次の二段（tier）に
分かれる。

#### Tier 1: canonical review recovery（既定、通常はこれで十分）

plain `--agent <name>` による project-discovery route（既存の
`worktree-agent-runtime-smoke` の structured lane・`--claude-agent-name
<name>` route を再利用してよい）で fresh runtime を起動し、以下を満たせば
canonical review を実行する能力そのものは回復したとみなす: (i)
process/runtime が正常終了する、(ii) parse 可能な canonical
verdict／出力契約が返る、(iii) `reviewed_head_sha` 等が candidate
HEADと一致する、(iv) blockers/warnings が canonical contract に従う。
**この Tier 1 の成功だけでは、candidate-head の agent 定義が実際に
使用されたことの証明にはならない**（`worktree-agent-runtime-smoke` の
project-discovery lane 自身の canonical documentation が
`agent_definition` 結果を独立検証不能として `status: unavailable` と
定義しており、Claude Code の agent 解決は managed settings → `--agents`
→ project → user → plugin の priority order で行われ、cwd に近い
`.claude/agents/` が優先されるため、共有ディレクトリツリー内に同名の
agent 定義が複数存在する場合は filesystem read order が実際に使用された
定義を隠しうる）。**ここでは spawned child 用の
`SubagentStart`/`SubagentStop` / `causal_evidence_source ==
hook_id_correlated` を成功条件にしない**（`--agent <name>` による
main-session persona binding では child SubAgent が spawn されないため、
これを要求すると正常な canonical review を harness の都合で FAIL させて
しまう）。

#### Tier 2: candidate-definition binding（実際に証明が必要な場合のみ）

candidate-head の agent 定義が実際に使用されていることを厳密に証明する
必要がある場合に限り、`verify_pr_reviewer_permission_boundary.py` が既に
使っている passthrough の仕組み（candidate branch の `.md` agent 定義
ファイルをディスクから fresh に読み、session-local `--agents <json>`
引数として fresh な Claude Code invocation に渡す）を再利用する。これは
その passthrough の仕組み／パターンのみを再利用するものであり、同スクリプト
の permission-canary test suite 一式の実行を要求するものではない。Tier 2
まで実施した場合、Tier 1 の (i)〜(iv) に加えて、candidate HEADと一致する
agent 定義ファイルが実際に fresh invocation へ渡されたことを確認できた
場合にのみ、candidate-definition binding が成功したとみなす。

#### Stage B のハンドオフ（通常の loop への合流）

Stage B で fresh runtime に渡すプロンプトは、通常の `spawn_agent` 呼び出し
が渡すのと同じ materialize 済みレビュー入力を持たせる（実際の
`pr_number` と現在の candidate `reviewed_head_sha` を値として渡し、
placeholder を渡さない）。fresh process の最終出力は canonical
`LOOP_VERDICT`（`verdict` / `reviewed_head_sha` / `blockers` /
`warnings`）として parse する。Stage B がこの形で有効な verdict を得た
場合、その verdict はその Step 4 iteration の `pr-reviewer` 結果を
**代替する**。orchestrator は同一 iteration に対して `spawn_agent` や
child 用 Common Completion Protocol を再実行してはならない（`--agent
<name>` による main-session persona binding は child task を spawn
せず、待機する対象が存在しないため）。`worktree-agent-runtime-smoke` の
runtime `exit_code == 0` だけでは APPROVE authority にならず、単独では
有効な parsed verdict を構成しない（実際に消費されるのは parse された
LOOP_VERDICT の中身である）。

### Stage C（delegation/discovery diagnostic、必要な場合のみ）

「Agent tool による child delegation 自体」または「candidate 定義が child
として discover/spawn できること」を検証する必要がある場合に限り、
`worktree-agent-runtime-smoke` の structured lane・`--require-min-subagents
1`・**child 自身が出力する語**を `--expect-marker` に指定し、
`subagent_causal_evidence.causal_evidence_source == hook_id_correlated`
かつ `exit_code == 0` を成功条件とする。canonical review 完遂の必須 gate
にはしない。

Stage B が成功すれば通常の `impl-review-loop` を継続する。Stage C まで
必要な場合でも自動的に実行可能なら人間を呼ばない。

candidate head が変わった場合、既存の runtime evidence／review は
**staleとして扱い再取得**する。

Stage A〜C の全てで genuinely recovery 不可能な場合（fresh direct
invocation でも runtime/parse 失敗が続く等）にのみ、human/operator
blocker として停止する。

#### 「not found」の二つの意味の区別

本節が扱う discovery failure は、あくまで (a) `Agent(subagent_type:
...)` による pre-dispatch / dispatch 時点での agent-type discovery 失敗
（recoverable、本節の Stage A〜C の対象）である。これに対し、(b) 既に
生成された canonical child task 自身が後になって `list_agents` で
`not_found` を報告するケースは lifecycle-integrity failure であり、
本節の recovery routing の対象ではなく、既存の Common Completion
Protocol（本ファイル「Common Completion Protocol」節）の
`errored`/`interrupted`/`shutdown`/`not_found` の扱いに従い fail-closed
のままとする。

#### `--add-dir` に関する補足

`--add-dir` で追加したディレクトリ内の `.claude/agents/` は discovery
対象としてロードされるが、**watch されない**ため、追加・編集後は session
restart が必要である（Stage A の「通常ケース」から除外される具体的理由）。
本 repository が `--add-dir` を recovery route の第一候補にしない理由は、
discovery が不可能だからではなく、`${CLAUDE_PROJECT_DIR}` resolution が
hooks の `${CLAUDE_PROJECT_DIR}` interpolation を壊すためである
（`scripts/agent-ops/verify_pr_reviewer_permission_boundary.py` 11-38行
参照）。

## 委譲呼び出し

```yaml
spawn_agent:
  task_name: pr_review_i{iteration}
  agent_type: pr-reviewer
  fork_turns: none
  message: |
    Objective: review the actual implementation PR against its live Issue contract.
    Live reference: bind the actual PR number, linked Issue number, and reviewed head SHA.
    Bounded scope: bind the actual PR diff, AC, Allowed Paths, Verification evidence, and required checks.
    Expected result: LOOP_VERDICT with reviewed_head_sha, blockers, and warnings.
```

### Materialization rule（実値を具体化する規則）

`task_name` は実行直前に実際の非負 iteration で `pr_review_i{iteration}` から materialize し、stale-head の再レビューでは次の未使用 iteration を使う。同一 root session 内で既に保存済みの canonical task name を再利用してはならない。`fork_turns: none` のため、root は message に実際の PR number、linked Issue number、reviewed head SHA、PR diff、AC、Allowed Paths、Verification evidence、required checks を値として埋め込む。`Step 1 PR number`、`current reviewed head SHA`、変数名、波括弧・山括弧の placeholder を child message に渡してはならない。この static template 自体を tool call として送信してはならない。

SubAgent 側は `.claude/skills/pr-review-judge/SKILL.md` の手順を実行し、verdict 本文と最小 convention フィールドを呼び出し元へ返す（pr-reviewer は Write/Edit を持たないため、実際の PR コメント投稿は control-plane が行う。詳細は「期待する出力」参照）。

## Common Completion Protocol（共通完了プロトコル）

この規範は Step 1 implementation、Step 2 verification、Step 4 PR review、post-merge cleanup の4 dispatch site に共通である。

1. root は `spawn_agent` の戻り値から canonical `task_name` を保存する。
2. `wait_agent` は mailbox activity を待つためだけに使う。timeout、steer、途中 mailbox update は成功ではない。
3. root は対象 task の final result を消費してから `list_agents` で canonical task name の terminal `completed` を確認する。
4. `errored`、`interrupted`、`shutdown`、`not_found` は成功にせず fail-closed とする。
5. root は terminal `completed` と result の両方を確認した後にだけ最終 routing を決定する。

## PR レビュー前の CI 待機ルート

Step 4 では verdict 判定前に `wait_ci_checks.sh` を使って required checks の head-scoped 完了を待つ。

- `--required` は必須
- expected head SHA は Step 4 入力の `reviewed_head_sha`
- helper は全終了経路で `CI_WAIT_RESULT_V1_JSON=...` を 1 行だけ出力する
- exit code は `0=passed` / `1=CI negative or incomplete` / `2=auth, gh, malformed, invalid args`

```bash
.claude/skills/impl-review-loop/scripts/wait_ci_checks.sh \
  --repo "$(gh repo view --json nameWithOwner --jq .nameWithOwner)" \
  --pr <pr_number> \
  --head-sha <reviewed_head_sha> \
  --required \
  --interval 15 \
  --timeout-seconds 1800
```

### CI_WAIT_RESULT_V1 status routing（ステータス別ルーティング）

| status | routing |
|---|---|
| `passed` | PR review を継続 |
| `failed` | `get_ci_failed_log.sh` を呼び出して failed log summary を取得 |
| `cancelled` | `get_ci_failed_log.sh` を呼び出して cancelled / interrupted context を取得 |
| `pending_timeout` | fail-closed。`CI_PENDING_TIMEOUT` blocker で REQUEST_CHANGES |
| `no_checks` | fail-closed。required checks 未解決として REQUEST_CHANGES |
| `skipped_only` | fail-closed。required checks が skipped のみとして REQUEST_CHANGES |
| `head_sha_changed` | stale review。最新 head に対して Step 4 を再実行 |
| `auth_error` | fail-closed。認証/権限問題として REQUEST_CHANGES |
| `gh_error` | fail-closed。CLI/runtime 問題として REQUEST_CHANGES |
| `malformed_gh_response` | fail-closed。machine-readable parse 不能として REQUEST_CHANGES |

`bucket=skipping` は成功扱いにしてはならない。required-only 集合に skipped entry が残る場合は incomplete とみなし、少なくとも `skipped_only` は fail-closed とする。

## 期待する出力

pr-reviewer は判定結果（verdict 本文 + `verdict` / `reviewed_head_sha` / `blockers` / `warnings` の最小 convention、Issue #1873）を呼び出し元（control-plane）へ返す。`merge_ready` / `mergeability` / `required_auto_actions` / `allowed_paths_gate` は pr-reviewer の自己申告として受け取らない。mergeability（`mergeable` / `merge_state_status`）は control-plane が `gh pr view --json headRefOid,mergeable,mergeStateStatus` で都度直接取得し、live mergeability file として `step5-terminal-gate` に渡す（`step-5-feedback-and-termination.md` の terminal gate 節と `step-5-mergeability-handling.md` 参照）。

pr-reviewer は Write/Edit を持たないため、監査用の verdict コメント投稿は control-plane が通常の `gh pr comment --body-file` で行う（専用 semantic publisher は使用しない）。投稿する verdict コメント本文には人間可読の判定根拠（Mergeability / Evidence Check / Blockers / Non-blockers）を書き、以下の最小 YAML ブロックを併記する:

```yaml
verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED
reviewed_head_sha: <SHA>
blockers: []
warnings: []
```

投稿前後に `gh pr view --json headRefOid` で head をリードバックし、投稿後に head が変化していた場合は stale note として扱い fresh review を実行する（Safety Invariants）。pr-reviewer 自身は生の `gh pr review` を呼ばない。

`route_loop_verdict_v2()` によるルーティングの詳細は `step-5-mergeability-handling.md` を canonical とする。

## reviewed_head_sha 整合チェック

```bash
CURRENT_HEAD=$(gh pr view <pr_number> --json headRefOid --jq .headRefOid)
```

`reviewed_head_sha != CURRENT_HEAD` の場合は stale review とみなし、Step 4 を現在 head で再実行する。

## CI 失敗ログの取得（get_ci_failed_log helper）

`wait_ci_checks.sh` が `failed` または `cancelled` を返した場合のみ呼び出す。pending 中は呼び出さない。

```bash
REVIEWED_HEAD_SHA=$(gh pr view <pr_number> --repo <repo> --json headRefOid --jq .headRefOid)

.claude/skills/impl-review-loop/scripts/get_ci_failed_log.sh \
  --repo <owner/repo> \
  --pr <pr_number> \
  --head-sha "$REVIEWED_HEAD_SHA" \
  --max-bytes 60000
```

`reviewed_head_sha` には branch 名ではなく現在の PR head SHA を渡す。

### CI_FAILED_LOG_RESULT_V1_JSON の解釈

helper は出力末尾に `CI_FAILED_LOG_RESULT_V1_JSON: {...}` を出す。主要フィールドは以下。

```yaml
CI_FAILED_LOG_RESULT_V1:
  status: ci_failed | ci_passed | ci_pending | no_matching_run | log_unavailable
  run_id: <int>
  attempt: <int>
  head_sha: <sha>
  workflow_name: <str>
  failed_jobs: ["job-name", ...]
  retrieval_method: gh_log_failed | rest_job_logs | none
  redaction_applied: true | false
  truncated: true | false
```

| status | routing |
|---|---|
| `ci_failed` | log summary を `reviewer_verdict.blockers[]` に反映 |
| `ci_passed` | CI pass とみなしログ取得をスキップ |
| `ci_pending` | wait helper を再実行、または `CI_PENDING` blocker |
| `no_matching_run` | `CI_LOG_UNAVAILABLE` blocker |
| `log_unavailable` | `CI_LOG_UNAVAILABLE` blocker |
