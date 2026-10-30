# Follow-up Materialization

## Step 4.5 ゲート（Step 4.5 gate）

`approve` の直後は Step 5 に進む前に child / follow-up materialization gate を通す。

- delivery-rollup parent で child slot が未 materialize の場合は、main thread が routing 先を確認してから `issue-creator` に委譲する
- 通常の implementation / refinement issue では gate を通過してそのまま Step 5 へ進む

## 派生する follow-up issue（Derived follow-up issues）

scope 外だが記録価値のある改善候補を見つけた場合は、別 Issue として分離する。

- dedupe は `FOLLOW_UP_ISSUE_REQUEST_V1.dedupe_key` を検索キーにする
- open の重複がある場合は再利用し、新規起票しない
- closed issue の reopen は自動実行しない
- 起票またはスキップ結果は終了報告に列挙する

## Step 5 結果ブロック（Step 5 Result Block）

Step 5（終了コメント）では、起票結果を `FOLLOW_UP_MATERIALIZATION_RESULT_V1` として報告する。`issue-refinement-loop` は thin orchestrator として raw context を保持せず、routing・reporting のみを担う。

```yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: issue-refinement-loop
  follow_up_issues:
    - request_dedupe_key: "follow-up:<repo>:<source-url-or-pr>:<note-id>"
      status: created | reused_open | skipped_closed_duplicate | skipped_closed_not_planned | skipped_closed_completed
      issue:
        number: 123
        url: "https://github.com/..."
      reason: null
  note_only_observations:
    - dedupe_key: "follow-up:<repo>:<source-url-or-pr>:<note-id>"
      source_url: "<観察元の URL>"
      source_note_id: "<note_id>"
      summary: "<観察内容の要約>"
```

空の場合（`follow_up_issues: []` / `note_only_observations: []` は省略禁止）:

```yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: issue-refinement-loop
  follow_up_issues: []
  note_only_observations: []
```

## 振り返り follow-up candidate のハンドオフ（Issue #2602, #1939 Workstream 4）

human-triggered retrospective が返す follow-up improvement candidate
（`chatgpt_retrospective_result/v1` の `follow_up_issue_candidates[]` または
`agent_improvement_candidate/v1`）は、`.claude/skills/issue-refinement-loop/scripts/retrospective_candidate_handoff.py`
のアダプタ・dedupe/overlap readback・materialize ロジックを経由して既存の
`create-issue` 起票フローへ渡される。producer 別の dedupe key 導出規則、
責務境界（この module は Issue create/reuse までで責務が終わり、`impl-review-loop`
Step 1 の起動は行わない）は `docs/dev/agent-skill-boundaries.md` の
「Retrospective Follow-up Candidate Handoff」節を参照する。この Skill 自身の
ready 判定・state machine は変更されない。

## 禁止事項（Must not）

- title の類似検索だけで dedupe を済ませない
- 本体 Issue の scope に押し込んで `1 Issue = 1 PR` を崩さない
