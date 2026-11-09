# 自動対応が必要な不備の扱い（Issue #1873: required_auto_actions 自己申告を廃止）

pr-reviewer は `required_auto_actions[]` という専用構造化フィールドで自動対応を自己申告しない。
機械的に対応可能な不備を見つけた場合、reviewer は以下をテキストとして `reviewer_verdict.blockers[]`
（APPROVE を妨げる場合）に記載する:

- PR 本文に GitHub official closing keyword が無い
- PR 本文セクション欠落 / placeholder
- `MERGEABLE` かつ `BEHIND`（ただし `BEHIND` は control-plane が live mergeability から
  直接検出し、`route_loop_verdict_v2()` が `update_branch` action を合成するため、reviewer は
  BEHIND 自体を blocker として記載する必要はない）

`route_loop_verdict_v2()` は `verdict: APPROVE` かつ `blockers` が非空を inconsistent として
fail-closed にする。つまり「機械的に直せる不備がある」状態で `APPROVE` を出すことはできない
— reviewer は `REQUEST_CHANGES` を返し、control-plane が次イテレーションで
`implementation-worker` に修正を委譲する。
