"""Stage 2-1b closure / false-green controls for controlled_skill_mutation_exec.

Fixture evidence only: every `gh` call below goes to an in-process fake (or is refused before
any process is started). No network, no real GitHub write. Pins:
- the executor imports under `python -I`; its trusted slug IS the repository identity config
  (no second slug definition) and an unusable config / layout shift fails closed at import;
- wrong / legacy / malformed repo (and a non-str trusted slug) are refused with ZERO
  subprocess calls (not even `git`), with a positive control proving the spy sees calls;
- only the approved live lanes (issue_body.update / issue_content.update /
  issue_comment.publish) can run; every other registered command id is refused before any
  git / gh call and before its handler is entered;
- issue_comment.publish / issue_content.update / issue_body.update: valid -> write ->
  readback succeeds; stale, duplicate, timeout, CLI failure, partial and readback mismatch
  never report success;
- `_find_gh_bin` stays pinned to the trusted path list and ignores PATH.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARDS = ROOT / "scripts" / "agent-guards"
EXECUTOR = GUARDS / "controlled_skill_mutation_exec.py"
IDENTITY_CONFIG = ROOT / "config" / "repository_identity.v1.json"
TRUSTED = json.loads(IDENTITY_CONFIG.read_text())["trusted_repo_slug"]
# Same owner, different repository (the literal legacy slug must not appear in a tracked file).
LEGACY = TRUSTED.split("/")[0] + "/some-other-repository"

if str(GUARDS) not in sys.path:
    sys.path.insert(0, str(GUARDS))

import controlled_skill_mutation_exec as ex
import controlled_skill_mutation_policy as policy

APPROVED = {
    policy.COMMAND_ID_ISSUE_BODY_UPDATE,
    policy.COMMAND_ID_ISSUE_CONTENT_UPDATE,
    policy.COMMAND_ID_ISSUE_COMMENT_PUBLISH,
}
UNAPPROVED = {
    policy.COMMAND_ID_ISSUE_RELATIONSHIP_UPDATE,
    policy.COMMAND_ID_ISSUE_DEPENDENCY_REMOVE,
    policy.COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
    policy.COMMAND_ID_TEST_VERDICT_PUBLISH,
    policy.COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE,
}
_real_subprocess_run = subprocess.run
# `_run_*` names that are NOT unapproved-lane handlers (approved lanes + a git helper); every
# other `_run_*` is spied, so a renamed / new unapproved handler cannot slip past the spy.
_NOT_SPIED_RUN_NAMES = {
    "_run_git_status_v2_z",
    "_run_issue_body_update",
    "_run_issue_content_update",
    "_run_issue_comment_publish",
    "_run_human_history_comment_publish",
}


def _real_run(argv, *args, **kwargs):
    """Real subprocess for tmp-repo setup/git, immune to an ambient GIT_DIR / GIT_WORK_TREE."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if kwargs.get("env") is None:
        kwargs["env"] = env
    return _real_subprocess_run(argv, *args, **kwargs)


_IMPORT_CODE = (
    "import sys; sys.path.insert(0, {path!r}); "
    "import controlled_skill_mutation_exec as e; print(e.TRUSTED_REPO)"
)
ISSUE = 4242


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- isolated import


def _isolated(code: str, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        cwd=str(cwd or ROOT),
        timeout=60,
        check=False,
    )


def _copy_layout(tmp_path: Path, *, config: str | None) -> Path:
    guards = tmp_path / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    for name in (
        "controlled_skill_mutation_exec.py",
        "controlled_skill_mutation_policy.py",
        "repo_identity.py",
    ):
        shutil.copy(GUARDS / name, guards / name)
    if config is not None:
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "repository_identity.v1.json").write_text(config)
    return guards


def test_isolated_import_and_slug_is_identity_config() -> None:
    r = _isolated(_IMPORT_CODE.format(path=str(GUARDS)))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == TRUSTED
    assert ex.TRUSTED_REPO == TRUSTED == policy.TRUSTED_REPO


