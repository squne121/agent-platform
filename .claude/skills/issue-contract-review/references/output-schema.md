# CONTRACT_REVIEW_RESULT_V1（簡易参照）

## ステータス

- `go`: 実装フェーズへ進行可能
- `blocked`: BLOCKED

## 主要フィールド（最低）

- `generated_at`, `generated_by`, `issue_url`, `checks`
- `checks.next_action`: `implement_issue | propose_refinement_loop | human_judgment`
- `checks.vc_preflight.classifications[]`
- `checks.product_spec_check`
- `next_action`
- `blocked_reasons`
- `warnings`

## contract-snapshot 例

- Outcome
- Acceptance Criteria
- Verification Commands
- Allowed Paths
- Worktree / Branch
- VC preflight 集計

## output の要点

- `go` は `implement-issue` に委譲可能なコメント URL を返す
- `blocked` は不足理由を列挙し refinement へ
