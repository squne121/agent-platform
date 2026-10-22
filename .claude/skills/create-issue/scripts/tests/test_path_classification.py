"""Tests for path_classification (Issue #1135 P1a).

Verifies the repo-specific path policy SSOT used by the C12 docs-only gate:
classification is not .md-extension-only, code/runtime scopes are detected, and
docs-only is conservative.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import path_classification as pc  # noqa: E402


@pytest.mark.parametrize(
    "path,expected",
    [
        # documentation
        ("docs/product/requirements.md", pc.DOCUMENTATION),
        ("CLAUDE.md", pc.DOCUMENTATION),
        (".claude/skills/review-issue/SKILL.md", pc.DOCUMENTATION),
        ("docs/product/", pc.DOCUMENTATION),
        ("`docs/dev/current-focus.md`", pc.DOCUMENTATION),  # backtick-wrapped
        # code / runtime
        ("src/runtime.ts", pc.CODE_RUNTIME),
        ("src/**", pc.CODE_RUNTIME),
        ("package.json", pc.CODE_RUNTIME),
        ("pnpm-lock.yaml", pc.CODE_RUNTIME),
        ("pyproject.toml", pc.CODE_RUNTIME),
        ("scripts/build.py", pc.CODE_RUNTIME),
        ("scripts/*.py", pc.CODE_RUNTIME),
        (".github/workflows/ci.yml", pc.CODE_RUNTIME),
        (".claude/skills/create-issue/scripts/validate_issue_body.py", pc.CODE_RUNTIME),
        ("src/components/App.tsx", pc.CODE_RUNTIME),
    ],
)
def test_classify_path(path, expected):
    assert pc.classify_path(path) == expected, (
        f"{path!r} expected {expected}, got {pc.classify_path(path)}"
    )


def test_md_extension_not_sole_signal():
    """A .py under .claude/** is code/runtime even though .claude also holds docs."""
    assert pc.classify_path(".claude/skills/x/scripts/y.py") == pc.CODE_RUNTIME
    assert pc.classify_path(".claude/skills/x/SKILL.md") == pc.DOCUMENTATION


def test_extract_allowed_paths():
    body = (
        "## Allowed Paths\n\n"
        "- `docs/dev/current-focus.md`\n"
        "- `src/runtime.ts`\n\n"
        "## Stop Conditions\n\n- 1\n"
    )
    assert pc.extract_allowed_paths(body) == [
        "docs/dev/current-focus.md",
        "src/runtime.ts",
    ]


def test_is_docs_only_allowed_paths_true_for_all_docs():
    assert pc.is_docs_only_allowed_paths(["docs/dev/current-focus.md", "CLAUDE.md"]) is True


def test_is_docs_only_allowed_paths_false_with_code():
    assert pc.is_docs_only_allowed_paths(["docs/x.md", "src/runtime.ts"]) is False


def test_is_docs_only_allowed_paths_false_when_empty():
    assert pc.is_docs_only_allowed_paths([]) is False


def test_has_code_or_runtime_scope():
    assert pc.has_code_or_runtime_scope(["docs/x.md", "src/runtime.ts"]) is True
    assert pc.has_code_or_runtime_scope(["docs/x.md", "CLAUDE.md"]) is False


def test_extract_allowed_paths_no_path_marker_returns_empty():
    """AC12 (Issue #2783): canonical marker `(none)` and legacy marker
    `読み取り専用。リポジトリ変更なし（既定）` as the sole Allowed Paths entry
    both resolve to []."""
    body_canonical = "## Allowed Paths\n\n- (none)\n\n## Stop Conditions\n\n- 1\n"
    assert pc.extract_allowed_paths(body_canonical) == []

    body_legacy = (
        "## Allowed Paths\n\n"
        "- 読み取り専用。リポジトリ変更なし（既定）\n\n"
        "## Stop Conditions\n\n- 1\n"
    )
    assert pc.extract_allowed_paths(body_legacy) == []
