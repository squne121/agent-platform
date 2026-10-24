"""Issue #2242 -- regression coverage for the writer/reader V2 SSOT drift, and
for the OWNER adversarial review fix_delta
(source-repo PR #2246 comment 5328161000)
that closed 4 further blockers on top of the original SSOT fix:

Blocker 1 (artifact-type conflation): a genuine E2E test that invokes the
REAL `produce` CLI/command (not just `write_semantic_artifact()` directly)
with a production-shaped `semantic_result`, and feeds `produce`'s own
`compact_result.artifact_path` (the canonical `readback`/`gate-final-review`
input) into `readback`/`gate-final-review`, asserting success.

Blocker 2 (AC4 binding verification was self-referential/tautological):
`readback_persisted_artifact()` now requires the CALLER to supply every
expected binding value independently (repo/issue/invocation/attempt/
artifact-sha256), and `gate-final-review`'s CLI now exposes
`--expected-repo`/`--expected-issue`/`--expected-invocation-id`/
`--expected-attempt`/`--expected-artifact-sha256` so each can be
independently mutated in a negative test.

Blocker 3 (custom insecure symlink-following file open): the custom
`os.open(..., O_NOFOLLOW)` + read loop is deleted; `readback_persisted_artifact()`
fully delegates to `reviewer_transport.verify_artifact()`. This file adds
negative coverage for intermediate-directory symlink, root escape, FIFO,
directory-as-artifact, oversize, raw-byte mutation, and artifact-path
substitution.

Blocker 4 (bool-as-int / empty-string / non-positive binding fields pass
unchecked): `reviewer_transport.extract_binding_context()` now uses exact
`type(x) is int` checks + positive-value constraints + canonical
owner/repo / invocation-id / body-sha256 format checks, and
`check_artifact_binding()` itself rejects a bool-typed `issue_number`/
`attempt` at the comparison layer. `semantic_result` is now validated
against the FULL `REVIEW_ISSUE_RESULT_V1` jsonschema
(`reviewer_transport.validate_semantic_result_schema()`), not merely
`verdict`/`blocking_issues` presence.

The ORIGINAL #2242 regression this module first covered:
`readback_persisted_artifact()` (`run_root_review_pipeline.py`) used to parse
persisted JSON itself and read `payload.get("body_sha256")` /
`payload.get("verdict")` as TOP-LEVEL keys.  The canonical V2 writer
(`reviewer_transport.write_semantic_artifact()`) has never persisted those
keys: body binding lives at the top-level `reviewed_body_sha256` field and
`verdict` lives INSIDE the nested `semantic_result` dict.  Every fresh V2
artifact therefore always read back as `None` for both fields, and
`gate_final_review()` was permanently `body_sha256_mismatch` /
`verdict_mismatch` (#2231 issuecomment-5315427655 / issuecomment-5316915493).

This module proves the FULL producer -> readback -> gate roundtrip against
the REAL canonical writer (never a hand-written payload the test reshapes to
match), both in-process and via the actual CLI subcommands, and pins the
#2054/PR #2142 SSOT contract that `reviewer_transport.py` remains the sole
owner of this schema.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
FIXTURES_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "fixtures"
PIPELINE_SCRIPT = SCRIPTS_DIR / "run_root_review_pipeline.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import reviewer_transport as transport  # noqa: E402
from _trusted_repo import TRUSTED_REPO  # noqa: E402


def _load_pipeline_module():
    spec = importlib.util.spec_from_file_location("run_root_review_pipeline_v2_ssot", PIPELINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_root_review_pipeline_v2_ssot", module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_pipeline_module()

_REPO = TRUSTED_REPO
_ISSUE = 2242
_BODY_SHA256 = "sha256:" + "c" * 64


def _approve_fixture() -> dict:
    return json.loads((FIXTURES_DIR / "review_result_approve.json").read_text(encoding="utf-8"))


def _needs_fix_fixture() -> dict:
    return json.loads((FIXTURES_DIR / "review_result_needs_fix.json").read_text(encoding="utf-8"))


def _write_artifact(
    tmp_path: Path, *, invocation_id: str, attempt: int, semantic_result: dict, body_sha256: str
) -> tuple[str, str]:
    return transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=_ISSUE,
        repo=_REPO,
        invocation_id=invocation_id,
        attempt=attempt,
        reviewed_body_sha256=body_sha256,
        semantic_result=semantic_result,
    )


# ---------------------------------------------------------------------------
# AC1 / Required regression test: canonical producer -> persisted V2
# artifact -> readback_persisted_artifact() -> gate_final_review() reaches
# final_review_allowed: true for a fresh "approve" artifact, with NO
# test-side payload reshaping (the exact bytes write_semantic_artifact()
# wrote are the exact bytes readback_persisted_artifact() consumes), using a
# COMPLETE production-shaped REVIEW_ISSUE_RESULT_V1 semantic_result (Issue
# #2242 OWNER Blocker 4: full schema, not merely verdict+blocking_issues).
# ---------------------------------------------------------------------------


def test_given_canonical_approve_artifact_when_full_roundtrip_then_final_review_allowed(tmp_path: Path):
    fixture = _approve_fixture()
    fixture["body_sha256"] = _BODY_SHA256
    relative, artifact_sha256 = _write_artifact(
        tmp_path, invocation_id="roundtrip-approve", attempt=1, semantic_result=fixture, body_sha256=_BODY_SHA256
    )

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="roundtrip-approve",
        expected_attempt=1,
        expected_artifact_sha256=artifact_sha256,
        expected_verdict="approve",
    )
    assert readback["violations"] == []
    assert readback["verdict_identity"] is True

    gate = _PIPELINE.gate_final_review(remote_update_ok=True, readback=readback)
    assert gate == {"final_review_allowed": True, "reasons": []}


# ---------------------------------------------------------------------------
# AC2: "needs-fix" artifact also correctly verifies expected verdict identity.
# ---------------------------------------------------------------------------


def test_given_canonical_needs_fix_artifact_when_full_roundtrip_then_final_review_allowed(tmp_path: Path):
    fixture = _needs_fix_fixture()
    fixture["body_sha256"] = _BODY_SHA256
    relative, artifact_sha256 = _write_artifact(
        tmp_path, invocation_id="roundtrip-needs-fix", attempt=1, semantic_result=fixture, body_sha256=_BODY_SHA256
    )

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="roundtrip-needs-fix",
        expected_attempt=1,
        expected_artifact_sha256=artifact_sha256,
        expected_verdict="needs-fix",
    )
    assert readback["violations"] == []
    assert readback["verdict_identity"] is True

    gate = _PIPELINE.gate_final_review(remote_update_ok=True, readback=readback)
    assert gate == {"final_review_allowed": True, "reasons": []}


# ---------------------------------------------------------------------------
# AC3: wrong reviewed_body_sha256 -> fail-closed (Issue #2242 Blocker 2: the
# violation code is now "artifact_binding_mismatch", the SAME single
# fail-closed reason reviewer_transport.check_artifact_binding() reports for
# ANY binding-field mismatch, not a body-sha-specific string -- every
# expected_* value is now independently caller-supplied, not derived from
# the artifact, so there is no longer a structural reason for body_sha256 to
# be the only field that could disagree).
# ---------------------------------------------------------------------------


def test_given_wrong_expected_body_sha256_when_readback_then_fail_closed(tmp_path: Path):
    fixture = _approve_fixture()
    fixture["body_sha256"] = _BODY_SHA256
    relative, artifact_sha256 = _write_artifact(
        tmp_path, invocation_id="wrong-body-sha", attempt=1, semantic_result=fixture, body_sha256=_BODY_SHA256
    )

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256="sha256:" + "0" * 64,
        expected_invocation_id="wrong-body-sha",
        expected_attempt=1,
        expected_artifact_sha256=artifact_sha256,
        expected_verdict="approve",
    )
    assert readback["verdict_identity"] is False
    assert "artifact_binding_mismatch" in readback["violations"]

    gate = _PIPELINE.gate_final_review(remote_update_ok=True, readback=readback)
    assert gate["final_review_allowed"] is False
    assert "artifact_binding_mismatch" in gate["reasons"]


# ---------------------------------------------------------------------------
# AC4 (Issue #2242 Blocker 2 rewrite): each of expected-repo / expected-issue
# / expected-invocation-id / expected-attempt / expected-artifact-sha256 is
# independently mutated/wrong while everything else stays correct, via the
# ACTUAL PUBLIC `gate-final-review` CLI (not `verify_artifact()` called
# directly) -- this is a real regression test for readback_persisted_artifact()
# / the gate-final-review CLI's wiring, not just for the unchanged
# verify_artifact() primitive it delegates to.
# ---------------------------------------------------------------------------


def _run_gate_final_review_cli(
    *,
    artifact_root: Path,
    artifact_relative: str,
    expected_repo: str,
    expected_issue: int,
    expected_body_sha256: str,
    expected_invocation_id: str,
    expected_attempt: int,
    expected_artifact_sha256: str,
    expected_verdict: str,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(PIPELINE_SCRIPT),
            "gate-final-review",
            "--artifact-root",
            str(artifact_root),
            "--artifact-relative",
            artifact_relative,
            "--expected-repo",
            expected_repo,
            "--expected-issue",
            str(expected_issue),
            "--expected-body-sha256",
            expected_body_sha256,
            "--expected-invocation-id",
            expected_invocation_id,
            "--expected-attempt",
            str(expected_attempt),
            "--expected-artifact-sha256",
            expected_artifact_sha256,
            "--expected-verdict",
            expected_verdict,
            "--remote-update-ok",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "mutate_key,mutate_value",
    [
        ("expected_repo", "other/repo"),
        ("expected_issue", 999999),
        ("expected_invocation_id", "some-other-invocation"),
        ("expected_attempt", 2),
        ("expected_artifact_sha256", "sha256:" + "0" * 64),
    ],
)
def test_given_wrong_binding_field_when_gate_final_review_cli_invoked_then_fail_closed(
    tmp_path: Path, mutate_key: str, mutate_value
):
    fixture = _approve_fixture()
    fixture["body_sha256"] = _BODY_SHA256
    relative, artifact_sha256 = _write_artifact(
        tmp_path, invocation_id="wrong-binding-cli", attempt=1, semantic_result=fixture, body_sha256=_BODY_SHA256
    )
    base_kwargs = dict(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="wrong-binding-cli",
        expected_attempt=1,
        expected_artifact_sha256=artifact_sha256,
        expected_verdict="approve",
    )

    # Sanity: the unmutated kwargs actually succeed (proves the mutation
    # below is the sole cause of failure, not a fixture bug).
    genuine = _run_gate_final_review_cli(**base_kwargs)
    assert genuine.returncode == 0, genuine.stderr
    assert json.loads(genuine.stdout) == {"final_review_allowed": True, "reasons": []}

    mutated_kwargs = {**base_kwargs, mutate_key: mutate_value}
    mutated = _run_gate_final_review_cli(**mutated_kwargs)
    assert mutated.returncode == 1, mutated.stdout
    payload = json.loads(mutated.stdout)
    assert payload["final_review_allowed"] is False


def test_given_wrong_binding_fields_when_verify_artifact_then_fail_closed(tmp_path: Path):
    """Original AC4 coverage retained: the underlying primitive
    `reviewer_transport.verify_artifact()` itself independently rejects each
    wrong binding field (this is a DIFFERENT layer than the CLI test above,
    which proves the CLI/`readback_persisted_artifact()` wiring actually
    reaches this primitive with caller-supplied, not self-derived, expected
    values)."""
    relative, digest = _write_artifact(
        tmp_path,
        invocation_id="wrong-binding",
        attempt=1,
        semantic_result={"verdict": "approve", "blocking_issues": []},
        body_sha256=_BODY_SHA256,
    )
    base_kwargs = dict(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="wrong-binding",
        expected_attempt=1,
        expected_sha256=digest,
    )
    assert transport.verify_artifact(**base_kwargs)["status"] == "valid"

    assert transport.verify_artifact(**{**base_kwargs, "expected_repo": "other/repo"})["status"] == "integrity_failure"
    assert transport.verify_artifact(**{**base_kwargs, "expected_issue": 1})["status"] == "integrity_failure"
    assert (
        transport.verify_artifact(**{**base_kwargs, "expected_invocation_id": "other-invocation"})["status"]
        == "integrity_failure"
    )
    assert transport.verify_artifact(**{**base_kwargs, "expected_attempt": 2})["status"] == "integrity_failure"
    assert (
        transport.verify_artifact(**{**base_kwargs, "expected_sha256": "sha256:" + "0" * 64})["status"]
        == "integrity_failure"
    )


# ---------------------------------------------------------------------------
# AC5: artifact semantic_result vs compact wire tampering mismatch ->
# fail-closed via verify_wire_matches_artifact().
# ---------------------------------------------------------------------------


def test_given_tampered_compact_wire_when_cross_checked_against_artifact_then_fail_closed(tmp_path: Path):
    relative, digest = _write_artifact(
        tmp_path,
        invocation_id="wire-tamper",
        attempt=1,
        semantic_result={"verdict": "needs-fix", "blocking_issues": [{"code": "C1"}]},
        body_sha256=_BODY_SHA256,
    )
    verified = transport.verify_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="wire-tamper",
        expected_attempt=1,
        expected_sha256=digest,
    )
    assert verified["status"] == "valid"

    tampered_wire = transport.build_compact_v2(
        verdict="approve",
        summary="contract ready",
        blockers=0,
        reviewed_body_sha256=_BODY_SHA256,
        attempt_id="wire-tamper",
        artifact_relative=relative,
        artifact_sha256=digest,
    )
    cross = transport.verify_wire_matches_artifact(
        wire=tampered_wire, verified_artifact=verified, artifact_relative=relative, artifact_sha256=digest
    )
    assert cross["status"] == "integrity_failure"
    assert cross["reason_code"] == "wire_artifact_semantic_mismatch"

    genuine_wire = transport.project_compact_v2_from_artifact(
        verified["payload"], attempt_id="wire-tamper", artifact_relative=relative, artifact_sha256=digest
    )
    genuine_cross = transport.verify_wire_matches_artifact(
        wire=genuine_wire, verified_artifact=verified, artifact_relative=relative, artifact_sha256=digest
    )
    assert genuine_cross["status"] == "valid"


# ---------------------------------------------------------------------------
# AC6: existing malformed JSON / duplicate key / non-finite JSON / symlink /
# non-regular-file security regressions still fail-closed (this module's
# OWN symlink / non-regular-file coverage; the malformed-JSON/duplicate-key
# fixtures themselves live in test_issue_reviewer_contract_static.py, updated
# to production V2 shape by this same Issue), PLUS Issue #2242 OWNER Blocker
# 3 negative fixtures: intermediate-directory symlink, root escape, FIFO,
# directory-as-artifact, oversize, raw-byte mutation, artifact-path
# substitution.
# ---------------------------------------------------------------------------


def test_given_symlinked_artifact_path_when_readback_then_rejected_as_not_regular_file(tmp_path: Path):
    relative, artifact_sha256 = _write_artifact(
        tmp_path,
        invocation_id="symlink-target",
        attempt=1,
        semantic_result={"verdict": "approve", "blocking_issues": []},
        body_sha256=_BODY_SHA256,
    )
    real_path = tmp_path / relative
    symlink_relative = "artifact_symlink.json"
    (tmp_path / symlink_relative).symlink_to(real_path)

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=symlink_relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="symlink-target",
        expected_attempt=1,
        expected_artifact_sha256=artifact_sha256,
        expected_verdict="approve",
    )
    assert readback["verdict_identity"] is False
    assert "artifact_not_regular_file" in readback["violations"]


def test_given_intermediate_directory_symlink_when_readback_then_rejected(tmp_path: Path):
    """Issue #2242 OWNER Blocker 3: `O_NOFOLLOW` on a single flat
    `os.open()` only rejects a symlink at the LEAF -- a symlinked
    INTERMEDIATE directory can smuggle in an out-of-root regular file. Full
    delegation to `verify_artifact()` (dir-fd anchored, no-follow on EVERY
    component) must reject this."""
    outside_dir = tmp_path.parent / f"{tmp_path.name}_outside_target"
    outside_dir.mkdir(exist_ok=True)
    real_relative, artifact_sha256 = transport.write_semantic_artifact(
        artifact_root=outside_dir,
        issue_number=_ISSUE,
        repo=_REPO,
        invocation_id="outside-target",
        attempt=1,
        reviewed_body_sha256=_BODY_SHA256,
        semantic_result={"verdict": "approve", "blocking_issues": []},
    )

    linkdir = tmp_path / "linkdir"
    linkdir.symlink_to(outside_dir, target_is_directory=True)
    smuggled_relative = f"linkdir/{real_relative}"

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=smuggled_relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="outside-target",
        expected_attempt=1,
        expected_artifact_sha256=artifact_sha256,
        expected_verdict="approve",
    )
    assert readback["verdict_identity"] is False
    assert readback["violations"] == ["artifact_not_regular_file"]


def test_given_root_escaping_relative_path_when_readback_then_rejected(tmp_path: Path):
    """Issue #2242 OWNER Blocker 3: a `../` (or absolute-path) escape from
    `artifact_root` must be rejected, not silently resolved outside root."""
    escape_relative = "../etc/passwd"
    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=escape_relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="escape",
        expected_attempt=1,
        expected_artifact_sha256="sha256:" + "0" * 64,
        expected_verdict="approve",
    )
    assert readback["verdict_identity"] is False
    assert readback["violations"] == ["artifact_not_regular_file"]

    absolute_relative = str((tmp_path.parent / "outside.json").resolve())
    readback_abs = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=absolute_relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="escape",
        expected_attempt=1,
        expected_artifact_sha256="sha256:" + "0" * 64,
        expected_verdict="approve",
    )
    assert readback_abs["verdict_identity"] is False
    assert readback_abs["violations"] == ["artifact_not_regular_file"]


def test_given_fifo_substituted_for_artifact_when_readback_then_rejected(tmp_path: Path):
    """Issue #2242 OWNER Blocker 3: a FIFO (named pipe) at the artifact path
    must be rejected as non-regular, not read/blocked-on."""
    fifo_relative = "fifo_artifact.json"
    os.mkfifo(str(tmp_path / fifo_relative))
    try:
        readback = _PIPELINE.readback_persisted_artifact(
            artifact_root=tmp_path,
            artifact_relative=fifo_relative,
            expected_repo=_REPO,
            expected_issue=_ISSUE,
            expected_body_sha256=_BODY_SHA256,
            expected_invocation_id="fifo",
            expected_attempt=1,
            expected_artifact_sha256="sha256:" + "0" * 64,
            expected_verdict="approve",
        )
        assert readback["verdict_identity"] is False
        assert readback["violations"] == ["artifact_not_regular_file"]
    finally:
        (tmp_path / fifo_relative).unlink(missing_ok=True)


def test_given_directory_substituted_for_artifact_when_readback_then_rejected(tmp_path: Path):
    """Issue #2242 OWNER Blocker 3: a directory at the artifact leaf path
    must be rejected, not silently treated as empty/malformed content."""
    dir_relative = "dir_artifact.json"
    (tmp_path / dir_relative).mkdir()

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=dir_relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="dir",
        expected_attempt=1,
        expected_artifact_sha256="sha256:" + "0" * 64,
        expected_verdict="approve",
    )
    assert readback["verdict_identity"] is False
    assert readback["violations"] == ["artifact_not_regular_file"]


def test_given_oversize_artifact_when_readback_then_rejected(tmp_path: Path):
    """Issue #2242 OWNER Blocker 3: an artifact exceeding
    `reviewer_transport.ARTIFACT_MAX_BYTES` (1 MiB) must be rejected."""
    oversize_relative = "oversize_artifact.json"
    payload_text = json.dumps(
        {"schema": transport.ARTIFACT_SCHEMA, "padding": "x" * (transport.ARTIFACT_MAX_BYTES + 1)}
    )
    (tmp_path / oversize_relative).write_text(payload_text, encoding="utf-8")
    assert len(payload_text.encode("utf-8")) > transport.ARTIFACT_MAX_BYTES

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=oversize_relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="oversize",
        expected_attempt=1,
        expected_artifact_sha256="sha256:" + "0" * 64,
        expected_verdict="approve",
    )
    assert readback["verdict_identity"] is False
    assert readback["violations"] == ["artifact_not_regular_file"]


def test_given_raw_byte_mutation_after_write_when_readback_then_sha256_mismatch(tmp_path: Path):
    """Issue #2242 OWNER Blocker 3: mutating a single byte of the persisted
    artifact after write (before verify) must be caught by the raw-byte
    SHA-256 check, never silently accepted."""
    relative, artifact_sha256 = _write_artifact(
        tmp_path,
        invocation_id="byte-mutate",
        attempt=1,
        semantic_result={"verdict": "approve", "blocking_issues": []},
        body_sha256=_BODY_SHA256,
    )
    artifact_path = tmp_path / relative
    raw = bytearray(artifact_path.read_bytes())
    # Flip one byte inside a JSON string value (avoid corrupting JSON
    # structure so the mutation is caught by the hash check, not an earlier
    # strict-JSON parse failure -- this specifically pins the SHA-256 gate).
    mutate_index = raw.index(b"squne121")
    raw[mutate_index] = ord("X")
    artifact_path.write_bytes(bytes(raw))

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="byte-mutate",
        expected_attempt=1,
        expected_artifact_sha256=artifact_sha256,
        expected_verdict="approve",
    )
    assert readback["verdict_identity"] is False
    assert readback["violations"] == ["artifact_sha256_mismatch"]


def test_given_artifact_path_substitution_when_readback_then_rejected(tmp_path: Path):
    """Issue #2242 OWNER Blocker 3: verifying against a DIFFERENT but
    validly-formed artifact at another relative path (a substitution/
    confused-deputy attempt) must be rejected by the SHA-256 + binding
    checks, not silently accepted because both artifacts individually
    parse."""
    genuine_relative, genuine_sha256 = _write_artifact(
        tmp_path,
        invocation_id="substitution-genuine",
        attempt=1,
        semantic_result={"verdict": "approve", "blocking_issues": []},
        body_sha256=_BODY_SHA256,
    )
    other_relative, other_sha256 = _write_artifact(
        tmp_path,
        invocation_id="substitution-other",
        attempt=1,
        semantic_result={"verdict": "needs-fix", "blocking_issues": [{"code": "C1"}]},
        body_sha256=_BODY_SHA256,
    )
    assert genuine_relative != other_relative

    # Verify the OTHER artifact's bytes/relative path against the GENUINE
    # artifact's expected identity -> must fail (both the raw-byte SHA and
    # the invocation_id binding disagree).
    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=other_relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="substitution-genuine",
        expected_attempt=1,
        expected_artifact_sha256=genuine_sha256,
        expected_verdict="approve",
    )
    assert readback["verdict_identity"] is False
    assert readback["violations"] == ["artifact_sha256_mismatch"]
    assert other_sha256 != genuine_sha256


def test_given_duplicate_json_key_when_readback_then_rejected_as_not_strict_json(tmp_path: Path):
    artifact_relative = "dup_key_artifact.json"
    (tmp_path / artifact_relative).write_text(
        '{"schema": "REVIEWER_COMPACT_ARTIFACT_V2", "repository": "example-org/example-repo", '
        '"issue_number": 2242, "reviewed_body_sha256": "sha256:aa", "reviewed_body_sha256": "sha256:bb", '
        '"invocation_id": "dup-key", "attempt": 1, '
        '"semantic_result": {"verdict": "approve", "blocking_issues": []}}',
        encoding="utf-8",
    )
    raw = (tmp_path / artifact_relative).read_bytes()
    actual_sha256 = transport.sha256_prefixed(raw)
    readback = _PIPELINE.readback_persisted_artifact(
        artifact_root=tmp_path,
        artifact_relative=artifact_relative,
        expected_repo=TRUSTED_REPO,
        expected_issue=2242,
        expected_body_sha256="sha256:aa",
        expected_invocation_id="dup-key",
        expected_attempt=1,
        expected_artifact_sha256=actual_sha256,
        expected_verdict="approve",
    )
    assert readback["verdict_identity"] is False
    assert "artifact_not_strict_json" in readback["violations"]


# ---------------------------------------------------------------------------
# AC7: run_root_review_pipeline.py must not maintain a second independent
# field-map of the canonical V2 persisted artifact layout, and (Issue #2242
# Blocker 2/3 rewrite) must fully delegate open/read/hash/binding to
# reviewer_transport.verify_artifact() rather than deriving "expected"
# binding values from the artifact it is verifying, or reimplementing the
# open/read primitive itself.
# ---------------------------------------------------------------------------


def test_pipeline_source_delegates_v2_field_access_to_reviewer_transport():
    """GIVEN run_root_review_pipeline.py's source
    WHEN scanned for direct top-level flat-V1-key access on a readback
    payload, and for full delegation of the artifact open/read/binding
    primitive
    THEN it no longer reads `payload.get("body_sha256")` /
    `payload.get("verdict")` directly (the original #2242 regression this
    Issue fixes), calls `reviewer_transport.verify_artifact()` (Issue #2242
    Blocker 2/3: full delegation, caller-supplied expected binding values),
    `reviewer_transport.validate_semantic_result_schema()` (Blocker 4: full
    REVIEW_ISSUE_RESULT_V1 schema validation), and
    `reviewer_transport.semantic_verdict_and_count()`, and no longer
    implements its own `os.open(..., O_NOFOLLOW)` call (Blocker 3: the
    custom leaf-only no-follow open is deleted, not reimplemented)."""
    source = PIPELINE_SCRIPT.read_text(encoding="utf-8")
    assert 'payload.get("body_sha256")' not in source
    assert 'payload.get("verdict")' not in source
    assert "_reviewer_transport.verify_artifact(" in source
    assert "_reviewer_transport.validate_semantic_result_schema(" in source
    assert "_reviewer_transport.semantic_verdict_and_count(" in source
    assert "def _open_readonly_no_follow" not in source


# ---------------------------------------------------------------------------
# AC8: #2054 AC8 / PR #2142 SSOT contract (reviewer_transport.py is the sole
# owner of artifact schema) pinned by an explicit regression test.
# ---------------------------------------------------------------------------


def test_reviewer_transport_owns_artifact_schema_constant_and_accessors():
    """GIVEN reviewer_transport.py
    WHEN checked for the canonical artifact schema tag and its accessors
    THEN ARTIFACT_SCHEMA and the binding/verdict/schema-validation accessors
    this Issue's readback fix depends on all live in reviewer_transport.py
    (not reimplemented anywhere else), pinning the #2054/PR #2142 SSOT
    contract (extended by Issue #2242's Blocker 2/3/4 fix_delta:
    `verify_artifact` and `validate_semantic_result_schema` are now also
    part of this pinned surface)."""
    assert transport.ARTIFACT_SCHEMA == "REVIEWER_COMPACT_ARTIFACT_V2"
    assert callable(transport.extract_binding_context)
    assert callable(transport.check_artifact_binding)
    assert callable(transport.semantic_verdict_and_count)
    assert callable(transport.verify_artifact)
    assert callable(transport.validate_semantic_result_schema)

    pipeline_source = PIPELINE_SCRIPT.read_text(encoding="utf-8")
    assert 'ARTIFACT_SCHEMA = "REVIEWER_COMPACT_ARTIFACT_V2"' not in pipeline_source


# ---------------------------------------------------------------------------
# AC9: gate-final-review CLI E2E test reaches final_review_allowed: true for
# an artifact generated by the canonical producer, using the new
# --artifact-root/--artifact-relative/--expected-* CLI surface.
# ---------------------------------------------------------------------------


def test_given_canonical_artifact_when_gate_final_review_cli_invoked_then_final_review_allowed(tmp_path: Path):
    fixture = _approve_fixture()
    fixture["body_sha256"] = _BODY_SHA256
    relative, artifact_sha256 = _write_artifact(
        tmp_path, invocation_id="cli-e2e", attempt=1, semantic_result=fixture, body_sha256=_BODY_SHA256
    )

    readback_proc = subprocess.run(
        [
            sys.executable,
            str(PIPELINE_SCRIPT),
            "readback",
            "--artifact-root",
            str(tmp_path),
            "--artifact-relative",
            relative,
            "--expected-repo",
            _REPO,
            "--expected-issue",
            str(_ISSUE),
            "--expected-body-sha256",
            _BODY_SHA256,
            "--expected-invocation-id",
            "cli-e2e",
            "--expected-attempt",
            "1",
            "--expected-artifact-sha256",
            artifact_sha256,
            "--expected-verdict",
            "approve",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert readback_proc.returncode == 0, readback_proc.stderr
    readback_payload = json.loads(readback_proc.stdout)
    assert readback_payload["verdict_identity"] is True

    gate_proc = _run_gate_final_review_cli(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="cli-e2e",
        expected_attempt=1,
        expected_artifact_sha256=artifact_sha256,
        expected_verdict="approve",
    )
    assert gate_proc.returncode == 0, gate_proc.stderr
    gate_payload = json.loads(gate_proc.stdout)
    assert gate_payload == {"final_review_allowed": True, "reasons": []}


# ---------------------------------------------------------------------------
# AC10 (Issue #2242 OWNER Blocker 1): a genuine E2E test that invokes the
# REAL `produce` CLI/command (`_cmd_produce()`, not just
# `write_semantic_artifact()` directly) with a production-shaped
# `semantic_result` (via the REAL checker chain, not a hand-written fixture
# `_cmd_produce` merely echoes), captures the real JSON output, extracts the
# canonical `compact_result.artifact_path` (== `verified_transport_artifact`)
# this Issue's fix defines as the canonical gate-final-review input, and
# feeds it into `readback`/`gate-final-review`, asserting success.
# ---------------------------------------------------------------------------

_MINIMAL_VALID_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: docs
parent_issue: none
goal_ref: "Issue #2242 fix_delta E2E fixture"
change_kind: docs
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test (Issue
#2242 OWNER Blocker 1, PR #2246 fix_delta).

## Acceptance Criteria

- [ ] AC1: fixture body is well-formed enough for check_issue_contract.py to
      synthesize a complete REVIEW_ISSUE_RESULT_V1.

## Verification Commands

```bash
# AC1
$ rg --version
```

## Allowed Paths

- fixture/e2e_produce_path.md
"""


