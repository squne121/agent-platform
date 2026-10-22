"""
test_issue_kind_ssot.py

Tests for ISSUE_KIND_POLICY_V1 SSOT integration.

AC1: plan_refinement_loop.py has no local ISSUE_KIND_ALLOWLIST definition
AC2: check_issue_contract.py has no local issue_kind allowlist definition
AC3: detect_issue_kind("design") does not return "implementation"
AC4: detect_issue_kind("foobar") does not silently return "implementation"
AC5: docs/dev/github-ops.md contains ISSUE_KIND_POLICY_V1
"""

from __future__ import annotations

import re
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
REVIEW_SCRIPTS = REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
REFINEMENT_SCRIPTS = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
GITHUB_OPS_MD = REPO_ROOT / "docs" / "dev" / "github-ops.md"

sys.path.insert(0, str(REVIEW_SCRIPTS))
sys.path.insert(0, str(REFINEMENT_SCRIPTS))


# ---------------------------------------------------------------------------
# AC1: plan_refinement_loop.py has no local ISSUE_KIND_ALLOWLIST
# ---------------------------------------------------------------------------

def test_plan_refinement_loop_no_local_allowlist():
    """AC1: plan_refinement_loop.py must not define ISSUE_KIND_ALLOWLIST at module level."""
    source = (REFINEMENT_SCRIPTS / "plan_refinement_loop.py").read_text(encoding="utf-8")
    # Must not have a top-level assignment like: ISSUE_KIND_ALLOWLIST = ...
    match = re.search(r"^ISSUE_KIND_ALLOWLIST\s*=", source, re.MULTILINE)
    assert match is None, (
        "plan_refinement_loop.py must not define a local ISSUE_KIND_ALLOWLIST. "
        "Use _get_issue_kind_allowlist() which reads from SSOT instead."
    )


# ---------------------------------------------------------------------------
# AC2: check_issue_contract.py has no local issue_kind allowlist
# ---------------------------------------------------------------------------

def test_check_issue_contract_no_local_allowlist():
    """AC2: check_issue_contract.py must not define ISSUE_KIND_ALLOWLIST at module level."""
    source = (REVIEW_SCRIPTS / "check_issue_contract.py").read_text(encoding="utf-8")
    match = re.search(r"^ISSUE_KIND_ALLOWLIST\s*=", source, re.MULTILINE)
    assert match is None, (
        "check_issue_contract.py must not define a local ISSUE_KIND_ALLOWLIST. "
        "Use _load_issue_kind_policy() which reads from SSOT instead."
    )


# ---------------------------------------------------------------------------
# AC3: detect_issue_kind does not return "implementation" for "design"
# ---------------------------------------------------------------------------

def test_design_not_implementation():
    """AC3: detect_issue_kind with issue_kind=design must NOT return 'implementation'.

    'design' is an alias for 'research' in ISSUE_KIND_POLICY_V1.
    """
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)
    cic._clear_issue_kind_policy_cache()

    # Build a minimal issue body with Machine-Readable Contract declaring design
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: design
```

## Outcome

Some outcome text.
"""
    result = cic.detect_issue_kind(body, labels="", title="")
    assert result != "implementation", (
        f"detect_issue_kind returned 'implementation' for issue_kind=design. "
        f"Expected 'research' (alias normalization) but got: {result!r}"
    )
    # Should be normalized to "research" via alias
    assert result == "research", (
        f"Expected detect_issue_kind to normalize 'design' to 'research', got: {result!r}"
    )


# ---------------------------------------------------------------------------
# AC4: detect_issue_kind does not silently return "implementation" for unknown kind
# ---------------------------------------------------------------------------

def test_unknown_kind_no_silent_fallback():
    """AC4: detect_issue_kind with unknown kind must NOT return 'implementation'.

    Unknown kinds (not in canonical_kinds or aliases) must return the
    UNKNOWN_ISSUE_KIND_SENTINEL instead of silently falling back to 'implementation'.
    """
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)
    cic._clear_issue_kind_policy_cache()

    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: foobar
```

## Outcome

