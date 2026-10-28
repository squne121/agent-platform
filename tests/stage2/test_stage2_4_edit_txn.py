"""Stage 2-4 (#34): edit_issue_txn / edit-issue Skill / issue-editor Agent migration.

Evidence boundary: every GitHub interaction here is a fake `gh` (an in-process recorder, or the stateful
`fixtures/fake_gh_txn.py` executable used by the real-subprocess tests). No live GitHub call, auth or
mutation is made. The real-subprocess tests copy the repo into a scratch tree and run the real
`edit_issue_txn.py` -> real guard / hygiene / readiness -> real controlled executor chain against that fake.

Test names carry the Issue AC tag (`ac1` .. `ac8`) so each AC's verification command is `-k acN`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TXN_SCRIPTS = ROOT / ".claude" / "skills" / "edit-issue" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRUSTED = "squne121/agent-platform"
LEGACY_SLUG = "/".join(["squne121", "loop-protocol"])  # noqa: FLY002 - no literal for the slug scan
ISSUE = 1287
UPDATED_AT = "2026-01-01T00:00:00Z"
SOURCE_DOC = ROOT / "docs" / "dev" / "agent-skill-boundaries.md"

if str(TXN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(TXN_SCRIPTS))
import edit_issue_txn as txn


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


VALID_BODY = (FIXTURES / "valid_implementation_body.md").read_text(encoding="utf-8")
OLD_BODY = "old issue body\n"


# --------------------------------------------------------------------------- in-process harness


class _CP:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture()
def repo_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "tmp").mkdir(parents=True)
    (root / "artifacts").mkdir()
    monkeypatch.setattr(txn, "REPO_ROOT", root)
    return root


def _payload(repo_tmp: Path, **over) -> dict:
    (repo_tmp / "tmp" / "new_body.md").write_text("new issue body", encoding="utf-8")
    data = {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": ISSUE,
        "repo": TRUSTED,
        "new_body_file": "tmp/new_body.md",
        "readiness_forwarding_payload": {
            "readiness_result": {
                "status": "go",
                "body_sha256": "sha256:old",
                "source_checks": ["contract_readiness_check.py --mode static"],
                "errors": [],
                "readiness_result_ref": "artifact.json",
            }
        },
        "comment_mode": {"mode": "skip"},
        "expected_previous_body_sha256": txn._sha256_text("old issue body"),
        "expected_previous_updated_at": UPDATED_AT,
        "title_update": {"required": False, "proposed_title": None, "reason": None},
    }
    data.update(over)
    return data


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every child command; any attempt to run one fails the test unless a test re-stubs it."""
    recorded: list[list[str]] = []

    def _run(args, *, env=None):
        recorded.append([str(a) for a in args])
        raise AssertionError(f"unexpected child command: {args}")

    monkeypatch.setattr(txn, "_run_command", _run)
    return recorded


# --------------------------------------------------------------------------- real-subprocess harness


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
    for cmd in (
        ["git", "init", "-q", str(dst)],
        ["git", "-C", str(dst), "remote", "add", "origin", f"https://github.com/{TRUSTED}.git"],
        ["git", "-C", str(dst), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
        [
            "git",
            "-C",
            str(dst),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "base",
        ],
    ):
        subprocess.run(cmd, check=True, capture_output=True)
    # The executor only looks for `gh` in a fixed system path list. In the scratch COPY (never in the
    # repo) that list is pointed at the fake bin dir so the real /usr/bin/gh can never be reached.
    executor = dst / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"
    src = executor.read_text(encoding="utf-8")
    needle = '_GH_TRUSTED_PATHS = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"'
    assert needle in src
    executor.write_text(
        src.replace(needle, f'_GH_TRUSTED_PATHS = "{dst / "fakebin"}"'), encoding="utf-8"
    )
    return dst


class Project:
    def __init__(self, root: Path, state_path: Path, log_path: Path) -> None:
        self.root, self.state_path, self.log_path = root, state_path, log_path

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
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    @property
    def gh_writes(self) -> list[list[str]]:
        return [
            c
            for c in self.gh_calls
            if c[:1] == ["api"] and "--method" in c and c[c.index("--method") + 1] != "GET"
        ]

    def env(self) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_REPO"}
        }
        env["PATH"] = f"{self.root / 'fakebin'}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_GH_STATE"] = str(self.state_path)
        env["FAKE_GH_LOG"] = str(self.log_path)
        return env

    def write_input(self, data: dict, name: str = "txn_input.json") -> str:
        (self.root / "tmp").mkdir(exist_ok=True)
        (self.root / "tmp" / name).write_text(json.dumps(data), encoding="utf-8")
        return f"tmp/{name}"

    def run_txn(
        self, data: dict | None = None, *, input_rel: str | None = None
    ) -> tuple[int, dict]:
        rel = input_rel or self.write_input(data or {})
        script = self.root / ".claude" / "skills" / "edit-issue" / "scripts" / "edit_issue_txn.py"
        cp = subprocess.run(
            [sys.executable, str(script), "--input-file", rel],
            cwd=self.root,
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert cp.stdout.strip(), cp.stderr
        return cp.returncode, json.loads(cp.stdout)

    def payload(self, new_body: str = VALID_BODY, **over) -> dict:
        (self.root / "tmp").mkdir(exist_ok=True)
        (self.root / "tmp" / "new_body.md").write_text(new_body, encoding="utf-8")
        st = self.state
        data = {
            "schema": "ISSUE_EDIT_TXN_INPUT_V1",
            "issue_number": ISSUE,
            "repo": TRUSTED,
            "new_body_file": "tmp/new_body.md",
            "readiness_forwarding_payload": {
                "readiness_result": {
                    "status": "go",
                    "body_sha256": f"sha256:{_sha(new_body)}",
                    "source_checks": ["contract_readiness_check.py --mode static"],
                    "errors": [],
                    "readiness_result_ref": "artifact.json",
                }
            },
            "comment_mode": {"mode": "skip"},
            "expected_previous_body_sha256": txn._sha256_text(st["body"]),
            "expected_previous_updated_at": st["updated_at"],
            "title_update": {"required": False, "proposed_title": None, "reason": None},
        }
        data.update(over)
        return data


@pytest.fixture()
def proj(tmp_path: Path) -> Project:
    root = _copy_project(tmp_path / "proj")
    bin_dir = root / "fakebin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    shutil.copy(FIXTURES / "fake_gh_txn.py", fake)
    fake.write_text(
        f"#!{sys.executable}\n" + fake.read_text(encoding="utf-8").split("\n", 1)[1],
        encoding="utf-8",
    )
    fake.chmod(0o755)
    state_path = tmp_path / "gh_state.json"
    state_path.write_text(
        json.dumps(
            {
                "number": ISSUE,
                "repo": TRUSTED,
                "title": "old title",
                "body": OLD_BODY,
                "updated_at": UPDATED_AT,
                "state": "OPEN",
                "comments": [],
                "patches": 0,
                "posts": 0,
                "patch_mode": "ok",
                "post_mode": "ok",
            }
        ),
        encoding="utf-8",
    )
    return Project(root, state_path, tmp_path / "gh_calls.log")


def test_harness_fake_gh_is_the_only_gh(proj: Project) -> None:
    """The scratch tree can only ever resolve `gh` to the fake (guards the whole real-subprocess lane)."""
    executor = (proj.root / "scripts/agent-guards/controlled_skill_mutation_exec.py").read_text(
        encoding="utf-8"
    )
    assert f'_GH_TRUSTED_PATHS = "{proj.root / "fakebin"}"' in executor
    which = subprocess.run(
        ["which", "gh"], env=proj.env(), capture_output=True, text=True, check=False
    ).stdout.strip()
    assert which == str(proj.root / "fakebin" / "gh")


def _assert_failed_no_mutation(proj: Project, rc: int, res: dict, code: str | None = None) -> None:
    assert rc == 1
    assert res["schema"] == "ISSUE_EDIT_TXN_RESULT_V1"
    assert res["status"] == "failed_no_mutation"
    assert res["mutation_started"] is False
    assert proj.gh_writes == []
    assert proj.state["patches"] == 0 and proj.state["posts"] == 0
    assert proj.state["body"] == OLD_BODY
    if code is not None:
        assert any(e["code"] == code or code in e["message"] for e in res["errors"]), res["errors"]


# --------------------------------------------------------------------------- AC1: repo identity, write-zero

WRONG_REPOS = {
    "legacy_repo": LEGACY_SLUG,
    "other_owner": "someone/agent-platform",
    "case_differs": "squne121/Agent-Platform",
    "no_slash": "squne121",
    "too_many_segments": "squne121/agent-platform/x",
    "trailing_space": "squne121/agent-platform ",
    "url_form": "https://github.com/squne121/agent-platform",
    "flag_shaped": "--repo=squne121/agent-platform",
    "dotdot": "squne121/..",
}


@pytest.mark.parametrize("repo", list(WRONG_REPOS.values()), ids=list(WRONG_REPOS))
def test_ac1_untrusted_repo_rejected_before_any_child_command(
    repo_tmp: Path, calls, repo: str
) -> None:
    with pytest.raises(ValueError, match="repo_untrusted"):
        txn.run_transaction(_payload(repo_tmp, repo=repo))
    assert calls == []


@pytest.mark.parametrize(
    "repo", [None, "", 7, ["squne121/agent-platform"]], ids=["none", "empty", "int", "list"]
)
def test_ac1_non_string_repo_rejected_before_any_child_command(repo_tmp: Path, calls, repo) -> None:
    with pytest.raises(ValueError, match="repo_invalid"):
        txn.run_transaction(_payload(repo_tmp, repo=repo))
    assert calls == []


def test_ac1_trusted_repo_reaches_the_first_gh_read(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the identity check is not what blocks a trusted repo."""
    seen: list[list[str]] = []

    def _run(args, *, env=None):
        seen.append([str(a) for a in args])
        return _CP(1, stderr="stop here")

    monkeypatch.setattr(txn, "_run_command", _run)
    result = txn.run_transaction(_payload(repo_tmp))
    assert result["status"] == "failed_no_mutation"
    assert (
        seen
        and seen[0][1:3] == ["issue", "view"]
        and seen[0][seen[0].index("--repo") + 1] == TRUSTED
    )


def _identity_config_dir(tmp_path: Path, config: str | None) -> Path:
    guards = tmp_path / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "agent-guards" / "repo_identity.py", guards / "repo_identity.py")
    if config is not None:
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "repository_identity.v1.json").write_text(config, encoding="utf-8")
    return guards


@pytest.mark.parametrize(
    "config",
    [
        None,
        "not json",
        "[]",
        json.dumps({"schema": "REPOSITORY_IDENTITY_V1"}),
        json.dumps({"schema": "WRONG", "trusted_repo_slug": TRUSTED}),
        json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "a/../b"}),
    ],
    ids=["missing", "malformed", "not_object", "no_slug", "wrong_schema", "dotdot_slug"],
)
def test_ac1_unusable_identity_config_rejects_before_any_child_command(
    repo_tmp: Path, calls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config
) -> None:
    guards = _identity_config_dir(tmp_path / "alt", config)
    monkeypatch.setattr(txn, "AGENT_GUARDS_IMPORT_ROOT", guards)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "repo_identity", raising=False)
    try:
        with pytest.raises(ValueError, match="repo_identity_unavailable"):
            txn.run_transaction(_payload(repo_tmp))
    finally:
        sys.modules.pop("repo_identity", None)  # never leak the scratch module into later tests
    assert calls == []


def test_ac1_shadow_repo_identity_module_cannot_answer(
    repo_tmp: Path, calls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `repo_identity` registered in sys.modules or placed first on sys.path is ignored: the module is
    always loaded from the fixed on-disk path."""
    import types

    fake = types.ModuleType("repo_identity")
    fake.is_trusted_repo = lambda repo, config_path=None: True
    monkeypatch.setitem(sys.modules, "repo_identity", fake)
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "repo_identity.py").write_text(
        "def is_trusted_repo(repo, config_path=None):\n    return True\n"
    )
    monkeypatch.setattr(sys, "path", [str(evil), *sys.path])
    with pytest.raises(ValueError, match="repo_untrusted"):
        txn.run_transaction(_payload(repo_tmp, repo="evil/x"))
    assert calls == []


def test_ac1_missing_identity_module_rejects_before_any_child_command(
    repo_tmp: Path, calls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(txn, "AGENT_GUARDS_IMPORT_ROOT", tmp_path / "empty")
    monkeypatch.setattr(
        sys, "path", [str(tmp_path / "empty")] + [p for p in sys.path if "agent-guards" not in p]
    )
    monkeypatch.delitem(sys.modules, "repo_identity", raising=False)
    try:
        with pytest.raises(ValueError, match="repo_identity_unavailable"):
            txn.run_transaction(_payload(repo_tmp))
    finally:
        sys.modules.pop("repo_identity", None)
    assert calls == []


@pytest.mark.parametrize("repo", list(WRONG_REPOS.values()), ids=list(WRONG_REPOS))
def test_ac1_e2e_untrusted_repo_makes_zero_gh_calls(proj: Project, repo: str) -> None:
    rc, res = proj.run_txn(proj.payload(repo=repo))
    assert rc == 1 and res["status"] == "failed_no_mutation" and res["mutation_started"] is False
    assert any("repo_untrusted" in e["message"] for e in res["errors"])
    assert proj.gh_calls == []  # not even `gh auth status` / `gh issue view`


def _run_backup(proj: Project, origin: str, issue: str = "34") -> subprocess.CompletedProcess[str]:
    work = proj.root.parent / "backup_work"
    if not work.exists():
        subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "remote", "remove", "origin"], capture_output=True, check=False
    )
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", origin], check=True, capture_output=True
    )
    script = (
        proj.root / ".claude" / "skills" / "edit-issue" / "scripts" / "backup-and-parse-issue.py"
    )
    return subprocess.run(
        [sys.executable, str(script), issue],
        cwd=work,
        env=proj.env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    "origin",
    [
        f"https://github.com/{LEGACY_SLUG}.git",
        "git@github.com:someone/agent-platform.git",
        f"https://evil.example/github.com/{TRUSTED}.git",
        f"ssh://git@evil.example/{TRUSTED}.git",
        f"git@evil.example:{TRUSTED}.git",
        "https://example.com/squne121/agent-platform.git",
    ],
    ids=[
        "legacy",
        "other_owner",
        "evil_host_path_trick",
        "evil_ssh_host",
        "evil_scp_host",
        "other_host",
    ],
)
def test_ac1_backup_script_untrusted_origin_makes_zero_gh_calls(proj: Project, origin: str) -> None:
    cp = _run_backup(proj, origin)
    assert cp.returncode == 1, cp.stderr
    assert "trusted" in cp.stderr
    assert proj.gh_calls == []
    assert not (
        proj.root.parent / "backup_work" / "tmp"
    ).exists()  # rejected before any local side effect


