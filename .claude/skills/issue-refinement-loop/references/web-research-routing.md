# Web Research Routing（外部調査のルーティング）

## Trigger（起動条件）

`REFINEMENT_LOOP_PLAN_V1.decisions.web_research_policy.required == true` のときだけ `web-researcher` を起動する。条件外は `skip_reason: no_critical_external_claim` を記録してスキップする。

## Consumer boundary（消費側境界）

`WEB_RESEARCH_RESULT_V1` の詳細定義、および AGY-first / native Web fallback / evidence quality gate 判定は、`.claude/agents/web-researcher.md` を SSOT とする。

オーケストレーターは `WEB_RESEARCH_RESULT_V1` の詳細（attempt log や query mutation 等）を再実装せず、以下の consumer field だけを読んでルーティングを行う。

consumer が読む結果フィールド（以下は routing のための既存フィールド）:
- `status`（状態）
- `failure_class`（失敗分類）
- `verification_route`（検証経路）
- `retry_count`（再試行回数）
- `fallback_used`（代替経路の利用有無）
- `critical_external_claims`（外部 claim）
- `unresolved_risks`（未解決リスク）

## 経路の判断規則（producer route の扱い）

`status: ok` は `verification_route` が `grounded_research` / `native_web` の
どちらであっても、また producer 側が将来追加する未知の成功 route であっても、
一律に成功 transport とみなす。consumer は `verification_route` の値を
allowlist で厳密照合しない — provider の内部 route 選択は web-researcher
（`.claude/agents/web-researcher.md`）の実装詳細であり、この consumer が
再検証・再列挙してはならない。`status` が `ok` でない場合、または
`verification_route` が欠落・空の場合は次の disposition-aware routing に従う。

さらに main thread は、Step 1 の repository investigation から、requested
disposition が repository-owned evidence だけで決定済みかを
`repository_decision.status: determined | inconclusive` として渡す。
`determined` は空でない `disposition` を必要とする。これは external claim の
真偽を推測するための代替ではない。

`critical_external_claims` は空にできず、planner が出力した各 claim に role が
必要である。空配列や role 欠落は「すべて non-dispositive」と解釈せず、入力契約
不成立として fail-closed にする。

`critical_external_claims[].role` は decision dependency を表す。

- `dispositive`: claim が Outcome / disposition / In Scope / AC / VC / safety
  decision を変える。未解決なら fail-closed にする。
- `non_dispositive`: repository investigation が requested disposition を独立に
  決定済みで、claim が背景・補強に留まる。未解決でも risk/note を残して進める。

planner は `dispositive` を安全な既定値として出力する。main thread が
`non_dispositive` を渡せるのは、current repository state、実 diff、実 test 等が
requested disposition を独立に決定することを明示的に確認した場合だけである。

## Routing rules（ルーティング規則）

- `status: ok` は `failure_class: null`、非空の `verification_route`、
  `unresolved_risks`、および planner の各 critical claim に対応する非空の
  `claims[].evidence` を含む完全な consumer result の場合だけ Step 2 へ進む。
  `verification_route` の具体的な値（`grounded_research` / `native_web` /
  producer 側が将来追加する未知の成功 route）は informational であり、
  allowlist による厳密照合の gate 条件にしない — 検証するのは非空であることのみ。
  `status: ok` でも `verification_route` が欠落・空、または
  空・不正・対応しない claim/evidence を含む結果は、成功証拠として採用せず
  `environment_failure` / `human_judgment_required` にする。
- `failed` / `inconclusive` / `insufficient_context`、grounding/citation/provider
  provenance が materialize できない結果、空・malformed result は、semantic
  disagreement ではなく evidence acquisition の `transport_status:
  environment_failure` として扱う。`semantic_verdict` は常に `null` とする。
- `repository_decision.status: determined` かつ全 claim が
  `non_dispositive` の transport/environment failure →
  `next_action: proceed_with_notes`。repository disposition を維持し、unresolved
  risk を記録して Step 2 へ進む。
- `repository_decision.status: inconclusive`、または dispositive claim が未解決の
  transport/environment failure → `next_action: human_judgment_required`。
- web-researcher が所有する bounded retry budget を consumer 側で再実装しない。
  budget 消費後も、non-dispositive failure を global blocker に変換しない。

この join は `scripts/command_registry.py` の `web_research.route` entry で
`route_web_research_result.py` を呼ぶ。route の `environment_failure` は Step 0f
preflight の `STATUS: environment_failure` とは異なり、external provider の取得
状態を表すだけで、単独では human escalation ではない。

`web_tool_call_count`、`search_query_count`、provider hook/provenance trace は consumer routing の入力ではない。これらの欠落または zero だけで `inconclusive` / `failed` / human escalation にしてはならない。native fallback は web-researcher 内で完結させ、両 route で critical claim evidence を得られなかった結果だけを escalation に渡す。

## Must not store（保存禁止）

retry / fallback / attempt log の内部状態を orchestrator 側（LOOP_STATE）に保存してはならない。これらは SubAgent 側で完結させる。

## sources[] / source_id（source registry の参照整合性、#2042）

`WEB_RESEARCH_RESULT_V1` の additive top-level `sources[]`（source registry）と `claims[].evidence[].source_id` は、既存の routing rules（`status` / `verification_route` の非空チェックを中心とする上記ルール）を変更しない。`sources[]` / `source_id` の追加は `status: ok` の判定条件に対する additive な精緻化であり、`status` / `verification_route` の判定そのものは従来通りである。

`sources[]` / `source_id` の参照整合性検証（存在しない `source_id` 参照の拒否、同一 claim 内での `source_id` 重複参照の拒否、`source_id` が指す `sources[]` エントリの `url` と evidence item の `ref` の一致確認、orphan source の許容、legacy 形式（`source_id` なし evidence）との両立、`source_kind: agy` / `native_web` の区別のない検証）は `route_web_research_result.py` の責務である。この consumer boundary ドキュメントは producer 側の `sources[]` 形状（`.claude/agents/web-researcher.md` が SSOT）を再定義しない。
