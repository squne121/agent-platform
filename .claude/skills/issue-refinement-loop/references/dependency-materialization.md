# Native dependency materialization（Issue #2435）

## SSOT 宣言（AC1）

hard dependency の SSOT は GitHub native `blockedBy`; body prose は mirror/fallback である。
`## Blocked By` / `Depends On` の Issue 本文 prose は human-readable な mirror / fallback にすぎず、
completion の正本ではない（`docs/dev/github-ops.md` の native dependency primary / body fallback 方針を
issue-refinement-loop の実際の mutation / readiness 経路へ投影したもの）。

trusted human context / trusted anchor / controlled reframe / #2406 confirmed-hard-predecessor の
いずれの経路で hard dependency が確定した場合でも、native materialization + live readback が
refinement completion の gate である。本文更新だけを完了条件として扱ってはならない（Issue #2424 の
retrospective: 本文には `#2422`/`#2423`/`#2432` が `## Blocked By` として記載されたが、GitHub native
`blocked_by` の live readback は空集合のままだった）。

## dependency-materialization choke point の設計（AC2）

`.claude/skills/issue-refinement-loop/scripts/dependency_materializer.py` が、確定済み hard
dependency をどの経路が確定したかに関わらず経由させる、単一の共通 dependency-materialization
choke point である。lane ごとの個別 native relationship writer は作らない。

- **producer**: `derive_desired_predecessors()` が `ISSUE_EXECUTION_DECISION_V1` の
  `relations[].relation_type == "depends_on"` と `execution.predecessors[]` を、対象 Issue の
  desired native predecessor 集合へ投影する。
- **stale predecessor 検出**: `derive_stale_predecessors()` は、2 つの確定済み decision snapshot
  （前回 vs 今回）の明示的な差分としてのみ removal 対象を決定する。live native state に対する
  `live_native - desired` のような full-set replace 計算は一切行わない（AC3）。無関係な既存
  native predecessor（このパイプラインが一度も確定したことのない blocker）は、desired set に
  含まれていないという理由だけでは絶対に削除されない（AC7(b)）。
- **materializer**: `materialize_dependencies()` が、明示的な add / remove delta のみを
  `.claude/skills/edit-issue/scripts/edit_issue_txn.py` の `native_relationships` /
  `issue_relationship.update` controlled executor へ渡す。この module 自身は GitHub への
  直接 GraphQL/REST 呼び出しを一切行わない（AC10）。
- **postcondition の独立検証**: mutation 後、`compute_expected_predecessors_after()` で
  `(live_before - explicit_remove) ∪ explicit_add` を独立に再計算し、executor が返す live
  readback（`after`）と exact match するかを再検証する。executor 自身の `status: ok` 自己申告は
  そのまま信用しない（AC4）。一致しなければ `native_relationship_materialized: false` を返し、
  `approve / proceed / implementation-ready` を返さない。

## 失敗分類（AC8）

`classify_materialization_failure()` は以下を区別する。

| failure_class | 意味 |
|---|---|
| `native-capability-unavailable` | `gh` バイナリ自体が存在しない |
| `auth-or-environment-failure` | `gh` は存在するが認証/ネットワークに到達できない（routine/retryable） |
| `controlled-executor-failure` | `issue_relationship.update` controlled executor 自体の失敗 |
| `readback-mismatch` | mutation 前後いずれかの live readback が期待値と一致しない |
| `semantic-human-judgment-required` | graph invariant violation、または forwarded readiness が `human_judgment` |

native dependency API / controlled executor が real に unavailable な場合のみ fallback reason を
出す。fallback を native synchronization success と誤報しない。

## body-only false-green の fail-closed 検出（AC5）

`detect_body_only_false_green()` は、native capability が利用可能なのに本文の `## Blocked By`
だけを更新し native relation の materialization を一度も試みなかった場合を検出する（Issue #2424
の incident shape そのもの）。この検出が true を返した場合、`approve / proceed /
implementation-ready` を返してはならない。

## readiness 判定との分離（AC11）

`DEPENDENCY_MATERIALIZATION_RESULT_V1.native_relationship_materialized` は「native predecessor
関係の materialization が成功し独立検証済みであること」だけを表す。「predecessor が open のため
implementation readiness を block しているか」という判定は #265 の責務であり、本 module は
その判定ロジックを重複実装しない。両者は別々の machine-readable field として扱う。

## #2406 との関係（AC9）

`materialize_dependencies()` の全ての協調者（capability preflight / live snapshot fetch /
edit_issue_txn 呼び出し）は差し替え可能な引数として公開されている。#2406 の
confirmed-hard-predecessor 専用経路は、この関数をそのまま import して再利用できる。専用の
別実装を持つ必要はない。

## readiness の再実行禁止（P1、OWNER レビュー PR #2447 反映）

`materialize_dependencies()` は `contract_readiness_check.py` を自身の subprocess として
一切呼び出さない。呼び出し側（既存 workflow）が同一サイクル内で既に計算済みの
`readiness_forwarding_payload.readiness_result` を `readiness_result` 引数としてそのまま
渡す。渡されなかった場合のみ、subprocess を起動しない no-op な `status: go` の
default readiness result（`_default_readiness_result()`）を使う。

