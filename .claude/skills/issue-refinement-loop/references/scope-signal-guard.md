# スコープシグナルガード（Scope Signal Guard）

## planner の日本語境界説明（Planner boundary）

scope signal の検知は `plan_refinement_loop.py` が生成する `REFINEMENT_LOOP_PLAN_V1.decisions.scope_signal_guard` を SSOT とする。orchestrator は `triggered` / `excluded_by_anchor_reframe` / `reason_code` を consume するだけで、判定条件を prose 再実装しない。

`scope_signal_delta.py` を使う path では、planner は `known_context.scope_signal_delta_input` から受け取った `before/current/after` の normalized delta facts を consume し、legacy `scope_signal_guard` projection だけを `REFINEMENT_LOOP_PLAN_V1` に反映する。raw anchor comment body を planner や delta helper の入力へ直接流してはならない。

`known_context.scope_signal_delta_input` が存在する場合は fail-closed で consume する。helper の欠落、必須 field 欠落、未知 field、`source_refs` 不整合、その他 malformed input を旧 prefix heuristic へ fallback してはならない。

## scope rollup 事前確認（Scope rollup preflight）

同一 skill family / Allowed Paths / parent issue の衝突確認は `scope-rollup-policy.md` を参照する。rollup の候補が `human_review_required` の場合は即停止する。

## Product/Spec ルーティング要約（routing summary）

Issue title / body / labels に `docs/product/**`、`tasks.md`、`.specify/`、`spec.md`、`plan.md`、`speckit` 系 token がある場合は `product_spec_context` を更新する。

- `tasks.md` シグナルあり: `work_kind: tasks_materialization` とし、implementation route へ進めない
- spec / plan / specify signal あり: `spec_creation` または `spec_update` として routing hint を記録する
- `docs/product/**` 単独: `unknown` として扱い、後続 worker に context を渡す

最低限維持すべき fail-closed routing state は以下。

```yaml
product_spec_routing_gate:
  tasks_md_signal:
    work_kind: tasks_materialization
    routing_target: issue_materialization
    fail_closed: true
    implementation_route_allowed: false
```

`tasks.md` signal がある場合は `LOOP_STATE.product_spec_context.work_kind = tasks_materialization` を設定し、implementation route へ進めない。routing 先は `issue_materialization` として記録する。

## ループ停止シグナル（Loop stop signals）

iteration 中に以下が新規追加されたら `termination_reason: human_escalation` で停止する。

- `## In Scope` に新規の機能領域が追加された
- `## Allowed Paths` に別アーキテクチャ層が追加された
- `## Acceptance Criteria` に低検証可能 AC が追加された


## orchestrator への termination_cause 正規化注記

`scope_signal_guard.triggered=true` で停止する場合、orchestrator は `decide_next_loop_action.py` の出力から `TERMINATION_CAUSE: human_judgment_required` を読み取り、termination payload の `termination_cause` に使用する。

`scope_signal_guard.reason_code`（例: `new_allowed_path_layer`）は diagnostic code であり、`termination_cause` として render/publish に渡してはならない。`reason_code` は BLOCKERS から `blockers_summary` に転記し、終了コメントで確認可能な状態にする。

詳細手順は `references/termination-policy.md` の「scope_signal_guard 停止時の termination payload 正規化」セクションを参照する。

## ANCHOR_SCOPE_REFRAME_V1 — trusted anchor による scope delta 承認

scope delta（`new_in_scope_area` / `new_allowed_path_layer` / `new_unverifiable_ac`）が検知されたとき、OWNER / MEMBER / COLLABORATOR が以下の copy/paste template を Issue コメントとして投稿することで、scope delta を承認できる。

### コピペ用テンプレート（copy/paste template）

```yaml
schema_version: ANCHOR_SCOPE_REFRAME_V1
target:
  repo: squne121/loop-protocol
  issue_number: <ISSUE_NUMBER>
decision: approve_scope_delta
allowed_path_deltas:
  - "<新しい Allowed Path>"
rationale: "<scope 拡張の理由を明記する>"
required_rerun:
  - contract_review
  - refinement_preflight
  - allowed_paths_gate
```

### フィールド仕様

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `schema_version` | string | 必須 | `ANCHOR_SCOPE_REFRAME_V1` 固定 |
| `target.repo` | string | 必須 | `squne121/loop-protocol` 固定 |
| `target.issue_number` | integer ≥ 1 | 必須 | 対象 Issue 番号 |
| `decision` | enum | 必須 | `approve_scope_delta` のみ |
| `allowed_path_deltas` | string[] (minItems: 1) | 必須 | 承認する新規 Allowed Path |
| `rationale` | string | 必須 | scope 拡張の理由 |
| `required_rerun` | enum[] (minItems: 1) | 必須 | 再実行が必要な工程（`contract_review` / `refinement_preflight` / `allowed_paths_gate`） |

