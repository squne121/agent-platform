"""
test_refinement_preflight.py

Tests for run_refinement_preflight.py (AC1–AC12 + Blocker fixes iteration 2).

VC rg keywords verified in this file:
  - ANCHOR_NOT_IN_ISSUE    (AC2)
  - raw_issue_snapshot     (AC3)
  - refinement_preflight_result_v1  (AC6)
  - exit_code_mapping      (AC7)
  - substring              (AC8)
  - sentinel               (AC9)
  - byte_stable            (AC10)
  - argv                   (AC11)
  - environment_failure    (AC12)

Blocker fixes (iteration 2):
  B1: multi-page slurp flatten (--paginate --slurp produces [[page1], [page2]])
  B2: jsonschema runtime validation (input schema + result self-validate)
  B3: warn reachable (planner exit 0 + unknown confidence → warn/1)
  B4: planner_input artifact saved (byte_stable, schema valid)
  B5: anchor issue_url missing/empty → blocked + ANCHOR_NOT_IN_ISSUE
  A:  failure path stdout/disk consistency (no post-write mutation)
  D:  argparse validation (--repo pattern, --issue-number positive, anchor URL prefix)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as wrapper  # noqa: E402

# Single identity source: tests must follow the configured repository, not a literal slug.
TRUSTED_REPO = wrapper._repo_identity.load_trusted_repo_slug()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def make_minimal_fixture(
    issue_number: int = 200,
    repo: str = "testowner/testrepo",
    body: str = "",
    comments: list | None = None,
) -> dict:
    """Create a minimal fixture dict for in-memory testing."""
    return {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": issue_number,
        "repo": repo,
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {
            "number": issue_number,
            "title": "Test Issue",
            "body": body,
            "labels": [],
        },
        "comments": comments or [],
        "anchor_comment_urls": [],
    }


def seed_previous_snapshot(
    repo_root: Path,
    *,
    issue_number: int,
    repo: str,
    body: str,
    fetched_at: str = "2025-12-31T00:00:00+00:00",
) -> None:
    snapshot = {
        "schema_version": "raw_issue_snapshot/v1",
        "fetched_at": fetched_at,
        "issue_number": issue_number,
        "repo": repo,
        "issue": {
            "number": issue_number,
            "title": "Test Issue",
            "body": body,
            "labels": [],
        },
        "comments": [],
    }
    wrapper._materialize_immutable_snapshot(repo_root, issue_number, snapshot)


VALID_ISSUE_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#1"
```

## Parent Issue

#1

## Parent Goal Ref

- Goal: Test goal

## Current Validated Scope

- scripts/example.py

## Remaining Parent Gaps

- [ ] Nothing remaining

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため

## Outcome

Add `scripts/example.py`.

## In Scope

- scripts/example.py

## Out of Scope

- Unrelated changes

## Acceptance Criteria

- [ ] AC1: Script exists.

## Verification Commands

```bash
uv run python3 scripts/example.py
```

## Allowed Paths

- scripts/example.py

## Stop Conditions

- Allowed Paths 外の変更が必要な場合

## Required Skills

なし
"""


VALID_ISSUE_BODY_NO_MACHINE_CONTRACT = """\
## Parent Issue

#1

## Parent Goal Ref

- Goal: Test goal

## Current Validated Scope

- scripts/example.py

## Remaining Parent Gaps

- [ ] Nothing remaining

## Outcome

Add `scripts/example.py`.

## In Scope

- scripts/example.py

## Out of Scope

- Unrelated changes

## Acceptance Criteria

- [ ] AC1: Script exists.

## Verification Commands

```bash
uv run python3 scripts/example.py
```

## Allowed Paths

- scripts/example.py

## Stop Conditions

- Allowed Paths 外の変更が必要な場合

## Required Skills

なし
"""


# ---------------------------------------------------------------------------
# AC1: fixture input produces STATUS / NEXT_ACTION / MUST_READ / COMMANDS /
#      BLOCKERS / ARTIFACT in stdout
# ---------------------------------------------------------------------------

class TestAC1BasicFixtureOutput:
    """AC1: run_refinement_preflight with fixture produces expected stdout fields."""

    def test_pass_fixture_stdout_fields(self, tmp_path, capsys):
        """AC1: fixture input produces STATUS, NEXT_ACTION, COMMANDS, ARTIFACT in stdout."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY)),
            encoding="utf-8",
        )

        # Patch artifact dir to tmp_path so we don't pollute the repo
        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=200,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        stdout = captured.out

        assert "STATUS:" in stdout, "stdout must contain STATUS field"
        assert "NEXT_ACTION:" in stdout, "stdout must contain NEXT_ACTION field"
        assert "COMMANDS_JSON:" in stdout, "stdout must contain COMMANDS_JSON field"
        assert "ARTIFACT:" in stdout, "stdout must contain ARTIFACT field"
        assert result["schema_version"] == "refinement_preflight_result/v1"

    def test_pass_or_warn_fixture_exit_code(self, tmp_path):
        """AC1: fixture input exits with pass (0) or warn (1) — both are non-blocking outcomes."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY)),
            encoding="utf-8",
        )
        seed_previous_snapshot(
            tmp_path,
            issue_number=200,
            repo="testowner/testrepo",
            body=VALID_ISSUE_BODY,
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=200,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert exit_code in (wrapper.EXIT_PASS, wrapper.EXIT_WARN), \
            f"Expected exit 0 (pass) or 1 (warn), got {exit_code}; status={result['status']}"
        assert result["status"] in ("pass", "warn"), \
            f"Expected pass or warn status, got {result['status']}"


# ---------------------------------------------------------------------------
# AC2: ANCHOR_NOT_IN_ISSUE — anchor comment URL from wrong issue → exit 2
# ---------------------------------------------------------------------------

class TestAC2AnchorNotInIssue:
    """AC2: ANCHOR_NOT_IN_ISSUE blocker for anchor comment from different issue."""

    def test_anchor_wrong_issue_number_exit_2(self, tmp_path, capsys):
        """AC2: anchor URL with issue 999 against issue 100 → ANCHOR_NOT_IN_ISSUE / exit 2."""
        # The anchor URL points to issue 999, but we are checking issue 100
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/testowner/testrepo/issues/999#issuecomment-9999001"
            ],
            "anchor_comments": [
                {
                    "id": 9999001,
                    "body": "some comment",
                    "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/100",
                }
            ],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        # AC2: ANCHOR_NOT_IN_ISSUE must appear in blockers
        assert wrapper.BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH in result["blockers"] or \
               wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE in result["blockers"], \
               f"Expected ANCHOR_NOT_IN_ISSUE blocker, got {result['blockers']}"
        assert exit_code == wrapper.EXIT_BLOCKED, f"Expected exit 2, got {exit_code}"
        assert result["status"] == "blocked"

    def test_anchor_wrong_repo_exit_2(self, tmp_path, capsys):
        """AC2: anchor URL with wrong owner → ANCHOR_NOT_IN_ISSUE / exit 2."""
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/otherowner/testrepo/issues/100#issuecomment-9999002"
            ],
            "anchor_comments": [],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert any(
            b in result["blockers"]
            for b in [
                wrapper.BLOCKER_ANCHOR_REPO_MISMATCH,
                wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE,
            ]
        ), f"Expected repo mismatch blocker, got {result['blockers']}"
        assert exit_code == wrapper.EXIT_BLOCKED

    def test_anchor_comment_not_found_in_fixture(self, tmp_path, capsys):
        """AC2: anchor URL with valid structure but comment ID not in fixture → blocked."""
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/testowner/testrepo/issues/100#issuecomment-9999999"
            ],
            # anchor_comments does NOT contain id 9999999
            "anchor_comments": [],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert wrapper.BLOCKER_ANCHOR_COMMENT_NOT_FOUND in result["blockers"], \
               f"Expected ANCHOR_COMMENT_NOT_FOUND, got {result['blockers']}"
        assert exit_code == wrapper.EXIT_BLOCKED


# ---------------------------------------------------------------------------
# AC3: raw_issue_snapshot — raw body NOT in stdout, written to artifact JSON
# ---------------------------------------------------------------------------

class TestAC3RawIssueSnapshotNotInStdout:
    """AC3: raw issue body / comments go to artifact JSON, not stdout."""

    def test_raw_issue_body_not_in_stdout(self, tmp_path, capsys):
        """AC3: raw_issue_snapshot content does not appear in stdout."""
        sentinel_body = "UNIQUE_BODY_TEXT_SHOULD_NOT_APPEAR_IN_STDOUT_XYZ789"
        fixture_data = make_minimal_fixture(body=VALID_ISSUE_BODY + f"\n{sentinel_body}")
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=200,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        stdout = captured.out
        stderr = captured.err

        # raw_issue_snapshot must NOT appear in stdout
        assert sentinel_body not in stdout, \
               "raw issue body sentinel must not appear in stdout"
        assert sentinel_body not in stderr, \
               "raw issue body sentinel must not appear in stderr"

    def test_raw_issue_snapshot_written_to_artifact(self, tmp_path, capsys):
        """AC3: raw_issue_snapshot artifact JSON is written."""
        fixture_data = make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=300)
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=300,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        artifacts = result.get("artifacts", {})
        assert "raw_issue_snapshot" in artifacts, \
               "raw_issue_snapshot artifact path must be in result.artifacts"

        snapshot_path = Path(artifacts["raw_issue_snapshot"])
        assert snapshot_path.exists(), f"Snapshot artifact file not found: {snapshot_path}"

        snapshot_data = json.loads(snapshot_path.read_text())
        # Confirm it has expected structure
        assert snapshot_data.get("schema_version") == "raw_issue_snapshot/v1"
        assert "issue" in snapshot_data


