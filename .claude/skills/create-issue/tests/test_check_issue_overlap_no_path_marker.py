"""AC11 (Issue #2783): two unrelated read-only research Issues whose bodies
declare Allowed Paths as ONLY the canonical no-path marker `(none)` or the
legacy exact marker `読み取り専用。リポジトリ変更なし（既定）` must not be
flagged as `duplicate` / `same_path_set()` overlap merely because they both
declare "no paths" the same way (before Issue #2783's fix, the legacy
marker's trailing full-width paren annotation was accidentally stripped by
the existing annotation normalizer into a non-empty bogus "path" string
identical across any two legacy-marker bodies, which `same_path_set()`
would then flag as an exact duplicate).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio  # noqa: E402


def _read_only_body(marker: str) -> str:
    return f"""## Outcome

Investigation-only research.

## Acceptance Criteria

- [ ] AC1: findings documented.

## Allowed Paths

- {marker}

## Stop Conditions

- none
"""


def test_no_path_marker_alone_not_duplicate():
    # GIVEN: two entirely unrelated read-only research Issues, both
    # declaring Allowed Paths as ONLY the legacy no-path marker.
    current = cio.IssueScope(
        title="research: 完全に無関係な調査 A",
        number=3001,
        body=_read_only_body("読み取り専用。リポジトリ変更なし（既定）"),
        goal="調査Aの目的",
    )
    candidate = cio.IssueScope(
        title="research: 完全に無関係な調査 B",
        number=3002,
        body=_read_only_body("読み取り専用。リポジトリ変更なし（既定）"),
        goal="調査Bの目的",
        state="OPEN",
    )

    # WHEN: same_path_set() is asked whether their effective Allowed Paths
    # collide.
    same = cio.same_path_set(
        current.effective_allowed_paths(), candidate.effective_allowed_paths()
    )

    # THEN: no-path-marker-only agreement is NOT a path-set match (both
    # sides normalize to the empty set, and same_path_set() requires a
    # non-empty set on both sides).
    assert same is False
    assert current.effective_allowed_paths() == ()
    assert candidate.effective_allowed_paths() == ()

    # AND: the full classify_overlap() routing does not classify these two
    # unrelated Issues as duplicate/overlap via allowed_paths matching
    # (title/goal are deliberately disjoint, so no other matched_fields
    # should fire either).
    result = cio.classify_overlap(current, [candidate])
    assert result.verdict != cio.DUPLICATE, result
    assert not any(
        "allowed_paths" in ev.matched_fields for ev in result.candidates
    ), result.candidates


def test_no_path_marker_canonical_alone_not_duplicate():
    """Same scenario with the canonical marker `(none)`."""
    current = cio.IssueScope(
        title="research: 完全に無関係な調査 C",
        number=3003,
        body=_read_only_body("(none)"),
        goal="調査Cの目的",
    )
    candidate = cio.IssueScope(
        title="research: 完全に無関係な調査 D",
        number=3004,
        body=_read_only_body("(none)"),
        goal="調査Dの目的",
        state="OPEN",
    )

    same = cio.same_path_set(
        current.effective_allowed_paths(), candidate.effective_allowed_paths()
    )
    assert same is False

    result = cio.classify_overlap(current, [candidate])
    assert result.verdict != cio.DUPLICATE, result


def _allowed_paths_section(inner: str) -> str:
    return f"""## Outcome

Investigation-only research.

## Allowed Paths

{inner}

## Stop Conditions

- none
"""


def test_code_fence_wrapped_canonical_marker_extracts_zero_paths():
    # PR #2791 review fix_delta (Issue #2783): a ```text fence-wrapped
    # `(none)` must not leak the fence delimiter lines themselves
    # (` ```text` / ` ``` `) as spurious candidate paths via the real
    # consumer `extract_allowed_path_entries()` / `extract_allowed_paths()`.
    body = _allowed_paths_section("```text\n(none)\n```")
    assert cio.extract_allowed_path_entries(body) == []
    assert cio.extract_allowed_paths(body) == []


def test_code_fence_wrapped_legacy_marker_extracts_zero_paths():
    body = _allowed_paths_section("```text\n読み取り専用。リポジトリ変更なし（既定）\n```")
    assert cio.extract_allowed_path_entries(body) == []
    assert cio.extract_allowed_paths(body) == []


def test_numbered_list_legacy_marker_extracts_zero_paths():
    # PR #2791 review fix_delta: numbered-list wrapping (`1.` / `1)`) must
    # be stripped BEFORE the marker exact-match, mirroring the existing
    # `_BULLET_RE` numbered-list support `normalize_path()` already has
    # (the shared `allowed_paths_policy._strip_wrapper()` only strips
    # bullet markers, not numbered lists -- this consumer must apply its
    # own numbered-list-aware wrapper strip first).
    body = _allowed_paths_section("1. 読み取り専用。リポジトリ変更なし（既定）")
    assert cio.extract_allowed_path_entries(body) == []
    assert cio.extract_allowed_paths(body) == []

    body_paren_close = _allowed_paths_section("1) 読み取り専用。リポジトリ変更なし（既定）")
    assert cio.extract_allowed_path_entries(body_paren_close) == []
    assert cio.extract_allowed_paths(body_paren_close) == []


def test_marker_and_real_path_coexist_marker_removed_path_kept():
    # PR #2791 review fix_delta: when a no-path marker line and a real
    # path both appear in the SAME Allowed Paths section, only the marker
    # line is dropped -- the real path must survive extraction unchanged.
    body = _allowed_paths_section(
        "- (none)\n- scripts/agent-ops/allowed_paths_policy.py"
    )
    entries = cio.extract_allowed_path_entries(body)
    assert entries == ["- scripts/agent-ops/allowed_paths_policy.py"]
    assert cio.extract_allowed_paths(body) == ["scripts/agent-ops/allowed_paths_policy.py"]


def test_unicode_and_annotated_real_paths_unaffected_by_marker_fix():
    # PR #2791 review fix_delta: Unicode/punctuation-bearing real paths
    # (e.g. `docs/日本語.md`) and the existing PR #684 annotation semantics
    # (`some/path.py（読み取り専用）` -> annotation stripped, bare path kept)
    # must still work exactly as before this fix.
    body = _allowed_paths_section(
        "- docs/日本語.md\n- some/path.py（読み取り専用）"
    )
    assert cio.extract_allowed_paths(body) == ["docs/日本語.md", "some/path.py"]


def test_no_path_marker_mixed_canonical_and_legacy_not_duplicate():
    """One side declares the canonical marker, the other the legacy marker
    -- still not a duplicate via allowed_paths (both normalize to 0
    paths)."""
    current = cio.IssueScope(
        title="research: 無関係な調査 E",
        number=3005,
        body=_read_only_body("(none)"),
        goal="調査Eの目的",
    )
    candidate = cio.IssueScope(
        title="research: 無関係な調査 F",
        number=3006,
        body=_read_only_body("読み取り専用。リポジトリ変更なし（既定）"),
        goal="調査Fの目的",
        state="OPEN",
    )

    result = cio.classify_overlap(current, [candidate])
    assert result.verdict != cio.DUPLICATE, result
