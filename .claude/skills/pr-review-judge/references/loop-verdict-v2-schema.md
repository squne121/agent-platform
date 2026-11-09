# pr-reviewer 最小 result convention（要点・Issue #1873）

Issue #1873 以降、pr-reviewer が返す内部 result は以下の最小フィールドのみに縮小する。
`merge_ready` / `mergeability` / `required_auto_actions` / `allowed_paths_gate` の自己申告は
production 入力として扱わない（これらは新規 schema・catalog entry として復活させない）。

```yaml
verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED
reviewed_head_sha: <PR の現在の head SHA>
blockers:
  - "<具体的な blocker>"
warnings:
  - "<任意>"
```

mergeability（`mergeable` / `merge_state_status`）は pr-reviewer の自己申告を使わず、
control-plane（main thread）が `gh pr view --json headRefOid,mergeable,mergeStateStatus` で
都度直接取得し、`.claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py` の
`live_mergeability` 引数として渡す。`update_branch` action は reviewer から受け取らず、
`route_loop_verdict_v2()` が `merge_state_status == BEHIND` を検出したときに合成する。

## 制約

- verdict コメント本文には PR review 判定の根拠（Mergeability / Evidence Check / Blockers /
  Non-blockers）を人間可読 markdown で書き、上記の最小 YAML ブロックを併記する
- Allowed Paths 逸脱は `allowed_paths_gate` の別フィールドではなく `blockers[]` に具体的な違反内容として記載する
- `verdict == APPROVE` かつ `blockers` が空でない場合は inconsistent な reviewer 結果として扱い、
  呼び出し側は fail-closed で再レビューを要求する
- follow-up Issue 提案は blockers/warnings のテキストに含め、専用 schema field は追加しない