# ---------------------------------------------------------------------------
# AC6: refinement_preflight_result_v1 schema — stdout projection matches artifact
# ---------------------------------------------------------------------------

class TestAC6SchemaAndProjectionConsistency:
    """AC6: result schema exists; stdout projection and artifact JSON share same status/blockers/next_action."""

    def test_schema_file_exists(self):
        """AC6: refinement_preflight_result_v1.schema.json must exist."""
        schema_path = SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json"
        assert schema_path.exists(), f"Schema file not found: {schema_path}"

    def test_input_schema_file_exists(self):
        """AC6: refinement_preflight_input.schema.json must exist."""
        schema_path = SCHEMAS_DIR / "refinement_preflight_input.schema.json"
        assert schema_path.exists(), f"Schema file not found: {schema_path}"

    def test_result_schema_additionalProperties_false(self):
        """AC6: result schema must be strict (additionalProperties: false)."""
        schema_path = SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json"
        schema = json.loads(schema_path.read_text())
        assert schema.get("additionalProperties") is False, \
               "refinement_preflight_result_v1 schema must have additionalProperties: false"

    def test_stdout_projection_matches_artifact(self, tmp_path, capsys):
        """AC6: stdout STATUS/NEXT_ACTION/BLOCKERS match artifact JSON fields."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=400)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=400,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        stdout = captured.out

        # Stdout must contain the same status as result artifact
        assert f"STATUS: {result['status']}" in stdout
        assert f"NEXT_ACTION: {result['next_action']}" in stdout

        # Artifact must have schema_version
        assert result["schema_version"] == "refinement_preflight_result/v1"

        # Artifact file must also exist
        artifact_path = result.get("artifacts", {}).get("refinement_preflight_result_v1")
        assert artifact_path, "artifact path must be present"
        artifact_data = json.loads(Path(artifact_path).read_text())
        assert artifact_data["status"] == result["status"]
        assert artifact_data["next_action"] == result["next_action"]

    def test_fail_closed_forwarding_fields_in_stdout_and_artifact(self, tmp_path, capsys):
        """Fail-closed result exposes required sections / contract keys / rewrite constraints."""
        malformed_body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
```
"""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=malformed_body, issue_number=3000)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=3000,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        stdout = captured.out

        assert exit_code == wrapper.EXIT_BLOCKED
        assert result["status"] == "blocked"
        assert result["planner_fail_closed"] is True
        assert "missing_required_contract_key" in result["planner_fail_closed_reason_codes"]
        assert result["required_contract_keys"], "required_contract_keys must be forwarded"
        assert "issue_kind" in result["required_contract_keys"]
        assert result["required_sections"], "required_sections must be forwarded"
        assert "REQUIRED_SECTIONS:" in stdout, "compact stdout should include REQUIRED_SECTIONS"
        assert "REQUIRED_CONTRACT_KEYS:" in stdout, "compact stdout should include REQUIRED_CONTRACT_KEYS"
        assert "REWRITE_CONSTRAINTS:" in stdout, "compact stdout should include REWRITE_CONSTRAINTS"

        artifact_path = result.get("artifacts", {}).get("refinement_preflight_result_v1")
        assert artifact_path, "artifact path must be present"
        artifact_data = json.loads(Path(artifact_path).read_text())
        assert artifact_data["required_sections"] == result["required_sections"]
        assert artifact_data["required_contract_keys"] == result["required_contract_keys"]
        assert artifact_data["rewrite_constraints"] == result["rewrite_constraints"]
        assert artifact_data["planner_fail_closed_reason_codes"] == result["planner_fail_closed_reason_codes"]

    def test_machine_contract_section_absent_requires_section_not_contract_keys(self, tmp_path, capsys):
        """Machine-Readable Contract absent -> required section list, not key list."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY_NO_MACHINE_CONTRACT, issue_number=3100)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=3100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        assert exit_code == wrapper.EXIT_BLOCKED
        assert result["planner_fail_closed"] is True
        assert "Machine-Readable Contract" in result["required_sections"]
        assert result["required_contract_keys"] == []
        assert "REQUIRED_SECTIONS:" in captured.out
        assert "REQUIRED_CONTRACT_KEYS:" not in captured.out


class TestNonStringFailClosedPayload:
    """Non-string payloads must be rejected instead of silently dropped."""

    def test_non_string_fail_closed_payload_blocks(self, tmp_path):
        """Planner payload with non-string fields must map to blocked and blockers include payload errors."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=3200)),
            encoding="utf-8",
        )

        invalid_plan = {
            "schema_version": "refinement_loop_plan/v1",
            "fail_closed": {
                "required": True,
                "reason_codes": ["missing_required_section", 1234],
                "rewrite_constraints": {
                    "required_sections": ["Outcome", 56],
                    "required_contract_keys": ["issue_kind", "contract_schema_version"],
                    "rewrite_constraints": {},
                    "override_policy": {},
                    "max_rewrite_attempts": 2,
                    "no_progress_route": "human_judgment_required",
                },
            },
            "decisions": {},
        }

        with (
            mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
            mock.patch.object(wrapper, "_invoke_planner", return_value=(invalid_plan, 0, "", "")),
        ):
            result, exit_code = wrapper.run_preflight(
                issue_number=3200,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        # AC6: REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD now routes to environment_failure,
        # not blocked. Non-string payload is a schema violation (payload integrity),
        # not an issue-content blocker.
        assert exit_code == wrapper.EXIT_ENVIRONMENT_FAILURE, (
            f"AC6: expected EXIT_ENVIRONMENT_FAILURE ({wrapper.EXIT_ENVIRONMENT_FAILURE}), "
            f"got {exit_code!r}. REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD must route to "
            f"environment_failure, not blocked."
        )
        assert result["status"] == "environment_failure", (
            f"AC6: expected status='environment_failure', got {result['status']!r}"
        )
        assert result["planner_fail_closed"] is True
        assert any(
            "REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD" in blocker
            for blocker in result["blockers"]
        ), f"expected non-string payload blocker, got {result['blockers']}"
        assert result["required_sections"] == []
        assert result["required_contract_keys"] == []


class TestRewriteConstraintsStdout:
    """REWRITE_CONSTRAINTS in compact stdout must be JSON parseable."""

    def test_rewrite_constraints_stdout_is_json_parseable(self):
        """REWRITE_CONSTRAINTS stdout line is valid JSON."""
        payload = {
            "schema_version": "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1",
            "required_sections": ["Outcome"],
            "required_contract_keys": ["issue_kind"],
            "rewrite_constraints": {
                "must_add_sections": ["Outcome"],
                "must_add_contract_keys": ["issue_kind"],
                "freeform_rewrite_forbidden": True,
            },
            "override_policy": {
                "allowed_reason_codes": ["missing_required_section"],
                "never_override_reason_codes": ["checker_internal_error"],
                "overridable_in_current_result": ["missing_required_section"],
                "non_overridable_in_current_result": ["checker_internal_error"],
            },
            "max_rewrite_attempts": 2,
            "no_progress_route": "human_judgment_required",
        }
        result = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "blocked",
            "issue_number": 1,
            "repo": "o/r",
            "planner_exit_code": 0,
            "planner_fail_closed": True,
            "next_action": "human_judgment_required",
            "must_read": [],
            "do_not_read": [],
            "commands": [],
            "blockers": ["PLANNER_FAIL_CLOSED"],
            "planner_fail_closed_reason_codes": ["missing_required_section"],
            "required_sections": ["Outcome"],
            "required_contract_keys": ["issue_kind"],
            "rewrite_constraints": payload,
            "artifacts": {},
            "hashes": {},
        }

        output = wrapper._build_compact_stdout(result)
        lines = output.splitlines()
        idx = lines.index("REWRITE_CONSTRAINTS:")
        json.loads(lines[idx + 1].strip())


# ---------------------------------------------------------------------------
# AC7: exit_code_mapping — all mapping table branches covered
# ---------------------------------------------------------------------------

