"""
test_compact_author_result.py - Tests for compact_author_result.py (AC5).

Verifies:
- subagent final response is compact stdout only (no raw body/diff/log in main context)
- body_hash required for ok status
- ISSUE_AUTHOR_RESULT_COMPACT_V1 schema constants are defined
- SUMMARY field is always present in stdout (B1)
- partial_failure is removed; unknown status → ValueError fail-close (B2, B8)
- stdout 2048 UTF-8 bytes limit is enforced at runtime (B3)
- artifact containment is enforced via repo_root (B4)
- artifact content is checked for secrets before writing (B5)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILLS_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from compact_author_result import (
    COMPACT_SCHEMA_NAME,
    COMPACT_SCHEMA_VERSION,
    REQUIRED_COMPACT_FIELDS,
    _atomic_write,
    _no_secret_check,
    compact_author_result,
)

FIXTURES_DIR = SKILLS_ROOT / "fixtures"


# ---------------------------------------------------------------------------
# Schema constants tests
# ---------------------------------------------------------------------------


def test_author_schema_name_is_defined():
    """GIVEN compact_author_result module WHEN importing THEN COMPACT_SCHEMA_NAME is defined."""
    assert COMPACT_SCHEMA_NAME == "ISSUE_AUTHOR_RESULT_COMPACT_V1"


def test_author_schema_version_is_defined():
    """GIVEN compact_author_result module WHEN importing THEN COMPACT_SCHEMA_VERSION is defined."""
    assert COMPACT_SCHEMA_VERSION == "1"


def test_author_required_compact_fields_contains_routing_fields():
    """GIVEN REQUIRED_COMPACT_FIELDS WHEN checked THEN contains SUMMARY, body_hash and artifact."""
    for field in ["STATUS", "SUMMARY", "BODY_HASH", "ARTIFACT", "NEXT_ACTION"]:
        assert field in REQUIRED_COMPACT_FIELDS, f"Missing required field: {field}"


def test_author_valid_statuses_excludes_partial_failure():
    """GIVEN VALID_STATUSES WHEN checked THEN partial_failure is not present (B2)."""
    from compact_author_result import VALID_STATUSES
    assert "partial_failure" not in VALID_STATUSES
    assert VALID_STATUSES == {"ok", "failed", "no_change"}


# ---------------------------------------------------------------------------
# Happy path: ok fixture with checked_body_sha256
# ---------------------------------------------------------------------------


def test_compact_author_result_ok_from_fixture(tmp_path):
    """GIVEN ok fixture with checked_body_sha256 WHEN compact_author_result THEN body_hash and SUMMARY set."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, stdout_lines, *_ = compact_author_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )

    assert compact_data["STATUS"] == "ok"
    assert compact_data["BODY_HASH"] != ""
    assert compact_data["NEXT_ACTION"] == "proceed"
    assert compact_data["ARTIFACT"].startswith("compact_author_result_v1=")
    # B1: SUMMARY must be present
    assert "SUMMARY" in compact_data
    assert compact_data["SUMMARY"] != ""
    lines_text = "\n".join(stdout_lines)
    assert "SUMMARY:" in lines_text


def test_compact_author_result_ok_artifact_written(tmp_path):
    """GIVEN ok fixture WHEN compact_author_result THEN artifact JSON is written."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, _stdout, artifact_path_val, artifact_content = compact_author_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )
    _atomic_write(artifact_path_val, artifact_content)

    artifact_path = Path(compact_data["ARTIFACT"].split("=", 1)[1])
    assert artifact_path.exists()
    artifact_json = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact_json["schema"] == "ISSUE_AUTHOR_RESULT_COMPACT_V1"
    assert artifact_json["status"] == "ok"


def test_compact_author_result_ok_artifact_permissions(tmp_path):
    """GIVEN ok fixture WHEN artifact written THEN file has 0600 permissions."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, _stdout, artifact_path_val, artifact_content = compact_author_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )
    _atomic_write(artifact_path_val, artifact_content)

    artifact_path = Path(compact_data["ARTIFACT"].split("=", 1)[1])
    stat = artifact_path.stat()
    assert oct(stat.st_mode & 0o777) == oct(0o600)


