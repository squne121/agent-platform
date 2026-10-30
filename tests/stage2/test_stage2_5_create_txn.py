"""Stage 2-5 (#35): create_issue_txn / create-issue Skill / issue-creator Agent migration.

Evidence boundary: every GitHub interaction here is the stateful fake `gh`
(`fixtures/fake_gh_create.py`), run as a real executable via `--gh` in a real subprocess, or via
`gh_bin=` from in-process calls. No live GitHub read, auth or mutation is made.

Test names carry the Issue AC tag (`ac1` .. `ac6`) so each AC's verification command is `-k acN`.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_REL = Path(".claude/skills/create-issue/scripts/create_issue_txn.py")
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = ROOT / "docs" / "dev" / "stage2-5-create-txn-manifest.json"
AGENT = ROOT / ".claude" / "agents" / "issue-creator.md"
TRUSTED = "squne121/agent-platform"
LEGACY_SLUG = "/".join(["squne121", "loop-protocol"])  # noqa: FLY002 - no literal for the slug scan
TITLE = "実装: fixture"
VALID_BODY = (FIXTURES / "valid_implementation_body.md").read_text(encoding="utf-8")
# Only these gh calls belong to the approved label-free path (an `api` call must be a GET or the
# read-only node-id query; `issue edit` / mutations / sub-issue POSTs are never approved).
APPROVED_GH = {
    ("issue", "list"),
    ("issue", "create"),
    ("issue", "comment"),
    ("api", "graphql"),
    ("api", "--method"),
}

sys.path.insert(0, str(ROOT / SCRIPT_REL.parent))
import create_issue_txn as txn

# --------------------------------------------------------------------------- harness


def _copy_project(dst: Path) -> Path:
    ignore = shutil.ignore_patterns(
        ".git",
        ".venv",
        "__pycache__",
        "worktrees",
        "tmp",
        "artifacts",
        ".pytest_cache",
        ".ruff_cache",
    )
    shutil.copytree(ROOT, dst, ignore=ignore)
    return dst


class Project:
    """A scratch copy of the repo (own repo_identity config) plus a fake `gh` and its state/log."""

    def __init__(self, root: Path, state_path: Path, log_path: Path, gh: Path) -> None:
        self.root, self.state_path, self.log_path, self.gh = root, state_path, log_path, gh

    @property
    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def set_state(self, **kw) -> None:
        data = self.state
        data.update(kw)
        self.state_path.write_text(json.dumps(data), encoding="utf-8")

    @property
    def gh_calls(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(x) for x in self.log_path.read_text(encoding="utf-8").splitlines()]

    @property
    def verbs(self) -> list[tuple[str, str]]:
        return [tuple(c[:2]) for c in self.gh_calls]  # type: ignore[misc]

    def env(self) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_REPO"}
        }
        env["FAKE_GH_STATE"] = str(self.state_path)
        env["FAKE_GH_LOG"] = str(self.log_path)
        return env

    def body_file(self, text: str = VALID_BODY, name: str = "body.md") -> str:
        path = self.root / "tmp" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run(self, *args: str, timeout: int = 120) -> tuple[int, dict]:
        cp = subprocess.run(
            [sys.executable, str(self.root / SCRIPT_REL), *args, "--gh", str(self.gh)],
            cwd=self.root,
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        assert cp.stdout.strip(), cp.stderr
        return cp.returncode, json.loads(cp.stdout.strip().splitlines()[-1])

    def create(self, *extra: str, repo: str = TRUSTED, title: str = TITLE, body: str | None = None):
        body_path = body if body is not None else self.body_file()
        return self.run("--repo", repo, "--title", title, "--body-file", body_path, *extra)

    def assert_gh_zero(self) -> None:
        assert self.gh_calls == [], self.gh_calls

    def assert_only_approved_gh(self) -> None:
        assert self.state.get("unexpected", []) == []
        assert set(self.verbs) <= APPROVED_GH, self.verbs
        for c in self.gh_calls:
            if c[0] == "api" and "--method" in c:
                assert c[c.index("--method") + 1] == "GET", c
            if c[:2] == ["api", "graphql"]:
                assert not any("mutation" in a for a in c), c


@pytest.fixture()
def proj(tmp_path: Path) -> Project:
    root = _copy_project(tmp_path / "proj")
    gh = tmp_path / "fakebin" / "gh"
    gh.parent.mkdir()
    shutil.copy(FIXTURES / "fake_gh_create.py", gh)
    gh.write_text(
        f"#!{sys.executable}\n" + gh.read_text(encoding="utf-8").split("\n", 1)[1], encoding="utf-8"
    )
    gh.chmod(0o755)
    state_path = tmp_path / "gh_state.json"
    state_path.write_text(
        json.dumps({"repo": TRUSTED, "issues": [], "next_number": 501}), encoding="utf-8"
    )
    return Project(root, state_path, tmp_path / "gh_calls.log", gh)


def test_harness_gh_is_the_fake_and_logs_every_call(proj: Project) -> None:
    rc, res = proj.create()
    assert rc == 0, res
    assert proj.gh_calls and proj.gh_calls[0][:2] == ["issue", "list"]


# --------------------------------------------------------------------------- AC1 repo identity

BAD_REPOS = [
    LEGACY_SLUG,
    "squne121/other-repo",
    "other-owner/agent-platform",
    "SQUNE121/agent-platform",
    "squne121/agent-platform ",
    "squne121",
    "squne121/agent-platform/extra",
    "https://github.com/squne121/agent-platform",
    "",
]


@pytest.mark.parametrize("repo", BAD_REPOS)
def test_ac1_create_untrusted_repo_makes_zero_gh_calls(proj: Project, repo: str) -> None:
    rc, res = proj.create(repo=repo)
    assert rc == 1 and res["status"] == "failure"
    assert res["failure_stage"] == "repo-identity" and res["failure_message"] == "repo_untrusted"
    proj.assert_gh_zero()


@pytest.mark.parametrize("repo", BAD_REPOS)
def test_ac1_reconcile_untrusted_repo_makes_zero_gh_calls(proj: Project, repo: str) -> None:
    rc, res = proj.run("reconcile", "--repo", repo, "--issue", "501")
    assert rc == 1 and res["failure_stage"] == "repo-identity"
    proj.assert_gh_zero()


@pytest.mark.parametrize("entry", ["create", "reconcile"])
def test_ac1_missing_repo_argument_is_rejected_before_gh(proj: Project, entry: str) -> None:
    cp = subprocess.run(
        [
            sys.executable,
            str(proj.root / SCRIPT_REL),
            *(["reconcile", "--issue", "1"] if entry == "reconcile" else ["--title", "t"]),
            "--gh",
            str(proj.gh),
        ],
        cwd=proj.root,
        env=proj.env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 2 and "--repo" in cp.stderr
    proj.assert_gh_zero()


def test_ac1_trusted_repo_reaches_the_first_gh_read(proj: Project) -> None:
    rc, res = proj.create()
    assert rc == 0 and res["status"] == "success"
    assert proj.gh_calls[0][:2] == ["issue", "list"]


@pytest.mark.parametrize(
    "config",
    [
        None,
        "{",
        "[]",
        '{"schema": "REPOSITORY_IDENTITY_V1"}',
        '{"schema": "X", "trusted_repo_slug": "a/b"}',
        '{"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "../x"}',
        '{"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "a/b", "x": 1}',
    ],
)
def test_ac1_unusable_identity_config_rejects_even_the_formerly_trusted_slug(
    proj: Project, config
) -> None:
    cfg = proj.root / "config" / "repository_identity.v1.json"
    if config is None:
        cfg.unlink()
    else:
        cfg.write_text(config, encoding="utf-8")
    for args in (
        ("--repo", TRUSTED, "--title", TITLE, "--body-file", proj.body_file()),
        ("reconcile", "--repo", TRUSTED, "--issue", "501"),
    ):
        rc, res = proj.run(*args)
        assert rc == 1 and res["failure_stage"] == "repo-identity"
        assert res["failure_message"] == "repo_identity_unavailable"
    proj.assert_gh_zero()


def test_ac1_third_slug_config_is_followed_and_the_old_slug_is_rejected(proj: Project) -> None:
    third = "example-org/third-repo"
    cfg = proj.root / "config" / "repository_identity.v1.json"
    cfg.write_text(
        json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": third}),
        encoding="utf-8",
    )
    proj.set_state(repo=third)
    rc, res = proj.create(repo=TRUSTED)
    assert rc == 1 and res["failure_message"] == "repo_untrusted"
    proj.assert_gh_zero()
    rc, res = proj.create(repo=third)
    assert rc == 0 and res["issue_url"] == f"https://github.com/{third}/issues/501"


def test_ac1_a_preregistered_repo_identity_module_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    fake = types.ModuleType("repo_identity")
    fake.is_trusted_repo = lambda repo, config_path=None: True  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "repo_identity", fake)
    calls: list = []
    monkeypatch.setattr(txn, "run_command", lambda *a, **k: calls.append(a))
    res = txn.run_transaction(
        repo=LEGACY_SLUG,
        title=TITLE,
        body=VALID_BODY,
        body_file="",
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[],
        gh_bin="gh",
    )
    assert res.failure_stage == "repo-identity" and calls == []


def test_ac1_non_string_repo_is_rejected_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(txn, "run_command", lambda *a, **k: calls.append(a))
    for bad in (None, 5, ["squne121/agent-platform"], b"squne121/agent-platform"):
        res = txn.run_transaction(
            repo=bad,
            title=TITLE,
            body=VALID_BODY,
            body_file="",
            labels=[],  # type: ignore[arg-type]
            parent_issue_number=0,
            dependency_issue_numbers=[],
            gh_bin="gh",
        )
        assert res.failure_stage == "repo-identity"
        res = txn.reconcile_transaction(
            repo=bad,
            issue_number=1,
            labels=[],
            parent_issue_number=0,  # type: ignore[arg-type]
            dependency_issue_numbers=[],
            gh_bin="gh",
        )
        assert res.failure_stage == "repo-identity"
    assert calls == []


def test_ac1_the_script_holds_no_slug_constant_of_its_own() -> None:
    text = (ROOT / SCRIPT_REL).read_text(encoding="utf-8")
    assert TRUSTED not in text and LEGACY_SLUG not in text
    assert "repo_identity.py" in text and "is_trusted_repo" in text


# --------------------------------------------------------------------------- AC2 create / readback


def test_ac2_valid_create_writes_once_and_the_new_issue_is_read_back(proj: Project) -> None:
    rc, res = proj.create()
    assert rc == 0 and res["status"] == "success", res
    st = proj.state
    assert st["create_calls"] == 1 and len(st["issues"]) == 1
    issue = st["issues"][0]
    assert res["issue_number"] == issue["number"] == 501
    assert res["issue_url"] == issue["url"] == f"https://github.com/{TRUSTED}/issues/501"
    assert issue["title"] == TITLE and issue["body"] == VALID_BODY
    assert res["completed_steps"][0] == "create" and "label" in res["completed_steps"]
    # fresh readback: after the create the title search must see exactly the created issue, then the
    # issue itself (number/title/body/url) is read back with a read-only GET (the last gh call)
    assert proj.verbs.index(("issue", "create")) < len(proj.verbs) - 1
    assert proj.verbs[-2:] == [("issue", "list"), ("api", "--method")]
    assert "body-readback" in res["completed_steps"] and "body-readback" in res["verified_steps"]
    assert res["applied_steps"] == [] and res["pending_steps"] == []
    proj.assert_only_approved_gh()


def test_ac2_same_request_rerun_does_not_create_a_duplicate(proj: Project) -> None:
    rc, first = proj.create()
    rc2, second = proj.create()
    assert rc == 0 and rc2 == 0
    assert first["status"] == "success" and second["status"] == "dedupe"
    assert second["issue_number"] == second["dedupe_number"] == first["issue_number"]
    assert proj.state["create_calls"] == 1 and len(proj.state["issues"]) == 1
    proj.assert_only_approved_gh()


@pytest.fixture()
def inproc(proj: Project, monkeypatch: pytest.MonkeyPatch) -> Project:
    for k, v in proj.env().items():
        if k.startswith("FAKE_GH"):
            monkeypatch.setenv(k, v)
    return proj


def _call(p: Project, **over):
    kw = {
        "repo": TRUSTED,
        "title": TITLE,
        "body": "",
        "body_file": p.body_file(),
        "labels": [],
        "parent_issue_number": 0,
        "dependency_issue_numbers": [],
        "gh_bin": str(p.gh),
        "sleep_fn": lambda _s: None,
    }
    kw.update(over)
    return txn.run_transaction(**kw)


def test_ac2_authoritative_wrapper_env(inproc: Project) -> None:
    res = txn.run_transaction_with_authoritative_readback(
        repo=TRUSTED,
        title=TITLE,
        body="",
        body_file=inproc.body_file(),
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[],
        gh_bin=str(inproc.gh),
        sleep_fn=lambda _s: None,
    )
    assert res.status == "success" and res.node_id == "I_node_501" and res.body_sha256


def test_ac2_search_index_lag_is_bounded_and_never_a_second_create(inproc: Project) -> None:
    inproc.set_state(list_mode="lag", lag_calls=3)
    res = _call(inproc)
    # dedupe (1) + poll attempts: lag hides the new issue for 2 polls, then it is confirmed
    assert res.status == "success" and inproc.state["create_calls"] == 1
    assert inproc.state["list_calls"] == 4


def test_ac2_permanent_lag_is_not_a_verified_success(inproc: Project) -> None:
    inproc.set_state(list_mode="lag", lag_calls=10_000)
    res = _call(inproc)
    assert res.status == "partial_failure" and res.failure_stage == "race-detection-inconclusive"
    assert res.issue_number == 501 and "readback-confirmation" in res.pending_steps
    st = inproc.state
    assert st["create_calls"] == 1 and st["list_calls"] == 1 + txn._DEFAULT_MAX_ATTEMPTS
    assert ("issue", "comment") in inproc.verbs  # the observability comment on the NEW issue only


def test_ac2_permanent_lag_cli_exit_code_is_2(proj: Project) -> None:
    proj.set_state(list_mode="lag", lag_calls=10_000)
    rc, res = proj.create()
    assert rc == 2 and res["status"] == "partial_failure"


def test_ac2_node_id_readback_failure_is_not_a_verified_success(inproc: Project) -> None:
    inproc.set_state(issues=[], created=False)
    res = txn.run_transaction_with_authoritative_readback(
        repo=TRUSTED,
        title=TITLE,
        body="",
        body_file=inproc.body_file(),
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[],
        gh_bin=str(inproc.gh),
        sleep_fn=lambda _s: None,
    )
    assert res.status == "success"  # control: the readback works when the fake answers
    inproc.set_state(graphql_broken=True)
    res = txn.run_transaction_with_authoritative_readback(
        repo=TRUSTED,
        title=TITLE + " 2",
        body="",
        body_file=inproc.body_file(),
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[],
        gh_bin=str(inproc.gh),
        sleep_fn=lambda _s: None,
    )
    assert res.status == "partial_failure" and res.failure_stage == "node-id-readback"
    assert res.node_id is None and res.issue_number is not None


def test_ac2_node_id_readback_failure_on_a_dedupe_hit_is_not_a_verified_success(
    inproc: Project,
) -> None:
    inproc.set_state(
        graphql_broken=True,
        issues=[
            {
                "number": 10,
                "title": TITLE,
                "body": "",
                "state": "OPEN",
                "url": f"https://github.com/{TRUSTED}/issues/10",
            }
        ],
    )
    res = txn.run_transaction_with_authoritative_readback(
        repo=TRUSTED,
        title=TITLE,
        body="",
        body_file=inproc.body_file(),
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[],
        gh_bin=str(inproc.gh),
        sleep_fn=lambda _s: None,
    )
    assert res.status == "partial_failure" and res.failure_stage == "node-id-readback"
    assert res.issue_number == 10 and res.dedupe_number == 10 and res.node_id is None


@pytest.mark.parametrize("mode", ["applied_then_hang", "garbage_url", "empty_url"])
def test_ac2_unknown_outcome_stays_unknown_when_the_readback_lags(
    inproc: Project, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setitem(txn.run_command.__kwdefaults__, "timeout", 1.0)
    inproc.set_state(create_mode=mode, list_mode="lag", lag_calls=10_000)
    res = _call(inproc)
    assert res.failure_stage == "issue-create-outcome-unknown" and res.issue_number is None
    assert "no open issue" in res.failure_message


def test_ac2_applied_then_failed_with_an_unavailable_readback_is_outcome_unknown(
    inproc: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    inproc.set_state(create_mode="applied_then_fail")
    real = txn._find_open_issues_by_title
    seen = {"n": 0}

    def flaky(repo, title, gh_bin):
        seen["n"] += 1
        if seen["n"] >= 2:  # the first call is the pre-create dedupe search
            raise txn.TransactionError("dedupe-search", "readback boom")
        return real(repo, title, gh_bin)

    monkeypatch.setattr(txn, "_find_open_issues_by_title", flaky)
    res = _call(inproc)
    assert res.failure_stage == "issue-create-outcome-unknown"
    assert "readback unavailable" in res.failure_message


def test_ac2_skip_internal_dedupe_never_attributes_an_unrelated_same_title_issue(
    inproc: Project,
) -> None:
    inproc.set_state(
        create_mode="fail",
        issues=[
            {
                "number": 10,
                "title": TITLE,
                "body": "",
                "state": "OPEN",
                "url": f"https://github.com/{TRUSTED}/issues/10",
            }
        ],
    )
    res = _call(inproc, skip_internal_title_dedupe=True)
    assert res.failure_stage == "issue-create-outcome-unknown"
    assert res.issue_number is None and "[10]" in res.failure_message


@pytest.mark.parametrize(
    "variant",
    ["shorthand", "crlf", "heading_only", "mrc_only"],
)
def test_ac3_body_parent_syntax_variants_are_all_rejected_before_gh(
    proj: Project, variant: str
) -> None:
    base = VALID_BODY
    if variant == "shorthand":
        body = (
            base.replace("parent_issue: none", "parent_issue: none")
            .replace("## Parent Issue\n\nnone", "## Parent Issue\n\n#29")
            .replace("parent_issue: none", 'parent_issue: "#29"')
        )
    elif variant == "crlf":
        body = (
            base.replace("parent_issue: none", 'parent_issue: "#29"')
            .replace("## Parent Issue\n\nnone", "## Parent Issue\n\n#29")
            .replace("\n", "\r\n")
        )
    elif variant == "heading_only":
        body = base.replace("parent_issue: none\n", "").replace(
            "## Parent Issue\n\nnone", "## Parent Issue\n\n#29"
        )
    else:
        body = base.replace("parent_issue: none", 'parent_issue: "#29"').replace(
            "## Parent Issue\n\nnone", "## Parent Issue\n\n#29"
        )
    _rc, res = proj.create(body=proj.body_file(body, f"{variant}.md"))
    assert res["status"] == "failure"
    assert res["failure_stage"] in {"unapproved-operation", "issue-body-validate"}, res
    proj.assert_gh_zero()


def test_ac2_race_duplicate_after_create_is_a_failure_not_success(inproc: Project) -> None:
    inproc.set_state(list_mode="dup_after_create")
    res = _call(inproc)
    assert res.status != "success" and res.failure_stage == "dedupe-race-detection"
    assert inproc.state["create_calls"] == 1


def test_ac2_ambiguous_dedupe_is_not_collapsed_to_one_and_never_creates(inproc: Project) -> None:
    inproc.set_state(
        issues=[
            {
                "number": n,
                "title": TITLE,
                "body": "",
                "url": f"https://github.com/{TRUSTED}/issues/{n}",
                "state": "OPEN",
            }
            for n in (10, 11)
        ]
    )
    res = _call(inproc)
    assert res.status != "success" and res.failure_stage == "dedupe-search"
    assert res.status != "dedupe"
    assert inproc.state.get("create_calls", 0) == 0


def test_ac2_title_collision_with_a_different_title_is_not_a_dedupe(inproc: Project) -> None:
    inproc.set_state(
        issues=[
            {
                "number": 10,
                "title": TITLE + " (別)",
                "body": "",
                "url": f"https://github.com/{TRUSTED}/issues/10",
                "state": "OPEN",
            }
        ]
    )
    res = _call(inproc)
    assert res.status == "success" and res.issue_number == 501


@pytest.mark.parametrize("mode", ["fail", "non_json"])
def test_ac2_dedupe_search_failure_or_non_json_never_creates(inproc: Project, mode: str) -> None:
    inproc.set_state(list_mode=mode)
    res = _call(inproc)
    assert res.status == "failure" and res.failure_stage == "dedupe-search"
    assert inproc.state.get("create_calls", 0) == 0


def test_ac2_create_cli_failure_with_nothing_written_is_a_plain_failure(inproc: Project) -> None:
    inproc.set_state(create_mode="fail")
    res = _call(inproc)
    assert res.status == "failure" and res.failure_stage == "issue-create"
    assert "no-mutation is not asserted" in res.failure_message
    assert res.issue_url is None


def test_ac2_create_applied_then_failed_is_reconciled_by_readback_not_called_clean(
    inproc: Project,
) -> None:
    inproc.set_state(create_mode="applied_then_fail")
    res = _call(inproc)
    assert res.status == "failure" and res.failure_stage == "issue-create-outcome-unknown"
    assert (
        res.issue_number == 501
    )  # the readback found the issue that the failed CLI actually wrote
    assert "Do not assume no mutation" in res.failure_message


@pytest.mark.parametrize("mode", ["empty_url", "garbage_url"])
def test_ac2_lost_or_unparsable_create_receipt_is_outcome_unknown(
    inproc: Project, mode: str
) -> None:
    inproc.set_state(create_mode=mode)
    res = _call(inproc)
    assert res.status == "failure" and res.failure_stage == "issue-create-outcome-unknown"
    assert res.issue_number == 501 and inproc.state["create_calls"] == 1


def test_ac2_create_timeout_is_outcome_unknown_and_never_failed_no_mutation(
    inproc: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(txn.run_command.__kwdefaults__, "timeout", 1.0)
    inproc.set_state(create_mode="applied_then_hang")
    res = _call(inproc)
    assert res.status == "failure" and res.failure_stage == "issue-create-outcome-unknown"
    assert "timeout" in res.failure_message and "Do not assume no mutation" in res.failure_message
    assert res.issue_number == 501
    assert inproc.state["create_calls"] == 1


def test_ac2_create_failure_is_never_reported_as_success_or_dedupe(inproc: Project) -> None:
    for mode in ("fail", "applied_then_fail", "empty_url", "garbage_url"):
        p = inproc
        p.set_state(create_mode=mode, issues=[], created=False, next_number=501)
        res = _call(p)
        assert res.status not in {"success", "dedupe"}, mode


def test_ac2_main_returns_json_and_nonzero_for_a_transaction_error(
    proj: Project, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        txn,
        "_dispatch",
        lambda args: (_ for _ in ()).throw(txn.TransactionError("x-stage", "boom")),
    )
    rc = txn.main(["--repo", TRUSTED, "--title", TITLE])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["status"] == "failure" and out["failure_stage"] == "x-stage"


def test_ac2_missing_body_file_is_a_failure_with_zero_gh(proj: Project) -> None:
    rc, res = proj.create(body=str(proj.root / "tmp" / "nope.md"))
    assert rc == 1 and res["failure_stage"] == "body-file-read"
    proj.assert_gh_zero()


def test_ac2_invalid_body_is_rejected_before_any_gh(proj: Project) -> None:
    rc, res = proj.create(body=proj.body_file("## Outcome\n\nonly\n"))
    assert rc == 1 and res["failure_stage"] == "issue-body-validate"
    proj.assert_gh_zero()


def test_ac2_invalid_issue_kind_is_rejected_before_any_gh(proj: Project) -> None:
    rc, res = proj.create(
        "--issue-kind", "research"
    )  # contradicts the body's MRC kind (implementation)
    assert rc == 1 and res["failure_stage"] == "issue-body-validate"
    proj.assert_gh_zero()
    cp = subprocess.run(
        [
            sys.executable,
            str(proj.root / SCRIPT_REL),
            "--repo",
            TRUSTED,
            "--title",
            TITLE,
            "--issue-kind",
            "bogus",
            "--body-file",
            proj.body_file(),
            "--gh",
            str(proj.gh),
        ],
        cwd=proj.root,
        env=proj.env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 1 and json.loads(cp.stdout)["status"] == "failure"
    proj.assert_gh_zero()


def test_ac2_whitespace_title_is_rejected(proj: Project) -> None:
    rc, res = proj.create(title="   ")
    assert rc == 1 and res["status"] == "failure"
    assert proj.state.get("create_calls", 0) == 0


# --------------------------------------------------------------------------- AC3 approved operations


def test_ac3_triage_only_is_not_label_free() -> None:
    assert txn._resolve_labels([], "", "triage_only") == ["triage-required"]
    assert txn._resolve_labels([], "research", "standard") == []
    assert txn._resolve_labels([], "", "standard") == []
    assert txn._resolve_labels([], "implementation", "standard") == list(
        txn._IMPLEMENTATION_STANDARD_LABELS
    )


@pytest.mark.parametrize("extra", [[], ["--issue-kind", ""]])
def test_ac3_standard_no_label_path_is_label_parent_dependency_free(
    proj: Project, extra: list[str]
) -> None:
    rc, res = proj.create(*extra)
    assert rc == 0 and res["status"] == "success"
    assert not any(c[:2] in (["issue", "edit"], ["api", "graphql"]) for c in proj.gh_calls)
    assert res["applied_steps"] == [] and res["parent_verified"] is None
    assert res["dependency_verified"] is None and res["blocking_verified"] is None
    assert not any(
        "sub_issue" in s or "dependency" in s or "blocking" in s for s in res["completed_steps"]
    )
    proj.assert_only_approved_gh()


@pytest.mark.parametrize(
    "args",
    [
        ["--label-profile", "triage_only"],
        ["--issue-kind", "implementation"],
        ["--label", "bug"],
        ["--label", "triage-required"],
        ["--parent-issue", "29"],
        ["--blocked-by", "5"],
        ["--dependency", "5"],
        ["--blocking", "5"],
        [
            "--label-profile",
            "triage_only",
            "--label",
            "x",
            "--parent-issue",
            "29",
            "--blocked-by",
            "5",
        ],
    ],
)
def test_ac3_unapproved_create_input_is_rejected_before_the_first_gh_call(
    proj: Project, args: list[str]
) -> None:
    rc, res = proj.create(*args)
    assert rc == 1 and res["status"] == "failure" and res["failure_stage"] == "unapproved-operation"
    assert res["issue_number"] is None and res["completed_steps"] == []
    proj.assert_gh_zero()  # no read, no auth, no create: no partial application is possible


def test_ac3_body_derived_parent_is_rejected_before_the_first_gh_call(proj: Project) -> None:
    body = VALID_BODY.replace("parent_issue: none", 'parent_issue: "#29"').replace(
        "## Parent Issue\n\nnone", "## Parent Issue\n\n#29"
    )
    assert body != VALID_BODY
    rc, res = proj.create(body=proj.body_file(body, "parent.md"))
    assert (
        rc == 1
        and res["failure_stage"] == "unapproved-operation"
        and "parent #29" in res["failure_message"]
    )
    proj.assert_gh_zero()


def test_ac3_a_dedupe_hit_never_updates_labels_or_relationships(proj: Project) -> None:
    proj.create()
    proj.log_path.write_text("", encoding="utf-8")
    rc, res = proj.create()
    assert rc == 0 and res["status"] == "dedupe"
    # the dedupe hit only reads: the title search and the existing-body GET
    assert set(proj.verbs) == {("issue", "list"), ("api", "--method")}
    assert all(c[c.index("--method") + 1] == "GET" for c in proj.gh_calls if c[0] == "api")
    assert res["applied_steps"] == []


@pytest.mark.parametrize(
    "args",
    [
        ["--label", "bug"],
        ["--parent-issue", "29"],
        ["--blocked-by", "5"],
        ["--dependency", "5"],
        ["--blocking", "5"],
    ],
)
def test_ac3_reconcile_with_any_unapproved_input_is_rejected_before_gh(
    proj: Project, args: list[str]
) -> None:
    rc, res = proj.run("reconcile", "--repo", TRUSTED, "--issue", "501", *args)
    assert rc == 1 and res["failure_stage"] == "unapproved-operation" and res["issue_number"] == 501
    proj.assert_gh_zero()


def test_ac3_reconcile_with_nothing_to_apply_is_a_no_op_with_zero_gh(proj: Project) -> None:
    rc, res = proj.run("reconcile", "--repo", TRUSTED, "--issue", "501")
    assert rc == 0 and res["status"] == "success" and res["applied_steps"] == []
    proj.assert_gh_zero()


def test_ac3_no_label_edit_or_relationship_graphql_is_ever_issued_on_any_gated_path(
    inproc: Project,
) -> None:
    for kw in (
        {"labels": ["x"]},
        {"issue_kind": "implementation"},
        {"label_profile": "triage_only"},
        {"parent_issue_number": 3},
        {"dependency_issue_numbers": [3]},
        {"blocking_issue_numbers": [3]},
        {"dependency_issue_numbers": ["https://github.com/o/r/issues/3"]},
        {"dependency_issue_numbers": [True]},
    ):
        res = _call(inproc, **kw)
        assert res.failure_stage == "unapproved-operation", kw
    assert inproc.gh_calls == []


def test_ac3_the_retained_source_handlers_are_only_reachable_through_the_gate() -> None:
    tree = ast.parse((ROOT / SCRIPT_REL).read_text(encoding="utf-8"))
    engines = {"_run_transaction_engine", "_reconcile_transaction_engine"}
    callers: dict[str, set[str]] = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in engines
            ):
                callers.setdefault(node.func.id, set()).add(fn.name)
    assert callers == {
        "_run_transaction_engine": {"run_transaction"},
        "_reconcile_transaction_engine": {"reconcile_transaction"},
    }
    # the public wrappers must invoke the gate first, and run_transaction must pin the engine flag
    src = (ROOT / SCRIPT_REL).read_text(encoding="utf-8")
    run_fn = src.split("def run_transaction(", 1)[1].split("\ndef ", 1)[0]
    assert run_fn.index("_gate_approved_operations(") < run_fn.index("_run_transaction_engine(")
    assert "approved_operations_only=True" in run_fn
    rec_fn = src.split("def reconcile_transaction(", 1)[1].split("\ndef ", 1)[0]
    assert rec_fn.index("_gate_approved_operations(") < rec_fn.index(
        "_reconcile_transaction_engine("
    )
    # main() reaches the engines only via the gated wrappers
    assert "_dispatch" in src and "run_transaction_with_authoritative_readback" in src


def test_ac3_authoritative_readback_wrapper_is_gated_too(inproc: Project) -> None:
    res = txn.run_transaction_with_authoritative_readback(
        repo=LEGACY_SLUG,
        title=TITLE,
        body=VALID_BODY,
        body_file="",
        labels=["x"],
        parent_issue_number=9,
        dependency_issue_numbers=[1],
        gh_bin=str(inproc.gh),
    )
    assert res.failure_stage == "repo-identity" and res.node_id is None
    assert inproc.gh_calls == []


def test_ac3_source_tests_drive_the_retained_handlers_through_the_engine_never_the_gate() -> None:
    for rel in (
        ".claude/skills/create-issue/tests/test_create_issue_txn.py",
        ".claude/skills/create-issue/tests/test_create_issue_txn_blocking.py",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "txn.run_transaction(" not in text and "txn.reconcile_transaction(" not in text, rel
        assert "_run_transaction_engine(" in text or "_reconcile_transaction_engine(" in text, rel


# --------------------------------------------------------------------------- AC4 real subprocess


def test_ac4_real_subprocess_positive_and_negative_controls_share_one_harness(
    proj: Project,
) -> None:
    rc, ok = proj.create()
    assert rc == 0 and ok["status"] == "success"
    ok_calls = len(proj.gh_calls)
    assert ok_calls >= 3
    rc, bad = proj.create(repo=LEGACY_SLUG, title="別タイトル")
    assert rc == 1 and bad["status"] == "failure"
    assert len(proj.gh_calls) == ok_calls  # the negative control added zero gh calls
    assert proj.state["create_calls"] == 1


def test_ac4_fake_gh_fails_closed_on_any_unapproved_surface(proj: Project) -> None:
    cp = subprocess.run(
        [str(proj.gh), "issue", "edit", "--repo", TRUSTED, "1", "--add-label", "x"],
        env=proj.env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 97 and proj.state["unexpected"]


# --------------------------------------------------------------------------- AC2 remote body readback


def _gh_get_calls(p: Project) -> list[list[str]]:
    return [c for c in p.gh_calls if c[0] == "api" and "--method" in c and "/issues/" in c[-1]]


def _assert_created_but_unverified(res, p: Project) -> None:
    """The issue exists, so the outcome is a partial_failure that keeps its identity (never no-mutation)."""
    assert res.status == "partial_failure", res
    assert res.failure_stage == "body-readback", res
    assert res.issue_number == 501 and res.issue_url == f"https://github.com/{TRUSTED}/issues/501"
    assert "body-readback" in res.pending_steps and "body-readback" not in res.verified_steps
    assert res.status not in {"success", "dedupe", "failed_no_mutation"}
    assert p.state["create_calls"] == 1


def test_ac2_remote_body_differs_from_the_requested_body_is_not_success(inproc: Project) -> None:
    # GitHub "saved" another body: create exits 0, URL/number/title are all correct (the false-green).
    inproc.set_state(stored_body="## Acceptance Criteria\n\n- [ ] AC1: GitHub rewrote this\n")
    _assert_created_but_unverified(_call(inproc), inproc)


def test_ac2_remote_body_dropped_is_not_success(inproc: Project) -> None:
    inproc.set_state(stored_body="")
    _assert_created_but_unverified(_call(inproc), inproc)


@pytest.mark.parametrize(
    "override",
    [
        {"title": "別のタイトル"},
        {"number": 999},
        {"html_url": "https://github.com/squne121/agent-platform/issues/999"},
        {"body": "__DELETE__"},
        {"title": "__DELETE__"},
        {"html_url": "__DELETE__"},
        {"number": "__DELETE__"},
        {"body": "<truncated>"},
        {"body": 12345},
        {"body": None},
    ],
    ids=lambda o: json.dumps(o, ensure_ascii=False),
)
def test_ac2_readback_identity_title_or_field_problems_are_not_success(
    inproc: Project, override: dict
) -> None:
    inproc.set_state(get_override=override)
    _assert_created_but_unverified(_call(inproc), inproc)


@pytest.mark.parametrize("mode", ["fail", "non_json", "hang"])
def test_ac2_readback_api_error_invalid_json_or_timeout_is_not_success(
    inproc: Project, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setitem(txn.run_command.__kwdefaults__, "timeout", 1.0)
    inproc.set_state(get_mode=mode)
    res = _call(inproc)
    _assert_created_but_unverified(res, inproc)
    assert res.node_id is None and res.body_sha256 is None


def test_ac2_transient_readback_mismatch_then_match_succeeds_within_the_bounded_retry(
    inproc: Project,
) -> None:
    inproc.set_state(get_override={"body": "stale"}, get_override_calls=2)
    sleeps: list[float] = []
    res = _call(inproc, sleep_fn=sleeps.append)
    assert res.status == "success", res
    assert len(_gh_get_calls(inproc)) == 3 and sleeps == list(txn._BODY_READBACK_RETRY_DELAYS)


def test_ac2_persistent_readback_mismatch_fails_after_a_bounded_number_of_gets(
    inproc: Project,
) -> None:
    inproc.set_state(get_override={"body": "stale"})
    sleeps: list[float] = []
    res = _call(inproc, sleep_fn=sleeps.append)
    _assert_created_but_unverified(res, inproc)
    assert len(_gh_get_calls(inproc)) == len(txn._BODY_READBACK_RETRY_DELAYS) + 1
    assert sum(sleeps) <= 2.0


def test_ac2_readback_normalises_only_line_endings_and_trailing_whitespace(inproc: Project) -> None:
    inproc.set_state(stored_body=VALID_BODY.replace("\n", "\r\n").rstrip() + "\r\n\r\n")
    assert _call(inproc).status == "success"


def test_ac2_the_comparison_baseline_is_the_pre_create_snapshot_not_a_reread_of_body_file(
    inproc: Project,
) -> None:
    body_file = Path(inproc.body_file())
    real = txn._issue_create

    def create_then_edit_the_file(*a, **k):
        url = real(*a, **k)
        body_file.write_text("edited after the create\n", encoding="utf-8")
        return url

    txn_create = txn._issue_create
    txn._issue_create = create_then_edit_the_file
    try:
        res = _call(inproc, body_file=str(body_file))
    finally:
        txn._issue_create = txn_create
    # the remote body equals the snapshot (what was validated and sent), so the edit cannot move
    # the baseline in either direction
    assert res.status == "success", res


def test_ac2_readback_uses_only_read_only_gets_and_no_extra_mutation(inproc: Project) -> None:
    inproc.set_state(get_override={"body": "stale"})
    _call(inproc)
    st = inproc.state
    assert st["create_calls"] == 1 and len(st["issues"]) == 1 and "comments" in st
    inproc.assert_only_approved_gh()
    assert [c[c.index("--method") + 1] for c in _gh_get_calls(inproc)] == ["GET"] * st["get_calls"]


def test_ac2_rerun_after_a_readback_failure_does_not_create_a_duplicate(proj: Project) -> None:
    proj.set_state(get_override={"body": "stale"}, get_override_calls=3)
    rc1, first = proj.create()
    assert (
        rc1 != 0
        and first["status"] == "partial_failure"
        and first["failure_stage"] == "body-readback"
    )
    rc2, second = proj.create()
    assert (
        rc2 == 0
        and second["status"] == "dedupe"
        and second["issue_number"] == first["issue_number"]
    )
    assert proj.state["create_calls"] == 1 and len(proj.state["issues"]) == 1


def test_ac2_wrapper_body_sha256_is_remote_verified_evidence_only(inproc: Project) -> None:
    import hashlib

    kw = {
        "repo": TRUSTED,
        "title": TITLE,
        "body": "",
        "body_file": inproc.body_file(),
        "labels": [],
        "parent_issue_number": 0,
        "dependency_issue_numbers": [],
        "gh_bin": str(inproc.gh),
        "sleep_fn": lambda _s: None,
    }
    ok = txn.run_transaction_with_authoritative_readback(**kw)
    assert ok.status == "success"
    assert ok.body_sha256 == hashlib.sha256(VALID_BODY.encode("utf-8")).hexdigest()
    # dedupe hit: the remote body was not compared by this transaction -> no hash is claimed
    dedupe = txn.run_transaction_with_authoritative_readback(**kw)
    assert dedupe.status == "dedupe" and dedupe.body_sha256 is None
    # remote body mismatch -> not success and no hash of unverified input
    inproc.set_state(
        issues=[], created=False, next_number=501, stored_body="other", get_calls=0, create_calls=0
    )
    bad = txn.run_transaction_with_authoritative_readback(**kw)
    assert bad.status == "partial_failure" and bad.failure_stage == "body-readback"
    assert bad.body_sha256 is None


def _wrapper_kw(p: Project, **over):
    kw = {
        "repo": TRUSTED,
        "title": TITLE,
        "body": "",
        "body_file": p.body_file(),
        "labels": [],
        "parent_issue_number": 0,
        "dependency_issue_numbers": [],
        "gh_bin": str(p.gh),
        "sleep_fn": lambda _s: None,
    }
    kw.update(over)
    return kw


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _after_node_id_get(monkeypatch: pytest.MonkeyPatch, action) -> None:
    """Run ``action`` right after the wrapper's node-id GET (i.e. after the body was verified)."""
    real = txn._issue_graphql_ids

    def wrapped(*a, **k):
        out = real(*a, **k)
        action()
        return out

    monkeypatch.setattr(txn, "_issue_graphql_ids", wrapped)