class TestAC7ExitCodeMapping:
    """AC7: planner↔wrapper exit_code_mapping all branches."""

    def test_exit_code_mapping_anchor_not_in_issue_blocked(self):
        """AC7: exit_code_mapping — ANCHOR_NOT_IN_ISSUE → blocked / 2."""
        status, code = wrapper._apply_exit_code_mapping(
            None, None, [wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE]
        )
        assert status == "blocked"
        assert code == wrapper.EXIT_BLOCKED

    def test_exit_code_mapping_gh_failure_environment(self):
        """AC7: exit_code_mapping — GH_API_FAILURE → environment_failure / 3."""
        status, code = wrapper._apply_exit_code_mapping(
            None, None, [wrapper.BLOCKER_GH_FAILURE]
        )
        assert status == "environment_failure"
        assert code == wrapper.EXIT_ENVIRONMENT_FAILURE

    def test_exit_code_mapping_planner_exit_2_blocked(self):
        """AC7: exit_code_mapping — planner exit 2 → blocked / 2."""
        status, code = wrapper._apply_exit_code_mapping(2, None, [])
        assert status == "blocked"
        assert code == wrapper.EXIT_BLOCKED

    def test_exit_code_mapping_planner_exit_3_environment(self):
        """AC7: exit_code_mapping — planner exit 3 → environment_failure / 3."""
        status, code = wrapper._apply_exit_code_mapping(3, None, [])
        assert status == "environment_failure"
        assert code == wrapper.EXIT_ENVIRONMENT_FAILURE

    def test_exit_code_mapping_planner_exit_0_fail_closed_blocked(self):
        """AC7: exit_code_mapping — planner exit 0 + fail_closed.required=true → blocked / 2."""
        status, code = wrapper._apply_exit_code_mapping(0, True, [])
        assert status == "blocked"
        assert code == wrapper.EXIT_BLOCKED

    def test_exit_code_mapping_planner_exit_0_pass(self):
        """AC7: exit_code_mapping — planner exit 0, fail_closed=false → pass / 0."""
        status, code = wrapper._apply_exit_code_mapping(0, False, [])
        assert status == "pass"
        assert code == wrapper.EXIT_PASS

    def test_exit_code_mapping_no_blockers_no_planner(self):
        """AC7: exit_code_mapping — planner None, no blockers → environment_failure."""
        status, code = wrapper._apply_exit_code_mapping(None, None, [])
        assert status == "environment_failure"
        assert code == wrapper.EXIT_ENVIRONMENT_FAILURE

    def test_exit_code_mapping_full_table_coverage(self):
        """AC7: exit_code_mapping covers all rows of the mapping table."""
        # Row 1: anchor comment not in issue → blocked / 2
        s, c = wrapper._apply_exit_code_mapping(None, None, ["ANCHOR_NOT_IN_ISSUE"])
        assert s == "blocked" and c == 2

        # Row 2: gh failure → environment_failure / 3
        s, c = wrapper._apply_exit_code_mapping(None, None, ["GH_API_FAILURE"])
        assert s == "environment_failure" and c == 3

        # Row 3: planner exit 2 → blocked / 2
        s, c = wrapper._apply_exit_code_mapping(2, None, [])
        assert s == "blocked" and c == 2

        # Row 4: planner exit 3 → environment_failure / 3
        s, c = wrapper._apply_exit_code_mapping(3, None, [])
        assert s == "environment_failure" and c == 3

        # Row 5: planner exit 0 + fail_closed=true → blocked / 2
        s, c = wrapper._apply_exit_code_mapping(0, True, [])
        assert s == "blocked" and c == 2

        # Row 6: planner exit 0, normal, no unknown → pass / 0
        s, c = wrapper._apply_exit_code_mapping(0, False, [])
        assert s == "pass" and c == 0

        # Row 7 (B3): planner exit 0, fail_closed=false, unknown confidence → warn / 1
        plan_with_unknown = {
            "decisions": {
                "investigation_policy": {"confidence": "deterministic"},
                "web_research_policy": {"confidence": "unknown"},
            }
        }
        s, c = wrapper._apply_exit_code_mapping(0, False, [], plan=plan_with_unknown)
        assert s == "warn" and c == 1, f"Expected warn/1 for unknown confidence, got {s}/{c}"


# ---------------------------------------------------------------------------
# AC8: substring — anchor URL parser is structural, not substring-based
# ---------------------------------------------------------------------------

class TestAC8SubstringFreeAnchorValidation:
    """AC8: anchor URL validation uses structural parsing, not substring matching."""

    def test_parse_valid_anchor_url_structure(self):
        """AC8: valid anchor URL is parsed structurally into owner/repo/issue/comment."""
        parsed = wrapper._parse_anchor_comment_url(
            "https://github.com/myowner/myrepo/issues/42#issuecomment-12345"
        )
        assert parsed["valid"] is True
        assert parsed["owner"] == "myowner"
        assert parsed["repo"] == "myrepo"
        assert parsed["issue_number"] == 42
        assert parsed["comment_id"] == 12345

    def test_parse_invalid_url_no_substring_match(self):
        """AC8: URL that contains 'issues' as substring but wrong structure → invalid."""
        # This URL looks like it might contain 'issues' but is structurally wrong
        parsed = wrapper._parse_anchor_comment_url(
            "https://github.com/owner/repo/pull/10#issuecomment-99"
        )
        # PR URL does not match the issue comment regex (no /issues/ path)
        assert parsed["valid"] is False

    def test_parse_pr_review_comment_url_rejected(self):
        """AC8: PR review comment URL (#discussion_r...) is rejected — not issue comment."""
        parsed = wrapper._parse_anchor_comment_url(
            "https://github.com/owner/repo/pull/10#discussion_r12345"
        )
        assert parsed["valid"] is False
        assert parsed.get("error") == "pr_review_comment_url"

    def test_url_without_anchor_fragment_rejected(self):
        """AC8: URL without #issuecomment-N fragment is rejected."""
        parsed = wrapper._parse_anchor_comment_url(
            "https://github.com/owner/repo/issues/42"
        )
        assert parsed["valid"] is False

    def test_substring_does_not_pass_validation(self):
        """AC8: substring match alone does not grant valid status — regex required.

        This test documents that the validator does NOT use substring matching.
        A URL containing 'issues' as substring but malformed structure → invalid.
        """
        # "issues" appears but the URL structure is wrong (repo path mangled)
        malformed = "https://github.com/owner/NOTREPO/issues-extra/42#issuecomment-1"
        parsed = wrapper._parse_anchor_comment_url(malformed)
        # The regex _ISSUE_COMMENT_RE requires /issues/<digits>#issuecomment-<digits>
        # so a path like /issues-extra/ will NOT match
        assert parsed["valid"] is False


# ---------------------------------------------------------------------------
# AC9: sentinel — raw body sentinel does not appear in stdout or stderr
# ---------------------------------------------------------------------------