def test_ac1_backup_script_trusted_origin_backs_up_body_and_metadata(proj: Project) -> None:
    proj.set_state(title="the title")
    cp = _run_backup(proj, f"https://github.com/{TRUSTED}.git")
    assert cp.returncode == 0, cp.stderr
    meta_rel = json.loads(cp.stdout)["metadata_file"]
    work = proj.root.parent / "backup_work"
    meta = json.loads(Path(meta_rel).read_text(encoding="utf-8"))
    assert meta["repo"] == TRUSTED and meta["issue_number"] == 34 and meta["title"] == "the title"
    assert set(meta) == {
        "issue_number",
        "repo",
        "backup_file",
        "title",
    }  # body is deliberately not in metadata
    assert Path(meta["backup_file"]).read_text(encoding="utf-8") == OLD_BODY
    assert Path(meta["backup_file"]).parent == work / "tmp"
    assert [c[:2] for c in proj.gh_calls] == [["issue", "view"]]


@pytest.mark.parametrize("breakage", ["config_missing", "module_missing"])
def test_ac1_backup_script_unusable_identity_makes_zero_gh_calls(
    proj: Project, breakage: str
) -> None:
    target = {
        "config_missing": proj.root / "config" / "repository_identity.v1.json",
        "module_missing": proj.root / "scripts" / "agent-guards" / "repo_identity.py",
    }[breakage]
    target.unlink()
    cp = _run_backup(proj, f"https://github.com/{TRUSTED}.git")
    assert cp.returncode == 1 and "identity unavailable" in cp.stderr
    assert proj.gh_calls == []
    assert not list((proj.root.parent / "backup_work").glob("tmp/issue_*"))


@pytest.mark.parametrize(
    "value", ["abc", "1; rm -rf /", "-1", "1.5", "", "０１"], ids=lambda v: repr(v)
)
def test_ac1_backup_script_rejects_non_numeric_issue_number(proj: Project, value: str) -> None:
    cp = _run_backup(proj, f"https://github.com/{TRUSTED}.git", issue=value)
    assert cp.returncode == 2  # argparse usage error, before any git/gh work
    assert proj.gh_calls == []


# --------------------------------------------------------------------------- AC2: transaction positive / negative

BODY_FIELDS = {"state", "body", "title"}


def _comment_mode(proj: Project, marker: str = "<!-- m1 -->") -> dict:
    (proj.root / "tmp").mkdir(exist_ok=True)
    (proj.root / "tmp" / "comment.md").write_text(f"hello {marker}", encoding="utf-8")
    return {"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": marker}


def test_ac2_valid_body_update_writes_once_and_reads_back_fresh(proj: Project) -> None:
    rc, res = proj.run_txn(proj.payload())
    assert rc == 0 and res["status"] == "ok" and res["mutation_started"] is True
    assert res["errors"] == [] and res["content_update"]["mutation_outcome"] == "applied"
    assert (
        proj.state["body"] == VALID_BODY
        and proj.state["patches"] == 1
        and proj.state["title"] == "old title"
    )
    assert res["body_update"]["new_body_sha256"] == res["body_update"]["remote_current_body_sha256"]
    assert res["body_update"]["new_body_sha256"] == f"sha256:{_sha(VALID_BODY)}"
    # a fresh `issue view` readback is the LAST gh call, after the only write
    writes = proj.gh_writes
    assert len(writes) == 1 and writes[0][writes[0].index("--method") + 1] == "PATCH"
    calls = proj.gh_calls
    assert calls[-1][:2] == ["issue", "view"] and calls.index(writes[0]) < len(calls) - 1


def test_ac2_title_only_update_preserves_body(proj: Project) -> None:
    title = {"required": True, "proposed_title": "new title", "reason": "retitle"}
    rc, res = proj.run_txn(proj.payload(OLD_BODY, title_update=title))
    assert rc == 0 and res["status"] == "ok"
    assert (
        proj.state["title"] == "new title"
        and proj.state["body"] == OLD_BODY
        and proj.state["patches"] == 1
    )
    assert res["content_update"]["requested_title"] == "new title"


