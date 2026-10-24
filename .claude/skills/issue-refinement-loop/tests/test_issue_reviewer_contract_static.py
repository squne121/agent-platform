#!/usr/bin/env python3
"""
test_issue_reviewer_contract_static.py — Issue #2049 AC9.

PR #2135 human REQUEST_CHANGES iteration-3 (P0-2/P1-5/P2-6) regression
coverage: proves the 0-byte / malformed-but-non-empty child-stdout ->
canonical-validator -> exactly-once-retry -> `reviewer_transport_failure`
chain is a SINGLE real execution path (not a separately reimplemented
simplified classifier), proves the AC7 strict-JSON readback correctly
rejects actual NaN/Infinity JSON tokens while accepting a legitimate string
value that merely CONTAINS the substring "Infinity"/"NaN", and proves the
root-owned `produce` command cleans up its invocation-private intermediate
files (only the canonical persisted artifacts survive).

Issue #2161 (native Codex CLI retirement): this file previously also
exercised `run_root_review_pipeline.check_agent_is_read_only_advisory()`
(Issue #2049 AC9's static TOML-format read-only/workspace-write contradiction
checker) against the real `.codex/agents/issue-reviewer.toml`. That checker
had no other production caller and its sole target file was deleted along
with `.codex/**`, so both the checker (and its `_split_sentences()` /
`_WORKSPACE_WRITE_MARKERS` / `_NEGATION_CUES` helpers) in
`run_root_review_pipeline.py` and the 4 tests exercising it here were removed
as an orphan DELETE_CHAIN. The provider-neutral `.claude/agents/issue-reviewer.md`
counterpart uses Markdown frontmatter + prose, not the TOML key=value /
triple-quoted-string grammar this checker parsed, so porting it forward would
have required new format-detection logic rather than a mechanical rename --
out of scope for a retirement issue.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
PIPELINE_SCRIPT = (
    ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_root_review_pipeline.py"
)


def _load_pipeline_module():
    spec = importlib.util.spec_from_file_location("run_root_review_pipeline", PIPELINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_root_review_pipeline", module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_pipeline_module()


# ---------------------------------------------------------------------------
# P0-2: classify_child_stdout / retry_once_on_transport_failure via the
# canonical validate_review_compact_output() validator (single execution
# path, not a duplicate simplified classifier)
# ---------------------------------------------------------------------------

FIXTURES_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "fixtures"

SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import reviewer_transport as _transport  # noqa: E402
from _trusted_repo import TRUSTED_REPO  # noqa: E402

# Issue #2054 AC5: the V1 envelope (`EVIDENCE` field, `compact_review_result_v1=`
# prefix) is retired; classify_child_stdout() now classifies against the V2
# grammar exclusively (SSOT: reviewer_transport.py).
_APPROVE_STDOUT = _transport.build_compact_v2(
    verdict="approve",
    summary="contract ready",
    blockers=0,
    reviewed_body_sha256="sha256:" + "a" * 64,
    attempt_id="fixture-attempt-2049",
    artifact_relative="2049/fixture-attempt-2049/attempt-001/compact_review_result_v2.json",
    artifact_sha256="sha256:" + "b" * 64,
).decode("utf-8")


def test_classify_child_stdout_empty_input_is_reviewer_transport_failure_and_retryable():
    """GIVEN 0-byte child stdout
    WHEN classified via `classify_child_stdout` (which delegates to the
    canonical `validate_review_compact_output.validate_review_compact_output()`)
    THEN it is classified `reviewer_transport_failure` / `empty_input` and
    marked retryable (Issue #2049 AC4/AC5)."""
    result = _PIPELINE.classify_child_stdout("", issue_number=2049)
    assert result == {
        "classification": "reviewer_transport_failure",
        "code": "empty_input",
        "retryable": True,
    }


def test_classify_child_stdout_malformed_non_empty_is_not_silently_ok():
    """GIVEN non-empty but malformed child stdout (not valid JSON, not a
    canonical envelope)
    WHEN classified via `classify_child_stdout`
    THEN it is classified `reviewer_transport_failure` / `malformed_output`
    -- NOT silently accepted as "ok" (PR #2135 iteration-3 P0-2: this was
    the exact bug in the prior simplified `classify_child_stdout`, which
    treated ANY non-empty string as "ok" unconditionally)."""
    result = _PIPELINE.classify_child_stdout("not a canonical envelope at all", issue_number=2049)
    assert result["classification"] == "reviewer_transport_failure"
    assert result["code"] == "malformed_output"
    assert result["retryable"] is True


def test_classify_child_stdout_valid_envelope_is_ok():
    """GIVEN a valid ISSUE_REVIEW_RESULT_COMPACT_V1 approve envelope
    WHEN classified via `classify_child_stdout`
    THEN it is classified "ok" (not retried)."""
    result = _PIPELINE.classify_child_stdout(_APPROVE_STDOUT, issue_number=2049)
    assert result == {"classification": "ok", "code": None, "retryable": False}


def test_classify_child_stdout_uses_canonical_validator_not_a_duplicate():
    """GIVEN `run_root_review_pipeline.classify_child_stdout`
    WHEN inspected for its implementation
    THEN it delegates to the SAME
    `validate_review_compact_output.validate_review_compact_output()`
    function the orchestrator's Step 2 routing table uses (proves this is a
    single execution path, not a second reimplemented classifier -- PR #2135
    iteration-3 P0-2)."""
    import validate_review_compact_output as _canonical

    # Both call sites must agree on validation_status for the same malformed
    # input: if `classify_child_stdout` used a separate, simplified
    # classifier, this could silently diverge from the canonical validator's
    # verdict for non-empty malformed input.
    raw = "garbage, not an envelope"
    canonical_result = _canonical.validate_review_compact_output(raw, issue_number=2049)
    pipeline_result = _PIPELINE.classify_child_stdout(raw, issue_number=2049)
    assert canonical_result["validation_status"] == "invalid"
    assert pipeline_result["classification"] == "reviewer_transport_failure"


def test_retry_once_on_transport_failure_retries_exactly_once_on_empty_then_succeeds():
    """GIVEN a child invocation that returns 0-byte stdout on the first call
    and a valid envelope on the second
    WHEN `retry_once_on_transport_failure` orchestrates the retry
    THEN it retries exactly once and reports `status: ok` with 2 attempts
    (Issue #2049 AC4/AC5)."""
    calls = {"n": 0}

    def invoke_child():
        calls["n"] += 1
        return "" if calls["n"] == 1 else _APPROVE_STDOUT

    result = _PIPELINE.retry_once_on_transport_failure(invoke_child, issue_number=2049)
    assert calls["n"] == 2
    assert result["attempts"] == 2
    assert result["status"] == "ok"


def test_retry_once_on_transport_failure_stops_on_second_empty_result():
    """GIVEN a child invocation that returns 0-byte stdout on BOTH calls
    WHEN `retry_once_on_transport_failure` orchestrates the retry
    THEN it stops after exactly 2 attempts and reports
    `status: reviewer_transport_failure` (Issue #2049 AC6) -- it never
    retries a third time and never silently reports "ok"."""
    calls = {"n": 0}

    def invoke_child():
        calls["n"] += 1
        return ""

    result = _PIPELINE.retry_once_on_transport_failure(invoke_child, issue_number=2049)
    assert calls["n"] == 2
    assert result["attempts"] == 2
    assert result["status"] == "reviewer_transport_failure"


def test_retry_once_on_transport_failure_stops_on_second_malformed_result():
    """GIVEN a child invocation that returns non-empty MALFORMED stdout on
    BOTH calls
    WHEN `retry_once_on_transport_failure` orchestrates the retry
    THEN it stops after exactly 2 attempts and reports
    `status: reviewer_transport_failure` -- a second malformed (non-empty)
    result is treated the same as a second empty result (PR #2135
    iteration-3 P0-2 fix_direction (b)): it is never silently accepted as
    "ok" just because it is non-empty."""
    calls = {"n": 0}

    def invoke_child():
        calls["n"] += 1
        return "{not even json"

    result = _PIPELINE.retry_once_on_transport_failure(invoke_child, issue_number=2049)
    assert calls["n"] == 2
    assert result["attempts"] == 2
    assert result["status"] == "reviewer_transport_failure"


def test_retry_once_on_transport_failure_accepts_valid_result_on_first_try():
    """GIVEN a child invocation that returns a valid envelope immediately
    WHEN `retry_once_on_transport_failure` orchestrates the retry
    THEN it does NOT retry (exactly 1 attempt) and reports `status: ok`."""
    calls = {"n": 0}

    def invoke_child():
        calls["n"] += 1
        return _APPROVE_STDOUT

    result = _PIPELINE.retry_once_on_transport_failure(invoke_child, issue_number=2049)
    assert calls["n"] == 1
    assert result["attempts"] == 1
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# P1-5: readback_persisted_artifact() strict JSON via
# compact_review_result._strict_json_loads() (parser-level, not substring)
# ---------------------------------------------------------------------------


# Issue #2242 AC6/AC10: these fixtures previously hand-wrote the OLD flat V1
# shape (`{"body_sha256": ..., "verdict": ...}` top-level), which does not
# match what the canonical V2 writer (`reviewer_transport.write_semantic_artifact()`)
# actually persists (top-level `reviewed_body_sha256` + nested
# `semantic_result.verdict`) and therefore could not detect the writer/reader
# drift regression this Issue fixes -- they are updated here to the
# production writer's V2 output shape while preserving each test's original
# security-regression intent (malformed-JSON / duplicate-key / non-finite
# rejection must still fail closed; a legitimate string that merely CONTAINS
# "NaN"/"Infinity" must still be accepted).

_FIXTURE_REPO = TRUSTED_REPO
_FIXTURE_ISSUE = 2242
_FIXTURE_INVOCATION_ID = "fixture-readback-2242"
_FIXTURE_ATTEMPT = 1
# Issue #2242 OWNER adversarial review Blocker 4
# (source-repo PR #2246 comment 5328161000):
# a well-formed sha256:<64-hex> value is now required (the previous
# "sha256:aa" fixture value would itself be rejected by the new
# reviewer_transport.extract_binding_context() format check).
_FIXTURE_BODY_SHA256 = "sha256:" + "a" * 64


def _fixture_review_result_v1(FIXTURES_DIR: Path, *, message: str | None = None) -> dict:
    """A COMPLETE production-shaped REVIEW_ISSUE_RESULT_V1 fixture (Issue
    #2242 Blocker 4: readback now validates the full schema, not merely
    verdict+blocking_issues), optionally carrying an extra `message` field
    inside `blocking_issues[0]` so a legitimate "NaN"/"Infinity" SUBSTRING
    can still be exercised without breaking schema validity."""
    import json as _json

    fixture = _json.loads((FIXTURES_DIR / "review_result_approve.json").read_text(encoding="utf-8"))
    fixture["body_sha256"] = _FIXTURE_BODY_SHA256
    if message is not None:
        fixture["findings"] = [
            {
                "finding_kind": "checker_gap",
                "deterministic_domain_key": "fixture_message_probe",
                "blocking": False,
                "checker_evidence": [],
                "message": message,
            }
        ]
    return fixture


def test_readback_rejects_actual_nan_json_token(tmp_path):
    """GIVEN a persisted artifact containing a literal `NaN` JSON constant
    inside an otherwise production-shaped (V2) payload
    WHEN read back via `readback_persisted_artifact`
    THEN it is rejected `artifact_not_strict_json` (parser-level rejection,
    Issue #2049 AC7 / PR #2135 iteration-3 P1-5; Issue #2242 AC6)."""
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        '{"schema": "REVIEWER_COMPACT_ARTIFACT_V2", "repository": "example-org/example-repo", '
        '"issue_number": 2242, "reviewed_body_sha256": "sha256:aa", '
        '"invocation_id": "fixture-readback-2242", "attempt": 1, '
        '"semantic_result": {"verdict": "approve", "blocking_issues": [], "score": NaN}}',
        encoding="utf-8",
    )
    result = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative="artifact.json",
        expected_repo=_FIXTURE_REPO,
        expected_issue=_FIXTURE_ISSUE,
        expected_body_sha256=_FIXTURE_BODY_SHA256,
        expected_invocation_id=_FIXTURE_INVOCATION_ID,
        expected_attempt=_FIXTURE_ATTEMPT,
        expected_artifact_sha256="sha256:" + "0" * 64,
        expected_verdict="approve",
    )
    assert result["verdict_identity"] is False
    assert "artifact_not_strict_json" in result["violations"]