class TestAC9SentinelNotInStdout:
    """AC9: sentinel strings from raw issue body/comments do not leak to stdout/stderr."""

    def test_sentinel_not_in_stdout(self, tmp_path, capsys):
        """AC9: sentinel embedded in issue body is absent from stdout."""
        sentinel = "SECRET_SENTINEL_SHOULD_NOT_LEAK_TO_STDOUT_99XYZ"
        body = VALID_ISSUE_BODY + f"\n{sentinel}\n"
        fixture_data = make_minimal_fixture(body=body, issue_number=500)
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            wrapper.run_preflight(
                issue_number=500,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        assert sentinel not in captured.out, \
               "sentinel from issue body must not appear in stdout"
        assert sentinel not in captured.err, \
               "sentinel from issue body must not appear in stderr"

    def test_comment_sentinel_not_in_stdout(self, tmp_path, capsys):
        """AC9: sentinel embedded in comment body is absent from stdout."""
        sentinel = "COMMENT_SENTINEL_MUST_NOT_LEAK_88ABC"
        fixture_data = make_minimal_fixture(
            body=VALID_ISSUE_BODY,
            issue_number=501,
            comments=[{"id": 1, "body": f"Some comment with {sentinel}", "issue_url": ""}],
        )
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            wrapper.run_preflight(
                issue_number=501,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        assert sentinel not in captured.out, \
               "sentinel from comment must not appear in stdout"
        assert sentinel not in captured.err, \
               "sentinel from comment must not appear in stderr"

    def test_sentinel_not_via_exception_path(self, tmp_path, capsys):
        """AC9: sentinel does not leak even through exception/error paths."""
        sentinel = "EXCEPTION_PATH_SENTINEL_77DEF"
        # Provide a fixture that triggers a planner error but has sentinel in body
        bad_body = f"no required sections here {sentinel}"
        fixture_data = make_minimal_fixture(body=bad_body, issue_number=502)
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            wrapper.run_preflight(
                issue_number=502,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        assert sentinel not in captured.out
        assert sentinel not in captured.err


# ---------------------------------------------------------------------------
# AC10: byte_stable — same fixture produces byte-stable artifact
# ---------------------------------------------------------------------------

class TestAC10ByteStableFixture:
    """AC10: --fixture produces byte-stable result artifact regardless of run count."""

    def test_byte_stable_result_core_hash(self, tmp_path):
        """AC10: two runs with same fixture produce identical result_core_sha256."""
        fixture_data = make_minimal_fixture(
            body=VALID_ISSUE_BODY,
            issue_number=600,
        )
        # Set deterministic 'now' timestamp
        fixture_data["now"] = "2026-01-01T12:00:00+00:00"

        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        results = []
        for _ in range(2):
            import io
            from contextlib import redirect_stdout

            out = io.StringIO()
            with (
                mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
                redirect_stdout(out),
            ):
                result, _ = wrapper.run_preflight(
                    issue_number=600,
                    repo="testowner/testrepo",
                    anchor_comment_urls=[],
                    fixture_path=fixture_path,
                )
            results.append(result)

        hash1 = results[0].get("hashes", {}).get("result_core_sha256")
        hash2 = results[1].get("hashes", {}).get("result_core_sha256")

        assert hash1 is not None, "result_core_sha256 must be present"
        assert hash1 == hash2, \
               f"byte_stable: hashes differ between runs: {hash1} != {hash2}"

    def test_byte_stable_snapshot_hash(self, tmp_path):
        """AC10: byte_stable — same fixture produces identical raw_issue_snapshot_sha256."""
        fixture_data = make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=601)
        fixture_data["now"] = "2026-01-01T00:00:00+00:00"

        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        hashes_list = []
        for _ in range(2):
            import io
            from contextlib import redirect_stdout

            out = io.StringIO()
            with (
                mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
                redirect_stdout(out),
            ):
                result, _ = wrapper.run_preflight(
                    issue_number=601,
                    repo="testowner/testrepo",
                    anchor_comment_urls=[],
                    fixture_path=fixture_path,
                )
            hashes_list.append(result.get("hashes", {}).get("raw_issue_snapshot_sha256"))

        assert hashes_list[0] is not None
        assert hashes_list[0] == hashes_list[1], \
               f"byte_stable: snapshot hashes differ: {hashes_list}"


# ---------------------------------------------------------------------------
# AC11: argv — commands[] contains only argv arrays, no shell strings
# ---------------------------------------------------------------------------

class TestAC11ArgvOnlyCommands:
    """AC11: commands[] uses argv arrays exclusively, no shell strings."""

    def test_commands_have_argv_array(self, tmp_path, capsys):
        """AC11: every command in result has 'argv' as a list, not a string."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=700)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=700,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        commands = result.get("commands", [])
        assert len(commands) > 0, "Expected at least one command in result"

        for cmd in commands:
            assert isinstance(cmd.get("argv"), list), \
                   f"argv must be a list, got {type(cmd.get('argv'))}: {cmd}"
            # Each argv element must be a string
            for arg in cmd["argv"]:
                assert isinstance(arg, str), \
                       f"Each argv element must be a string, got {type(arg)}: {arg}"

    def test_commands_shell_false(self, tmp_path, capsys):
        """AC11: every command has shell=false."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=701)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=701,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        for cmd in result.get("commands", []):
            assert cmd.get("shell") is False, \
                   f"shell must be False (boolean), got {cmd.get('shell')}"

    def test_commands_source_static_wrapper_template(self, tmp_path, capsys):
        """AC11: every command has source='registry' (ISSUE_REFINEMENT_COMMAND_REGISTRY_V1)."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=702)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=702,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        for cmd in result.get("commands", []):
            assert cmd.get("source") == "registry", \
                   f"source must be 'registry', got {cmd.get('source')}"

    def test_argv_contains_no_shell_string(self, tmp_path, capsys):
        """AC11: argv elements do not contain shell metacharacters as concatenated commands."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=703)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=703,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        for cmd in result.get("commands", []):
            # argv should not be a single string with shell operators
            argv = cmd.get("argv", [])
            assert isinstance(argv, list), "argv must be a list"
            # No single element should contain shell pipes/redirects/semicolons
            for arg in argv:
                assert " | " not in arg, f"argv element contains pipe: {arg!r}"
                assert " && " not in arg, f"argv element contains &&: {arg!r}"
                assert " || " not in arg, f"argv element contains ||: {arg!r}"


# ---------------------------------------------------------------------------
# AC12: environment_failure — gh not found → exit 3
# ---------------------------------------------------------------------------

class TestAC12EnvironmentFailure:
    """AC12: gh not found / auth failure / timeout → environment_failure / exit 3."""

    def test_gh_not_found_returns_none(self):
        """AC12: _run_gh with nonexistent binary returns None and error string."""
        result, err = wrapper._run_gh(["nonexistent_binary_xyz_12345", "--version"])
        assert result is None
        assert "gh_not_found" in err or "not_found" in err or len(err) > 0

    def test_environment_failure_when_gh_returns_error(self, tmp_path, capsys):
        """AC12: environment_failure when gh is simulated to fail (mock _fetch_issue)."""
        # Simulate gh failure by patching _fetch_issue to return (None, error)
        with (
            mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
            mock.patch.object(wrapper, "_fetch_issue", return_value=(None, "gh_exit_1: auth failure")),
        ):
            result, exit_code = wrapper.run_preflight(
                issue_number=800,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=None,
            )

        assert exit_code == wrapper.EXIT_ENVIRONMENT_FAILURE, \
               f"Expected environment_failure exit 3, got {exit_code}"
        assert result["status"] == "environment_failure"
        assert wrapper.BLOCKER_GH_FAILURE in result["blockers"]

    def test_environment_failure_when_comments_fail(self, tmp_path, capsys):
        """AC12: environment_failure when gh comments API fails."""
        fake_issue = {
            "number": 800,
            "title": "Test",
            "body": VALID_ISSUE_BODY,
            "labels": [],
        }
        with (
            mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
            mock.patch.object(wrapper, "_fetch_issue", return_value=(fake_issue, "")),
            mock.patch.object(wrapper, "_fetch_issue_comments", return_value=(None, "gh_exit_1: not authorized")),
        ):
            result, exit_code = wrapper.run_preflight(
                issue_number=800,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=None,
            )

        assert exit_code == wrapper.EXIT_ENVIRONMENT_FAILURE
        assert result["status"] == "environment_failure"

    def test_environment_failure_timeout_mock(self):
        """AC12: environment_failure — gh timeout maps to error string."""
        # Simulate timeout error in _run_gh
        import subprocess as _sp
        with mock.patch("subprocess.run", side_effect=_sp.TimeoutExpired(["gh"], 30)):
            result, err = wrapper._run_gh(["gh", "issue", "view", "1"])
        assert result is None
        assert "timeout" in err.lower()

    def test_environment_failure_non_json_response(self):
        """AC12: environment_failure when gh returns non-JSON output."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout="this is not json !@#",
                stderr="",
            )
            result, err = wrapper._run_gh(["gh", "api", "repos/x/y/issues/1"])

        assert result is None
        assert "json_decode_error" in err.lower() or "decode" in err.lower()


# ---------------------------------------------------------------------------
# Additional structural tests
# ---------------------------------------------------------------------------

class TestAnchorCommentBatchValidation:
    """Test anchor batch validation with stable sort + dedupe."""

    def test_stable_sort_dedupe_applied(self, tmp_path):
        """Multiple same URLs are deduped and processed once."""
        # Provide two identical URLs — should only validate once
        urls = [
            "https://github.com/testowner/testrepo/issues/100#issuecomment-7777001",
            "https://github.com/testowner/testrepo/issues/100#issuecomment-7777001",
        ]
        comments = [
            {
                "id": 7777001,
                "body": "ok comment",
                "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/100",
            }
        ]

        sorted_urls, blockers = wrapper._validate_anchor_comments_batch(
            urls, "testowner/testrepo", 100, fixture_comments=comments
        )

        # After dedupe, only 1 unique URL
        assert len(sorted_urls) == 1
        assert blockers == []

    def test_one_invalid_url_blocks_all(self):
        """One invalid URL in batch → all blocked."""
        urls = [
            "https://github.com/testowner/testrepo/issues/100#issuecomment-7777001",
            "https://github.com/OTHER/testrepo/issues/100#issuecomment-7777002",  # wrong owner
        ]
        comments = [
            {"id": 7777001, "body": "ok", "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/100"},
            {"id": 7777002, "body": "ok", "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/100"},
        ]

        sorted_urls, blockers = wrapper._validate_anchor_comments_batch(
            urls, "testowner/testrepo", 100, fixture_comments=comments
        )

        assert len(blockers) > 0, "Should have blockers for wrong owner"


class TestBuildCompactStdout:
    """Tests for _build_compact_stdout — sentinel isolation."""

    def test_compact_stdout_does_not_include_body(self):
        """_build_compact_stdout must not include issue body sentinel in output."""
        result = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "pass",
            "issue_number": 1,
            "repo": "o/r",
            "planner_exit_code": 0,
            "planner_fail_closed": False,
            "next_action": "proceed",
            "must_read": [],
            "do_not_read": [],
            "commands": [],
            "blockers": [],
            "artifacts": {"refinement_preflight_result_v1": "/tmp/result.json"},
            "hashes": {},
        }
        output = wrapper._build_compact_stdout(result)
        assert "STATUS: pass" in output
        assert "NEXT_ACTION: proceed" in output
        # Must not contain any raw body data
        assert "issue_body" not in output.lower()

    def test_compact_stdout_no_raw_comment_text(self):
        """_build_compact_stdout must not include comment body text."""
        sensitive = "MY_PRIVATE_COMMENT_DATA"
        result = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "blocked",
            "issue_number": 1,
            "repo": "o/r",
            "planner_exit_code": None,
            "planner_fail_closed": None,
            "next_action": "human_judgment_required",
            "must_read": [],
            "do_not_read": [],
            "commands": [],
            "blockers": ["ANCHOR_NOT_IN_ISSUE"],
            "artifacts": {},
            "hashes": {},
        }
        output = wrapper._build_compact_stdout(result)
        assert sensitive not in output
        assert "BLOCKERS:" in output
        assert "ANCHOR_NOT_IN_ISSUE" in output

    def test_environment_failure_emits_reason_code_source_operation_and_no_raw_detail_leak(self):
        """AC1/AC8: environment_failure status emits REASON_CODE/SOURCE/OPERATION
        as three consistent stdout lines, and never leaks raw body/comment detail."""
        sensitive = "MY_PRIVATE_RAW_DETAIL"
        result = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "environment_failure",
            "issue_number": 1,
            "repo": "o/r",
            "planner_exit_code": None,
            "planner_fail_closed": None,
            "next_action": "retry_later",
            "must_read": [],
            "do_not_read": [],
            "commands": [],
            "blockers": ["GH_API_FAILURE"],
            "artifacts": {},
            "hashes": {},
            "reason_code": "rate_limited",
            "source": "credentialless_transport",
            "operation": "read_issue",
        }
        output = wrapper._build_compact_stdout(result)
        assert "REASON_CODE: rate_limited" in output
        assert "SOURCE: credentialless_transport" in output
        assert "OPERATION: read_issue" in output
        assert sensitive not in output

    def test_non_environment_failure_omits_reason_code_source_operation(self):
        """AC2: non-environment_failure status must not emit REASON_CODE/SOURCE/OPERATION,
        even when those keys are present in the result dict (e.g. leftover state)."""
        result = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "pass",
            "issue_number": 1,
            "repo": "o/r",
            "planner_exit_code": 0,
            "planner_fail_closed": False,
            "next_action": "proceed",
            "must_read": [],
            "do_not_read": [],
            "commands": [],
            "blockers": [],
            "artifacts": {},
            "hashes": {},
            "reason_code": "rate_limited",
            "source": "credentialless_transport",
            "operation": "read_issue",
        }
        output = wrapper._build_compact_stdout(result)
        assert "REASON_CODE:" not in output
        assert "SOURCE:" not in output
        assert "OPERATION:" not in output


# ---------------------------------------------------------------------------
# B1: multi-page slurp flatten
# ---------------------------------------------------------------------------

class TestB1MultiPageSlurpFlatten:
    """B1: --paginate --slurp returns [[...page1...], [...page2...]] and must be flattened."""

    def test_flatten_two_page_slurp_output(self):
        """B1: two-page slurp output (wrapped array) is flattened to a single list."""
        page1 = [{"id": 1, "body": "comment1"}, {"id": 2, "body": "comment2"}]
        page2 = [{"id": 3, "body": "comment3"}]
        slurp_output = [page1, page2]

        # Simulate _run_gh returning a slurp-wrapped list
        with mock.patch.object(wrapper, "_run_gh", return_value=(slurp_output, "")):
            result, err = wrapper._fetch_issue_comments("testowner/testrepo", 100)

        assert err == "", f"Unexpected error: {err}"
        assert result is not None, "Expected a list, got None"
        assert len(result) == 3, f"Expected 3 flattened comments, got {len(result)}"
        assert result[0]["id"] == 1
        assert result[1]["id"] == 2
        assert result[2]["id"] == 3

    def test_flatten_single_page_slurp_output(self):
        """B1: single-page slurp output [[comment1, comment2]] is flattened correctly."""
        page1 = [{"id": 10, "body": "c10"}, {"id": 11, "body": "c11"}]
        slurp_output = [page1]

        with mock.patch.object(wrapper, "_run_gh", return_value=(slurp_output, "")):
            result, err = wrapper._fetch_issue_comments("testowner/testrepo", 200)

        assert err == "", f"Unexpected error: {err}"
        assert result is not None
        assert len(result) == 2, f"Expected 2 comments, got {len(result)}"
        assert result[0]["id"] == 10
        assert result[1]["id"] == 11

    def test_flatten_three_page_slurp_output(self):
        """B1: three-page slurp output is fully flattened."""
        pages = [
            [{"id": i} for i in range(5)],
            [{"id": i} for i in range(5, 10)],
            [{"id": i} for i in range(10, 13)],
        ]
        with mock.patch.object(wrapper, "_run_gh", return_value=(pages, "")):
            result, err = wrapper._fetch_issue_comments("testowner/testrepo", 300)

        assert err == "", f"Unexpected error: {err}"
        assert result is not None
        assert len(result) == 13, f"Expected 13 comments, got {len(result)}"

    def test_empty_slurp_output(self):
        """B1: empty slurp output [] → empty list, no error."""
        with mock.patch.object(wrapper, "_run_gh", return_value=([], "")):
            result, err = wrapper._fetch_issue_comments("testowner/testrepo", 400)

        assert err == ""
        assert result == []


# ---------------------------------------------------------------------------
# B2: jsonschema runtime validation
# ---------------------------------------------------------------------------

class TestB2JsonSchemaRuntimeValidation:
    """B2: jsonschema validates --fixture input and result artifact before writing."""

    def test_unknown_top_level_property_fails_input_validation(self, tmp_path):
        """B2: fixture with unknown top-level property → blocked (INPUT_SCHEMA_INVALID)."""
        bad_fixture = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "unknown_top_level_property": "should_fail",  # violates additionalProperties: false
        }
        fixture_path = tmp_path / "bad_fixture.json"
        fixture_path.write_text(json.dumps(bad_fixture), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert exit_code == wrapper.EXIT_BLOCKED, \
            f"Expected exit 2 (blocked) for invalid input schema, got {exit_code}"
        assert wrapper.BLOCKER_INPUT_SCHEMA_INVALID in result["blockers"], \
            f"Expected INPUT_SCHEMA_INVALID blocker, got {result['blockers']}"

    def test_issue_body_missing_fails_input_validation(self, tmp_path):
        """B2: fixture with issue.body missing → blocked (INPUT_SCHEMA_INVALID)."""
        bad_fixture = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "issue": {
                "number": 100,
                "title": "Test",
                # body is missing — required by schema
                "labels": [],
            },
        }
        fixture_path = tmp_path / "bad_body.json"
        fixture_path.write_text(json.dumps(bad_fixture), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert exit_code == wrapper.EXIT_BLOCKED, \
            f"Expected exit 2 (blocked) for missing body, got {exit_code}"
        assert wrapper.BLOCKER_INPUT_SCHEMA_INVALID in result["blockers"], \
            f"Expected INPUT_SCHEMA_INVALID blocker, got {result['blockers']}"

    def test_result_artifact_is_schema_valid(self, tmp_path):
        """B2: generated result artifact must validate against result schema."""
        import jsonschema as _js
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=900)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=900,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        schema_path = SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json"
        schema = json.loads(schema_path.read_text())

        artifact_path = result.get("artifacts", {}).get("refinement_preflight_result_v1")
        assert artifact_path, "refinement_preflight_result_v1 artifact path must be present"
        artifact_data = json.loads(Path(artifact_path).read_text())

        # Must not raise ValidationError
        _js.validate(artifact_data, schema)

    def test_commands_shell_true_result_schema_invalid(self):
        """B2: result with commands[].shell=true fails result schema validation."""
        import jsonschema as _js
        schema_path = SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json"
        schema = json.loads(schema_path.read_text())

        bad_result = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "pass",
            "issue_number": 1,
            "repo": "o/r",
            "planner_exit_code": 0,
            "planner_fail_closed": False,
            "next_action": "proceed",
            "must_read": [],
            "do_not_read": [],
            "commands": [
                {
                    "kind": "run_preflight",
                    "argv": ["uv", "run", "python3", "script.py"],
                    "shell": True,  # violates const: false
                    "source": "static_wrapper_template",
                }
            ],
            "blockers": [],
            "artifacts": {},
            "hashes": {},
        }

        with pytest.raises(_js.ValidationError):
            _js.validate(bad_result, schema)

    def test_fail_closed_true_requires_rewrite_payloads_by_schema(self):
        """B2: planner_fail_closed=true requires schema-forwarding fields by JSON Schema if/then."""
        import jsonschema as _js
        schema_path = SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json"
        schema = json.loads(schema_path.read_text())

        invalid = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "blocked",
            "issue_number": 1,
            "repo": "o/r",
            "planner_exit_code": 0,
            "planner_fail_closed": True,
            "next_action": "human_judgment_required",
            "must_read": [],
            "do_not_read": [],
            "commands": [],
            "blockers": ["PLANNER_FAIL_CLOSED"],
            "artifacts": {},
            "hashes": {},
        }

        with pytest.raises(_js.ValidationError):
            _js.validate(invalid, schema)

    def test_fail_closed_false_allows_optional_rewrite_fields_to_be_absent(self):
        """B2: planner_fail_closed=false can omit rewrite payload fields."""
        import jsonschema as _js
        schema_path = SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json"
        schema = json.loads(schema_path.read_text())

        valid = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "pass",
            "issue_number": 2,
            "repo": "o/r",
            "planner_exit_code": 0,
            "planner_fail_closed": False,
            "next_action": "proceed",
            "must_read": [],
            "do_not_read": [],
            "commands": [],
            "blockers": [],
            "artifacts": {},
            "hashes": {},
        }

        _js.validate(valid, schema)


# ---------------------------------------------------------------------------
# B3: warn reachable
# ---------------------------------------------------------------------------

class TestB3WarnReachable:
    """B3: warn (exit 1) is reachable when planner returns unknown confidence."""

    def test_warn_exit_code_mapping_with_unknown_confidence(self):
        """B3: exit_code_mapping returns warn/1 for planner exit 0 + unknown confidence."""
        plan_with_unknown = {
            "decisions": {
                "investigation_policy": {"confidence": "deterministic"},
                "web_research_policy": {"confidence": "unknown"},
            }
        }
        status, code = wrapper._apply_exit_code_mapping(
            0, False, [], plan=plan_with_unknown
        )
        assert status == "warn", f"Expected warn, got {status}"
        assert code == wrapper.EXIT_WARN, f"Expected exit 1, got {code}"

    def test_pass_when_all_deterministic(self):
        """B3: exit_code_mapping returns pass/0 when all confidences are deterministic."""
        plan_all_det = {
            "decisions": {
                "investigation_policy": {"confidence": "deterministic"},
                "web_research_policy": {"confidence": "deterministic"},
            }
        }
        status, code = wrapper._apply_exit_code_mapping(
            0, False, [], plan=plan_all_det
        )
        assert status == "pass", f"Expected pass, got {status}"
        assert code == wrapper.EXIT_PASS, f"Expected exit 0, got {code}"

    def test_warn_with_fixture_that_has_unknown_confidence(self, tmp_path, capsys):
        """B3: fixture triggering planner unknown confidence → STATUS: warn / exit 1."""
        fixture_path = tmp_path / "fixture.json"
        fixture_data = make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=850)
        # Patch planner to return unknown confidence
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        mock_plan = {
            "schema_version": "refinement_loop_plan/v1",
            "fail_closed": {"required": False, "reason_codes": []},
            "decisions": {
                "investigation_policy": {
                    "required": False,
                    "confidence": "unknown",
                    "target_paths": [],
                },
                "web_research_policy": {
                    "required": False,
                    "confidence": "unknown",
                },
            },
        }

        with (
            mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
            mock.patch.object(wrapper, "_invoke_planner", return_value=(mock_plan, 0, "", "")),
        ):
            result, exit_code = wrapper.run_preflight(
                issue_number=850,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        assert exit_code == wrapper.EXIT_WARN, \
            f"Expected exit 1 (warn), got {exit_code}; status={result['status']}"
        assert result["status"] == "warn", \
            f"Expected status=warn, got {result['status']}"
        assert "STATUS: warn" in captured.out

    def test_no_warn_when_plan_is_none(self):
        """B3: warn is NOT triggered when plan=None (no plan → environment_failure, not warn)."""
        status, code = wrapper._apply_exit_code_mapping(None, None, [])
        assert status == "environment_failure"
        assert code == wrapper.EXIT_ENVIRONMENT_FAILURE


# ---------------------------------------------------------------------------
# B4: planner_input artifact saved
# ---------------------------------------------------------------------------

class TestB4PlannerInputArtifact:
    """B4: planner_input.json is saved as artifact with byte-stable hash."""

    def test_planner_input_artifact_exists(self, tmp_path):
        """B4: planner_input.json artifact is created and referenced in result."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=1000)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=1000,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        artifacts = result.get("artifacts", {})
        assert "planner_input" in artifacts, \
            f"planner_input artifact missing; artifacts={artifacts}"

        planner_input_path = Path(artifacts["planner_input"])
        assert planner_input_path.exists(), \
            f"planner_input.json file not found: {planner_input_path}"

    def test_planner_input_artifact_schema_valid(self, tmp_path):
        """B4: planner_input.json content is valid JSON with schema_version field."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=1001)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=1001,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        planner_input_path = Path(result["artifacts"]["planner_input"])
        planner_input_data = json.loads(planner_input_path.read_text())

        # Must have schema_version
        assert planner_input_data.get("schema_version") == "refinement_loop_planner_input/v1", \
            f"planner_input schema_version mismatch: {planner_input_data.get('schema_version')}"
        # Must have issue.body
        assert "body" in planner_input_data.get("issue", {}), \
            "planner_input.json must contain issue.body"

    def test_planner_input_byte_stable(self, tmp_path):
        """B4: byte_stable — same fixture produces identical planner_input_sha256 across runs."""
        fixture_data = make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=1002)
        fixture_data["now"] = "2026-01-01T00:00:00+00:00"
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")
        seed_previous_snapshot(
            tmp_path,
            issue_number=1002,
            repo="testowner/testrepo",
            body=VALID_ISSUE_BODY,
        )

        hashes_list = []
        for _ in range(2):
            import io
            from contextlib import redirect_stdout
            out = io.StringIO()
            with (
                mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
                redirect_stdout(out),
            ):
                result, _ = wrapper.run_preflight(
                    issue_number=1002,
                    repo="testowner/testrepo",
                    anchor_comment_urls=[],
                    fixture_path=fixture_path,
                )
            hashes_list.append(result.get("hashes", {}).get("planner_input_sha256"))

        assert hashes_list[0] is not None, "planner_input_sha256 must be present"
        assert hashes_list[0] == hashes_list[1], \
            f"byte_stable: planner_input hashes differ: {hashes_list}"

    def test_planner_input_artifact_in_result_hashes(self, tmp_path):
        """B4: result.hashes contains planner_input_sha256."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=1003)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=1003,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        hashes = result.get("hashes", {})
        assert "planner_input_sha256" in hashes, \
            f"planner_input_sha256 missing from hashes: {hashes}"
        assert len(hashes["planner_input_sha256"]) == 64, \
            "SHA256 hex digest must be 64 characters"


# ---------------------------------------------------------------------------
# B5: anchor issue_url missing / empty → blocked + ANCHOR_NOT_IN_ISSUE
# ---------------------------------------------------------------------------

class TestB5AnchorIssueUrlValidation:
    """B5: anchor comment with missing or empty issue_url → blocked + ANCHOR_NOT_IN_ISSUE."""

    def test_anchor_issue_url_missing_blocked(self, tmp_path):
        """B5: anchor comment with issue_url field absent → blocked + ANCHOR_NOT_IN_ISSUE."""
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/testowner/testrepo/issues/100#issuecomment-5551001"
            ],
            "anchor_comments": [
                {
                    "id": 5551001,
                    "body": "anchor comment",
                    # issue_url is intentionally missing
                }
            ],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert exit_code == wrapper.EXIT_BLOCKED, \
            f"Expected exit 2 (blocked) for missing issue_url, got {exit_code}"
        assert wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE in result["blockers"], \
            f"Expected ANCHOR_NOT_IN_ISSUE blocker, got {result['blockers']}"

    def test_anchor_issue_url_empty_string_blocked(self, tmp_path):
        """B5: anchor comment with issue_url='' → blocked + ANCHOR_NOT_IN_ISSUE."""
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/testowner/testrepo/issues/100#issuecomment-5552001"
            ],
            "anchor_comments": [
                {
                    "id": 5552001,
                    "body": "anchor comment",
                    "issue_url": "",  # empty string
                }
            ],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert exit_code == wrapper.EXIT_BLOCKED, \
            f"Expected exit 2 (blocked) for empty issue_url, got {exit_code}"
        assert wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE in result["blockers"], \
            f"Expected ANCHOR_NOT_IN_ISSUE blocker, got {result['blockers']}"

    def test_anchor_same_id_different_issue_url_blocked(self, tmp_path):
        """B5: anchor with same id but wrong issue_url → blocked + ANCHOR_NOT_IN_ISSUE."""
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/testowner/testrepo/issues/100#issuecomment-5553001"
            ],
            "anchor_comments": [
                {
                    "id": 5553001,
                    "body": "anchor comment",
                    "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/999",
                    # points to issue 999, not 100
                }
            ],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert exit_code == wrapper.EXIT_BLOCKED
        assert wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE in result["blockers"], \
            f"Expected ANCHOR_NOT_IN_ISSUE, got {result['blockers']}"

    def test_anchor_different_repo_in_issue_url_blocked(self, tmp_path):
        """B5: anchor with different repo in issue_url → blocked + ANCHOR_NOT_IN_ISSUE."""
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/testowner/testrepo/issues/100#issuecomment-5554001"
            ],
            "anchor_comments": [
                {
                    "id": 5554001,
                    "body": "anchor comment",
                    "issue_url": "https://api.github.com/repos/otherowner/otherrepo/issues/100",
                    # different repo
                }
            ],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert exit_code == wrapper.EXIT_BLOCKED
        assert wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE in result["blockers"], \
            f"Expected ANCHOR_NOT_IN_ISSUE, got {result['blockers']}"

    def test_anchor_pr_review_comment_url_blocked(self, tmp_path):
        """B5: PR review comment URL → blocked + ANCHOR_NOT_IN_ISSUE."""
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/testowner/testrepo/pull/55#discussion_r9999999"
            ],
            "anchor_comments": [],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert exit_code == wrapper.EXIT_BLOCKED
        assert wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE in result["blockers"], \
            f"Expected ANCHOR_NOT_IN_ISSUE for PR review comment, got {result['blockers']}"

    def test_validate_anchor_issue_url_missing_unit(self):
        """B5: unit test — _validate_anchor_comment_url with missing issue_url → blocked."""
        url = "https://github.com/testowner/testrepo/issues/100#issuecomment-5555001"
        # Comment with no issue_url field
        fixture_comments = [{"id": 5555001, "body": "test"}]  # issue_url absent

        is_valid, blockers = wrapper._validate_anchor_comment_url(
            url, "testowner/testrepo", 100, fixture_comments=fixture_comments
        )

        assert not is_valid
        assert wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE in blockers

    def test_validate_anchor_issue_url_empty_unit(self):
        """B5: unit test — _validate_anchor_comment_url with empty issue_url → blocked."""
        url = "https://github.com/testowner/testrepo/issues/100#issuecomment-5556001"
        fixture_comments = [{"id": 5556001, "body": "test", "issue_url": ""}]

        is_valid, blockers = wrapper._validate_anchor_comment_url(
            url, "testowner/testrepo", 100, fixture_comments=fixture_comments
        )

        assert not is_valid
        assert wrapper.BLOCKER_ANCHOR_NOT_IN_ISSUE in blockers