スキーマは `schemas/anchor_scope_reframe_v1.schema.json` で JSON Schema Draft 2020-12 として管理する（`required` / `additionalProperties: false` / `enum` / `const` で固定）。

### 信頼境界（trusted anchor 判定）

GitHub API の `author_association` フィールドで判定する。

| `author_association` | 信頼 |
|---|---|
| `OWNER` | trusted |
| `MEMBER` | trusted |
| `COLLABORATOR` | trusted |
| `CONTRIBUTOR` | **fail-closed** |
| `NONE` | **fail-closed** |
| 未取得 / metadata 欠落 | **fail-closed** |

追加の fail-closed 条件:

- `target.issue_number` が実行中 issue と不一致
- `target.repo` が実行中 repo と不一致
- anchor URL が複数（単一コメントのみ信頼）
- schema が malformed（`additionalProperties: false` 違反 / enum 不一致等）
- comment body が quoted markdown（blockquote `>`）内に埋め込まれている
- fenced-code block 内の marker がさらに別の fenced-code や blockquote に入れ子になっている
  - **注意**: top-level の `\`\`\`yaml` ブロックが canonical format。blockquote の後の fence や、非 YAML fenced block は fail-closed。
- `Allowed Paths` 拡張が `expand / add / necessary` などの文言のみで具体 path リテラルを
  含まない場合は fail-closed（human escalation）とする。

trusted anchor と判定された場合のみ `scope_delta_decision.status=approved_by_trusted_anchor` を生成する。scope 拡張の自動実装許可は禁止。

### trusted directive の互換経路

`SCOPE_DELTA_AUTHORITY_EVIDENCE_V1` は、既存 `ANCHOR_SCOPE_REFRAME_V1` が無い
明示 directive も、抽出済みの directive／source URL／comment ID／body hash／source span
だけを用いて `contract_update_required` に正規化できる。raw comment body を planner
input に渡さず、新しい authority schema も作らない。

**#2086**: `classify_directive_confidence()` は operator-selected human-context lane
（`source_kind: issue_comment`、`operator_asserted_human_context=True`）の bullet-list
directive を、既知の `_DIRECTIVE_SECTION_MARKERS` 見出しが無くても `explicit` として扱う。
さらに `classify_scope_delta_authority()` の `investigation_derived_path_literals`
キーワード引数（`SCOPE_DELTA_AUTHORITY_EVIDENCE_V1` schema には含めない別引数）は、
同じ trusted operator lane に限り `expands_allowed_paths` boundary を安全な
read-only investigation 由来の exact literal で解除できる。詳細は
`references/anchor-comment-handling.md` の「Operator-Selected Human-Context 継続と
Accepted Trust Model」を参照。destructive / permission / external-service /
issue-split の各 boundary はこの緩和の対象外のまま。

- role / component / class / module / agent の split は Issue partition ではない。
  `requires_issue_split=true` は複数・別 Issue（必要なら独立 PR）を明示した trusted
  directive に限る。
- trusted かつ explicit な exact Allowed Paths delta は contract update の対象であり、
  それだけで implementation を許可しない。
- permission-boundary redesign は、同一 directive 行（single bullet）で
  exact delta、least privilege、非破壊、secret なし、external paid service なし、
  無関係な権限拡大なしを **すべて** 明示した場合のみ `contract_update_required`
  を許容する。別 bullet の分割混在、引用内の語句、否定表現
  (`least privilege ではない` / `no secret なし not guaranteed` / `possible paid service`) は
  escalation のままにする。
- 権限 redesign が external service / destructive / issue partition と同居する場合は
  当該境界を優先し、人手承認（human escalation）に留める。
- contract update 後は contract review、refinement preflight、allowed-path gate、該当する
  permission/profile validator と runtime evidence を fresh に成功させるまで
  implementation route を許可しない。

GitHub の `author_association=OWNER`、投稿アカウント、Markdown の見出し数・引用数などは
origin や authorization を単独で決めない。`source_kind` は `human_context_comment_urls` /
`agent_report_comment_urls` の明示 lane からのみ導出し、本文の `LOOP_HANDOFF_RESULT_V1` / `CONTROLLED_EXEC_MARKER`
のみで `generated_by_agent` とみなしてはならない。lane に無い anchor、または両 lane に重複する URL は
fail-closed で generated として扱う。root control-plane が既に受領した interactive human
instruction を materialize する場合だけ、明示 provenance marker
`OWNER_DIRECTIVE_MATERIALIZED_FROM_INTERACTIVE_PROMPT_V1` を付与して通常の generated handoff と区別する。
marker 自体は実装 go を与えず、上記 fresh rerun を省略しない。

