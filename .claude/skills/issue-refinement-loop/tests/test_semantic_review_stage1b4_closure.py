"""Stage 1b-4 (agent-platform#15) closure / wiring tests for the semantic review consumer.

New in this repository (not in the source tag): the migrated unit tests exercise `pin_bundle` /
`record_result` / `join_review_results` in isolation with hand-written strings, so nothing proved
that a REAL `record_result` outcome is what `join_review_results` consumes. These tests drive the
three real modules end to end (pin -> record -> join) with positive and tampered controls, check
that the scripts import under `python -I`, validate the schema with jsonschema, and show that a
shifted `parents[4]` layout fails closed instead of validating against a missing schema.

Evidence boundary: the "reviewer" here is a fake that writes a JSON file. No real
`issue-design-reviewer` SubAgent runs; this is fixture evidence, never live evidence.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SKILL_DIR.parents[2]
_SCRIPTS = _SKILL_DIR / "scripts"
_SCHEMA_PATH = _REPO_ROOT / "schemas" / "semantic_review_result_v1.schema.json"
_SEMANTIC_SCRIPTS = (
    "semantic_review_transport.py",
    "semantic_review_trigger.py",
    "join_review_results.py",
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRANSPORT = _load(_SCRIPTS / "semantic_review_transport.py", "stage1b4_transport")
JOINER = _load(_SCRIPTS / "join_review_results.py", "stage1b4_joiner")


def _stamp(offset_seconds: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def pinned(tmp_path_factory):
    """One pinned invocation shared by the scenarios; a genuine wait separates pin and completion."""
    root = tmp_path_factory.mktemp("stage1b4")
    bundle = TRANSPORT.pin_bundle(
        issue_number=15,
        body_text="# pinned body\n",
        prompt_version="v1",
        requested_model="sonnet",
        artifacts_root=root / "artifacts",
    )
    time.sleep(1.05)
    return root, bundle


def _record(pinned, raw: str, *, completed_at: str | None = None, digest: str | None = None):
    root, bundle = pinned
    result_file = root / f"raw-{abs(hash(raw))}.json"
    result_file.write_text(raw, encoding="utf-8")
    return TRANSPORT.record_result(
        invocation_dir=bundle["invocation_dir"],
        result_file=result_file,
        completed_at=completed_at or _stamp(),
        current_body_sha256=digest or bundle["body_sha256"],
    )


def _join(outcome: dict, **kwargs):
    """Feed a REAL record_result outcome into the real joiner, the way the orchestrator does."""
    artifact = outcome.get("artifact") or {}
    return JOINER.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment=artifact.get("assessment", "missing"),
        transport_status=outcome["transport_status"],
        findings=artifact.get("findings"),
        **kwargs,
    )


CLEAR = json.dumps({"assessment": "clear", "findings": []})
BLOCKER = json.dumps(
    {"assessment": "findings", "findings": [{"severity": "blocker", "summary": "AC cannot be verified"}]}
)


def test_canonical_result_flows_pin_record_join_to_approve(pinned):
    outcome = _record(pinned, CLEAR)
    assert outcome["transport_status"] == "ok"
    joined = _join(outcome, transport_policy="required")
    assert joined["effective_verdict"] == "approve"
    assert joined["warnings"] == [] and joined["semantic_review_unavailable"] is False


def test_open_blocker_from_a_real_record_reaches_needs_fix(pinned):
    outcome = _record(pinned, BLOCKER)
    assert outcome["transport_status"] == "ok"
    assert _join(outcome)["effective_verdict"] == "needs-fix"


def test_body_drift_is_stale_and_required_policy_escalates_to_human(pinned):
    outcome = _record(pinned, CLEAR, digest="0" * 64)
    assert outcome["transport_status"] == "stale"
    assert outcome["artifact"]["freshness_valid"] is False
    assert _join(outcome, transport_policy="required")["effective_verdict"] == "human_judgment_required"
    assert _join(outcome, transport_policy="best_effort")["effective_verdict"] == "retry"
    retried = _join(outcome, transport_policy="best_effort", retry_already_attempted=True)
    assert retried["effective_verdict"] == "approve"
    assert retried["semantic_review_unavailable"] is True
    assert any(JOINER.SEMANTIC_REVIEW_UNAVAILABLE_WARNING in w for w in retried["warnings"])


@pytest.mark.parametrize(
    ("label", "raw", "reason"),
    [
        (
            "self-exemption at top level",
            json.dumps({"assessment": "clear", "findings": [], "owner_disposition": {"state": "accepted"}}),
            "model_self_disposition_forbidden",
        ),
        (
            "self-exemption inside a finding",
            json.dumps(
                {
                    "assessment": "findings",
                    "findings": [{"severity": "high", "summary": "x", "owner_disposition": {"state": "accepted"}}],
                }
            ),
            "model_self_disposition_forbidden",
        ),
        ("duplicate JSON key", '{"assessment": "clear", "assessment": "findings", "findings": []}', "malformed_json"),
        ("unknown assessment enum", json.dumps({"assessment": "maybe", "findings": []}), "invalid_assessment"),
        (
            "unknown severity enum",
            json.dumps({"assessment": "findings", "findings": [{"severity": "fatal", "summary": "x"}]}),
            "invalid_finding_severity",
        ),
        ("not a JSON object", "[1, 2]", "model_output_not_object"),
        ("unexpected top-level key", json.dumps({"assessment": "clear", "findings": [], "extra": 1}), "unexpected"),
    ],
)
def test_tampered_or_malformed_result_is_an_error_and_never_approves_when_required(pinned, label, raw, reason):
    outcome = _record(pinned, raw)
    assert outcome["transport_status"] == "error", label
    assert outcome["reason_code"].startswith(reason), label
    assert outcome["artifact"] is None, "a rejected result must not persist a bound artifact"
    assert _join(outcome, transport_policy="required")["effective_verdict"] == "human_judgment_required", label


def test_completion_not_after_pin_is_rejected(pinned):
    _, bundle = pinned
    outcome = _record(pinned, CLEAR, completed_at=bundle["pinned_at"])
    assert outcome["transport_status"] == "error" and outcome["reason_code"] == "foreground_not_verified"


def test_stale_result_file_older_than_the_pin_is_rejected(pinned):
    root, bundle = pinned
    old = root / "old-result.json"
    old.write_text(CLEAR, encoding="utf-8")
    pinned_epoch = TRANSPORT._parse_iso(bundle["pinned_at"])
    import os

    os.utime(old, (pinned_epoch - 60, pinned_epoch - 60))
    outcome = TRANSPORT.record_result(
        invocation_dir=bundle["invocation_dir"],
        result_file=old,
        completed_at=_stamp(),
        current_body_sha256=bundle["body_sha256"],
    )
    assert outcome["reason_code"] == "stale_result_reused"
    assert _join(outcome, transport_policy="required")["effective_verdict"] == "human_judgment_required"


def test_unknown_enum_values_reaching_the_joiner_are_transport_failures_not_success():
    for status, assessment in (("ok", "maybe"), ("fine", "clear"), ("ok", "missing")):
        joined = JOINER.join_review_results(
            deterministic_verdict="approve",
            semantic_assessment=assessment,
            transport_status=status,
            transport_policy="required",
        )
        assert joined["effective_verdict"] == "human_judgment_required", (status, assessment)


@pytest.mark.parametrize("script", _SEMANTIC_SCRIPTS)
def test_script_imports_under_isolated_mode(script):
    """`python -I` ignores PYTHONPATH/cwd: the script must import from stdlib alone (AC-1)."""
    code = (
        "import importlib.util, sys; p = sys.argv[1]; "
        "s = importlib.util.spec_from_file_location('probe', p); m = importlib.util.module_from_spec(s); "
        "sys.modules['probe'] = m; s.loader.exec_module(m)"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code, str(_SCRIPTS / script)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


def _canonical_artifact() -> dict:
    return {
        "schema": "SEMANTIC_REVIEW_RESULT_V1",
        "issue_number": 15,
        "body_sha256": "a" * 64,
        "prompt_version": "v1",
        "requested_model": "sonnet",
        "assessment": "findings",
        "findings": [{"severity": "high", "summary": "s", "evidence_refs": ["a.py"]}],
        "artifact_valid": True,
        "input_binding_valid": True,
        "freshness_valid": True,
    }


def test_schema_is_valid_draft07_with_a_public_safe_id():
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft7Validator.check_schema(schema)
    assert schema["$id"].startswith("urn:agent-platform:"), schema["$id"]


def test_schema_accepts_the_canonical_artifact_and_rejects_each_tampering():
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    assert list(validator.iter_errors(_canonical_artifact())) == []

    def mutated(**changes) -> dict:
        artifact = _canonical_artifact()
        artifact.update(changes)
        return artifact

    broken = {
        "unknown assessment": mutated(assessment="maybe"),
        "clear with findings": mutated(assessment="clear"),
        "findings with none": mutated(findings=[]),
        "unknown severity": mutated(findings=[{"severity": "fatal", "summary": "s"}]),
        "bad digest": mutated(body_sha256="XYZ"),
        "extra key": mutated(bogus=1),
        "freshness not boolean": mutated(freshness_valid="yes"),
        "missing required": {k: v for k, v in _canonical_artifact().items() if k != "freshness_valid"},
    }
    for label, artifact in broken.items():
        assert list(validator.iter_errors(artifact)), f"schema accepted: {label}"


def _install_transport(tmp_path: Path, depth_dirs: tuple[str, ...], with_schema: bool):
    """Copy the transport into `tmp_path/<depth_dirs>` and load it from there."""
    scripts = tmp_path.joinpath(*depth_dirs)
    scripts.mkdir(parents=True)
    copied = scripts / "semantic_review_transport.py"
    shutil.copy(_SCRIPTS / "semantic_review_transport.py", copied)
    if with_schema:
        (tmp_path / "schemas").mkdir()
        shutil.copy(_SCHEMA_PATH, tmp_path / "schemas" / _SCHEMA_PATH.name)
    return _load(copied, f"layout_probe_{len(depth_dirs)}_{with_schema}")


def _pin_and_record(module, tmp_path: Path):
    bundle = module.pin_bundle(
        issue_number=15, body_text="b", prompt_version="v1", requested_model="sonnet", artifacts_root=tmp_path / "a"
    )
    time.sleep(1.05)
    raw = tmp_path / "raw.json"
    raw.write_text(CLEAR, encoding="utf-8")
    return module.record_result(
        invocation_dir=bundle["invocation_dir"],
        result_file=raw,
        completed_at=_stamp(),
        current_body_sha256=bundle["body_sha256"],
    )


_CANONICAL_DEPTH = (".claude", "skills", "issue-refinement-loop", "scripts")


def test_layout_positive_control_resolves_the_schema_at_parents_4(tmp_path):
    module = _install_transport(tmp_path, _CANONICAL_DEPTH, with_schema=True)
    assert module._repo_root() == tmp_path
    assert _pin_and_record(module, tmp_path)["transport_status"] == "ok"


@pytest.mark.parametrize(
    "depth",
    [
        (".claude", "skills", "scripts"),  # one level too shallow
        ("extra", *_CANONICAL_DEPTH),  # one level too deep
    ],
    ids=["shallower", "deeper"],
)
def test_layout_shift_fails_closed_instead_of_skipping_schema_validation(tmp_path, depth):
    module = _install_transport(tmp_path, depth, with_schema=True)
    assert module._repo_root() != tmp_path
    outcome = _pin_and_record(module, tmp_path)
    assert outcome["transport_status"] == "error"
    assert outcome["reason_code"].startswith("schema_validation_failed")