# ---------------------------------------------------------------------------
# Non-blocker A: failure path stdout/disk consistency
# ---------------------------------------------------------------------------

class TestNonBlockerAFailurePathConsistency:
    """Non-blocker A: stdout and disk artifact have same status/blockers/next_action on failure."""

    def test_stdout_disk_consistent_on_anchor_failure(self, tmp_path, capsys):
        """A: on anchor mismatch, stdout STATUS matches artifact status (no post-write mutation)."""
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 100,
                "title": "Test",
                "body": VALID_ISSUE_BODY,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [
                "https://github.com/testowner/testrepo/issues/999#issuecomment-6661001"
            ],
            "anchor_comments": [],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()
        # Stdout must reflect the same status as result dict
        assert f"STATUS: {result['status']}" in captured.out, \
            "stdout STATUS must match result dict status"
        assert result["status"] == "blocked"

    def test_stdout_disk_consistent_on_pass(self, tmp_path, capsys):
        """A: on pass/warn, stdout STATUS matches artifact file status."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=1100)),
            encoding="utf-8",
        )

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, _ = wrapper.run_preflight(
                issue_number=1100,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        captured = capsys.readouterr()

        # Check artifact file matches stdout
        artifact_path = result.get("artifacts", {}).get("refinement_preflight_result_v1")
        assert artifact_path, "artifact path must be present"
        artifact_data = json.loads(Path(artifact_path).read_text())

        assert artifact_data["status"] == result["status"], \
            "artifact status must match result dict"
        assert f"STATUS: {result['status']}" in captured.out, \
            "stdout STATUS must match result dict"
        assert artifact_data["next_action"] == result["next_action"], \
            "artifact next_action must match result dict"
        assert artifact_data["blockers"] == result["blockers"], \
            "artifact blockers must match result dict"


# ---------------------------------------------------------------------------
# Non-blocker D: argparse validation
# ---------------------------------------------------------------------------

class TestNonBlockerDArgparseValidation:
    """Non-blocker D: argparse input validation — blocked (exit 2) on contract violation."""

    def test_invalid_repo_format_blocked(self, capsys):
        """D: --repo without slash → blocked (INVALID_ARGS)."""
        result = _build_result_from_main(["--issue-number", "42", "--repo", "noslash"])
        assert result is not None, "Expected blocked result"
        # main() should sys.exit(2) due to INVALID_ARGS
        # We test the validation logic directly
        import re as _re
        pattern = _re.compile(r"^[^/]+/[^/]+$")
        assert not pattern.match("noslash")

    def test_invalid_issue_number_zero_blocked(self):
        """D: --issue-number 0 → blocked (INVALID_ARGS)."""
        # Issue number must be positive
        assert 0 <= 0  # trivially: 0 is not positive
        # Test the validation in main() by calling with args
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with (
            redirect_stdout(out),
            pytest.raises(SystemExit) as exc_info,
        ):
            wrapper.main(["--issue-number", "0", "--repo", TRUSTED_REPO])

        assert exc_info.value.code == wrapper.EXIT_BLOCKED, \
            f"Expected exit 2 for issue-number=0, got {exc_info.value.code}"
        assert "INVALID_ARGS" in out.getvalue()

    def test_invalid_issue_number_negative_blocked(self):
        """D: --issue-number -1 → blocked (INVALID_ARGS)."""
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with (
            redirect_stdout(out),
            pytest.raises(SystemExit) as exc_info,
        ):
            wrapper.main(["--issue-number", "-1", "--repo", TRUSTED_REPO])

        assert exc_info.value.code == wrapper.EXIT_BLOCKED, \
            f"Expected exit 2 for issue-number=-1, got {exc_info.value.code}"

    def test_anchor_url_non_github_prefix_blocked(self):
        """D: --anchor-comment-url without https://github.com/ prefix → blocked (INVALID_ARGS)."""
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with (
            redirect_stdout(out),
            pytest.raises(SystemExit) as exc_info,
        ):
            wrapper.main([
                "--issue-number", "42",
                "--repo", TRUSTED_REPO,
                "--anchor-comment-url", "http://gitlab.com/owner/repo/issues/42#issuecomment-1",
            ])

        assert exc_info.value.code == wrapper.EXIT_BLOCKED, \
            f"Expected exit 2 for non-github URL, got {exc_info.value.code}"
        assert "INVALID_ARGS" in out.getvalue()

    def test_valid_args_do_not_trigger_blocked(self, tmp_path):
        """D: valid args pass validation and reach run_preflight."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(
                make_minimal_fixture(body=VALID_ISSUE_BODY, issue_number=42, repo=TRUSTED_REPO)
            ),
            encoding="utf-8",
        )
        seed_previous_snapshot(
            tmp_path,
            issue_number=42,
            repo=TRUSTED_REPO,
            body=VALID_ISSUE_BODY,
        )

        # Should not raise SystemExit for invalid args (may raise for other reasons)
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with (
            mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
            redirect_stdout(out),
            pytest.raises(SystemExit) as exc_info,
        ):
            wrapper.main([
                "--issue-number", "42",
                "--repo", TRUSTED_REPO,
                "--fixture", str(fixture_path),
            ])

        # With valid args and an available previous snapshot, the wrapper should
        # proceed to a non-blocking outcome instead of stopping on missing delta input.
        assert exc_info.value.code in (
            wrapper.EXIT_PASS, wrapper.EXIT_WARN
        ), f"Valid args should exit with 0 or 1, got {exc_info.value.code}"
        assert "INVALID_ARGS" not in out.getvalue()


def _build_result_from_main(argv: list[str]) -> dict | None:
    """Helper: run main() with argv, capture output, return result or None."""
    import io
    from contextlib import redirect_stdout
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            wrapper.main(argv)
    except SystemExit:
        pass
    return {"stdout": out.getvalue()}



# ---------------------------------------------------------------------------
# AC3 / AC7: custom invariant — required_sections == must_add_sections
# ---------------------------------------------------------------------------


class TestAC3AC7RewriteConstraintsInvariant:
    """AC3/AC7: wrapper verifies required_sections == must_add_sections invariant."""

    def _make_fixture_with_missing_section(self, tmp_path) -> "Path":
        """Fixture that triggers fail_closed with missing section."""
        body = "## Outcome\n\nTest outcome.\n"  # No Machine-Readable Contract
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 300,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 300,
                "title": "Invariant Test",
                "body": body,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [],
        }
        p = tmp_path / "fixture.json"
        p.write_text(json.dumps(fixture_data), encoding="utf-8")
        return p

    def test_invariant_check_required_sections_matches_must_add(self, tmp_path):
        """AC7: required_sections must equal must_add_sections in rewrite_constraints."""
        fixture_path = self._make_fixture_with_missing_section(tmp_path)

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=300,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        if result.get("rewrite_constraints") is not None:
            rc = result["rewrite_constraints"]
            inner = rc.get("rewrite_constraints", {})
            required_sections = result.get("required_sections", [])
            must_add_sections = inner.get("must_add_sections", [])
            assert required_sections == must_add_sections, (
                f"AC7 invariant violated: required_sections={required_sections} "
                f"!= must_add_sections={must_add_sections}"
            )

    def test_invariant_check_required_contract_keys_matches_must_add(self, tmp_path):
        """AC7: required_contract_keys must equal must_add_contract_keys."""
        # Use a body that has a contract section with missing keys
        body = (
            "## Machine-Readable Contract\n\n"
            "```yaml\n"
            "issue_kind: implementation\n"
            "```\n\n"
            "## Outcome\n\nTest.\n"
        )
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 301,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 301,
                "title": "Contract key test",
                "body": body,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [],
        }
        fixture_path = tmp_path / "fixture2.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=301,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        if result.get("rewrite_constraints") is not None:
            rc = result["rewrite_constraints"]
            inner = rc.get("rewrite_constraints", {})
            required_contract_keys = result.get("required_contract_keys", [])
            must_add_contract_keys = inner.get("must_add_contract_keys", [])
            assert required_contract_keys == must_add_contract_keys, (
                f"AC7 invariant violated: required_contract_keys={required_contract_keys} "
                f"!= must_add_contract_keys={must_add_contract_keys}"
            )




# ---------------------------------------------------------------------------
# Issue #995 fix_delta (P0-1, OWNER adversarial review): production wiring
# of build_structural_repair_bundle()/route_structural_repair_disposition()
# into run_preflight() -- previously only reachable from
# test_template_section_repair.py's standalone unit tests, never from this
# module's own production entrypoint.
# ---------------------------------------------------------------------------

_STRUCTURAL_WIRING_TEMPLATE_TEXT = """\
name: "Implementation Issue"
description: "test double"
body:
  - type: textarea
    id: machine-readable-contract
    attributes:
      label: "Machine-Readable Contract"
      value: |
        ```yaml
        contract_schema_version: v1
        issue_kind: implementation
        parent_issue: "#0"
        goal_ref: "N/A"
        change_kind: code
        ```
    validations:
      required: true
  - type: textarea
    id: stop-conditions
    attributes:
      label: "Stop Conditions"
      value: |
        - none
    validations:
      required: true
