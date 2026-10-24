#!/usr/bin/env python3
"""Tests for semantic_review_transport.py (Issue #2296 AC2/AC8; fix_delta
iteration 6).

These are fake-agent-transport integration tests: a "fake agent" here is
simply a function that writes an issue-design-reviewer-shaped raw JSON
result file, standing in for the orchestrator's Agent tool call. The
regression this file protects against (AC8) is a transport that accepts a
result WITHOUT the caller having actually waited for the launched SubAgent
to complete -- e.g. reusing a stale canned fixture, or racing ahead of the
pin timestamp. mtime is only ever checked/asserted as a weak staleness
heuristic here (P0-2) -- these tests do not claim it proves "foreground"
execution.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import semantic_review_transport as transport  # noqa: E402


def _now_str(offset_seconds: float = 0.0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fake_agent_writes_result(path: Path, assessment: str, findings=None) -> None:
    """Stand-in for issue-design-reviewer's raw stdout capture."""
    path.write_text(
        json.dumps({"assessment": assessment, "findings": findings or []}),
        encoding="utf-8",
    )


def test_pin_bundle_writes_bundle_and_returns_invocation_id():
    with tempfile.TemporaryDirectory() as d:
        artifacts_root = Path(d) / "artifacts"
        bundle = transport.pin_bundle(
            issue_number=1,
            body_text="some issue body",
            prompt_version="v1",
            requested_model="sonnet",
            artifacts_root=artifacts_root,
        )
        inv_dir = Path(bundle["invocation_dir"])
        assert (inv_dir / "bundle.json").exists()
        assert "cache_hit" not in bundle


def test_pin_bundle_writes_body_md_matching_body_sha256():
    """P0-1: pin-bundle must persist the pinned Issue body as a body.md
    file whose content hash matches bundle.json's body_sha256, so a fresh
    issue-design-reviewer subagent context has a concrete file to read."""
    with tempfile.TemporaryDirectory() as d:
        artifacts_root = Path(d) / "artifacts"
        body_text = "pinned issue body text\nwith multiple lines\n"
        bundle = transport.pin_bundle(
            issue_number=1,
            body_text=body_text,
            prompt_version="v1",
            requested_model="sonnet",
            artifacts_root=artifacts_root,
        )
        inv_dir = Path(bundle["invocation_dir"])
        bundle_json = json.loads((inv_dir / "bundle.json").read_text(encoding="utf-8"))
        assert bundle_json["body_file"] == "body.md"
        body_file_path = inv_dir / bundle_json["body_file"]
        assert body_file_path.exists()
        assert body_file_path.read_text(encoding="utf-8") == body_text
        assert transport._sha256_hex(body_file_path.read_bytes()) == bundle_json["body_sha256"]
        assert bundle_json["body_sha256"] == bundle["body_sha256"]


def test_pin_bundle_is_idempotent_invocation_id_for_same_body_and_model():
    with tempfile.TemporaryDirectory() as d:
        artifacts_root = Path(d) / "artifacts"
        b1 = transport.pin_bundle(
            issue_number=1,
            body_text="same body",
            prompt_version="v1",
            requested_model="sonnet",
            artifacts_root=artifacts_root,
        )
        b2 = transport.pin_bundle(
            issue_number=1,
            body_text="same body",
            prompt_version="v1",
            requested_model="sonnet",
            artifacts_root=artifacts_root,
        )
        assert b1["invocation_id"] == b2["invocation_id"]


def test_pin_bundle_different_body_gives_different_invocation_id():
    with tempfile.TemporaryDirectory() as d:
        artifacts_root = Path(d) / "artifacts"
        b1 = transport.pin_bundle(
            issue_number=1,
            body_text="body A",
            prompt_version="v1",
            requested_model="sonnet",
            artifacts_root=artifacts_root,
        )
        b2 = transport.pin_bundle(
            issue_number=1,
            body_text="body B",
            prompt_version="v1",
            requested_model="sonnet",
            artifacts_root=artifacts_root,
        )
        assert b1["invocation_id"] != b2["invocation_id"]