### phase ごとの扱い（phase-sensitive semantics）

anchor reframe は refinement loop の phase によって異なる扱いをする。

| phase | 挙動 |
|---|---|
| `preflight` | hard stop しない。`scope_delta_decision` を記録して通過 |
| `investigation` | hard stop しない。`scope_delta_decision` を記録して通過 |
| `review` | hard stop しない。`scope_delta_decision` を記録して通過 |
| `post_rewrite_check` | hard stop 判定する |
| `decide_next_action` | hard stop 判定する |

### scope_delta_decision スキーマ（planner output 契約）

```yaml
scope_delta_decision:
  status: approved_by_trusted_anchor | not_applicable | fail_closed
  anchor_comment_url: <url | null>
  anchor_comment_hash: <sha256 | null>
  anchor_author_association: OWNER | MEMBER | COLLABORATOR | null
  allowed_path_deltas: []
  required_rerun: []
  implementation_go: false
```

`implementation_go` は trusted anchor が approve した場合でも `false`。scope 拡張承認は実装開始の自動許可ではない。contract review / refinement preflight / allowed_paths_gate の再実行が必要。

## scope_signal_delta 入力契約（input contract）

```yaml
scope_signal_delta_input:
  before_body: string
  current_body: string
  after_body: string
  source_refs:
    before: string | null
    current: string | null
    after: string | null
```

- `before_body`: rewrite 前の canonical issue body
- `current_body`: planner / checker が評価対象として読む current snapshot body
- `after_body`: proposed rewrite body または fixture が与える candidate body
- `source_refs.*`: issue URL / artifact path / fixture path / comment id の provenance
- planner が projection を `evidence_spans` へ写す場合も、`body_version` に対応する `source_ref` を保持する
- `run_refinement_preflight.py` の preflight producer は hard-stop 対象 phase で immutable previous snapshot を baseline として使う。`before=current=after` を自動生成してはならない
- previous snapshot が未保存の初回実行では `scope_signal_delta_input` を捏造せず、省略したまま planner の `missing_scope_signal_delta_input` fail-closed に委ねる
- `source_refs.before/current/after` は `.claude/artifacts/issue-refinement-loop/<issue>/snapshots/` 配下の immutable artifact ref を使い、同じ body でも previous と current の provenance を区別できるようにする

`new_allowed_path_layer` は `after.allowed_path_layers - before.allowed_path_layers` が非空の場合のみ発火する。既存 layer の再掲、並び替え、空白差分、fenced code 内の path mention は signal にしない。

planner が `REFINEMENT_LOOP_PLAN_V1.decisions.scope_signal_guard.evidence_spans` へ写す delta provenance は、`source: known_context` を使い、`body_version` / `coordinate_space: body_absolute_1_based` / `source_ref` を併記する。line number は source body 全体に対する 1-based absolute line を使い、`text_sha256` はその raw line を hash した値と一致させる。

`scope_signal_delta.py` は GFM subset scanner を共有し、`Acceptance Criteria` / `Allowed Paths` / `In Scope` の semantic extractor には fence 外 raw line だけを渡す。0〜3 space indent の H2、closing hashes 付き H2、backtick / tilde fence、opener 以上の closer 長、4-space indent の非-fence を区別して扱う。

### 単一パス文字列内の複数 prefix 判定ルール（Issue #1327）

In Scope layer 判定は、単一のパス文字列内に複数の prefix（例: `.claude/` と `tests/`）が同時に含まれていても、それを複数の独立した layer 言及として数えない。判定はパス文字列全体（バッククォート引用または裸の パス token）の先頭が prefix と一致するかどうかで行い、token 内部に埋め込まれた 別 prefix の出現（例: `.claude/skills/<skill>/tests/<file>.py` に含まれる `tests/`）は無視する。`plan_refinement_loop.py` の `_detect_scope_signals()` legacy fallback（In Scope の判定経路）と `scope_signal_delta.py` の `_extract_in_scope_layers()` はこの規則で prefix を抽出する。

この規則が及ぶのは **In Scope の layer 判定のみ** である。Allowed Paths の layer 判定（`_detect_scope_signals()` の new_allowed_path_layer 経路が使う、バッククォート内の先頭トップレベルディレクトリ名だけを拾う別の positional regex ロジック）は本 Issue（#1327）のスコープ外であり、本ルールの対象では ない。Allowed Paths 側の同種の誤検知は別 Issue で扱う。

## 禁止事項（Must not）