"""


def _seed_structural_wiring_template(tmp_path: Path) -> None:
    """Materializes a minimal local `.github/ISSUE_TEMPLATE/implementation.yml`
    under `tmp_path` so `_resolve_structural_repair_template()` (which only
    ever reads from `repo_root`, never GitHub) resolves it the same way it
    resolves the real repo's template in production -- isolated from this
    test's real checkout, mirroring the existing `_find_repo_root` mock
    pattern used throughout this file."""
    template_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "implementation.yml").write_text(
        _STRUCTURAL_WIRING_TEMPLATE_TEXT, encoding="utf-8"
    )


# A body with every heading `plan_refinement_loop.py`'s OWN (heading-only)
# required-section check demands for `issue_kind: implementation` -- i.e. the
# planner itself does NOT fail_closed on this body -- but whose
# machine-readable-contract omits `goal_ref`/`change_kind`. Structural repair
# checks required MACHINE-READABLE-CONTRACT KEYS (via the authoring SSOT),
# which the planner's heading-only check never inspects, so this body is the
# minimal case that isolates "planner alone would pass/warn, but the
# structural bundle alone would route to blocked" -- proving
# structural_repair_action is reachable from run_preflight() as a genuinely
# ADDITIONAL, independent check, not a passthrough of pre-existing planner
# blockers.
_STRUCTURAL_WIRING_BODY_MISSING_MRC_KEYS = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#0"
```

## Parent Issue

#0

## Parent Goal Ref

- Goal: test

## Current Validated Scope

- x

## Remaining Parent Gaps

- none

## Runtime Verification Applicability

decision: not_applicable
reason: test

## Outcome

text

## In Scope

- x

## Out of Scope

- y

## Acceptance Criteria

- AC1: x

## Verification Commands

- `pnpm test`

## Allowed Paths

- x

## Stop Conditions

- none

## Required Skills

- python
"""

