#!/usr/bin/env python3
"""Tests for semantic_review_trigger.py (Issue #2296 AC2).

Covers each explicit signal's true/false contribution to
``semantic_review_applicable`` independently, and confirms the classifier
performs no before/after body comparison (no ``--previous-body-file``
option exists at all -- P0-2).
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "scripts")
)

import pytest  # noqa: E402
import semantic_review_trigger as trig  # noqa: E402


def test_all_false_signals_not_applicable():
    """GIVEN all explicit signals are false/zero/empty
    WHEN evaluate_semantic_review_applicable runs
    THEN semantic_review_applicable is False and triggered_by is empty."""
    result = trig.evaluate_semantic_review_applicable({})
    assert result["semantic_review_applicable"] is False
    assert result["triggered_by"] == []


def test_user_requested_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable({"user_requested": True})
    assert result["semantic_review_applicable"] is True
    assert "user_requested" in result["triggered_by"]


def test_semantic_rewrite_requested_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"semantic_rewrite_requested": True}
    )
    assert result["semantic_review_applicable"] is True
    assert "semantic_rewrite_requested" in result["triggered_by"]


def test_checker_gap_count_zero_does_not_trigger():
    result = trig.evaluate_semantic_review_applicable({"checker_gap_count": 0})
    assert result["semantic_review_applicable"] is False


def test_checker_gap_count_positive_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable({"checker_gap_count": 1})
    assert result["semantic_review_applicable"] is True
    assert "checker_gap_count" in result["triggered_by"]


def test_heuristic_concern_count_positive_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"heuristic_concern_count": 3}
    )
    assert result["semantic_review_applicable"] is True
    assert "heuristic_concern_count" in result["triggered_by"]


def test_severity_tagged_anchor_findings_non_empty_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"severity_tagged_anchor_findings": ["P0-1"]}
    )
    assert result["semantic_review_applicable"] is True
    assert "severity_tagged_anchor_findings" in result["triggered_by"]


def test_owner_decision_conflict_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"owner_decision_conflict": True}
    )
    assert result["semantic_review_applicable"] is True
    assert "owner_decision_conflict" in result["triggered_by"]


def test_cross_contract_change_schema_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"cross_contract_change": {"schema": True}}
    )
    assert result["semantic_review_applicable"] is True
    assert "cross_contract_change" in result["triggered_by"]


def test_cross_contract_change_protocol_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"cross_contract_change": {"protocol": True}}
    )
    assert result["semantic_review_applicable"] is True


def test_cross_contract_change_orchestration_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"cross_contract_change": {"orchestration": True}}
    )
    assert result["semantic_review_applicable"] is True


def test_cross_contract_change_all_false_does_not_trigger():
    result = trig.evaluate_semantic_review_applicable(
        {
            "cross_contract_change": {
                "schema": False,
                "protocol": False,
                "orchestration": False,
            }
        }
    )
    assert result["semantic_review_applicable"] is False


def test_no_before_after_diff_cli_option_exists():
    """P0-2: the classifier must not implement a before/after comparison
    entrypoint (no --previous-body-file CLI argument)."""
    parser = trig._build_arg_parser()
    dest_names = {action.dest for action in parser._actions}
    assert "previous_body_file" not in dest_names


def test_multiple_signals_all_recorded_in_triggered_by():
    result = trig.evaluate_semantic_review_applicable(
        {"user_requested": True, "checker_gap_count": 2}
    )
    assert result["semantic_review_applicable"] is True
    assert set(result["triggered_by"]) == {"user_requested", "checker_gap_count"}


def test_string_false_boolean_is_rejected_not_coerced_true():
    """P1-3: bool("false") == True in Python -- a JSON string "false" must
    be rejected, never silently coerced to True."""
    import pytest

    with pytest.raises(ValueError):
        trig.evaluate_semantic_review_applicable({"user_requested": "false"})


def test_string_true_boolean_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        trig.evaluate_semantic_review_applicable({"owner_decision_conflict": "true"})


def test_negative_checker_gap_count_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        trig.evaluate_semantic_review_applicable({"checker_gap_count": -1})


def test_negative_heuristic_concern_count_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        trig.evaluate_semantic_review_applicable({"heuristic_concern_count": -3})


def test_unknown_top_level_key_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        trig.evaluate_semantic_review_applicable({"totally_unknown_field": True})


def test_unknown_cross_contract_change_key_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        trig.evaluate_semantic_review_applicable(
            {"cross_contract_change": {"schema": True, "unknown_nested": True}}
        )


def test_build_semantic_review_trigger_input_counts_gaps_and_concerns():
    raw = trig.build_semantic_review_trigger_input(
        deterministic_checker_gaps=["gap1", "gap2"],
        heuristic_concerns=["concern1"],
    )
    result = trig.evaluate_semantic_review_applicable(raw)
    assert result["semantic_review_applicable"] is True
    assert "checker_gap_count" in result["triggered_by"]
    assert "heuristic_concern_count" in result["triggered_by"]


def test_build_semantic_review_trigger_input_extracts_severity_tags_from_anchor_bodies():
    raw = trig.build_semantic_review_trigger_input(
        anchor_comment_bodies=["## P0-1\n\nSomething is broken.\n\n- fix it"]
    )
    assert raw["severity_tagged_anchor_findings"] == ["P0-1"]
    result = trig.evaluate_semantic_review_applicable(raw)
    assert result["semantic_review_applicable"] is True
    assert "severity_tagged_anchor_findings" in result["triggered_by"]


def test_build_semantic_review_trigger_input_all_defaults_not_applicable():
    raw = trig.build_semantic_review_trigger_input()
    result = trig.evaluate_semantic_review_applicable(raw)
    assert result["semantic_review_applicable"] is False


# ---------------------------------------------------------------------------
# Issue #2994: HTML-only / bracketed severity headings (trusted OWNER review)
# ---------------------------------------------------------------------------
# The fixture below mirrors the *shape* of the observed #2836 OWNER anchor
# (comment 6058171888): a CF_HTML clipboard envelope (outer html/body +
# StartFragment ... first EndFragment) that carries HTML only -- there is no
# Markdown tail after <!--EndFragment--> -- and whose finding headings look like
# ``<h3><span>1. </span><span>[P1] </span><span>...</span></h3>`` (leading
# number, text split across several inline spans, bracketed severity). The body
# text is shortened/anonymized; the structure is what matters.

_OWNER_HTML_ONLY_ANCHOR = (
    "<html>\n<body>\n"
    "<!--StartFragment--><html><head></head><body><div><div></div><div><div>"
    "<h1><span>Issue </span><span>#2836 </span><span>敵対的レビュー</span></h1>"
    "<div>P1 — 実装前の契約修正を推奨</div>"
    "<p><span>判定：</span><span>修正の優先度は高い。</span></p>"
    "<h2><span>Findings</span></h2>"
    "<h3><span>1. </span><span>[P1] </span><span>Required-check </span>"
    "<span>inventory </span><span>の正本が未定義</span></h3>"
    "<p><span>問題</span></p>"
    "<h3><span>2. </span><span>[P1] </span><span>stale run </span>"
    "<span>の扱い</span></h3>"
    "<p><span>詳細</span></p>"
    "</div></div></div></body></html><!--EndFragment-->\n"
    "</body>\n</html>"
)

_ONLY_SEVERITY_SIGNAL = ["severity_tagged_anchor_findings"]
_SEVERITY_OFF = {
    "user_requested": False,
    "semantic_rewrite_requested": False,
    "checker_gap_count": False,
    "heuristic_concern_count": False,
    "owner_decision_conflict": False,
    "cross_contract_change": False,
}


def _trigger_for_anchor(body):
    """Build the trigger input from ONE raw anchor body (no hand-set flags)
    and evaluate it through the canonical producer + evaluator."""
    raw = trig.build_semantic_review_trigger_input(anchor_comment_bodies=[body])
    return raw, trig.evaluate_semantic_review_applicable(raw)


def test_owner_html_only_bracketed_p1_triggers_semantic_review():
    """GIVEN an HTML-only CF_HTML fragment shaped like the #2836 OWNER anchor
    (numbered <h3>, several inline spans, [P1], no Markdown tail)
    WHEN severity tags are extracted and the canonical trigger is evaluated
    THEN tags == ["P1"] and semantic_review_applicable is True, triggered
    only by severity_tagged_anchor_findings (no hand-set user_requested)."""
    import scope_signal_delta as delta

    # The fixture must keep the observed structure (not be sanitized away).
    assert "<span>1. </span><span>[P1] </span>" in _OWNER_HTML_ONLY_ANCHOR
    assert _OWNER_HTML_ONLY_ANCHOR.count("[P1]") == 2
    after_end = _OWNER_HTML_ONLY_ANCHOR.split("<!--EndFragment-->", 1)[1]
    assert after_end.strip() == "</body>\n</html>"  # no Markdown tail
    assert "#" not in _OWNER_HTML_ONLY_ANCHOR.replace("#2836", "")

    assert delta.extract_severity_tags(_OWNER_HTML_ONLY_ANCHOR) == ["P1"]

    raw, result = _trigger_for_anchor(_OWNER_HTML_ONLY_ANCHOR)
    assert raw["user_requested"] is False
    assert raw["severity_tagged_anchor_findings"] == ["P1"]
    assert result["semantic_review_applicable"] is True
    assert result["triggered_by"] == _ONLY_SEVERITY_SIGNAL


_ENV_OPEN = "<html>\n<body>\n<!--StartFragment-->"
_ENV_CLOSE = "<!--EndFragment-->\n</body>\n</html>"

_POSITIVE_SEVERITY_MATRIX = [
    ("markdown_p1_1", "### P1-1\n\nSomething is broken.\n", ["P1-1"]),
    ("markdown_p0_1", "## P0-1\n\n- fix it\n", ["P0-1"]),
    ("markdown_bracketed_p1", "### [P1]\n\nBody.\n", ["P1"]),
    ("markdown_numbered_bracketed", "### 1. [P1] Title\n\nBody.\n", ["P1"]),
    ("markdown_lowercase_bracketed", "### [p2] Title\n", ["P2"]),
    ("html_only_owner_numbered_bracketed", _OWNER_HTML_ONLY_ANCHOR, ["P1"]),
    (
        "html_only_p1_1_existing_form",
        _ENV_OPEN + "<h3><span>P1-1 — Title</span></h3>" + _ENV_CLOSE,
        ["P1-1"],
    ),
    (
        "html_only_entities_and_inline_spans",
        _ENV_OPEN
        + "<h2><span>2) </span><span>&#91;P2&#93; </span><span>A &amp; B</span></h2>"
        + _ENV_CLOSE,
        ["P2"],
    ),
    (
        "html_only_details_with_legit_heading",
        _ENV_OPEN
        + "<details><summary>x</summary><h3><span>[P0] </span><span>Blocker</span></h3></details>"
        + _ENV_CLOSE,
        ["P0"],
    ),
    (
        "complete_envelope_markdown_tail_heading",
        _ENV_OPEN + "<p>intro</p>" + "<!--EndFragment-->\n</body>\n</html>\n\n### P1-2\n\nTail.\n",
        ["P1-2"],
    ),
    (
        "complete_envelope_html_and_markdown_tail",
        _ENV_OPEN
        + "<h3><span>[P1] </span><span>x</span></h3>"
        + "<!--EndFragment-->\n</body>\n</html>\n\n## P0-1\n",
        ["P0-1", "P1"],
    ),
    (
        "incomplete_envelope_real_markdown_heading_companion",
        _ENV_OPEN + "<h3><span>[P1] </span><span>x</span></h3>\n\n### P1-1\n\nreal.\n",
        ["P1-1"],
    ),
    (
        "fence_contains_comment_opener_heading_after_fence_counts",
        "```\n<!--\n```\n### P1-1\n",
        ["P1-1"],
    ),
    (
        "comment_contains_fence_opener_is_ignored",
        "<!--\n```\n-->\n### P1-1\n",
        ["P1-1"],
    ),
    (
        "heading_on_same_line_after_comment_close",
        "<!--\nnote\n--> ### P1-1\n",
        ["P1-1"],
    ),
    (
        "heading_after_closed_fence_and_closed_comment",
        "```\n### P0-9\n```\n<!-- ### P0-8 -->\n### P2-1\n",
        ["P2-1"],
    ),
    (
        "details_with_markdown_heading_stays_detected",
        "<details>\n<summary>x</summary>\n\n### P1-1\n\nbody\n</details>\n",
        ["P1-1"],
    ),
    # OWNER review on PR #3011 (P1-1 / P2-1): symbols written as literal text,
    # not syntax, must not hide a real heading that follows.
    (
        "closed_code_span_with_comment_opener_then_heading",
        "開始記号 `<!--` の処理を確認する。\n\n### P1-1 実在する不具合\n",
        ["P1-1"],
    ),
    (
        "escaped_comment_opener_then_heading",
        "開始記号 \\<!-- の処理を確認する。\n\n### P1-1 実在する不具合\n",
        ["P1-1"],
    ),
    (
        "double_backtick_code_span_with_comment_opener_then_heading",
        "``a <!-- b`` text\n\n### P1-1 x\n",
        ["P1-1"],
    ),
    (
        "triple_backtick_inline_code_span_line_then_heading",
        "```literal```\n\n### P1-1 実在する不具合\n",
        ["P1-1"],
    ),
    (
        "backtick_info_string_with_backtick_is_not_fence_then_heading",
        "```text `example`\n\n### P1-1 実在する不具合\n",
        ["P1-1"],
    ),
    (
        "tilde_info_string_with_backtick_is_still_fence_then_heading_after",
        "~~~ text `x`\n### P0-9\n~~~\n### P2-1\n",
        ["P2-1"],
    ),
    (
        "closed_code_span_comment_then_real_comment_hides_inner_heading",
        "`<!--` ok <!--\n### P0-9\n-->\n### P2-1\n",
        ["P2-1"],
    ),
    (
        "four_backtick_fence_with_inner_three_backtick_line_then_heading",
        "````\n```\n### P0-9\n````\n### P1-1\n",
        ["P1-1"],
    ),
]


def test_severity_positive_matrix_triggers_only_severity_signal():
    """GIVEN each supported Markdown / HTML-only severity heading form
    WHEN the canonical trigger is built and evaluated
    THEN the expected tags are extracted, semantic_review_applicable is True
    and triggered_by contains ONLY severity_tagged_anchor_findings (all other
    signals stay false)."""
    import scope_signal_delta as delta

    for label, body, expected in _POSITIVE_SEVERITY_MATRIX:
        assert delta.extract_severity_tags(body) == expected, label
        raw, result = _trigger_for_anchor(body)
        assert raw["severity_tagged_anchor_findings"] == expected, label
        assert result["semantic_review_applicable"] is True, label
        assert result["triggered_by"] == _ONLY_SEVERITY_SIGNAL, label
        breakdown = dict(result["signal_breakdown"])
        assert breakdown.pop("severity_tagged_anchor_findings") is True, label
        assert breakdown == _SEVERITY_OFF, label


_NEGATIVE_SEVERITY_MATRIX = [
    ("general_prose_with_bracketed_p1", "This is prose. We discussed [P1] items today.\n"),
    ("bold_bracketed_not_heading", "**[P1]** bold text, not a heading\n"),
    ("list_item_not_heading", "- [ ] P1-1 item in a task list\n"),
    ("embedded_bracketed_in_heading", "### Notes about [P1] policy\n\nBody.\n"),
    ("heading_prose_p1_without_dash_number", "### P1 policy discussion\n"),
    (
        "raw_html_heading_without_envelope",
        "<h3><span>1. </span><span>[P1] </span><span>Finding</span></h3>\n",
    ),
    ("backtick_fence", "```\n### P1-1\n```\n"),
    ("tilde_fence", "~~~\n### P1-1\n~~~\n"),
    ("unclosed_fence_until_eof", "intro\n\n```\n### P1-1\nmore\n"),
    ("nested_four_backtick_fence", "````\n```\n### P1-1\n```\n````\n"),
    ("multiline_html_comment", "<!--\n### P1-1\n-->\n"),
    ("single_line_html_comment", "<!-- ### P1-1 -->\n"),
    (
        "real_unclosed_comment_after_closed_code_span_runs_to_eof",
        "`code` then <!--\n### P1-1\nmore\n",
    ),
    ("tilde_fence_info_string_with_backtick", "~~~ a`b\n### P1-1\n~~~\n"),
    ("unclosed_html_comment_until_eof", "text\n<!--\n### P1-1\nmore\n"),
    (
        "unrelated_html_and_gfm_structures",
        "<details><summary>x</summary>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n</details>\n",
    ),
    (
        "complete_envelope_pre_code_line_start_heading",
        _ENV_OPEN
        + "<pre><code>\n### P1-1\n</code></pre><p>unrelated</p>"
        + _ENV_CLOSE,
    ),
    (
        "complete_envelope_unrelated_details",
        _ENV_OPEN
        + "<details><summary>x</summary><p>[P1] mentioned in prose</p></details>"
        + _ENV_CLOSE,
    ),
    (
        "incomplete_envelope_html_heading_only",
        _ENV_OPEN + "<h3><span>1. </span><span>[P1] </span><span>x</span></h3>",
    ),
]


def test_severity_negative_matrix_does_not_trigger():
    """GIVEN inputs that must NOT be read as severity findings (plain prose,
    embedded [P1], envelope-less raw HTML, fenced / commented headings,
    <pre><code> inside an envelope, incomplete envelope with HTML heading only)
    WHEN the canonical trigger is built and evaluated
    THEN no severity signal is produced and semantic_review_applicable stays
    False with an empty triggered_by (other signals fixed to false/0)."""
    import scope_signal_delta as delta

    for label, body in _NEGATIVE_SEVERITY_MATRIX:
        assert delta.extract_severity_tags(body) == [], label
        raw, result = _trigger_for_anchor(body)
        assert raw["severity_tagged_anchor_findings"] == [], label
        assert raw["checker_gap_count"] == 0, label
        assert raw["heuristic_concern_count"] == 0, label
        assert result["semantic_review_applicable"] is False, label
        assert result["triggered_by"] == [], label


def test_fence_opener_limited_rules_four_space_indent_and_container_prefix():
    """The fence scanner follows only the basic GFM opener rule (0-3 space
    indent). A 4-space-indented opener and a ``> `` container-prefixed opener
    are NOT fence openers, so a real heading that follows is still detected.
    (Container structure is intentionally not tracked.)"""
    import scope_signal_delta as delta

    four_space = "    ```\n### P1-1\n"
    container = "> ```\n### P1-1\n"
    assert delta.extract_severity_tags(four_space) == ["P1-1"]
    assert delta.extract_severity_tags(container) == ["P1-1"]
    # control: a genuine 3-space-indented opener DOES start a fence.
    assert delta.extract_severity_tags("   ```\n### P1-1\n   ```\n") == []


# ---------------------------------------------------------------------------
# Issue #3016: trusted OWNER review ``P<n>:`` / ``P<n>：`` headings
# ---------------------------------------------------------------------------
# Each case is an independent raw anchor body passed to the actual
# build_semantic_review_trigger_input() -> evaluate_semantic_review_applicable()
# (no hand-set trigger flags). Envelope cases are complete CF_HTML envelopes.


def _colon_envelope(fragment, tail=""):
    body = _ENV_OPEN + fragment + _ENV_CLOSE
    return body + "\n\n" + tail if tail else body


_COLON_TRIGGER_CASES = [
    # (body, expected severity_tagged_anchor_findings)
    pytest.param("### 1. P2：全角コロンの指摘\n\n本文。\n", ["P2"], id="P1"),
    pytest.param("### P2: ASCII colon finding\n\nBody.\n", ["P2"], id="P2"),
    pytest.param("### 1. P2: ASCII colon with number\n", ["P2"], id="P3a"),
    pytest.param("### P2：全角コロン番号なし\n", ["P2"], id="P3b"),
    pytest.param(
        _colon_envelope("<h3>1. P2：全角の指摘</h3><p>詳細</p>", "ordinary prose only.\n"),
        ["P2"],
        id="P4",
    ),
    pytest.param(
        _colon_envelope("<h3>P2: ASCII finding</h3>", "ordinary prose only.\n"),
        ["P2"],
        id="P5",
    ),
    pytest.param(
        _colon_envelope("<h3>1. P2：全角の指摘</h3>", "### 1. P2：全角の指摘\n"),
        ["P2"],
        id="P6",
    ),
    pytest.param(
        _colon_envelope(
            "<h3>1. P2：全角の指摘</h3>",
            "Markdown tail は ordinary prose のみ。P2: という語を含む文章。\n",
        ),
        ["P2"],
        id="P7",
    ),
    pytest.param(
        _colon_envelope("<h3>概要</h3><p>severity なし</p>", "### P2: tail finding\n"),
        ["P2"],
        id="P8",
    ),
    pytest.param(
        _colon_envelope("<h3>1. P2：全角の指摘</h3>", "### P3: tail finding\n"),
        ["P2", "P3"],
        id="P9",
    ),
    # negatives: no severity signal at all
    pytest.param("P2: ordinary prose paragraph.\n\nP2：全角の通常文。\n", [], id="N1a"),
    pytest.param("P2: ASCII の通常文だけの段落。\n", [], id="N1b"),
    pytest.param("P2：全角の通常文だけの段落。\n", [], id="N1c"),
    pytest.param(
        _colon_envelope("<p>P2: ordinary prose</p>", "P2：ordinary prose tail.\n"),
        [],
        id="N1d",
    ),
    pytest.param(_colon_envelope("<p>P2：段落であり見出しではない</p>"), [], id="N2"),
    pytest.param("説明文に `P2：コード` を含む段落。\n", [], id="N3a"),
    pytest.param("### `P2: code span heading`\n", [], id="N3b"),
    pytest.param("`### P2：見出し風のコードスパン`\n", [], id="N3c"),
    pytest.param("```\n### P2：fence 内\n```\n", [], id="N4a"),
    pytest.param("~~~\n### P2: fence 内\n~~~\n", [], id="N4b"),
    pytest.param("<!--\n### P2：comment 内\n-->\n", [], id="N5"),
    pytest.param("### Notes about P2: heading\n", [], id="N6a"),
    pytest.param("### 1. See P2：中途の言及\n", [], id="N6b"),
    pytest.param(_colon_envelope("<h3>Notes about P2: heading</h3>"), [], id="N7a"),
    pytest.param(_colon_envelope("<h3>1. See P2：中途の言及</h3>"), [], id="N7b"),
    pytest.param(_colon_envelope("<pre><h3>1. P2：pre 内</h3></pre>"), [], id="N8a"),
    pytest.param(
        _colon_envelope('<script>var s = "<h3>P2: script 内</h3>";</script>'),
        [],
        id="N8b",
    ),
    pytest.param(
        _colon_envelope("<style>/* <h3>1. P2：style 内</h3> */</style>"), [], id="N8c"
    ),
]


@pytest.mark.parametrize(("body", "expected"), _COLON_TRIGGER_CASES)
def test_owner_colon_semantic_trigger(body, expected):
    """GIVEN one independent raw OWNER anchor body (colon-form severity
    heading, or a near-miss that must not count)
    WHEN it goes through the actual build_semantic_review_trigger_input() and
    evaluate_semantic_review_applicable() with every other signal at default
    THEN positives set severity_tagged_anchor_findings and trigger only that
    signal; negatives leave semantic_review_applicable False."""
    raw, result = _trigger_for_anchor(body)
    assert raw["user_requested"] is False
    assert raw["severity_tagged_anchor_findings"] == expected
    if expected:
        assert result["semantic_review_applicable"] is True
        assert result["triggered_by"] == _ONLY_SEVERITY_SIGNAL
        breakdown = dict(result["signal_breakdown"])
        assert breakdown.pop("severity_tagged_anchor_findings") is True
        assert breakdown == _SEVERITY_OFF
    else:
        assert result["semantic_review_applicable"] is False
        assert result["triggered_by"] == []


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