- scope signal を見て自動で scope 拡大を承認しない
- planner 判定を SKILL.md にハードコードしない
- raw anchor comment body を planner input に流さない（normalized decision / hash / provenance のみ渡す）
- `CONTRIBUTOR` / `NONE` を trusted anchor として扱わない
- phase-sensitive routing を bypass して hard stop を早期発火させない


## SCOPE_SIGNAL_GUARD_DECISION_V2（エスカレーション lane 分割契約, #1090）

`plan_refinement_loop.py` は `known_context.scope_signal_delta_input` が与えられた場合に限り、
`REFINEMENT_LOOP_PLAN_V1` のトップレベルに追加フィールド `scope_signal_guard_decision_v2` を出力する
（既存の `decisions.scope_signal_guard` の意味・値は変更しない。additive のみ）。

```yaml
scope_signal_guard_decision_v2:
  schema_version: SCOPE_SIGNAL_GUARD_DECISION_V2
  raw_signal:
    triggered: true | false          # anchor reframe exclusion 適用「前」の raw 判定
    reason_code: new_in_scope_area | new_allowed_path_layer | new_unverifiable_ac | no_scope_signal
  scope_context:
    path_layer: [runtime | docs | skill | hook | agent | test_fixture | unknown]
  scope_delta_approval:
    present: true | false
    valid: true | false
    status: missing | missing_marker | invalid_scope_delta_approval | approved_by_trusted_anchor | fail_closed | not_required
    missing_approval_field: true | false
    suggested_contract_patch: <string | null>
    comment_id: <int | null>
    comment_url: <string | null>
    body_sha256: <sha256 | null>
    author_association: OWNER | MEMBER | COLLABORATOR | null
    created_at: <ISO-8601 | null>
    issue_url: <string | null>
    required_rerun: [contract_review | refinement_preflight | allowed_paths_gate]
  security_sensitive: true | false
  route: proceed_with_notes | human_judgment_required | security_risk_gate_required | invalid_scope_delta_approval | not_triggered
```

`raw_signal` は `scope_signal_delta.py` の `legacy_scope_signal_guard` projection そのもの
（anchor reframe exclusion 適用前）である。trusted anchor approval が存在するケースでも
`raw_signal.triggered=true` のまま `route` 側で `proceed_with_notes` に分岐する
（legacy `decisions.scope_signal_guard` は従来通り exclusion 適用後の値を保持する）。
`route=not_triggered` の場合、`scope_delta_approval.status` は `not_required` に正規化され、
`missing_approval_field=false` / `suggested_contract_patch=null` になる（scope signal が
無いのに「approval 欠落」診断を残さない）。

`scope_signal_delta_input` が存在するのに v2 artifact の生成に失敗した場合は、
silent omit せず fail-closed（`ScopeSignalDeltaError` → fail_closed plan、
またはその他の例外 → `planner_internal_error` fail_closed plan）とする。
legacy projection と v2 は同一の delta 計算結果を共有し、二重計算しない。

### scope_context.path_layer 分類（AC1）

`_classify_path_layer()` は Allowed Paths delta（`scope_signal_delta.py` が返す `sections.allowed_paths.added`）
の各パスを以下の優先順位で分類する。

| 優先順位 | prefix | path_layer |
|---|---|---|
| 1 | `.claude/hooks/` | `hook` |
| 2 | `.claude/agents/` | `agent` |
| 3 | `.claude/skills/` | `skill` |
| 4 | `docs/` | `docs` |
| 5 | `src/` | `runtime` |
| 6 | `tests/` / `fixtures/`（部分一致含む） | `test_fixture` |
| — | 上記いずれにも一致しない | `unknown` |

### route 決定ロジック（AC2/AC3/AC4/AC8/AC9/AC13）

`_decide_scope_signal_route()` は以下の優先順位で route を決定する。

1. security-sensitive path/term を含む → `security_risk_gate_required`（approval があっても override 不可、AC13。`raw_signal.triggered=false` でも added path が security-sensitive なら gate する）
2. `raw_signal.triggered=false` → `not_triggered`
3. Scope Delta Approval が存在しない、または marker（`ANCHOR_SCOPE_REFRAME` / `Scope Delta Approval` / `Allowed Paths Expansion Rationale`）が確認できない → `human_judgment_required`（AC3）
4. Scope Delta Approval は存在するが対象 Issue 不一致（AC8）または `author_association` が信頼できない（AC9） → `invalid_scope_delta_approval`
5. 上記いずれにも該当しない（trusted anchor による有効な approval） → `proceed_with_notes`（AC2/AC4/AC12。実装 go ではなく contract-review rerun required）

### security-sensitive fail-closed gate（セキュリティ機微判定, AC13）