def test_ac2_body_and_title_update_in_one_patch(proj: Project) -> None:
    title = {"required": True, "proposed_title": "both", "reason": "retitle"}
    rc, res = proj.run_txn(proj.payload(VALID_BODY, title_update=title))
    assert rc == 0 and res["status"] == "ok"
    assert (
        proj.state["title"] == "both"
        and proj.state["body"] == VALID_BODY
        and proj.state["patches"] == 1
    )


@pytest.mark.parametrize(
    "over",
    [
        {"expected_previous_body_sha256": "sha256:" + "0" * 64},
        {"expected_previous_updated_at": "2020-01-01T00:00:00Z"},
    ],
    ids=["stale_body_sha", "stale_updated_at"],
)
def test_ac2_stale_precondition_is_failed_no_mutation(proj: Project, over: dict) -> None:
    rc, res = proj.run_txn(proj.payload(**over))
    _assert_failed_no_mutation(proj, rc, res, "stale_precondition_before_mutation")


def test_ac2_stale_title_via_remote_drift_is_failed_no_mutation(proj: Project) -> None:
    """The remote body drifted after the caller computed its precondition."""
    data = proj.payload()
    proj.set_state(body="somebody else edited this\n")
    rc, res = proj.run_txn(data)
    assert rc == 1 and res["status"] == "failed_no_mutation" and proj.gh_writes == []
    assert proj.state["body"] == "somebody else edited this\n"


def test_ac2_no_change_makes_no_write(proj: Project) -> None:
    rc, res = proj.run_txn(proj.payload(OLD_BODY))
    assert rc == 0 and res["status"] == "no_change" and res["mutation_started"] is False
    assert proj.gh_writes == [] and proj.state["patches"] == 0


@pytest.mark.parametrize("mode", ["noop", "fail"], ids=["silently_not_applied", "write_failed"])
def test_ac2_unconfirmed_write_is_never_ok_and_never_failed_no_mutation(
    proj: Project, mode: str
) -> None:
    proj.set_state(patch_mode=mode)
    rc, res = proj.run_txn(proj.payload())
    assert rc == 1
    assert (
        res["status"] == "mutation_outcome_unknown"
    )  # readback could not confirm the requested body
    assert res["status"] not in {"ok", "no_change", "failed_no_mutation"}
    assert (
        res["content_update"]["patch_attempted"] is True
        and res["content_update"]["mutation_outcome"] == "unknown"
    )
    assert (
        proj.state["body"] == OLD_BODY and proj.state["patches"] == 1
    )  # exactly one attempt, no blind retry


def test_ac2_write_applied_but_reported_failure_is_reconciled_by_readback(proj: Project) -> None:
    """Partial application: the write reached GitHub but the client saw an error. Only the fresh
    readback matching the requested body may turn this into `ok`; the write is not repeated."""
    proj.set_state(patch_mode="applied_then_fail")
    rc, res = proj.run_txn(proj.payload())
    assert (rc, res["status"]) == (0, "ok")
    assert proj.state["body"] == VALID_BODY and proj.state["patches"] == 1


def test_ac2_comment_publish_posts_once_and_is_idempotent_by_marker(proj: Project) -> None:
    cm = _comment_mode(proj)
    rc, res = proj.run_txn(proj.payload(OLD_BODY, comment_mode=cm))
    assert (rc, res["status"]) == (0, "ok")
    assert res["comment_publish"]["status"] == "ok" and res["comment_publish"]["comment_url"]
    assert res["comment_publish"]["comment_body_sha256"].endswith(
        _sha(proj.state["comments"][0]["body"])
    )
    assert proj.state["posts"] == 1 and proj.state["patches"] == 0
    # same marker again -> no second POST, still exactly one comment
    rc2, res2 = proj.run_txn(proj.payload(OLD_BODY, comment_mode=cm))
    assert (rc2, res2["status"]) == (0, "ok")
    assert proj.state["posts"] == 1 and len(proj.state["comments"]) == 1


def test_ac2_comment_publish_failure_is_failed_after_mutation(proj: Project) -> None:
    proj.set_state(post_mode="fail")
    rc, res = proj.run_txn(proj.payload(OLD_BODY, comment_mode=_comment_mode(proj, "<!-- m2 -->")))
    assert rc == 1 and res["status"] == "failed_after_mutation" and res["mutation_started"] is True
    assert res["comment_publish"]["status"] == "failed"
    assert any(e["code"] == "issue_comment_publish_failed" for e in res["errors"])
    assert proj.state["comments"] == []


def test_ac2_comment_failure_after_body_update_is_failed_after_mutation(proj: Project) -> None:
    proj.set_state(post_mode="fail")
    rc, res = proj.run_txn(
        proj.payload(VALID_BODY, comment_mode=_comment_mode(proj, "<!-- m4 -->"))
    )
    assert rc == 1 and res["status"] == "failed_after_mutation" and res["mutation_started"] is True
    assert res["body_update"]["status"] == "ok" and res["comment_publish"]["status"] == "failed"
    assert proj.state["body"] == VALID_BODY and proj.state["comments"] == []


def test_ac2_body_update_then_comment_publishes_both(proj: Project) -> None:
    rc, res = proj.run_txn(
        proj.payload(VALID_BODY, comment_mode=_comment_mode(proj, "<!-- m3 -->"))
    )
    assert (rc, res["status"]) == (0, "ok")
    assert proj.state["body"] == VALID_BODY and len(proj.state["comments"]) == 1
    assert res["body_update"]["status"] == "ok" and res["comment_publish"]["status"] == "ok"


@pytest.mark.parametrize("status", ["human_judgment", "input_or_runtime_error"])
def test_ac2_forwarded_readiness_requiring_a_human_stops_with_zero_gh(
    proj: Project, status: str
) -> None:
    data = proj.payload()
    data["readiness_forwarding_payload"]["readiness_result"]["status"] = status
    rc, res = proj.run_txn(data)
    assert rc == 1 and res["status"] == "human_judgment" and res["mutation_started"] is False
    assert proj.gh_calls == []


def test_ac2_forwarded_needs_fix_without_evidence_on_open_issue_is_rejected(proj: Project) -> None:
    data = proj.payload()
    data["readiness_forwarding_payload"]["readiness_result"]["status"] = "needs_fix"
    rc, res = proj.run_txn(data)
    _assert_failed_no_mutation(proj, rc, res, "readiness_needs_fix_without_resolution_evidence")


def test_ac2_candidate_failing_the_real_guard_is_rejected_before_mutation(proj: Project) -> None:
    rc, res = proj.run_txn(proj.payload("## Outcome\n\nnot a contract body\n"))
    _assert_failed_no_mutation(proj, rc, res, "guard_or_readiness_failed_before_mutation")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("repo"),
        lambda d: d.pop("expected_previous_body_sha256"),
        lambda d: d.pop("readiness_forwarding_payload"),
        lambda d: d.__setitem__("issue_number", "1287"),
        lambda d: d.__setitem__("issue_number", True),
        lambda d: d.__setitem__("schema", "ISSUE_EDIT_TXN_INPUT_V2"),
        lambda d: d.__setitem__("surprise", 1),
        lambda d: d["comment_mode"].__setitem__("mode", "delete"),
        lambda d: d.__setitem__("comment_mode", {"mode": "publish"}),
        lambda d: d.__setitem__(
            "title_update", {"required": True, "proposed_title": " ", "reason": "r"}
        ),
        lambda d: d.__setitem__(
            "title_update", {"required": False, "proposed_title": "x", "reason": None}
        ),
    ],
    ids=[
        "no_repo",
        "no_sha",
        "no_readiness",
        "str_issue_number",
        "bool_issue_number",
        "wrong_schema",
        "unknown_key",
        "bad_comment_mode",
        "publish_without_file",
        "blank_title",
        "title_not_required_but_set",
    ],
)
def test_ac2_invalid_input_makes_zero_gh_calls(proj: Project, mutate) -> None:
    data = proj.payload()
    mutate(data)
    rc, res = proj.run_txn(data)
    assert rc == 1 and res["status"] == "failed_no_mutation" and res["mutation_started"] is False
    assert res["errors"][0]["code"] == "txn_input_or_runtime_error"
    assert proj.gh_calls == []


@pytest.mark.parametrize("new_body_file", ["../outside.md", "tmp/does_not_exist.md", "/etc/passwd"])
def test_ac2_unusable_new_body_file_never_writes(proj: Project, new_body_file: str) -> None:
    """The candidate file is resolved after the (read-only) issue readback; either way no write happens."""
    rc, res = proj.run_txn(proj.payload(new_body_file=new_body_file))
    _assert_failed_no_mutation(proj, rc, res)
    assert res["errors"][0]["code"] == "txn_input_or_runtime_error"


@pytest.mark.parametrize(
    "content", ["{not json", "[]", '"str"', ""], ids=["malformed", "array", "string", "empty"]
)
def test_ac2_malformed_input_file_makes_zero_gh_calls(proj: Project, content: str) -> None:
    (proj.root / "tmp").mkdir(exist_ok=True)
    (proj.root / "tmp" / "bad.json").write_text(content, encoding="utf-8")
    rc, res = proj.run_txn(input_rel="tmp/bad.json")
    assert rc == 1 and res["status"] == "failed_no_mutation" and proj.gh_calls == []


def test_ac2_missing_input_file_is_a_bounded_failed_no_mutation(proj: Project) -> None:
    rc, res = proj.run_txn(input_rel="tmp/nope.json")
    assert rc == 1 and res["status"] == "failed_no_mutation" and proj.gh_calls == []