def test_given_real_produce_cli_invocation_when_artifact_fed_to_gate_final_review_then_allowed(
    tmp_path, monkeypatch, capsys
):
    """GIVEN a REAL `_cmd_produce()` invocation (only `fetch_and_pin_live_body`
    is monkeypatched, to avoid a live `gh` dependency in this always-run
    regression test -- every downstream step, including the checker
    subprocess chain `run-checker-attempt` spawns via
    `reviewer_transport.run_reviewer_transport()`, runs for REAL against a
    well-formed fixture Issue body)
    WHEN its real stdout JSON's `compact_result.artifact_path` /
    `verified_transport_artifact` is extracted and fed into `readback`/
    `gate-final-review`
    THEN both succeed against the REAL persisted artifact -- proving the
    full producer roundtrip end to end, not a shortcut that only exercises
    `write_semantic_artifact()` in isolation (Issue #2242 OWNER Blocker 1)."""
    import argparse

    body = _MINIMAL_VALID_BODY
    body_sha256 = transport.sha256_prefixed(body.encode("utf-8"))

    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)

    def _fake_fetch(issue_number, repo, timeout_seconds=15):
        return body, body_sha256, None

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch)

    args = argparse.Namespace(issue_number=2242, repo=_REPO)
    rc = _PIPELINE._cmd_produce(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["status"] == "ok"

    # Blocker 1 item 2: typed artifact-role separation is present and
    # self-consistent with compact_result.
    assert out["full_review_artifact"]["schema"] == "REVIEW_ISSUE_RESULT_V1"
    assert out["full_review_artifact"]["path"] == out["artifact_path"]
    assert out["verified_transport_artifact"]["schema"] == "REVIEWER_COMPACT_ARTIFACT_V2"
    vta = out["verified_transport_artifact"]
    assert str(Path(vta["root"]) / vta["relative_path"]) == out["compact_result"]["artifact_path"]

    # The merged semantic_result produced by the REAL checker chain is a
    # COMPLETE REVIEW_ISSUE_RESULT_V1 (not a hand-written {"verdict":...,
    # "blocking_issues":...} projection) -- proves the full schema
    # round-trips through the real pipeline.
    assert transport.validate_semantic_result_schema(out["merged_review_result"]) is None

    gate_proc = _run_gate_final_review_cli(
        artifact_root=Path(vta["root"]),
        artifact_relative=vta["relative_path"],
        expected_repo=_REPO,
        expected_issue=2242,
        expected_body_sha256=body_sha256,
        expected_invocation_id=vta["invocation_id"],
        expected_attempt=vta["attempt"],
        expected_artifact_sha256=vta["sha256"],
        expected_verdict=out["merged_review_result"]["verdict"],
    )
    assert gate_proc.returncode == 0, gate_proc.stderr
    assert json.loads(gate_proc.stdout) == {"final_review_allowed": True, "reasons": []}


# ---------------------------------------------------------------------------
# Issue #2242 OWNER Blocker 4: extract_binding_context() negative fixtures
# (bool-as-int, non-positive, empty-string, malformed-format binding
# fields), and check_artifact_binding()'s own bool-rejection at the
# comparison layer.
# ---------------------------------------------------------------------------


def _valid_binding_payload() -> dict:
    return {
        "schema": transport.ARTIFACT_SCHEMA,
        "repository": _REPO,
        "issue_number": _ISSUE,
        "reviewed_body_sha256": _BODY_SHA256,
        "invocation_id": "blocker4-fixture",
        "attempt": 1,
        "semantic_result": {"verdict": "approve", "blocking_issues": []},
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("issue_number", True),
        ("issue_number", False),
        ("attempt", True),
        ("attempt", False),
        ("issue_number", 0),
        ("attempt", 0),
        ("issue_number", -1),
        ("attempt", -1),
        ("repository", ""),
        ("invocation_id", ""),
        ("invocation_id", "   "),
    ],
)
def test_extract_binding_context_rejects_malformed_binding_field(field, value):
    payload = _valid_binding_payload()
    payload[field] = value
    assert transport.extract_binding_context(payload) is None


