"""
test_bilingual_heading.py

Issue #654 AC4: bilingual heading が check_issue_contract.py の
extract_section() で canonical section として抽出できることを検証する。
"""

import sys
from pathlib import Path

import pytest

# check_issue_contract.py は review-issue/scripts/ にある
_REVIEW_SCRIPTS = Path(__file__).parent.parent
sys.path.insert(0, str(_REVIEW_SCRIPTS))

from check_issue_contract import extract_section


# ===========================================================================
# AC4: bilingual heading の extract_section 認識
# ===========================================================================


class TestExtractSectionBilingual:
    """AC4: bilingual heading（半角・全角括弧）を extract_section で認識する"""

    def test_extract_section_bilingual_halfwidth(self):
        """GIVEN: ## 成果物 (Outcome) heading WHEN: extract_section(body, "Outcome")
        THEN: 本文が抽出できる（半角括弧）"""
        body = (
            "## 成果物 (Outcome)\n\n"
            "これは成果物セクションの本文です。\n\n"
            "## 背景 (Background)\n\n"
            "背景セクション\n"
        )
        result = extract_section(body, "Outcome")
        assert result != "", "bilingual heading '## 成果物 (Outcome)' を Outcome として抽出できなかった"
        assert "成果物セクションの本文" in result

    def test_extract_section_bilingual_fullwidth(self):
        """GIVEN: ## 成果物（Outcome）heading WHEN: extract_section(body, "Outcome")
        THEN: 本文が抽出できる（全角括弧）"""
        body = (
            "## 成果物（Outcome）\n\n"
            "これは成果物セクションの本文です。\n\n"
            "## 背景（Background）\n\n"
            "背景セクション\n"
        )
        result = extract_section(body, "Outcome")
        assert result != "", "bilingual heading '## 成果物（Outcome）' を Outcome として抽出できなかった"
        assert "成果物セクションの本文" in result

    def test_extract_section_canonical_english_still_works(self):
        """GIVEN: ## Outcome（英語正規形）WHEN: extract_section(body, "Outcome")
        THEN: 引き続き抽出できる（既存動作の維持）"""
        body = "## Outcome\n\nOutcome content here.\n\n## Background\n\nBackground.\n"
        result = extract_section(body, "Outcome")
        assert result == "Outcome content here."

    def test_extract_section_multiple_bilingual(self):
        """GIVEN: bilingual heading が複数あるボディ WHEN: 各セクションを抽出
        THEN: それぞれ正しく抽出できる"""
        body = (
            "## 成果物 (Outcome)\n\n"
            "成果物の内容。\n\n"
            "## 受け入れ条件 (Acceptance Criteria)\n\n"
            "- [ ] AC1\n"
            "- [ ] AC2\n\n"
            "## 停止条件 (Stop Conditions)\n\n"
            "- 条件1\n"
        )
        outcome = extract_section(body, "Outcome")
        assert "成果物の内容" in outcome

        ac = extract_section(body, "Acceptance Criteria")
        assert "AC1" in ac

        sc = extract_section(body, "Stop Conditions")
        assert "条件1" in sc

    def test_indented_bilingual_heading_extract_section(self):
        """GIVEN: '   ## 成果物 (Outcome)'（3 spaces indent）WHEN: extract_section
        THEN: Outcome セクションが抽出できる（B2 fix_delta: GFM ATX leading spaces 対応）"""
        body = (
            "   ## 成果物 (Outcome)\n\n"
            "インデント付き bilingual 見出しの内容。\n\n"
            "## 背景 (Background)\n\n"
            "背景セクション\n"
        )
        result = extract_section(body, "Outcome")
        assert result != "", (
            "3 spaces indent の '   ## 成果物 (Outcome)' は extract_section で Outcome として認識されるべき"
        )
        assert "インデント付き bilingual 見出しの内容" in result

    def test_closing_hash_heading_extract_section(self):
        """GIVEN: '## 成果物 (Outcome) ##'（closing hash）WHEN: extract_section
        THEN: Outcome セクションが抽出できる（B2 fix_delta: GFM ATX closing # 対応）"""
        body = (
            "## 成果物 (Outcome) ##\n\n"
            "closing hash 付き bilingual 見出しの内容。\n\n"
            "## 背景 (Background)\n\n"
            "背景セクション\n"
        )
        result = extract_section(body, "Outcome")
        assert result != "", (
            "'## 成果物 (Outcome) ##' は closing hash があっても extract_section で Outcome として認識されるべき"
        )
        assert "closing hash 付き bilingual 見出しの内容" in result

    def test_extract_section_not_found_returns_empty(self):
        """GIVEN: 存在しないセクション名 WHEN: extract_section
        THEN: 空文字列を返す"""
        body = "## Outcome\n\nContent.\n"
        result = extract_section(body, "NonExistentSection")
        assert result == ""

    def test_extract_section_stops_at_next_heading(self):
        """GIVEN: bilingual heading が続く文書 WHEN: extract_section
        THEN: 次の ## で終わる（範囲が正しい）"""
        body = (
            "## 成果物 (Outcome)\n\n"
            "This is outcome content.\n\n"
            "## 背景 (Background)\n\n"
            "This is background.\n"
        )
        result = extract_section(body, "Outcome")
        assert "background" not in result.lower(), (
            "Outcome セクションに Background の内容が混入してはならない"
        )

    def test_extract_section_all_bilingual_heading_policy(self):
        """GIVEN: HEADING_POLICY の全 entry について bilingual heading を含む body
        WHEN: extract_section
        THEN: 各セクションが正しく抽出できる"""
        # create-issue/scripts をパスに追加して HEADING_POLICY を import
        _create_scripts = Path(__file__).parent.parent.parent.parent / "create-issue" / "scripts"
        if str(_create_scripts) not in sys.path:
            sys.path.insert(0, str(_create_scripts))

        try:
            from prose_boundary_policy import HEADING_POLICY
        except ImportError:
            pytest.skip("prose_boundary_policy not available")

        for canonical_en, entry in HEADING_POLICY.items():
            # accepted_forms の bilingual 形式（括弧付き）を試す
            bilingual_forms = [
                f for f in entry["accepted_forms"]
                if "(" in f or "（" in f
            ]
            for form in bilingual_forms:
                body = f"## {form}\n\nContent for {canonical_en}.\n"
                result = extract_section(body, canonical_en)
                assert result != "", (
                    f"bilingual form '## {form}' の extract_section(body, '{canonical_en}') が失敗"
                )