# ---------------------------------------------------------------------------
# body_hash via --updated-body argument
# ---------------------------------------------------------------------------


def test_compact_author_result_body_hash_from_updated_body(tmp_path):
    """GIVEN ok fixture without sha256 WHEN updated_body provided THEN body_hash computed."""
    fixture = FIXTURES_DIR / "author_result_no_body_hash.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    body = "## Updated Issue Body\n\nSome content here."
    compact_data, *_ = compact_author_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42, updated_body=body
    )

    import hashlib
    expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert compact_data["BODY_HASH"] == expected


# ---------------------------------------------------------------------------
# Error path: body_hash missing for ok status
# ---------------------------------------------------------------------------


def test_compact_author_result_missing_body_hash_raises(tmp_path):
    """GIVEN ok fixture without any body hash WHEN compact_author_result THEN ValueError raised."""
    fixture = FIXTURES_DIR / "author_result_no_body_hash.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    with pytest.raises(ValueError, match="body_hash is required"):
        compact_author_result(raw_result, artifact_dir=artifact_dir, issue_number=42)


def test_compact_author_result_cli_missing_body_hash_exits_2(tmp_path):
    """GIVEN CLI with fixture missing body_hash WHEN run THEN exit code is 2."""
    import subprocess

    fixture = FIXTURES_DIR / "author_result_no_body_hash.json"
    script = SCRIPTS_DIR / "compact_author_result.py"

    result = subprocess.run(
        [sys.executable, str(script), "--input-file", str(fixture),
         "--artifact-dir", str(tmp_path), "--issue-number", "42"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# Stdout compliance: no raw content returned to main context (AC5)
# ---------------------------------------------------------------------------


def test_compact_author_result_stdout_no_raw_body(tmp_path):
    """GIVEN ok fixture WHEN stdout generated THEN no raw issue body in stdout."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    _compact, stdout_lines, *_ = compact_author_result(raw_result, artifact_dir=artifact_dir, issue_number=42)
    lines_text = "\n".join(stdout_lines)

    # No diff markers
    assert "diff --git" not in lines_text
    assert "@@ -" not in lines_text
    # No traceback
    assert "Traceback" not in lines_text
    # No raw issue body markers
    assert "## Machine-Readable Contract" not in lines_text


def test_compact_author_result_stdout_byte_limit(tmp_path):
    """GIVEN ok fixture WHEN stdout generated THEN UTF-8 bytes <= 2048."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    _compact, stdout_lines, *_ = compact_author_result(raw_result, artifact_dir=artifact_dir, issue_number=42)
    lines_text = "\n".join(stdout_lines)

    byte_count = len(lines_text.encode("utf-8"))
    assert byte_count <= 2048, f"stdout too large: {byte_count} bytes"


def test_compact_author_result_raw_log_fixture_would_fail_check_script():
    """GIVEN raw log content WHEN check_agent_friendly_stdout called THEN violation detected."""
    # This test validates the fixture would FAIL the stdout checker
    # (simulating raw output being returned to main context)
    import importlib.util

    checker_path = Path(__file__).parent.parent.parent.parent.parent / "scripts" / "check_agent_friendly_stdout.py"
    if not checker_path.exists():
        pytest.skip(f"check_agent_friendly_stdout.py not found at {checker_path}")

    spec = importlib.util.spec_from_file_location("check_agent_friendly_stdout", checker_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    raw_log = "STATUS: failed\nTraceback (most recent call last):\n  File 'foo.py', line 1\nValueError: oops\n"
    violations = module.check_stdout(raw_log)
    assert any("RAW_LOG" in v for v in violations), f"Expected RAW_LOG violation, got: {violations}"


# ---------------------------------------------------------------------------
# B2: partial_failure removed — unknown/invalid status → ValueError (B8)
# ---------------------------------------------------------------------------


def test_compact_author_result_partial_failure_raises_valueerror(tmp_path):
    """GIVEN fixture with partial_failure status WHEN compact_author_result THEN ValueError (B2/B8)."""
    raw_result = {"status": "partial_failure", "checked_body_sha256": "abc123"}
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"
    with pytest.raises(ValueError, match="Unknown/invalid status"):
        compact_author_result(raw_result, artifact_dir=artifact_dir, issue_number=42)


def test_compact_author_result_unknown_status_raises_valueerror(tmp_path):
    """GIVEN fixture with unknown status WHEN compact_author_result THEN ValueError (B8)."""
    raw_result = {"status": "mystery", "checked_body_sha256": "abc123"}
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"
    with pytest.raises(ValueError, match="Unknown/invalid status"):
        compact_author_result(raw_result, artifact_dir=artifact_dir, issue_number=42)


def test_compact_author_result_partial_failure_cli_exits_2(tmp_path):
    """GIVEN CLI with partial_failure status WHEN run THEN exit code is 2 (B2/B8)."""
    import subprocess

    partial_fixture = tmp_path / "partial_failure.json"
    partial_fixture.write_text(
        '{"status": "partial_failure", "checked_body_sha256": "abc123"}', encoding="utf-8"
    )
    script = SCRIPTS_DIR / "compact_author_result.py"
    result = subprocess.run(
        [sys.executable, str(script), "--input-file", str(partial_fixture),
         "--artifact-dir", str(tmp_path), "--issue-number", "42"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# B4: artifact containment
# ---------------------------------------------------------------------------


def test_compact_author_result_containment_check_passes(tmp_path):
    """GIVEN valid repo_root WHEN compact_author_result THEN artifact is under base (B4)."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    repo_root = tmp_path
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, *_ = compact_author_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42, repo_root=repo_root
    )
    assert "compact_author_result_v1=" in compact_data["ARTIFACT"]


def test_compact_author_result_containment_check_rejects_escape(tmp_path):
    """GIVEN artifact_dir outside repo_root WHEN compact_author_result THEN ValueError (B4)."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    # Use a separate tmp dir as repo_root so artifact_dir is outside
    import tempfile
    with tempfile.TemporaryDirectory() as other_root:
        repo_root = Path(other_root) / "repo"
        repo_root.mkdir()
        # artifact_dir is inside tmp_path but repo_root is different
        artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"
        with pytest.raises(ValueError, match="escapes base directory"):
            compact_author_result(
                raw_result, artifact_dir=artifact_dir, issue_number=42, repo_root=repo_root
            )


# ---------------------------------------------------------------------------
# B5: artifact content secret check
# ---------------------------------------------------------------------------


def test_compact_author_result_artifact_secret_check_fails(tmp_path):
    """GIVEN author result with secret-like content WHEN compact_author_result THEN ValueError (B5)."""
    raw_result = {
        "status": "ok",
        "checked_body_sha256": "abc123def456abc123def456abc123def456abc123def456abc123def456abc123",
        "mutation_result": {"diff_summary": "token: ghp_" + "A" * 36},
    }
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"
    with pytest.raises(ValueError, match="secret-like strings detected in artifact content"):
        compact_author_result(raw_result, artifact_dir=artifact_dir, issue_number=42)


# ---------------------------------------------------------------------------
# Artifact path security
# ---------------------------------------------------------------------------


def test_compact_author_result_rejects_path_traversal():
    """GIVEN path with .. WHEN _validate_artifact_path called THEN ValueError raised."""
    from compact_author_result import _validate_artifact_path
    with pytest.raises(ValueError, match="traversal"):
        _validate_artifact_path("../../etc/passwd")


def test_compact_author_result_rejects_absolute_path():
    """GIVEN absolute path WHEN _validate_artifact_path called THEN ValueError raised."""
    from compact_author_result import _validate_artifact_path
    with pytest.raises(ValueError, match="Absolute"):
        _validate_artifact_path("/etc/passwd")


# ---------------------------------------------------------------------------
# #2726: ghs_ pattern widened to detect new-format ghs_<APP_ID>_<JWT> tokens
# ---------------------------------------------------------------------------


def _make_new_format_ghs_token(app_id: str) -> str:
    """Build a deterministic synthetic new-format ghs_ installation token.

    Mirrors the PR #2735 precedent (`tests/test_summarize_agent_transcript.py`)
    for real GitHub App installation tokens: `ghs_<APP_ID>_<header>.<payload>.<signature>`,
    a JWT-shaped body with exactly two '.' separators and '_'/'.'/'-' chars
    present, built up to real installation-token scale (>=520 chars) by
    programmatically padding the payload/signature segments with repeated
    filler rather than hardcoding a 520-char literal. This lets tests exercise
    the dedicated `ghs_[A-Za-z0-9._-]{36,}` matcher's real length regime
    instead of an artificially short fixture.
    """
    jwt_header = "eyJhbGciOiJSUzI1NiJ9"
    jwt_payload = "eyJpc3MiOiJodHRwczovL2V4YW1wbGUuY29tIn0"
    jwt_signature = "s1gN4tuRe-_placeholder123456789"

    token = f"ghs_{app_id}_{jwt_header}.{jwt_payload}.{jwt_signature}"
    while len(token) < 520:
        jwt_payload += "A"
        jwt_signature += "B"
        token = f"ghs_{app_id}_{jwt_header}.{jwt_payload}.{jwt_signature}"

    assert token.count(".") == 2
    assert len(token) >= 520
    return token


def test_no_secret_check_detects_new_format_ghs_installation_token():
    """GIVEN new-format ghs_<APP_ID>_<JWT> token with '_', '.', '-' chars,
    built via _make_new_format_ghs_token() at real (>=520 char) installation-
    token scale (PR #2735 precedent) WHEN _no_secret_check called THEN
    'GitHub server token' violation detected (AC2)."""
    new_format_token = _make_new_format_ghs_token("123456789")
    assert new_format_token.count(".") == 2
    assert len(new_format_token) >= 520
    violations = _no_secret_check(new_format_token)
    assert "GitHub server token" in violations


def test_no_secret_check_detects_legacy_ghs_token():
    """GIVEN legacy fixed-length classic ghs_ token (ghs_ + 36 alnum chars)
    WHEN _no_secret_check called THEN 'GitHub server token' violation still detected (AC4)."""
    legacy = "ghs_" + "A" * 36
    violations = _no_secret_check(legacy)
    assert "GitHub server token" in violations


def test_compact_author_result_new_format_ghs_token_raises_valueerror(tmp_path):
    """GIVEN artifact pass-through field containing a new-format ghs_<APP_ID>_<JWT>
    token (built via _make_new_format_ghs_token(), PR #2735 precedent) WHEN
    compact_author_result called THEN ValueError is raised while assembling the
    artifact JSON, before compact_author_result returns any success result to
    its caller — so the caller never reaches the point where it would write a
    success artifact via its own _atomic_write call (fail-closed semantics
    maintained, AC3).

    The diff_summary text deliberately avoids the pre-existing generic
    `token:` / `secret:` / `api_key:` detector prefixes, so this test proves
    the fail-closed path is driven by the newly widened dedicated `ghs_`
    matcher itself rather than by a false pass through the generic detector.
    """
    new_format_token = _make_new_format_ghs_token("987654321")
    assert new_format_token.count(".") == 2
    assert len(new_format_token) >= 520
    diff_summary = f"credential fixture {new_format_token}"
    # Negative control: confirm this fixture does not incidentally trip the
    # pre-existing generic detectors, so a pass here is attributable only to
    # the dedicated ghs_ matcher.
    assert "token:" not in diff_summary
    assert "secret:" not in diff_summary
    assert "api_key:" not in diff_summary
    raw_result = {
        "status": "ok",
        "checked_body_sha256": "abc123def456abc123def456abc123def456abc123def456abc123def456abc123",
        "mutation_result": {"diff_summary": diff_summary},
    }
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"
    with pytest.raises(ValueError, match="secret-like strings detected in artifact content"):
        compact_author_result(raw_result, artifact_dir=artifact_dir, issue_number=42)