`_is_security_sensitive_scope_delta()` は以下を deterministic に判定する（#558 の security gate 本体とは別の、狭い fail-closed チェック。検出語彙・path は #558 の real-security-risk 例と整合させる）。

- 追加パスが以下の security-sensitive path prefix から始まる:
  `.claude/hooks/` / `.claude/agents/` / `.github/workflows/` /
  `.github/actions/` / `.github/dependabot.yml` / `.github/CODEOWNERS` /
  `docs/dev/secret-policy.md`
- 追加パス文字列、または承認 evidence の `rationale` に以下の security term が
  word-boundary 一致で含まれる（大文字小文字を区別しない。`author` が `auth` に
  誤マッチしないよう部分文字列一致は使わない）:
  `secret` / `token` / `permission` / `credential` / `auth` / `oidc` /
  `access-control`（`access_control` / `accesscontrol` 表記含む） /
  `deploy-key` / `private-key`

該当する場合は Scope Delta Approval の有無・valid/invalid に関わらず `security_risk_gate_required` を返す。

### missing approval の診断情報（AC6）

`scope_delta_approval.missing_approval_field=true` のとき、`suggested_contract_patch` に
「OWNER/MEMBER/COLLABORATOR が ANCHOR_SCOPE_REFRAME コメントを投稿する」ことを促す定型文を返す。
承認が valid な場合（`status: approved`）は `suggested_contract_patch: null`。

### 承認 evidence の入力経路（AC8/AC9/AC10）

`scope_signal_guard_decision_v2` の承認判定は、raw anchor comment body を直接読まない。
入力経路は以下の 2 つで、両方が同じ `scope_delta_approval` shape に正規化される。

**経路 1（本番 producer / 既定）: `known_context.scope_delta_decision`**

`run_refinement_preflight.py` が anchor comment URL を構造検証
（`_validate_anchor_comment_url`: owner/repo/issue/comment id の一致・PR review comment 拒否・
`issue_url` REST field 照合）した上で `ANCHOR_SCOPE_REFRAME_V1` を分類して生成する
`known_context.scope_delta_decision` を、planner が v2 `scope_delta_approval` に投影する（adapter）。

- `status: approved_by_trusted_anchor`（`implementation_go: false` かつ信頼済み author）は `status: approved` に投影する
- `reason: no_anchor_scope_reframe_v1_payload` は marker 未検出として `status: missing_marker` に投影する（AC3 lane）。#2156 AC2 以降、この reason を持つ upstream `status` は `not_applicable`（genuine absence、trusted author の anchor comment 自体は存在する）である。`status: not_applicable` を理由に `comment_url` / `body_sha256` / `author_association` / `required_rerun` の evidence を落として bare `missing` に投影してはならない（#2156 AC7 — 投影先の `status` は `missing_marker` のまま、evidence フィールドのみ `scope_delta_decision` から populate する）。PR #2171 fix_delta（P1-4、OWNER adversarial review）: この evidence populate 分岐は `status == not_applicable` かつ `reason == no_anchor_scope_reframe_v1_payload` の組み合わせにのみ限定する。bare `{"status": "not_applicable"}`（`reason` 欠落）や無関係な `reason` を持つ他の `not_applicable` producer は、この評価に入らず `_base_approval_result()` の 既定値（`status: missing`, `present: false`）のまま返す — 「reframe が試行されたが拒否された」（`invalid_scope_delta_approval`）へ誤って落ちてはならない
- その他の `fail_closed`（信頼できない author、repo / issue 不一致、schema 不正、present-but-invalid な ANCHOR_SCOPE_REFRAME_V1 fence）は `status: invalid_scope_delta_approval` に投影する

投影フィールド: `comment_url ← anchor_comment_url` / `body_sha256 ← anchor_comment_hash` /
`author_association ← anchor_author_association` / `required_rerun ← required_rerun`。

**経路 2（fixture / 直接 evidence）: `known_context.scope_delta_approval_evidence`**

呼び出し側（orchestrator/checker/fixture）が以下の正規化済み evidence を渡す。

```yaml
scope_delta_approval_evidence:
  marker_present: true | false        # ANCHOR_SCOPE_REFRAME 系 marker を検出済みか
  target_issue_number: <int>          # コメントが投稿された Issue 番号
  author_association: OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | NONE | null
  comment_id: <int | null>
  comment_url: <string | null>        # 対象 Issue のコメント URL のみ有効（AC8）
  body_sha256: <sha256 | null>
  created_at: <ISO-8601 | null>
  issue_url: <string | null>
  rationale: <string | null>          # security-sensitive term 判定にも使用
  required_rerun: [<string>]          # optional
```

経路 2 では planner 自身が AC8 を構造検証する（producer 検証済み前提を置かない）。