def test_no_slug_definition_in_executor_source() -> None:
    src = EXECUTOR.read_text()
    assert TRUSTED not in src  # the slug is read from config, never written here
    assert LEGACY not in src
    assert TRUSTED.split("/")[0] + "/loop" + "-protocol" not in src


def test_layout_replica_with_valid_config_imports(tmp_path: Path) -> None:
    guards = _copy_layout(tmp_path, config=IDENTITY_CONFIG.read_text())
    r = _isolated(_IMPORT_CODE.format(path=str(guards)), cwd=guards)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == TRUSTED


_BAD_CONFIGS = {
    "missing": None,  # layout shift: config not where parents[2] expects it
    "malformed_json": "not json",
    "not_object": "[]",
    "missing_slug": json.dumps({"schema": "REPOSITORY_IDENTITY_V1"}),
    "schema_mismatch": json.dumps({"schema": "WRONG", "trusted_repo_slug": "a/b"}),
    "slug_no_slash": json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "x"}),
    "slug_dotdot": json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "a/../b"}),
    "slug_empty": json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": ""}),
    "slug_not_str": json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": ["a/b"]}),
    "extra_key": json.dumps(
        {"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "a/b", "x": 1}
    ),
}


@pytest.mark.parametrize("config", list(_BAD_CONFIGS.values()), ids=list(_BAD_CONFIGS))
def test_unusable_identity_config_fails_closed_at_import(tmp_path: Path, config) -> None:
    guards = _copy_layout(tmp_path, config=config)
    r = _isolated(_IMPORT_CODE.format(path=str(guards)), cwd=guards)
    assert r.returncode != 0
    assert "RepositoryIdentityError" in r.stderr
    assert r.stdout.strip() == ""


def test_layout_shift_of_executor_alone_fails_closed(tmp_path: Path) -> None:
    # Executor + policy moved one directory deeper: parents[2] no longer reaches config/.
    deep = tmp_path / "scripts" / "agent-guards" / "nested"
    deep.mkdir(parents=True)
    for name in ("controlled_skill_mutation_exec.py", "controlled_skill_mutation_policy.py"):
        shutil.copy(GUARDS / name, deep / name)
    shutil.copy(GUARDS / "repo_identity.py", deep / "repo_identity.py")
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "repository_identity.v1.json").write_text(IDENTITY_CONFIG.read_text())
    r = _isolated(_IMPORT_CODE.format(path=str(deep)), cwd=deep)
    assert r.returncode != 0
    assert "RepositoryIdentityError" in r.stderr


# --------------------------------------------------------------------------- fake gh / harness