def test_pin_bundle_never_reuses_a_prior_successful_result_without_relaunch():
    """P1-2: the cross-invocation result cache/reuse mechanism is removed
    entirely -- pinning the same (body_sha256, prompt_version,
    requested_model) again always re-pins fresh and does not report or
    return any prior cached result."""
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "cache reuse body", 12)
        result_file = Path(d) / "result.json"
        _fake_agent_writes_result(result_file, "clear")
        transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )

        bundle2 = transport.pin_bundle(
            issue_number=12,
            body_text="cache reuse body",
            prompt_version="v1",
            requested_model="sonnet",
            artifacts_root=Path(d) / "artifacts",
        )
        assert "cache_hit" not in bundle2
        assert "cached_result" not in bundle2
        assert bundle2["invocation_id"] == bundle["invocation_id"]


def test_record_result_success_after_genuine_wait():
    """GIVEN pin_bundle() has pinned an invocation
    WHEN a "fake agent" writes its result strictly AFTER the pin timestamp
      AND completed_at is strictly after pinned_at
    THEN record_result() returns transport_status: ok and persists an
    artifact restricted to the model-writable + transport-bound fields,
    validated against schemas/semantic_review_result_v1.schema.json."""
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "genuine wait body", 42)
        result_file = Path(d) / "result.json"
        _fake_agent_writes_result(result_file, "clear")

        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )
        assert outcome["transport_status"] == "ok"
        assert outcome["artifact"]["assessment"] == "clear"
        assert outcome["artifact"]["artifact_valid"] is True
        assert outcome["artifact"]["input_binding_valid"] is True
        assert outcome["artifact"]["freshness_valid"] is True
        assert "owner_disposition" not in outcome["artifact"]

        artifact_path = Path(bundle["invocation_dir"]) / "semantic_review_result.json"
        assert artifact_path.exists()


def test_record_result_rejects_stale_result_reused_without_genuine_wait():
    """AC8 (fake-agent-transport structural guard): a fake transport that
    tries to complete WITHOUT actually waiting -- by reusing a result file
    whose mtime PREDATES the pin -- must fail, not silently succeed. This
    mtime check is a weak staleness heuristic only (P0-2), not proof of
    genuine agent execution."""
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "stale reuse body", 42)
        stale_result = Path(d) / "stale_result.json"
        _fake_agent_writes_result(stale_result, "clear")
        old_epoch = time.time() - 10_000
        os.utime(stale_result, (old_epoch, old_epoch))

        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=stale_result,
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )
        assert outcome["transport_status"] == "error"
        assert outcome["reason_code"] == "stale_result_reused"
        assert outcome["artifact"] is None


def test_record_result_rejects_completed_at_not_after_pinned_at():
    """A fake transport that doesn't actually wait supplies a completed_at
    that is not strictly after pinned_at -- rejected (foreground_not_verified)."""
    with tempfile.TemporaryDirectory() as d:
        artifacts_root = Path(d) / "artifacts"
        bundle = transport.pin_bundle(
            issue_number=7,
            body_text="no wait body",
            prompt_version="v1",
            requested_model="sonnet",
            artifacts_root=artifacts_root,
        )
        result_file = Path(d) / "immediate_result.json"
        _fake_agent_writes_result(result_file, "clear")

        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=bundle["pinned_at"],
            current_body_sha256=bundle["body_sha256"],
        )
        assert outcome["transport_status"] == "error"
        assert outcome["reason_code"] == "foreground_not_verified"


def test_record_result_rejects_missing_result_file():
    with tempfile.TemporaryDirectory() as d:
        artifacts_root = Path(d) / "artifacts"
        bundle = transport.pin_bundle(
            issue_number=7,
            body_text="missing file body",
            prompt_version="v1",
            requested_model="sonnet",
            artifacts_root=artifacts_root,
        )
        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=Path(d) / "does_not_exist.json",
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )
        assert outcome["transport_status"] == "error"
        assert outcome["reason_code"] == "missing_result_file"


def test_record_result_rejects_model_self_disposition_top_level():
    """P0-3: model output must not include owner_disposition at all."""
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "self disposition body", 8)
        result_file = Path(d) / "result.json"
        result_file.write_text(
            json.dumps(
                {
                    "assessment": "clear",
                    "findings": [],
                    "owner_disposition": {"status": "accepted", "recorded_by": "owner"},
                }
            ),
            encoding="utf-8",
        )
        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )
        assert outcome["transport_status"] == "error"
        assert outcome["reason_code"] == "model_self_disposition_forbidden"


