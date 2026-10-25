"""
test_guard_ac_vc_alignment.py

guard-issue-body.py の guard_ac_vc_alignment（および main() 内 issue_kind 未確定時の
フォールバック分岐）が「集合一致」で判定することを確認するユニットテスト（#1694）。

テスト戦略:
- 関数本体（guard_ac_vc_alignment / _evaluate_ac_vc_alignment）は importlib で直接呼び出す
- issue_kind 未確定時のフォールバック分岐は main() のコードパスであり、
  subprocess 経由で --issue-kind を渡さず MRC も持たない body を渡すことで到達させる
- 1つの AC に複数 VC コマンドを束ねる正当なパターン（同一 AC 番号が複数回登場）で
  passed: true になることを確認する（関数本体・フォールバック分岐の両方）
- 真の不一致（AC に無い番号が VC にのみ登場、または AC にある番号が VC に無い）では
  passed: false になることを確認する（関数本体・フォールバック分岐の両方）
- grouped marker（`# AC1, AC2`）・inline suffix（`$ some_command  # AC1`）から
  全ての AC 番号が抽出されることを確認する
- `## Acceptance Criteria` セクション境界外のチェックボックスは AC として数えない
- bash fence 外の `# AC1` 風コメントは VC として数えない
- AC 定義側の重複は fail、VC 側の重複参照（束ね）は許容する非対称性を確認する
- 既存 subprocess ベースのテスト（issue_kind 未確定分岐）は guard の passed だけでなく
  CLI 全体の returncode / all_passed も検証する
- known-kind の完全な正当な candidate body による production CLI 呼び出しで
  returncode == 0 かつ all_passed is True になることを確認する
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# テスト対象モジュールをインポートするために scripts ディレクトリをパスに追加
# ファイル名が guard-issue-body.py（ハイフン）のため importlib で読み込む
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
_MODULE_PATH = _SCRIPTS_DIR / "guard-issue-body.py"
_MODULE_PATH_STR = str(_MODULE_PATH)

_spec = importlib.util.spec_from_file_location("guard_issue_body_ac_vc", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

guard_ac_vc_alignment = _mod.guard_ac_vc_alignment
_evaluate_ac_vc_alignment = _mod._evaluate_ac_vc_alignment


# ---------------------------------------------------------------------------
# template_dir フィクスチャ（implementation 種別のみ、Verification Commands required）
# ---------------------------------------------------------------------------

_IMPLEMENTATION_TEMPLATE = {
    "name": "Implementation",
    "body": [
        {
            "type": "textarea",
            "attributes": {"label": "Machine-Readable Contract"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Acceptance Criteria"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Verification Commands"},
            "validations": {"required": True},
        },
    ],
}


@pytest.fixture
def template_dir(tmp_path):
    """一時 ISSUE_TEMPLATE ディレクトリを作成し implementation.yml を書き込む。"""
    tmpl_dir = tmp_path / "ISSUE_TEMPLATE"
    tmpl_dir.mkdir()
    path = tmpl_dir / "implementation.yml"
    path.write_text(yaml.dump(_IMPLEMENTATION_TEMPLATE, allow_unicode=True), encoding="utf-8")
    return tmpl_dir


_BUNDLED_VC_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため



## Acceptance Criteria

- [ ] AC7 4つの検証コマンドを束ねる

## Verification Commands

```bash
# AC7
$ pnpm typecheck
# AC7
$ pnpm lint
# AC7
$ pnpm test
# AC7
$ pnpm build
```
"""

_TRUE_MISMATCH_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため



## Acceptance Criteria

- [ ] AC1 テスト1

## Verification Commands