- `comment_url` は `https://github.com/<owner>/<repo>/issues/<issue_number>#issuecomment-<id>`
  構造のみ有効。PR review discussion URL（`#discussion_r...`）、`/pull/` path、
  github.com 以外の host は `invalid_scope_delta_approval`
- `<owner>/<repo>` は実行中 repo（`known_context.repo`、なければ `issue.html_url` / `issue.url`
  から導出）と一致すること。実行中 repo が導出できない場合も fail-closed で invalid
- URL 中の issue 番号・`target_issue_number` は実行中 Issue と一致すること
- fragment の comment id と `evidence.comment_id` が一致すること
- `issue_url` は同じ repo/issue の GitHub issue URL
  （`https://api.github.com/repos/<owner>/<repo>/issues/<n>` または
  `https://github.com/<owner>/<repo>/issues/<n>`）と一致すること

`author_association` が `OWNER` / `MEMBER` / `COLLABORATOR` のいずれでもない場合も
fail-closed（AC9）。両経路が同時に存在する場合は経路 2（明示 evidence）を優先する。

### opt-in ガード（既存 golden fixture 非破壊）

`scope_signal_guard_decision_v2` は `known_context.scope_signal_delta_input` が与えられた場合にのみ
`REFINEMENT_LOOP_PLAN_V1` に追加される。これにより `schemas/refinement_loop_plan_v1.json`
（本 Issue の Allowed Paths 外）を変更せずに additive な出力拡張を実現している。
`scope_signal_delta_input` を使わない既存呼び出し元は、この新フィールドを一切目にしない。


## scope_delta_authority（人間レビュー由来の契約更新分離契約, #1323）

### 背景と目的

`scope_signal_guard_decision_v2`（#1090）は「trusted anchor が明示的に `ANCHOR_SCOPE_REFRAME_V1` を投稿したか」だけを二値で扱っていた。
本 Issue（#1323）は #1008（trusted anchor scope amendment）と #1011（`CONTEXT_PROVENANCE_V1`）を supersede し、
「誰が・どの権限で・何を変更しようとしているか」を `scope_delta_authority` として `scope_signal_guard_decision_v2` の下に additive に格納する。
`scope_signal_guard_decision_v2` 自体の shape（`raw_signal` / `scope_context` / `scope_delta_approval` / `security_sensitive` / `route`）は変更しない。

### scope_delta_authority の shape

```yaml
scope_delta_authority:
  schema_version: SCOPE_DELTA_AUTHORITY_V1
  authority_category: ai_inferred | human_review_directive | existing_parent_contract | related_issue_dependency
  provenance:
    source_kind: issue_comment | pull_request_review | parent_issue | related_issue | generated_by_agent | null
    source_ref: <string | null>
    body_sha256: <sha256 | null>
    author_association: OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | NONE | null
  directive:
    confidence: explicit | ambiguous | conflicting | inferred | null
    extracted_markers: [<string>]
  boundary_flags:
    expands_allowed_paths: true | false
    changes_permission_boundary: true | false
    changes_external_service_boundary: true | false
    destructive_or_non_idempotent_operation: true | false
    requires_issue_split: true | false
  route:
    action: contract_update_required | human_escalation | not_triggered
    reason_code: explicit_human_contract_directive | ambiguous_human_directive | conflicting_human_directives |
                 requires_issue_split | expands_allowed_paths | changes_permission_boundary |
                 changes_external_service_boundary | destructive_or_non_idempotent_operation |
                 ai_inferred_scope_delta | untrusted_author_association | null
    implementation_allowed: true | false
    next_step: rerun_refinement_after_contract_update | null
  contract_patch_plan: <CONTRACT_PATCH_PLAN_V1 | omitted>  # 存在するのは route.action == contract_update_required のときのみ
```

`.claude/skills/issue-refinement-loop/scripts/scope_signal_delta.py` の `classify_scope_delta_authority()` が唯一の分類ロジックであり、
`plan_refinement_loop.py` はこれを呼び出すだけで判定条件を再実装しない。
`known_context.scope_delta_authority_evidence`（単一 dict または複数レビュアー分の list）が
与えられた場合にのみ `scope_signal_guard_decision_v2.scope_delta_authority` を additive に出力する（opt-in、既存 golden fixture 非破壊）。

### 4 分類の判定基準

