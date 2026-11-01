"""Stage 2-7 (#37): canonical terminal path Step 4 -> 4.5 -> 5, real subprocess + fake `gh`.

Evidence boundary: every GitHub interaction is the stateful fake `gh` executable
(`fixtures/fake_gh_txn.py`) installed as the ONLY resolvable `gh` of a scratch copy of the repo. No
live GitHub call, auth or mutation. Nothing here is live GitHub evidence.

The chain exercised, each hop a separate real Python process:
  Step 4    edit_issue_txn.py (issue-editor's controlled transaction) -> controlled executor -> fake gh
  Step 4.5  dependency_materializer.py gate (no hard dependency: proceed; hard dependency: fail-closed)
  Step 5    publish_termination_report.py -> isolation bridge -> controlled executor -> fake gh

Positive (approved ops: issue_edit + issue_comment, no dependency) and negative (unapproved native
relationship, wrong repo, stale digest, partial failure, readback mismatch, duplicate) are separate tests;
the unapproved native relationship route is never reported as a success.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from test_stage2_4_edit_txn import (
    ISSUE,
    LEGACY_SLUG,
    OLD_BODY,
    TRUSTED,
    VALID_BODY,
    Project,
)
from test_stage2_4_edit_txn import proj as _stage2_4_proj

proj = _stage2_4_proj  # re-exported pytest fixture (scratch repo copy + fake gh as the only gh)

SKILL = Path(".claude/skills/issue-refinement-loop/scripts")
SUMMARY = "## 終了レポート\n\nissue-refinement-loop terminated: approved.\n"


def _run(proj: Project, script: Path, *args: str, stdin: str | None = None):
    return subprocess.run(
        [sys.executable, str(proj.root / script), *args],
        cwd=proj.root,
        env=proj.env(),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _last_json(cp: subprocess.CompletedProcess[str]) -> dict:
    lines = [ln for ln in cp.stdout.strip().splitlines() if ln.startswith("{")]
    assert lines, (cp.stdout, cp.stderr)
    return json.loads(lines[-1])


def _step4(proj: Project, **over) -> tuple[int, dict]:
    return proj.run_txn(proj.payload(**over))


def _step45_gate(proj: Project, relations: list[dict], repo: str = TRUSTED):
    decision = proj.root / "tmp" / "decision.json"
    decision.parent.mkdir(exist_ok=True)
    decision.write_text(json.dumps({"relations": relations}), encoding="utf-8")
    return _run(
        proj,
        SKILL / "dependency_materializer.py",
        "gate",
        "--target-issue",
        str(ISSUE),
        "--repo",
        repo,
        "--current-decision-file",
        str(decision),
    )


def _step5(proj: Project, repo: str = TRUSTED, issue: int = ISSUE):
    body = proj.root / "tmp" / "summary.md"
    body.parent.mkdir(exist_ok=True)
    body.write_text(SUMMARY, encoding="utf-8")
    return _run(
        proj,
        SKILL / "publish_termination_report.py",
        "--issue-number",
        str(issue),
        "--repo",
        repo,
        "--body-file",
        str(body),
    )


def _no_native_relationship_call(proj: Project) -> None:
    for call in proj.gh_calls:
        joined = " ".join(call)
        assert "blocked_by" not in joined and "dependencies" not in joined, call


# ------------------------------------------------------------------------- AC1 positive


def test_ac1_positive_step4_then_gate_then_step5_reaches_fake_gh_end_to_end(proj: Project) -> None:
    rc, res = _step4(proj)
    assert rc == 0 and res["status"] == "ok", res
    assert proj.state["body"] == VALID_BODY and proj.state["patches"] == 1
    assert proj.state["comments"] == []

    gate = _step45_gate(proj, relations=[])
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert _last_json(gate)["decision"] == "proceed"
    writes_after_gate = len(proj.gh_writes)
    assert writes_after_gate == 1  # only the Step 4 PATCH; the gate wrote nothing

    cp = _step5(proj)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    comments = proj.state["comments"]
    assert len(comments) == 1 and SUMMARY.strip() in comments[0]["body"]
    assert proj.state["posts"] == 1 and proj.state["patches"] == 1
    assert proj.state["body"] == VALID_BODY  # Step 5 never rewrites the body
    assert "unexpected" not in proj.state
    _no_native_relationship_call(proj)


def test_ac1_positive_step5_rerun_is_idempotent(proj: Project) -> None:
    assert _step5(proj).returncode == 0
    assert _step5(proj).returncode == 0
    assert len(proj.state["comments"]) == 1 and proj.state["posts"] == 1


# ------------------------------------------------------------------------- Step 4 negatives


def test_step4_stale_digest_is_write_zero_and_step5_is_not_reached(proj: Project) -> None:
    proj.set_state(body="somebody else edited this\n")
    data = proj.payload()
    data["expected_previous_body_sha256"] = (
        __import__("hashlib").sha256(OLD_BODY.encode()).hexdigest()
    )
    rc, res = proj.run_txn(data)
    assert rc == 1 and res["status"] != "ok" and res["mutation_started"] is False
    assert proj.gh_writes == [] and proj.state["patches"] == 0
    assert proj.state["body"] == "somebody else edited this\n"


def test_step4_wrong_repo_is_write_zero(proj: Project) -> None:
    rc, res = _step4(proj, repo=LEGACY_SLUG)
    assert rc == 1 and res["status"] == "failed_no_mutation"
    assert proj.gh_calls == [] or proj.gh_writes == []
    assert proj.state["patches"] == 0 and proj.state["body"] == OLD_BODY


@pytest.mark.parametrize("mode", ["fail", "noop"])
def test_step4_failed_or_unconfirmed_write_is_never_ok(proj: Project, mode: str) -> None:
    proj.set_state(patch_mode=mode)
    rc, res = _step4(proj)
    assert rc == 1 and res["status"] not in {"ok", "no_change"}, res
    assert proj.state["body"] == OLD_BODY


def test_step4_write_landed_but_gh_failed_is_ok_only_because_readback_confirms(
    proj: Project,
) -> None:
    """`applied_then_fail`: gh exits 1 after the PATCH landed. The transaction may only report `ok` when
    the fresh readback shows exactly the requested body; the state is checked independently here."""
    proj.set_state(patch_mode="applied_then_fail")
    rc, res = _step4(proj)
    if res["status"] == "ok":
        assert proj.state["body"] == VALID_BODY and rc == 0
    else:
        assert rc == 1


# ------------------------------------------------------------------------- Step 4.5 negatives


def test_step45_unapproved_native_relationship_fails_closed_with_zero_writes(proj: Project) -> None:
    """A confirmed hard dependency needs a native relationship write: an unapproved live mutation. The
    gate must never reach `proceed`, and nothing may be written."""
    gate = _step45_gate(
        proj,
        relations=[
            {
                "source_issue_number": ISSUE,
                "target_issue_number": 3,
                "relation_type": "depends_on",
            }
        ],
    )
    assert gate.returncode != 0, gate.stdout + gate.stderr
    assert _last_json(gate)["decision"] in {"block_persistent", "block_retryable"}
    assert proj.gh_writes == []
    assert proj.state["patches"] == 0 and proj.state["posts"] == 0
    _no_native_relationship_call(proj)


def test_step45_gate_wrong_repo_is_write_zero(proj: Project) -> None:
    gate = _step45_gate(
        proj,
        relations=[
            {"source_issue_number": ISSUE, "target_issue_number": 3, "relation_type": "depends_on"}
        ],
        repo=LEGACY_SLUG,
    )
    assert gate.returncode == 1 and _last_json(gate)["decision"] == "block_persistent"
    assert proj.gh_calls == []


# ------------------------------------------------------------------------- Step 5 negatives


@pytest.mark.parametrize("repo", [LEGACY_SLUG, "someone/agent-platform", "squne121"])
def test_step5_wrong_repo_is_write_zero(proj: Project, repo: str) -> None:
    cp = _step5(proj, repo=repo)
    assert cp.returncode != 0, cp.stdout + cp.stderr
    assert proj.gh_writes == [] and proj.state["posts"] == 0 and proj.state["comments"] == []


def test_step5_wrong_issue_binding_is_write_zero(proj: Project) -> None:
    cp = _step5(proj, issue=ISSUE + 1)
    assert cp.returncode != 0, cp.stdout + cp.stderr
    assert proj.state["comments"] == [] and proj.state["posts"] == 0
    assert proj.state.get("unexpected")  # the fake refused the foreign-issue comment endpoint


@pytest.mark.parametrize("mode", ["fail", "noop"])
def test_step5_failed_or_unreadable_post_is_not_success(proj: Project, mode: str) -> None:
    proj.set_state(post_mode=mode)
    cp = _step5(proj)
    assert cp.returncode != 0, cp.stdout + cp.stderr
    assert proj.state["comments"] == []  # readback finds nothing: never a success


def test_step5_post_landed_but_gh_failed_is_ok_only_because_readback_confirms(
    proj: Project,
) -> None:
    proj.set_state(post_mode="applied_then_fail")
    cp = _step5(proj)
    if cp.returncode == 0:
        comments = proj.state["comments"]
        assert len(comments) == 1 and SUMMARY.strip() in comments[0]["body"]
    else:
        assert cp.returncode != 0


def test_step5_duplicate_marker_comments_fail_before_any_write(proj: Project) -> None:
    assert _step5(proj).returncode == 0
    state = proj.state
    state["comments"].append(
        dict(state["comments"][0], id="IC_dup", url=state["comments"][0]["url"] + "x")
    )
    proj.set_state(comments=state["comments"])
    posts_before = proj.state["posts"]
    cp = _step5(proj)
    assert cp.returncode != 0, cp.stdout + cp.stderr
    assert proj.state["posts"] == posts_before and len(proj.state["comments"]) == 2


# ------------------------------------------------------------------------- unapproved metadata ops


def test_unapproved_label_state_and_relationship_operations_are_not_available() -> None:
    """The capability route table, not a test fixture, is what keeps these unavailable."""
    sys.path.insert(0, str(HERE.parents[1] / "scripts" / "agent-ops"))
    import workflow_capability_preflight as wcp

    approved = {"issue_comment", "issue_edit", "issue_create"}
    assert wcp._KNOWN_OPERATION_ROUTES == approved
    for op in (
        "issue_label_edit",
        "issue_state_change",
        "issue_native_parent_edit",
        "issue_dependency_edit",
        "issue_relationship_edit",
        "pr_create",
    ):
        assert wcp._operation_route(op) == "unavailable"
        assert op in wcp._MUTATION_OPERATION_NAMES