def test_record_result_rejects_model_self_disposition_inside_finding():
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "finding disposition body", 9)
        result_file = Path(d) / "result.json"
        result_file.write_text(
            json.dumps(
                {
                    "assessment": "findings",
                    "findings": [
                        {
                            "severity": "high",
                            "summary": "x",
                            "owner_disposition": {
                                "status": "accepted",
                                "recorded_by": "owner",
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )
        assert outcome["transport_status"] == "error"
        assert outcome["reason_code"] == "model_self_disposition_forbidden"


def test_record_result_rejects_duplicate_json_keys():
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "dup key body", 10)
        result_file = Path(d) / "result.json"
        result_file.write_text(
            '{"assessment": "clear", "assessment": "findings", "findings": []}',
            encoding="utf-8",
        )
        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )
        assert outcome["transport_status"] == "error"
        assert "malformed_json" in outcome["reason_code"]


def test_record_result_rejects_invalid_severity():
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "bad severity body", 11)
        result_file = Path(d) / "result.json"
        result_file.write_text(
            json.dumps(
                {
                    "assessment": "findings",
                    "findings": [{"severity": "critical", "summary": "x"}],
                }
            ),
            encoding="utf-8",
        )
        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )
        assert outcome["transport_status"] == "error"
        assert outcome["reason_code"] == "invalid_finding_severity"


_MISSING = object()
_OK = {"assessment": "findings", "findings": [{"severity": "high", "summary": "x"}]}


@pytest.mark.parametrize(
    "raw_findings, ok",
    [({}, 0), (False, 0), (0, 0), ("", 0), (None, 0), (_MISSING, 0), ([], 1), (_OK["findings"], 1)],
)
def test_record_result_raw_findings_shape(raw_findings, ok):
    """Raw non-list/absent findings are rejected, never normalized to []."""
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "raw findings shape body", 12)
        raw = {"assessment": "findings" if raw_findings else "clear"}
        if raw_findings is not _MISSING:
            raw["findings"] = raw_findings
        result_file = Path(d) / "result.json"
        result_file.write_text(json.dumps(raw), encoding="utf-8")
        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )
        persisted = (Path(bundle["invocation_dir"]) / "semantic_review_result.json").exists()
        assert (outcome["transport_status"] == "ok") == bool(ok) == persisted
        if not ok:
            assert outcome["reason_code"] == "invalid_findings_shape"


def test_record_result_rejects_assessment_clear_with_nonempty_findings():
    """P0-3 (schema + explicit invariant): assessment=clear plus a
    blocker/high finding present must be rejected as invalid, not
    approved."""
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "clear with findings body", 20)
        result_file = Path(d) / "result.json"
        result_file.write_text(
            json.dumps(
                {
                    "assessment": "clear",
                    "findings": [{"severity": "blocker", "summary": "should not be here"}],
                }
            ),
            encoding="utf-8",
        )
        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )
        assert outcome["transport_status"] == "error"
        assert outcome["reason_code"] == "assessment_findings_mismatch:clear_with_findings"
        artifact_path = Path(bundle["invocation_dir"]) / "semantic_review_result.json"
        assert not artifact_path.exists()


def test_record_result_rejects_assessment_findings_with_empty_findings():
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "findings empty body", 21)
        result_file = Path(d) / "result.json"
        result_file.write_text(
            json.dumps({"assessment": "findings", "findings": []}),
            encoding="utf-8",
        )
        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=_now_str(),
            current_body_sha256=bundle["body_sha256"],
        )
        assert outcome["transport_status"] == "error"
        assert outcome["reason_code"] == "assessment_findings_mismatch:findings_empty"


def test_record_result_freshness_invalid_when_body_sha256_stale():
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d, "freshness body", 13)
        result_file = Path(d) / "result.json"
        _fake_agent_writes_result(result_file, "clear")
        outcome = transport.record_result(
            invocation_dir=bundle["invocation_dir"],
            result_file=result_file,
            completed_at=_now_str(),
            current_body_sha256="0" * 64,
        )
        assert outcome["transport_status"] == "stale"
        assert outcome["artifact"]["freshness_valid"] is False