def test_ac2_body_sha256_survives_body_file_edit_after_verification(
    inproc: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = "## Acceptance Criteria\n\n- [ ] AC1: swapped after the readback\n"
    body_file = Path(inproc.body_file())
    _after_node_id_get(monkeypatch, lambda: body_file.write_text(other, encoding="utf-8"))
    res = txn.run_transaction_with_authoritative_readback(
        **_wrapper_kw(inproc, body_file=str(body_file))
    )
    assert res.status == "success", res
    assert body_file.read_text(encoding="utf-8") == other  # the swap really happened
    assert res.body_sha256 == _sha(VALID_BODY) != _sha(other)
    assert res.body_sha256 == _sha(inproc.state["issues"][0]["body"])  # == the remote body


def test_ac2_body_sha256_survives_body_file_deletion_after_verification(
    inproc: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    body_file = Path(inproc.body_file())
    _after_node_id_get(monkeypatch, body_file.unlink)
    res = txn.run_transaction_with_authoritative_readback(
        **_wrapper_kw(inproc, body_file=str(body_file))
    )
    assert not body_file.exists()
    assert res.status == "success", res
    assert res.body_sha256 == _sha(VALID_BODY)


def test_ac2_body_sha256_is_the_raw_remote_body_hash_not_the_normalised_or_local_one(
    inproc: Project,
) -> None:
    # remote differs from the local text only by line endings / trailing whitespace (accepted by
    # the normalised comparison); the evidence hash must still point at what the remote stored.
    remote = VALID_BODY.replace("\n", "\r\n") + "  \n"
    inproc.set_state(stored_body=remote)
    res = txn.run_transaction_with_authoritative_readback(**_wrapper_kw(inproc))
    assert res.status == "success", res
    assert inproc.state["issues"][0]["body"] == remote
    assert res.body_sha256 == _sha(remote) != _sha(VALID_BODY)


def test_ac2_late_step_failure_after_verified_readback_keeps_the_remote_body_hash(
    inproc: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a, **_k):
        raise txn.TransactionError(stage="label", message="injected post-readback failure")

    monkeypatch.setattr(txn, "_issue_apply_labels", boom)
    res = txn.run_transaction_with_authoritative_readback(**_wrapper_kw(inproc))
    assert res.status == "partial_failure" and res.failure_stage == "label"
    assert "body-readback" in res.verified_steps and res.issue_number == 501
    assert res.body_sha256 == _sha(inproc.state["issues"][0]["body"])


def test_ac2_remote_body_change_after_create_never_reports_success_or_a_hash(
    inproc: Project,
) -> None:
    inproc.set_state(get_override={"body": "tampered remote body"})
    res = txn.run_transaction_with_authoritative_readback(**_wrapper_kw(inproc))
    assert res.status == "partial_failure" and res.failure_stage == "body-readback"
    assert res.body_sha256 is None and res.issue_number == 501 and res.issue_url


def _load_mutant(project: Project, old: str, new: str):
    """Load a one-edit mutant of the real source from inside the scratch project copy (the gate
    resolves repo_identity relative to the module location, so it must live at the same layout)."""
    import importlib.util

    src = (ROOT / SCRIPT_REL).read_text(encoding="utf-8")
    assert src.count(old) == 1, old
    path = project.root / SCRIPT_REL.parent / "create_issue_txn_mutant.py"
    path.write_text(src.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("create_issue_txn_mutant", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _evidence_scenario(p: Project, mod) -> list[str]:
    """Return the list of violated evidence invariants (empty == the code is sound)."""
    violations: list[str] = []
    # (a) remote body mismatch must not be success and must not carry a hash
    p.set_state(get_override={"body": "tampered remote body"})
    bad = mod.run_transaction_with_authoritative_readback(**_wrapper_kw(p))
    if bad.status == "success" or bad.body_sha256 is not None:
        violations.append("remote-mismatch-accepted")
    p.set_state(
        issues=[], created=False, next_number=501, get_override=None, get_calls=0, create_calls=0
    )
    # (b) the hash must follow the verified remote body, not a later body_file state
    body_file = Path(p.body_file())
    real = mod._issue_graphql_ids

    def swap(*a, **k):
        out = real(*a, **k)
        body_file.write_text("swapped after verification\n", encoding="utf-8")
        return out

    mod._issue_graphql_ids = swap
    try:
        ok = mod.run_transaction_with_authoritative_readback(
            **_wrapper_kw(p, body_file=str(body_file))
        )
    finally:
        mod._issue_graphql_ids = real
    if ok.status != "success" or ok.body_sha256 != _sha(VALID_BODY):
        violations.append("hash-does-not-follow-verified-body")
    # (c) a step AFTER the verified readback fails -> partial_failure that still carries the hash
    p.set_state(issues=[], created=False, next_number=501, get_calls=0, create_calls=0)

    def boom(*_a, **_k):
        raise mod.TransactionError(stage="label", message="injected post-readback failure")

    real_labels = mod._issue_apply_labels
    mod._issue_apply_labels = boom
    try:
        late = mod.run_transaction_with_authoritative_readback(**_wrapper_kw(p))
    finally:
        mod._issue_apply_labels = real_labels
    if late.status != "partial_failure" or late.body_sha256 != _sha(VALID_BODY):
        violations.append("late-failure-drops-verified-hash")
    return violations


def test_ac2_evidence_scenario_is_clean_on_the_real_source(inproc: Project) -> None:
    assert _evidence_scenario(inproc, txn) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            "_normalize_readback_body(remote_body) != expected_norm",
            "False",
            "remote-mismatch-accepted",
        ),
        (
            'return hashlib.sha256(remote_body.encode("utf-8")).hexdigest()',
            'return hashlib.sha256(expected_body.encode("utf-8")).hexdigest()',
            None,  # equal to the pre-create snapshot: only observable with a normalised-equal remote
        ),
        (
            "body_sha256 = result.verified_body_sha256",
            'body_sha256 = hashlib.sha256(Path(body_file).read_text(encoding="utf-8").encode("utf-8")).hexdigest()',
            "hash-does-not-follow-verified-body",
        ),
        (
            'if "body-readback" in result.verified_steps:\n        body_sha256 = result.verified_body_sha256',
            'if True:\n        body_sha256 = result.verified_body_sha256 or hashlib.sha256(body.encode("utf-8")).hexdigest()',
            "remote-mismatch-accepted",
        ),
        (
            "failed_readbacks=failed_readbacks,\n            verified_body_sha256=verified_body_sha256,\n        )\n\n\ndef _require_trusted_repo",
            "failed_readbacks=failed_readbacks,\n        )\n\n\ndef _require_trusted_repo",
            "late-failure-drops-verified-hash",
        ),
    ],
    ids=[
        "comparison-disabled",
        "hash-of-snapshot",
        "wrapper-rereads-body-file",
        "hash-claimed-without-verification",
        "late-failure-drops-hash",
    ],
)
def test_ac2_mutants_of_body_comparison_and_hash_propagation_are_detected(
    inproc: Project, old: str, new: str, expected: str | None
) -> None:
    mod = _load_mutant(inproc, old, new)
    if expected is None:
        # snapshot-hash mutant: remote differs from the snapshot only by CRLF + trailing spaces
        remote = VALID_BODY.replace("\n", "\r\n") + "  \n"
        inproc.set_state(stored_body=remote)
        res = mod.run_transaction_with_authoritative_readback(**_wrapper_kw(inproc))
        assert res.body_sha256 != _sha(remote), (
            "mutant survived: hash no longer follows the remote body"
        )
        # and the real source is caught by the very same observation
        inproc.set_state(issues=[], created=False, next_number=501, get_calls=0, create_calls=0)
        assert txn.run_transaction_with_authoritative_readback(
            **_wrapper_kw(inproc)
        ).body_sha256 == _sha(remote)
        return
    assert expected in _evidence_scenario(inproc, mod)


# --------------------------------------------------------------------------- AC5 Agent / Skill parity


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, _, rest = text.partition("---\n")
    front, sep, body = rest.partition("\n---\n")
    assert sep
    data = yaml.safe_load(front)
    assert isinstance(data, dict)
    return data, body


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_ac5_issue_creator_frontmatter_equals_the_recorded_source_frontmatter() -> None:
    front, _ = _frontmatter(AGENT)
    assert front == _manifest()["agent_skill_parity"]["issue_creator_source_frontmatter"]


def test_ac5_tools_disallowed_permission_mode_and_skills() -> None:
    front, _ = _frontmatter(AGENT)
    assert front["tools"] == ["Bash", "Read"]
    assert front["disallowedTools"] == ["Agent", "Edit", "MultiEdit", "Write", "Skill"]
    assert front["permissionMode"] == "acceptEdits"
    assert front["skills"] == ["create-issue"]
    assert (front["model"], front["effort"]) == ("sonnet", "medium")


def test_ac5_permission_mode_is_not_a_permission_bypass() -> None:
    front, _ = _frontmatter(AGENT)
    assert front["permissionMode"] not in {"bypassPermissions", "dontAsk", "auto"}
    assert {"Edit", "MultiEdit", "Write"} <= set(
        front["disallowedTools"]
    )  # acceptEdits has nothing to accept


def test_ac5_skills_preload_is_not_a_tool_allowlist_and_nested_calls_are_blocked_structurally() -> (
    None
):
    front, body = _frontmatter(AGENT)
    assert "Skill" not in front["tools"] and "Agent" not in front["tools"]
    assert {"Skill", "Agent"} <= set(front["disallowedTools"])
    assert "preload の宣言であり、実行時アクセス制御ではない" in body
    assert "nested Skill invocation は構造的に不可能" in body
    assert "nested SubAgent invocation" in body


def test_ac5_raw_mutation_ban_is_documented_as_procedural_and_approvals_are_not_implied() -> None:
    _, body = _frontmatter(AGENT)
    assert "procedural contract" in body and "技術的強制ではない" in body
    assert "create_issue_txn.py" in body
    assert "承認を意味しない" in body or "承認されていない" in body
    for needle in ("label", "parent", "blocked-by", "blocking"):
        assert needle in body
    assert "`--label-profile triage_only` は無ラベル起票を保証しない" in body


def test_ac5_issue_creator_and_issue_editor_keep_their_split() -> None:
    creator, cbody = _frontmatter(AGENT)
    editor, ebody = _frontmatter(ROOT / ".claude" / "agents" / "issue-editor.md")
    assert creator["skills"] == ["create-issue"] and editor["skills"] == ["edit-issue"]
    assert "issue-editor" in cbody and "issue-creator" in ebody
    for key in ("tools", "disallowedTools", "permissionMode", "model", "effort"):
        assert creator[key] == editor[key]


def test_ac5_skill_documents_the_approved_operation_boundary() -> None:
    skill = (ROOT / ".claude/skills/create-issue/SKILL.md").read_text(encoding="utf-8")
    assert "Stage 2-5" in skill and "triage_only" in skill and "triage-required" in skill
    assert "未承認" in skill


# --------------------------------------------------------------------------- AC6 manifest


def test_ac6_manifest_schema_counts_and_zero_skip_contract() -> None:
    m = _manifest()
    assert m["schema"] == "STAGE2_5_CREATE_TXN_MANIFEST_V1"
    assert m["issue"] == "squne121/agent-platform#35"
    assert m["source"]["commit"] == "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
    assert set(m["test_files"]) == set(m["expected_collect"]) == set(m["expected_ran"])
    deferred = {d["id"] for d in m["deferred_tests"]}
    for d in m["deferred_tests"]:
        assert d["owner_stage"] and d["reason"] and d["rerun_condition"]
    for f in m["test_files"]:
        assert m["expected_ran"][f] <= m["expected_collect"][f]
    assert sum(m["expected_collect"].values()) - sum(m["expected_ran"].values()) == len(deferred)
    assert "skip" in json.dumps(m["fixture_live_evidence_boundary"]).lower()


def test_ac6_source_hashes_in_the_manifest_match_the_migrated_files() -> None:
    import hashlib

    m = _manifest()
    for rel, rec in m["source_parity"].items():
        assert (ROOT / rel).is_file(), rel
        moved = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        if rec["verbatim"]:
            assert moved == rec["source_sha256"], rel
        else:
            assert moved == rec["moved_sha256"] != rec["source_sha256"], rel
            assert rec["intentional_diff"], rel


def test_ac6_stage_2_4_deferred_issue_creator_tests_are_released_and_really_run() -> None:
    parent = json.loads(
        (ROOT / "docs/dev/stage2-4-edit-txn-manifest.json").read_text(encoding="utf-8")
    )
    ids = {d["id"] for d in parent["deferred_tests"]}
    split = ".claude/agents/tests/test_issue_creator_issue_editor_split.py"
    assert not [i for i in ids if i.startswith(split)]
    assert parent["expected_collect"][split] == parent["expected_ran"][split] == 7
    assert (ROOT / split).is_file() and AGENT.is_file()
