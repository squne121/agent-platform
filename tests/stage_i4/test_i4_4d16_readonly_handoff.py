"""I-4-4D16 (#118): single-PR read-only handoff, Step 4 -> dispatch_seq -> reviewer_verdict -> Step 5.

What this module proves (fixture level, no real GitHub, no model call, no SubAgent dispatch):
the EXISTING CLIs are wired together as documented, with positive and negative controls:

    extract-vc-metadata -> TEST_VERDICT_MACHINE/v2 -> CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 envelope
    -> step4-adjudicate -> dispatch_seq -> reviewer_verdict -> step5-terminal-gate

The envelopes come from the real ``ensure_contract_snapshot.py`` run against a stateful fake ``gh``
(reusing the harness of ``test_step4_contract_snapshot_handoff.py``). The fake ``gh`` world is the only
place a "post" ever happens in this file; the runtime acceptance procedure pinned in the manifest has
no GitHub-write flag.

What it does NOT prove: that a real test-runner / pr-reviewer SubAgent ran, that a trusted GO exists
for the live default branch tip, or that the Step 5 gate reached ``approved`` on a live PR. Those are
recorded by the main session outside this file; a fixture result is never a runtime PASS.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
IRL = ROOT / ".claude" / "skills" / "impl-review-loop"
CONSUMER = IRL / "scripts" / "adjudicate_vc_result.py"
HANDOFF_TESTS = IRL / "tests" / "test_step4_contract_snapshot_handoff.py"
MANIFEST = ROOT / "docs" / "dev" / "i4-4d16-readonly-handoff-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
THIS = "tests/stage_i4/test_i4_4d16_readonly_handoff.py"
D16_JOB = "i4-4d16-readonly-handoff"
D16_MANIFEST_REL = "docs/dev/i4-4d16-readonly-handoff-manifest.json"
WRITE_FLAGS = re.compile(
    r"--mode\s+auto\b|--post\b|gh\s+pr\s+(review|comment|edit|merge|ready|close)\b"
    r"|gh\s+issue\s+(comment|edit|close|create)\b|test_verdict\.publish|git\s+(push|commit)\b"
)

_spec = importlib.util.spec_from_file_location("handoff_harness_for_i4_4d16", HANDOFF_TESTS)
assert _spec is not None and _spec.loader is not None
h = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = h
_spec.loader.exec_module(h)

HEAD_A, HEAD_B = h.HEAD_A, h.HEAD_B
OTHER = "sha256:" + "7" * 64


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# --- manifest / CI / boundary ------------------------------------------------------------------


def test_manifest_shape_and_boundary_false() -> None:
    m = _manifest()
    assert m["schema"] == "I4_4D16_READONLY_HANDOFF_V1"
    assert m["issue"] == "squne121/agent-platform#118"
    assert m["tracker"] == "squne121/agent-platform#60"
    assert m["research"] == "squne121/agent-platform#117"
    assert m["test_files"] == [THIS]
    assert m["deferred_tests"] == []
    assert m["totals"]["skipped"] == m["totals"]["xfailed"] == m["totals"]["deselected"] == 0
    for key in m["boundary_false"]:
        assert m["evidence_boundary"][key] is False, key
    for required in (
        "impl_review_loop_auto_started",
        "verdict_published",
        "reviewer_github_write",
        "final_gate_wired",
        "controlled_pr_actor_enabled",
        "bot_broker_permission_changed",
        "test_verdict_publish_enabled",
        "merge_or_auto_merge",
    ):
        assert required in m["boundary_false"], required
    assert m["evidence_boundary"]["fixture_result_is_runtime_pass"] is False
    assert m["evidence_boundary"]["main_drift_facts_fabricated"] is False


def test_manifest_counts_match_collected_tests() -> None:
    m = _manifest()
    declared = m["expected_collect"][THIS]
    assert declared == m["expected_ran"][THIS] == m["totals"]["ran"] == m["totals"]["collected"]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", THIS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    collected = [ln for ln in completed.stdout.splitlines() if ln.startswith(THIS + "::")]
    assert len(collected) == declared


def test_runtime_acceptance_procedure_has_no_write_flag() -> None:
    procedure = _manifest()["runtime_acceptance"]["commands"]
    assert procedure, "the read-only acceptance procedure must be pinned"
    for command in procedure:
        assert not WRITE_FLAGS.search(command), command
    joined = "\n".join(procedure)
    assert "--mode check-only" in joined
    assert "step4-adjudicate" in joined and "step5-terminal-gate" in joined


@pytest.mark.parametrize(
    "bad",
    [
        "ensure_contract_snapshot.py --mode auto --post",
        "ensure_contract_snapshot.py --mode dry-run",
        "gh pr review 1 --approve",
        "gh issue comment 1 --body x",
        "materialize_test_verdict_artifact.py test_verdict.publish",
    ],
)
def test_negative_control_write_flag_detector(bad: str) -> None:
    if "dry-run" in bad:
        # dry-run materializes (runs Issue-authored VCs locally); it is excluded by the manifest rule
        # `forbidden_modes`, not by the write-flag regex.
        assert "dry-run" in _manifest()["runtime_acceptance"]["forbidden_modes"]
        return
    assert WRITE_FLAGS.search(bad)


def test_impl_review_loop_stays_disabled() -> None:
    text = (IRL / "SKILL.md").read_text(encoding="utf-8")
    front = yaml.safe_load(re.match(r"---\n(.*?)\n---\n", text, re.DOTALL).group(1))  # type: ignore[union-attr]
    assert front["disable-model-invocation"] is True


def test_ci_lane_uses_existing_runner_and_manifest() -> None:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    assert D16_JOB in jobs
    runs = "\n".join(step.get("run", "") for step in jobs[D16_JOB]["steps"])
    assert f"scripts/ci/run_stage1b1_tests.py {D16_MANIFEST_REL}" in runs
    assert "permissions" not in jobs[D16_JOB]
    assert not WRITE_FLAGS.search(runs)


# --- the chain (real CLIs, fake gh) --------------------------------------------------------------


@pytest.fixture(scope="module")
def chain(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    world = h.World(tmp_path_factory.mktemp("d16-chain") / "w")
    out: dict[str, Any] = {"world": world}
    out["human"] = world.run_producer("--mode", "check-only")
    out["dry"] = world.run_producer("--mode", "dry-run", "--evidence-mode", "baseline")
    # fixture-only: the "post" lands in the fake gh state, never on GitHub
    world.run_producer("--mode", "auto", "--post")
    out["existing"] = world.run_producer("--mode", "check-only")
    return out


def _step4(chain: dict[str, Any], key: str, **kw: Any) -> tuple[int, dict[str, Any], str]:
    w = chain["world"]
    rc, _env, art = chain[key]
    w.loop_state.unlink(missing_ok=True)
    kw.setdefault("producer_exit_code", str(rc))
    return w.run_consumer(art, **kw)


def _step5(
    chain: dict[str, Any],
    *,
    seq: int = 1,
    head: str = HEAD_A,
    body: str = h.BODY_SHA256,
    hashes: list[str] | None = None,
    reviewer: Any = None,
    live: Any = None,
    tag: str = "s5",
) -> tuple[int, dict[str, Any]]:
    w = chain["world"]
    rv = (
        reviewer
        if reviewer is not None
        else {"verdict": "APPROVE", "reviewed_head_sha": HEAD_A, "blockers": [], "warnings": []}
    )
    lm = (
        live
        if live is not None
        else {"head_sha": HEAD_A, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"}
    )
    argv = [
        sys.executable,
        str(CONSUMER),
        "step5-terminal-gate",
        "--loop-state-file",
        str(w.loop_state),
        "--reviewer-verdict-file",
        str(w.write_json(f"rv-{tag}.json", rv)),
        "--live-mergeability-file",
        str(w.write_json(f"lm-{tag}.json", lm)),
        "--expected-head-sha",
        head,
        "--expected-contract-body-sha256",
        body,
        "--expected-command-hashes-file",
        str(w.write_json(f"hh-{tag}.json", hashes or [h.COMMAND_HASH])),
        "--dispatch-seq",
        str(seq),
    ]
    c = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
    assert c.stdout.strip(), c.stderr
    return c.returncode, json.loads(c.stdout.strip().splitlines()[-1])


def test_extract_vc_metadata_feeds_the_binding(tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text(h.ISSUE_BODY, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(CONSUMER), "extract-vc-metadata", "--body-file", str(body)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    meta = json.loads(completed.stdout)
    assert meta["status"] == "ok" and meta["errors"] == []
    assert meta["command_hashes"] == [h.COMMAND_HASH]
    assert [c["ac"] for c in meta["commands"]] == ["AC1"]


def test_extract_vc_metadata_rejects_a_body_without_commands(tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text("## Outcome\n\nnothing\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(CONSUMER), "extract-vc-metadata", "--body-file", str(body)],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
    assert (
        completed.returncode != 0
        or payload.get("status") != "ok"
        or not payload.get("command_hashes")
    )


def test_producer_envelope_pairs(chain: dict[str, Any]) -> None:
    rc, env, _ = chain["human"]
    assert (rc, env["status"]) == (20, "human_judgment")
    rc, env, _ = chain["dry"]
    assert (rc, env["status"], env["source"]) == (20, "dry_run_would_post", "materialized_go")
    rc, env, _ = chain["existing"]
    assert (rc, env["status"], env["source"]) == (0, "ok", "existing_go")


def test_step4_invoke_on_dry_run_materialized_go_persists_seq(chain: dict[str, Any]) -> None:
    rc, payload, err = _step4(chain, "dry")
    assert rc == 0, (payload, err)
    assert payload["invoke_pr_reviewer"] is True and payload["seq"] == 1
    state = json.loads(chain["world"].loop_state.read_text(encoding="utf-8"))
    assert state["dispatch"] == {"binding_key": payload["binding_key"], "seq": 1}
    assert payload["binding_key"] in state["vc_adjudication"]


def test_step4_invoke_on_ok_existing_go_requires_repo_and_issue(chain: dict[str, Any]) -> None:
    rc, payload, err = _step4(chain, "existing", repo=h.REPO)
    assert rc == 0 and payload["invoke_pr_reviewer"] is True and payload["seq"] == 1, (payload, err)
    rc, payload, _ = _step4(chain, "existing")  # no --repo
    assert rc != 0 and payload["invoke_pr_reviewer"] is False
    rc, payload, _ = _step4(chain, "existing", repo=h.REPO, expected_issue_number=None)
    assert rc != 0 and payload["invoke_pr_reviewer"] is False


def test_step4_rejects_human_judgment_envelope(chain: dict[str, Any]) -> None:
    rc, payload, _ = _step4(chain, "human", repo=h.REPO)
    assert rc != 0 and payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert (
        "producer_status_not_handoff_eligible:human_judgment" in payload["adjudication"]["errors"]
    )
    assert not chain["world"].stored_keys()


@pytest.mark.parametrize("bad_exit", ["0", "1", "20x", "-1"])
def test_step4_rejects_producer_exit_inconsistent_with_status(
    chain: dict[str, Any], bad_exit: str
) -> None:
    rc, payload, _ = _step4(chain, "dry", producer_exit_code=bad_exit)
    assert rc != 0 and payload["invoke_pr_reviewer"] is False
    assert not chain["world"].stored_keys()


def test_step4_rejects_existing_go_with_exit_20(chain: dict[str, Any]) -> None:
    rc, payload, _ = _step4(chain, "existing", producer_exit_code="20", repo=h.REPO)
    assert rc != 0 and payload["invoke_pr_reviewer"] is False


def test_step4_requires_producer_exit_code_with_an_envelope(chain: dict[str, Any]) -> None:
    w = chain["world"]
    _rc, _env, art = chain["dry"]
    w.loop_state.unlink(missing_ok=True)
    rc, payload, _ = w.run_consumer(art)
    assert rc != 0 and "producer_exit_code_missing" in payload["adjudication"]["errors"]


@pytest.mark.parametrize(
    "drift",
    [
        {"head": HEAD_B, "expected_head": HEAD_A},
        {"body_sha256": OTHER},
        {"command_hashes": [OTHER]},
        {"verdict_hash": OTHER},
    ],
    ids=["head", "issue_body_sha", "command_hashes", "verdict_command_hash"],
)
def test_step4_rejects_binding_drift_without_dispatch(
    chain: dict[str, Any], drift: dict[str, Any]
) -> None:
    rc, payload, _ = _step4(chain, "dry", **drift)
    assert rc == 1, payload  # rerun, not malformed
    assert payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert not chain["world"].stored_keys()


def test_step4_distinguishes_malformed_from_rerun(chain: dict[str, Any]) -> None:
    w = chain["world"]
    _rc, _env, art = chain["dry"]
    w.loop_state.unlink(missing_ok=True)
    argv = [
        sys.executable,
        str(CONSUMER),
        "step4-adjudicate",
        "--loop-state-file",
        str(w.loop_state),
        "--expected-head-sha",
        HEAD_A,
        "--expected-contract-body-sha256",
        h.BODY_SHA256,
        "--expected-command-hashes-file",
        str(w.root / "does-not-exist.json"),
        "--test-verdict-file",
        str(art),
        "--contract-snapshot-file",
        str(art),
        "--diff-summary-file",
        str(art),
        "--allowed-paths-file",
        str(art),
    ]
    c = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
    assert c.returncode == 2, (c.stdout, c.stderr)


# --- Step 5 ----------------------------------------------------------------------------------------


def _dispatched(chain: dict[str, Any]) -> int:
    rc, payload, err = _step4(chain, "dry")
    assert rc == 0, (payload, err)
    return int(payload["seq"])


def test_step5_positive_control_reaches_approved_after_step4(chain: dict[str, Any]) -> None:
    seq = _dispatched(chain)
    rc, out = _step5(chain, seq=seq)
    assert (rc, out["route"], out["reason_code"]) == (0, "approved", None)


@pytest.mark.parametrize("stale", [0, 2, 99])
def test_step5_rejects_stale_dispatch_seq(chain: dict[str, Any], stale: int) -> None:
    _dispatched(chain)
    rc, out = _step5(chain, seq=stale)
    assert rc == 1 and out["route"] != "approved" and out["reason_code"] == "dispatch_seq_mismatch"


@pytest.mark.parametrize(
    "drift",
    [{"head": HEAD_B}, {"body": OTHER}, {"hashes": [OTHER]}],
    ids=["head", "issue_body_sha", "command_hashes"],
)
def test_step5_rejects_binding_drift_after_dispatch(
    chain: dict[str, Any], drift: dict[str, Any]
) -> None:
    seq = _dispatched(chain)
    rc, out = _step5(chain, seq=seq, **drift)
    assert rc == 1 and out["route"] != "approved"
    assert out["reason_code"] == "binding_changed_since_dispatch"


def test_step5_cannot_approve_without_a_step4_pass(chain: dict[str, Any]) -> None:
    chain["world"].loop_state.unlink(missing_ok=True)
    rc, out = _step5(chain)
    assert rc == 2 and out.get("route") != "approved"
    assert out["reason_code"] == "loop_state_malformed"


def test_step5_cannot_approve_after_a_failed_step4(chain: dict[str, Any]) -> None:
    rc, _payload, _ = _step4(chain, "dry", head=HEAD_B, expected_head=HEAD_A)
    assert rc == 1
    assert not chain["world"].loop_state.exists() or "dispatch" not in json.loads(
        chain["world"].loop_state.read_text(encoding="utf-8")
    )
    rc, out = _step5(chain)
    assert rc != 0 and out.get("route") != "approved"


@pytest.mark.parametrize(
    ("verdict", "route", "reason", "rc"),
    [
        ("REQUEST_CHANGES", "continue_loop", None, 1),
        ("HUMAN_REVIEW_REQUIRED", "route_human_escalation", "reviewer_human_review_required", 1),
    ],
)
def test_step5_non_approving_reviewer_verdicts(
    chain: dict[str, Any], verdict: str, route: str, reason: str | None, rc: int
) -> None:
    seq = _dispatched(chain)
    got_rc, out = _step5(
        chain,
        seq=seq,
        reviewer={"verdict": verdict, "reviewed_head_sha": HEAD_A, "blockers": [], "warnings": []},
    )
    assert (got_rc, out["route"], out["reason_code"]) == (rc, route, reason)


def test_step5_approve_with_blockers_is_not_approved(chain: dict[str, Any]) -> None:
    seq = _dispatched(chain)
    rc, out = _step5(
        chain,
        seq=seq,
        reviewer={
            "verdict": "APPROVE",
            "reviewed_head_sha": HEAD_A,
            "blockers": ["x"],
            "warnings": [],
        },
    )
    assert (rc, out["route"], out["reason_code"]) == (
        1,
        "fail_closed",
        "approve_with_blockers_inconsistent",
    )


def test_step5_reviewed_head_mismatch_routes_to_stale_head_rereview(chain: dict[str, Any]) -> None:
    seq = _dispatched(chain)
    rc, out = _step5(
        chain,
        seq=seq,
        reviewer={
            "verdict": "APPROVE",
            "reviewed_head_sha": HEAD_B,
            "blockers": [],
            "warnings": [],
        },
    )
    assert (rc, out["route"], out["reason_code"]) == (
        1,
        "route_stale_head_rereview",
        "stale_reviewed_head_sha",
    )


@pytest.mark.parametrize(
    ("live", "route", "reason"),
    [
        (
            {"head_sha": HEAD_A, "mergeable": "CONFLICTING", "merge_state_status": "DIRTY"},
            "conflict_hard_stop",
            "conflict_mergeable_CONFLICTING",
        ),
        (
            {"head_sha": HEAD_A, "mergeable": "MERGEABLE", "merge_state_status": "BEHIND"},
            "route_to_update_branch",
            None,
        ),
        (
            {"head_sha": HEAD_A, "mergeable": "UNKNOWN", "merge_state_status": "UNKNOWN"},
            "fail_closed",
            "mergeability_unknown",
        ),
        (
            {"head_sha": HEAD_B, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"},
            "route_stale_head_rereview",
            "stale_reviewed_head_sha",
        ),
    ],
    ids=["conflicting", "behind", "unknown", "live_head_moved"],
)
def test_step5_live_mergeability_never_false_green(
    chain: dict[str, Any], live: dict[str, str], route: str, reason: str | None
) -> None:
    seq = _dispatched(chain)
    rc, out = _step5(chain, seq=seq, live=live)
    assert rc == 1 and (out["route"], out["reason_code"]) == (route, reason)


# --- reviewer 4-field contract -----------------------------------------------------------------------

_GOOD = {"verdict": "APPROVE", "reviewed_head_sha": HEAD_A, "blockers": [], "warnings": []}


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"verdict": "LGTM"}, "schema_invalid_verdict_value"),
        ({"verdict": None}, "schema_invalid_verdict_value"),
        ({"reviewed_head_sha": ""}, "schema_invalid_reviewed_head_sha_empty_or_missing"),
        ({"reviewed_head_sha": 1}, "schema_invalid_reviewed_head_sha_empty_or_missing"),
        ({"blockers": "none"}, "schema_invalid_blockers_not_string_list"),
        ({"blockers": [1]}, "schema_invalid_blockers_not_string_list"),
        ({"warnings": "none"}, "schema_invalid_warnings_not_string_list"),
        ({"warnings": [None]}, "schema_invalid_warnings_not_string_list"),
    ],
)
def test_step5_rejects_malformed_reviewer_fields(
    chain: dict[str, Any], patch: dict[str, Any], reason: str
) -> None:
    seq = _dispatched(chain)
    rc, out = _step5(chain, seq=seq, reviewer={**_GOOD, **patch})
    assert rc == 1 and out["route"] == "fail_closed" and out["reason_code"].startswith(reason)


@pytest.mark.parametrize(
    "legacy",
    [
        "merge_ready",
        "required_auto_actions",
        "allowed_paths_gate",
        "mergeability",
        "mergeStateStatus",
        "recommendations",
        "test_verdict",
    ],
)
def test_step5_rejects_legacy_authority_fields(chain: dict[str, Any], legacy: str) -> None:
    seq = _dispatched(chain)
    rc, out = _step5(chain, seq=seq, reviewer={**_GOOD, legacy: True})
    assert rc == 1 and out["route"] == "fail_closed"
    assert out["reason_code"] == f"schema_invalid_legacy_field_present:{legacy}"


def test_step5_rejects_non_mapping_reviewer_verdict(chain: dict[str, Any]) -> None:
    seq = _dispatched(chain)
    rc, out = _step5(chain, seq=seq, reviewer=["APPROVE"])
    assert rc == 1 and out["route"] == "fail_closed"


def test_characterization_omitted_blockers_and_warnings_default_to_empty(
    chain: dict[str, Any],
) -> None:
    """Observed, not endorsed: the existing validator treats an omitted `blockers` / `warnings` as `[]`.

    So a reviewer result that drops those two fields can still route to ``approved``. The main session
    must therefore collect all four fields itself (the D15 acceptance does); this is recorded as a
    residual constraint in the manifest, and no new gate is added in D16.
    """
    seq = _dispatched(chain)
    rc, out = _step5(chain, seq=seq, reviewer={"verdict": "APPROVE", "reviewed_head_sha": HEAD_A})
    assert (rc, out["route"]) == (0, "approved")
    assert "omitted_four_field_defaults_to_empty" in _manifest()["residual_constraints"]


def test_manifest_records_untested_and_unverified_items() -> None:
    m = copy.deepcopy(_manifest())
    for key in (
        "main_drift_facts_producer_absent",
        "diff_summary_manual_construction",
        "terminal_gate_oracle_cwd",
    ):
        assert key in m["residual_constraints"], key
    assert m["runtime_acceptance"]["status"] == "OBSERVED_BY_MAIN_SESSION_OUTSIDE_THIS_FILE"