```bash
# AC1
$ pnpm test
# AC2
$ pnpm build
```
"""


# ---------------------------------------------------------------------------
# 関数本体（guard_ac_vc_alignment）のテスト
# ---------------------------------------------------------------------------

class TestGuardAcVcAlignmentSetBased:
    def test_bundled_vc_commands_for_single_ac_passes(self, template_dir):
        """GIVEN 1 つの AC に複数 VC コマンドを束ねる（同一 AC 番号が複数回登場）
        WHEN guard_ac_vc_alignment THEN 集合一致で passed: true"""
        result = guard_ac_vc_alignment(_BUNDLED_VC_BODY, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is True
        assert result["ac_count"] == 1
        assert result["vc_ac_count"] == 4

    def test_true_mismatch_ac_missing_from_vc_fails(self, template_dir):
        """GIVEN AC に無い番号が VC にのみ登場する真の不一致
        WHEN guard_ac_vc_alignment THEN passed: false"""
        result = guard_ac_vc_alignment(_TRUE_MISMATCH_BODY, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is False
        assert result["missing_in_vc"] == []
        assert result["extra_in_vc"] == ["2"]

    def test_set_based_equality_ignores_ordering_and_duplicates(self, template_dir):
        """GIVEN AC 番号集合と VC 番号集合が同じだが並び・重複が異なる
        WHEN guard_ac_vc_alignment THEN 集合一致で passed: true"""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため



## Acceptance Criteria

- [ ] AC2 二番目
- [ ] AC1 一番目

## Verification Commands

```bash
# AC1
$ pnpm test
# AC1
$ pnpm test --watch
# AC2
$ pnpm build
```
"""
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# _evaluate_ac_vc_alignment（共通抽出・比較関数）のパラメトライズテスト（P1）
# ---------------------------------------------------------------------------

_GROUPED_MARKER_BODY = """\
## Acceptance Criteria

- [ ] AC1 一番目
- [ ] AC2 二番目

## Verification Commands

```bash
# AC1, AC2
$ pnpm build
```
"""

_INLINE_SUFFIX_BODY = """\
## Acceptance Criteria

- [ ] AC1 一番目

## Verification Commands

```bash
$ some_command  # AC1
```
"""

_SINGLE_AC_SINGLE_VC_BODY = """\
## Acceptance Criteria

- [ ] AC1 一番目

## Verification Commands

```bash
# AC1
$ pnpm test
```
"""

_AC_NO_VC_REF_BODY = """\
## Acceptance Criteria

- [ ] AC1 一番目

## Verification Commands

```bash
$ pnpm test
```
"""

_VC_ONLY_NO_AC_BODY = """\
## Acceptance Criteria

## Verification Commands

```bash
# AC99
$ pnpm test
```
"""

_BOTH_EMPTY_BODY = """\
## Acceptance Criteria

## Verification Commands

```bash
$ true
```
"""

_DUPLICATE_AC_DEFINITION_BODY = """\
## Acceptance Criteria

- [ ] AC1 一番目
- [ ] AC1 重複定義

## Verification Commands

```bash
# AC1
$ pnpm test
```
"""

_AC_SECTION_BOUNDARY_BODY = """\
## Some Other Section

- [ ] AC99 このセクションの外側なので数えない

## Acceptance Criteria

- [ ] AC1 一番目

## Verification Commands

```bash
# AC1
$ pnpm test
```
"""

_FENCE_OUTSIDE_COMMENT_BODY = """\
## Acceptance Criteria

- [ ] AC1 一番目

## Verification Commands

# AC1 (fence 外のコメントは VC として数えない)

```bash
$ pnpm test
```
"""


@pytest.mark.parametrize(
    "name,body,expected_passed",
    [
        ("single_ac_single_vc", _SINGLE_AC_SINGLE_VC_BODY, True),
        ("bundled_marker_repeat", _BUNDLED_VC_BODY, True),
        ("grouped_marker", _GROUPED_MARKER_BODY, True),
        ("inline_suffix", _INLINE_SUFFIX_BODY, True),
        ("ac_no_vc_ref", _AC_NO_VC_REF_BODY, False),
        ("true_mismatch_extra_in_vc", _TRUE_MISMATCH_BODY, False),
        ("vc_only_no_ac", _VC_ONLY_NO_AC_BODY, False),
        ("both_empty", _BOTH_EMPTY_BODY, True),
        ("duplicate_ac_definition", _DUPLICATE_AC_DEFINITION_BODY, False),
        ("ac_section_boundary_excludes_outside", _AC_SECTION_BOUNDARY_BODY, True),
        ("fence_outside_comment_not_counted", _FENCE_OUTSIDE_COMMENT_BODY, False),
    ],
)
def test_evaluate_ac_vc_alignment_parametrized(name, body, expected_passed):
    result = _evaluate_ac_vc_alignment(body)
    assert result["passed"] is expected_passed, (
        f"case={name!r} expected passed={expected_passed}, got {result!r}"
    )


