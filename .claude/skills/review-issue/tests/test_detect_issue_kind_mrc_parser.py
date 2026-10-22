"""
test_detect_issue_kind_mrc_parser.py

PR #1878 P1 review: detect_issue_kind() previously used an independent,
non-section-bound regex to locate `issue_kind:` inside a Machine-Readable
Contract YAML fence (order-dependent on `contract_schema_version` appearing
before `issue_kind`). This module verifies detect_issue_kind() now delegates
MRC detection to the shared, section-bound
mrc_contract_parser.parse_machine_readable_contract() (the same SSOT parser
used by validate_issue_body.py / #1135 P0), while preserving the existing
label/title fallback ONLY for legacy bodies with no MRC section at all.

Cases covered:
  1. issue_kind: parent (normal)
  2. tracking alias
  3. implementation MRC + parent-looking label (implementation MRC wins)
  4. research
  5. unknown kind
  6. key-order permutation (issue_kind before contract_schema_version)
  7. decoy YAML in a non-MRC section (## Notes) with the real MRC being
     implementation
  8. multiple YAML-fence-looking MRC blocks (multiple fences inside the MRC
     section → malformed, fail-closed, no label fallback)
  9. duplicate issue_kind key (malformed, fail-closed, no label fallback)
  10. malformed MRC (YAML syntax error, fail-closed, no label fallback)
  11. MRC section heading present but no fence at all (malformed, fail-closed)
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
REVIEW_SCRIPTS = REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"

sys.path.insert(0, str(REVIEW_SCRIPTS))

import check_issue_contract as cic  # noqa: E402

importlib.reload(cic)


def _reset():
    cic._clear_issue_kind_policy_cache()


def test_issue_kind_parent_normal():
    """Case 1: issue_kind: parent (normal MRC) -> 'parent'."""
    _reset()
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
parent_mode: delivery-rollup
```

## Summary

Parent tracker.
"""
    assert cic.detect_issue_kind(body, labels="", title="") == "parent"


def test_tracking_alias_normalizes_to_parent():
    """Case 2: tracking alias -> 'parent'."""
    _reset()
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: tracking
```

## Summary

Tracking body.
"""
    assert cic.detect_issue_kind(body, labels="", title="") == "parent"


def test_implementation_mrc_wins_over_parent_looking_label():
    """Case 3: implementation MRC + a parent-looking label must resolve as
    'implementation' — the MRC field is authoritative, not the label."""
    _reset()
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Outcome

Implementation outcome text.
"""
    result = cic.detect_issue_kind(body, labels="tracking, parent", title="")
    assert result == "implementation", (
        f"MRC issue_kind: implementation must win over parent-looking labels, got: {result!r}"
    )


def test_research_kind():
    """Case 4: issue_kind: research -> 'research'."""
    _reset()
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
```

## Summary

Research body.
"""
    assert cic.detect_issue_kind(body, labels="", title="") == "research"


def test_unknown_kind_returns_sentinel():
    """Case 5: unknown kind value -> UNKNOWN_ISSUE_KIND_SENTINEL, no label fallback."""
    _reset()
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: some-future-kind
```

## Summary

Unknown kind body.
"""
    result = cic.detect_issue_kind(body, labels="phase/implementation", title="")
    assert result == cic.UNKNOWN_ISSUE_KIND_SENTINEL, (
        f"Unknown MRC issue_kind must return UNKNOWN_ISSUE_KIND_SENTINEL "
        f"(no fallback to label), got: {result!r}"
    )


def test_key_order_permutation_issue_kind_before_schema_version():
    """Case 6: issue_kind declared BEFORE contract_schema_version in the YAML
    fence must still be detected (the old regex required schema_version to
    appear first; the new section-bound parser is key-order independent)."""
    _reset()
    body = """## Machine-Readable Contract

```yaml
issue_kind: implementation
contract_schema_version: v1
```

## Outcome

Key-order permutation outcome text.
"""
    result = cic.detect_issue_kind(body, labels="", title="")
    assert result == "implementation", (
        f"issue_kind before contract_schema_version must still resolve to "
        f"'implementation', got: {result!r}"
    )


def test_decoy_yaml_in_notes_section_does_not_win():
    """Case 7: a decoy `issue_kind: parent` YAML fence living under an
    unrelated `## Notes` section must NOT be used; only the real MRC section
    (here: implementation) is authoritative."""
    _reset()
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Outcome