def test_readback_rejects_actual_infinity_json_token(tmp_path):
    """GIVEN a persisted artifact containing a literal `Infinity` JSON
    constant inside an otherwise production-shaped (V2) payload
    WHEN read back via `readback_persisted_artifact`
    THEN it is rejected `artifact_not_strict_json` (Issue #2049 AC7 / PR
    #2135 iteration-3 P1-5; Issue #2242 AC6)."""
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        '{"schema": "REVIEWER_COMPACT_ARTIFACT_V2", "repository": "example-org/example-repo", '
        '"issue_number": 2242, "reviewed_body_sha256": "sha256:aa", '
        '"invocation_id": "fixture-readback-2242", "attempt": 1, '
        '"semantic_result": {"verdict": "approve", "blocking_issues": [], "score": Infinity}}',
        encoding="utf-8",
    )
    result = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative="artifact.json",
        expected_repo=_FIXTURE_REPO,
        expected_issue=_FIXTURE_ISSUE,
        expected_body_sha256=_FIXTURE_BODY_SHA256,
        expected_invocation_id=_FIXTURE_INVOCATION_ID,
        expected_attempt=_FIXTURE_ATTEMPT,
        expected_artifact_sha256="sha256:" + "0" * 64,
        expected_verdict="approve",
    )
    assert result["verdict_identity"] is False
    assert "artifact_not_strict_json" in result["violations"]


