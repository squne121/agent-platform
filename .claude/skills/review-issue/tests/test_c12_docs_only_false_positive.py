"""Issue #1135: C12 docs-only false-negative regression tests.

After the re-design (OWNER REQUEST_CHANGES on PR #1140), the C12 docs-only
exemption is gated on a *validated* classification, the MRC is parsed strictly
from the ``## Machine-Readable Contract`` section via the shared parser, and
duplicate keys / decoy YAML / code-scoped docs claims can no longer drop C12 to
n/a.

Responsibility boundary: C12 is review-issue structural hygiene; product-spec
semantics (PS001-PS006) live in issue-contract-review / check_product_spec_contract.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
DOCS_ONLY_PSC_FIXTURE = "c12_docs_only_with_psc_issue.md"


def _run_on_path(path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--file", str(path), "--json"],
        capture_output=True, text=True,
    )
    assert result.returncode in (0, 1), (
        f"Script exited {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout)


def run_checker(fixture_name: str) -> dict:
    fixture_path = FIXTURES_DIR / fixture_name
    assert fixture_path.exists(), f"Fixture not found: {fixture_path}"
    return _run_on_path(fixture_path)


def run_checker_text(tmp_path: Path, body: str) -> dict:
    fixture = tmp_path / "inline_issue.md"
    fixture.write_text(body, encoding="utf-8")
    return _run_on_path(fixture)


def c12(output: dict) -> str:
    return output["deterministic_checks"]["C12_product_trace_fields_structure"]


def _build_body(
    *,
    mrc_lines: list[str],
    allowed_paths: list[str],
    product_spec_context: str | None = None,
    pre_sections: str = "",
) -> str:
    """Build a complete issue body so the checker runs (exit 0/1).

    pre_sections is raw markdown inserted BEFORE the MRC (e.g. a decoy ## Notes
    YAML fence). allowed_paths populates the ## Allowed Paths section used by the
    docs-only path-policy gate.
    """
    mrc_block = "\n".join(mrc_lines)
    psc = ""
    if product_spec_context is not None:
        psc = "## Product Spec Context\n\n" + product_spec_context + "\n\n"
    ap = "\n".join(f"- `{p}`" for p in allowed_paths)
    return (
        "---\n"
        "LABELS: phase/implementation,kind/implementation\n"
        "TITLE: 実装: inline c12 docs-only test\n"
        "---\n"
        f"{pre_sections}"
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        f"{mrc_block}\n"
        "```\n\n"
        f"{psc}"
        "## Outcome\n\nXを実装する。\n\n"
        "## Acceptance Criteria\n\n- [ ] AC1: X\n\n"
        "## Verification Commands\n\n```bash\n# AC1\n$ test -f X\n```\n\n"
        "## Stop Conditions\n\n- 1\n- 2\n- 3\n- 4\n- 5\n- 6\n\n"
        "## Runtime Verification Applicability\n\ndecision: not_applicable\n\n"
        "## Allowed Paths\n\n" + ap + "\n"
    )


_DOCS_PSC_BODY = (
    "- source_of_truth: `docs/product/playable-roadmap.md`\n"
    "- diff_rationale: roadmap 正本と乖離しているため同期する\n"
    "- changed_requirement_id: `DOC-M4-001`\n"
    "- affected_sections: `M4: Upgrade Loop` セクション\n"
    "- product_spec_change_mode: docs_ssot_sync_only\n"
)


def test_docs_only_psc_returns_na():
    """AC5: #1094-type docs-only PSC issue (documentation-only Allowed Paths) -> n/a.

    n/a proves changed_requirement_id is not misread AND that the validated
    docs-only exemption applies.
    """
    output = run_checker(DOCS_ONLY_PSC_FIXTURE)
    assert c12(output) == "n/a", (
        f"docs-only PSC must be n/a; got {c12(output)}; blocking={output.get('blocking_issues')}"
    )
    blocking = "\n".join(output.get("blocking_issues", []))
    for field in ("product_spec_id", "source_task_id"):
        assert field not in blocking, f"unexpected {field} blocking issue: {blocking}"


def test_docs_with_code_allowed_paths_fails(tmp_path):
    """AC6: change_kind: docs but Allowed Paths include code/runtime -> blocking fail
    (the docs-only exemption must not apply to a code-scoped issue)."""
    body = _build_body(
        mrc_lines=[
            "contract_schema_version: v1",
            "issue_kind: implementation",
            "change_kind: docs",
        ],
        allowed_paths=["src/runtime.ts"],
        product_spec_context=_DOCS_PSC_BODY,
    )
    output = run_checker_text(tmp_path, body)
    assert c12(output) == "fail", (
        f"docs + code Allowed Paths must FAIL (not n/a); got {c12(output)}"
    )
    blocking = "\n".join(output.get("blocking_issues", []))
    assert "code/runtime" in blocking, f"expected code/runtime contradiction message; got {blocking}"


def test_decoy_mrc_does_not_exempt(tmp_path):
    """AC7: a decoy YAML (change_kind: docs) before the real MRC (change_kind: code)
    must not exempt C12; the section-bound parser reads the real MRC."""
    decoy = "## Notes\n\n```yaml\ncontract_schema_version: v1\nchange_kind: docs\n```\n\n"
    body = _build_body(
        pre_sections=decoy,
        mrc_lines=[
            "contract_schema_version: v1",
            "issue_kind: implementation",
            "change_kind: code",
        ],
        allowed_paths=["docs/dev/current-focus.md"],
        product_spec_context=_DOCS_PSC_BODY,
    )
    output = run_checker_text(tmp_path, body)
    assert c12(output) == "fail", (
        f"decoy MRC must not exempt (real change_kind=code -> applicable/fail); got {c12(output)}"
    )


def test_duplicate_change_kind_does_not_exempt(tmp_path):
    """AC8: a duplicate change_kind (code -> docs) makes the MRC parse fail closed,
    so the docs exemption is not applied (applicable -> fail)."""
    body = _build_body(
        mrc_lines=[
            "contract_schema_version: v1",
            "issue_kind: implementation",
            "change_kind: code",
            "change_kind: docs",
        ],
        allowed_paths=["docs/dev/current-focus.md"],
        product_spec_context=_DOCS_PSC_BODY,
    )
    output = run_checker_text(tmp_path, body)
    assert c12(output) != "n/a", "duplicate change_kind must not silently exempt to n/a"
    assert c12(output) == "fail", f"expected fail; got {c12(output)}"


@pytest.mark.parametrize("where", ["psc", "mrc"])
def test_explicit_malformed_trace_field_still_fails(tmp_path, where):
    """AC9: change_kind: docs but an explicitly-declared malformed trace field (in
    PSC or MRC) still FAILS (docs is not a blanket bypass; both lineages)."""
    if where == "psc":
        mrc_lines = [
            "contract_schema_version: v1",
            "issue_kind: implementation",
            "change_kind: docs",
        ]
        psc = "- source_of_truth: `docs/x.md`\n- requirement_id: BADFORMAT\n"
    else:
        mrc_lines = [
            "contract_schema_version: v1",
            "issue_kind: implementation",
            "change_kind: docs",
            "requirement_id: BADFORMAT",
        ]
        psc = None
    body = _build_body(
        mrc_lines=mrc_lines,
        allowed_paths=["docs/dev/current-focus.md"],
        product_spec_context=psc,
    )
    output = run_checker_text(tmp_path, body)
    assert c12(output) == "fail", (
        f"[{where}] explicit malformed requirement_id must FAIL even for docs; got {c12(output)}"
    )
    assert "requirement_id" in "\n".join(output.get("blocking_issues", []))


def test_task_lineage_marker_still_applies(tmp_path):
    """AC10: change_kind: docs but a task-lineage marker keeps C12 applicable."""
    body = _build_body(
        mrc_lines=[
            "contract_schema_version: v1",
            "issue_kind: implementation",
            "change_kind: docs",
            "generated_from_task: T123",
        ],
        allowed_paths=["docs/dev/current-focus.md"],
        product_spec_context=_DOCS_PSC_BODY,
    )
    output = run_checker_text(tmp_path, body)
    assert c12(output) != "n/a", "task-lineage marker must keep C12 applicable even for docs"
    assert c12(output) == "fail", f"expected fail; got {c12(output)}"
