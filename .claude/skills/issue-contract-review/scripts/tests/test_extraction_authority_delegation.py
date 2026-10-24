"""
test_extraction_authority_delegation.py

Issue #2782 AC7 / AC8:

`baseline_vc_preflight.py::extract_verification_commands_section()`（AC7）と
`contract_readiness_check.py::_extract_section_by_canonical_name()`（AC8）が、
`prose_boundary_policy.py` の新設 shared helper
（`extract_level2_section()` / `extract_level2_section_with_bounds()`）へ
実際に委譲しており、section boundary algorithm（fence-aware 見出し境界判定
ループ）の独立コピーをそれぞれのファイル自身に持たないことを固定する回帰
テスト。

AC7 はさらに、`baseline_vc_preflight.py` から `contract_readiness_check.py`
への逆 import が発生しないこと（循環依存回避）も固定する
（`contract_readiness_check.py` は既に `baseline_vc_preflight` から
`extract_verification_commands_section` 等を import しているため、逆方向の
import が発生すると import 時点で循環依存になる）。

Runtime Verification Applicability: not_applicable
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent
# _SCRIPTS_DIR = .../.claude/skills/issue-contract-review/scripts
# parents: [0]=issue-contract-review, [1]=skills, [2]=.claude
_CREATE_ISSUE_SCRIPTS_DIR = (
    _SCRIPTS_DIR.parents[1] / "create-issue" / "scripts"
)

_BASELINE_VC_PREFLIGHT_PATH = _SCRIPTS_DIR / "baseline_vc_preflight.py"
_CONTRACT_READINESS_CHECK_PATH = _SCRIPTS_DIR / "contract_readiness_check.py"
_PROSE_BOUNDARY_POLICY_PATH = _CREATE_ISSUE_SCRIPTS_DIR / "prose_boundary_policy.py"

for _p in (str(_SCRIPTS_DIR), str(_CREATE_ISSUE_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _import_and_verify_module(name: str, expected_path: Path):
    """Import `name` through the ordinary import machinery and confirm it
    resolved to the expected repo file.

    This intentionally does NOT force-replace any pre-existing
    `sys.modules[name]` entry with a freshly constructed module object --
    it relies on Python's normal import cache, so a module already
    imported earlier in this same process (including via the bare
    `import baseline_vc_preflight` / `import prose_boundary_policy`
    statements inside `contract_readiness_check.py` and
    `baseline_vc_preflight.py` themselves) resolves to the very same
    object this test module imports.

    The `module.__file__` check below only *detects* -- it never repairs
    -- an accidental `sys.modules` collision with an unrelated same-named
    module loaded from a different path earlier in a shared pytest
    session; if that happens, the assertion fails loudly instead of
    silently testing the wrong file.
    """
    module = importlib.import_module(name)
    actual_path = Path(module.__file__).resolve()
    assert actual_path == expected_path.resolve(), (
        f"{name!r} resolved to {actual_path}, expected {expected_path} "
        "(sys.modules likely already holds an unrelated same-named module "
        "from a different path -- this test does not force-replace it)"
    )
    return module


_prose_boundary_policy = _import_and_verify_module(
    "prose_boundary_policy", _PROSE_BOUNDARY_POLICY_PATH
)
_baseline_vc_preflight = _import_and_verify_module(
    "baseline_vc_preflight", _BASELINE_VC_PREFLIGHT_PATH
)
_contract_readiness_check = _import_and_verify_module(
    "contract_readiness_check", _CONTRACT_READINESS_CHECK_PATH
)


def _module_top_level_import_targets(path: Path) -> set[str]:
    """Return the set of dotted module names referenced by `import X` /
    `from X import ...` statements at any level in the file's AST."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                targets.add(node.module)
    return targets