def test_duplicate_ac_definition_reports_duplicate_ac_ids():
    """GIVEN AC 定義側の重複 WHEN _evaluate_ac_vc_alignment THEN duplicate_ac_ids に記録される"""
    result = _evaluate_ac_vc_alignment(_DUPLICATE_AC_DEFINITION_BODY)
    assert result["duplicate_ac_ids"] == ["1"]


def test_vc_side_duplicate_reference_is_allowed():
    """GIVEN VC 側で同一 AC を複数コマンドから束ねる（VC 側の重複参照）
    WHEN _evaluate_ac_vc_alignment THEN duplicate_ac_ids は空で passed: true"""
    result = _evaluate_ac_vc_alignment(_BUNDLED_VC_BODY)
    assert result["duplicate_ac_ids"] == []
    assert result["passed"] is True


def test_ac_section_boundary_excludes_outside_checkbox_from_ac_count():
    """GIVEN Acceptance Criteria セクション外に checkbox 行がある body
    WHEN _evaluate_ac_vc_alignment THEN ac_count はセクション内のみを数える"""
    result = _evaluate_ac_vc_alignment(_AC_SECTION_BOUNDARY_BODY)
    assert result["ac_count"] == 1


def test_fence_outside_comment_not_counted_as_vc():
    """GIVEN VC セクション内だが bash fence 外にある `# AC1` コメント
    WHEN _evaluate_ac_vc_alignment THEN vc_ac_count は 0（fence 内のみ数える）"""
    result = _evaluate_ac_vc_alignment(_FENCE_OUTSIDE_COMMENT_BODY)
    assert result["vc_ac_count"] == 0
    assert result["passed"] is False


# ---------------------------------------------------------------------------
# issue_kind 未確定時のフォールバック分岐のテスト（main() 内、subprocess 経由）
# ---------------------------------------------------------------------------