Real MRC section outcome.

## Notes

Some historical decoy below, not part of the MRC section:

```yaml
contract_schema_version: v1
issue_kind: parent
```
"""
    result = cic.detect_issue_kind(body, labels="", title="")
    assert result == "implementation", (
        f"Decoy YAML in '## Notes' must not override the real MRC section's "
        f"issue_kind: implementation, got: {result!r}"
    )


def test_multiple_yaml_fences_inside_mrc_section_is_malformed_no_fallback():
    """Case 8: two YAML fences inside the MRC section is malformed
    (mrc_yaml_fence_multiple) -> fail-closed sentinel, no label fallback even
    though a parent-looking label/title is present."""
    _reset()
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

```yaml
issue_kind: parent
```

## Outcome

Multiple fence body.
"""
    result = cic.detect_issue_kind(body, labels="tracking, parent", title="実装: multiple fence")
    assert result == cic.UNKNOWN_ISSUE_KIND_SENTINEL, (
        f"Multiple YAML fences inside MRC section must fail-closed to "
        f"UNKNOWN_ISSUE_KIND_SENTINEL (no label/title fallback), got: {result!r}"
    )


def test_duplicate_issue_kind_key_is_malformed_no_fallback():
    """Case 9: duplicate `issue_kind` key inside the same fence is malformed
    (duplicate_key) -> fail-closed sentinel, no label fallback."""
    _reset()
    body = (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        "issue_kind: parent\n"
        "issue_kind: implementation\n"
        "```\n\n"
        "## Outcome\n\nDuplicate key body.\n"
    )
    result = cic.detect_issue_kind(body, labels="phase/implementation", title="実装: dup key")
    assert result == cic.UNKNOWN_ISSUE_KIND_SENTINEL, (
        f"Duplicate issue_kind key must fail-closed to UNKNOWN_ISSUE_KIND_SENTINEL "
        f"(no label/title fallback), got: {result!r}"
    )


def test_malformed_yaml_syntax_is_malformed_no_fallback():
    """Case 10: YAML syntax error inside the MRC fence -> fail-closed
    sentinel, no label fallback."""
    _reset()
    body = (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        "issue_kind: [unterminated\n"
        "```\n\n"
        "## Outcome\n\nMalformed YAML body.\n"
    )
    result = cic.detect_issue_kind(body, labels="phase/implementation", title="実装: malformed")
    assert result == cic.UNKNOWN_ISSUE_KIND_SENTINEL, (
        f"Malformed MRC YAML must fail-closed to UNKNOWN_ISSUE_KIND_SENTINEL "
        f"(no label/title fallback), got: {result!r}"
    )


def test_mrc_heading_without_fence_is_malformed_no_fallback():
    """Case 11: `## Machine-Readable Contract` heading present but with no
    fenced YAML block at all -> malformed (mrc_yaml_fence_missing) ->
    fail-closed sentinel, no label fallback."""
    _reset()
    body = (
        "## Machine-Readable Contract\n\n"
        "No YAML fence here, just prose.\n\n"
        "## Outcome\n\nNo fence body.\n"
    )
    result = cic.detect_issue_kind(body, labels="phase/implementation", title="実装: no fence")
    assert result == cic.UNKNOWN_ISSUE_KIND_SENTINEL, (
        f"MRC section without a YAML fence must fail-closed to "
        f"UNKNOWN_ISSUE_KIND_SENTINEL (no label/title fallback), got: {result!r}"
    )


def test_legacy_body_without_mrc_section_keeps_label_fallback():
    """Contrast case: a genuinely legacy body with NO MRC section at all must
    still use the pre-existing label/title fallback logic unchanged."""
    _reset()
    body = "## Outcome\n\nLegacy body with no MRC section.\n"
    result = cic.detect_issue_kind(body, labels="phase/implementation", title="")
    assert result == "implementation", (
        f"Legacy body (no MRC section) must keep using label fallback, got: {result!r}"
    )


def test_legacy_body_title_fallback_unchanged():
    """Contrast case: legacy body with no MRC section and no matching label
    falls back to title prefix matching, unchanged."""
    _reset()
    body = "## Outcome\n\nLegacy body with no MRC section, no labels.\n"
    result = cic.detect_issue_kind(body, labels="", title="実装: legacy title fallback")
    assert result == "implementation", (
        f"Legacy body (no MRC section) must keep using title fallback, got: {result!r}"
    )
