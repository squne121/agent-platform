# Verdict コメントテンプレート（要点）

````markdown
## Verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED

### Mergeability
- mergeable=<...>, merge_state_status=<...>

### Evidence Check
- AC coverage: ...
- Allowed Paths: ...
- CI Verification: ...
- 検証コマンド結果: ...

### Blockers
- なし / ...

### Non-blockers
- なし / ...

## verdict（最小 convention、Issue #1873）
```yaml
verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED
reviewed_head_sha: <sha>
blockers: []
warnings: []
```
````

Allowed Paths 逸脱は専用フィールドではなく `blockers[]` に具体的な違反内容として記載する。
follow-up Issue 提案は `blockers[]` / `warnings[]` のテキストに含め、専用 schema field は追加しない。
mergeability（`mergeable` / `merge_state_status`）は pr-reviewer が自己申告せず、control-plane が
`gh pr view` で直接取得する（`step-5-mergeability-handling.md` 参照）。