def _tree_references_name_as_identifier(tree: ast.AST, name: str) -> bool:
    """Return True if `name` is referenced as a live Python identifier
    (an `ast.Name` load/use, or the attribute name of an `ast.Attribute`
    access) anywhere in `tree`.

    This is deliberately narrower than a raw substring-in-source check: a
    comment or a docstring that merely *mentions* `name` does not produce
    an `ast.Name` / `ast.Attribute` node, so it does not trip this check.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
    return False


def _tree_defines_function(tree: ast.AST, func_name: str) -> bool:
    """Return True if `tree` contains a (sync or async) function
    definition named `func_name`."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return True
    return False


def _file_tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _file_references_name_as_identifier(path: Path, name: str) -> bool:
    return _tree_references_name_as_identifier(_file_tree(path), name)


def _file_defines_function(path: Path, func_name: str) -> bool:
    return _tree_defines_function(_file_tree(path), func_name)


# ---------------------------------------------------------------------------
# AC7: baseline_vc_preflight.py delegates to the shared helper and does not
# import contract_readiness_check.py (no circular dependency)
# ---------------------------------------------------------------------------


class TestBaselineVcPreflightDelegation:
    def test_baseline_vc_preflight_imports_shared_prose_boundary_policy_helper(self):
        targets = _module_top_level_import_targets(_BASELINE_VC_PREFLIGHT_PATH)
        assert "prose_boundary_policy" in targets, (
            "baseline_vc_preflight.py must import the section extraction "
            "authority from prose_boundary_policy.py"
        )

    def test_baseline_vc_preflight_does_not_import_contract_readiness_check(self):
        targets = _module_top_level_import_targets(_BASELINE_VC_PREFLIGHT_PATH)
        assert "contract_readiness_check" not in targets, (
            "baseline_vc_preflight.py must not import contract_readiness_check "
            "(contract_readiness_check.py already imports baseline_vc_preflight; "
            "the reverse import would form a cycle)"
        )

    def test_baseline_vc_preflight_has_no_independent_heading_boundary_primitives(self):
        """AC7: baseline_vc_preflight.py's own source no longer references the
        heading-boundary primitives as live identifiers -- it only calls the
        shared prose_boundary_policy helper, so it cannot hold an independent
        copy of the section-boundary algorithm.

        This is an AST-based structural check (not a raw substring-in-source
        check): a comment or docstring merely mentioning the forbidden name
        does not trip it (see
        TestAstStructuralGateIgnoresProseOccurrences below)."""
        assert not _file_references_name_as_identifier(
            _BASELINE_VC_PREFLIGHT_PATH, "parse_atx_heading_line"
        )
        assert not _file_references_name_as_identifier(
            _BASELINE_VC_PREFLIGHT_PATH, "lookup_heading_policy"
        )

    def test_baseline_vc_preflight_delegates_actual_call_to_shared_helper(self):
        """Functional delegation check: patching
        prose_boundary_policy.extract_level2_section() (the exact object
        baseline_vc_preflight.py imported a reference to) changes
        extract_verification_commands_section()'s output -- proving the
        call flows through the shared helper rather than a locally
        reimplemented algorithm."""
        sentinel = "SENTINEL_FROM_SHARED_HELPER"
        calls: list[tuple[str, str]] = []

        def _fake_extract_level2_section(body: str, canonical_en: str):
            calls.append((body, canonical_en))
            return sentinel

        body = "## Verification Commands\n\nreal content\n"
        with patch.object(
            _baseline_vc_preflight,
            "_extract_level2_section",
            _fake_extract_level2_section,
        ):
            result = _baseline_vc_preflight.extract_verification_commands_section(body)

        assert calls == [(body, "Verification Commands")]
        assert result == sentinel


# ---------------------------------------------------------------------------
# AC8: contract_readiness_check.py delegates to the shared helper and has no
# independent fence-aware section boundary loop
# ---------------------------------------------------------------------------


