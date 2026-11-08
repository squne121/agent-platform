# Step 2: Verification（検証ステップ）

> **移管状況 (I-4-4D2)**: 本書は移管元 `squne121/loop-protocol` tag `pre-split-agent-platform-20261020` (commit `6fa07b6a0ab932a62f08db6a8d521c0dfded2075`) の同名文書を最小差分で移管したもの。差分は `docs/dev/i4-4d2-step2-verification-manifest.json` の `intentional_diff` に記録している。本書が参照する `step-4-pr-review.md`・他の steps・Agent 定義 (`test-runner` 等)・`body_only_repair_plan.py` 等のうち、本 repository に未移管のものは後続の I-4-4D スライスの担当であり、現時点では存在しない。未移管の対象を起動できるものとして扱ってはならない。

Step 1 で PR が起票されたら、`test-runner` SubAgent に検証を委譲する。

Codex CLI では `test-runner` custom agent を起動し、root thread は file edit / test 実行 / commit / push / review judgment を直接行わない。

## 委譲呼び出し

Agent ツールで以下の static call shape を使って起動する:

```yaml
spawn_agent:
  task_name: verification_i{iteration}
  agent_type: test-runner
  fork_turns: none
  message: |
    Objective: execute the actual linked Issue Verification Commands as an independent read-only report.
    Live reference: bind the actual Issue number, PR number, contract body SHA, and diff head SHA.
    Bounded scope: bind the literal AC list and literal Verification Commands for that exact head only.
    Per-command inputs — echo these values exactly (one entry per Verification Command, in declaration order; the ac label includes the literal AC_UNKNOWN fallback and comma-joined multi-AC labels):
    - ac: <baseline ac label 1>
      command: <raw_command 1, verbatim>
      command_hash: <command_hash 1, sha256:<hex>, taken from extract-vc-metadata>
    - ac: <baseline ac label 2>
      command: <raw_command 2, verbatim>
      command_hash: <command_hash 2, sha256:<hex>, taken from extract-vc-metadata>
    Echo ac / command / command_hash verbatim into the matching runtime_ac_results[] row. Do not guess, recompute, or infer command_hash, and do not rely on implicit inheritance from the parent context.
    Expected result: a head-bound TEST_VERDICT_MACHINE/v2 read-only report that includes generated_at (UTC RFC 3339 time at which test-runner generated the report), per-command command_hash, and per-AC PASS, FAIL, or SKIP facts, without fabricating any GitHub workflow, check run, or artifact field.
    Trust markers: omit every GitHub-specific trust marker key (workflow_run_id, workflow_run_attempt, check_run_id, the artifact* keys, and the receipt keys) entirely; never emit them as null, empty, or placeholder values (a present key selects the legacy route).
```

### Materialization rule（実値を具体化する規則）

`task_name` は実行直前に実際の非負 iteration で `verification_i{iteration}` から materialize し、同一 root session 内で既に保存済みの canonical task name を再利用してはならない。`fork_turns: none` のため、root は message に実際の Issue number、PR number、AC 全文、literal Verification Commands 全文（各 command の `(ac, raw_command, command_hash)` の triplet。後述「`ac` label の出所と verbatim echo 規則」節に従う）、contract body SHA、diff head SHA を値として埋め込む。`LOOP_STATE`、`Step 1 PR number`、`current contract body SHA`、変数名、波括弧・山括弧の placeholder を child message に渡してはならない。この static template 自体を tool call として送信してはならない。

