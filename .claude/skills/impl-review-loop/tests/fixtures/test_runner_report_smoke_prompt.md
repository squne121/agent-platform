Issue #2892 runtime smoke (test-runner の TEST_VERDICT_MACHINE/v2 報告契約の read-only 合成 smoke).
<!-- このタイトル行は Issue #2892 の runtime smoke を説明する日本語注記である。test-runner SubAgent が自明な read-only の fixture VC だけを実行し、報告契約どおりの値水準の report を最終 message に出力するかを確認する。 -->

This is a harmless, read-only, synthetic smoke. It does not use the network, does not call GitHub,
does not run `git`, does not run `uv run`, and does not write or mutate any file or any GitHub object.
Its only purpose is to confirm that a delegated `test-runner` SubAgent runs three trivial read-only
fixture commands, obtains `generated_at` by running exactly `date -u +%Y-%m-%dT%H:%M:%SZ`, and returns
a TEST_VERDICT_MACHINE/v2 report whose values follow the machine-valid example in its agent definition.
This smoke does not prove that the report parses as YAML or that a consumer accepts it; that is covered by the pytest contract test.
<!-- この段落は、ネットワーク・GitHub・git・uv run・書込みを一切使わない read-only の合成 smoke であること、test-runner が 3 つの自明な fixture コマンドを実行し、date の exact 形で generated_at を取得して、agent 定義の machine-valid example に従う report を返すことを確認する旨を説明する日本語注記である。report が YAML として parse できることや consumer が受理することは pytest の contract test の範囲であり、この smoke は主張しない。 -->

Use the `Agent` tool exactly once, with `subagent_type: "test-runner"`, to launch the `test-runner` SubAgent in the foreground (do not run it in the background), with exactly this message. Wait for the SubAgent to finish and return its final response before doing anything else:
<!-- Agent ツールを 1 回だけ foreground で subagent_type が test-runner の SubAgent として起動し、子の完了と最終応答を待つ旨を示す日本語注記であり、次のコードブロックは SubAgent へ送信する指示文そのものなので内容は変更しないこと。 -->

```text
Objective: execute the three fixture Verification Commands below as an independent read-only report, and return your final TEST_VERDICT_MACHINE/v2 report.
Fixture binding values (synthetic smoke values, not a real PR; do not call gh and do not run mergeable detection): issue_number 2892, pr_number 1, head_sha 0123456789abcdef0123456789abcdef01234567, reviewed_head_sha 0123456789abcdef0123456789abcdef01234567, diff_head_sha 0123456789abcdef0123456789abcdef01234567, contract_body_sha256 sha256:0000000000000000000000000000000000000000000000000000000000000000.
AC list: AC1, AC2.
Per-command (ac, command, command_hash) tuples. Echo each ac label, command and command_hash verbatim into runtime_ac_results (one command = one row, never rename, merge, split or range-compress an ac label, and never compute or guess a command_hash):
1. ac: AC1 | command: echo test-runner-report-smoke-ac1 | command_hash: sha256:bbe7aa4eb971c713a6279d904efba16fe07babd0cc67946707fc0a435fe485f5
2. ac: AC_UNKNOWN | command: echo test-runner-report-smoke-unlabeled | command_hash: sha256:8d5bee214d1668ff6e21acfb70679e60694d4c48b93c7975412a0549087bcb48
3. ac: AC1,AC2 | command: test -f .claude/agents/test-runner.md | command_hash: sha256:a276395890eac0420d91408c11bc598301b8834dceba698c50527b7863427f5c
Run each command verbatim exactly once. Right before you write the report, obtain generated_at by running exactly `date -u +%Y-%m-%dT%H:%M:%SZ` and put its output into generated_at as a quoted string. Never guess or reuse a fixed time.
Return the report in the "machine-valid example" form of your agent definition: a single yaml block starting with TEST_VERDICT:, no placeholders, strings quoted EXCEPT `schema` and `result`, which must be bare (unquoted) scalars exactly as in the machine-valid example (`schema: TEST_VERDICT_MACHINE/v2`, `result: PASS`), booleans as true/false, exit_code as an integer, result PASS only if all three commands passed, and no GitHub-derived field (no producer_kind, repository, run_id, run_url, workflow_run_id, workflow_run_attempt, check_run_id, artifact).
Do not run any other command, and do not modify any file.
```
<!-- このコードブロックは子 test-runner SubAgent へ送る英語の指示文である。合成の binding 値、3 つの (ac, command, command_hash) の組（通常 AC・literal AC_UNKNOWN・カンマ連結ラベル）、date の exact 形による generated_at 取得、machine-valid example の形での report 返却（文字列は引用付きだが schema と result だけは example どおり引用なしの bare scalar とする）、GitHub 由来 field を含めないことを指示する。 -->

After the SubAgent has completed and returned its final response, write your own final message as the SubAgent's full final response text copied verbatim, and nothing else.
Do not summarize, paraphrase, reformat, or omit any line of the report, and do not add any text of your own.
<!-- この段落は、SubAgent の最終応答を要約・言い換え・整形・省略せず、そのまま最終 message として転記し、自分の文章を追加しない要件を説明する日本語注記である。 -->

Do not modify any repository-tracked file. Do not run any command yourself.
<!-- この段落は repository 内の file を変更せず、親自身は command を実行しない制約を説明する日本語注記である。 -->

## Operator 用の期待 marker 一覧（Operator-lane expected markers、SubAgent への指示ではない）

The operator lane passes the following value-level markers to the runtime smoke runner with `--expect-marker`, one literal per line. This section is not part of the SubAgent message; do not include it in your output.
The three (ac, command, command_hash) constants above were derived independently with the same shared helper as `adjudicate_vc_result.py extract-vc-metadata`.
<!-- この節は operator lane が runtime smoke runner へ --expect-marker として渡す値水準 marker の一覧であり、SubAgent への指示ではない。上の 3 つの (ac, command, command_hash) 定数は adjudicate_vc_result.py extract-vc-metadata と同じ共有 helper で独立に導出した値である。 -->

```text
schema: TEST_VERDICT_MACHINE/v2
result: PASS
generated_at: "20
fallback_detected: false
status: "pass"
ac: "AC1"
ac: "AC_UNKNOWN"
ac: "AC1,AC2"
command_hash: "sha256:bbe7aa4eb971c713a6279d904efba16fe07babd0cc67946707fc0a435fe485f5"
command_hash: "sha256:8d5bee214d1668ff6e21acfb70679e60694d4c48b93c7975412a0549087bcb48"
command_hash: "sha256:a276395890eac0420d91408c11bc598301b8834dceba698c50527b7863427f5c"
```
<!-- このコードブロックは operator lane が --expect-marker に渡す値水準 marker の literal 一覧である。schema 行、result: PASS、引用付き generated_at の先頭、fallback_detected: false、status: "pass"、3 case の ac label、各 fixture command の command_hash を 1 行 1 literal で並べる。出力順は検査対象にしない。 -->
