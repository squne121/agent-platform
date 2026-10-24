"""V2-only validator regression tests (Issue #2054)."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402
from validate_review_compact_output import build_result  # noqa: E402


def _wire() -> bytes:
    return transport.build_compact_v2(
        verdict="approve", summary="contract ready", blockers=0,
        reviewed_body_sha256="sha256:" + "1" * 64, attempt_id="validator-parent",
        artifact_relative="2054/validator-parent/attempt-001/compact_review_result_v2.json",
        artifact_sha256="sha256:" + "2" * 64,
    )


def test_given_exact_v2_bytes_when_validated_then_result_contains_original_byte_binding():
    result, code = build_result(_wire(), issue_number=2054, invocation_id="validator-parent", attempt=1)
    assert code == 0
    assert result["validation_status"] == "valid"
    assert result["input_sha256"].startswith("sha256:")


def test_given_v1_or_extra_line_when_validated_then_fail_closed():
    assert build_result(b"STATUS: ok\\nVERDICT: approve\\n", issue_number=2054)[1] == 1
    assert build_result(_wire() + b"EXTRA: forbidden\\n", issue_number=2054)[1] == 1