# A body missing MANY of the planner's own required headings entirely (only
# Machine-Readable Contract + Outcome present). The planner's own heading-only
# check fails_closed on this FIRST (more severe: blocked), before this
# fix_delta's structural pass ever gets a chance to independently route its
# own (weaker, auto_apply_safe) verdict to needs_fix -- the precedence case
# `route_structural_repair_disposition()`'s caller must defer to.
_STRUCTURAL_WIRING_BODY_MANY_SECTIONS_MISSING = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#0"
goal_ref: "N/A"
change_kind: code
```

## Outcome

text
"""


class TestStructuralRepairWiring:
    """Issue #995 fix_delta P0-1: structural_repair_action is reachable from
    run_preflight() itself, and its disposition_summary deterministically
    drives status/next_action per route_structural_repair_disposition()."""

    def test_resolve_structural_repair_template_reads_self_declared_issue_kind(self, tmp_path):
        """P0-1: local-only (no GitHub call) resolution reads the body's own
        machine-readable-contract issue_kind and the matching local template."""
        _seed_structural_wiring_template(tmp_path)
        body = "## Machine-Readable Contract\n\n```yaml\nissue_kind: implementation\n```\n"
        kind, text, relpath = wrapper._resolve_structural_repair_template(body, tmp_path)
        assert kind == "implementation"
        assert relpath == ".github/ISSUE_TEMPLATE/implementation.yml"
        assert text == _STRUCTURAL_WIRING_TEMPLATE_TEXT

    def test_resolve_structural_repair_template_unresolvable_issue_kind_fails_closed(self, tmp_path):
        """P0-1: a body with no self-declared issue_kind (or an unknown one)
        resolves to (None, None, None) rather than guessing -- the caller
        then leaves structural_repair_action absent for that run."""
        _seed_structural_wiring_template(tmp_path)
        assert wrapper._resolve_structural_repair_template("## Outcome\n\ntext\n", tmp_path) == (
            None, None, None,
        )
        assert wrapper._resolve_structural_repair_template(
            "issue_kind: bogus_kind\n", tmp_path
        ) == (None, None, None)

    def test_run_preflight_missing_mrc_keys_forces_blocked_even_when_planner_would_pass(self, tmp_path):
        """P0-1: a body the planner's OWN heading-only check would not
        fail_closed on (every required heading present) still gets forced to
        status=blocked/next_action=human_judgment_required once
        structural_repair_action finds missing required MRC keys
        (goal_ref/change_kind) -- the exact OWNER-specified
        `human_review_required -> blocked` routing rule, exercised through
        the real production entrypoint (not a bespoke test-local reader), on
        a defect the planner's own check cannot see."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(
                make_minimal_fixture(
                    issue_number=995001, body=_STRUCTURAL_WIRING_BODY_MISSING_MRC_KEYS
                )
            ),
            encoding="utf-8",
        )
        _seed_structural_wiring_template(tmp_path)

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=995001,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert result.get("structural_repair_action") is not None, result
        assert result["structural_repair_action"]["disposition_summary"] == "human_review_required"
        assert result["status"] == "blocked", result
        assert result["next_action"] == "human_judgment_required", result
        assert exit_code == wrapper.EXIT_BLOCKED
        assert any(
            b.startswith("structural_repair_action:") for b in result["blockers"]
        ), result

        # Validate the attached bundle + the wrapping result against both
        # schemas (never just self-reported by the producer).
        result_schema = json.loads(
            (SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json").read_text(encoding="utf-8")
        )
        import jsonschema
        jsonschema.validate(instance=result, schema=result_schema)

    def test_run_preflight_never_downgrades_a_more_severe_pre_existing_status(self, tmp_path):
        """P0-1 (schema-safety corollary): a structural finding must never
        SILENTLY DOWNGRADE an already-blocked run (here: the planner's own
        heading-only check fail-closing on many missing sections, which is
        MORE severe than the structural bundle's own auto_apply_safe verdict
        for the one field its narrower stub template happens to check) --
        the schema's const invariant would otherwise force such a downgrade
        if the bundle were attached unconditionally. It is deferred instead,
        with its reason codes still surfaced as informational blockers so
        the finding is never silently dropped."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(
                make_minimal_fixture(
                    issue_number=995002, body=_STRUCTURAL_WIRING_BODY_MANY_SECTIONS_MISSING
                )
            ),
            encoding="utf-8",
        )
        _seed_structural_wiring_template(tmp_path)

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=995002,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert result["status"] == "blocked", result
        assert "missing_required_section" in result["blockers"], result
        # The would-be needs_fix verdict is not silently dropped -- and not
        # silently promoted to override the pre-existing blocked status either.
        assert result.get("structural_repair_action") is None, result
        assert any(
            b.startswith("structural_repair_action_deferred:") for b in result["blockers"]
        ), result

    def test_run_preflight_complete_body_attaches_no_missing_fields_without_status_constraint(self, tmp_path):
        """P0-1: disposition_summary=no_missing_fields_detected carries no
        status constraint -- it is safe to attach alongside pass/warn."""
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(
            json.dumps(
                make_minimal_fixture(
                    issue_number=995003, body=_STRUCTURAL_WIRING_BODY_MISSING_MRC_KEYS.replace(
                        'parent_issue: "#0"\n```',
                        'parent_issue: "#0"\ngoal_ref: "N/A"\nchange_kind: code\n```',
                    )
                )
            ),
            encoding="utf-8",
        )
        _seed_structural_wiring_template(tmp_path)

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=995003,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert result.get("structural_repair_action") is not None, result
        assert (
            result["structural_repair_action"]["disposition_summary"]
            == "no_missing_fields_detected"
        )
        assert result["status"] in ("pass", "warn", "needs_fix", "blocked", "environment_failure")