class FakeGh:
    """Stateful in-process `gh`. `calls` records EVERY subprocess call (gh and git)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.title = "old title"
        self.body = "old body"
        self.updated_at = "2026-01-01T00:00:00Z"
        self.is_pr = False
        self.comments: list[dict] = []
        self.patch_mode = "ok"  # ok | fail | noop | timeout_applied | timeout_lost
        self.post_mode = "ok"  # ok | fail | noop | timeout_applied | timeout_lost
        self.patches = 0
        self.posts = 0
        self.gh = "/fake/trusted/gh"
        self.unexpected: list[list[str]] = []
        self.mutate_next = False

    def gh_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == self.gh]

    def _cp(self, argv, rc=0, out="", err=""):
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)

    def run(self, argv, *a, **kw):
        self.calls.append([str(x) for x in argv])
        if argv[0] != self.gh:  # git: keep the real tmp repo so the remote/postcondition run
            return _real_run(argv, *a, **kw)
        if argv[1:3] == ["issue", "view"]:
            if argv[3] != str(ISSUE) or argv[argv.index("--repo") + 1] != TRUSTED:
                self.unexpected.append(list(argv))
            return self._cp(argv, out=json.dumps({"comments": self.comments}))
        assert argv[1] == "api", argv
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        path = next(x for x in argv if x.startswith("repos/"))
        if method == "GET":
            if path != f"repos/{TRUSTED}/issues/{ISSUE}":
                self.unexpected.append(list(argv))
            return self._cp(
                argv,
                out=json.dumps(
                    {
                        "title": self.title,
                        "body": self.body,
                        "updatedAt": self.updated_at,
                        "isPullRequest": self.is_pr,
                    }
                ),
            )
        if method == "PATCH" and path.endswith(f"/issues/{ISSUE}"):
            self.patches += 1
            if "--input" in argv:  # issue_content.update: title + body in one request
                data = json.loads(Path(argv[argv.index("--input") + 1]).read_text())
            else:  # issue_body.update: body only
                spec = argv[argv.index("--field") + 1].split("=@", 1)[1]
                data = {"title": self.title, "body": Path(spec).read_text()}
            return self._write(argv, self.patch_mode, lambda: self._apply_patch(data))
        if method == "POST" and path.endswith("/comments"):
            self.posts += 1
            body = Path(argv[argv.index("--field") + 1].split("=@", 1)[1]).read_text()
            return self._write(argv, self.post_mode, lambda: self._add_comment(body))
        self.unexpected.append(list(argv))  # the code under test may swallow exceptions
        raise AssertionError(f"unexpected gh call: {argv}")

    def _write(self, argv, mode, apply):
        if mode == "fail":
            return self._cp(argv, rc=1, err="boom")
        if mode == "timeout_lost":
            raise subprocess.TimeoutExpired(argv, 15)
        if mode == "timeout_applied":
            apply()
            raise subprocess.TimeoutExpired(argv, 15)
        if mode == "mutated":  # persisted, but not byte-identical to what was sent
            self.mutate_next = True
        out = "" if mode == "noop" else apply()
        return self._cp(argv, out=out or "{}")

    def _apply_patch(self, data):
        self.title, self.body = data["title"], data["body"]
        self.updated_at = "2026-01-02T00:00:00Z"
        return "{}"

    def _add_comment(self, body):
        n = len(self.comments) + 1
        url = f"https://github.com/{TRUSTED}/issues/{ISSUE}#issuecomment-{9000 + n}"
        if self.mutate_next:
            body += "X"
        self.comments.append({"id": f"IC_{n}", "url": url, "body": body})
        return json.dumps({"html_url": url, "id": 9000 + n})


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    fg = FakeGh()
    project = tmp_path / "project"
    (project / "scripts" / "agent-guards").mkdir(parents=True)
    _real_run(["git", "init", "-q", str(project)], check=True)
    _real_run(
        ["git", "-C", str(project), "remote", "add", "origin", f"https://github.com/{TRUSTED}.git"],
        check=True,
    )
    monkeypatch.setattr(ex, "PROJECT_ROOT", project)
    monkeypatch.setattr(ex, "_find_gh_bin", lambda: (fg.gh, ""))
    monkeypatch.setattr(subprocess, "run", fg.run)
    monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
    fg.project = project
    yield fg
    assert fg.unexpected == []


def _write_input(fg: FakeGh, command_id: str, data: dict, name: str = "input.json") -> str:
    rel = f"artifacts/{ISSUE}/issue-metadata/{command_id}/{name}"
    path = fg.project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return rel


def _run(fg, command_id, rel, *, repo=TRUSTED, extra=()):
    argv = ["--command-id", command_id, "--issue-number", str(ISSUE), "--repo", repo, "--json"]
    if rel is not None:
        argv += ["--input-file", rel]
    return ex.main(argv + list(extra))


def _result(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _comment_input(marker="<!-- cm-1 -->", body=None) -> dict:
    return {
        "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
        "issue_number": ISSUE,
        "comment_body": body or f"status update {marker}",
        "marker": marker,
    }


def _content_input(fg: FakeGh, **over) -> dict:
    new_body = "new body\n"
    data = {
        "schema": "ISSUE_CONTENT_UPDATE_INPUT_V1",
        "issue_number": ISSUE,
        "repo": TRUSTED,
        "expected_previous_title": fg.title,
        "expected_previous_body_sha256": _sha(fg.body),
        "expected_previous_updated_at": fg.updated_at,
        "new_title": "new title",
        "new_body": new_body,
        "new_body_sha256": _sha(new_body),
        "operation_reason": "fixture",
        "idempotency_key": f"{TRUSTED}:{ISSUE}:fixture:v1",
    }
    data.update(over)
    return data


def _body_input(fg: FakeGh, **over) -> dict:
    new_body = "new body\n"
    data = {
        "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
        "issue_number": ISSUE,
        "previous_body_sha256": _sha(fg.body),
        "previous_updated_at": fg.updated_at,
        "new_body": new_body,
        "new_body_sha256": _sha(new_body),
    }
    data.update(over)
    return data


# --------------------------------------------------------------------------- write-zero


@pytest.mark.parametrize("command_id", sorted(APPROVED))
@pytest.mark.parametrize(
    "bad_repo",
    [LEGACY, "", "not-a-slug", TRUSTED + "x", TRUSTED.upper(), TRUSTED + "/", " " + TRUSTED],
    ids=["legacy", "empty", "malformed", "suffix", "case", "trailing_slash", "leading_space"],
)
def test_wrong_repo_is_refused_with_zero_subprocess_calls(fake, capsys, command_id, bad_repo):
    rc = _run(
        fake, command_id, f"artifacts/{ISSUE}/issue-metadata/{command_id}/input.json", repo=bad_repo
    )
    assert rc == 2
    assert _result(capsys)["reason"].startswith("repo_mismatch")
    assert fake.calls == []  # not even `git remote get-url`


@pytest.mark.parametrize(
    "bad_slug", [None, 123, ["a/b"], b"a/b"], ids=["none", "int", "list", "bytes"]
)
def test_non_str_trusted_slug_is_refused_with_zero_calls(fake, capsys, monkeypatch, bad_slug):
    monkeypatch.setattr(ex, "TRUSTED_REPO", bad_slug)
    rc = _run(fake, policy.COMMAND_ID_ISSUE_COMMENT_PUBLISH, "x.json", repo=str(bad_slug))
    assert rc == 2
    assert _result(capsys)["reason"].startswith("repo_mismatch")
    assert fake.calls == []


def test_positive_control_trusted_repo_reaches_git(fake, capsys):
    # Proves the call spy has detection power: the trusted repo gets past the repo gate.
    rc = _run(fake, policy.COMMAND_ID_ISSUE_COMMENT_PUBLISH, None)
    assert rc == 2
    assert _result(capsys)["reason"].startswith("missing_input_source")
    assert any(c[0] == "git" for c in fake.calls)
    assert fake.gh_calls() == []


# --------------------------------------------------------------------------- unapproved lanes


@pytest.mark.parametrize(
    "origin",
    [
        f"https://github.com/{LEGACY}.git",
        f"https://evil.example/{TRUSTED}.git",
    ],
    ids=["other_repo", "untrusted_host"],
)
@pytest.mark.parametrize("command_id", sorted(APPROVED))
def test_wrong_git_origin_is_refused_before_any_gh_call(fake, capsys, command_id, origin):
    _real_run(["git", "-C", str(fake.project), "remote", "set-url", "origin", origin], check=True)
    rc = _run(fake, command_id, None)
    assert rc == 2
    assert _result(capsys)["reason"].startswith("git_remote_origin_")
    assert fake.gh_calls() == []


def test_approved_and_unapproved_partition_the_registry() -> None:
    assert ex.APPROVED_LIVE_COMMAND_IDS == APPROVED
    assert policy.ALL_COMMAND_IDS == APPROVED | UNAPPROVED
    assert not (APPROVED & UNAPPROVED)


@pytest.mark.parametrize("command_id", sorted(UNAPPROVED))
@pytest.mark.parametrize("extra", [(), ("--dry-run",)], ids=["live", "dry_run"])
def test_unapproved_command_id_is_refused_before_any_call(
    fake, capsys, monkeypatch, command_id, extra
):
    handlers = {}
    for name in dir(ex):
        if name.startswith("_run_") and name not in _NOT_SPIED_RUN_NAMES:
            handlers[name] = Mock(return_value=0)
            monkeypatch.setattr(ex, name, handlers[name])
    assert handlers  # the spy covers the real handler names
    # Even with a fully valid-looking input file the lane must not be entered.
    rel = _write_input(fake, command_id, {"schema": policy.INPUT_SCHEMA_BY_COMMAND[command_id]})
    rc = _run(fake, command_id, rel, extra=extra)
    out = _result(capsys)
    assert rc == 2 and out["status"] == "error"
    assert out["reason"].startswith("command_id_not_approved_for_live_execution")
    assert fake.calls == []
    assert not any(h.called for h in handlers.values())


@pytest.mark.parametrize("command_id", sorted(UNAPPROVED))
def test_unapproved_command_id_is_refused_by_a_real_process(command_id: str) -> None:
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            str(EXECUTOR),
            "--command-id",
            command_id,
            "--issue-number",
            "1",
            "--repo",
            TRUSTED,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 2, r.stderr
    assert json.loads(r.stdout)["reason"].startswith("command_id_not_approved_for_live_execution")


def test_unapproved_command_id_with_wrong_repo_reports_the_gate_first(fake, capsys):
    cid = policy.COMMAND_ID_ISSUE_DEPENDENCY_REMOVE
    assert _run(fake, cid, None, repo=LEGACY) == 2
    assert _result(capsys)["reason"].startswith("command_id_not_approved_for_live_execution")
    assert fake.calls == []


def test_trusted_slug_with_always_equal_non_str_is_refused(fake, capsys, monkeypatch):
    class AlwaysEqual:
        def __eq__(self, other):
            return True

        __hash__ = None

    monkeypatch.setattr(ex, "TRUSTED_REPO", AlwaysEqual())
    assert _run(fake, policy.COMMAND_ID_ISSUE_COMMENT_PUBLISH, "x.json", repo="anything/x") == 2
    assert fake.calls == []


def test_unknown_command_id_is_still_refused(fake, capsys):
    assert _run(fake, "issue.delete", None) == 2
    assert _result(capsys)["reason"].startswith("unknown_command_id")
    assert fake.calls == []


@pytest.mark.parametrize("command_id", sorted(APPROVED))
def test_positive_control_approved_command_id_passes_the_gate(fake, capsys, command_id):
    assert _run(fake, command_id, None) == 2
    assert not _result(capsys)["reason"].startswith("command_id_not_approved")
    assert any(c[0] == "git" for c in fake.calls)


def test_scope_snapshot_handler_fails_closed_without_materializer(fake, capsys):
    # Defence in depth behind the gate: the dormant handler itself refuses when the
    # materializer module is absent (it is not migrated; owner_stage in the manifest).
    cid = policy.COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE
    args = Mock(issue_number=ISSUE, repo=TRUSTED, command_id=cid, dry_run=False)
    fail = Mock(return_value=2)
    ex._run_issue_scope_snapshot_materialize(args, {}, fake.gh, fail, Mock())
    assert fail.called and fake.calls == []


# --------------------------------------------------------------------------- gh binary pin


def test_find_gh_bin_is_pinned_and_ignores_path(tmp_path, monkeypatch):
    assert ex._GH_TRUSTED_PATHS == "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    decoy = tmp_path / "bin"
    decoy.mkdir()
    gh = decoy / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(decoy))
    found, err = ex._find_gh_bin()
    assert found is None or not found.startswith(str(decoy))
    assert (found is None) == bool(err)


# --------------------------------------------------------------------------- issue_comment.publish

CID_COMMENT = policy.COMMAND_ID_ISSUE_COMMENT_PUBLISH
CID_CONTENT = policy.COMMAND_ID_ISSUE_CONTENT_UPDATE
CID_BODY = policy.COMMAND_ID_ISSUE_BODY_UPDATE


def test_comment_publish_valid_write_then_readback(fake, capsys):
    rel = _write_input(fake, CID_COMMENT, _comment_input())
    assert _run(fake, CID_COMMENT, rel) == 0
    out = _result(capsys)
    assert out["status"] == "ok" and out["repo"] == TRUSTED and out["issue_number"] == ISSUE
    assert fake.posts == 1 and len(fake.comments) == 1
    assert out["comment_id"] == "IC_1" and out["comment_url"] == fake.comments[0]["url"]
    assert all(f"repos/{TRUSTED}/" in " ".join(c) for c in fake.gh_calls() if "api" in c)


def test_comment_publish_second_run_does_not_post_again(fake, capsys):
    rel = _write_input(fake, CID_COMMENT, _comment_input())
    assert _run(fake, CID_COMMENT, rel) == 0
    capsys.readouterr()
    assert _run(fake, CID_COMMENT, rel) == 0
    assert fake.posts == 1 and len(fake.comments) == 1


def test_comment_publish_duplicate_remote_marker_blocks_post(fake, capsys):
    fake.comments = [
        {"id": "IC_a", "url": "u1#issuecomment-1", "body": "x <!-- cm-1 -->"},
        {"id": "IC_b", "url": "u2#issuecomment-2", "body": "y <!-- cm-1 -->"},
    ]
    rel = _write_input(fake, CID_COMMENT, _comment_input())
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"] == "duplicate_marker_conflict_pre_mutation"
    assert fake.posts == 0


@pytest.mark.parametrize("mode", ["fail", "noop", "timeout_lost"])
def test_comment_publish_failed_or_lost_post_is_never_success(fake, capsys, mode):
    fake.post_mode = mode
    rel = _write_input(fake, CID_COMMENT, _comment_input())
    assert _run(fake, CID_COMMENT, rel) != 0
    assert _result(capsys)["status"] != "ok"
    assert fake.posts == 1 and fake.comments == []
    assert not (
        fake.project
        / "artifacts"
        / str(ISSUE)
        / "issue-metadata"
        / CID_COMMENT
        / "issue_comment_publish.marker.json"
    ).exists()


def test_comment_publish_timeout_after_apply_is_read_back_once(fake, capsys):
    fake.post_mode = "timeout_applied"
    rel = _write_input(fake, CID_COMMENT, _comment_input())
    assert _run(fake, CID_COMMENT, rel) == 0
    out = _result(capsys)
    assert fake.posts == 1 and len(fake.comments) == 1  # never re-POSTed
    assert out["status"] == "ok" and out["comment_id"] == "IC_1"


def test_comment_publish_stored_body_differs_from_sent_is_not_success(fake, capsys):
    fake.post_mode = "mutated"
    rel = _write_input(fake, CID_COMMENT, _comment_input())
    assert _run(fake, CID_COMMENT, rel) == 1
    out = _result(capsys)
    assert out["reason"] == "postcondition_body_sha256_mismatch" and out["status"] == "failed"
    marker = fake.project / "artifacts" / str(ISSUE) / "issue-metadata" / CID_COMMENT
    assert not (marker / "issue_comment_publish.marker.json").exists()


def test_comment_publish_failed_post_with_foreign_same_marker_comment_is_unknown(fake, capsys):
    # POST fails, yet a same-marker comment with different content exists: ambiguous, not created.
    fake.post_mode = "fail"
    fake.comments = [{"id": "IC_z", "url": "u#issuecomment-5", "body": "other <!-- cm-1 -->"}]
    rel = _write_input(fake, CID_COMMENT, _comment_input())
    rc = _run(fake, CID_COMMENT, rel)
    out = _result(capsys)
    assert rc != 0 and out["status"] != "ok"
    assert fake.posts <= 1 and len(fake.comments) == 1


def test_comment_publish_marker_missing_from_body_is_refused(fake, capsys):
    data = _comment_input()
    data["comment_body"] = "no marker here"
    rel = _write_input(fake, CID_COMMENT, data)
    assert _run(fake, CID_COMMENT, rel) == 2
    assert fake.posts == 0


# --------------------------------------------------------------------------- issue_content.update


def test_content_update_valid_write_then_readback(fake, capsys):
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 0
    out = _result(capsys)
    assert out["status_detail"] == "applied" and out["patch_attempted"] is True
    assert (fake.title, fake.body, fake.patches) == ("new title", "new body\n", 1)
    assert out["new_body_sha256"] == _sha("new body\n")


def test_content_update_dry_run_writes_nothing(fake, capsys):
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel, extra=["--dry-run"]) == 0
    assert _result(capsys)["status_detail"] == "dry_run_ok"
    assert fake.patches == 0 and fake.body == "old body"
    assert fake.gh_calls() == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("expected_previous_title", "someone else's title", "stale_precondition_title_mismatch"),
        ("expected_previous_body_sha256", _sha("other"), "stale_precondition_body_sha256_mismatch"),
        (
            "expected_previous_updated_at",
            "1999-01-01T00:00:00Z",
            "stale_precondition_updated_at_mismatch",
        ),
    ],
)
def test_content_update_stale_precondition_never_patches(fake, capsys, field, value, reason):
    rel = _write_input(fake, CID_CONTENT, _content_input(fake, **{field: value}))
    assert _run(fake, CID_CONTENT, rel) == 1
    assert _result(capsys)["reason"] == reason
    assert fake.patches == 0 and fake.body == "old body"


def test_content_update_second_run_is_idempotent(fake, capsys):
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 0
    capsys.readouterr()
    assert _run(fake, CID_CONTENT, rel) == 0
    assert _result(capsys)["status_detail"] == "already_applied"
    assert fake.patches == 1


@pytest.mark.parametrize("mode", ["fail", "timeout_lost"])
def test_content_update_failed_patch_is_never_success_and_not_retried(fake, capsys, mode):
    fake.patch_mode = mode
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 1
    out = _result(capsys)
    assert out["status"] == "failed" and out["mutation_outcome"] == "unknown"
    assert fake.patches == 1 and fake.body == "old body"


def test_content_update_readback_mismatch_after_silent_noop_is_not_success(fake, capsys):
    fake.patch_mode = "noop"  # exit 0 but nothing persisted
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 1
    out = _result(capsys)
    assert out["reason"].startswith("postcondition_new_title_mismatch")
    assert fake.patches == 1


def test_content_update_timeout_after_apply_is_read_back_not_retried(fake, capsys):
    fake.patch_mode = "timeout_applied"
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 0
    assert _result(capsys)["status_detail"] == "already_applied"
    assert fake.patches == 1 and fake.body == "new body\n"


def test_content_update_partial_title_only_apply_is_not_success(fake, capsys, monkeypatch):
    def partial(data):
        fake.title = data["title"]  # title persisted, body lost
        return "{}"

    monkeypatch.setattr(fake, "_apply_patch", partial)
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 1
    assert _result(capsys)["reason"] == "postcondition_new_body_sha256_mismatch"


def test_content_update_pull_request_target_is_refused(fake, capsys):
    fake.is_pr = True
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 2
    assert _result(capsys)["reason"] == "target_is_pull_request"
    assert fake.patches == 0


def test_content_update_input_naming_a_wrong_repo_is_refused(fake, capsys):
    rel = _write_input(fake, CID_CONTENT, _content_input(fake, repo=LEGACY))
    assert _run(fake, CID_CONTENT, rel) == 2
    assert fake.patches == 0 and fake.gh_calls() == []


# --------------------------------------------------------------------------- issue_body.update


def test_body_update_valid_write_then_readback(fake, capsys):
    rel = _write_input(fake, CID_BODY, _body_input(fake))
    assert _run(fake, CID_BODY, rel) == 0
    assert _result(capsys)["status"] == "ok"
    assert fake.body == "new body\n" and fake.patches == 1


def test_body_update_stale_precondition_never_patches(fake, capsys):
    rel = _write_input(fake, CID_BODY, _body_input(fake, previous_body_sha256=_sha("other")))
    assert _run(fake, CID_BODY, rel) == 1
    assert _result(capsys)["status"] != "ok"
    assert fake.patches == 0 and fake.body == "old body"


def test_body_update_stale_updated_at_never_patches(fake, capsys):
    rel = _write_input(
        fake, CID_BODY, _body_input(fake, previous_updated_at="2020-01-01T00:00:00Z")
    )
    assert _run(fake, CID_BODY, rel) == 1
    out = _result(capsys)
    assert out["status"] != "ok"
    assert str(out["reason"]).startswith("stale_precondition_updated_at_mismatch")
    assert fake.patches == 0 and fake.body == "old body"


@pytest.mark.parametrize("mode", ["fail", "timeout_lost"])
def test_body_update_failed_patch_is_never_success_and_not_retried(fake, capsys, mode):
    fake.patch_mode = mode
    rel = _write_input(fake, CID_BODY, _body_input(fake))
    assert _run(fake, CID_BODY, rel) != 0
    assert _result(capsys)["status"] != "ok"
    assert fake.patches == 1 and fake.body == "old body"


def test_body_update_silent_noop_is_not_success(fake, capsys):
    fake.patch_mode = "noop"
    rel = _write_input(fake, CID_BODY, _body_input(fake))
    assert _run(fake, CID_BODY, rel) != 0
    assert _result(capsys)["status"] != "ok"
    assert fake.body == "old body"


# --------------------------------------------------------------------------- executor argv gate


def test_executor_exists_and_argv_gate_resolves_to_it() -> None:
    assert EXECUTOR.is_file()
    assert (ROOT / policy.EXECUTOR_SCRIPT).resolve() == EXECUTOR.resolve()
    cmd = (
        f"python3 {policy.EXECUTOR_SCRIPT} --command-id {CID_COMMENT} "
        f"--issue-number 1 --input-file artifacts/1/issue-metadata/{CID_COMMENT}/i.json "
        f"--repo {TRUSTED} --json"
    )
    assert policy.is_controlled_skill_mutation_exec_command(cmd, str(ROOT))
    for tail in ("; echo pwned", " | cat", " --unknown-flag x", "$(id)", "`id`"):
        assert not policy.is_controlled_skill_mutation_exec_command(cmd + tail, str(ROOT))


def test_executor_script_is_not_world_executable_entry() -> None:
    # Imported and invoked as `python3 <path>`; never made directly executable.
    assert not EXECUTOR.stat().st_mode & 0o111


def test_manifest_pins_files_and_honest_deferrals() -> None:
    m = json.loads((ROOT / "docs" / "dev" / "stage2-1b-exec-manifest.json").read_text())
    assert m["source"]["commit"] == "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
    for rel in m["required_files"]:
        assert (ROOT / rel).is_file(), rel
    assert set(m["test_files"]) == set(m["expected_collect"]) == set(m["expected_ran"])
    for entry in m["deferred_tests"]:
        assert all(entry.get(k) for k in ("id", "owner_stage", "reason", "rerun_condition"))
    refs = {d["script"]: d["owner_stage"] for d in m["deferred_script_refs"]}
    stage1a_refs = {
        d["script"]
        for d in json.loads((ROOT / "docs/dev/stage1a-core-manifest.json").read_text())[
            "import_closure"
        ]["deferred_script_refs"]
    }
    # no ghost entries: every ref this manifest records is still deferred in the authoritative
    # stage1a manifest, and the two I-4 / scope-snapshot refs are present.
    assert set(refs) <= stage1a_refs
    assert {"ensure_contract_snapshot.py", "materialize_issue_scope_snapshot.py"} <= set(refs)
    assert "controlled_skill_mutation_exec.py" not in refs
    assert "controlled_skill_mutation_exec.py" not in m["dangling_refs"]
    assert m["live_operations"]["approved"] == sorted(APPROVED)
    assert m["live_operations"]["refused"] == sorted(UNAPPROVED)
    stage1a = json.loads((ROOT / "docs/dev/stage1a-core-manifest.json").read_text())
    listed = {d["script"] for d in stage1a["import_closure"]["deferred_script_refs"]}
    assert "controlled_skill_mutation_exec.py" not in listed
    assert {"ensure_contract_snapshot.py", "materialize_issue_scope_snapshot.py"} <= listed
    assert "docs/dev/stage2-1b-exec-manifest.json" in stage1a["later_stage_test_manifests"]
