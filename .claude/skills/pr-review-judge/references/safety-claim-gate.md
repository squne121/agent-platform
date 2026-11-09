# Safety Claim Gate

## Safety-sensitive 判定（fail-closed）

以下のいずれかで safety-sensitive と判定。

- changed path が `transport|permission|sandbox|auth|mcp|tool` や `.github/workflows/**`、`.claude/skills/**`
- PR / diff / issue に safety 境界ワードを含む
- issue ラベル/本文に `safety`, `permission`, `runtime verification`, ... が含まれる

## Deterministic Minimum Floor（producer と reviewer が共有する最小判定基準、Issue #2808 AC4）

上記の reviewer 判定は本節が定義する floor より広く、reviewer はこの floor 以外の
semantic concern を引き続き検出してよい。一方、authoring 側の
`.claude/skills/open-pr/scripts/validate_pr_body.py::_is_safety_sensitive()` は
reviewer の全 semantic 判定を複製せず、両者が最低限共有すべき **deterministic
minimum floor** だけを実装する。

- 入力: `changed_paths` + PR body + （利用可能な場合の）linked Issue body の3つに限定する。
- path 側は既存の `SAFETY_SENSITIVE_PATH_PATTERNS`（`transport|permission|sandbox|auth|mcp|.claude/skills/|.github/workflows/`）。
- text 側は `SAFETY_SENSITIVE_TEXT_PATTERNS`（GitHub PAT prefix `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`github_pat_`、
  `personal access token`、`secret-like token`、`credential redaction`、`redaction`）という強い signal に限定し、
  bare `token` のような naive substring では判定しない（PR #2806 の credential redaction 再発防止・
  近傍語 `parser token` / `design token` の過検知回避、Issue #2808 AC4/AC5）。
- この floor が sensitive と判定した場合、authoring（`open_pr.py` の create path、`update_pr.py` の
  update path の両方）と reviewer は必ず Safety Claim Matrix を要求する。
- floor が non-sensitive と判定しても、reviewer は上記セクション冒頭の判定基準（semantic wording 等）で
  追加的に safety-sensitive と判断してよい。floor は authoring 側の false-pass を閉じるための下限であり、
  reviewer の判断力の上限ではない。

## 要求

- `Safety Claim Matrix` セクション必須
- `Claim / Implemented / Not controlled / Evidence / Follow-up` を確認

## 禁止条件

- `Not controlled` が非空なのに open な follow-up が無い
- `Evidence` が linked issue ならびに PR 証跡と不整合
- Not controlled と無限定 safe/read-only/main claim が衝突する主張

## Claim-to-Evidence Coverage（有限 subclaim の直接証拠確認、Issue #2765）

Safety Claim Matrix の Claim が有限の複数 subclaim（例: 複数トークン形式、複数プラットフォーム、複数実行パス等）について all / each / 全対応 / 非回帰などの exhaustive completeness を主張する場合、常に test evidence を要求する設計にはせず、Evidence が各 subclaim を直接 support しているかを確認する claim-to-evidence coverage を適用する。

- Evidence が **test** の場合のみ、`references/ac-evidence-checks.md` の Enumerated / Exhaustive Claim Evidence Coverage rule（case identity AND relevant executable assertion が PASS していること）を適用する。
- Evidence が test 以外（source/configuration inspection、policy/config diff、permission declaration、runtime artifact、CI/CheckRun、deterministic validator output 等）の場合は、これらの非-test evidence を「test でない」という理由だけで不当に排除しない。各 subclaim に対応する具体的な evidence の所在と、Claim との対応関係（どの subclaim をどの evidence が support するか）を確認する。
- 同一 Evidence セル内に test evidence と非-test evidence（例: source inspection + test result）が混在する場合、セル全体を一律に test/non-test へ二分しない。**各 subclaim を support する evidence item が test の場合に**上記 enumerated-test rule を適用し、同じセル内の他の legitimate な非-test evidence item を、隣接する test evidence の有無を理由に排除しない。
- 単なる例示列挙（「例: A/B」等）は本 rule の対象外。
