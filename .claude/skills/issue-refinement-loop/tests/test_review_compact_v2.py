"""V2 compact wire contract: parent-owned and exact (Issue #2054)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402
import validate_review_compact_output as validator  # noqa: E402


def test_given_valid_v2_when_validated_then_exact_eleven_lines_are_accepted():
    raw = transport.build_compact_v2(
        verdict="approve",
        summary="contract ready",
        blockers=0,
        reviewed_body_sha256="sha256:" + "a" * 64,
        attempt_id="inv-1",
        artifact_relative="2054/inv-1/attempt-001/compact_review_result_v2.json",
        artifact_sha256="sha256:" + "b" * 64,
    )
    result = transport.validate_compact_v2(raw, issue_number=2054, invocation_id="inv-1", attempt=1)
    assert result["validation_status"] == "valid"
    assert raw.count(b"\n") == 11


def test_given_v1_or_extra_field_when_validated_then_fail_closed():
    old = b"STATUS: ok\nVERDICT: approve\n"
    assert transport.validate_compact_v2(old)["validation_status"] == "invalid"


def test_given_missing_out_of_order_or_mismatched_identity_when_validated_then_fail_closed():
    raw = transport.build_compact_v2(
        verdict="approve",
        summary="contract ready",
        blockers=0,
        reviewed_body_sha256="sha256:" + "a" * 64,
        attempt_id="inv-1",
        artifact_relative="2054/inv-1/attempt-001/compact_review_result_v2.json",
        artifact_sha256="sha256:" + "b" * 64,
    )
    assert transport.validate_compact_v2(raw.replace(b"SUMMARY", b"EXTRA", 1))["validation_status"] == "invalid"
    assert (
        transport.validate_compact_v2(raw.replace(b"2054/inv-1", b"2055/inv-1"), issue_number=2054)["validation_status"]
        == "invalid"
    )
    assert (
        transport.validate_compact_v2(raw.replace(b"attempt-001", b"attempt-002"), attempt=1)["validation_status"]
        == "invalid"
    )


def test_given_duplicate_json_when_strict_parse_then_rejected():
    try:
        transport.strict_json_loads(b'{"x":1,"x":2}')
    except ValueError as exc:
        assert str(exc) == "duplicate_json_key"
    else:
        raise AssertionError("duplicate keys must fail closed")


def test_given_v2_wire_when_registry_validator_consumes_bytes_then_v1_is_not_accepted(tmp_path: Path):
    raw = transport.build_compact_v2(
        verdict="approve",
        summary="contract ready",
        blockers=0,
        reviewed_body_sha256="sha256:" + "a" * 64,
        attempt_id="inv-1",
        artifact_relative="2054/inv-1/attempt-001/compact_review_result_v2.json",
        artifact_sha256="sha256:" + "b" * 64,
    )
    assert validator.build_result(raw, issue_number=2054, invocation_id="inv-1", attempt=1)[1] == 0
    assert validator.build_result(b"STATUS: ok\\nVERDICT: approve\\n", issue_number=2054)[1] == 1


def test_given_parent_transport_failure_when_router_consumes_receipt_then_environment_failure(tmp_path: Path):
    receipt = tmp_path / "transport.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "REVIEWER_TRANSPORT_RESULT_V1",
                "transport_status": "environment_failure",
                "semantic_verdict": None,
            }
        )
    )
    loop_state = tmp_path / "loop-state.json"
    loop_state.write_text(json.dumps({"iteration": 1, "max_iterations": 3, "last_verdict": None}))
    router = SCRIPTS / "decide_next_loop_action.py"
    run = subprocess.run(
        [
            sys.executable,
            str(router),
            "--loop-state-file",
            str(loop_state),
            "--reviewer-transport-result-file",
            str(receipt),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 4
    assert "STATUS: environment_failure" in run.stdout