完了の扱いは4 site 共通の [Common Completion Protocol](step-4-pr-review.md#common-completion-protocol) に従う。Step 4 起動前に、root は `list_agents` で test-runner task の terminal `completed` と、その task の final result（read-only report）の両方を確認する（Issue #88 Required Design #1）。terminal `completed` のみ、または final result のみでは Step 2 完了とみなさない。

SubAgent の起動のみ、task completed のみ、final report のみを test-runner 完了扱いにしてはならない。final result（read-only report）を実際に取得した後にだけ、`adjudicate_vc_result.py step4-adjudicate`（`step-4-pr-review.md` の canonical 手順）へ進む。final result が不在・不読の場合は結果を捏造・補完せず、`step4-adjudicate` に test-verdict ファイルを渡せないものとして扱う（同一 binding の既存 PASS が失効し、gate は閉じる）。

SubAgent 側は `.claude/agents/test-runner.md` の手順を実行し、Verification Commands を実行して結果を **read-only report として呼び出し元へ返す**。test-runner は PR へのコメント投稿を行わない（Issue #1648, #88）。

## test-runner 委譲契約（producer report の必須項目、Issue #2837）

root が test-runner へ渡す委譲契約として、`TEST_VERDICT_MACHINE/v2` の read-only report は次の「独立した実行事実」を必ず含める。これは `adjudicate_vc_result.py` の canonical consumer（`adapt` -> `adjudicate` -> persist -> gate）が一度で PASS envelope を構成するために必要な最小集合であり、新しい schema family は作らない。

### 独立した実行事実（通常 VC / runtime_only の双方で必須）

| field | 内容 |
|---|---|
| `schema` | `TEST_VERDICT_MACHINE/v2` |
| `issue_number` / `pr_number` | root が `gh` で独立取得した live 値（runtime_only の binding が `issue_number` と `diff_summary.pr_number` を要求する） |
| `head_sha` / `reviewed_head_sha` / `diff_head_sha` | 実行対象の PR current head SHA（3 つは同一 head に束縛する） |
| `contract_body_sha256` | `sha256:` 付きの live Issue body SHA-256 |
| `generated_at` | test-runner が **この report を生成した UTC 時刻（RFC 3339、例: `2026-10-03T12:34:56Z`）**。root が後から記録した受領時刻・委譲時刻を実行時刻として代用してはならない。空文字・欠落は consumer が fail-closed にする（runtime_only は `runtime_only_generated_at_missing`、通常 VC は `uncertified_current_pass`） |
| `result` | `PASS` / `PARTIAL` / `FAIL` |
| `runtime_ac_results[]` | 全 Verification Command について `ac` / `command`（逐語） / **per-command `command_hash`**（`sha256:` 付き。必須） / `exit_code` / `status` / `fallback_detected` / `human_review_required` / `stop_condition_triggered` / `notes` |

`command_hash` は現行どおり command ごとに必須であり、欠落した行は consumer が受理しない。`generated_at` は上記の意味でのみ使う（既存 field であり、新 field・新 schema は追加しない）。

### `runtime_ac_results[]` 行に追加できる任意項目 `test_count` の導出規則（Issue #2971）

pytest 系 command の行に限り、test-runner は optional field `test_count: {subject, passed}` を返してよい（上の表の既存 field・必須性は変えず、この field の有無で `adjudicate_vc_result.py` の既存判定は変わらない）。`impl-review-loop` の body-only lane（`body_only_repair_plan.py plan`）が、PR 本文の test 件数 drift を機械的に同期する際の evidence として、この field を `TEST_VERDICT_MACHINE/v2` report の raw 行から読む。導出規則は `test-runner.md` の同名節を正本とし、次のとおり。

- `passed`: 当該 command の出力の最終 summary 行の `<N> passed` の `N` を機械的に写した非負整数。
- `subject`: 当該 command の pytest 対象 selector（path / node id / `-k` 式）を command 文字列から逐語で写した文字列。
- 省略: summary 行に `failed` / `error` を含む、非 pytest command、summary 行を解釈できない場合は field を省略する。`notes` の自然言語や reviewer の記述から導出しない。
- root は返却された `test_count` を補正・補完・別行への流用をしない（`command` / `command_hash` と同様に行ごとの事実として扱う）。

### `ac` label の出所と verbatim echo 規則（baseline 由来、Issue #2837 fix_delta）

consumer（`adjudicate_vc_result.py`）は baseline と current を `(ac, command_hash)` の組で対応付ける。`command_hash` だけが一致しても、`ac` 文字列が baseline classification と 1 文字でも違えば対応付けは成立しない。したがって per-command の `ac` 値の出所を次のとおり一意に固定する。

- **出所**: baseline classification（contract snapshot の `results[]`。`baseline_vc_preflight.py` が出力する `results[].ac`）の `ac` 値そのもの。コマンドに AC 注記が無い場合の fallback リテラル `AC_UNKNOWN`（例: `pnpm lint` / `pnpm test` / `pnpm build` や gemini の pytest）、および複数 AC に紐づく場合のカンマ連結ラベル（例: `AC1,AC2,AC3,AC4,AC5,AC6,AC7,AC8`、`AC10,AC11`。Issue 宣言順のまま）を含む。
- **root の責務**: root は test-runner へ渡す message に、全 Verification Command について `(ac, raw_command, command_hash)` の triplet を **そのまま逐語で** 埋め込む（`ac` は baseline classification 由来、`raw_command` / `command_hash` は後述の `extract-vc-metadata` の `commands[]` 由来）。この triplet は `--expected-command-hashes-file` と同じ独立ソース（live Issue 本文から `adjudicate_vc_result.py extract-vc-metadata --body-file <live body>` で再導出した `commands[]`。下記「副作用のない metadata 取得」参照）から取り、test-runner の出力から取ってはならない。root は report 受領後に label を付け替えて辻褄を合わせてはならない（remap 禁止）。
- **副作用のない metadata 取得**: `(ac, raw_command, command_hash)` の triplet と順序付き command hash は、live Issue 本文（`gh issue view <issue_number> --json body --jq .body` で独立取得して保存したファイル）に対する `uv run python3 .claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py extract-vc-metadata --body-file <live body>` から得る。この subcommand は parse-only で、`baseline_vc_preflight.py` の共有 parser と label / hash 関数（`AC_UNKNOWN` fallback、複数 AC の数値昇順カンマ連結、宣言順、`sha256:<hex>`）をそのまま再利用し、Verification Command を **一切実行しない**（subprocess を起動しない）。`baseline_vc_preflight.py --body-file ... --format json` は VC を実行する executor であり、`--static-only` を付けても正常な全 command の hash を列挙する API ではないため、metadata 取得に使ってはならない。出力の `commands[]`（`ac` / `line` / `raw_command` / `command_hash`）と `command_hashes[]`（宣言順）を使い、`status: blocked`（exit 2。`non_dollar_command` 等の静的 error は `static_errors[]` に表示）の場合は Step 2 に進まず live Issue 本文の是正を求める。baseline classification の authority は引き続き contract snapshot であり、この subcommand は classification を行わない。root は metadata 取得を名目に test を重複実行しない。test の実行は引き続き `test-runner` のみが行う。
- **triplet の埋め込み規則**: root は `extract-vc-metadata` の `commands[]` から得た `(ac, raw_command, command_hash)` の triplet を、1 command = 1 entry でそのまま逐語で dispatch message に埋め込む。test-runner は `command_hash` を推測・再計算してはならず、親 context の暗黙継承に依存してはならない（`fork_turns: none` のため親 context は存在しない）。test-runner の返却 report は `command_hash` を必須とするため、渡されなかった hash を test-runner 側で補完させてはならない。
- **test-runner の責務**: `runtime_ac_results[]` の各行に、渡された `ac` を **逐語のまま**（改名・統合・分割・範囲表記 `AC1-AC8` / `AC10-AC11` への圧縮・別 AC への再帰属をしない）、逐語の `command`、およびその `command_hash` とともに返す。1 command = 1 行で、root が渡した組と 1 対 1 に対応させる。

message template（上記 `Per-command inputs` ブロックを materialize 時に次の形で実値化する。`<...>` の placeholder は残さない）:

```text
Per-command inputs — echo these values exactly:
- ac: AC1
  command: <extract-vc-metadata の raw_command 逐語>
  command_hash: sha256:<extract-vc-metadata の command_hash 逐語>
```

`ac` は改名・統合・分割・範囲表記への圧縮をせず、そのまま `runtime_ac_results[].ac` に echo させる。

#### 失敗様式と是正

report の `(ac, command_hash)` 集合が baseline の集合と異なる場合（label の改名・統合・範囲表記・`AC_UNKNOWN` の欠落を含む）、`step4-adjudicate` は gate を閉じ、`errors: ["baseline_current_mapping_mismatch"]` / `overall_status: indeterminate` / `reason_code: adjudication_missing_or_malformed` を返して exit 1 となる（同一 binding の既存 PASS も失効する）。是正は **正確な label を渡して test-runner を再起動する** ことであり、report の手編集・label の事後 remap・consumer の check の緩和（曖昧一致・別名対応の追加）ではない。

### GitHub 由来情報（適用条件つき、legacy publish 経路と `--require-producer-receipt` 指定時のみ必須）

次の field は、materializer / publisher を経由する legacy publish 経路、または `--require-producer-receipt` を指定した adjudication でのみ必要になる（`pr_review_only` AC を含むだけでは必須にならない。経路の判定は後述「`pr_review_only` AC の current-head 証跡の取得と経路判定」節に従う）。通常 VC / runtime_only / `pr_review_only` の独立経路では必須ではなく、存在しないことを理由に report を不正扱いしてはならない。

- `workflow_run_id` / `workflow_run_attempt` / `check_run_id`
- `artifact`（name / artifact_digest / url）/ `artifact_payload` / `artifact_payload_sha256`、receipt 本体と `receipt_sha256`、およびそれらに紐づく GitHub 側 readback 情報
- 記述的 field（`producer_kind` / `repository` / `run_id` / `run_url`）は、独立経路では信頼根拠にならない。存在しても拒否せず、存在を要求もしない（legacy 経路では従来どおり検証される）

「公開された artifact が無い」ことと「独立した実行証拠が無い」ことを同一視しない。通常 VC / runtime_only / `pr_review_only` の独立経路は、上記の独立した実行事実（head / body binding と per-command の実行結果）だけで current-head の独立検証が成立する。一方、GitHub 由来情報が必要な経路（legacy 経路）で欠落している場合は、捏造・補完せず fail-closed とする。

### `pr_review_only` AC の current-head 証跡の取得と経路判定（Issue #2912）

baseline の contract snapshot に `# preflight-scope: pr_review_only` の VC（producer skip envelope）が含まれる場合、次の 4 つの概念を別々の状態として扱う。

1. **baseline の scope 認可**: baseline 側の producer skip envelope。「この AC は baseline では検証せず PR review 側へ責務を移す」ことだけを示す（`_is_producer_authorized_pr_review_only_skip()`）。実行済みの証拠ではない。
2. **current-head の実行事実**: test-runner の独立 report を adapter が `(ac, command_hash)` 単位で lossless に転記した command / exit_code / status / fallback / skip。adapter は `pr_review_only` を理由に値を書き換えず、producer が生成していない identifier / envelope を作らない。
3. **reviewer の意味判断**: pr-reviewer が実行事実・AC・diff を照合した verdict。
4. **terminal approval**: `step5-terminal-gate` だけが確定する。`step4-adjudicate` の exit 0 は reviewer 起動許可であり AC 達成の証明ではない（`step-4-pr-review.md` 参照）。

経路は per-item 検証より前に 1 回だけ決まる。次のいずれかに該当すれば **legacy 経路**、いずれにも該当しなければ **独立経路**である。

| 条件 | 経路 |
|---|---|
| `--require-producer-receipt` が指定されている | legacy |
| test_verdict が object でない（欠落・None） | legacy |
| GitHub 固有 trust marker のキーが 1 つでも存在する（値が null・空・placeholder でも存在すれば） | legacy |
| `TEST_VERDICT_MACHINE/v2` の report でない（`{"TEST_VERDICT": ...}` ラッパー、別 schema、`{}`） | legacy（`test_verdict_schema_mismatch` 等で fail-closed のまま） |
| 上記のいずれにも該当しない | 独立経路 |

独立経路では GitHub 固有 trust marker のキーを省略させる。trust marker のキー集合の正本は `adjudicate_vc_result.py` の `_PR_REVIEW_ONLY_TRUST_MARKER_KEYS`（`workflow_run_id` / `workflow_run_attempt` / `check_run_id` / `artifact` / `artifact_payload` / `artifact_payload_sha256` / `receipt_sha256` と receipt 本体のキーの 8 つ）である。root は委譲 message でこれらのキーを省略するよう test-runner に指示し、null・空文字・placeholder で埋めさせてはならない（キーが存在すれば legacy 経路になり、値が実値でなければ fail-closed になる）。legacy publish 経路と `--require-producer-receipt` 指定時のみ GitHub 固有 trust marker は必須である。雛形どおりに全 field を埋めた report は独立経路の入力にならない。producer 側の agent 文書の追従は #2892 の所有であり、追従までは root が Step 2 の message で省略を指示する。identifier の GitHub 上の実在性は検証せず、独立経路では不要な CI identifier を要求しない。

独立経路の current item は、adapter 由来の executed PASS（`status: pass` / `exit_code: 0` / fallback・human_review・stop_condition がすべて false）だけを受理する。skip envelope の echo は `pr_review_only_independent_requires_executed_item:<AC>`、executed だが PASS でない item は `pr_review_only_current_execution_not_pass:<AC>` で fail-closed になり（reviewer を起動せず dispatch seq も記録しない）、PASS への書き換えや skip metadata による被覆は行わない。実行失敗を reviewer の意味判断へ委譲する route は follow-up #2916 が所有する。独立経路の current-head binding（contract `go`・report 全体 `pass`・current / reviewed / diff head 一致・Issue body digest 一致・Issue / PR 番号の独立取得値との exact equality・changed paths が Allowed Paths 内・`(ac, command_hash)` 集合の完全一致）は runtime_only と同じ検証を `pr_review_only_*` の reason code で行い、`--expected-issue-number` / `--expected-pr-number` は必須である。`per_ac` は resolved な `pr_review_only` entry を Issue 宣言順の位置に常に含める（legacy 経路は従来どおり、通常 AC と混在する場合は除外する）。

### `test-runner.md` との優先関係

`.claude/agents/test-runner.md` は「`TEST_VERDICT_MACHINE/v2` の全フィールドは必ず含める（routing 必須フィールド）」と記述している。Step 2 の委譲契約と食い違う場合、Step 2（本節）が **適用条件の分離について優先する**: 通常 VC / runtime_only / `pr_review_only` の独立経路では GitHub 由来情報を report に捏造して埋めず、trust marker のキーごと省略する（上表と上記「`pr_review_only` AC の current-head 証跡の取得と経路判定」節の適用条件に従う）。`test-runner.md` 本体の報告例・規約の追従（例の `generated_at` 追記等）は agent 定義変更として別 Issue が所有し、本 Issue では変更しない。実 SubAgent がこの契約どおりに出力するかどうかの検証は follow-up の責務である。

## test-runner report の lossless な受渡し手順（handoff、Issue #3033）

root が test-runner の full `TEST_VERDICT_MACHINE/v2` を Step 4 の `step4-adjudicate` へ渡すときは、child が観測した per-command の実行事実を一切変えずに伝達する。新しい schema・gate・ledger・汎用 wrapper は導入せず、既存の `step4-adjudicate` / `step5-terminal-gate` の authority をそのまま使う。

### 受渡し経路

経路は `child の full final result（生テキスト）→ 本節の抽出・型保持変換 → 試行ごとに新しい --test-verdict-file → 既存 step4-adjudicate` に固定する。

- root は child の final result 全文を Write で `tmp/s4/test_runner_final_i{iteration}_a{attempt}.txt` のように **無加工のまま** 保存する（要約・整形・一部抜粋をしない）。
- 変換の出力先は試行ごとに新しいパス（例: `tmp/s4/test_verdict_i{iteration}_a{attempt}.json`）にする。旧 report のパスを今回の入力として渡さない。
- 変換後のファイルを `step4-adjudicate --test-verdict-file` へ渡す。それ以外の経路（要約値からの組立て等）で作った report を渡してはならない。

### 許可される変換と禁止される再構成

許可するのは Markdown / YAML から JSON への構文・型保持変換だけである。次を禁止する。

- `passed[]`、VC metadata（`extract-vc-metadata` の `commands[]`）、ログ断片、過去 report の成功値などの要約値から `runtime_ac_results[]` を再構成しない（`zip` による行の組立てを含む）。
- `runtime_ac_results[]`・`exit_code`・`status`・`fallback_detected`・`generated_at`・最終 `result` を新規に作らない・補完しない・書き換えない。
- `default=str`、`bool(...)`、欠落値の既定値補充など、「通る形」に直す処理を入れない。
- `generated_at` は `test-runner.md` の既存契約どおり引用付き文字列である。無引用の timestamp など JSON 化できない値は補正せず、変換失敗（次節の handoff 失敗）として扱う。

変換は生 YAML の時点で情報消失を検出して失敗させる（parse 後の object と JSON の比較だけでは、parse 時の消失は検出できないため）。次はすべて handoff 失敗である: mapping の重複 key（後続の PASS が先行の FAIL を上書きするため）、JSON で型が変わる非文字列 key、`on` / `off` / `yes` / `no` のような無引用の boolean 風 scalar や `010` のような YAML 1.1 固有の数値表記（文字列は引用する）、fence の未終端、既知 field の型不一致、JSON 往復で値・型・順序が変わる入力。fence の開始・終了は独立した行として認識し、`command` / `notes` に含まれる 3 連バッククォートは終端として扱わない。

### 実行可能な変換例

次の code block が変換の正本であり、`uv run --locked python3 - "$RAW_FINAL_RESULT_FILE" "$ATTEMPT_VERDICT_FILE"` の stdin にこの code block を **変更せず** 渡して実行する。回帰テストは同じ code block を文書から抽出して実行する。
<!-- step2-handoff-conversion-example -->
```python
import json
import re
import sys
from pathlib import Path
import yaml
fence = chr(96) * 3  # Markdown の fence 記号（この code block 自体の fence と衝突させないため直書きしない）


class StrictLoader(yaml.SafeLoader):
    """この変換専用の loader。global の SafeLoader / 他 consumer の semantics は変えない。"""


def _mapping(loader, node, deep=False):
    # 重複 key は後勝ちで先行事実（FAIL 等）を消すため fail-closed。key は JSON で型が変わらない str のみ。
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str:
            raise ValueError(f"non-string mapping key {key!r} is not preserved by JSON")
        if key in result:
            raise ValueError(f"duplicate mapping key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


def _bool(loader, node):
    # YAML 1.1 の on/off/yes/no は意図を保持できない。true / false 以外の無引用 scalar は拒否する。
    if node.value not in ("true", "false"):
        raise ValueError(f"ambiguous unquoted boolean-like scalar {node.value!r}; quote it as a string")
    return node.value == "true"


def _number(construct):
    def build(loader, node):
        # 8 進・60 進・桁区切りなど YAML 1.1 固有表記は JSON 数値として読めないので拒否する。
        try:
            json.loads(node.value)
        except ValueError:
            raise ValueError(f"ambiguous unquoted numeric scalar {node.value!r}; quote it as a string") from None
        return construct(loader, node)

    return build


StrictLoader.add_constructor("tag:yaml.org,2002:map", _mapping)
StrictLoader.add_constructor("tag:yaml.org,2002:bool", _bool)
StrictLoader.add_constructor("tag:yaml.org,2002:int", _number(yaml.SafeLoader.construct_yaml_int))
StrictLoader.add_constructor("tag:yaml.org,2002:float", _number(yaml.SafeLoader.construct_yaml_float))


def yaml_blocks(text):
    # fence は独立した行として認識する。closing は opening と同種で opening 以上の長さの fence のみ。
    blocks, lines, index = [], text.splitlines(), 0
    while index < len(lines):
        opening = re.fullmatch(r"(`{3,})[ \t]*([^`\s]*)[ \t]*", lines[index])
        index += 1
        if opening is None:
            continue
        body = []
        while index < len(lines) and not re.fullmatch(r" {0,3}`{%d,}[ \t]*" % len(opening.group(1)), lines[index]):
            body.append(lines[index])
            index += 1
        if index >= len(lines):
            raise ValueError("unterminated markdown fence")
        index += 1
        if opening.group(2) in ("yaml", "yml") and any(line.startswith("TEST_VERDICT:") for line in body):
            blocks.append("\n".join(body) + "\n")
    return blocks


def same(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return list(left) == list(right) and all(same(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    return left == right


raw_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
try:
    out_path.unlink(missing_ok=True)  # 失敗時に古い有効 report を残さない
    blocks = yaml_blocks(raw_path.read_text(encoding="utf-8"))
    if len(blocks) != 1:
        raise ValueError(f"expected exactly one TEST_VERDICT yaml block, found {len(blocks)}")
    report = yaml.load(blocks[0], Loader=StrictLoader)["TEST_VERDICT"]
    if not isinstance(report, dict):
        raise ValueError("TEST_VERDICT is not a mapping")
    rows = report.get("runtime_ac_results")
    if type(report.get("result")) is not str or type(rows) is not list or not all(isinstance(row, dict) for row in rows):
        raise ValueError("result must be a string and runtime_ac_results a list of mappings")
    for owner, expected in [(report, {"generated_at": str, "schema": str}), *[(row, {"command": str, "command_hash": str, "status": str, "exit_code": int, "fallback_detected": bool, "notes": str}) for row in rows]]:
        for key, kind in expected.items():
            if key in owner and type(owner[key]) is not kind:
                raise ValueError(f"{key} must be {kind.__name__}, got {type(owner[key]).__name__}")
    # default= を渡さない・NaN を許さない: JSON 非対応型は TypeError / ValueError で失敗させる
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False)
    if not same(json.loads(payload), report):
        raise ValueError("JSON round trip changed a value, type, or order")
    out_path.write_text(payload, encoding="utf-8")
    if not same(json.loads(out_path.read_text(encoding="utf-8")), report):
        raise ValueError("saved handoff file differs from the parsed report")
except Exception as exc:
    out_path.unlink(missing_ok=True)
    print(f"handoff failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
```

### handoff 失敗の分岐

次のいずれかを handoff 失敗として扱う。

- (a) child が full report を返さない。
- (b) full report を受領したが、抽出・YAML parse・型保持変換・JSON 化・保存のいずれかに失敗する（上記変換例は失敗時に出力先を残さず exit 1 で終わる）。

回復は次の順で行う。

1. raw report の再取得（SubAgent の既存 final result があれば優先する）。再取得できたら新しい試行として、新しい raw 保存先と新しい `--test-verdict-file` パスで変換をやり直す。
2. 再取得が不可能なら、同一 binding（head / body sha / command triplet）で test-runner を再実行する。

handoff 失敗が解消できない間は、**未作成または不正な当該試行の入力パス**を通常の `step4-adjudicate`（`--reuse-stored` なし）に渡し、`step-4-pr-review.md` 既存の失効経路（exit 1、同一 binding の旧 PASS 失効）へ到達させる。失効と再 adjudication が完了するまで、`--reuse-stored` と旧 `step4-gate` の PASS を根拠にしない。LOOP_STATE の手編集はしない。手組みの PASS・stale HEAD・別 body の証跡転用を許さない。

### #2916 との関係

root は実行済みの non-pass（FAIL / SKIP / fallback）を PASS に変えない。`pr_review_only` の実行済み non-pass の既定は fail-closed（`step4-adjudicate` が拒否する）である。`--delegate-pr-review-only-nonpass` の既存条件（`step-4-pr-review.md` 参照）を満たす場合に限り、失敗事実を保持したまま reviewer dispatch を許可できる。dispatch 許可（`invoke_pr_reviewer: true`）は AC 達成でも terminal approval でもなく、最終承認は既存の `step5-terminal-gate` だけが確定する。不正・欠落・矛盾・binding 不一致の report は委譲の対象外で、fail-closed のままである。

### 実セッションでの最小確認手順

1. SubAgent の terminal `completed` を確認し、final result 全文を取得する。
2. 全文を無加工で raw 保存する。
3. 上記の変換を実行し、試行ごとに新しい `--test-verdict-file` を作る。
4. child の full report と handoff file を readback 比較する（schema、`generated_at`、`result`、全 `runtime_ac_results[]` の順序と値、任意 field、HEAD・body・command binding）。
5. 比較が一致した場合だけ `step4-adjudicate` へ渡す。

静的テストの PASS を、実セッションでの handoff の保証と混同しない。実セッションでの readback 比較を実施していない間は「未確認」と報告する。

## 独立検証（証拠権限の切替、Issue #1856 Phase 1）

Step 2 の routing 正本は、TEST_VERDICT comment/artifact の有無に依存しない。orchestrator（呼び出し元）が以下をその場で照合するだけで判定する。materializer・dedicated publisher・producer側の署名情報・PR 上の TEST_VERDICT コメントは、この照合の routing input として要求しない（それらの実装ファイル自体は Phase 3 の別 Issue まで物理削除しないが、Step 2 の判定ロジックはこれらに依存しない）。

1. **current head SHA の一致**: test-runner の read-only report が主張する `head_sha` が、orchestrator が独立に取得した PR の current head SHA（`gh pr view --json headRefOid` 等）と一致すること。不一致は stale evidence として fail-closed（`VC_ADJUDICATION_RESULT_V1.blocking = true`）。
2. **literal command SHA256 の一致**: report に含まれる各 Verification Command の実行文字列が、linked Issue の Verification Commands ブロックに記載された literal command と一致すること（command 文字列そのものの SHA256 一致、または orchestrator による文字列比較のいずれかで確認する）。commands の改変・省略・置換は fail-closed。
3. **AC ごとの PASS/FAIL/SKIP と exit code**: report の per-AC 結果を、上記 2 点の照合が成立した場合にのみ evidence として採用する。

TEST_VERDICT（materializer/publisher 経由で PR に投稿される YAML、存在する場合）は、上記の独立検証を代替しない。TEST_VERDICT の有無に関わらず、Step 2 は同じ独立検証手順から同じ判定を返す。新規 artifact schema は追加しない。

## 判定ルーティング

`VC_ADJUDICATION_RESULT_V1.overall_status` と `blocking` を、上記の独立検証結果 + contract snapshot + diff summary + allowed paths から生成し、Step 2 routing の正本にする。

`VC_ADJUDICATION_RESULT_V1` の `overall_status` / `blocking` が欠落、破損、期限切れである場合は fail-closed とし、Step 2 の判定は blocking とする。

判定表:

| 手順 | 条件 | 次アクション |
|---|---|---|
| 1 | test-runner report の `head_sha` != PR current head SHA | stale evidence として fail-closed。`VC_ADJUDICATION_RESULT_V1.blocking = true` 扱いで再検証へ |
| 2 | 実行された Verification Command の文字列が linked Issue の記載と不一致 | fail-closed。`VC_ADJUDICATION_RESULT_V1.blocking = true` 扱いで再検証へ |
| 3 | `VC_ADJUDICATION_RESULT_V1` 欠落・破損・期限切れ | fail-closed。Step 2 エビデンス不足/再実行扱いとして再判定へ |
| 4 | `VC_ADJUDICATION_RESULT_V1.blocking == false` | Step 3（pr-reviewer）へ |
| 5 | `VC_ADJUDICATION_RESULT_V1.blocking == true` | Step 5 へ。rerun / REQUEST_CHANGES / human escalation を判定 |

## runtime_only VC の取り扱い（Issue #2467）

linked Issue の Verification Commands に `# preflight-scope: runtime_only` marker（正規 producer envelope: `runner=skipped`/`classification=skipped`/`category=preflight_scope_runtime_only`/`decision=go`/`scope_class=runtime_only`/`verification_owner=impl-review-loop`/非空 `deferred_reason`/`runtime_verification_required=true`）が含まれる場合、`adjudicate_vc_result.py` はそれを **baseline 側の delegation authorization** としてのみ扱う（post-implementation の実行を test-runner へ委譲してよい、という許可）。current 側にこの skip envelope を再要求することはない。

current 側は、同じ `(AC, command_hash)` に対する **test-runner の実際の実行結果**（`TEST_VERDICT_MACHINE/v2.runtime_ac_results[]` を `adapt_test_verdict_to_current_vc_result()` で変換したもの）で `status: pass` / `exit_code: 0` / `fallback_detected: false` / `human_review_required: false` / `stop_condition_triggered: false` を満たす場合にのみ、current-head 独立 binding（Issue / PR / current head / reviewed head / diff head / Issue body digest / source integrity。Issue / PR は orchestrator が独立取得した live 値との exact equality で照合する）と合わせて nonblocking で adjudicate される（`runtime_only_current_head_binding_pass`、`per_ac` には他の AC と同じく Issue 宣言順で残る）。baseline snapshot 上のスキップ宣言だけでは（current 側が同じ skip envelope を echo しただけでは）nonblocking PASS にならない（`runtime_only_current_execution_not_pass`）。artifact / receipt / TEST_VERDICT はこの判定の必須入力ではなく、存在する場合の診断用 optional provenance に留まる（`pr_review_only` の legacy 経路が要求する GitHub Actions readback とは異なる）。この分岐は既存の `pr_review_only` authorization scope を拡張するものではなく、独立した fail-closed 経路として扱われる（`pr_review_only` の legacy 経路のスキップは、通常 AC と混在する場合 `per_ac` から除外される既存の非回帰仕様を維持する。独立経路の扱いは上記「`pr_review_only` AC の current-head 証跡の取得と経路判定」節を参照）。

## BEHIND 状態の取り扱い

`merge_state_status: BEHIND` は「head ref が base branch より古い（base が先行している）」状態を意味し、`mergeable: MERGEABLE` と両立する。
`BEHIND` は `CONFLICTING / DIRTY / BLOCKED` と同一視しない。`CONFLICTING PR Escalation Runbook` の発動条件に該当しない。

`BEHIND` の場合、Step 2 では `update-branch` / `rebase` を実行しない。
branch の更新（`gh pr update-branch` 等）は Step 5 および `#67` の責務として分離されており、Step 2 はその実行を担わない。

## 出力

LOOP_STATE.last_step = "verification" に更新し、`VC_ADJUDICATION_RESULT_V1` を会話履歴に保持して次ステップへ。

## #88 との関係（Issue #1648 AC5 / Issue #1856 Phase 1 / Issue #88 reframe で更新）

`#88`（Step 2/4 の docs-only 責務記述）は、当初は read-only report -> materializer -> dedicated publisher の実装経路によって実現されていた。Issue #1856（evidence authority cutover, Phase 1）により、Step 2 の routing 正本は TEST_VERDICT comment/artifact の有無に依存しない独立検証（current head SHA + literal command SHA256 の照合）へ切り替わった。materializer/publisher/producer側の実装自体は Phase 3 の別 Issue まで物理削除しないが、Step 2 の判定契約はそれらを要求しない。

その後 PR #2310 のレビューで、独立 Issue VC が Step 4 の初回 reviewer 起動前に渡されず、同一 head に対して不要な二重 review（REQUEST_CHANGES → APPROVE）が発生した。これを受けて `#88` は「独立VCが current-head で完了・束縛・adjudicate されるまで Step 4 の pr-reviewer を起動しない」contract へ reframe された。この gate 自体は `step-4-pr-review.md` の「current-head gate」節、および `.claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py` の `evaluate_step4_vc_gate()` / `step4_gate_from_loop_state()` として実装される。`#88` 自体のクローズは、merged readback を前提とした supersede close の対象として別途判断する（本ドキュメント更新自体は `#88` を自動でクローズしない）。

test-runner が返す `TEST_VERDICT_MACHINE/v2` の read-only report は、`adjudicate_vc_result.py` が受理する `baseline_vc_preflight/v1` 形とスキーマが異なる。Step 2 は final result 取得後にこの report を `adjudicate_vc_result.py step4-adjudicate`（単一 process で canonical adapter `adapt_test_verdict_to_current_vc_result()` -> adjudicate -> `step4_persist_vc_adjudication()` -> gate を実行する canonical 入口。手順と引数は `step-4-pr-review.md` 参照）へ渡す。得られた `VC_ADJUDICATION_RESULT_V1` が `evaluate_step4_vc_gate()` を満たす場合のみ `LOOP_STATE.vc_adjudication`（binding tuple キー付きの plain mapping）へ永続化され、同一 binding で gate を開かない結果は既存の保存済み PASS を失効させる。`adapt` / `adjudicate` / `step4-gate` の個別 subcommand は診断用に残る。Step 4 は test-runner を再起動せず、`step4_gate_from_loop_state()`（`adjudicate_vc_result.py step4-gate` CLI、`step-4-pr-review.md` 参照）で live binding へ再照合するのみとし、stale/invalid なら Step 2 へ戻して新規 test-runner task を起動する。