def test_ac2_unreadable_remote_issue_is_failed_no_mutation(proj: Project) -> None:
    data = proj.payload()
    proj.set_state(
        number=999
    )  # fake gh now serves a different issue: `issue view` keeps working, PATCH 404s
    (proj.root / "tmp" / "x").write_text("", encoding="utf-8")
    broken = proj.root / "fakebin" / "gh"
    broken.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stderr.write('HTTP 502')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    rc, res = proj.run_txn(data)
    assert (
        rc == 1
        and res["status"] == "failed_no_mutation"
        and any(e["code"] == "issue_readback_failed" for e in res["errors"])
    )


# in-process: timeout / partial application where a real subprocess cannot be timed deterministically


def _scripted(
    monkeypatch: pytest.MonkeyPatch,
    repo_tmp: Path,
    *,
    hygiene=(1, "", ""),
    controlled=(0, "{}", ""),
    live=None,
):
    live = live or {
        "title": "t",
        "body": "old issue body",
        "updatedAt": UPDATED_AT,
        "state": "OPEN",
    }
    log: list[tuple[str, list[str]]] = []

    def _run(args, *, env=None):
        args = [str(a) for a in args]
        name = (
            Path(args[1]).name
            if len(args) > 1 and args[0] == sys.executable
            else Path(args[0]).name
        )
        if args[1:3] == ["issue", "view"]:
            name = "gh-issue-view"
        log.append((name, args))
        if name == "gh-issue-view":
            return _CP(0, json.dumps(live))
        if name == "guard-issue-body.py":
            return _CP(0, "{}")
        if name == "issue_contract_hygiene_autofix.py":
            return _CP(*hygiene)
        if name == "contract_readiness_check.py":
            return _CP(0, "{}")
        if name == "controlled_skill_mutation_exec.py":
            return _CP(*controlled)
        raise AssertionError(f"unexpected child command: {args}")

    monkeypatch.setattr(txn, "_run_command", _run)
    for attr, fname in (
        ("GUARD_SCRIPT", "guard-issue-body.py"),
        ("HYGIENE_SCRIPT", "issue_contract_hygiene_autofix.py"),
        ("READINESS_SCRIPT", "contract_readiness_check.py"),
        ("CONTROLLED_EXEC", "controlled_skill_mutation_exec.py"),
    ):
        monkeypatch.setattr(txn, attr, repo_tmp / fname)
    return log


def test_ac2_controlled_executor_timeout_is_not_failed_no_mutation(
    repo_tmp: Path, monkeypatch
) -> None:
    log = _scripted(monkeypatch, repo_tmp, controlled=(124, "", "child command timeout after 30s"))
    result = txn.run_transaction(_payload(repo_tmp))
    assert result["status"] in {"mutation_outcome_unknown", "failed_after_mutation"}
    assert (
        result["body_update"]["attempted"] is True and result["body_update"]["status"] == "failed"
    )
    assert any(e["code"] == "issue_body_update_failed" for e in result["errors"])
    assert any(
        n == "controlled_skill_mutation_exec.py" for n, _ in log
    )  # the write was attempted exactly via the executor
    assert [n for n, _ in log].count(
        "controlled_skill_mutation_exec.py"
    ) == 1  # and never blindly retried


# --------------------------------------------------------------------------- AC3: native_relationships unapproved

_NOOP_PARENT = {"action": "unchanged"}
NATIVE_INPUTS = {
    "empty_object": {},
    "noop_shape": {
        "parent": _NOOP_PARENT,
        "add_blocked_by": [],
        "remove_blocked_by": [],
        "add_blocking": [],
        "remove_blocking": [],
    },
    "expected_before_only": {"expected_before": {"parent": None, "blocked_by": [], "blocking": []}},
    "parent_set": {"parent": {"action": "set", "issue_number": 5}},
    "parent_remove": {"parent": {"action": "remove", "issue_number": None}},
    "add_blocked_by": {"add_blocked_by": [7]},
    "remove_blocking": {"remove_blocking": [8]},
    "unknown_key": {"surprise": True},
    "not_an_object": "set-parent",
    "list": [],
    "zero": 0,
    "false": False,
}


@pytest.mark.parametrize("native", list(NATIVE_INPUTS.values()), ids=list(NATIVE_INPUTS))
def test_ac3_native_relationships_rejected_before_any_child_command(
    repo_tmp: Path, calls, native
) -> None:
    with pytest.raises(ValueError, match="native_relationships_unapproved"):
        txn.run_transaction(_payload(repo_tmp, native_relationships=native))
    assert calls == []