def test_record_result_current_body_sha256_is_a_required_keyword_argument():
    """P1-2: current_body_sha256 must always be explicitly supplied -- it
    is no longer an optional freshness re-check."""
    import inspect

    sig = inspect.signature(transport.record_result)
    assert sig.parameters["current_body_sha256"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Issue #3007: bare / "sha256:"-prefixed current_body_sha256 equivalence and
# invalid-format vs genuine-drift separation.
# ---------------------------------------------------------------------------

_TRANSPORT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "semantic_review_transport.py"
_JOIN_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"

_HIGH_FINDING = {
    "severity": "high",
    "summary": "AC が検証不能な設計になっている",
}


def _pin_and_wait(d: str, body_text: str = "sha256 notation body", issue_number: int = 3007):
    """pin a bundle and wait so completed_at / mtime satisfy the join barrier."""
    bundle = transport.pin_bundle(
        issue_number=issue_number,
        body_text=body_text,
        prompt_version="v1",
        requested_model="sonnet",
        artifacts_root=Path(d) / "artifacts",
    )
    time.sleep(1.05)
    return bundle


def _record(bundle, d: str, digest: str, assessment: str = "clear", findings=None):
    result_file = Path(d) / "result.json"
    _fake_agent_writes_result(result_file, assessment, findings)
    return transport.record_result(
        invocation_dir=bundle["invocation_dir"],
        result_file=result_file,
        completed_at=_now_str(),
        current_body_sha256=digest,
    )


def test_record_result_equivalent_prefixed_and_bare_sha256():
    """AC1: bare and sha256:-prefixed forms of the same digest are equivalent."""
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d)
        bare = bundle["body_sha256"]
        for digest in (bare, "sha256:" + bare):
            outcome = _record(bundle, d, digest)
            assert outcome["transport_status"] == "ok", digest
            assert outcome["reason_code"] is None
            assert outcome["artifact"]["freshness_valid"] is True
            # persisted / returned body_sha256 is always the bare form.
            assert outcome["artifact"]["body_sha256"] == bare


def test_record_result_distinguishes_invalid_format_from_real_body_drift():
    """AC2: a different well-formed digest is stale; malformed digests are
    error/invalid_current_body_sha256 and keep the validated findings."""
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d)
        bare = bundle["body_sha256"]
        other = "0" * 64
        for digest in (other, "sha256:" + other):
            outcome = _record(bundle, d, digest)
            assert outcome["transport_status"] == "stale", digest
            assert outcome["reason_code"] is None
            assert outcome["artifact"]["freshness_valid"] is False

        invalid_digests = [
            "",
            bare[:63],
            bare + "0",
            bare.upper(),
            "sha256:" + bare.upper(),
            " " + bare,
            bare + " ",
            bare + "\n",
            "sha256:" + bare + "\n",
            "SHA256:" + bare,
            "sha256:",
            "sha256: " + bare,
            "sha512:" + bare,
            "sha256:sha256:" + bare,
            "not-a-digest",
            "g" * 64,
        ]
        for digest in invalid_digests:
            outcome = _record(bundle, d, digest, assessment="findings", findings=[dict(_HIGH_FINDING)])
            assert outcome["transport_status"] == "error", repr(digest)
            assert outcome["reason_code"] == "invalid_current_body_sha256", repr(digest)
            artifact = outcome["artifact"]
            assert artifact is not None, repr(digest)
            assert artifact["freshness_valid"] is False
            assert artifact["artifact_valid"] is True
            assert artifact["body_sha256"] == bare
            assert artifact["findings"] == [_HIGH_FINDING]
            persisted = json.loads(
                (Path(bundle["invocation_dir"]) / "semantic_review_result.json").read_text(encoding="utf-8")
            )
            assert persisted == artifact

        # An invalid raw reviewer result keeps its existing rejection reason
        # even when the caller digest is also malformed.
        outcome = _record(bundle, d, "garbage", assessment="clear", findings=[dict(_HIGH_FINDING)])
        assert outcome["transport_status"] == "error"
        assert outcome["reason_code"] == "assessment_findings_mismatch:clear_with_findings"
        assert outcome["artifact"] is None