| authority_category | 判定条件 |
|---|---|
| `human_review_directive` | evidence の `source_kind` が `issue_comment`（構造的 URL/repo 検証を通過したもののみ）かつ `author_association` が `OWNER`/`MEMBER`/`COLLABORATOR`。`source_kind: pull_request_review` は現状 fail-closed で拒否される（後述） |
| `ai_inferred` | evidence が存在しない、`source_kind: generated_by_agent`、`author_association` が信頼できない（fail-closed、AC13）、または URL/repo 構造検証に失敗した evidence（fail-closed、AC16） |
| `existing_parent_contract` | evidence の `source_kind: parent_issue` |
| `related_issue_dependency` | evidence の `source_kind: related_issue` |

### route 決定の優先順位

1. `triggered=false` → `not_triggered`（`implementation_allowed: true`）
2. evidence 欠落 / untrusted author / URL・repo 構造検証失敗 → `ai_inferred` として `human_escalation`（`reason_code: ai_inferred_scope_delta` または `untrusted_author_association`）
3. `existing_parent_contract` / `related_issue_dependency` → `not_triggered`（既に承認済みの契約に由来するため）
4. `boundary_flags` のいずれかが true（trusted anchor 承認があっても） → `human_escalation`（AC18。優先順位: destructive > permission > external_service > allowed_paths > issue_split）
5. 複数レビュアーの `extracted_directives` が矛盾 → `human_escalation`（`reason_code: conflicting_human_directives`, AC17）
6. `directive.confidence: explicit` かつ `base_issue_body_sha256` が解決済み → `contract_update_required`（`reason_code: explicit_human_contract_directive`, `next_step: rerun_refinement_after_contract_update`）
6b. `directive.confidence: explicit` だが `base_issue_body_sha256` が `null`（未解決）→ `human_escalation`（`reason_code: missing_base_issue_body_sha256`, PR #1332 review fix P1 -- 未解決な Issue body snapshot に対する contract_patch_plan 生成を防ぐ）
7. `directive.confidence: ambiguous` / それ以外 → `human_escalation`（`reason_code: ambiguous_human_directive`）

### `expected_repo` 必須化と URL/repo 構造検証（AC16 hardening, PR #1332 review fix P0/P1）

`classify_scope_delta_authority()` は `expected_repo`（`owner/name` 形式）を受け取り、`validate_scope_delta_authority_evidence_url()` へ
そのまま転送する。`plan_refinement_loop.py` の呼び出し元は既存の `_expected_repo_for_issue(issue, known_context)` ヘルパー
（#1090 の `scope_delta_approval` evidence 検証で使われているものと同一）を再利用して渡す。

- `source_kind: issue_comment` の evidence は `expected_repo` が **必須**。`expected_repo` が渡されない場合、
  または `comment_url` の owner/repo が `expected_repo` と一致しない場合は fail-closed で拒否する
  （同一 issue 番号でも別 repo の URL によるなりすましを防ぐ）。
- `source_kind: pull_request_review` の evidence は、`SCOPE_DELTA_AUTHORITY_EVIDENCE_V1` が
  `pull_request_url` / `_links.pull_request` のような PR 紐付けフィールドをまだ持たないため、
  **常に fail-closed で拒否する**（follow-up でこれらのフィールドを追加し構造検証できるようになるまでの暫定措置）。

### scope_delta_authority_evidence_v1（正規化済み evidence, AC14）

`run_refinement_preflight.py` の `_build_scope_delta_authority_evidence()` が anchor comment の GitHub metadata
（author、author_association、comment_url、issue_url、created_at）を構造検証した上で生成する。
raw comment body は決して `known_context` へ渡さない。渡すのは以下の正規化済み evidence のみ。

```yaml
scope_delta_authority_evidence_v1:
  schema_version: SCOPE_DELTA_AUTHORITY_EVIDENCE_V1
  source_kind: issue_comment | pull_request_review | parent_issue | related_issue | generated_by_agent
  source_ref: <string>
  source_issue_number: <int>
  comment_id: <int | string | null>
  comment_url: <string | null>
  issue_url: <string | null>
  body_sha256: <sha256>
  author_login: <string | null>
  author_type: User | Bot | Organization | unknown
  author_association: OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | NONE | null
  captured_at: <ISO-8601>
  directive_markers: [<string>]        # 検出された "Revised Acceptance Criteria" 等のセクション marker
  extracted_directives: [<string>]     # marker 配下の箇条書き行（bullet list）から抽出した提案テキスト
  ambiguity_flags: [<string>]
  boundary_flags: [<string>]           # true になった boundary flag 名のリスト
  confidence: explicit | ambiguous | conflicting | inferred
```

`_classify_anchor_scope_reframe()`（構造化 `ANCHOR_SCOPE_REFRAME_V1` fenced yaml 専用）とは独立した経路であり、
Issue #1270 のような **freeform** な human review コメント（構造化 yaml を含まない Revised Acceptance Criteria 提示）でも
evidence を生成できる。URL が対象 Issue の issue comment として構造的に無効（PR review discussion URL との混同、
issue 番号不一致等、AC16）な場合は evidence を生成せず `None` を返し、呼び出し元は evidence 欠落として扱う（fail-closed）。

