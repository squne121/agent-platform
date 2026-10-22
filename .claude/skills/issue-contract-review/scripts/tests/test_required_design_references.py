#!/usr/bin/env python3
"""
Tests for Issue #1346: Required Design References static check in
contract_readiness_check.py.

Covers:
  - AC4: `## Required Design References` presence/content check for
    `issue_kind: implementation` issues (empty / N/A / none-only / no
    repo-relative design-doc path -> needs_fix)
  - AC6: existing responsibility boundary is not broken (section absent
    entirely -> no RDR001 error, mirrors the RVA precedent)
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_SCRIPT = _SCRIPTS_DIR / "contract_readiness_check.py"


def run_readiness_with_body(body: str, mode: str = "static") -> tuple[dict, int]:
    """Run contract_readiness_check.py against a body string via temp file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--body-file", tmp_path, "--mode", mode],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.stdout, f"No stdout (stderr: {result.stderr})"
        return json.loads(result.stdout), result.returncode
    finally:
        import os

        os.unlink(tmp_path)


_BASE_IMPLEMENTATION_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test goal"
change_kind: docs
```

## Parent Issue

なし（単独改善）

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
$ test -f some_file.py
```

## Allowed Paths

```
docs/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

- decision: not_applicable
- reason: docs-only change

{rdr_section}
"""


def _body_with_rdr(rdr_section_body: str) -> str:
    rdr_section = f"## Required Design References\n\n{rdr_section_body}\n"
    return _BASE_IMPLEMENTATION_BODY.format(rdr_section=rdr_section)


def _body_with_rdr_heading(heading: str, rdr_section_body: str) -> str:
    """AC10: build a body using a custom (e.g. Japanese) RDR heading text."""
    rdr_section = f"## {heading}\n\n{rdr_section_body}\n"
    return _BASE_IMPLEMENTATION_BODY.format(rdr_section=rdr_section)


def _body_without_rdr_section() -> str:
    return _BASE_IMPLEMENTATION_BODY.format(rdr_section="")


def _body_non_implementation(rdr_section_body: str) -> str:
    body = _body_with_rdr(rdr_section_body)
    return body.replace("issue_kind: implementation", "issue_kind: research")


# ---------------------------------------------------------------------------
# AC4: empty / N/A / none-only Required Design References -> needs_fix
# ---------------------------------------------------------------------------


def test_rdr_empty_section_needs_fix():
    """AC4: Required Design References section present but empty -> needs_fix."""
    data, exit_code = run_readiness_with_body(_body_with_rdr(""))
    assert data["status"] == "needs_fix", (
        f"Expected needs_fix for empty RDR section, got: {data['status']}. "
        f"Errors: {data['errors']}"
    )
    rule_ids = [e["rule_id"] for e in data["errors"]]
    assert "RDR001" in rule_ids, f"RDR001 not found in errors: {rule_ids}"
    assert exit_code == 1


def test_rdr_na_only_needs_fix():
    """AC4: Required Design References section containing only 'N/A' -> needs_fix."""
    data, _ = run_readiness_with_body(_body_with_rdr("N/A"))
    categories = [e["category"] for e in data["errors"]]
    assert "required_design_references_missing_or_empty" in categories, categories
    assert data["status"] == "needs_fix"


def test_rdr_none_only_needs_fix():
    """AC4: Required Design References section containing only 'none' -> needs_fix."""
    data, _ = run_readiness_with_body(_body_with_rdr("none"))
    rule_ids = [e["rule_id"] for e in data["errors"]]
    assert "RDR001" in rule_ids, rule_ids
    assert data["status"] == "needs_fix"


def test_rdr_prose_without_path_needs_fix():
    """AC4: RDR section with prose but no repo-relative design-doc path -> needs_fix."""
    data, _ = run_readiness_with_body(
        _body_with_rdr("この Issue は既存の設計に従う。")
    )
    rule_ids = [e["rule_id"] for e in data["errors"]]
    assert "RDR001" in rule_ids, rule_ids
    assert data["status"] == "needs_fix"


# ---------------------------------------------------------------------------
# AC4: valid repo-relative design-doc path reference -> no RDR001 error
# ---------------------------------------------------------------------------


def test_rdr_valid_agent_skill_boundaries_reference_passes():
    """AC4: reference to docs/dev/github-ops.md (Parallel Agent Runtime
    Safety) is detected as a valid design-doc reference."""
    data, _ = run_readiness_with_body(
        _body_with_rdr(
            "- docs/dev/github-ops.md#ISSUE_KIND_POLICY_V1"
        )
    )
    rdr_errors = [
        e for e in data["errors"] if e.get("category") == "required_design_references_missing_or_empty"
    ]
    assert not rdr_errors, f"Unexpected RDR001 errors for valid reference: {rdr_errors}"


def test_rdr_valid_claude_skills_reference_passes():
    """AC4: reference to a .claude/ skill doc is also accepted as a valid path."""
    data, _ = run_readiness_with_body(
        _body_with_rdr("- .claude/skills/issue-contract-review/references/vc-preflight.md")
    )
    rdr_errors = [
        e for e in data["errors"] if e.get("category") == "required_design_references_missing_or_empty"
    ]
    assert not rdr_errors, f"Unexpected RDR001 errors for valid reference: {rdr_errors}"


# ---------------------------------------------------------------------------
# AC6: responsibility boundary — section absent entirely does not fail here
# ---------------------------------------------------------------------------


def test_rdr_section_absent_no_error():
    """AC6: Required Design References section entirely absent -> no RDR001 error
    (mirrors RVA precedent; existence enforcement belongs to the template /
    review-issue, not this static checker). Existing go fixtures predating
    this section must not regress."""
    data, _ = run_readiness_with_body(_body_without_rdr_section())
    rdr_errors = [
        e for e in data["errors"] if e.get("category") == "required_design_references_missing_or_empty"
    ]
    assert not rdr_errors, f"Unexpected RDR001 errors when section absent: {rdr_errors}"


def test_rdr_not_applied_to_non_implementation_kind():
    """AC6: research issue_kind is out of scope for this check even with an
    empty Required Design References section."""
    data, _ = run_readiness_with_body(_body_non_implementation(""))
    rdr_errors = [
        e for e in data["errors"] if e.get("category") == "required_design_references_missing_or_empty"
    ]
    assert not rdr_errors, f"Unexpected RDR001 errors for non-implementation kind: {rdr_errors}"


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


def test_rdr_error_shape_matches_schema():
    """RDR001 error entries carry the full ISSUE_CONTRACT_READINESS_RESULT_V1
    error shape (no missing keys)."""
    data, _ = run_readiness_with_body(_body_with_rdr(""))
    rdr_errors = [e for e in data["errors"] if e["rule_id"] == "RDR001"]
    assert rdr_errors, "Expected at least one RDR001 error"
    required_keys = {
        "rule_id",
        "severity",
        "source_check",
        "category",
        "section",
        "line_start",
        "line_end",
        "minimal_context",
        "fix_hint",
        "autofixable",
    }
    for err in rdr_errors:
        missing = required_keys - err.keys()
        assert not missing, f"RDR001 error missing keys: {missing}"
# ---------------------------------------------------------------------------
# AC10 (#1346): heading extraction shares HEADING_POLICY accepted forms
# ---------------------------------------------------------------------------


def test_rdr_japanese_heading_parenthesis_form_detected():
    """AC10: '必要設計リファレンス (Required Design References)' heading form (registered
    in prose_boundary_policy.HEADING_POLICY accepted_forms) is detected by RDR001,
    not just the plain English heading."""
    data, _ = run_readiness_with_body(
        _body_with_rdr_heading(
            "必要設計リファレンス (Required Design References)",
            "- docs/dev/github-ops.md#ISSUE_KIND_POLICY_V1",
        )
    )
    rdr_errors = [
        e for e in data["errors"] if e.get("category") == "required_design_references_missing_or_empty"
    ]
    assert not rdr_errors, f"Unexpected RDR001 errors for valid Japanese-heading reference: {rdr_errors}"


def test_rdr_japanese_heading_fullwidth_parenthesis_form_empty_needs_fix():
    """AC10: '必要設計リファレンス（Required Design References）' (fullwidth parens) heading
    form is also recognised, and an empty section under that heading still needs_fix."""
    data, _ = run_readiness_with_body(
        _body_with_rdr_heading("必要設計リファレンス（Required Design References）", "")
    )
    rule_ids = [e["rule_id"] for e in data["errors"]]
    assert "RDR001" in rule_ids, rule_ids
    assert data["status"] == "needs_fix"


# ---------------------------------------------------------------------------
# AC11 (#1346): design-doc path judgment narrowed to docs/**, .claude/skills/**/SKILL.md,
# .claude/skills/**/references/**/*.md; with Path.exists() check; autofixable: False
# ---------------------------------------------------------------------------


def test_rdr_src_path_only_needs_fix():
    """AC11: a src/ path reference alone (previously accepted) must now be rejected —
    src/ and scripts/ paths are implementation paths, not design-doc references."""
    data, _ = run_readiness_with_body(_body_with_rdr("- src/some/module.ts"))
    rule_ids = [e["rule_id"] for e in data["errors"]]
    assert "RDR001" in rule_ids, rule_ids
    assert data["status"] == "needs_fix"


def test_rdr_scripts_path_only_needs_fix():
    """AC11: a scripts/ path reference alone must now be rejected."""
    data, _ = run_readiness_with_body(_body_with_rdr("- scripts/agent-ops/some_tool.py"))
    rule_ids = [e["rule_id"] for e in data["errors"]]
    assert "RDR001" in rule_ids, rule_ids
    assert data["status"] == "needs_fix"


def test_rdr_nonexistent_docs_path_needs_fix():
    """AC11: a syntactically-valid docs/ path that does not exist in the repo must
    still be rejected (Path.exists() check)."""
    data, _ = run_readiness_with_body(_body_with_rdr("- docs/dev/this-file-does-not-exist.md"))
    rule_ids = [e["rule_id"] for e in data["errors"]]
    assert "RDR001" in rule_ids, rule_ids
    assert data["status"] == "needs_fix"


def test_rdr_skill_md_reference_passes():
    """AC11: a .claude/skills/**/SKILL.md reference is accepted when it exists."""
    data, _ = run_readiness_with_body(
        _body_with_rdr("- .claude/skills/issue-contract-review/SKILL.md")
    )
    rdr_errors = [
        e for e in data["errors"] if e.get("category") == "required_design_references_missing_or_empty"
    ]
    assert not rdr_errors, f"Unexpected RDR001 errors for existing SKILL.md reference: {rdr_errors}"


def test_rdr_error_autofixable_is_false():
    """AC11: RDR001 autofixable must be False (correct reference requires human
    judgment, not a mechanical fix)."""
    data, _ = run_readiness_with_body(_body_with_rdr(""))
    rdr_errors = [e for e in data["errors"] if e["rule_id"] == "RDR001"]
    assert rdr_errors, "Expected at least one RDR001 error"
    for err in rdr_errors:
        assert err["autofixable"] is False, err