def test_readback_accepts_legitimate_string_containing_infinity_substring(tmp_path):
    """GIVEN a persisted artifact, written by the REAL production writer
    (`reviewer_transport.write_semantic_artifact()`, not a hand-written
    fixture), whose ONLY appearance of the text "Infinity" is inside a
    legitimate `semantic_result` string value (not a JSON constant token)
    WHEN read back via `readback_persisted_artifact`
    THEN it is NOT rejected for that reason, and verdict/body-sha binding
    both resolve correctly against the V2 schema (PR #2135 iteration-3 P1-5
    closes the prior false-positive bug where a raw substring search over
    the whole file rejected valid strings like `"message": "Infinity
    handling"`; Issue #2242 AC6/AC10 proves this against the ACTUAL V2
    writer output, not a hand-written flat fixture)."""
    semantic_result = _fixture_review_result_v1(FIXTURES_DIR, message="Infinity handling")
    relative, artifact_sha256 = _transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=_FIXTURE_ISSUE,
        repo=_FIXTURE_REPO,
        invocation_id=_FIXTURE_INVOCATION_ID,
        attempt=_FIXTURE_ATTEMPT,
        reviewed_body_sha256=_FIXTURE_BODY_SHA256,
        semantic_result=semantic_result,
    )
    result = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_FIXTURE_REPO,
        expected_issue=_FIXTURE_ISSUE,
        expected_body_sha256=_FIXTURE_BODY_SHA256,
        expected_invocation_id=_FIXTURE_INVOCATION_ID,
        expected_attempt=_FIXTURE_ATTEMPT,
        expected_artifact_sha256=artifact_sha256,
        expected_verdict="approve",
    )
    assert result["verdict_identity"] is True
    assert result["violations"] == []


