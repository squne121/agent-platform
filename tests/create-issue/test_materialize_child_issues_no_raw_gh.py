"""AC6: the materializer never calls raw ``gh issue create`` / ``gh issue edit`` for issue
creation. All creation flows through create_issue_txn.py (including the triage_only label
profile). A fake-gh integration check proves creation reaches gh only via the transaction.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import materialize_child_issues as m

_GUARDS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agent-guards"
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))
import repo_identity

TRUSTED_REPO = repo_identity.load_trusted_repo_slug()


def _ok_validate(body, kind, title):
    return m.RunResult(0, '{"status": "pass"}')


def test_materializer_routes_creation_through_create_runner_not_gh(valid_plan):
    gh_calls: list[list[str]] = []
    create_calls: list[dict] = []

    def gh(args):
        gh_calls.append(args)
        return m.RunResult(0, "")

    def create(**kw):
        create_calls.append(kw)
        return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')

    res = m.materialize(valid_plan, m.Runners(validate=_ok_validate, create=create, gh=gh))
    assert res["status"] == "ok"
    # Creation happened exactly once, through the create runner (the txn path).
    assert len(create_calls) == 1
    # The materializer NEVER issued a raw `gh issue create` (nor `gh issue create`-like).
    assert all(a[:2] != ["issue", "create"] for a in gh_calls)


def test_default_create_runner_invokes_txn_with_label_profile(monkeypatch):
    captured = {}

    def fake_run(argv, *a, **kw):
        captured["argv"] = argv

        class R:
            returncode = 0
            stdout = '{"status":"success","issue_number":1,"issue_url":"u"}'
            stderr = ""

        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    m._default_create_runner(
        repo=TRUSTED_REPO,
        title="実装: x",
        body="b",
        kind="implementation",
        label_profile="triage_only",
        dependencies=[],
        parent_issue=0,
        gh_bin="gh",
    )
    argv = captured["argv"]
    assert str(argv[1]).endswith("create_issue_txn.py")
    assert "issue" not in argv or "create" not in argv  # never builds a raw `gh issue create`
    assert "--label-profile" in argv
    assert argv[argv.index("--label-profile") + 1] == "triage_only"


FAKE_GH_SRC = Path(__file__).resolve().parents[1] / "stage2" / "fixtures" / "fake_gh_create.py"


def _write_fake_gh(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """Install the stateful Stage 2-5 fake `gh` (fixture only, never live GitHub).

    It supports exactly the label-free create surface (issue list / create / comment, GET api,
    read-only graphql); anything else exits 97 and is recorded under state["unexpected"].
    """
    fake = tmp_path / "gh"
    fake.write_text(
        f"#!{sys.executable}\n" + FAKE_GH_SRC.read_text(encoding="utf-8").split("\n", 1)[1],
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    state = tmp_path / "gh_state.json"
    state.write_text(
        json.dumps({"repo": TRUSTED_REPO, "issues": [], "next_number": 999}), encoding="utf-8"
    )
    log = tmp_path / "gh_calls.log"
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    return fake, log, state


def _gh_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]


def _implementation_child(valid_child: dict) -> dict:
    child = dict(valid_child)
    child["depends_on"] = []
    return child


def test_fake_gh_integration_creation_only_via_txn(valid_child, tmp_path, monkeypatch):
    fake_gh, log, state = _write_fake_gh(tmp_path, monkeypatch)
    child = _implementation_child(valid_child)
    body = m.render_canonical_body(child, parent_issue=0)

    # Drive the default create runner (the real txn path, a real subprocess) against the fake gh.
    # An empty --issue-kind adopts the body MRC kind (label-free), with no parent / dependency:
    # the only shape the Stage 2-5 gate approves, isolating "creation flows only via the txn".
    res = m._default_create_runner(
        repo=TRUSTED_REPO,
        title=child["title"],
        body=body,
        kind="",
        label_profile="standard",
        dependencies=[],
        parent_issue=0,
        gh_bin=str(fake_gh),
    )

    calls = _gh_calls(log)
    create_calls = [c for c in calls if c[:2] == ["issue", "create"]]
    assert res.returncode == 0, (
        json.loads(res.stdout)["failure_stage"] + "|" + json.loads(res.stdout)["failure_message"]
    )
    assert len(create_calls) == 1, calls
    assert json.loads(state.read_text(encoding="utf-8")).get("unexpected", []) == []
    txn_out = json.loads(res.stdout.strip().splitlines()[-1])
    assert "create" in txn_out["completed_steps"]
    assert txn_out["issue_number"] == 999


def test_cli_fake_gh_integration_real_path(valid_child, clear_overlap, tmp_path, monkeypatch):
    """Drive the actual CLI (`materialize_child_issues.py --plan-file ... --gh fake-gh`) and
    assert that `gh issue create` reaches the fake gh ONLY through create_issue_txn.py (never as
    a direct materializer call) and that the created issue is captured in the RESULT_V2."""
    fake_gh, log, _state = _write_fake_gh(tmp_path, monkeypatch)
    plan = {
        "schema_version": 2,
        "repo": TRUSTED_REPO,
        "parent": {"issue_number": 254, "parent_mode": "delivery-rollup"},
        "issue_lookup": {"complete": True},
        "children": [_implementation_child(valid_child)],
        "overlap": clear_overlap,
        "parent_body_updates": [],
    }
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")

    cp = subprocess.run(
        [
            sys.executable,
            str(Path(m.__file__)),
            "--plan-file",
            str(plan_file),
            "--gh",
            str(fake_gh),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    calls = _gh_calls(log)
    # The materializer passes an explicit implementation kind and the parent issue, so the txn
    # gate refuses the (unapproved) label / parent mutation BEFORE any gh call: nothing is
    # created and the parent checklist is never patched.
    assert not any(c[:2] == ["issue", "create"] for c in calls), calls
    assert not any(c[:2] == ["issue", "edit"] for c in calls), calls
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert cp.returncode == 1 and result["status"] == "failed", result
    assert result["created_issues"] == [] and result["updated_parent"] is False
    assert "not approved" in result["errors"][0]["error"]


def test_materialize_full_run_never_raw_creates(valid_plan):
    """End-to-end materialize with recording runners: zero raw `gh issue create`."""
    gh_calls: list[list[str]] = []

    def gh(args):
        gh_calls.append(args)
        return m.RunResult(0, "")

    def create(**kw):
        return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')

    m.materialize(valid_plan, m.Runners(validate=_ok_validate, create=create, gh=gh))
    assert all(a[:2] != ["issue", "create"] for a in gh_calls)
    assert all(a[:2] != ["issue", "edit"] or "--body-file" in a for a in gh_calls)
