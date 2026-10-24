"""CLI wire reference for the V2-only validator."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402


def test_given_parent_v2_wire_when_validator_cli_runs_then_exact_bytes_are_accepted():
    raw = transport.build_compact_v2(
        verdict="approve",
        summary="contract ready",
        blockers=0,
        reviewed_body_sha256="sha256:" + "3" * 64,
        attempt_id="wire-parent",
        artifact_relative="2054/wire-parent/attempt-001/compact_review_result_v2.json",
        artifact_sha256="sha256:" + "4" * 64,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "validate_review_compact_output.py"),
            "--issue-number",
            "2054",
            "--invocation-id",
            "wire-parent",
            "--attempt",
            "1",
        ],
        input=raw,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["validation_status"] == "valid"