class TestContractReadinessCheckDelegation:
    def test_contract_readiness_check_imports_shared_prose_boundary_policy_helper(self):
        targets = _module_top_level_import_targets(_CONTRACT_READINESS_CHECK_PATH)
        assert "prose_boundary_policy" in targets

    def test_contract_readiness_check_has_no_independent_heading_boundary_primitives(self):
        """AC8: contract_readiness_check.py's own source no longer
        references the heading-boundary primitives as live identifiers (it
        used to call parse_atx_heading_line()/lookup_heading_policy() in its
        own fence-aware loop before delegating to the shared helper), and no
        longer defines its own `_fenced_line_indices()`.

        This is an AST-based structural check (not a raw
        substring-in-source check): a comment or docstring merely
        mentioning the forbidden name/def does not trip it (see
        TestAstStructuralGateIgnoresProseOccurrences below)."""
        assert not _file_references_name_as_identifier(
            _CONTRACT_READINESS_CHECK_PATH, "parse_atx_heading_line"
        )
        assert not _file_references_name_as_identifier(
            _CONTRACT_READINESS_CHECK_PATH, "lookup_heading_policy"
        )
        assert not _file_defines_function(
            _CONTRACT_READINESS_CHECK_PATH, "_fenced_line_indices"
        ), (
            "contract_readiness_check.py must not define its own "
            "_fenced_line_indices() -- that primitive now lives only in "
            "prose_boundary_policy.py"
        )

    def test_contract_readiness_check_delegates_actual_call_to_shared_helper(self):
        """Functional delegation check: patching
        prose_boundary_policy.extract_level2_section_with_bounds() (the
        exact object contract_readiness_check.py imported a reference to)
        changes _extract_section_by_canonical_name()'s output."""
        sentinel = ("SENTINEL_TEXT", 3, 7)
        calls: list[tuple[str, str]] = []

        def _fake_extract_level2_section_with_bounds(body: str, canonical_en: str):
            calls.append((body, canonical_en))
            return sentinel

        body = "## Verification Commands\n\nreal content\n"
        with patch.object(
            _contract_readiness_check,
            "_extract_level2_section_with_bounds",
            _fake_extract_level2_section_with_bounds,
        ):
            result = _contract_readiness_check._extract_section_by_canonical_name(
                body, "Verification Commands"
            )

        assert calls == [(body, "Verification Commands")]
        assert result == sentinel

    def test_contract_readiness_check_import_direction_toward_preflight_module_preserved(self):
        """Existing import direction (contract_readiness_check.py ->
        preflight module) is preserved (Issue #2782 explicitly keeps this
        direction; only the reverse direction is disallowed)."""
        targets = _module_top_level_import_targets(_CONTRACT_READINESS_CHECK_PATH)
        assert "baseline_vc_preflight" in targets


# ---------------------------------------------------------------------------
# Pin: the AST-based structural gate above must not false-positive on a
# comment/docstring that merely mentions a forbidden name/def -- unlike a
# raw substring-in-source check, which would.
# ---------------------------------------------------------------------------


class TestAstStructuralGateIgnoresProseOccurrences:
    _FIXTURE_SOURCE = (
        '"""This docstring mentions parse_atx_heading_line and '
        'lookup_heading_policy only as prose, never as a call or a '
        'definition. It also mentions _fenced_line_indices in prose."""\n'
        "\n"
        "# another comment referencing lookup_heading_policy and "
        "parse_atx_heading_line\n"
        "\n"
        "def _fenced_line_indices_is_a_different_identifier():\n"
        "    return 1\n"
    )

    def test_prose_only_mentions_do_not_trip_the_identifier_check(self):
        tree = ast.parse(self._FIXTURE_SOURCE)
        assert not _tree_references_name_as_identifier(tree, "parse_atx_heading_line")
        assert not _tree_references_name_as_identifier(tree, "lookup_heading_policy")

    def test_prose_only_mention_does_not_trip_the_function_definition_check(self):
        tree = ast.parse(self._FIXTURE_SOURCE)
        assert not _tree_defines_function(tree, "_fenced_line_indices")
