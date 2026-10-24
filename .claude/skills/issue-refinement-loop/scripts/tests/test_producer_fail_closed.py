#!/usr/bin/env python3
"""
test_producer_fail_closed.py

Tests for Issue #1165: issue-refinement-loop producer fail-closed routing.

AC1: canonical failure envelope (STATUS/NEXT_ACTION/REASON_CODE/ARTIFACT/ARTIFACT_SHA256)
AC2: schema mismatch fixture matrix per script
AC3: output_budget_violation is machine-readable
AC4: publish_termination_report never called on producer failure
AC5: #1154/#1165/#1166 responsibility split in docs (checked by rg VC)
AC6: compact_author_result schema-less consumer contract fixed by fixture
AC7: canonical artifact path .claude/artifacts/issue-refinement-loop/<issue>/
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import unittest.mock as mock
from pathlib import Path

# ---------------------------------------------------------------------------
# Script paths
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
COMPACT_REVIEW_SCRIPT = _SCRIPTS_DIR / "compact_review_result.py"
COMPACT_AUTHOR_SCRIPT = _SCRIPTS_DIR / "compact_author_result.py"
PREFLIGHT_SCRIPT = _SCRIPTS_DIR / "run_refinement_preflight.py"
PUBLISH_SCRIPT = _SCRIPTS_DIR / "publish_termination_report.py"

# Canonical artifact path for production invocations (AC7)
CANONICAL_ARTIFACT_BASE = ".claude/artifacts/issue-refinement-loop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_FAILURE_FIELDS = {
    "STATUS: failed",
    "NEXT_ACTION:",
    "REASON_CODE:",
    "ARTIFACT:",
    "ARTIFACT_SHA256:",
}


def _assert_failure_envelope(stdout: str) -> None:
    """Assert that stdout contains all required canonical failure envelope fields."""
    for field in _REQUIRED_FAILURE_FIELDS:
        assert field in stdout, (
            f"Canonical failure envelope missing {field!r}.\n"
            f"stdout={stdout!r}"
        )
    # Envelope must be ≤ 2048 UTF-8 bytes (AC1)
    byte_count = len(stdout.encode("utf-8"))
    assert byte_count <= 2048, (
        f"Failure envelope stdout exceeds 2048 bytes: {byte_count} bytes\n"
        f"stdout={stdout!r}"
    )


def _minimal_valid_review_result() -> dict:
    """Build a minimal REVIEW_ISSUE_RESULT_V1 that passes schema validation."""
    return {
        "schema": "REVIEW_ISSUE_RESULT_V1",
        "schema_version": "1",
        "verdict": "approve",
        "status": "ok",
        "body_sha256": "sha256:" + "a" * 64,
        "issue_kind": "implementation",
        "generated_at": "2024-01-01T00:00:00Z",
        "deterministic_checks": {},
        "blocking_issues": [],
        "structured_blockers": [],
        "non_blocking_improvements": [],
        "findings": [],
        "diff_proposal": {},
        "parsed_vc_commands": [],
    }


# ---------------------------------------------------------------------------
# AC1 / AC2: compact_review_result schema mismatch → canonical failure envelope
# ---------------------------------------------------------------------------


def test_review_compact_schema_mismatch_emits_failure_artifact(tmp_path):
    """Issue #2054 AC5: compact_review_result.py's CLI is retired (V1
    producer removed; ISSUE_REVIEW_RESULT_COMPACT_V2 has no downgrade
    fallback). Any invocation -- valid or invalid input -- always fails
    closed (exit 2), performs no artifact I/O, and never leaks its input
    verbatim to stdout."""
    valid_input = json.dumps(_minimal_valid_review_result())
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=valid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, f"retired CLI must always fail closed. stderr={result.stderr!r}"
    assert result.stdout == ""
    assert "retired" in result.stderr.lower()
    assert not artifact_dir.exists(), "retired CLI must perform no artifact I/O"


def test_review_compact_invalid_verdict_emits_failure_artifact(tmp_path):
    """Issue #2054 AC5: retired CLI fails closed identically for an invalid
    verdict input -- no schema_mismatch envelope is produced anymore."""
    invalid_input = json.dumps({
        "schema": "REVIEW_ISSUE_RESULT_V1",
        "schema_version": "1",
        "verdict": "invalid_verdict",  # not in VALID_VERDICTS
        "status": "ok",
        "body_sha256": "sha256:" + "a" * 64,
        "issue_kind": "impl",
        "generated_at": "2024-01-01T00:00:00Z",
        "deterministic_checks": {},
        "blocking_issues": [],
        "structured_blockers": [],
        "non_blocking_improvements": [],
        "findings": [],
        "diff_proposal": {},
        "parsed_vc_commands": [],
    })
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert not artifact_dir.exists()


# ---------------------------------------------------------------------------
# AC3: output budget violation → machine-readable failure envelope
# ---------------------------------------------------------------------------


def test_review_compact_noncanonical_relative_artifact_dir_fails_closed(tmp_path):
    """Issue #2054 AC5: retired CLI fails closed identically regardless of
    the (now-ignored) --artifact-dir value; no artifact is ever written."""
    long_artifact_dir = (
        Path("x" * 200)
        / ("y" * 200)
        / ("z" * 200)
        / ("w" * 200)
        / ("v" * 200)
    )
    valid_input = json.dumps(_minimal_valid_review_result())

    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(long_artifact_dir),
            "--issue-number", "1165",
        ],
        input=valid_input,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 2, (
        f"Retired CLI should always fail closed. stdout={result.stdout!r}"
    )
    assert result.stdout == ""
    assert "VERDICT:" not in result.stdout
    assert "SUMMARY:" not in result.stdout
    assert not (tmp_path / long_artifact_dir).exists()


# ---------------------------------------------------------------------------
# AC2 / AC6: compact_author_result schema mismatch → canonical failure envelope
# ---------------------------------------------------------------------------


def test_author_compact_schema_mismatch_emits_failure_artifact(tmp_path):
    """AC2/AC6: compact_author_result.py emits canonical failure envelope on schema mismatch.

    Schema-less consumer contract (AC6):
    - status must be present and in VALID_STATUSES ("ok", "failed", "no_change")
    - Rejection: missing status or invalid status → REASON_CODE: schema_mismatch (B1)
    """
    # Case 1: Missing status field entirely (B1: must be schema_mismatch, not treated as "ok")
    missing_status_input = json.dumps({
        "comment_url": "",
        "checked_body_sha256": "sha256:" + "a" * 64,
    })
    artifact_dir = tmp_path / "artifacts"
    result_missing = subprocess.run(
        [
            sys.executable, str(COMPACT_AUTHOR_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=missing_status_input,
        capture_output=True,
        text=True,
    )
    assert result_missing.returncode != 0, (
        f"B1: missing status must exit non-zero (not default to ok). "
        f"stdout={result_missing.stdout!r}"
    )
    _assert_failure_envelope(result_missing.stdout)
    assert "REASON_CODE: schema_mismatch" in result_missing.stdout, (
        f"B1: missing status must produce schema_mismatch. stdout={result_missing.stdout!r}"
    )

    # Case 2: Invalid status - not in VALID_STATUSES
    invalid_input = json.dumps({
        "status": "completely_invalid_status",
        "comment_url": "",
        "checked_body_sha256": "",
    })
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_AUTHOR_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"Schema mismatch should exit non-zero. stdout={result.stdout!r}"
    )
    _assert_failure_envelope(result.stdout)
    assert "REASON_CODE: schema_mismatch" in result.stdout, (
        f"REASON_CODE must be schema_mismatch. stdout={result.stdout!r}"
    )


def test_author_compact_ok_without_body_hash_emits_failure(tmp_path):
    """AC6: compact_author_result.py fails when status=ok but no body_hash provided."""
    invalid_input = json.dumps({
        "status": "ok",
        "comment_url": "",
        # No checked_body_sha256, no --updated-body, no --updated-body-file
    })
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_AUTHOR_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "status=ok without body_hash should fail"
    _assert_failure_envelope(result.stdout)
    assert "REASON_CODE: schema_mismatch" in result.stdout


def test_author_compact_schema_less_contract_fields():
    """AC6: ISSUE_AUTHOR_RESULT_V1_SCHEMA_LESS_CONTRACT exists and documents checked fields."""
    spec = importlib.util.spec_from_file_location(
        "compact_author_result_ac6",
        str(COMPACT_AUTHOR_SCRIPT),
    )
    car = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(car)  # type: ignore[union-attr]

    contract = getattr(car, "ISSUE_AUTHOR_RESULT_V1_SCHEMA_LESS_CONTRACT", None)
    assert contract is not None, "ISSUE_AUTHOR_RESULT_V1_SCHEMA_LESS_CONTRACT must exist"
    assert contract.get("consumer_mode") == "schema_less"
    assert "checked_fields" in contract
    assert "status" in contract["checked_fields"]
    assert "unchecked_fields" in contract
    # Rejection reason code must be documented
    assert contract["checked_fields"]["status"].get("rejection_reason_code") == "schema_mismatch"


# ---------------------------------------------------------------------------
# AC2: preflight planner_fail_closed_payload_invalid routes environment_failure
# ---------------------------------------------------------------------------


def test_preflight_planner_fail_closed_payload_invalid_routes_environment_failure():
    """AC2: BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID routes to environment_failure (not blocked).

    This verifies the fail-closed routing table in run_refinement_preflight.py.
    """
    spec = importlib.util.spec_from_file_location(
        "run_refinement_preflight_ac2",
        str(PREFLIGHT_SCRIPT),
    )
    rfp = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(rfp)  # type: ignore[union-attr]

    blocker = rfp.BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID

    # Bare blocker code → environment_failure
    status, exit_code = rfp._apply_exit_code_mapping(
        planner_exit_code=None,
        planner_fail_closed=None,
        blockers=[blocker],
    )
    assert status == "environment_failure", (
        f"BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID must route to environment_failure, "
        f"got {status!r}"
    )
    assert exit_code == rfp.EXIT_ENVIRONMENT_FAILURE

    # Blocker with detail (colon-separated) → still environment_failure
    blocker_with_detail = f"{blocker}: some invalid payload detail"
    status2, exit_code2 = rfp._apply_exit_code_mapping(
        planner_exit_code=None,
        planner_fail_closed=None,
        blockers=[blocker_with_detail],
    )
    assert status2 == "environment_failure", (
        f"Blocker with detail must also route to environment_failure, got {status2!r}"
    )

    # Verify it does NOT route to "blocked"
    assert status != "blocked", "planner_fail_closed_payload_invalid must NOT route to blocked"
    assert status2 != "blocked"


# ---------------------------------------------------------------------------
# AC4: producer failure never invokes publish_termination_report
# ---------------------------------------------------------------------------


def test_producer_failure_never_invokes_publish_termination_report(tmp_path):
    """AC4: producer failure (schema mismatch) never invokes publish_termination_report.

    Verifies via:
    1. Static AST analysis: compact scripts must not import publish_termination_report
    2. Behavioral: failure exit has no publish-related output
    """
    # --- Static analysis ---
    for script_path, name in [
        (COMPACT_REVIEW_SCRIPT, "compact_review_result"),
        (COMPACT_AUTHOR_SCRIPT, "compact_author_result"),
    ]:
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "publish_termination_report", (
                    f"{name} must not import publish_termination_report"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "publish_termination_report", (
                        f"{name} must not import publish_termination_report"
                    )

    # --- Behavioral: schema mismatch → failure, no gh comment attempt ---
    invalid_input = json.dumps({"verdict": "approve"})  # missing required schema fields
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "Schema mismatch should fail"
    combined = result.stdout + result.stderr
    # publish_termination_report writes "[publish_termination_report] comment posted"
    assert "[publish_termination_report]" not in combined, (
        "publish_termination_report must not be invoked on producer failure"
    )
    assert "gh issue comment" not in combined, (
        "gh issue comment must not be called on producer failure"
    )
    assert "comment posted" not in combined


def test_termination_bypass_fixture_call_count_zero(tmp_path):
    """AC4: call count for publish() and _post_github_comment() = 0 during producer failure.

    Uses monkeypatching to ensure that even if publish_termination_report were somehow
    imported, its publish() and _post_github_comment() functions are never called.
    """
    # Load compact_review_result with a mocked publish_termination_report in sys.modules
    mock_ptr = mock.MagicMock()
    publish_calls: list = []
    gh_calls: list = []

    mock_ptr.publish = mock.MagicMock(side_effect=lambda **kw: publish_calls.append(kw) or 0)
    mock_ptr._post_github_comment = mock.MagicMock(
        side_effect=lambda **kw: gh_calls.append(kw) or -1
    )

    with mock.patch.dict(sys.modules, {"publish_termination_report": mock_ptr}):
        spec = importlib.util.spec_from_file_location(
            "crr_ac4_fixture",
            str(COMPACT_REVIEW_SCRIPT),
        )
        crr = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(crr)  # type: ignore[union-attr]

        artifact_dir = tmp_path / "artifacts"
        # Call compact_review_result with invalid input (missing verdict → ValueError)
        try:
            crr.compact_review_result(
                {"status": "ok"},  # missing verdict → ValueError
                artifact_dir=artifact_dir,
                issue_number=1165,
            )
        except (ValueError, Exception):
            pass  # Expected failure

    # Call count must be zero for both publish() and _post_github_comment()
    assert len(publish_calls) == 0, (
        f"publish() must not be called on producer failure "
        f"(was called {len(publish_calls)} times)"
    )
    assert len(gh_calls) == 0, (
        f"_post_github_comment() must not be called on producer failure "
        f"(was called {len(gh_calls)} times)"
    )


# ---------------------------------------------------------------------------
# AC1: failure stdout never contains raw issue body or comment
# ---------------------------------------------------------------------------


def test_failure_stdout_never_contains_raw_issue_body_or_comment(tmp_path):
    """Issue #2054 AC5/AC11: the retired CLI never echoes stdin content --
    it never even reads it (fails closed before any input is consumed)."""
    RAW_BODY_SENTINEL = "THIS_IS_RAW_ISSUE_BODY_CONTENT_DO_NOT_EMIT_12345"
    RAW_COMMENT_SENTINEL = "THIS_IS_RAW_COMMENT_CONTENT_DO_NOT_EMIT_67890"

    invalid_input_with_raw = json.dumps({
        "verdict": "invalid_verdict_that_should_not_appear_in_stdout",
        "status": "ok",
        "raw_body": RAW_BODY_SENTINEL,
        "raw_comment": RAW_COMMENT_SENTINEL,
    })
    artifact_dir = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(artifact_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input_with_raw,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert RAW_BODY_SENTINEL not in combined
    assert RAW_COMMENT_SENTINEL not in combined
    assert result.stdout == ""


def test_canonical_artifact_path_in_failure_artifact(tmp_path):
    """Issue #2054 AC5/AC7: the retired CLI writes no artifact anywhere --
    no wire reference, canonical or otherwise, is ever emitted."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    invalid_input = json.dumps({"verdict": "not_valid"})

    result = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--repo-root", str(repo_root),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, f"stdout={result.stdout!r}"
    assert result.stdout == ""
    assert not any(repo_root.rglob("*")), "retired CLI must perform no artifact I/O"

    arbitrary_dir = tmp_path / "arbitrary"
    result2 = subprocess.run(
        [
            sys.executable, str(COMPACT_REVIEW_SCRIPT),
            "--artifact-dir", str(arbitrary_dir),
            "--issue-number", "1165",
        ],
        input=invalid_input,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result2.returncode == 2, f"stdout={result2.stdout!r}"
    assert result2.stdout == ""
    assert not arbitrary_dir.exists()