def test_extract_binding_context_rejects_malformed_reviewed_body_sha256():
    payload = _valid_binding_payload()
    payload["reviewed_body_sha256"] = "not-a-sha256"
    assert transport.extract_binding_context(payload) is None


def test_extract_binding_context_accepts_well_formed_payload():
    payload = _valid_binding_payload()
    assert transport.extract_binding_context(payload) == {
        "repository": _REPO,
        "issue_number": _ISSUE,
        "invocation_id": "blocker4-fixture",
        "attempt": 1,
    }


def test_check_artifact_binding_rejects_bool_typed_issue_number_and_attempt():
    """Issue #2242 OWNER Blocker 4: `payload.get(key) != value` alone is
    insecure for int-typed binding fields because `bool` is an `int`
    subclass in Python (`True == 1`). `check_artifact_binding()` must reject
    a bool-typed `issue_number`/`attempt` even when it is truthy-equal to
    the expected int."""
    payload = _valid_binding_payload()
    payload["issue_number"] = True  # would compare equal to expected_issue=1 under `!=`
    violation = transport.check_artifact_binding(
        payload,
        expected_repo=_REPO,
        expected_issue=1,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="blocker4-fixture",
        expected_attempt=1,
    )
    assert violation == "artifact_binding_mismatch"

    payload2 = _valid_binding_payload()
    payload2["attempt"] = True  # would compare equal to expected_attempt=1 under `!=`
    violation2 = transport.check_artifact_binding(
        payload2,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="blocker4-fixture",
        expected_attempt=True,  # even a bool-typed EXPECTED value must not silently succeed
    )
    assert violation2 == "artifact_binding_mismatch"


def test_validate_semantic_result_schema_rejects_minimal_projection():
    """Issue #2242 OWNER Blocker 4: a `{"verdict":..., "blocking_issues":...}`
    projection (the OLD, insufficiently-validated shape) must be rejected by
    the full REVIEW_ISSUE_RESULT_V1 schema validator."""
    assert transport.validate_semantic_result_schema({"verdict": "approve", "blocking_issues": []}) == (
        "semantic_result_schema_invalid"
    )
    assert transport.validate_semantic_result_schema(_approve_fixture()) is None
    assert transport.validate_semantic_result_schema(_needs_fix_fixture()) is None
