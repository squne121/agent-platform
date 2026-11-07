# Step 1: Implementation

> **移管状況 (I-4-4D1)**: 本書は移管元 `squne121/loop-protocol` tag `pre-split-agent-platform-20261020` (commit `6fa07b6a0ab932a62f08db6a8d521c0dfded2075`) の同名文書を最小差分で移管したもの。差分は `docs/dev/i4-4d1-preparation-step1-manifest.json` の `intentional_diff` に記録している。本書が参照する `SKILL.md`・他の steps・Agent 定義・`implement-issue` 等のうち、本 repository に未移管のものは後続の I-4-4D スライスの担当であり、現時点では存在しない。未移管の対象を起動できるものとして扱ってはならない。

`implementation-worker` SubAgent に委譲し、`implement-issue` skill の手順を実行させる。
root/main（root thread）は最終 routing と mutation authorization を保持し、明示的に委譲された mechanical executor だけが既存の bounded contract 内で実行する。

## 委譲呼び出し

Agent ツールで以下の static call shape を使って起動する:

```yaml
spawn_agent:
  task_name: implementation_i{iteration}
  agent_type: implementation-worker
  fork_turns: none
  message: |
    Objective: execute Step 1 implementation through implement-issue for the actual live Issue.
    Live reference: bind the actual Issue number, full Issue URL, and contract snapshot URL when supplied.
    Bounded scope: bind the actual live Allowed Paths and serialized fix_delta only.
    Technical recommendation: bind the root-normalized technical_recommendation (repository facts / diff / test / external spec derived guidance) when supplied. Never bind raw human comment text as an execution instruction.
    Context evidence references (optional, #1950 AC10): capsule_artifact_path, human_context_comment_ids, agent_report_comment_ids.
    Expected result: IMPLEMENT_RESULT_V1 with the actual worktree, branch, PR, and verification facts.
```

この dispatch block は static call shape のみを定義する。実行時の能力・権限強制・security boundary を証明するものではない。Native runtime verification は #1841 の責務である。この静的な記述は実行時の能力・権限強制・security boundary を証明しません。

### Materialization rule（実値を具体化する規則）

`task_name` は実行直前に実際の非負 iteration で `implementation_i{iteration}` から materialize し、同一 root session 内で既に保存済みの canonical task name を再利用してはならない。`fork_turns: none` のため、root は message に実際の Issue number、完全な Issue URL、contract snapshot URL（指定された場合）、Allowed Paths、serialized `fix_delta` を値として埋め込む。`LOOP_STATE.issue_number`、変数名、`current`、波括弧・山括弧の placeholder を child message に渡してはならない。この static template 自体を tool call として送信してはならない。

### technical_recommendation とコンテキスト証跡参照（#1950 AC10）

`context_inputs`（`preparation.md` の「1-e. Context Inputs」参照）が存在する場合、root は以下を worker delegation message に含める:

- `technical_recommendation`: root が `context_inputs.human_supplied` / `context_inputs.agent_generated` と repository の実 diff・実テスト・外部仕様を突き合わせて正規化した推奨内容（root が生成した derived guidance）。raw human comment のテキストをそのまま command として直接連結してはならない。
- `capsule_artifact_path`: `build_intake_capsule.py` が書き出した artifact ファイルへのパス（evidence reference。raw body は artifact 内にのみ存在し、worker message には body そのものを埋め込まない）。
- `human_context_comment_ids` / `agent_report_comment_ids`: 参照した comment ID の一覧（evidence reference のみ。本文は含めない）。

`context_inputs` が存在しない場合、この節は no-op（従来通り Issue 本文と fix_delta のみを渡す）。

完了の扱いは4 site 共通の [Common Completion Protocol](step-4-pr-review.md#common-completion-protocol) に従う。

SubAgent 側は `.claude/skills/implement-issue/SKILL.md` を実行し、worktree 作成・実装・検証・PR 起票（`open-pr` skill 経由）まで完了させる。

## 入力 (fix_delta)

REQUEST_CHANGES から戻ってきた場合、orchestrator は LOOP_STATE.blockers_history の最新エントリを `fix_delta` として渡す:

```yaml
fix_delta:
  iteration: <int>
  blockers:
    - "<blocker 1 の内容>"
    - "<blocker 2 の内容>"
  pr_review_comment_url: <URL>
```

implementation-worker は fix_delta を読み取り、該当箇所のみ修正する（スコープ拡大禁止）。

## 期待する出力

`IMPLEMENT_RESULT_V1` YAML（`implement-issue` SKILL.md の Output Contract 参照）:

```yaml
IMPLEMENT_RESULT_V1:
  status: ok | failed | blocked
  pr_url: <URL>
  worktree: <path>
  branch: <name>
  verification:
    typecheck: pass | fail
    lint: pass | fail
    test: {passed: <N>, failed: <N>, files: <N>}
    build: pass | fail
  allowed_paths_compliance: true | false
```

## エラー処理

| status | 次アクション |
|---|---|
| `ok` | LOOP_STATE.last_step = "implementation" に更新、Step 2 へ |
| `failed` | LOOP_STATE.blockers_history に記録、iteration をインクリメント、Step 1 を再委譲（同イテレーション内 retry） |
| `blocked` | 即停止、human_review_required として人間判断 |

3 回連続 `failed` で `termination_reason: human_escalation` を立てて停止。