def test_ac3_source_native_handlers_are_unreachable_from_the_entrypoint(
    repo_tmp: Path, calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source `issue_relationship.update` handlers still exist; existence is not approval."""
    for name in (
        "_prepare_native_relationship",
        "_execute_native_relationship",
        "_relationship_capability_preflight",
        "_fetch_native_relationships",
        "_finalize_native_relationships",
    ):
        assert hasattr(txn, name)
        monkeypatch.setattr(txn, name, lambda *_a, _n=name, **_k: pytest.fail(f"{_n} must not run"))
    with pytest.raises(ValueError, match="native_relationships_unapproved"):
        txn.run_transaction(_payload(repo_tmp, native_relationships=NATIVE_INPUTS["noop_shape"]))
    assert calls == []


def test_ac3_null_native_relationships_is_the_same_as_absent(repo_tmp: Path) -> None:
    """`null` carries no structured input, so it keeps the pre-#1883 (body/comment-only) behaviour."""
    txn._validate_input_payload(_payload(repo_tmp, native_relationships=None))


@pytest.mark.parametrize("native", list(NATIVE_INPUTS.values()), ids=list(NATIVE_INPUTS))
def test_ac3_e2e_native_relationships_make_zero_gh_calls_and_no_artifacts(
    proj: Project, native
) -> None:
    rc, res = proj.run_txn(proj.payload(native_relationships=native))
    assert rc == 1 and res["status"] == "failed_no_mutation" and res["mutation_started"] is False
    assert any("native_relationships_unapproved" in e["message"] for e in res["errors"])
    assert res["native_relationships"]["attempted"] is False
    assert proj.gh_calls == []  # no auth status, no issue view, no GraphQL
    assert not (proj.root / "artifacts").exists()  # no executor input file was ever written


def test_ac3_e2e_valid_native_input_does_not_leak_into_a_content_write(proj: Project) -> None:
    rc, _ = proj.run_txn(proj.payload(native_relationships=NATIVE_INPUTS["parent_set"]))
    assert rc == 1 and proj.state["patches"] == 0 and proj.state["body"] == OLD_BODY


def test_ac3_no_unapproved_command_id_is_invoked_by_the_transaction() -> None:
    """Every controlled-executor command id the helper can request is on the approved list."""
    source = (TXN_SCRIPTS / "edit_issue_txn.py").read_text(encoding="utf-8")
    ids = set(re.findall(r'"(issue_[a-z_]+\.(?:update|publish|remove|materialize))"', source))
    approved = {"issue_body.update", "issue_comment.publish", "issue_content.update"}
    # `issue_relationship.update` is referenced only inside the (unreachable) native handlers.
    assert ids - approved <= {"issue_relationship.update"}
    native_src = source.split("def _relationship_capability_preflight", 1)[1].split(
        "def _safe_repo_file", 1
    )[0]
    outside = source.replace(native_src, "")
    assert '"issue_relationship.update"' in native_src
    assert '"issue_relationship.update"' not in outside


# --------------------------------------------------------------------------- AC8: hygiene exit 2 and friends

HYGIENE_REJECTED = {
    "exit_2_unsafe_or_checker_error": (
        (2, "", "[ERROR] check_error"),
        "issue_contract_hygiene_unsafe_or_checker_error",
    ),
    "exit_2_compiler_load_failed": (
        (2, "", "[ERROR] vc_baseline_shape_compiler could not be loaded"),
        "issue_contract_hygiene_unsafe_or_checker_error",
    ),
    "exit_2_silent": ((2, "", ""), "issue_contract_hygiene_unsafe_or_checker_error"),
    "timeout_124": ((124, "", "child command timeout after 30s"), "issue_contract_hygiene_timeout"),
    "exit_3": ((3, "", "boom"), "issue_contract_hygiene_runtime_error"),
    "exit_127": ((127, "", "not found"), "issue_contract_hygiene_runtime_error"),
    "killed_by_signal": ((-9, "", ""), "issue_contract_hygiene_runtime_error"),
    "exit_1_uncaught_traceback": (
        (1, "", "Traceback (most recent call last):\n  File x\nValueError"),
        "issue_contract_hygiene_runtime_error",
    ),
    "exit_0_with_stdout": (
        (0, "body leaked to stdout", ""),
        "issue_contract_hygiene_invalid_output",
    ),
    "exit_1_with_stdout": (
        (1, '{"unexpected": true}', ""),
        "issue_contract_hygiene_invalid_output",
    ),
}


@pytest.mark.parametrize(
    ("hygiene", "code"), list(HYGIENE_REJECTED.values()), ids=list(HYGIENE_REJECTED)
)
def test_ac8_hygiene_failure_is_failed_no_mutation_before_readiness_and_executor(
    repo_tmp: Path, monkeypatch, hygiene, code: str
) -> None:
    log = _scripted(monkeypatch, repo_tmp, hygiene=hygiene)
    result = txn.run_transaction(_payload(repo_tmp))
    assert result["status"] == "failed_no_mutation" and result["mutation_started"] is False
    assert (
        result["body_update"]["attempted"] is False
        and result["comment_publish"]["attempted"] is False
    )
    assert [e["code"] for e in result["errors"]] == [code]
    names = [n for n, _ in log]
    assert names == ["gh-issue-view", "guard-issue-body.py", "issue_contract_hygiene_autofix.py"]
    # order guard -> hygiene is preserved, and nothing after hygiene ran: no readiness, no executor
    assert (
        "contract_readiness_check.py" not in names
        and "controlled_skill_mutation_exec.py" not in names
    )


@pytest.mark.parametrize("rc", [0, 1], ids=["exit_0_repaired", "exit_1_no_repair"])
def test_ac8_hygiene_success_codes_proceed_to_readiness_then_the_executor(
    repo_tmp: Path, monkeypatch, rc: int
) -> None:
    log = _scripted(
        monkeypatch, repo_tmp, hygiene=(rc, "", "status: x"), controlled=(1, "", "stop")
    )
    txn.run_transaction(_payload(repo_tmp))
    names = [n for n, _ in log]
    assert names[:4] == [
        "gh-issue-view",
        "guard-issue-body.py",
        "issue_contract_hygiene_autofix.py",
        "contract_readiness_check.py",
    ]
    assert names[4] == "controlled_skill_mutation_exec.py"


def test_ac8_guard_failure_means_hygiene_never_runs(repo_tmp: Path, monkeypatch) -> None:
    log = _scripted(monkeypatch, repo_tmp)
    original = txn._run_command

    def _guard_fails(args, *, env=None):
        if len(args) > 1 and Path(str(args[1])).name == "guard-issue-body.py":
            log.append(("guard-issue-body.py", [str(a) for a in args]))
            return _CP(1, "", "guard failed")
        return original(args, env=env)

    monkeypatch.setattr(txn, "_run_command", _guard_fails)
    result = txn.run_transaction(_payload(repo_tmp))
    assert result["status"] == "failed_no_mutation"
    assert [e["code"] for e in result["errors"]] == ["guard_or_readiness_failed_before_mutation"]
    assert "issue_contract_hygiene_autofix.py" not in [n for n, _ in log]


def test_ac8_hygiene_exit_codes_are_the_only_documented_success_set() -> None:
    cp = lambda rc: subprocess.CompletedProcess([], rc, stdout="", stderr="")
    assert {rc for rc in range(-10, 200) if txn._hygiene_rejection(cp(rc)) is None} == {0, 1}


def _break_hygiene_dependency(proj: Project, rel: str) -> None:
    (proj.root / rel).unlink()


E2E_HYGIENE_BREAKAGE = {
    "compiler_missing": ".claude/skills/issue-contract-review/scripts/vc_baseline_shape_compiler.py",
    "checker_missing": ".claude/skills/review-issue/scripts/check_issue_contract.py",
}


@pytest.mark.parametrize("rel", list(E2E_HYGIENE_BREAKAGE.values()), ids=list(E2E_HYGIENE_BREAKAGE))
def test_ac8_e2e_real_hygiene_exit_2_is_rejected_with_zero_github_writes(
    proj: Project, rel: str
) -> None:
    # positive control first: the same candidate passes the real guard + hygiene (exit 1) + readiness
    ok_rc, ok_res = proj.run_txn(proj.payload())
    assert (ok_rc, ok_res["status"]) == (0, "ok")
    proj.set_state(body=OLD_BODY, patches=0, updated_at=UPDATED_AT)
    proj.log_path.write_text("", encoding="utf-8")
    _break_hygiene_dependency(proj, rel)
    # the real hygiene script now exits 2 (compiler_load_failed / check_error)
    direct = subprocess.run(
        [
            sys.executable,
            str(proj.root / ".claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py"),
            "--body-file",
            str(proj.root / "tmp" / "new_body.md"),
        ],
        cwd=proj.root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert direct.returncode == 2, direct.stderr
    rc, res = proj.run_txn(proj.payload())
    _assert_failed_no_mutation(proj, rc, res, "issue_contract_hygiene_unsafe_or_checker_error")
    assert not [c for c in proj.gh_calls if c[:1] == ["api"] and "--method" in c]


def _parent_kind_body() -> str:
    text = (ROOT / ".claude/skills/edit-issue/tests/test_edit_issue_txn.py").read_text(
        encoding="utf-8"
    )
    start = text.index('parent_body = """') + len('parent_body = """')
    return text[start : text.index('"""', start)]


def test_ac8_e2e_parent_kind_body_hitting_c9_exit_2_is_rejected(proj: Project) -> None:
    """Known consequence of AC8 (source test_1844 is deferred because of it): a parent-kind candidate without
    Allowed Paths / Runtime Verification Applicability makes the real hygiene exit 2, which is now refused
    instead of tolerated. Pinned so a hygiene follow-up changes this deliberately."""
    body = _parent_kind_body()
    (proj.root / "tmp").mkdir(exist_ok=True)
    (proj.root / "tmp" / "parent.md").write_text(body, encoding="utf-8")
    direct = subprocess.run(
        [
            sys.executable,
            str(proj.root / ".claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py"),
            "--body-file",
            str(proj.root / "tmp" / "parent.md"),
        ],
        cwd=proj.root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert direct.returncode == 2 and "Allowed Paths section is missing" in direct.stderr
    rc, res = proj.run_txn(proj.payload(body))
    _assert_failed_no_mutation(proj, rc, res, "issue_contract_hygiene_unsafe_or_checker_error")


def test_ac8_e2e_real_hygiene_exit_1_and_exit_0_are_both_accepted(proj: Project) -> None:
    rc, res = proj.run_txn(proj.payload())  # VALID_BODY needs no repair -> hygiene exit 1
    assert (rc, res["status"]) == (0, "ok")
    direct = subprocess.run(
        [
            sys.executable,
            str(proj.root / ".claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py"),
            "--body-file",
            str(proj.root / "tmp" / "new_body.md"),
        ],
        cwd=proj.root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert direct.returncode == 1


# --------------------------------------------------------------------------- AC4: Agent / Skill static parity

AGENT = ROOT / ".claude" / "agents" / "issue-editor.md"
SKILL = ROOT / ".claude" / "skills" / "edit-issue" / "SKILL.md"
MANIFEST = ROOT / "docs" / "dev" / "stage2-4-edit-txn-manifest.json"


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


def test_ac4_issue_editor_frontmatter_equals_the_recorded_source_frontmatter() -> None:
    front, _ = _frontmatter(AGENT)
    recorded = _manifest()["agent_skill_parity"]["issue_editor_source_frontmatter"]
    expected = dict(recorded)
    assert front == expected, "issue-editor.md frontmatter drifted from the source tag"


def test_ac4_issue_editor_tools_disallowed_permission_and_skills() -> None:
    front, _ = _frontmatter(AGENT)
    assert front["tools"] == ["Bash", "Read"]  # exact set; no Skill / Agent / Write / Edit
    assert set(front["disallowedTools"]) == {"Agent", "Edit", "MultiEdit", "Write", "Skill"}
    assert not {"Skill", "Agent", "Edit", "MultiEdit", "Write"} & set(front["tools"])
    assert front["skills"] == ["edit-issue"]
    assert front["permissionMode"] == "acceptEdits"
    assert front["model"] == "sonnet" and front["name"] == "issue-editor"


def test_ac4_permission_mode_is_not_a_permission_bypass() -> None:
    front, _ = _frontmatter(AGENT)
    assert front["permissionMode"] not in {"bypassPermissions", "dontAsk", "auto"}
    # acceptEdits only auto-accepts file-edit tools; this agent has none (Edit/MultiEdit/Write disallowed),
    # so the mode grants nothing beyond Bash/Read, which stay subject to the normal permission rules.
    assert {"Edit", "MultiEdit", "Write"} <= set(front["disallowedTools"])


def test_ac4_skills_preload_is_declared_separately_from_tool_permission() -> None:
    front, body = _frontmatter(AGENT)
    assert "skills" in front and "Skill" not in front["tools"]
    assert "preload の宣言であり、実行時アクセス制御ではない" in body
    assert "`Skill` を含まない" in body


def test_ac4_nested_delegation_is_structurally_blocked_by_frontmatter() -> None:
    front, body = _frontmatter(AGENT)
    assert "Agent" in front["disallowedTools"] and "Agent" not in front["tools"]
    assert "Skill" in front["disallowedTools"] and "Skill" not in front["tools"]
    assert "nested Skill invocation は構造的に不可能" in body


def test_ac4_raw_mutation_ban_is_documented_as_procedural_not_technical() -> None:
    _, body = _frontmatter(AGENT)
    skill_text = SKILL.read_text(encoding="utf-8")
    assert "procedural contract" in body and "技術的強制ではない" in body
    assert "Bash tool レベルでの技術的な強制ではない" in body
    assert "技術的に拒否するとは主張しない" in body
    # no claim that raw gh mutation is technically blocked
    for text in (body, skill_text):
        assert not re.search(
            r"(Bash tool|Bash)[^。\n]*(技術的に|物理的に)(拒否|遮断|ブロック)(する|される|している)(?!とは)",
            text,
        )
    for token in (
        "gh issue edit",
        "gh issue comment",
        "gh api --method PATCH",
        "gh api --method POST",
    ):
        assert token not in body and token not in skill_text


def test_ac4_skill_frontmatter_matches_source_and_names_the_canonical_docs() -> None:
    front, body = _frontmatter(SKILL)
    recorded = _manifest()["agent_skill_parity"]["edit_issue_skill_source_frontmatter_sha256"]
    assert (
        hashlib.sha256(
            yaml.safe_dump(front, allow_unicode=True, sort_keys=True).encode()
        ).hexdigest()
        == recorded
    )
    assert front["name"] == "edit-issue"
    assert "docs/dev/agent-skill-boundaries.md" in body
    assert "edit_issue_txn.py" in body


def test_ac4_title_update_and_native_documentation_match_the_implementation(repo_tmp: Path) -> None:
    """Documented contract == executable contract (intentional diff vs the source Agent text)."""
    _, body = _frontmatter(AGENT)
    skill_text = SKILL.read_text(encoding="utf-8")
    assert "v1 scope 外" not in body  # source claimed title_update.required==true was out of scope
    assert "title_update.required == true" in body and "2026-10-25" in body
    for text in (body, skill_text):
        assert "未承認" in text and "native_relationships" in text
    # the executable side agrees: a required title update is accepted, native input is not
    txn._validate_input_payload(
        _payload(repo_tmp, title_update={"required": True, "proposed_title": "t", "reason": "r"})
    )
    with pytest.raises(ValueError, match="native_relationships_unapproved"):
        txn._validate_input_payload(_payload(repo_tmp, native_relationships={}))


def test_ac4_output_budget_is_referenced_and_defined() -> None:
    _, body = _frontmatter(AGENT)
    assert (
        "OUTPUT_BUDGET_V1" in body and "docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1" in body
    )
    docs = SOURCE_DOC.read_text(encoding="utf-8")
    section = docs.split("## OUTPUT_BUDGET_V1", 1)[1].split("\n## ", 1)[0]
    budget = yaml.safe_load(section.split("```yaml", 1)[1].split("```", 1)[0])["OUTPUT_BUDGET_V1"]
    assert budget["max_human_summary_lines"] == 30 and budget["max_human_summary_chars"] == 2400
    assert budget["machine_yaml"]["required_schema_fields"] == "must_include_all"
    # the budget is stated to apply to every agent and every SKILL.md, which includes both migrated files
    assert ".claude/agents/*.md" in section and ".claude/skills/*/SKILL.md" in section


# --------------------------------------------------------------------------- AC5: real subprocess wiring

PREFLIGHT_DRIVER = """
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / ".claude/skills/issue-refinement-loop/scripts"))
import run_refinement_preflight as p
args = json.loads(sys.argv[2])
res = p._dispatch_candidate_body_default_transaction(
    current_issue_=args["current"], candidate_body=Path(args["candidate_file"]).read_text(encoding="utf-8"),
    root=Path(sys.argv[1]), issue_number=args["issue_number"], repo=args["repo"])
print(json.dumps(res))
"""


def _dispatch_from_preflight(proj: Project, tmp_path: Path, body: str, repo: str = TRUSTED) -> dict:
    cand = tmp_path / "cand.md"
    cand.write_text(body, encoding="utf-8")
    st = proj.state
    args = {
        "current": {
            "body": st["body"],
            "updatedAt": st["updated_at"],
            "title": st["title"],
            "state": "OPEN",
        },
        "candidate_file": str(cand),
        "issue_number": ISSUE,
        "repo": repo,
    }
    cp = subprocess.run(
        [sys.executable, "-c", PREFLIGHT_DRIVER, str(proj.root), json.dumps(args)],
        cwd=proj.root,
        env=proj.env(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout.strip().splitlines()[-1])


def test_ac5_preflight_script_reference_resolves_to_the_migrated_txn() -> None:
    preflight = (
        ROOT / ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py"
    ).read_text(encoding="utf-8")
    assert 'EDIT_ISSUE_TXN_SCRIPT_REL = "edit-issue/scripts/edit_issue_txn.py"' in preflight
    assert (ROOT / ".claude" / "skills" / "edit-issue" / "scripts" / "edit_issue_txn.py").is_file()


def test_ac5_preflight_dispatch_runs_the_real_txn_subprocess_against_fake_gh(
    proj: Project, tmp_path: Path
) -> None:
    result = _dispatch_from_preflight(proj, tmp_path, VALID_BODY)
    assert result["schema"] == "ISSUE_EDIT_TXN_RESULT_V1" and result["status"] == "ok"
    assert proj.state["body"] == VALID_BODY and proj.state["patches"] == 1
    assert len(proj.gh_writes) == 1  # the one controlled PATCH; no raw `gh issue edit`
    assert not [c for c in proj.gh_calls if c[:2] in (["issue", "edit"], ["issue", "comment"])]


def test_ac5_preflight_dispatch_with_an_untrusted_repo_reaches_zero_gh_calls(
    proj: Project, tmp_path: Path
) -> None:
    result = _dispatch_from_preflight(proj, tmp_path, VALID_BODY, repo=LEGACY_SLUG)
    assert result["status"] == "failed_no_mutation"
    assert proj.gh_calls == [] and proj.state["patches"] == 0


def test_ac5_preflight_dispatch_surfaces_a_stale_remote_as_failed_no_mutation(
    proj: Project, tmp_path: Path
) -> None:
    cand = tmp_path / "cand.md"
    cand.write_text(VALID_BODY, encoding="utf-8")
    st = proj.state
    stale = {
        "body": "an older body\n",
        "updatedAt": st["updated_at"],
        "title": st["title"],
        "state": "OPEN",
    }
    args = {"current": stale, "candidate_file": str(cand), "issue_number": ISSUE, "repo": TRUSTED}
    cp = subprocess.run(
        [sys.executable, "-c", PREFLIGHT_DRIVER, str(proj.root), json.dumps(args)],
        cwd=proj.root,
        env=proj.env(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert cp.returncode == 0, cp.stderr
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert (
        result["status"] == "failed_no_mutation"
        and proj.gh_writes == []
        and proj.state["body"] == OLD_BODY
    )


# --------------------------------------------------------------------------- AC7: canonical schema excerpt


def _doc_section(name: str) -> str:
    return (
        SOURCE_DOC.read_text(encoding="utf-8")
        .split(f"### {name}", 1)[1]
        .split("\n### ", 1)[0]
        .split("\n## ", 1)[0]
    )


def _doc_schema(name: str) -> dict:
    section = _doc_section(name)
    return yaml.safe_load(section.split("```yaml", 1)[1].split("```", 1)[0])


def _validator(schema: dict) -> jsonschema.Draft202012Validator:
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _valid_input() -> dict:
    return {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": ISSUE,
        "repo": TRUSTED,
        "new_body_file": "tmp/new_body.md",
        "readiness_forwarding_payload": {
            "readiness_result": {
                "status": "go",
                "body_sha256": "sha256:x",
                "source_checks": ["c"],
                "errors": [],
                "readiness_result_ref": "r",
            }
        },
        "comment_mode": {"mode": "skip"},
        "expected_previous_body_sha256": "sha256:y",
        "expected_previous_updated_at": UPDATED_AT,
        "title_update": {"required": False, "proposed_title": None, "reason": None},
    }


def test_ac7_both_schema_blocks_parse_as_valid_json_schema() -> None:
    for name in ("ISSUE_EDIT_TXN_INPUT_V1", "ISSUE_EDIT_TXN_RESULT_V1"):
        schema = _doc_schema(name)
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object" and schema["additionalProperties"] is False


def test_ac7_input_schema_accepts_the_valid_instance_and_the_txn_agrees() -> None:
    schema = _doc_schema("ISSUE_EDIT_TXN_INPUT_V1")
    instance = _valid_input()
    _validator(schema).validate(instance)
    txn._validate_input_payload(
        instance
    )  # same instance passes the executable validator (repo is trusted)


def test_ac7_input_properties_equal_the_validators_closed_key_set() -> None:
    schema = _doc_schema("ISSUE_EDIT_TXN_INPUT_V1")
    assert set(schema["properties"]) == set(txn.TOP_LEVEL_KEYS)


# The schema requires these two keys but the executable validator defaults them (comment_mode -> skip,
# title_update -> not required). The leniency is a pinned source behaviour, not a schema change.
VALIDATOR_DEFAULTS_ABSENT_KEY = {"comment_mode", "title_update"}


@pytest.mark.parametrize("key", _doc_schema("ISSUE_EDIT_TXN_INPUT_V1")["required"])
def test_ac7_each_schema_required_key_is_enforced(key: str) -> None:
    instance = _valid_input()
    del instance[key]
    assert list(_validator(_doc_schema("ISSUE_EDIT_TXN_INPUT_V1")).iter_errors(instance))
    if key in VALIDATOR_DEFAULTS_ABSENT_KEY:
        txn._validate_input_payload(instance)
    else:
        with pytest.raises(ValueError):
            txn._validate_input_payload(instance)


def test_ac7_input_schema_is_closed_at_every_object_level() -> None:
    schema = _doc_schema("ISSUE_EDIT_TXN_INPUT_V1")
    validator = _validator(schema)
    for path in (
        [],
        ["readiness_forwarding_payload"],
        ["readiness_forwarding_payload", "readiness_result"],
        ["comment_mode"],
        ["title_update"],
    ):
        instance = _valid_input()
        node = instance
        for part in path:
            node = node[part]
        node["unexpected_key"] = 1
        assert list(validator.iter_errors(instance)), path
        if not path:
            with pytest.raises(ValueError, match="input_unknown_keys"):
                txn._validate_input_payload(instance)


def test_ac7_input_enums_match_the_validator() -> None:
    schema = _doc_schema("ISSUE_EDIT_TXN_INPUT_V1")
    status_enum = schema["properties"]["readiness_forwarding_payload"]["properties"][
        "readiness_result"
    ]["properties"]["status"]["enum"]
    assert set(status_enum) == set(txn.READINESS_ALLOWED)
    for bad in ("ok", "GO", "", None):
        instance = _valid_input()
        instance["readiness_forwarding_payload"]["readiness_result"]["status"] = bad
        assert list(_validator(schema).iter_errors(instance))
        with pytest.raises(ValueError, match="readiness_status_invalid"):
            txn._validate_input_payload(instance)
    modes = schema["properties"]["comment_mode"]["properties"]["mode"]["enum"]
    assert set(modes) == {"skip", "publish"}


def test_ac7_native_relationships_stays_in_the_schema_but_the_entrypoint_rejects_it() -> None:
    schema = _doc_schema("ISSUE_EDIT_TXN_INPUT_V1")
    assert "native_relationships" in schema["properties"]  # verbatim source schema, not edited
    instance = _valid_input()
    instance["native_relationships"] = {"parent": {"action": "unchanged"}}
    assert "native_relationships" in txn.TOP_LEVEL_KEYS
    with pytest.raises(ValueError, match="native_relationships_unapproved"):
        txn._validate_input_payload(instance)


SEMANTIC = {
    "schema_version": "SEMANTIC_REWRITE_CONSTRAINTS_V1",
    "source_artifact": "artifacts/2296/semantic-review/x.json",
    "checked_body_sha256": "a" * 64,
    "findings": [{"severity": "blocker", "summary": "s"}],
    "max_rewrite_attempts": 2,
    "no_progress_route": "human_judgment_required",
}


@pytest.mark.parametrize(
    ("lane", "constraints", "ok"),
    [
        ("omit", "omit", True),
        ("fail_closed_repair", "omit", True),
        ("semantic", SEMANTIC, True),
        ("semantic", "omit", False),
        ("semantic", None, False),
        ("omit", SEMANTIC, False),
        ("fail_closed_repair", SEMANTIC, False),
        ("bogus", "omit", False),
    ],
    ids=[
        "legacy",
        "explicit_repair",
        "semantic_ok",
        "semantic_missing",
        "semantic_null",
        "constraints_no_lane",
        "constraints_repair_lane",
        "bogus_lane",
    ],
)
def test_ac7_presence_correlation_agrees_between_schema_and_validator(
    lane, constraints, ok: bool
) -> None:
    schema = _doc_schema("ISSUE_EDIT_TXN_INPUT_V1")
    instance = _valid_input()
    if lane != "omit":
        instance["rewrite_lane"] = lane
    if constraints != "omit":
        instance["semantic_rewrite_constraints"] = constraints
    schema_ok = not list(_validator(schema).iter_errors(instance))
    try:
        txn._validate_input_payload(instance)
        validator_ok = True
    except ValueError:
        validator_ok = False
    assert schema_ok is ok and validator_ok is ok


def test_ac7_result_required_keys_are_present_in_every_real_result(proj: Project) -> None:
    required = set(_doc_schema("ISSUE_EDIT_TXN_RESULT_V1")["required"])
    seen: dict[str, dict] = {}
    seen["ok"] = proj.run_txn(proj.payload())[1]
    proj.set_state(body=OLD_BODY, patches=0, updated_at=UPDATED_AT)
    seen["no_change"] = proj.run_txn(proj.payload(OLD_BODY))[1]
    seen["failed_no_mutation"] = proj.run_txn(proj.payload(repo="x/y"))[1]
    data = proj.payload()
    data["readiness_forwarding_payload"]["readiness_result"]["status"] = "human_judgment"
    seen["human_judgment"] = proj.run_txn(data)[1]
    proj.set_state(post_mode="fail")
    seen["failed_after_mutation"] = proj.run_txn(
        proj.payload(OLD_BODY, comment_mode=_comment_mode(proj))
    )[1]
    proj.set_state(patch_mode="fail", post_mode="ok")
    seen["mutation_outcome_unknown"] = proj.run_txn(proj.payload())[1]
    for expected_status, result in seen.items():
        assert result["status"] == expected_status
        assert required <= set(result), (expected_status, required - set(result))
        assert (
            result["schema"] == "ISSUE_EDIT_TXN_RESULT_V1" and result["rollback_attempted"] is False
        )
        assert isinstance(result["mutation_started"], bool) and isinstance(result["errors"], list)


def test_ac7_known_source_drift_between_result_schema_and_producer_is_pinned_exactly(
    proj: Project,
) -> None:
    """The source canonical RESULT schema is stale vs the source producer (pre-existing at the source tag):
    the producer emits a `content_update` block and a `mutation_outcome_unknown` status that the schema
    (additionalProperties: false, 5-value enum) does not list. The excerpt is kept verbatim, so the drift is
    pinned here instead of hidden: any change on either side fails this test and must be reconciled
    deliberately (tracked by a follow-up Issue, see the manifest `known_source_defects`)."""
    validator = _validator(_doc_schema("ISSUE_EDIT_TXN_RESULT_V1"))
    ok = proj.run_txn(proj.payload())[1]
    proj.set_state(body=OLD_BODY, patches=0, updated_at=UPDATED_AT, patch_mode="fail")
    unknown = proj.run_txn(proj.payload())[1]
    assert [e.message for e in validator.iter_errors(ok)] == [
        "Additional properties are not allowed ('content_update' was unexpected)"
    ]
    messages = sorted(e.message for e in validator.iter_errors(unknown))
    assert len(messages) == 2
    assert messages[0].startswith("'mutation_outcome_unknown' is not one of")
    assert messages[1] == "Additional properties are not allowed ('content_update' was unexpected)"
    # and nothing else drifts: with the two known deviations removed the real output validates
    for result in (ok, unknown):
        trimmed = {k: v for k, v in result.items() if k != "content_update"}
        if trimmed["status"] == "mutation_outcome_unknown":
            trimmed["status"] = "failed_after_mutation"
        assert not list(validator.iter_errors(trimmed)), trimmed


def test_ac7_result_status_enum_matches_what_the_producer_can_emit() -> None:
    schema = _doc_schema("ISSUE_EDIT_TXN_RESULT_V1")
    enum = set(schema["properties"]["status"]["enum"])
    source = (TXN_SCRIPTS / "edit_issue_txn.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'status="([a-z_]+)"', source)) - {"failed", "not_run", "ok_"}
    emitted = {
        s
        for s in emitted
        if s in enum or s.startswith(("mutation_", "failed_", "human_", "no_", "ok"))
    }
    assert emitted - enum == {"mutation_outcome_unknown"}  # the single known, pinned drift
    assert {
        "ok",
        "no_change",
        "failed_no_mutation",
        "failed_after_mutation",
        "human_judgment",
    } <= enum


def test_ac7_excerpt_is_verbatim_from_the_recorded_source_sections() -> None:
    manifest = _manifest()["canonical_schema_excerpt"]
    text = SOURCE_DOC.read_text(encoding="utf-8")
    for key, heading, end in (
        ("ISSUE_EDIT_TXN_INPUT_V1", "### ISSUE_EDIT_TXN_INPUT_V1", "### ISSUE_EDIT_TXN_RESULT_V1"),
        ("ISSUE_EDIT_TXN_RESULT_V1", "### ISSUE_EDIT_TXN_RESULT_V1", None),
    ):
        section = heading + text.split(heading, 1)[1]
        section = section.split(end, 1)[0] if end else section
        section = section.rstrip("\n") + "\n"
        assert (
            hashlib.sha256(section.encode("utf-8")).hexdigest()
            == manifest["sections"][key]["sha256"]
        ), key
    budget = "## OUTPUT_BUDGET_V1" + text.split("## OUTPUT_BUDGET_V1", 1)[1].split("\n## ", 1)[0]
    assert (
        hashlib.sha256((budget.rstrip("\n") + "\n").encode("utf-8")).hexdigest()
        == manifest["sections"]["OUTPUT_BUDGET_V1"]["sha256"]
    )


def test_ac7_excerpt_is_an_excerpt_not_the_full_document() -> None:
    lines = SOURCE_DOC.read_text(encoding="utf-8").count("\n")
    assert lines < 500  # source is 3,223 lines; only the two approved sections are carried
    headings = re.findall(r"^## .*$", SOURCE_DOC.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert len(headings) == 2, headings


# --------------------------------------------------------------------------- AC6: manifest closure (static)


def test_ac6_manifest_is_internally_consistent() -> None:
    m = _manifest()
    assert (
        m["schema"] == "STAGE2_4_EDIT_TXN_MANIFEST_V1"
        and m["issue"] == "squne121/agent-platform#34"
    )
    assert m["source"]["commit"] == "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
    assert set(m["test_files"]) == set(m["expected_collect"]) == set(m["expected_ran"])
    deferred_per_file: dict[str, int] = {}
    for entry in m["deferred_tests"]:
        assert all(entry.get(k) for k in ("id", "owner_stage", "reason", "rerun_condition")), entry[
            "id"
        ]
        deferred_per_file[entry["id"].split("::")[0]] = (
            deferred_per_file.get(entry["id"].split("::")[0], 0) + 1
        )
    for f in m["test_files"]:
        assert m["expected_collect"][f] - deferred_per_file.get(f, 0) == m["expected_ran"][f], f
    inv = m["source_inventory"]
    assert inv["ran_total"] == sum(m["expected_ran"].values())
    assert inv["collected_total"] == sum(m["expected_collect"].values())
    assert inv["deferred_total"] == len(m["deferred_tests"]) and inv["skipped"] == 0
    assert (
        inv["source_tests_in_scope"][".claude/skills/edit-issue/tests/test_edit_issue_txn.py"] == 69
    )
    assert (
        inv["source_tests_in_scope"][
            ".claude/skills/edit-issue/tests/test_edit_issue_txn_preexisting_readiness.py"
        ]
        == 118
    )
    assert (
        inv["source_tests_in_scope"][
            ".claude/skills/edit-issue/tests/test_issue_content_update_txn.py"
        ]
        == 3
    )


def test_ac6_every_deferred_id_is_a_native_creator_runtime_or_hygiene_conflict() -> None:
    m = _manifest()
    for entry in m["deferred_tests"]:
        if "issue_creator_issue_editor_split" in entry["id"]:
            assert entry["owner_stage"].startswith("Stage 2-5 / Issue #35")
            assert "#35" in entry["rerun_condition"]
        elif "runtime_smoke" in entry["id"]:
            assert "runtime smoke" in entry["owner_stage"]
        elif entry["owner_stage"].startswith("Stage 2-4 follow-up: hygiene C9"):
            assert entry["id"].endswith(
                "test_1844_parent_candidate_runs_real_local_validators_before_executor"
            )
        else:
            assert entry["id"].startswith(".claude/skills/edit-issue/tests/")
            assert entry["owner_stage"].startswith("native-relationship approval")


def test_ac6_deferred_native_tests_really_use_native_relationships() -> None:
    """A test may only be deferred as `native` when its source actually exercises native_relationships."""
    import ast

    m = _manifest()
    trees: dict[str, tuple[ast.Module, str]] = {}
    for entry in m["deferred_tests"]:
        file, _, name = entry["id"].partition("::")
        if not entry["owner_stage"].startswith("native-relationship"):
            continue
        if file not in trees:
            text = (ROOT / file).read_text(encoding="utf-8")
            trees[file] = (ast.parse(text), text)
        tree, text = trees[file]
        fn = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name.split("[")[0]
        )
        segment = "\n".join(
            text.splitlines()[fn.lineno - 1 - len(fn.decorator_list) - 30 : fn.end_lineno]
        )
        assert re.search(r"native|relationship|_relationships", segment, re.IGNORECASE), entry["id"]


def test_ac6_moved_files_match_the_recorded_hashes() -> None:
    m = _manifest()
    for rel, entry in m["source_parity"].items():
        if entry.get("moved_sha256") is None:
            assert not (ROOT / rel).exists()
            continue
        moved = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        assert moved == entry["moved_sha256"], rel
        assert entry["verbatim"] == (entry["source_sha256"] == entry["moved_sha256"]), rel
        if not entry["verbatim"]:
            assert entry["intentional_diff"], rel


def test_ac6_reformatted_moved_tests_are_ast_identical_to_the_source() -> None:
    import ast

    for rel, entry in _manifest()["source_parity"].items():
        if entry.get("intentional_diff", "").startswith("`ruff format` only"):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            assert (
                hashlib.sha256(ast.dump(tree).encode()).hexdigest() == entry["moved_ast_sha256"]
            ), rel
            assert entry["moved_ast_sha256"] == entry["source_ast_sha256"], rel


def test_ac6_reused_stage_2_3_files_are_untouched_by_this_stage() -> None:
    """guard / hygiene / compiler are reused, not re-migrated or edited by Stage 2-4."""
    stage23 = json.loads(
        (ROOT / "docs/dev/stage2-3-edit-guards-manifest.json").read_text(encoding="utf-8")
    )
    for rel in (
        ".claude/skills/edit-issue/scripts/guard-issue-body.py",
        ".claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py",
        ".claude/skills/issue-contract-review/scripts/vc_baseline_shape_compiler.py",
    ):
        assert (
            hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
            == stage23["source_parity"][rel]["moved_sha256"]
        )


def test_ac6_stage_1a_no_longer_defers_edit_issue_txn() -> None:
    stage1a = json.loads((ROOT / "docs/dev/stage1a-core-manifest.json").read_text(encoding="utf-8"))
    closure = stage1a["import_closure"]
    assert "edit_issue_txn" not in {e["module"] for e in closure["deferred_imports"]}
    assert "edit_issue_txn.py" not in {e["script"] for e in closure["deferred_script_refs"]}
    # the other stages' deferrals are kept
    assert {"run_contract_review_once", "materialize_issue_scope_snapshot"} <= {
        e["module"] for e in closure["deferred_imports"]
    }
    stage21b = json.loads(
        (ROOT / "docs/dev/stage2-1b-exec-manifest.json").read_text(encoding="utf-8")
    )
    assert not [d for d in stage21b["deferred_tests"] if "edit_issue_txn" in d["id"]]
    assert "edit_issue_txn.py" not in stage21b["dangling_refs"]


def test_ac6_no_skip_xfail_or_deselect_is_added_by_the_new_tests() -> None:
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    banned = {"skip", "skipif", "xfail", "importorskip"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            raise AssertionError(f"line {node.lineno}: {node.attr}")
        if isinstance(node, ast.Name) and node.id in banned:
            raise AssertionError(f"line {node.lineno}: {node.id}")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("-" * 2 + "deselect")
        ):
            raise AssertionError(f"line {node.lineno}: deselect option")


def test_ac6_ci_has_a_dedicated_lane_for_this_manifest() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/ci/run_stage1b1_tests.py docs/dev/stage2-4-edit-txn-manifest.json" in ci
    assert "stage2-4-edit-txn:" in ci


# Issue #34 Allowed Paths (exact) + the 2026-10-28 PR #47-only exception (two exact files).
# Parent manifests are the two that carried stale `edit_issue_txn.py` deferrals.
_ALLOWED_GLOBS = (
    ".claude/skills/edit-issue/",
    ".claude/agents/tests/",
    "tests/fixtures/agent-parity/",
    "tests/stage2/",
)
_ALLOWED_EXACT = frozenset(
    {
        ".claude/agents/issue-editor.md",
        "docs/dev/stage2-4-edit-txn-manifest.json",
        "docs/dev/agent-skill-boundaries.md",
        ".github/workflows/ci.yml",
        "docs/dev/stage1a-core-manifest.json",
        "docs/dev/stage2-1b-exec-manifest.json",
        # PR #47-only approval (2026-10-28 JST): exactly these two files
        ".claude/skills/issue-refinement-loop/tests/test_semantic_review_bounded_discovery_contract.py",
        "docs/dev/stage1b4-semantic-review-manifest.json",
    }
)


_PR47_EXCEPTION = frozenset(
    {
        ".claude/skills/issue-refinement-loop/tests/test_semantic_review_bounded_discovery_contract.py",
        "docs/dev/stage1b4-semantic-review-manifest.json",
    }
)


def _paths_outside_allowed(paths: list[str]) -> list[str]:
    def ok(p: str) -> bool:
        if ".." in p.split("/") or p.startswith("/"):
            return False
        return p in _ALLOWED_EXACT or p.startswith(_ALLOWED_GLOBS)

    return [p for p in paths if not ok(p)]


def test_ac6_manifest_referenced_paths_stay_inside_allowed_paths() -> None:
    """Scope of this test: only the paths the manifest *references* (test_files / required_fixtures).

    It does NOT verify the PR's changed files; that audit is done against the
    fresh `gh pr diff --name-only` list and recorded in the PR body.
    """
    m = _manifest()
    assert _paths_outside_allowed([*m["test_files"], *m["required_fixtures"]]) == []


def test_ac6_allowed_paths_matcher_rejects_unapproved_paths() -> None:
    """Negative control: unapproved / broader-prefix paths are detected."""
    unapproved = [
        ".claude/skills/issue-refinement-loop/SKILL.md",
        ".claude/skills/issue-refinement-loop/tests/test_other.py",
        ".claude/skills/review-issue/SKILL.md",
        ".claude/agents/issue-creator.md",
        ".claude/agents/issue-design-reviewer.md",
        "docs/dev/stage1b3a-producer-transport-manifest.json",
        "docs/dev/other.md",
        "scripts/agent-guards/repo_identity.py",
        "config/repository_identity.v1.json",
        "pyproject.toml",
        "AGENTS.md",
        ".github/workflows/other.yml",
        ".claude/hooks/x.py",
        "tests/other/x.py",
        "tests/fixtures/other/x",
        "tests/stage20/x.py",
        ".claude/skills/edit-issue-evil/x",
        "tests/stage2/../../AGENTS.md",
        "/etc/passwd",
    ]
    assert _paths_outside_allowed(unapproved) == unapproved
    assert _paths_outside_allowed([*unapproved, "tests/stage2/test_x.py"]) == unapproved


def test_ac6_pr47_exception_is_exactly_two_files() -> None:
    assert len(_PR47_EXCEPTION) == 2
    assert _PR47_EXCEPTION <= _ALLOWED_EXACT
    assert _paths_outside_allowed(sorted(_PR47_EXCEPTION)) == []
    # the exception grants nothing beyond those two files (siblings stay rejected)
    assert _paths_outside_allowed(
        [
            ".claude/skills/issue-refinement-loop/tests/test_other.py",
            "docs/dev/stage1b3a-producer-transport-manifest.json",
        ]
    ) == [
        ".claude/skills/issue-refinement-loop/tests/test_other.py",
        "docs/dev/stage1b3a-producer-transport-manifest.json",
    ]
