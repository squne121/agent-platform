"""
test_review_compact_registry_entry.py

AC10 (Issue #1507): `review_compact.validate` is registered in
`command_registry.py` as an exact `shell: false` argv entry with
`mutation: false` and `network_effect: local_only`.

AC22 (Issue #1507, P1-2 of the second owner review): the registered argv
matches its own `mutation: false` / `network_effect: local_only`
declarations exactly (`uv run --locked --offline --no-sync python3 ...`),
and the rendered argv is invoked as a real subprocess to confirm the exit
code and stdout JSON schema.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SKILLS_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILLS_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import command_registry as reg  # noqa: E402


class TestReviewCompactValidateRegistryEntry:
    def test_entry_exists_in_registry(self):
        """GIVEN REGISTRY WHEN queried THEN review_compact.validate is present."""
        assert "review_compact.validate" in reg.REGISTRY

    def test_entry_has_required_fields(self):
        """GIVEN the review_compact.validate entry WHEN inspected THEN all
        AC1-required registry fields are present."""
        required_fields = {
            "id", "argv", "cwd_policy", "stdin_contract",
            "stdout_contract", "timeout_seconds", "mutation", "placeholders",
        }
        entry = reg.REGISTRY["review_compact.validate"]
        missing = required_fields - set(entry.keys())
        assert not missing, f"Missing fields: {missing}"

    def test_entry_id_matches_key(self):
        """GIVEN the entry WHEN inspected THEN id matches the registry key."""
        entry = reg.REGISTRY["review_compact.validate"]
        assert entry["id"] == "review_compact.validate"

    def test_entry_argv_is_list_of_strings(self):
        """GIVEN the entry WHEN inspected THEN argv is list[str] (no shell string)."""
        entry = reg.REGISTRY["review_compact.validate"]
        argv = entry["argv"]
        assert isinstance(argv, list)
        assert all(isinstance(tok, str) for tok in argv)

    def test_entry_shell_is_false(self):
        """GIVEN the entry WHEN inspected THEN shell is exactly False."""
        entry = reg.REGISTRY["review_compact.validate"]
        assert entry["shell"] is False

    def test_entry_mutation_is_false(self):
        """GIVEN the entry WHEN inspected THEN mutation is exactly False."""
        entry = reg.REGISTRY["review_compact.validate"]
        assert entry["mutation"] is False

    def test_entry_network_effect_is_local_only(self):
        """GIVEN the entry WHEN inspected THEN network_effect is local_only."""
        entry = reg.REGISTRY["review_compact.validate"]
        assert entry["network_effect"] == "local_only"

    def test_entry_argv_points_to_validator_script(self):
        """GIVEN the entry WHEN inspected THEN argv references
        validate_review_compact_output.py under the issue-refinement-loop
        scripts directory via `uv run --locked --offline --no-sync python3`
        (no shell wrapper) followed by the required --issue-number
        placeholder (AC22)."""
        entry = reg.REGISTRY["review_compact.validate"]
        argv = entry["argv"]
        assert argv[0] == "uv"
        assert argv[1] == "run"
        assert argv[2] == "--locked"
        assert argv[3] == "--offline"
        assert argv[4] == "--no-sync"
        assert argv[5] == "python3"
        assert argv[6].endswith("validate_review_compact_output.py")
        assert "issue-refinement-loop/scripts/" in argv[6]
        assert argv[7] == "--issue-number"
        assert argv[8] == "{issue_number}"

    def test_entry_appears_in_export_registry(self):
        """GIVEN export_registry() WHEN called THEN review_compact.validate
        is present in the exported commands dict."""
        data = reg.export_registry()
        assert "review_compact.validate" in data["commands"]

    def test_entry_argv_free_of_shell_operators(self):
        """GIVEN the entry's argv tokens WHEN checked THEN none contain
        shell operator substrings (structurally safe for shell=False exec)."""
        deny_tokens = {"&&", "||", ";", "|", ">", "<", ">>", "<<", "`", "$("}
        entry = reg.REGISTRY["review_compact.validate"]
        for token in entry["argv"]:
            for deny in deny_tokens:
                assert deny not in token, f"argv token {token!r} contains denied operator {deny!r}"

    def test_entry_placeholders_require_issue_number(self):
        """GIVEN the entry's placeholders WHEN inspected THEN issue_number
        is a required positive_int placeholder (Issue #1507 AC15/AC22)."""
        entry = reg.REGISTRY["review_compact.validate"]
        placeholders = entry["placeholders"]
        assert "issue_number" in placeholders
        assert placeholders["issue_number"]["type"] == "positive_int"
        assert placeholders["issue_number"]["required"] is True

    def test_registry_entry_invoked_as_subprocess(self):
        """GIVEN render_command('review_compact.validate', {'issue_number': N})
        WHEN the rendered argv is invoked as a real subprocess with a valid
        V2 approve envelope on stdin THEN exit code 0 and stdout is a single
        REVIEW_COMPACT_VALIDATION_RESULT_V1 JSON object (Issue #2054 AC5/AC8:
        the registered CLI validates ISSUE_REVIEW_RESULT_COMPACT_V2 only)."""
        import reviewer_transport as transport

        argv = reg.render_command("review_compact.validate", {"issue_number": 1507})
        stdin_bytes = transport.build_compact_v2(
            verdict="approve",
            summary="contract ready",
            blockers=0,
            reviewed_body_sha256="sha256:" + "1" * 64,
            attempt_id="registry-entry-attempt",
            artifact_relative="1507/registry-entry-attempt/attempt-001/compact_review_result_v2.json",
            artifact_sha256="sha256:" + "2" * 64,
        )
        proc = subprocess.run(
            argv,
            input=stdin_bytes,
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8")
        payload = json.loads(proc.stdout.decode("utf-8"))
        assert payload["schema"] == "REVIEW_COMPACT_VALIDATION_RESULT_V1"
        assert payload["validation_status"] == "valid"

    def test_registry_entry_invoked_as_subprocess_rejects_mismatched_issue_number(self):
        """GIVEN render_command bound to a different --issue-number than the
        ARTIFACT's issue segment WHEN invoked as a real subprocess THEN
        exit code 1 and validation_status invalid (Issue #2054 AC5/AC8 parity
        with the retired V1 AC15/AC22 behavior)."""
        import reviewer_transport as transport

        argv = reg.render_command("review_compact.validate", {"issue_number": 9999})
        stdin_bytes = transport.build_compact_v2(
            verdict="approve",
            summary="contract ready",
            blockers=0,
            reviewed_body_sha256="sha256:" + "1" * 64,
            attempt_id="registry-entry-attempt",
            artifact_relative="1507/registry-entry-attempt/attempt-001/compact_review_result_v2.json",
            artifact_sha256="sha256:" + "2" * 64,
        )
        proc = subprocess.run(
            argv,
            input=stdin_bytes,
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 1
        payload = json.loads(proc.stdout.decode("utf-8"))
        assert payload["validation_status"] == "invalid"


def test_review_compact_validate_stdin_contract_is_v2_not_retired_v1():
    """The validator consumes ISSUE_REVIEW_RESULT_COMPACT_V2 wire bytes; the retired V1 contract
    name must not survive in the registry (Stage 1b-3: stale V1 stdin_contract residue)."""
    contract = reg.REGISTRY["review_compact.validate"]["stdin_contract"]
    assert contract == "issue_review_result_compact_v2/raw_bytes"
    assert "v1" not in contract
