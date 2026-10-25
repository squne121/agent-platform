"""Stage 2-1c AC4 / AC5: comment-PATCH and issue-title reconciliation (fixture evidence only).

Every `gh` call goes to an in-process fake; git runs against a throw-away repository. No network
and no real GitHub write: nothing here is live evidence.

AC4  issue_comment.publish, update-an-existing-marker-comment branch (generic and human-history):
     positive (same issue, marker, authenticated-actor author, numeric id from the permalink,
     PATCH then fresh body-SHA readback) and negatives (foreign author, no marker hit, duplicate
     marker, unresolvable comment id, unconfirmed actor, PATCH failure / timeout / partial,
     readback failure / SHA mismatch) that must never report success.
AC5  issue_content.update: title-only and reverse-partial applies, stale preconditions leave the
     title untouched, timeout reconciliation through an authoritative readback, idempotent re-run.
     (title+body, the three stale checks, a title-only partial and timeout-applied are also pinned
     in tests/stage2/test_stage2_1b_exec_closure.py; the cases here are the ones it lacks.)
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import controlled_skill_mutation_exec as ex

TRUSTED = ex.TRUSTED_REPO
ISSUE = 4242
ACTOR = "trusted-actor"
CID_COMMENT = "issue_comment.publish"
CID_CONTENT = "issue_content.update"
_real_subprocess_run = subprocess.run


def _real_run(argv, *args, **kwargs):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if kwargs.get("env") is None:
        kwargs["env"] = env
    return _real_subprocess_run(argv, *args, **kwargs)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeGh:
    """Stateful in-process `gh`; `calls` records every subprocess call (gh and git)."""

    def __init__(self) -> None:
        self.gh = "/fake/trusted/gh"
        self.calls: list[list[str]] = []
        self.unexpected: list[list[str]] = []
        # issue (title/body)
        self.title = "old title"
        self.body = "old body"
        self.updated_at = "2026-01-01T00:00:00Z"
        self.issue_patch_mode = "ok"  # ok | fail | noop | timeout_applied | timeout_lost
        self.issue_patches = 0
        self.partial: str | None = None  # None | "title_only" | "body_only"
        # comments
        self.comments: list[dict] = []
        self.login: str | None = ACTOR
        self.user_rc = 0
        self.comment_patch_mode = (
            "ok"  # ok | fail | noop | timeout_applied | timeout_lost | mutated
        )
        self.comment_patches: list[tuple[str, str]] = []  # (path, body)
        self.posts = 0
        self.view_fail_after_patch = False
        self.dup_after_patch = False

    def gh_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == self.gh]

    def _cp(self, argv, rc=0, out="", err=""):
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)

    def add_comment(self, body, *, numeric=555000, author=ACTOR, url=None):
        c = {
            "id": f"IC_node_{numeric}",
            "url": url or f"https://github.com/{TRUSTED}/issues/{ISSUE}#issuecomment-{numeric}",
            "body": body,
            "author": None if author is None else {"login": author},
        }
        self.comments.append(c)
        return c

    def run(self, argv, *a, **kw):
        self.calls.append([str(x) for x in argv])
        if argv[0] != self.gh:
            return _real_run(argv, *a, **kw)
        if argv[1:3] == ["issue", "view"]:
            if argv[3] != str(ISSUE) or argv[argv.index("--repo") + 1] != TRUSTED:
                self.unexpected.append(list(argv))
            if self.view_fail_after_patch and self.comment_patches:
                return self._cp(argv, rc=1, err="HTTP 502")
            return self._cp(argv, out=json.dumps({"comments": self.comments}))
        assert argv[1] == "api", argv
        if "--hostname" not in argv or argv[argv.index("--hostname") + 1] != "github.com":
            self.unexpected.append(list(argv))
        if argv[-3:] == ["user", "--jq", ".login"]:
            if self.user_rc:
                return self._cp(argv, rc=1, err="HTTP 401")
            return self._cp(argv, out=(self.login or "") + "\n")
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        path = next(x for x in argv if x.startswith("repos/"))
        if method == "PATCH" and "/issues/comments/" in path:
            if not path.startswith(f"repos/{TRUSTED}/issues/comments/"):
                self.unexpected.append(list(argv))
            body = Path(argv[argv.index("--field") + 1].split("=@", 1)[1]).read_text()
            self.comment_patches.append((path, body))
            return self._write(
                argv, self.comment_patch_mode, lambda: self._apply_comment(path, body)
            )
        if method == "POST" and path == f"repos/{TRUSTED}/issues/{ISSUE}/comments":
            self.posts += 1
            body = Path(argv[argv.index("--field") + 1].split("=@", 1)[1]).read_text()
            n = len(self.comments) + 1
            url = f"https://github.com/{TRUSTED}/issues/{ISSUE}#issuecomment-{9000 + n}"
            self.add_comment(body, numeric=9000 + n, author=self.login, url=url)
            return self._cp(argv, out=json.dumps({"html_url": url, "id": 9000 + n}))
        if method == "GET" and path == f"repos/{TRUSTED}/issues/{ISSUE}":
            return self._cp(
                argv,
                out=json.dumps(
                    {
                        "title": self.title,
                        "body": self.body,
                        "updatedAt": self.updated_at,
                        "isPullRequest": False,
                    }
                ),
            )
        if method == "PATCH" and path == f"repos/{TRUSTED}/issues/{ISSUE}":
            self.issue_patches += 1
            data = json.loads(Path(argv[argv.index("--input") + 1]).read_text())
            return self._write(argv, self.issue_patch_mode, lambda: self._apply_issue(data))
        self.unexpected.append(list(argv))
        raise AssertionError(f"unexpected gh call: {argv}")

    def _write(self, argv, mode, apply):
        if mode == "fail":
            return self._cp(argv, rc=1, err="boom")
        if mode == "timeout_lost":
            raise subprocess.TimeoutExpired(argv, 15)
        if mode == "timeout_applied":
            apply()
            raise subprocess.TimeoutExpired(argv, 15)
        if mode != "noop":
            apply()
        return self._cp(argv, out="{}")

    def _apply_comment(self, path, body):
        numeric = path.rsplit("/", 1)[1]
        for c in self.comments:
            if c["url"].endswith(f"issuecomment-{numeric}"):
                c["body"] = body + ("X" if self.comment_patch_mode == "mutated" else "")
        if self.dup_after_patch:
            self.add_comment(body, numeric=777000)

    def _apply_issue(self, data):
        if self.partial == "title_only":
            self.title = data["title"]
        elif self.partial == "body_only":
            self.body = data["body"]
        else:
            self.title, self.body = data["title"], data["body"]
        self.updated_at = "2026-01-02T00:00:00Z"


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


def _write_input(fg: FakeGh, command_id: str, data: dict) -> str:
    rel = f"artifacts/{ISSUE}/issue-metadata/{command_id}/input.json"
    path = fg.project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return rel


def _run(fg, command_id, rel, extra=()):
    argv = ["--command-id", command_id, "--issue-number", str(ISSUE), "--repo", TRUSTED, "--json"]
    return ex.main([*argv, "--input-file", rel, *extra])


def _result(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


MARKER = "<!-- ac4-report:v1 issue=4242 -->"


def _generic_body(text: str) -> str:
    return f"{MARKER}\n{text}\n"


def _comment_input(body: str, marker: str = MARKER) -> dict:
    return {
        "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
        "issue_number": ISSUE,
        "comment_body": body,
        "marker": marker,
    }


# ----------------------------------------------------------------------------- AC4 generic


def test_ac4_update_own_marker_comment_patches_then_reads_back_sha(fake, capsys):
    old, new = _generic_body("old payload"), _generic_body("new payload")
    fake.add_comment(old, numeric=555000)
    rel = _write_input(fake, CID_COMMENT, _comment_input(new))
    assert _run(fake, CID_COMMENT, rel) == 0
    out = _result(capsys)
    assert out["status_detail"] == "updated"
    assert out["body_sha256"] == _hex(new)
    assert out["comment_id"] == "IC_node_555000"
    assert fake.posts == 0
    # PATCH target = numeric id of the matched same-issue comment's permalink, trusted repo only
    assert fake.comment_patches == [(f"repos/{TRUSTED}/issues/comments/555000", new)]
    assert fake.comments[0]["body"] == new
    # the actor lookup precedes the PATCH, and a fresh issue view follows it (readback)
    kinds = ["user" if c[-3:] == ["user", "--jq", ".login"] else c[1] for c in fake.gh_calls()]
    assert kinds.index("user") < next(i for i, c in enumerate(fake.gh_calls()) if "--method" in c)
    assert fake.gh_calls()[-1][1:3] == ["issue", "view"]


def test_ac4_update_is_idempotent_on_rerun(fake, capsys):
    fake.add_comment(_generic_body("old payload"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 0
    capsys.readouterr()
    assert _run(fake, CID_COMMENT, rel) == 0
    assert _result(capsys)["status_detail"] == "already_published"
    assert len(fake.comment_patches) == 1


@pytest.mark.parametrize("author", ["someone-else", None], ids=["other_author", "author_absent"])
def test_ac4_foreign_or_unattributed_comment_is_never_patched(fake, capsys, author):
    fake.add_comment(_generic_body("old payload"), author=author)
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"] == "remote_marker_author_mismatch_pre_mutation"
    assert fake.comment_patches == [] and fake.posts == 0
    assert fake.comments[0]["body"] == _generic_body("old payload")


def test_ac4_other_marker_comment_is_not_a_patch_target(fake, capsys):
    other = "<!-- different-report:v1 -->\nother payload\n"
    fake.add_comment(other, author=ACTOR)
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 0
    assert _result(capsys)["status_detail"] == "created"
    assert fake.comment_patches == [] and fake.posts == 1
    assert fake.comments[0]["body"] == other


def test_ac4_duplicate_marker_hits_fail_closed_before_any_write(fake, capsys):
    fake.add_comment(_generic_body("a"), numeric=1)
    fake.add_comment(_generic_body("b"), numeric=2)
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"] == "duplicate_marker_conflict_pre_mutation"
    assert fake.comment_patches == [] and fake.posts == 0


@pytest.mark.parametrize(
    "url",
    [
        "",
        f"https://github.com/{TRUSTED}/issues/{ISSUE}",
        f"https://github.com/{TRUSTED}/issues/{ISSUE}#issuecomment-abc",
        f"https://github.com/{TRUSTED}/issues/{ISSUE}#issuecomment-12x",
        f"https://github.com/{TRUSTED}/issues/{ISSUE}#issuecomment-",
        f"https://github.com/{TRUSTED}/issues/{ISSUE}#issuecomment-5/../../x",
    ],
)
def test_ac4_unresolvable_comment_id_is_never_patched(fake, capsys, url):
    fake.add_comment(_generic_body("old payload"), url=url or "no-url")
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"] == "remote_marker_comment_id_unresolvable_pre_mutation"
    assert fake.comment_patches == []


@pytest.mark.parametrize("mode", ["rc", "empty"])
def test_ac4_unconfirmed_actor_blocks_patch(fake, capsys, mode):
    fake.add_comment(_generic_body("old payload"))
    if mode == "rc":
        fake.user_rc = 1
    else:
        fake.login = ""
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"].startswith("marker_update_author_check_failed")
    assert fake.comment_patches == []


@pytest.mark.parametrize("mode", ["fail", "timeout_lost"])
def test_ac4_patch_failure_or_lost_timeout_is_not_success_and_not_retried(fake, capsys, mode):
    fake.comment_patch_mode = mode
    fake.add_comment(_generic_body("old payload"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 1
    out = _result(capsys)
    assert out["status"] == "failed" and out["patch_attempted"] is True
    assert out["mutation_outcome"] == "unknown"
    assert len(fake.comment_patches) == 1
    assert fake.comments[0]["body"] == _generic_body("old payload")


def test_ac4_timeout_after_apply_is_unknown_then_rerun_reconciles_without_second_patch(
    fake, capsys
):
    fake.comment_patch_mode = "timeout_applied"
    fake.add_comment(_generic_body("old payload"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 1  # not reported as success inside the same run
    assert _result(capsys)["mutation_outcome"] == "unknown"
    assert _run(fake, CID_COMMENT, rel) == 0  # authoritative remote state decides on the re-run
    assert _result(capsys)["status_detail"] == "already_published"
    assert len(fake.comment_patches) == 1


def test_ac4_partial_apply_sha_mismatch_is_not_success(fake, capsys):
    fake.comment_patch_mode = "mutated"  # persisted, not byte-identical to what was sent
    fake.add_comment(_generic_body("old payload"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 1
    out = _result(capsys)
    assert out["reason"] == "postcondition_body_sha256_mismatch"
    assert out["mutation_outcome"] == "unknown"


def test_ac4_silent_noop_patch_is_not_success(fake, capsys):
    fake.comment_patch_mode = "noop"  # exit 0, nothing persisted
    fake.add_comment(_generic_body("old payload"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"] == "postcondition_body_sha256_mismatch"


def test_ac4_readback_failure_after_patch_is_not_success(fake, capsys):
    fake.view_fail_after_patch = True
    fake.add_comment(_generic_body("old payload"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 1
    out = _result(capsys)
    assert out["reason"].startswith("readback_failed")
    assert out["mutation_outcome"] == "unknown"


def test_ac4_ambiguous_readback_after_patch_is_not_success(fake, capsys):
    fake.dup_after_patch = True  # a second marker comment shows up: readback is not unique
    fake.add_comment(_generic_body("old payload"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"].startswith("readback_failed")


def test_ac4_dry_run_never_calls_gh(fake, capsys):
    fake.add_comment(_generic_body("old payload"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_generic_body("new payload")))
    assert _run(fake, CID_COMMENT, rel, extra=["--dry-run"]) == 0
    assert fake.gh_calls() == [] and fake.comment_patches == []


# ----------------------------------------------------------------------------- AC4 human-history


def _hh_marker(seed: str = "identity") -> str:
    return f"<!-- loop-protocol/human-history:v1:sha256:{_hex(seed)} -->"


def _hh_body(visible: str, marker: str | None = None) -> str:
    return f"{visible}\n- phase: review-complete\n{marker or _hh_marker()}\n"


def test_ac4_human_history_update_patches_own_comment_and_reads_back_digest(fake, capsys):
    fake.add_comment(_hh_body("古い履歴"), numeric=700)
    new = _hh_body("新しい履歴")
    rel = _write_input(fake, CID_COMMENT, _comment_input(new, _hh_marker()))
    assert _run(fake, CID_COMMENT, rel) == 0
    out = _result(capsys)
    assert out["status_detail"] == "updated" and out["patch_attempted"] is True
    assert fake.comment_patches == [(f"repos/{TRUSTED}/issues/comments/700", new)]
    assert fake.comments[0]["body"] == new


def test_ac4_human_history_rerun_is_noop(fake, capsys):
    fake.add_comment(_hh_body("古い履歴"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_hh_body("新しい履歴"), _hh_marker()))
    assert _run(fake, CID_COMMENT, rel) == 0
    capsys.readouterr()
    assert _run(fake, CID_COMMENT, rel) == 0
    assert _result(capsys)["status_detail"] == "already_published"
    assert len(fake.comment_patches) == 1


def test_ac4_human_history_foreign_author_never_patched(fake, capsys):
    fake.add_comment(_hh_body("古い履歴"), author="someone-else")
    rel = _write_input(fake, CID_COMMENT, _comment_input(_hh_body("新しい履歴"), _hh_marker()))
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"] == "human_history_remote_marker_author_mismatch"
    assert fake.comment_patches == [] and fake.posts == 0


def test_ac4_human_history_duplicate_owned_marker_blocks(fake, capsys):
    fake.add_comment(_hh_body("a"), numeric=1)
    fake.add_comment(_hh_body("b"), numeric=2)
    rel = _write_input(fake, CID_COMMENT, _comment_input(_hh_body("新しい履歴"), _hh_marker()))
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"] == "human_history_duplicate_owned_marker"
    assert fake.comment_patches == [] and fake.posts == 0


def test_ac4_human_history_unresolvable_comment_id_never_patched(fake, capsys):
    fake.add_comment(_hh_body("古い履歴"), url="https://example.invalid/not-a-permalink")
    rel = _write_input(fake, CID_COMMENT, _comment_input(_hh_body("新しい履歴"), _hh_marker()))
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"] == "human_history_remote_marker_comment_id_unresolvable"
    assert fake.comment_patches == []


@pytest.mark.parametrize("mode", ["fail", "timeout_lost", "timeout_applied"])
def test_ac4_human_history_patch_failure_is_not_success(fake, capsys, mode):
    fake.comment_patch_mode = mode
    fake.add_comment(_hh_body("古い履歴"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_hh_body("新しい履歴"), _hh_marker()))
    assert _run(fake, CID_COMMENT, rel) == 1
    out = _result(capsys)
    assert out["status"] == "failed" and out["mutation_outcome"] == "unknown"
    assert len(fake.comment_patches) == 1


@pytest.mark.parametrize("mode", ["noop", "mutated"])
def test_ac4_human_history_readback_digest_mismatch_is_not_success(fake, capsys, mode):
    fake.comment_patch_mode = mode
    fake.add_comment(_hh_body("古い履歴"))
    rel = _write_input(fake, CID_COMMENT, _comment_input(_hh_body("新しい履歴"), _hh_marker()))
    assert _run(fake, CID_COMMENT, rel) == 1
    assert _result(capsys)["reason"].startswith("human_history_readback_failed")


# ----------------------------------------------------------------------------- AC5


def _content_input(fg: FakeGh, **over) -> dict:
    new_body = over.pop("_new_body", "new body\n")
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


def test_ac5_title_only_update_keeps_body_and_reads_back(fake, capsys):
    rel = _write_input(fake, CID_CONTENT, _content_input(fake, _new_body=fake.body))
    assert _run(fake, CID_CONTENT, rel) == 0
    out = _result(capsys)
    assert out["status_detail"] == "applied" and out["new_body_sha256"] == _sha("old body")
    assert (fake.title, fake.body, fake.issue_patches) == ("new title", "old body", 1)


def test_ac5_title_only_that_is_silently_ignored_is_not_success(fake, capsys):
    fake.issue_patch_mode = "noop"
    rel = _write_input(fake, CID_CONTENT, _content_input(fake, _new_body=fake.body))
    assert _run(fake, CID_CONTENT, rel) == 1
    assert _result(capsys)["reason"] == "postcondition_new_title_mismatch"
    assert fake.title == "old title"


def test_ac5_title_only_rerun_is_idempotent(fake, capsys):
    rel = _write_input(fake, CID_CONTENT, _content_input(fake, _new_body=fake.body))
    assert _run(fake, CID_CONTENT, rel) == 0
    capsys.readouterr()
    assert _run(fake, CID_CONTENT, rel) == 0
    assert _result(capsys)["status_detail"] == "already_applied"
    assert fake.issue_patches == 1


def test_ac5_body_applied_but_title_not_is_not_success(fake, capsys):
    fake.partial = "body_only"
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 1
    out = _result(capsys)
    assert out["reason"] == "postcondition_new_title_mismatch"
    assert out["mutation_outcome"] == "unknown"


def test_ac5_title_only_applied_after_timeout_is_not_reconciled_as_success(fake, capsys):
    fake.partial = "title_only"
    fake.issue_patch_mode = "timeout_applied"
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 1
    out = _result(capsys)
    assert out["status"] == "failed" and out["mutation_outcome"] == "unknown"
    assert fake.issue_patches == 1 and fake.title == "new title" and fake.body == "old body"


def test_ac5_timeout_with_both_fields_applied_is_reconciled_by_readback(fake, capsys):
    fake.issue_patch_mode = "timeout_applied"
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 0
    out = _result(capsys)
    assert out["status_detail"] == "already_applied" and out["mutation_outcome"] == "applied"
    assert fake.issue_patches == 1


@pytest.mark.parametrize("which", ["title", "body_sha", "updated_at"])
def test_ac5_stale_precondition_leaves_title_and_body_untouched(fake, capsys, which):
    over = {
        "title": {"expected_previous_title": "someone else's title"},
        "body_sha": {"expected_previous_body_sha256": _sha("other")},
        "updated_at": {"expected_previous_updated_at": "1999-01-01T00:00:00Z"},
    }[which]
    rel = _write_input(fake, CID_CONTENT, _content_input(fake, **over))
    assert _run(fake, CID_CONTENT, rel) == 1
    assert _result(capsys)["reason"].startswith("stale_precondition_")
    assert (fake.title, fake.body, fake.issue_patches) == ("old title", "old body", 0)


def test_ac5_remote_title_changed_between_plan_and_apply_is_stale_even_for_title_only(fake, capsys):
    rel = _write_input(fake, CID_CONTENT, _content_input(fake, _new_body=fake.body))
    fake.title = "edited by someone else"
    assert _run(fake, CID_CONTENT, rel) == 1
    assert _result(capsys)["reason"] == "stale_precondition_title_mismatch"
    assert fake.issue_patches == 0 and fake.title == "edited by someone else"


@pytest.mark.parametrize("mode", ["fail", "timeout_lost"])
def test_ac5_failed_or_lost_patch_is_unknown_and_never_retried(fake, capsys, mode):
    fake.issue_patch_mode = mode
    rel = _write_input(fake, CID_CONTENT, _content_input(fake))
    assert _run(fake, CID_CONTENT, rel) == 1
    out = _result(capsys)
    assert out["status"] == "failed" and out["mutation_outcome"] == "unknown"
    assert fake.issue_patches == 1 and fake.title == "old title"


# ----------------------------------------------------------------------------- binding gates
# Review residue of PR #40: input containment, LOOP_ISSUE_NUMBER, git origin forms and the gh
# hostname are bound before / around every write. Each negative is a write-zero check against a
# comment lane that WOULD patch if the gate were removed; a positive control proves the lane.


def _arm_comment_update(fg: FakeGh) -> str:
    fg.add_comment(_generic_body("old payload"))
    return _write_input(fg, CID_COMMENT, _comment_input(_generic_body("new payload")))


def test_binding_positive_control_reaches_the_patch(fake, capsys):
    rel = _arm_comment_update(fake)
    assert _run(fake, CID_COMMENT, rel) == 0
    assert len(fake.comment_patches) == 1


@pytest.mark.parametrize(
    "origin",
    [
        f"git@github.com:{TRUSTED}.git",
        f"ssh://git@github.com/{TRUSTED}.git",
        f"https://github.com/{TRUSTED}",
    ],
    ids=["scp_like", "ssh_url", "https_no_suffix"],
)
def test_git_origin_canonical_forms_are_accepted(fake, capsys, origin):
    _real_run(["git", "-C", str(fake.project), "remote", "set-url", "origin", origin], check=True)
    rel = _arm_comment_update(fake)
    assert _run(fake, CID_COMMENT, rel) == 0
    assert len(fake.comment_patches) == 1


_OTHER = TRUSTED.split("/")[0] + "/some-other-repository"


@pytest.mark.parametrize(
    "origin",
    [
        f"git@github.com:{_OTHER}.git",
        f"git@evil.example:{TRUSTED}.git",
        f"attacker@github.com:{TRUSTED}.git",
        f"github.com:{_OTHER}",
        f"ssh://git@github.com/{_OTHER}.git",
        f"ssh://git@evil.example/{TRUSTED}.git",
        f"ssh://attacker@github.com/{TRUSTED}.git",
        f"ssh://git@github.com:2222/{TRUSTED}.git",
        f"https://evil.example/{TRUSTED}.git",
        f"https://github.com.evil.example/{TRUSTED}.git",
        f"https://github.com:8443/{TRUSTED}.git",
        f"https://token@github.com/{TRUSTED}.git",
        f"http://github.com/{TRUSTED}.git",
        f"file:///{TRUSTED}",
        f"git://github.com/{TRUSTED}.git",
        f"https://github.com/{_OTHER}.git",
    ],
    ids=[
        "scp_other_repo",
        "scp_other_host",
        "scp_other_user",
        "scp_no_user_other_repo",
        "ssh_other_repo",
        "ssh_other_host",
        "ssh_other_user",
        "ssh_other_port",
        "https_other_host",
        "https_host_suffix",
        "https_other_port",
        "https_userinfo",
        "http_scheme",
        "file_scheme",
        "git_scheme",
        "https_other_repo",
    ],
)
def test_git_origin_other_forms_are_refused_before_any_gh_call(fake, capsys, origin):
    _real_run(["git", "-C", str(fake.project), "remote", "set-url", "origin", origin], check=True)
    rel = _arm_comment_update(fake)
    assert _run(fake, CID_COMMENT, rel) == 2
    assert _result(capsys)["reason"].startswith("git_remote_origin_")
    assert fake.gh_calls() == [] and fake.comment_patches == []


def test_loop_issue_number_mismatch_is_refused_before_any_gh_call(fake, capsys, monkeypatch):
    rel = _arm_comment_update(fake)
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", str(ISSUE + 1))
    assert _run(fake, CID_COMMENT, rel) == 2
    assert fake.gh_calls() == [] and fake.comment_patches == []


def test_loop_issue_number_matching_is_accepted(fake, capsys, monkeypatch):
    rel = _arm_comment_update(fake)
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", str(ISSUE))
    assert _run(fake, CID_COMMENT, rel) == 0


@pytest.mark.parametrize("value", ["abc", "42x", "-4242", "4243 ", "٤٢٤٣", "4242.0"])
def test_loop_issue_number_non_digit_or_other_issue_is_refused(fake, capsys, monkeypatch, value):
    rel = _arm_comment_update(fake)
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", value)
    assert _run(fake, CID_COMMENT, rel) == 2
    assert fake.gh_calls() == [] and fake.comment_patches == []


def test_input_file_of_another_issue_subtree_is_refused(fake, capsys):
    _arm_comment_update(fake)
    other = f"artifacts/{ISSUE + 1}/issue-metadata/{CID_COMMENT}/input.json"
    path = fake.project / other
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_comment_input(_generic_body("new payload"))))
    assert _run(fake, CID_COMMENT, other) == 2
    assert fake.gh_calls() == [] and fake.comment_patches == []


def test_input_file_outside_artifacts_is_refused(fake, capsys):
    _arm_comment_update(fake)
    (fake.project / "input.json").write_text(
        json.dumps(_comment_input(_generic_body("new payload")))
    )
    assert _run(fake, CID_COMMENT, "input.json") == 2
    assert fake.gh_calls() == [] and fake.comment_patches == []


def test_input_file_symlink_to_another_issue_is_refused(fake, capsys):
    _arm_comment_update(fake)
    other_dir = fake.project / f"artifacts/{ISSUE + 1}/issue-metadata/{CID_COMMENT}"
    other_dir.mkdir(parents=True)
    (other_dir / "input.json").write_text(json.dumps(_comment_input(_generic_body("new payload"))))
    link = fake.project / f"artifacts/{ISSUE}/issue-metadata/{CID_COMMENT}/link.json"
    link.symlink_to(other_dir / "input.json")
    assert _run(fake, CID_COMMENT, f"artifacts/{ISSUE}/issue-metadata/{CID_COMMENT}/link.json") == 2
    assert fake.gh_calls() == [] and fake.comment_patches == []


def test_every_gh_write_and_actor_lookup_is_pinned_to_the_trusted_host(fake, capsys):
    rel = _arm_comment_update(fake)
    assert _run(fake, CID_COMMENT, rel) == 0
    api_calls = [c for c in fake.gh_calls() if c[1] == "api"]
    assert api_calls  # the actor lookup and the PATCH both go through `gh api`
    assert all(c[2:4] == ["--hostname", "github.com"] for c in api_calls)
    assert all(c[0] == fake.gh for c in fake.gh_calls())


@pytest.mark.parametrize("value", [" 4242 ", "٤٢٤٢"], ids=["whitespace", "unicode_digits"])
def test_loop_issue_number_lenient_spellings_still_bind_only_the_same_issue(
    fake, capsys, monkeypatch, value
):
    """Characterization (Minor observation): `.strip()` + `str.isdigit()` accept padded and
    non-ASCII-digit spellings, but only when they are numerically the bound issue."""
    rel = _arm_comment_update(fake)
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", value)
    assert _run(fake, CID_COMMENT, rel) == 0