def _run_guard_issue_body(body_text: str, tmp_path) -> tuple:
    """--issue-kind を渡さず MRC も持たない body で main() を実行し、
    issue_kind 未確定時のフォールバック分岐（ac_vc_alignment を含む）に到達させる。

    Returns:
        (returncode, output_dict)
    """
    body_file = tmp_path / "body.md"
    body_file.write_text(body_text, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            _MODULE_PATH_STR,
            str(body_file),
            "--format", "json",
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode, json.loads(result.stdout)


class TestGuardAcVcAlignmentFallbackBranch:
    def test_fallback_bundled_vc_commands_for_single_ac_passes(self, tmp_path):
        """GIVEN issue_kind 未確定（MRC なし）で 1 AC に複数 VC コマンドを束ねる body
        WHEN main() のフォールバック分岐 THEN ac_vc_alignment が passed: true を返す。
        issue_kind が未確定のため template_guard は fail し CLI 全体は exit 2 /
        all_passed: false になる（unknown-kind は必ず CLI 全体を fail させる仕様）。"""
        body = """\
## Acceptance Criteria

- [ ] AC7 4つの検証コマンドを束ねる

## Verification Commands

```bash
# AC7
$ pnpm typecheck
# AC7
$ pnpm lint
# AC7
$ pnpm test
# AC7
$ pnpm build
```
"""
        returncode, output = _run_guard_issue_body(body, tmp_path)
        ac_vc_results = [g for g in output["guards"] if g["name"] == "ac_vc_alignment"]
        assert len(ac_vc_results) == 1
        assert ac_vc_results[0]["passed"] is True
        assert ac_vc_results[0]["ac_count"] == 1
        assert ac_vc_results[0]["vc_ac_count"] == 4
        # unknown-kind: template_guard は必ず fail するため CLI 全体は exit 2 / all_passed: false
        assert returncode == 2
        assert output["all_passed"] is False

    def test_fallback_true_mismatch_fails(self, tmp_path):
        """GIVEN issue_kind 未確定（MRC なし）で AC に無い番号が VC にのみ登場する body
        WHEN main() のフォールバック分岐 THEN ac_vc_alignment が passed: false を返す。
        CLI 全体も exit 2 / all_passed: false になる（unknown-kind の template_guard fail
        と ac_vc_alignment fail の両方が原因になり得る）。"""
        body = """\
## Acceptance Criteria

- [ ] AC1 テスト1

## Verification Commands

```bash
# AC1
$ pnpm test
# AC2
$ pnpm build
```
"""
        returncode, output = _run_guard_issue_body(body, tmp_path)
        ac_vc_results = [g for g in output["guards"] if g["name"] == "ac_vc_alignment"]
        assert len(ac_vc_results) == 1
        assert ac_vc_results[0]["passed"] is False
        assert returncode == 2
        assert output["all_passed"] is False

    def test_fallback_unknown_kind_cli_always_fails_regardless_of_alignment(self, tmp_path):
        """GIVEN issue_kind 未確定（MRC なし）で ac_vc_alignment 自体は passed: true になる body
        WHEN main() のフォールバック分岐 THEN unknown-kind の template_guard fail により
        CLI 全体は alignment の pass/fail に関係なく exit 2 / all_passed: false になる。"""
        body = """\
## Acceptance Criteria

- [ ] AC1 一番目

## Verification Commands

```bash
# AC1
$ pnpm test
```
"""
        returncode, output = _run_guard_issue_body(body, tmp_path)
        ac_vc_results = [g for g in output["guards"] if g["name"] == "ac_vc_alignment"]
        template_results = [g for g in output["guards"] if g["name"] == "template_guard"]
        assert ac_vc_results[0]["passed"] is True
        assert template_results[0]["passed"] is False
        assert returncode == 2
        assert output["all_passed"] is False


# ---------------------------------------------------------------------------
# known-kind の完全な正当な candidate body による production CLI テスト
# ---------------------------------------------------------------------------

def _make_full_valid_implementation_body() -> str:
    """他の guard も全部満たす、known-kind の完全な正当な candidate body。"""
    return """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: "テスト用"
change_kind: code
```

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため



## Parent Issue

none

## Parent Goal Ref

- Goal: なし
- Desired Destination: N/A

## Current Validated Scope

- テスト実装

## Remaining Parent Gaps

なし

## Outcome

テストが PASS している状態

## In Scope

- テスト実装

## Out of Scope

- 本番実装

## Acceptance Criteria

- [ ] AC1 テストが PASS する
- [ ] AC2 型エラーがない

## Verification Commands

```bash
# AC1
$ uv run pytest tests/ -v
# AC2
$ pnpm typecheck
```

## Allowed Paths

- tests/test_example.py

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約（キー集合・スキーマ・型定義）の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合（スコープ分割が発生する場合）
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Required Design References

- docs/dev/agent-skill-boundaries.md

## Required Skills

- Python / pytest
"""


class TestGuardIssueBodyProductionFullPass:
    def test_full_valid_implementation_body_cli_returncode_zero_all_passed_true(self, tmp_path):
        """GIVEN known-kind の完全な正当な candidate body（他の guard も全部満たす）
        WHEN guard-issue-body.py CLI を実行 THEN returncode == 0 かつ all_passed is True"""
        body_file = tmp_path / "body.md"
        body_file.write_text(_make_full_valid_implementation_body(), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                _MODULE_PATH_STR,
                str(body_file),
                "--issue-kind", "implementation",
                "--format", "json",
            ],
            capture_output=True,
            text=True,
        )
        output = json.loads(result.stdout)
        assert result.returncode == 0, (
            f"expected returncode 0, got {result.returncode}. "
            f"output={output!r} stderr={result.stderr!r}"
        )
        assert output["all_passed"] is True