Some outcome text.
"""
    result = cic.detect_issue_kind(body, labels="", title="")
    assert result != "implementation", (
        "detect_issue_kind silently returned 'implementation' for unknown kind 'foobar'. "
        "Unknown kinds must be blocked (UNKNOWN_ISSUE_KIND_SENTINEL)."
    )
    assert result == cic.UNKNOWN_ISSUE_KIND_SENTINEL, (
        f"Expected UNKNOWN_ISSUE_KIND_SENTINEL for unknown kind 'foobar', got: {result!r}"
    )


# ---------------------------------------------------------------------------
# AC5: docs/dev/github-ops.md contains ISSUE_KIND_POLICY_V1
# ---------------------------------------------------------------------------

def test_github_ops_md_has_issue_kind_policy_v1():
    """AC5: docs/dev/github-ops.md must contain ISSUE_KIND_POLICY_V1."""
    assert GITHUB_OPS_MD.exists(), f"docs/dev/github-ops.md not found at {GITHUB_OPS_MD}"
    content = GITHUB_OPS_MD.read_text(encoding="utf-8")
    assert "ISSUE_KIND_POLICY_V1" in content, (
        "docs/dev/github-ops.md must contain 'ISSUE_KIND_POLICY_V1'"
    )


def test_github_ops_md_policy_has_canonical_kinds():
    """AC5: ISSUE_KIND_POLICY_V1 must include canonical_kinds."""
    content = GITHUB_OPS_MD.read_text(encoding="utf-8")
    assert "canonical_kinds" in content, (
        "ISSUE_KIND_POLICY_V1 in docs/dev/github-ops.md must include 'canonical_kinds'"
    )


def test_github_ops_md_policy_has_aliases():
    """AC5: ISSUE_KIND_POLICY_V1 must include aliases."""
    content = GITHUB_OPS_MD.read_text(encoding="utf-8")
    assert "aliases" in content, (
        "ISSUE_KIND_POLICY_V1 in docs/dev/github-ops.md must include 'aliases'"
    )


def test_github_ops_md_policy_has_unknown_kind_policy():
    """AC5: ISSUE_KIND_POLICY_V1 must include unknown_kind_policy."""
    content = GITHUB_OPS_MD.read_text(encoding="utf-8")
    assert "unknown_kind_policy" in content, (
        "ISSUE_KIND_POLICY_V1 in docs/dev/github-ops.md must include 'unknown_kind_policy'"
    )


# ---------------------------------------------------------------------------
# Additional: SSOT consistency — plan_refinement_loop loads from SSOT
# ---------------------------------------------------------------------------

def test_plan_refinement_loop_ssot_loader_exists():
    """plan_refinement_loop.py must expose _load_issue_kind_policy and _get_issue_kind_allowlist."""
    import importlib
    import plan_refinement_loop as prl
    importlib.reload(prl)
    prl._clear_issue_kind_policy_cache()

    assert hasattr(prl, "_load_issue_kind_policy"), (
        "plan_refinement_loop.py must define _load_issue_kind_policy()"
    )
    assert hasattr(prl, "_get_issue_kind_allowlist"), (
        "plan_refinement_loop.py must define _get_issue_kind_allowlist()"
    )


def test_plan_refinement_loop_allowlist_matches_ssot_canonical_kinds():
    """plan_refinement_loop._get_issue_kind_allowlist() must match SSOT canonical_kinds."""
    import importlib
    import plan_refinement_loop as prl
    importlib.reload(prl)
    prl._clear_issue_kind_policy_cache()

    allowlist = prl._get_issue_kind_allowlist()
    assert "implementation" in allowlist
    assert "research" in allowlist
    assert "parent" in allowlist
    # design/tracking are aliases, not canonical — must NOT be in canonical_kinds
    assert "design" not in allowlist, (
        "'design' must not be in canonical_kinds (it is an alias for 'research')"
    )
    assert "foobar" not in allowlist


def test_check_issue_contract_ssot_policy_has_design_alias():
    """SSOT policy loaded by check_issue_contract must have design→research alias."""
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)
    cic._clear_issue_kind_policy_cache()

    policy = cic._load_issue_kind_policy()
    aliases = policy["aliases"]
    assert "design" in aliases, "SSOT aliases must include 'design'"
    assert aliases["design"] == "research", (
        f"'design' alias must map to 'research', got: {aliases['design']!r}"
    )


def test_check_issue_contract_ssot_policy_unknown_kind_policy_is_block():
    """SSOT policy loaded by check_issue_contract must have unknown_kind_policy: block."""
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)
    cic._clear_issue_kind_policy_cache()

    policy = cic._load_issue_kind_policy()
    assert policy["unknown_kind_policy"] == "block", (
        f"unknown_kind_policy must be 'block', got: {policy['unknown_kind_policy']!r}"
    )


# ---------------------------------------------------------------------------
# Blocker 4 — Strengthened tests: loader reads from docs, not hardcoded
# ---------------------------------------------------------------------------

def _make_github_ops_fixture(canonical_kinds: list, aliases: dict, tmp_dir: Path) -> Path:
    """Write a minimal docs/dev/github-ops.md fixture with the given SSOT values."""
    kinds_lines = "\n".join(f"  - {k}" for k in canonical_kinds)
    aliases_lines = "\n".join(f"  {k}: {v}" for k, v in aliases.items())
    # Build the YAML block body without leading indentation so the fenced block
    # starts at column 0 (required by the loader regex).
    yaml_body = (
        "ISSUE_KIND_POLICY_V1:\n"
        "  schema_version: \"1\"\n"
        "  canonical_kinds:\n"
        f"{kinds_lines}\n"
        "  aliases:\n"
        f"{aliases_lines}\n"
        "  unknown_kind_policy: block\n"
        "  unknown_kind_reason_code: unknown_issue_kind\n"
        "  consumer_requirements:\n"
        "  - plan_refinement_loop.py\n"
        "  - check_issue_contract.py\n"
    )
    content = (
        "# GitHub Ops\n\n"
        "## ISSUE_KIND_POLICY_V1\n\n"
        "```yaml\n"
        + yaml_body
        + "```\n"
    )
    docs_dev = tmp_dir / "docs" / "dev"
    docs_dev.mkdir(parents=True, exist_ok=True)
    ssot_path = docs_dev / "github-ops.md"
    ssot_path.write_text(content, encoding="utf-8")
    return tmp_dir


def test_loader_reads_from_docs_not_hardcoded_cic():
    """Blocker 4: Changing canonical_kinds in docs fixture changes what check_issue_contract loader returns.

    Proves the loader reads from docs/dev/github-ops.md, not a hardcoded value.
    """
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Use a custom kind that is NOT in the real SSOT
        _make_github_ops_fixture(
            canonical_kinds=["implementation", "research", "parent", "custom_kind_xyz"],
            aliases={"design": "research"},
            tmp_dir=tmp_path,
        )
        cic._clear_issue_kind_policy_cache()
        policy = cic._load_issue_kind_policy(repo_root=tmp_path)
        assert "custom_kind_xyz" in policy["canonical_kinds"], (
            "Loader must return canonical_kinds from the fixture file, not hardcoded defaults. "
            f"Got: {policy['canonical_kinds']}"
        )


def test_loader_reads_from_docs_not_hardcoded_prl():
    """Blocker 4: Changing canonical_kinds in docs fixture changes what plan_refinement_loop loader returns.

    Proves the loader reads from docs/dev/github-ops.md, not a hardcoded value.
    """
    import importlib
    import plan_refinement_loop as prl
    importlib.reload(prl)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_github_ops_fixture(
            canonical_kinds=["implementation", "research", "parent", "custom_kind_abc"],
            aliases={"design": "research"},
            tmp_dir=tmp_path,
        )
        prl._clear_issue_kind_policy_cache()
        allowlist = prl._load_issue_kind_policy(repo_root=tmp_path)["canonical_kinds"]
        assert "custom_kind_abc" in allowlist, (
            "Loader must return canonical_kinds from the fixture file, not hardcoded defaults. "
            f"Got: {allowlist}"
        )


def test_malformed_ssot_block_fail_closed_cic():
    """Blocker 4: Malformed/missing SSOT block causes fail-closed (IssueKindPolicyLoadError), not silent fallback.

    check_issue_contract._load_issue_kind_policy must raise IssueKindPolicyLoadError
    when the ISSUE_KIND_POLICY_V1 block is absent from github-ops.md.
    """
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        docs_dev = tmp_path / "docs" / "dev"
        docs_dev.mkdir(parents=True, exist_ok=True)
        # Write a github-ops.md WITHOUT the ISSUE_KIND_POLICY_V1 block
        (docs_dev / "github-ops.md").write_text(
            "# GitHub Ops\n\nNo policy block here.\n",
            encoding="utf-8",
        )
        cic._clear_issue_kind_policy_cache()
        with pytest.raises(cic.IssueKindPolicyLoadError):
            cic._load_issue_kind_policy(repo_root=tmp_path)


def test_malformed_ssot_block_fail_closed_prl():
    """Blocker 4: Malformed/missing SSOT block causes fail-closed (IssueKindPolicyLoadError) in plan_refinement_loop.

    plan_refinement_loop._load_issue_kind_policy must raise IssueKindPolicyLoadError
    when the ISSUE_KIND_POLICY_V1 block is absent from github-ops.md.
    """
    import importlib
    import plan_refinement_loop as prl
    importlib.reload(prl)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        docs_dev = tmp_path / "docs" / "dev"
        docs_dev.mkdir(parents=True, exist_ok=True)
        (docs_dev / "github-ops.md").write_text(
            "# GitHub Ops\n\nNo policy block here.\n",
            encoding="utf-8",
        )
        prl._clear_issue_kind_policy_cache()
        with pytest.raises(prl.IssueKindPolicyLoadError):
            prl._load_issue_kind_policy(repo_root=tmp_path)


def test_missing_ssot_file_fail_closed_cic():
    """Blocker 4: Missing github-ops.md file causes fail-closed, not silent fallback to 'implementation'."""
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Do NOT create docs/dev/github-ops.md
        cic._clear_issue_kind_policy_cache()
        with pytest.raises(cic.IssueKindPolicyLoadError):
            cic._load_issue_kind_policy(repo_root=tmp_path)

        # detect_issue_kind must return UNKNOWN_ISSUE_KIND_SENTINEL, not "implementation"
        cic._clear_issue_kind_policy_cache()
        body = textwrap.dedent("""\
            ## Machine-Readable Contract

            ```yaml
            contract_schema_version: v1
            issue_kind: implementation
            ```
            """)
        # We can't force repo_root in detect_issue_kind directly, but we verify
        # that detect_issue_kind returns UNKNOWN when SSOT is unavailable
        # by monkey-patching the repo root finder temporarily.
        original_find_root = cic._find_repo_root_for_contract
        try:
            cic._find_repo_root_for_contract = lambda: tmp_path  # type: ignore[assignment]
            cic._clear_issue_kind_policy_cache()
            result = cic.detect_issue_kind(body, labels="", title="")
            assert result == cic.UNKNOWN_ISSUE_KIND_SENTINEL, (
                f"detect_issue_kind must return UNKNOWN_ISSUE_KIND_SENTINEL when SSOT is missing, "
                f"got: {result!r}"
            )
        finally:
            cic._find_repo_root_for_contract = original_find_root  # type: ignore[assignment]
            cic._clear_issue_kind_policy_cache()


def test_plan_refinement_loop_normalizes_design_to_research():
    """Blocker 4: plan_refinement_loop._normalize_issue_kind normalizes 'design' to 'research'.

    Verifies that plan_refinement_loop applies alias normalization (not just allowlist check).
    """
    import importlib
    import plan_refinement_loop as prl
    importlib.reload(prl)
    prl._clear_issue_kind_policy_cache()

    assert hasattr(prl, "_normalize_issue_kind"), (
        "plan_refinement_loop.py must expose _normalize_issue_kind()"
    )
    result = prl._normalize_issue_kind("design")
    assert result == "research", (
        f"_normalize_issue_kind('design') must return 'research' (alias normalization), "
        f"got: {result!r}"
    )


def test_plan_refinement_loop_normalize_unknown_returns_none():
    """Blocker 4: plan_refinement_loop._normalize_issue_kind returns None for unknown kind."""
    import importlib
    import plan_refinement_loop as prl
    importlib.reload(prl)
    prl._clear_issue_kind_policy_cache()

    result = prl._normalize_issue_kind("foobar_unknown_xyz")
    assert result is None, (
        f"_normalize_issue_kind for unknown kind must return None, got: {result!r}"
    )