### contract_patch_plan_v1（生成専用、direct_github_write 禁止, AC3/AC6/AC19）

`route.action == contract_update_required` のときのみ `scope_delta_authority.contract_patch_plan` が付与される。

```yaml
contract_patch_plan_v1:
  schema_version: CONTRACT_PATCH_PLAN_V1
  target_issue_number: <int | null>
  base_issue_body_sha256: <sha256>  # 必須・non-null (PR #1332 review fix P1) -- build_contract_patch_plan_v1() は
                                     # null の場合 ContractPatchPlanBaseShaMissingError を送出し、呼び出し元
                                     # (classify_scope_delta_authority) は human_escalation にフォールバックする
  source_evidence:
    - source_ref: <string | null>
      source_body_sha256: <sha256 | null>
      author_association: OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | NONE | null
      source_comment_id: <int | string | null>       # PR #1332 review fix P1
      extracted_text_sha256: <sha256 | null>          # 抽出済み directive text の sha256 (raw body は含まない, AC14)
      captured_at: <ISO-8601 | null>
      start_line: <int | null>
      end_line: <int | null>
  operations:
    - section: In Scope | Out of Scope | Acceptance Criteria | Stop Conditions | Verification Commands | Allowed Paths
      op: append
      text: <string>
      rationale: <string>
      source_evidence_index: <int>
  forbidden: [direct_github_write, implementation_phase_transition]
  required_next_step: rerun_refinement_after_contract_update
```

`forbidden` は常に `direct_github_write` と `implementation_phase_transition` の 2 値を含む。
本 plan の生成コードパスから Issue 本文への実際の書き込みや実装フェーズへの遷移を実行することはできない
（`build_contract_patch_plan_v1()` は dict を返すのみで、`gh` 呼び出しを一切含まない）。
実際の適用は既存 Issue 更新専用の `issue-editor` / `edit-issue` skill 経由でのみ行う。

### decide_next_loop_action.py の非破壊分岐（AC20）

`decide_next_loop_action.py` は `scope_signal_guard_decision_v2` を **`--loop-state-file`/`--loop-state-json` とは別の
sidecar 引数**（`--scope-signal-guard-decision-v2-file` / `--scope-signal-guard-decision-v2-json`）として受け取る。
#1873: `schemas/loop_state.schema.json` と `build_loop_state.py` は撤去された。
`scope_signal_guard_decision_v2` は元々 `LOOP_STATE_V1` 本体のフィールドではなく、
`decide_next_loop_action.py` へ別引数で渡すサイドカーとしてのみ存在した契約であり、
この非統合方針は撤去後も変わらない（`LOOP_STATE_V1` のトップレベル契約には追加しない）。

`scope_signal_guard_decision_v2.scope_delta_authority.route.action == contract_update_required`
（**ネストされた** `SCOPE_DELTA_AUTHORITY_V1` の `route.action` -- トップレベルの
`scope_signal_guard_decision_v2["route"]` フィールドとは別物であることに注意。トップレベル `route` は
#1090 由来の既存 enum（`not_triggered` / `human_judgment_required` / `invalid_scope_delta_approval` /
`proceed_with_notes`）であり、`contract_update_required` という値を取ることは一切ない。PR #1332 review で
この 2 つの `route` フィールドの階層が混同されていた実装バグが指摘され修正済み）を受け取ると、
`NEXT_ACTION: proceed_with_contract_update` を返す（`termination_reason` は一切変更しない。loop は継続したまま、
契約更新 → refinement 再実行の non-destructive branch に入る）。sidecar が未指定、`scope_delta_authority` キー欠落、
またはパース失敗時は soft-fail（絶対値として扱わず、既存の `scope_signal_guard.triggered` ベースの hard stop 判定に
フォールバックする）。

### #1008 / #1011 との対応関係（Supersede）

| 旧 Issue | 旧概念 | 本 Issue での統合先 |
|---|---|---|
| #1008 AC1〜AC5 | trusted anchor 判定・AI 推定との区別・untrusted fail-closed・raw snapshot 非流入 | `scope_delta_authority.authority_category` / `provenance.author_association` / AC13 / AC14 |
| #1011 AC1〜AC5 | `CONTEXT_PROVENANCE_V1`（human-requested / agent-inferred / repo-evidence 分離） | `scope_delta_authority.provenance`（`source_kind` / `source_ref` / `author_association`） |

両 Issue は本 Issue マージ後、「Closed as superseded by #1323」として close する。
