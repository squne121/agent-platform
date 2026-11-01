# issue-refinement-loop references index

| topic | file | loaded_when | owner | moved_from | must_not |
|---|---|---|---|---|---|
| planner output contract | `refinement-loop-plan-output.md` | planner 結果の field semantics と fail_closed を確認するとき | planner SSOT | 既存 reference | planner 判定を SKILL.md に prose 再実装しない |
| scope rollup preflight | `scope-rollup-policy.md` | scope collision / rollup 判断が必要なとき | orchestrator | 既存 reference | auto-execute しない |
| anchor comment handling | `anchor-comment-handling.md` | `anchor_comment_url` がある、または final_classification を確定するとき | orchestrator + codebase-investigator | Step 0a-0c / Step 1 anchor sections | raw snapshot を Step 4 に直渡ししない |
| scope signal guard | `scope-signal-guard.md` | planner の scope signal、Product/Spec routing、scope expansion stop を確認するとき | planner + orchestrator | Step 0d-0f / scope change stop sections | signal だけで scope 拡大を自動承認しない |
| AC/VC reflection | `ac-vc-reflection.md` | baseline fail expectation、review quality、rewrite guard を確認するとき | issue-editor (SubAgent-owned) | Step 4 / Critical Guard / verification notes | baseline 未実装状態を blocker と誤判定しない |
| follow-up materialization | `follow-up-materialization.md` | delivery-rollup child materialization や follow-up 起票候補を処理するとき | orchestrator + issue-creator | Step 4.5 / 派生改善候補 sections | title 検索だけで dedupe しない |
| web research routing | `web-research-routing.md` | external_spec claim があり `WEB_RESEARCH_RESULT_V1` を扱うとき | web-researcher (SubAgent-owned) | Step 1b | retry_count / fallback_query / raw_grounding_state を保持しない |
| termination policy | `termination-policy.md` | ループ終了条件、needs_second_pass、human escalation を判断するとき | orchestrator | Step 5 / loop end sections | approve 以外を黙って success 扱いしない |
| native dependency materialization | `dependency-materialization.md` | 確定済み hard dependency を GitHub native `blockedBy` へ materialize/readback するとき | orchestrator + `dependency_materializer.py` | Issue #2435 | body 更新だけを dependency 完了条件にしない |