def test_readback_accepts_legitimate_string_containing_nan_substring(tmp_path):
    """GIVEN a persisted artifact, written by the REAL production writer,
    whose ONLY appearance of the text "NaN" is inside a legitimate
    `semantic_result` string value
    WHEN read back via `readback_persisted_artifact`
    THEN it is NOT rejected for that reason (PR #2135 iteration-3 P1-5;
    Issue #2242 AC6/AC10)."""
    semantic_result = _fixture_review_result_v1(FIXTURES_DIR, message="NaN check passed")
    relative, artifact_sha256 = _transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=_FIXTURE_ISSUE,
        repo=_FIXTURE_REPO,
        invocation_id=_FIXTURE_INVOCATION_ID,
        attempt=_FIXTURE_ATTEMPT,
        reviewed_body_sha256=_FIXTURE_BODY_SHA256,
        semantic_result=semantic_result,
    )
    result = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_FIXTURE_REPO,
        expected_issue=_FIXTURE_ISSUE,
        expected_body_sha256=_FIXTURE_BODY_SHA256,
        expected_invocation_id=_FIXTURE_INVOCATION_ID,
        expected_attempt=_FIXTURE_ATTEMPT,
        expected_artifact_sha256=artifact_sha256,
        expected_verdict="approve",
    )
    assert result["verdict_identity"] is True
    assert result["violations"] == []


# ---------------------------------------------------------------------------
# P1-3: persist_to_canonical_artifact_directory() run-unique filenames +
# hardened compact_review_result._atomic_write() reuse
# ---------------------------------------------------------------------------


def test_persist_to_canonical_artifact_directory_is_run_unique(tmp_path):
    """GIVEN two `persist_to_canonical_artifact_directory` calls for the SAME
    issue within the same wall-clock second
    WHEN persisted
    THEN they land at two DISTINCT final paths (run-unique `pid` + `uuid4`
    component), not a silent last-writer-wins collision (Issue #2049 AC3 /
    PR #2135 iteration-3 P1-3)."""
    payload = {"body_sha256": "sha256:aa", "verdict": "approve"}
    path_a = _PIPELINE.persist_to_canonical_artifact_directory(2049, payload, repo_root=tmp_path)
    path_b = _PIPELINE.persist_to_canonical_artifact_directory(2049, payload, repo_root=tmp_path)
    assert path_a != path_b
    assert path_a.exists()
    assert path_b.exists()