def _run_record_cli(bundle, result_file: Path, completed_at: str, digest=None, omit_digest=False):
    cmd = [
        sys.executable,
        str(_TRANSPORT_SCRIPT),
        "record-result",
        "--invocation-dir",
        bundle["invocation_dir"],
        "--result-file",
        str(result_file),
        "--completed-at",
        completed_at,
    ]
    if not omit_digest:
        cmd += ["--current-body-sha256", digest]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_record_result_cli_exit_codes_and_persisted_artifact():
    """AC2/AC3: real CLI subprocess -- exit codes, stdout JSON, persisted artifact."""
    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d)
        bare = bundle["body_sha256"]
        result_file = Path(d) / "result.json"
        _fake_agent_writes_result(result_file, "findings", [dict(_HIGH_FINDING)])
        completed_at = _now_str()
        persisted_path = Path(bundle["invocation_dir"]) / "semantic_review_result.json"

        for digest in (bare, "sha256:" + bare):
            proc = _run_record_cli(bundle, result_file, completed_at, digest)
            assert proc.returncode == 0, proc.stderr
            out = json.loads(proc.stdout)
            assert out["transport_status"] == "ok"
            assert out["artifact"]["freshness_valid"] is True

        for digest in ("1" * 64, "sha256:" + "1" * 64):
            proc = _run_record_cli(bundle, result_file, completed_at, digest)
            assert proc.returncode == 0, proc.stderr
            out = json.loads(proc.stdout)
            assert out["transport_status"] == "stale"
            assert out["artifact"]["freshness_valid"] is False

        for digest in ("", bare.upper(), bare[:10], bare + "\n", "nonsense"):
            proc = _run_record_cli(bundle, result_file, completed_at, digest)
            assert proc.returncode == 1, (digest, proc.stdout, proc.stderr)
            out = json.loads(proc.stdout)
            assert out["transport_status"] == "error"
            assert out["reason_code"] == "invalid_current_body_sha256"
            assert out["artifact"]["freshness_valid"] is False
            assert out["artifact"]["artifact_valid"] is True
            assert out["artifact"]["findings"] == [_HIGH_FINDING]
            persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
            assert persisted == out["artifact"]
            assert persisted["body_sha256"] == bare
            assert persisted["findings"] == [_HIGH_FINDING]

        # missing required option keeps the argparse usage error (exit 2).
        proc = _run_record_cli(bundle, result_file, completed_at, omit_digest=True)
        assert proc.returncode == 2
        assert "--current-body-sha256" in proc.stderr


def test_invalid_digest_preserves_high_finding_through_join():
    """AC3: transport error caused only by a malformed caller digest must not
    drop a validated high finding -- the joiner still returns needs-fix."""
    sys.path.insert(0, str(_JOIN_SCRIPT_DIR))
    import join_review_results as joiner

    with tempfile.TemporaryDirectory() as d:
        bundle = _pin_and_wait(d)
        outcome = _record(bundle, d, "NOT-A-DIGEST", assessment="findings", findings=[dict(_HIGH_FINDING)])
        assert outcome["transport_status"] == "error"
        assert outcome["reason_code"] == "invalid_current_body_sha256"
        artifact = outcome["artifact"]
        assert artifact is not None

        assert artifact["findings"], "validated high finding must be present on the artifact"
        for policy in ("best_effort", "required"):
            for retry in (False, True):
                case = (policy, retry)
                joined = joiner.join_review_results(
                    deterministic_verdict="approve",
                    semantic_assessment=artifact["assessment"],
                    transport_status=outcome["transport_status"],
                    findings=artifact["findings"],
                    transport_policy=policy,
                    retry_already_attempted=retry,
                    source_artifact=str(Path(bundle["invocation_dir"]) / "semantic_review_result.json"),
                    checked_body_sha256=artifact["body_sha256"],
                )
                assert joined["effective_verdict"] == "needs-fix", case
                assert joined["effective_verdict"] != "approve", case
                assert joined["rewrite_lane"] == "semantic", case
                preserved = joined["semantic_rewrite_constraints"]["findings"]
                assert preserved == [_HIGH_FINDING], case


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
