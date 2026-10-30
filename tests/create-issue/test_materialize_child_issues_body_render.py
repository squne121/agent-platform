"""AC2: canonical body rendering is spec-driven (ISSUE_TEMPLATE label order) and the
rendered body passes validate_issue_body.py --kind --title.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import materialize_child_issues as m
import pytest

SCRIPTS_DIR = Path(m.__file__).resolve().parent
VALIDATOR = SCRIPTS_DIR / "validate_issue_body.py"


def _sections_in_order(body: str) -> list[str]:
    return [ln[3:].strip() for ln in body.splitlines() if ln.startswith("## ")]


def test_render_uses_template_label_order(valid_child):
    body = m.render_canonical_body(valid_child, parent_issue=254)
    rendered_sections = _sections_in_order(body)
    # Spec-driven: the rendered section order must equal the template's required label
    # order, NOT a hardcoded list. Compare against the validator's own loader.
    expected = m.required_section_labels("implementation")
    assert rendered_sections == expected
    # And the order must actually come from the template file (sanity: MRC first).
    assert rendered_sections[0] == "Machine-Readable Contract"


def test_render_order_tracks_template_not_hardcoded(valid_child, monkeypatch):
    # If the template loader returns a different order, the render must follow it.
    fake_order = [
        "Machine-Readable Contract",
        "Outcome",
        "Acceptance Criteria",
        "Verification Commands",
        "Allowed Paths",
    ]
    monkeypatch.setattr(m, "required_section_labels", lambda kind: list(fake_order))
    body = m.render_canonical_body(valid_child, parent_issue=254)
    assert _sections_in_order(body) == fake_order


def test_rendered_body_contains_structured_content(valid_child):
    body = m.render_canonical_body(valid_child, parent_issue=254)
    assert "- [ ] AC1" in body and "- [ ] AC2" in body
    assert "uv run pytest tests/foo_test.py -q" in body
    assert "- `src/foo.ts`" in body
    assert "issue_kind: implementation" in body
    assert "#254" in body


def test_rendered_body_passes_real_validator(valid_child):
    body = m.render_canonical_body(valid_child, parent_issue=254)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(body)
        body_file = f.name
    try:
        cp = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                "--body-file",
                body_file,
                "--kind",
                "implementation",
                "--title",
                valid_child["title"],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(body_file).unlink(missing_ok=True)
    assert cp.returncode == 0, f"validator failed: {cp.stdout}\n{cp.stderr}"


def test_mrc_yaml_safe_with_special_chars(valid_child):
    # Medium 1: a goal_ref containing YAML-significant chars must not break the MRC YAML.
    valid_child["sections"]["goal_ref"] = 'tricky: "value" with: colons # and hash'
    body = m.render_canonical_body(valid_child, parent_issue=254)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(body)
        bf = f.name
    try:
        cp = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                "--body-file",
                bf,
                "--kind",
                "implementation",
                "--title",
                valid_child["title"],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(bf).unlink(missing_ok=True)
    assert cp.returncode == 0, f"validator failed on special-char goal_ref: {cp.stdout}"


def test_strict_template_loader_fails_closed_on_missing_kind():
    # Medium 2: the materializer loader fail-closes (no silent downgrade) for a kind whose
    # template file does not exist.
    with pytest.raises(m.PlanValidationError, match="not found"):
        m.required_section_labels("nonexistent_kind")


def test_research_kind_renders_research_template_order(valid_child):
    # Research kind must render the research template's required order, proving the render
    # is parameterized by kind (spec-driven) rather than implementation-only.
    research_child = dict(valid_child)
    research_child["kind"] = "research"
    research_child["title"] = "調査: overlap helper"
    body = m.render_canonical_body(research_child, parent_issue=254)
    assert _sections_in_order(body) == m.required_section_labels("research")