def test_persist_to_canonical_artifact_directory_uses_hardened_permissions(tmp_path):
    """GIVEN a persisted artifact
    WHEN its filesystem permissions are inspected
    THEN they are 0600 (proves reuse of `compact_review_result._atomic_write()`'s
    hardened primitive, not a separately reimplemented weaker writer -- PR
    #2135 iteration-3 P1-3)."""
    import stat

    payload = {"body_sha256": "sha256:aa", "verdict": "approve"}
    path = _PIPELINE.persist_to_canonical_artifact_directory(2049, payload, repo_root=tmp_path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# P0-1: produce_compact_result() is the SOLE producer of the compact
# envelope end-to-end; its stdout ARTIFACT reference matches the file it
# actually persisted
# ---------------------------------------------------------------------------


def test_produce_compact_result_persists_artifact_matching_its_own_stdout_reference(tmp_path):
    """GIVEN a merged REVIEW_ISSUE_RESULT_V1
    WHEN `produce_compact_result` renders + persists the compact envelope
    THEN the returned `artifact_path` is the SAME file the `ARTIFACT:` stdout
    line references (no post-hoc rename desyncing the two), the file exists,
    and its permissions are hardened 0600 (Issue #2049 AC1-AC3 / PR #2135
    iteration-3 P0-1/P1-3)."""
    import json as _json
    import stat

    fixture = _json.loads((FIXTURES_DIR / "review_result_approve.json").read_text(encoding="utf-8"))
    fixture["body_sha256"] = "sha256:" + "a" * 64

    result = _PIPELINE.produce_compact_result(2049, fixture, repo_root=tmp_path)
    artifact_line = next(line for line in result["stdout_lines"] if line.startswith("ARTIFACT:"))
    wire_path = artifact_line.split("=", 1)[1]

    assert result["artifact_path"].endswith(wire_path)
    assert Path(result["artifact_path"]).exists()
    assert stat.S_IMODE(Path(result["artifact_path"]).stat().st_mode) == 0o600
    assert result["verdict"] == "approve"


# ---------------------------------------------------------------------------
# P2-6: _cmd_produce() cleans up invocation-private intermediate files
# ---------------------------------------------------------------------------


def test_cmd_produce_cleans_up_intermediate_files_and_persists_canonical_artifacts(tmp_path, monkeypatch, capsys):
    """GIVEN a full (mocked) `produce` invocation
    WHEN it completes successfully
    THEN no intermediate files (pinned body, `*.review_result.json`,
    `*.readiness_result.json`, `*.merged_review_result.json`) remain under
    `tmp/` afterward -- only the canonical persisted artifacts under
    `.claude/artifacts/issue-refinement-loop/<N>/` survive (PR #2135
    iteration-3 P2-6 closes the prior bug where `_cmd_produce()` had no
    `finally` cleanup and left at least 4 files under `tmp/` per run)."""
    import argparse
    import json as _json

    fixture = _json.loads((FIXTURES_DIR / "review_result_approve.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)

    def _fake_fetch(issue_number, repo, timeout_seconds=15):
        return "body", "sha256:" + "b" * 64, None

    def _fake_check(body_file, timeout_seconds=30):
        return {"schema": "REVIEW_ISSUE_RESULT_V1"}, 0, None

    def _fake_readiness(body_file, mode="execute", timeout_seconds=60):
        return {"errors": []}, 0, None

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch)
    monkeypatch.setattr(_PIPELINE, "run_check_issue_contract", _fake_check)
    monkeypatch.setattr(_PIPELINE, "run_contract_readiness_check", _fake_readiness)
    monkeypatch.setattr(_PIPELINE, "run_merge_readiness", lambda **kwargs: (dict(fixture), 0, None))

    args = argparse.Namespace(issue_number=2049, repo=TRUSTED_REPO)
    rc = _PIPELINE._cmd_produce(args)
    assert rc == 0

    tmp_root = tmp_path / "tmp"
    leftovers = list(tmp_root.rglob("*")) if tmp_root.exists() else []
    assert leftovers == [], f"expected no leftover intermediate files, found: {leftovers}"

    artifacts_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / "2049"
    persisted = sorted(p.name for p in artifacts_dir.glob("*"))
    assert any(name.startswith("root_review_pipeline_") for name in persisted)
    assert any((artifacts_dir / name).is_dir() for name in persisted), (
        "expected an invocation-id subdirectory holding the persisted "
        f"ISSUE_REVIEW_RESULT_COMPACT_V2 artifact, found: {persisted}"
    )

    out = _json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert "compact_result" in out
    assert "body_file" not in out




# ---------------------------------------------------------------------------
# Issue #2054 PR #2142 owner REQUEST_CHANGES P0-2: classify_child_stdout()
# wire<->artifact cross-check. A grammatically valid but tampered wire
# (ARTIFACT/ARTIFACT_SHA256 unchanged, VERDICT/BLOCKERS/NEXT_ACTION rewritten)
# must be classified integrity_failure, not silently accepted.
# ---------------------------------------------------------------------------


def test_classify_child_stdout_with_artifact_context_accepts_genuine_relay(tmp_path):
    import reviewer_transport as _transport

    relative, digest = _transport.write_semantic_artifact(
        artifact_root=tmp_path, issue_number=2049, repo=TRUSTED_REPO,
        invocation_id="classify-genuine", attempt=1, reviewed_body_sha256="sha256:" + "a" * 64,
        semantic_result={"verdict": "approve", "blocking_issues": []},
    )
    wire = _transport.build_compact_v2(
        verdict="approve", summary="contract ready", blockers=0,
        reviewed_body_sha256="sha256:" + "a" * 64, attempt_id="classify-genuine",
        artifact_relative=relative, artifact_sha256=digest,
    ).decode("utf-8")
    result = _PIPELINE.classify_child_stdout(
        wire, issue_number=2049, artifact_root=tmp_path, repo=TRUSTED_REPO,
        expected_body_sha256="sha256:" + "a" * 64,
    )
    assert result == {"classification": "ok", "code": None, "retryable": False}


def test_classify_child_stdout_with_artifact_context_rejects_tampered_relay(tmp_path):
    import reviewer_transport as _transport

    relative, digest = _transport.write_semantic_artifact(
        artifact_root=tmp_path, issue_number=2049, repo=TRUSTED_REPO,
        invocation_id="classify-tampered", attempt=1, reviewed_body_sha256="sha256:" + "b" * 64,
        semantic_result={"verdict": "needs-fix", "blocking_issues": [{"code": "C1"}]},
    )
    # ARTIFACT / ARTIFACT_SHA256 point at the real, hash-verifiable artifact
    # above, but VERDICT/BLOCKERS/NEXT_ACTION are self-consistently rewritten
    # to disagree with that artifact's actual semantic_result.
    tampered_wire = _transport.build_compact_v2(
        verdict="approve", summary="contract ready", blockers=0,
        reviewed_body_sha256="sha256:" + "b" * 64, attempt_id="classify-tampered",
        artifact_relative=relative, artifact_sha256=digest,
    ).decode("utf-8")
    result = _PIPELINE.classify_child_stdout(
        tampered_wire, issue_number=2049, artifact_root=tmp_path, repo=TRUSTED_REPO,
        expected_body_sha256="sha256:" + "b" * 64,
    )
    assert result["classification"] == "integrity_failure"
    assert result["retryable"] is False


def test_retry_once_on_transport_failure_never_retries_integrity_failure(tmp_path):
    import reviewer_transport as _transport

    relative, digest = _transport.write_semantic_artifact(
        artifact_root=tmp_path, issue_number=2049, repo=TRUSTED_REPO,
        invocation_id="retry-tampered", attempt=1, reviewed_body_sha256="sha256:" + "c" * 64,
        semantic_result={"verdict": "needs-fix", "blocking_issues": [{"code": "C1"}]},
    )
    tampered_wire = _transport.build_compact_v2(
        verdict="approve", summary="contract ready", blockers=0,
        reviewed_body_sha256="sha256:" + "c" * 64, attempt_id="retry-tampered",
        artifact_relative=relative, artifact_sha256=digest,
    ).decode("utf-8")
    calls = {"n": 0}

    def invoke_child():
        calls["n"] += 1
        return tampered_wire

    result = _PIPELINE.retry_once_on_transport_failure(
        invoke_child, issue_number=2049, artifact_root=tmp_path, repo=TRUSTED_REPO,
        expected_body_sha256="sha256:" + "c" * 64,
    )
    assert calls["n"] == 1, "integrity_failure must not be retried (a misbehaving relay will not self-heal)"
    assert result["status"] == "integrity_failure"