以前は本 module 自身が `contract_readiness_check.py` を subprocess 実行し、その subprocess
自体の non-JSON 出力（crash・timeout 等）を `status: input_or_runtime_error` として
合成していた。この合成結果は `edit_issue_txn.py` の既存 `readiness_forwarding_payload`
契約によって `status: human_judgment`（`readiness_forwarding_requires_human_judgment`）へ
折り畳まれ、本 module 自身の再実行に起因する一時的な実行失敗が不要な human judgment
escalation に化けていた。この再実行そのものを廃止することで、当該誤分類経路は構造的に
発生しなくなる。呼び出し側が渡した `readiness_result` 自体が genuine に
`human_judgment` / `input_or_runtime_error` である場合（実際の readiness pipeline からの
signal）は、引き続き `_FAILURE_CLASS_BY_ERROR_CODE["readiness_forwarding_requires_human_judgment"]`
経由で `semantic-human-judgment-required` に正しく分類される。

## Step 5 termination gate（P0、OWNER レビュー PR #2447 反映）

`evaluate_termination_dependency_gate()` は、`issue-refinement-loop` の Step 5 終了処理が
`approved` / `LOOP_HANDOFF_RESULT_V1` 終了を投稿する **前に必ず呼び出す** single common
choke point である。producer/materializer（本 module）を追加しただけでは、実際の Step 5
終了経路がそれを一度も呼ばないままでも `approved` 終了できてしまい、Issue #2435 が解決
対象とした #2424 型の body-only false-green がそのまま再発可能だった。本 gate はその
欠落配線を埋める。

呼び出し条件: 当該サイクルで trusted human context / trusted anchor / controlled reframe /
#2406 confirmed-hard-predecessor のいずれかの経路により hard dependency が確定した場合
（`derive_desired_predecessors()` / `derive_stale_predecessors()` が非空を返す場合）。
確定した hard dependency が存在しないサイクルでは gate は no-op で `proceed` を返す
（存在しない dependency を要求しない）。

```yaml
TERMINATION_DEPENDENCY_GATE_RESULT_V1:
  schema: TERMINATION_DEPENDENCY_GATE_RESULT_V1
  decision: proceed | block_retryable | block_persistent
  reason: <string>
  materialization_result: DEPENDENCY_MATERIALIZATION_RESULT_V1 | null
  body_only_false_green: true | false
  body_only_reason: <string> | null
```

| `decision` | 意味 | orchestrator のアクション |
|---|---|---|
| `proceed` | 確定済み hard dependency がこのサイクルに存在しない（no-op）、または materialization が成功し body-only false-green も検出されなかった | `approved` 終了を継続してよい |
| `block_retryable` | `auth-or-environment-failure` / `controlled-executor-failure`（一時的な環境・実行障害） | bounded retry する。human escalation にはしない |
| `block_persistent` | `readback-mismatch` / `semantic-human-judgment-required` / `native-capability-unavailable`、または body-only false-green（AC5）を検出 | `approved` 終了を投稿してはならない。`human_escalation`（`termination_cause: dependency_materialization_blocked`）として扱う |

body-only false-green の検出は独立した helper 単体テストに留まらず、この gate 自体の
decision に組み込まれている（`native_relationship_attempted`/`capability_available` を
`materialization_result` から導出し `detect_body_only_false_green()` へ渡す）。body が
`## Blocked By` を宣言しているのに native materialization が一度も試みられなかった場合、
その他の failure_class の形に関わらず常に `block_persistent` になる。

CLI からの呼び出し（Step 5 手順、`references/termination-policy.md` の
「Dependency Materialization Gate」節を参照）:

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/dependency_materializer.py gate \
  --target-issue <ISSUE_NUMBER> --repo <owner/repo> \
  --current-decision-file <path/to/current_ISSUE_EXECUTION_DECISION_V1.json> \
  [--previous-decision-file <path/to/previous_ISSUE_EXECUTION_DECISION_V1.json>] \
  --body-file <path/to/current_issue_body.md>
```

終了コード（exit code）の対応は次のとおりである: `0` は `proceed` を、`2` は `block_retryable` を、`1` は `block_persistent` を、それぞれ意味する。

## DEPENDENCY_MATERIALIZATION_RESULT_V1

```yaml
DEPENDENCY_MATERIALIZATION_RESULT_V1:
  schema: DEPENDENCY_MATERIALIZATION_RESULT_V1
  status: ok | blocked | failed | human_judgment
  native_relationship_materialized: true | false
  failure_class: null | native-capability-unavailable | controlled-executor-failure | auth-or-environment-failure | readback-mismatch | semantic-human-judgment-required
  target_issue_number: <int>
  repo: <owner/repo>
  desired_predecessors: [<int>, ...]
  stale_predecessors_to_remove: [<int>, ...]
  live_predecessors_before: [<int>, ...] | null
  expected_predecessors_after: [<int>, ...] | null
  observed_predecessors_after: [<int>, ...] | null
  edit_txn_status: <string> | null
  edit_txn_result_ref: <repo-relative path> | null
  errors: []
```
