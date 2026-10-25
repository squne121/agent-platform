"""
test_guard_issue_body.py

guard-issue-body.py のユニットテスト。

テスト戦略:
- tmp_path で一時 ISSUE_TEMPLATE ディレクトリを作る方式を採用
- implementation / research / parent の 3 種別すべてで正常 body が template guard を pass することを確認
- required section を 1 つ欠いた body が fail することを確認
- parent 種別で guard_ac_vc_alignment() が skipped: true を返すことを確認
- PyYAML は yaml.safe_load() のみ使用
"""

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# テスト対象モジュールをインポートするために scripts ディレクトリをパスに追加
# ファイル名が guard-issue-body.py（ハイフン）のため importlib で読み込む
import importlib.util

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
_MODULE_PATH = _SCRIPTS_DIR / "guard-issue-body.py"

_spec = importlib.util.spec_from_file_location("guard_issue_body", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

guard_ac_vc_alignment = _mod.guard_ac_vc_alignment
guard_template = _mod.guard_template
guard_vc_compound_shell_disallowed = _mod.guard_vc_compound_shell_disallowed
guard_ready_tuple = _mod.guard_ready_tuple
guard_issue_body_validation = _mod.guard_issue_body_validation
check_ready_tuple = _mod.check_ready_tuple
load_required_labels = _mod.load_required_labels
extract_issue_kind_from_body = _mod.extract_issue_kind_from_body
validate_issue_kind = _mod.validate_issue_kind
validate_path = _mod.validate_path
_MODULE_PATH_STR = str(_MODULE_PATH)


# ---------------------------------------------------------------------------
# ISSUE_TEMPLATE フィクスチャ用の YAML コンテンツ
# ---------------------------------------------------------------------------

_IMPLEMENTATION_TEMPLATE = {
    "name": "Implementation",
    "body": [
        {
            "type": "markdown",
            "attributes": {"value": "説明テキスト"},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Machine-Readable Contract"},
            "validations": {"required": True},
        },
        {
            "type": "input",
            "attributes": {"label": "Parent Issue"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Parent Goal Ref"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Current Validated Scope"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Remaining Parent Gaps"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Outcome"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "In Scope"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Out of Scope"},
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
        {
            "type": "textarea",
            "attributes": {"label": "Allowed Paths"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Stop Conditions"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Required Skills"},
            "validations": {"required": True},
        },
    ],
}

_RESEARCH_TEMPLATE = {
    "name": "Research",
    "body": [
        {
            "type": "markdown",
            "attributes": {"value": "説明テキスト"},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Machine-Readable Contract"},
            "validations": {"required": True},
        },
        {
            "type": "input",
            "attributes": {"label": "Parent Issue"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Outcome"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "In Scope"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Out of Scope"},
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
        {
            "type": "textarea",
            "attributes": {"label": "Allowed Paths"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Stop Conditions"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Handoff Contract"},
            "validations": {"required": True},
        },
    ],
}

_PARENT_TEMPLATE = {
    "name": "Parent",
    "body": [
        {
            "type": "markdown",
            "attributes": {"value": "説明テキスト"},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Machine-Readable Contract"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Summary"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Goal"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Desired Destination"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Current Validated Scope"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Decisions Fixed"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Quality Decision Record"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Parent Closure Rule"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Child Issues"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Remaining Parent Gaps"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Phase Handoff Contract"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Acceptance Criteria"},
            "validations": {"required": True},
        },
        # Notes は required: false なので含めない
    ],
}


@pytest.fixture
def template_dir(tmp_path):
    """一時 ISSUE_TEMPLATE ディレクトリを作成し、3 種別の YAML を書き込む。"""
    tmpl_dir = tmp_path / "ISSUE_TEMPLATE"
    tmpl_dir.mkdir()

    for kind, content in [
        ("implementation", _IMPLEMENTATION_TEMPLATE),
        ("research", _RESEARCH_TEMPLATE),
        ("parent", _PARENT_TEMPLATE),
    ]:
        path = tmpl_dir / f"{kind}.yml"
        path.write_text(yaml.dump(content, allow_unicode=True), encoding="utf-8")

    return tmpl_dir


# ---------------------------------------------------------------------------
# ヘルパー: 各種別の正常 body を生成
# ---------------------------------------------------------------------------

def make_implementation_body() -> str:
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
uv run pytest tests/ -v
# AC2
pnpm typecheck
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


def make_research_body() -> str:
    return """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "テスト調査"
change_kind: research-only
```

## Parent Issue

none

## Outcome

調査完了し次の implementation Issue が起票可能な状態

## In Scope

- アルゴリズム候補 3 件の比較

## Out of Scope

- 実装コード

## Acceptance Criteria

- [ ] AC1 比較結果が本 Issue 本文に記載されている
- [ ] AC2 後続 Issue が起票されている

## Verification Commands

```bash
# AC1
rg -n "比較結果" docs/research/example.md
# AC2
gh issue view 999
```

## Allowed Paths

- 読み取り専用

## Stop Conditions

- Allowed Paths 外への書き込みを試みた場合は即停止

## Handoff Contract

- Current Objective
- Next Action
"""


def make_parent_body() -> str:
    return """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: "親 Issue トラッカー"
change_kind: workflow
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Summary

複数 child issue を束ねるトラッカー

## Goal

機能 X の完成

## Desired Destination

機能 X が main ブランチに統合された状態

## Current Validated Scope

- child issue #1、#2、#3

## Decisions Fixed

- 2026-05-20: アーキテクチャを ECS に決定

## Quality Decision Record

- Status: N/A
- Decision Date: 未記録
- Does this prove the parent goal complete?: N/A
- Reason: N/A
- Evidence: N/A
- Next Action: N/A

## Parent Closure Rule

- delivery-rollup: 全 child issue が close されたら close する
- quality-gate: N/A
- routing-map: N/A
- decision-log: N/A

## Child Issues

- [ ] #100 — implementation child

## Remaining Parent Gaps

- [ ] 後続実装

## Phase Handoff Contract

- Desired Destination
- Current Validated Scope

## Acceptance Criteria

- [ ] AC1 全 child issue が close される
"""


# ---------------------------------------------------------------------------
# load_required_labels のテスト
# ---------------------------------------------------------------------------

class TestLoadRequiredLabels:
    def test_implementation_returns_expected_labels(self, template_dir):
        """GIVEN implementation テンプレート WHEN load_required_labels を呼ぶ THEN 必須ラベルが返る"""
        labels = load_required_labels(template_dir, "implementation")
        assert "Outcome" in labels
        assert "In Scope" in labels
        assert "Out of Scope" in labels
        assert "Acceptance Criteria" in labels
        assert "Verification Commands" in labels
        assert "Allowed Paths" in labels
        assert "Stop Conditions" in labels
        # markdown 要素は除外される
        assert len([lbl for lbl in labels if lbl is None]) == 0

    def test_research_returns_expected_labels(self, template_dir):
        """GIVEN research テンプレート WHEN load_required_labels を呼ぶ THEN 必須ラベルが返る"""
        labels = load_required_labels(template_dir, "research")
        assert "Outcome" in labels
        assert "Verification Commands" in labels
        assert "Handoff Contract" in labels

    def test_parent_returns_expected_labels(self, template_dir):
        """GIVEN parent テンプレート WHEN load_required_labels を呼ぶ THEN 必須ラベルが返る"""
        labels = load_required_labels(template_dir, "parent")
        assert "Summary" in labels
        assert "Goal" in labels
        assert "Acceptance Criteria" in labels
        # parent は Verification Commands を持たない
        assert "Verification Commands" not in labels

    def test_missing_template_raises_file_not_found(self, template_dir):
        """GIVEN 存在しない kind WHEN load_required_labels を呼ぶ THEN FileNotFoundError"""
        with pytest.raises(FileNotFoundError, match="ISSUE_TEMPLATE not found"):
            load_required_labels(template_dir, "nonexistent")

    def test_markdown_items_are_excluded(self, template_dir):
        """GIVEN テンプレートに markdown 要素がある WHEN load_required_labels を呼ぶ THEN markdown は除外される"""
        labels = load_required_labels(template_dir, "implementation")
        # markdown 要素の label は None なので結果リストに None が含まれない
        for label in labels:
            assert isinstance(label, str)


# ---------------------------------------------------------------------------
# extract_issue_kind_from_body のテスト
# ---------------------------------------------------------------------------

class TestExtractIssueKindFromBody:
    def test_extracts_implementation_from_mrc_block(self):
        """GIVEN implementation の MRC ブロック WHEN extract_issue_kind_from_body THEN 'implementation' を返す"""
        body = make_implementation_body()
        result = extract_issue_kind_from_body(body)
        assert result == "implementation"

    def test_extracts_research_from_mrc_block(self):
        """GIVEN research の MRC ブロック WHEN extract_issue_kind_from_body THEN 'research' を返す"""
        body = make_research_body()
        result = extract_issue_kind_from_body(body)
        assert result == "research"

    def test_extracts_parent_from_mrc_block(self):
        """GIVEN parent の MRC ブロック WHEN extract_issue_kind_from_body THEN 'parent' を返す"""
        body = make_parent_body()
        result = extract_issue_kind_from_body(body)
        assert result == "parent"

    def test_returns_none_when_no_mrc_block(self):
        """GIVEN MRC ブロックがない本文 WHEN extract_issue_kind_from_body THEN None を返す"""
        body = "# タイトル\n\n本文のみ"
        result = extract_issue_kind_from_body(body)
        assert result is None

    def test_returns_none_when_no_issue_kind_in_mrc(self):
        """GIVEN issue_kind が無い MRC ブロック WHEN extract_issue_kind_from_body THEN None を返す"""
        body = "```yaml\ncontract_schema_version: v1\n```\n"
        result = extract_issue_kind_from_body(body)
        assert result is None


# ---------------------------------------------------------------------------
# guard_template のテスト（#68 AC2/AC3、#2794 の fence 境界）
# ---------------------------------------------------------------------------

# #2794 の記録に基づく literal fixture。#2788 編集前の raw bytes は未取得であり、
# 当時の本文と同一だったという主張はしない。
_ORPHAN_BACKTICK_LITERAL_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Parent Issue

none

## Parent Goal Ref

- Goal: テスト

## Current Validated Scope

- 範囲

## Remaining Parent Gaps

なし

## Background

次行は info string の末尾に backtick を含む不揃いな prose 行。
```bash`

## Outcome

実在する成果物。

## In Scope

- 実在する対象。

## Out of Scope

- 対象外。

## Acceptance Criteria

- [ ] AC1: 見出しを保持する。

## Verification Commands

```bash
# AC1
uv run pytest tests/ -q
```

## Allowed Paths

- tests/

## Stop Conditions

- 範囲外の変更時に停止。

## Required Skills

なし
"""


class TestGuardTemplate:
    def test_implementation_valid_body_passes(self, template_dir):
        """GIVEN implementation の正常 body WHEN guard_template THEN passed=True"""
        body = make_implementation_body()
        result = guard_template(body, "implementation", template_dir=template_dir)
        assert result["passed"] is True
        assert result["missing_sections"] == []

    def test_implementation_missing_section_fails(self, template_dir):
        """GIVEN implementation の body から Outcome を削除 WHEN guard_template THEN passed=False"""
        body = make_implementation_body()
        body = body.replace("## Outcome\n", "## DELETED\n")
        result = guard_template(body, "implementation", template_dir=template_dir)
        assert result["passed"] is False
        assert "## Outcome" in result["missing_sections"]

    def test_research_valid_body_passes(self, template_dir):
        """GIVEN research の正常 body WHEN guard_template THEN passed=True"""
        body = make_research_body()
        result = guard_template(body, "research", template_dir=template_dir)
        assert result["passed"] is True
        assert result["missing_sections"] == []

    def test_research_missing_section_fails(self, template_dir):
        """GIVEN research の body から Handoff Contract を削除 WHEN guard_template THEN passed=False"""
        body = make_research_body()
        body = body.replace("## Handoff Contract\n", "## DELETED\n")
        result = guard_template(body, "research", template_dir=template_dir)
        assert result["passed"] is False
        assert "## Handoff Contract" in result["missing_sections"]

    def test_parent_valid_body_passes(self, template_dir):
        """GIVEN parent の正常 body WHEN guard_template THEN passed=True"""
        body = make_parent_body()
        result = guard_template(body, "parent", template_dir=template_dir)
        assert result["passed"] is True
        assert result["missing_sections"] == []

    def test_parent_missing_section_fails(self, template_dir):
        """GIVEN parent の body から Goal を削除 WHEN guard_template THEN passed=False"""
        body = make_parent_body()
        body = body.replace("## Goal\n", "## DELETED\n")
        result = guard_template(body, "parent", template_dir=template_dir)
        assert result["passed"] is False
        assert "## Goal" in result["missing_sections"]

    def test_nonexistent_kind_fails(self, template_dir):
        """GIVEN 存在しない kind WHEN guard_template THEN passed=False かつ error が含まれる"""
        body = make_implementation_body()
        result = guard_template(body, "nonexistent", template_dir=template_dir)
        assert result["passed"] is False
        assert "error" in result

    def test_orphan_backtick_shared_parser_adapter(self, template_dir):
        """GIVEN #2794 に記録された不揃いな prose WHEN guard_template THEN 実見出しを保持する。"""
        lines = _ORPHAN_BACKTICK_LITERAL_BODY.splitlines()
        orphan_index = lines.index("```bash`")
        vc_index = lines.index("```bash", orphan_index + 1)
        # opener の3文字 + info string 末尾の1文字を、文字列そのもので固定する。
        assert lines[orphan_index].startswith("`" * 3 + "bash")
        assert lines[orphan_index][3:] == "bash`"  # info string 末尾の U+0060
        assert lines[orphan_index].count("`") == 4
        assert lines[orphan_index] == lines[orphan_index].lstrip(" ")
        assert len(lines[orphan_index]) - len(lines[orphan_index].lstrip(" ")) == 0
        assert orphan_index < lines.index("## Outcome") < lines.index("## In Scope") < vc_index
        assert lines[vc_index] == "```bash"
        assert "```\n\n## Allowed Paths" in _ORPHAN_BACKTICK_LITERAL_BODY

        result = guard_template(
            _ORPHAN_BACKTICK_LITERAL_BODY, "implementation", template_dir=template_dir
        )
        assert result["passed"] is True
        assert result["missing_sections"] == []

        # 同じ不揃い文字列が prose の inline にある場合も実見出しを保持する。
        inline_body = _ORPHAN_BACKTICK_LITERAL_BODY.replace(
            "\n```bash`\n", "\n本文中の不揃い ```bash` は prose の一部。\n", 1
        )
        assert "本文中の不揃い ```bash` は prose の一部。" in inline_body
        assert "\n```bash`\n" not in inline_body
        assert guard_template(inline_body, "implementation", template_dir=template_dir)[
            "missing_sections"
        ] == []

    def test_shared_parser_fence_boundary_cases(self, template_dir):
        """GIVEN GFM fence 境界 WHEN guard_template THEN code 内だけ除外して実見出しを保持する。"""
        base = make_implementation_body()
        # opener の 0–3 spaces と、4-backtick 中の3-backtick、tilde、
        # 同種・同長以上の closer 制約を consumer 経由で代表検証する。
        fences = (
            "```bash\n## Outcome\n```",                     # 通常の偽見出し
            " ```bash\n## Outcome\n ```",                   # 1 space
            "   ```bash\n## Outcome\n   ```",               # 3 spaces
            "````bash\n```\n## Outcome\n````",            # 短い closer は無効
            "~~~bash\n```\n## Outcome\n~~~",              # backtick は tilde を閉じない
            "```bash\n~~~\n## Outcome\n```",              # tilde は backtick を閉じない
        )
        for fence in fences:
            without_real = base.replace("## Outcome\n", fence + "\n## REMOVED\n", 1)
            result = guard_template(without_real, "implementation", template_dir=template_dir)
            assert "## Outcome" in result["missing_sections"], fence

            with_real = base.replace("## Outcome\n", fence + "\n## Outcome\n", 1)
            result = guard_template(with_real, "implementation", template_dir=template_dir)
            assert result["missing_sections"] == [], fence

        # 4-space indented opener と info string 内 backtick は fence ではない。
        for prose_line, indent in (
            ("    ```bash", 4),
            (" ```bash`", 1),
            ("   ```bash`", 3),
            ("本文中の ```bash`", 0),
        ):
            assert len(prose_line) - len(prose_line.lstrip(" ")) == indent
            if prose_line.lstrip(" ").startswith("```") and prose_line.endswith("`"):
                assert prose_line.lstrip(" ")[3:] == "bash`"
            body = base.replace("## Outcome\n", prose_line + "\n## Outcome\n", 1)
            result = guard_template(body, "implementation", template_dir=template_dir)
            assert result["missing_sections"] == [], prose_line

        # EOF 未閉 fence は最後まで code。中の ## Outcome は実見出しではない。
        unclosed = base.replace("## Outcome\n", "## REMOVED\n", 1)
        unclosed += "\n```bash\n## Outcome\n"
        result = guard_template(unclosed, "implementation", template_dir=template_dir)
        assert "## Outcome" in result["missing_sections"]

    def test_shared_parser_delegation_without_regex(self, template_dir, monkeypatch):
        """GIVEN parser block 種別 WHEN guard_template THEN CODE_FENCE のみを除外する。"""
        assert "_FENCED_CODE_BLOCK_RE" not in _MODULE_PATH.read_text(encoding="utf-8")
        assert "_pbp.iter_markdown_blocks" in inspect.getsource(_mod._strip_fenced_code_blocks)
        body = make_implementation_body().replace("## Outcome\n", "## REMOVED\n", 1)
        seen = []

        def blocks_without_outcome(text):
            seen.append(text)
            yield body, _mod._pbp.BLOCK_KIND_HUMAN_PROSE
            yield "## Outcome\n", _mod._pbp.BLOCK_KIND_CODE_FENCE

        monkeypatch.setattr(_mod._pbp, "iter_markdown_blocks", blocks_without_outcome)
        result = guard_template(body, "implementation", template_dir=template_dir)
        assert seen == [body]
        assert "## Outcome" in result["missing_sections"]

        def blocks_with_nonfence_outcome(text):
            seen.append(text)
            yield body, _mod._pbp.BLOCK_KIND_HUMAN_PROSE
            yield "## Outcome\n", _mod._pbp.BLOCK_KIND_CODE_FENCE
            yield "## Outcome\n", _mod._pbp.BLOCK_KIND_TABLE

        monkeypatch.setattr(_mod._pbp, "iter_markdown_blocks", blocks_with_nonfence_outcome)
        result = guard_template(body, "implementation", template_dir=template_dir)
        assert seen == [body, body]
        assert result["missing_sections"] == []


# ---------------------------------------------------------------------------
# guard_ac_vc_alignment のテスト（#99 AC1/AC3 対応）
# ---------------------------------------------------------------------------

class TestGuardAcVcAlignment:
    def test_parent_returns_skipped_true(self, template_dir):
        """GIVEN parent 種別 WHEN guard_ac_vc_alignment THEN skipped=True を返す"""
        body = make_parent_body()
        result = guard_ac_vc_alignment(body, "parent", template_dir=template_dir)
        assert result["passed"] is True
        assert result["skipped"] is True
        assert "reason" in result

    def test_implementation_not_skipped_ac_vc_match(self, template_dir):
        """GIVEN implementation 種別 で AC/VC が一致 WHEN guard_ac_vc_alignment THEN passed=True, skipped=False"""
        body = make_implementation_body()
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is True

    def test_implementation_not_skipped_ac_vc_mismatch(self, template_dir):
        """GIVEN implementation 種別 で AC 2 件・VC に # AC コメントなし WHEN guard_ac_vc_alignment THEN passed=False"""
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

- [ ] AC1 テスト1
- [ ] AC2 テスト2

## Verification Commands

```bash
pnpm test
```
"""
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is False
        assert result["ac_count"] == 2
        assert result["vc_ac_count"] == 0

    def test_research_not_skipped(self, template_dir):
        """GIVEN research 種別（VC required あり）WHEN guard_ac_vc_alignment THEN skipped=False"""
        body = make_research_body()
        result = guard_ac_vc_alignment(body, "research", template_dir=template_dir)
        assert result["skipped"] is False

    def test_parent_skipped_even_with_ac_vc_content(self, template_dir):
        """GIVEN parent 種別で AC VC コンテンツが存在する WHEN guard_ac_vc_alignment THEN skipped=True"""
        body = make_parent_body() + "\n# AC1\n# AC2\n"
        result = guard_ac_vc_alignment(body, "parent", template_dir=template_dir)
        assert result["passed"] is True
        assert result["skipped"] is True

    def test_implementation_zero_ac_passes(self, template_dir):
        """GIVEN implementation 種別で AC が 0 件 WHEN guard_ac_vc_alignment THEN passed=True（ゼロ AC は許容）"""
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

なし
"""
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is True
        assert result["ac_count"] == 0


# ---------------------------------------------------------------------------
# Finding 2: validate_issue_kind のテスト（パストラバーサル対策）
# ---------------------------------------------------------------------------

class TestValidateIssueKind:
    def test_valid_kind_passes(self):
        """GIVEN 有効な issue_kind WHEN validate_issue_kind THEN 例外なし"""
        # 例外が出なければ OK
        validate_issue_kind("implementation")
        validate_issue_kind("research")
        validate_issue_kind("parent")
        validate_issue_kind("my-kind")
        validate_issue_kind("kind_123")

    def test_path_traversal_rejected(self):
        """GIVEN '../etc' のようなパストラバーサル文字列 WHEN validate_issue_kind THEN ValueError"""
        with pytest.raises(ValueError, match="Invalid issue_kind"):
            validate_issue_kind("../etc")

    def test_slash_rejected(self):
        """GIVEN スラッシュを含む文字列 WHEN validate_issue_kind THEN ValueError"""
        with pytest.raises(ValueError, match="Invalid issue_kind"):
            validate_issue_kind("a/b")

    def test_dot_prefix_rejected(self):
        """GIVEN ドットで始まる文字列 WHEN validate_issue_kind THEN ValueError"""
        with pytest.raises(ValueError, match="Invalid issue_kind"):
            validate_issue_kind(".hidden")

    def test_empty_string_rejected(self):
        """GIVEN 空文字列 WHEN validate_issue_kind THEN ValueError"""
        with pytest.raises(ValueError, match="Invalid issue_kind"):
            validate_issue_kind("")


# ---------------------------------------------------------------------------
# Finding 3: extract_issue_kind_from_body の仕様一致テスト
# ---------------------------------------------------------------------------

class TestExtractIssueKindFromBodyFinding3:
    def test_no_contract_schema_version_returns_none(self):
        """GIVEN contract_schema_version がない yaml ブロック WHEN extract_issue_kind_from_body THEN None"""
        body = """\
## Machine-Readable Contract

```yaml
issue_kind: implementation
```
"""
        result = extract_issue_kind_from_body(body)
        assert result is None

    def test_wrong_schema_version_returns_none(self):
        """GIVEN contract_schema_version が v1 以外 WHEN extract_issue_kind_from_body THEN None"""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v2
issue_kind: implementation
```
"""
        result = extract_issue_kind_from_body(body)
        assert result is None

    def test_yaml_outside_mrc_section_not_extracted(self):
        """GIVEN MRC セクション外の yaml ブロックのみ WHEN extract_issue_kind_from_body THEN None"""
        body = """\
## Some Other Section

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため



## Machine-Readable Contract

（MRC セクションに yaml ブロックなし）
"""
        result = extract_issue_kind_from_body(body)
        assert result is None

    def test_mrc_section_with_v1_and_issue_kind_extracted(self):
        """GIVEN MRC セクション内に contract_schema_version: v1 + issue_kind WHEN extract THEN 正しく返す"""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
```
"""
        result = extract_issue_kind_from_body(body)
        assert result == "research"


# ---------------------------------------------------------------------------
# Finding 4: Template Guard の fenced code block 内偽陽性テスト
# ---------------------------------------------------------------------------

class TestGuardTemplateFinding4:
    def test_outcome_in_fenced_block_not_mistaken_as_section(self, template_dir):
        """GIVEN fenced code block 内に ## Outcome がある WHEN guard_template THEN 見出しと誤認しない"""
        # fenced code block 内に ## Outcome があるが、実際の ## Outcome セクションはない body
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため



## Parent Issue

none

## Parent Goal Ref

- Goal: テスト

## Current Validated Scope

テスト

## Remaining Parent Gaps

なし

```markdown
## Outcome

ここは fenced block 内の偽見出し
```

## In Scope

- テスト

## Out of Scope

- 対象外

## Acceptance Criteria

- [ ] AC1 テスト

## Verification Commands

```bash
# AC1
pnpm test
```

## Allowed Paths

- tests/

## Stop Conditions

- Allowed Paths 外の変更が必要な場合

## Required Skills

- pytest
"""
        result = guard_template(body, "implementation", template_dir=template_dir)
        # fenced block 内の ## Outcome は実際のセクションではないため missing に含まれる
        assert result["passed"] is False
        assert "## Outcome" in result["missing_sections"]

    def test_real_outcome_section_passes(self, template_dir):
        """GIVEN 行頭の正規 ## Outcome セクションがある WHEN guard_template THEN pass する"""
        body = make_implementation_body()
        result = guard_template(body, "implementation", template_dir=template_dir)
        assert result["passed"] is True

    def test_outcome_in_blockquote_not_counted(self, template_dir):
        """GIVEN fenced block 内の ## Outcome のみで実際のセクションがない WHEN guard_template THEN fail"""
        # fenced code block 内に ## Outcome があるだけで実際のセクションがない
        body_without_outcome = make_implementation_body().replace("## Outcome\n", "## REMOVED\n")
        # fenced block に ## Outcome を追加
        body_with_fake = body_without_outcome + "\n```\n## Outcome\nfake\n```\n"
        result = guard_template(body_with_fake, "implementation", template_dir=template_dir)
        assert result["passed"] is False
        assert "## Outcome" in result["missing_sections"]


# ---------------------------------------------------------------------------
# Finding 5: AC/VC alignment の集合一致テスト
# ---------------------------------------------------------------------------

class TestGuardAcVcAlignmentFinding5:
    def test_duplicate_vc_ac1_passes_as_bundled_commands(self, template_dir):
        """GIVEN # AC1 が VC に複数回ある（1 AC に複数 VC コマンドを束ねる正当なパターン）
        WHEN guard_ac_vc_alignment THEN 集合一致で pass する（#1694: false positive 解消）"""
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

- [ ] AC1 テスト1

## Verification Commands

```bash
# AC1
pnpm test
# AC1
pnpm test --watch
```
"""
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        # AC: {1}, VC: {1, 1} -> set({1}) == set({1}) -> pass（集合一致・重複は許容）
        assert result["passed"] is True

    def test_vc_ac_outside_vc_section_not_counted(self, template_dir):
        """GIVEN ## Verification Commands セクション外の # AC1 WHEN guard_ac_vc_alignment THEN カウントしない"""
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

- [ ] AC1 テスト1

## Some Other Section

```bash
# AC1
echo "this should not be counted"
```

## Verification Commands

```bash
# AC1
pnpm test
```
"""
        # VC セクション内に # AC1 が 1 つ → AC 番号 [1] と一致 → pass
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is True

    def test_ac1_and_ac2_matching_passes(self, template_dir):
        """GIVEN AC1/AC2 と VC # AC1/# AC2 が対応 WHEN guard_ac_vc_alignment THEN pass"""
        body = make_implementation_body()
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["passed"] is True

    def test_ac1_missing_in_vc_fails(self, template_dir):
        """GIVEN AC1/AC2 があるが VC は # AC2 のみ WHEN guard_ac_vc_alignment THEN fail"""
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

- [ ] AC1 テスト1
- [ ] AC2 テスト2

## Verification Commands

```bash
# AC2
pnpm test
```
"""
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is False

    def test_vc_ac_numbers_outside_section_ignored(self, template_dir):
        """GIVEN AC なし VC セクション外に # AC1 WHEN guard_ac_vc_alignment THEN AC=0 で pass"""
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

なし

## Other Section

```bash
# AC1
not in VC section
```
"""
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        # AC が 0 件なので pass
        assert result["passed"] is True
        assert result["ac_count"] == 0


# ---------------------------------------------------------------------------
# guard_vc_compound_shell_disallowed のテスト (#445 AC2/AC3 対応)
# ---------------------------------------------------------------------------

def test_guard_issue_body_rejects_compound_shell_vc():
    """
    operator matrix テスト。各ケースを個別に検証する。
    """
    # --- case 1: cmd && echo PASS → FAIL（&& operator 検出）---
    compound_body = """\
## Verification Commands

```bash
# AC1
cmd && echo PASS || echo FAIL
```
"""
    result = guard_vc_compound_shell_disallowed(compound_body)
    assert result["name"] == "vc_compound_shell_disallowed"
    assert result["passed"] is False
    assert len(result["violations"]) >= 1
    # violations は structured diagnostics 形式
    v0 = result["violations"][0]
    assert v0["command"] == "cmd && echo PASS || echo FAIL"
    assert v0["category"] == "compound_command_disallowed"
    assert v0["operator"] in ("&&", "||")  # 最初に検出したもの
    assert v0["ac_label"] == "AC1"
    assert "line_number" in v0
    assert isinstance(v0["line_number"], int)

    # --- case 2: grep -E "foo|bar" file → PASS（quoted operator は誤検出しない）---
    quoted_pipe_body = """\
## Verification Commands

```bash
# AC2
grep -E "foo|bar" file
```
"""
    result_quoted = guard_vc_compound_shell_disallowed(quoted_pipe_body)
    assert result_quoted["name"] == "vc_compound_shell_disallowed"
    assert result_quoted["passed"] is True
    assert result_quoted["violations"] == []

    # --- case 3: unmatched quote (parse error) → FAIL（fail-closed）---
    parse_error_body = """\
## Verification Commands

```bash
# AC3
cmd "foo
```
"""
    result_parse = guard_vc_compound_shell_disallowed(parse_error_body)
    assert result_parse["name"] == "vc_compound_shell_disallowed"
    assert result_parse["passed"] is False
    assert len(result_parse["violations"]) >= 1
    v_parse = result_parse["violations"][0]
    assert v_parse["operator"] == "_parse_error"
    assert v_parse["category"] == "compound_command_disallowed"

    # --- case 4: cmd ; cmd2 → FAIL（; operator）---
    semicolon_body = """\
## Verification Commands

```bash
# AC4
cmd ; cmd2
```
"""
    result_semi = guard_vc_compound_shell_disallowed(semicolon_body)
    assert result_semi["name"] == "vc_compound_shell_disallowed"
    assert result_semi["passed"] is False
    assert len(result_semi["violations"]) >= 1
    v_semi = result_semi["violations"][0]
    assert v_semi["operator"] == ";"
    assert v_semi["category"] == "compound_command_disallowed"

    # --- case 5: violations に ac_label / line_number / operator が含まれることを検証 ---
    structured_body = """\
## Verification Commands

```bash
# AC5
bad_cmd && other_cmd
```
"""
    result_structured = guard_vc_compound_shell_disallowed(structured_body)
    assert result_structured["passed"] is False
    v_s = result_structured["violations"][0]
    assert "ac_label" in v_s
    assert "line_number" in v_s
    assert "operator" in v_s
    assert v_s["ac_label"] == "AC5"
    assert v_s["operator"] == "&&"

    # --- single command fixture: compound operator なし（既存テスト継続）---
    single_body = """\
## Verification Commands

```bash
# AC1
grep -F "VC_SINGLE_COMMAND_GUARDRAIL" .claude/agents/issue-author.md
```
"""
    result_single = guard_vc_compound_shell_disallowed(single_body)
    assert result_single["name"] == "vc_compound_shell_disallowed"
    assert result_single["passed"] is True
    assert result_single["violations"] == []


def _make_vc_body(command: str) -> str:
    """単一コマンドを含む VC セクション body を生成するヘルパー。"""
    return f"""\
## Verification Commands

```bash
# AC1
{command}
```
"""


def test_guard_vc_compound_shell_b4_reject_punctuation_run_tokens():
    """
    B4: shlex punctuation run token（>&, &>, |& 等）を REJECT することを確認する。
    """
    # 2>&1 → shlex が >&  を punctuation run として返す
    result = guard_vc_compound_shell_disallowed(_make_vc_body("cmd 2>&1"))
    assert result["passed"] is False, "cmd 2>&1 should be rejected (>& is punctuation run)"
    assert len(result["violations"]) >= 1
    assert result["violations"][0]["category"] == "compound_command_disallowed"

    # &> /dev/null → &> が punctuation run
    result = guard_vc_compound_shell_disallowed(_make_vc_body("cmd &> /dev/null"))
    assert result["passed"] is False, "cmd &> /dev/null should be rejected (&> is punctuation run)"

    # |& tee log → |& が punctuation run
    result = guard_vc_compound_shell_disallowed(_make_vc_body("cmd |& tee log"))
    assert result["passed"] is False, "cmd |& tee log should be rejected (|& is punctuation run)"

    # > outfile → > は exact match
    result = guard_vc_compound_shell_disallowed(_make_vc_body("cmd > outfile"))
    assert result["passed"] is False, "cmd > outfile should be rejected (> is exact match)"

    # < infile → < は exact match
    result = guard_vc_compound_shell_disallowed(_make_vc_body("cmd < infile"))
    assert result["passed"] is False, "cmd < infile should be rejected (< is exact match)"

    # cat << EOF → << は exact match（heredoc）
    result = guard_vc_compound_shell_disallowed(_make_vc_body("cat << EOF"))
    assert result["passed"] is False, "cat << EOF should be rejected (<< is exact match)"


def test_guard_vc_compound_shell_b4_pass_quoted_operators():
    """
    B4 false positive 防止: quoted operator は誤検出しないことを確認する。
    """
    # grep -E "foo|bar" file → quoted | は誤検出しない
    result = guard_vc_compound_shell_disallowed(_make_vc_body('grep -E "foo|bar" file'))
    assert result["passed"] is True, 'grep -E "foo|bar" should pass (quoted |)'

    # rg 'pattern' file → single quote 内
    result = guard_vc_compound_shell_disallowed(_make_vc_body("rg 'pattern' file"))
    assert result["passed"] is True, "rg 'pattern' file should pass"

    # test -f .claude/agents/issue-author.md → ファイルパスの - は問題なし
    result = guard_vc_compound_shell_disallowed(
        _make_vc_body("test -f .claude/agents/issue-author.md")
    )
    assert result["passed"] is True, "test -f <path> should pass (- is not shell operator)"

    # grep -F "2>&1" file → quoted 2>&1 は誤検出しない
    result = guard_vc_compound_shell_disallowed(_make_vc_body('grep -F "2>&1" file'))
    assert result["passed"] is True, 'grep -F "2>&1" should pass (quoted 2>&1)'


def test_guard_vc_compound_shell_b3_inline_ac_suffix():
    """
    B3: inline # ACN suffix からも ac_label を抽出することを確認する。
    """
    # inline suffix のみ（直前行に # AC<N> なし）
    inline_body = """\
## Verification Commands

```bash
grep -F "VC_SINGLE_COMMAND_GUARDRAIL" .claude/agents/issue-author.md  # AC1
```
"""
    result = guard_vc_compound_shell_disallowed(inline_body)
    # このコマンド自体は compound でないので passed=True
    assert result["passed"] is True
    assert result["violations"] == []

    # inline suffix を持つ compound コマンド → violation の ac_label が抽出されること
    inline_compound_body = """\
## Verification Commands

```bash
cmd && echo done  # AC2
```
"""
    result = guard_vc_compound_shell_disallowed(inline_compound_body)
    assert result["passed"] is False
    assert len(result["violations"]) >= 1
    # inline suffix から ac_label が抽出される
    assert result["violations"][0]["ac_label"] == "AC2"
    # command から inline suffix が除去されていること
    assert "# AC2" not in result["violations"][0]["command"]


# ---------------------------------------------------------------------------
# check_ready_tuple / guard_ready_tuple tests (AC5, AC10, AC12)
# ---------------------------------------------------------------------------

class TestCheckReadyTuple:
    """Tests for check_ready_tuple() function — AC5, AC10, AC12."""

    @pytest.mark.parametrize("issue_kind,title,label_names,expected_pass", [
        # AC5 case 1: implementation + 実装: prefix + phase/implementation => pass
        (
            "implementation",
            "実装: foo bar",
            ["phase/implementation", "enhancement"],
            True,
        ),
        # AC5 case 2: implementation + implement: prefix + phase/implementation => pass
        (
            "implementation",
            "implement: foo bar",
            ["phase/implementation"],
            True,
        ),
        # AC5 case 3: implementation + non-compliant title + phase/implementation => fail
        (
            "implementation",
            "feat: foo",
            ["phase/implementation"],
            False,
        ),
        # AC10 case: implementation + 実装: prefix + NO phase/implementation => fail
        (
            "implementation",
            "実装: foo",
            ["enhancement"],
            False,
        ),
        # AC5 case 4: research kind => pass (LP031/ready_tuple not applicable)
        (
            "research",
            "調査: foo",
            [],
            True,
        ),
        # AC5 case 5: research kind with no phase/implementation => pass
        (
            "research",
            "調査: foo",
            ["phase/implementation"],
            True,
        ),
        # non-implementation kind (parent) => pass
        (
            "parent",
            "なんでもよい",
            [],
            True,
        ),
        # None kind => pass (no check)
        (
            None,
            "feat: foo",
            [],
            True,
        ),
    ])
    def test_check_ready_tuple_parametrized(self, issue_kind, title, label_names, expected_pass):
        """Parametrized test for check_ready_tuple() covering all AC5/AC10 cases."""
        errors = check_ready_tuple(issue_kind, title, label_names)
        if expected_pass:
            assert errors == [], f"Expected PASS but got errors: {errors}"
        else:
            assert len(errors) > 0, "Expected FAIL but got no errors"

    def test_check_ready_tuple_title_error_message_contains_got(self):
        """Error message for title mismatch should contain the actual title."""
        errors = check_ready_tuple("implementation", "chore: cleanup", ["phase/implementation"])
        assert len(errors) == 1
        assert "chore: cleanup" in errors[0]

    def test_check_ready_tuple_label_error_message_contains_label_name(self):
        """Error message for missing label should mention 'phase/implementation'."""
        errors = check_ready_tuple("implementation", "実装: foo", [])
        assert len(errors) == 1
        assert "phase/implementation" in errors[0]

    def test_check_ready_tuple_both_errors_when_both_missing(self):
        """When both title prefix and label are wrong, two errors are returned."""
        errors = check_ready_tuple("implementation", "feat: foo", ["enhancement"])
        assert len(errors) == 2

    def test_check_ready_tuple_empty_labels_list(self):
        """Empty label list for implementation kind should fail."""
        errors = check_ready_tuple("implementation", "実装: bar", [])
        assert len(errors) == 1
        assert "phase/implementation" in errors[0]


class TestGuardReadyTuple:
    """Tests for guard_ready_tuple() — AC12 output schema integration."""

    def test_guard_ready_tuple_schema_pass(self):
        """guard_ready_tuple returns {name, passed, errors} schema when passing."""
        result = guard_ready_tuple("implementation", "実装: foo", ["phase/implementation"])
        assert result["name"] == "ready_tuple"
        assert result["passed"] is True
        assert result["errors"] == []

    def test_guard_ready_tuple_schema_fail(self):
        """guard_ready_tuple returns {name, passed, errors} schema when failing."""
        result = guard_ready_tuple("implementation", "feat: foo", ["phase/implementation"])
        assert result["name"] == "ready_tuple"
        assert result["passed"] is False
        assert len(result["errors"]) > 0

    def test_guard_ready_tuple_non_implementation_always_pass(self):
        """Non-implementation kind always returns passed=True."""
        result = guard_ready_tuple("research", "調査: foo", [])
        assert result["name"] == "ready_tuple"
        assert result["passed"] is True
        assert result["errors"] == []


# ---------------------------------------------------------------------------
# --readback-json path validation and schema validation tests (Blocker 1)
# ---------------------------------------------------------------------------

class TestReadbackJsonPathValidation:
    """Tests for --readback-json validate_path type and schema validation.

    validate_path rejects unsafe chars via argparse, returning SystemExit.
    Schema validation returns structured guard result on malformed JSON.
    """

    def test_readback_json_rejects_unsafe_path_semicolon(self, tmp_path):
        """GIVEN a path with semicolon WHEN passed as --readback-json THEN SystemExit (argparse rejects)."""
        body_file = tmp_path / "body.md"
        body_file.write_text("## Outcome\ntest\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                _MODULE_PATH_STR,
                str(body_file),
                "--issue-kind", "implementation",
                "--check-ready-tuple",
                "--readback-json", "/tmp/foo;bar.json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, (
            f"Expected exit 2 (argparse error) for unsafe path, got {result.returncode}. "
            f"stderr={result.stderr!r}"
        )

    def test_readback_json_rejects_unsafe_path_space(self, tmp_path):
        """GIVEN a path with a space WHEN passed as --readback-json THEN SystemExit (argparse rejects)."""
        body_file = tmp_path / "body.md"
        body_file.write_text("## Outcome\ntest\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                _MODULE_PATH_STR,
                str(body_file),
                "--issue-kind", "implementation",
                "--check-ready-tuple",
                "--readback-json", "/tmp/foo bar.json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, (
            f"Expected exit 2 for path with space, got {result.returncode}. "
            f"stderr={result.stderr!r}"
        )

    def test_readback_json_rejects_unsafe_path_shell_meta(self, tmp_path):
        """GIVEN a path with shell metachar ($) WHEN passed as --readback-json THEN argparse rejects."""
        body_file = tmp_path / "body.md"
        body_file.write_text("## Outcome\ntest\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                _MODULE_PATH_STR,
                str(body_file),
                "--issue-kind", "implementation",
                "--check-ready-tuple",
                "--readback-json", "/tmp/$foo.json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, (
            f"Expected exit 2 for path with $, got {result.returncode}. "
            f"stderr={result.stderr!r}"
        )

    def test_readback_json_malformed_not_dict(self, tmp_path):
        """GIVEN readback JSON where root is a list WHEN guard runs THEN error result returned."""
        body_file = tmp_path / "body.md"
        body_file.write_text("## Outcome\ntest\n", encoding="utf-8")
        rb_file = tmp_path / "readback.json"
        rb_file.write_text(json.dumps([{"title": "実装: foo"}]), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                _MODULE_PATH_STR,
                str(body_file),
                "--issue-kind", "implementation",
                "--check-ready-tuple",
                "--readback-json", str(rb_file),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        output = json.loads(result.stdout)
        assert output["all_passed"] is False
        errors = output["guards"][0]["errors"]
        assert any("malformed readback JSON" in e for e in errors)

    def test_readback_json_missing_labels_key(self, tmp_path):
        """GIVEN readback JSON without 'labels' key WHEN guard runs THEN defaults to empty labels (no error)."""
        body_file = tmp_path / "body.md"
        body_file.write_text("## Outcome\ntest\n", encoding="utf-8")
        rb_file = tmp_path / "readback.json"
        # title present but no labels key -> defaults to [] -> passes schema validation
        rb_file.write_text(json.dumps({"title": "実装: foo"}), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                _MODULE_PATH_STR,
                str(body_file),
                "--issue-kind", "implementation",
                "--check-ready-tuple",
                "--readback-json", str(rb_file),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
        )
        # Schema is valid (missing labels defaults to []), but ready_tuple itself may fail
        # because phase/implementation label is absent — that's a logic fail, not schema error
        output = json.loads(result.stdout)
        guards = {g["name"]: g for g in output["guards"]}
        if "ready_tuple" in guards:
            # errors should NOT mention "malformed readback JSON"
            for e in guards["ready_tuple"].get("errors", []):
                assert "malformed readback JSON" not in e

    def test_readback_json_label_not_dict(self, tmp_path):
        """GIVEN readback JSON where a label element is not a dict WHEN guard runs THEN error result."""
        body_file = tmp_path / "body.md"
        body_file.write_text("## Outcome\ntest\n", encoding="utf-8")
        rb_file = tmp_path / "readback.json"
        rb_file.write_text(
            json.dumps({"title": "実装: foo", "labels": ["phase/implementation"]}),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                _MODULE_PATH_STR,
                str(body_file),
                "--issue-kind", "implementation",
                "--check-ready-tuple",
                "--readback-json", str(rb_file),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        output = json.loads(result.stdout)
        assert output["all_passed"] is False
        errors = output["guards"][0]["errors"]
        assert any("malformed readback JSON" in e for e in errors)


def _make_invalid_lp016_body() -> str:
    """LP016 で NG になる不正 marker を含む最小の本文。"""
    return """\
## Acceptance Criteria

- [ ] AC1: テスト

## Verification Commands

```bash
# AC1: description
test -f /etc/passwd
```

## Allowed Paths

- .
"""


class TestIssueBodyValidationGuard:
    """Tests for edit-issue issue body pre-validator guard."""

    def test_guard_issue_body_validation_passes(self, tmp_path):
        body_file = tmp_path / "valid.md"
        body_file.write_text(make_implementation_body(), encoding="utf-8")

        result = guard_issue_body_validation(body_file, "implementation")
        assert result["name"] == "issue_body_validation"
        assert result["passed"] is True
        assert result["status"] == "pass"

    def test_guard_issue_body_validation_reports_lp016(self, tmp_path):
        body_file = tmp_path / "invalid.md"
        body_file.write_text(_make_invalid_lp016_body(), encoding="utf-8")

        result = guard_issue_body_validation(body_file, "implementation")
        assert result["name"] == "issue_body_validation"
        assert result["passed"] is False
        errors = result.get("errors", [])
        assert any(err.get("rule_id") == "LP016" for err in errors)