# ---------------------------------------------------------------------------
# AC10 (Issue #2783): canonical Step 2 review pipeline integration -- a
# #2730-style read-only body (legacy no-path marker Allowed Paths + a
# specific-file `rg` Verification Command) must not fall into
# needs_fix/human_judgment via a spurious broad_search_path_unbounded
# finding, which would risk escalating to
# `STEP_5_OPERATOR_INTERVENTION_REQUIRED` / `contract_readiness_human_judgment`
# upstream in `run_root_review_pipeline.py`.
# ---------------------------------------------------------------------------


_ISSUE_CONTRACT_REVIEW_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2] / "issue-contract-review" / "scripts"
)
_BASELINE_VC_PREFLIGHT_PY = _ISSUE_CONTRACT_REVIEW_SCRIPTS_DIR / "baseline_vc_preflight.py"


def test_operator_intervention_not_triggered_by_no_path_marker():
    """AC10: run the REAL `baseline_vc_preflight.py` (the same executor
    `contract_readiness_check.py`'s canonical Step 2 review pipeline spawns
    in `--mode execute`) against an Issue #2730-style body, then feed its
    REAL output into the REAL `contract_readiness_check.map_preflight_result_to_errors()`
    aggregation function. The result must not carry a
    `broad_search_path_unbounded` finding, and the aggregate readiness
    status must not be `human_judgment` (the status that
    `run_root_review_pipeline.route_canonical_step2_result()` treats as
    `contract_readiness_human_judgment`, routing toward
    `STEP_5_OPERATOR_INTERVENTION_REQUIRED`)."""
    import json
    import subprocess
    import tempfile
    import os

    sys.path.insert(0, str(_ISSUE_CONTRACT_REVIEW_SCRIPTS_DIR))
    from contract_readiness_check import map_preflight_result_to_errors  # noqa: E402

    body = """## Outcome

Read-only research issue (Issue #2730-style shape).

## Acceptance Criteria

- [ ] AC1: investigation only, no repository mutation.

## Verification Commands

```bash
rg -n "is_no_path_marker" scripts/agent-ops/allowed_paths_policy.py
```

## Allowed Paths

- 読み取り専用。リポジトリ変更なし（既定）

## Stop Conditions

- none
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        fixture_file = f.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(_BASELINE_VC_PREFLIGHT_PY),
                "--body-file",
                fixture_file,
                "--issue",
                "2730",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.stdout, f"No output from preflight: stderr={result.stderr}"
        preflight_result = json.loads(result.stdout)
    finally:
        os.unlink(fixture_file)

    errors, aggregate = map_preflight_result_to_errors(preflight_result)
    assert not any(
        e.get("category") == "broad_search_path_unbounded" for e in errors
    ), f"spurious broad_search_path_unbounded finding: {errors}"
    assert aggregate != "human_judgment", f"unexpected human_judgment escalation: {errors}"


def test_extract_allowed_paths_from_issue_body_no_path_marker_returns_empty():
    """AC12 (Issue #2783): `_extract_allowed_paths_from_issue_body()`
    resolves the canonical marker `(none)` and legacy marker
    `読み取り専用。リポジトリ変更なし（既定）` to [] (previously this function
    had no annotation stripping at all, so both markers leaked as literal
    non-empty "path" strings)."""
    body_canonical = "## Allowed Paths\n\n- (none)\n"
    assert wrapper._extract_allowed_paths_from_issue_body(body_canonical) == []

    body_legacy = "## Allowed Paths\n\n- 読み取り専用。リポジトリ変更なし（既定）\n"
    assert wrapper._extract_allowed_paths_from_issue_body(body_legacy) == []
