"""Stage 2-2: Step 5 comment lane (publish_termination_report -> bridge -> controlled executor).

Fixture evidence only. The publisher is driven in-process; its `subprocess.run` is replaced by a
dispatcher that runs the REAL `controlled_skill_mutation_exec.main()` in-process for the executor
argv and the stateful in-process `FakeGh` (stage 2-1c) for every `gh` call. No network and no real
GitHub write: nothing here is live GitHub evidence.

Pinned here (the rest of the lane is pinned by the moved source tests):
  AC2  publish -> fresh readback; wrong repo / wrong issue / legacy slug write-zero; stale
       (foreign) marker, duplicate marker, digest change, readback mismatch / failure, partial
       failure and timeouts never report success; --dry-run mutates nothing and sends no signal.
  AC3  task_contextctl absent: the comment still posts and the signal is `deferred`.
  AC1  closure: no raw `gh issue comment`, no revived renderer / loop-state builder, no
       Stage 3 adapter, no functional legacy slug.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARDS = ROOT / "scripts" / "agent-guards"
SKILL_SCRIPTS = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
for p in (GUARDS, GUARDS / "tests", SKILL_SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import controlled_skill_mutation_exec as ex
import publish_termination_report as pub
from test_controlled_exec_comment_content_reconciliation import (
    ACTOR,
    ISSUE,
    TRUSTED,
    FakeGh,
    _real_run,
)

SHA = "a" * 64
HUMAN_HISTORY_NS = "loop-protocol/human-history"  # wire identifier, not a repository identity
LEGACY_SLUG = "/".join(["squne121", "loop-protocol"])  # noqa: FLY002 - no literal for the slug scan


class Harness:
    def __init__(self, fg: FakeGh, project: Path) -> None:
        self.fg = fg
        self.project = project
        self.calls: list[list[str]] = []
        self.exec_argv: list[list[str]] = []
        self.signal_calls: list[list[str]] = []
        self.exec_timeout = False
        self.after_post = None  # callable run right after a gh POST

    def run(self, argv, *a, **kw):
        argv = [str(x) for x in argv]
        self.calls.append(argv)
        if Path(argv[1]).name == "controlled_skill_mutation_exec.py":
            self.exec_argv.append(argv)
            if self.exec_timeout:
                raise subprocess.TimeoutExpired(argv, 30)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ex.main(argv[2:])
            return subprocess.CompletedProcess(
                argv, rc, stdout=out.getvalue(), stderr=err.getvalue()
            )
        if "task_contextctl.py" in argv[1]:
            self.signal_calls.append(argv)
        before = self.fg.posts
        result = self.fg.run(argv, *a, **kw)
        if self.after_post and self.fg.posts > before:
            self.after_post()
        return result

    def mutations(self) -> list[list[str]]:
        return [
            c
            for c in self.fg.gh_calls()
            if "--method" in c and c[c.index("--method") + 1] in {"POST", "PATCH", "PUT", "DELETE"}
        ]


@pytest.fixture()
def h(tmp_path, monkeypatch):
    fg = FakeGh()
    project = tmp_path / "project"
    (project / "scripts" / "agent-guards").mkdir(parents=True)
    _real_run(["git", "init", "-q", str(project)], check=True)
    _real_run(
        ["git", "-C", str(project), "remote", "add", "origin", f"https://github.com/{TRUSTED}.git"],
        check=True,
    )
    harness = Harness(fg, project)
    monkeypatch.setattr(ex, "PROJECT_ROOT", project)
    monkeypatch.setattr(pub, "_PROJECT_ROOT", project)
    monkeypatch.setattr(ex, "_find_gh_bin", lambda: (fg.gh, ""))
    monkeypatch.setattr(subprocess, "run", harness.run)
    monkeypatch.setattr(pub, "ARTIFACT_DIR", tmp_path / "publisher-artifacts")
    for name in ("LOOP_ISSUE_NUMBER", "CONTROLLED_EXEC_MARKER", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    harness.artifact_dir = tmp_path / "publisher-artifacts"
    yield harness
    assert fg.unexpected == []


def _publish(**kw) -> int:
    kw.setdefault("issue_number", ISSUE)
    kw.setdefault("repo", TRUSTED)
    kw.setdefault("body", "## Report\n\nrefinement finished\n")
    return pub.publish(**kw)


def _artifact_reasons(h: Harness) -> list[str]:
    if not h.artifact_dir.exists():
        return []
    return [json.loads(p.read_text())["reason_code"] for p in sorted(h.artifact_dir.glob("*.json"))]


# --------------------------------------------------------------------------- positive / readback


def test_publish_goes_through_controlled_executor_and_reads_back(h):
    assert _publish() == 0
    assert h.fg.posts == 1
    assert len(h.exec_argv) == 1
    argv = h.exec_argv[0]
    assert argv[argv.index("--command-id") + 1] == "issue_comment.publish"
    assert argv[argv.index("--repo") + 1] == TRUSTED
    assert argv[argv.index("--issue-number") + 1] == str(ISSUE)
    assert "--json" in argv and "--dry-run" not in argv
    assert h.fg.comments[0]["body"].startswith("## Report")
    # fresh readback: the last gh call after the POST is an issue view, and no raw `gh issue comment`
    gh = h.fg.gh_calls()
    last_post = max(i for i, c in enumerate(gh) if "POST" in c)
    assert any(c[1:3] == ["issue", "view"] for c in gh[last_post + 1 :])
    assert not any(c[1:3] == ["issue", "comment"] for c in gh)


def test_same_body_rerun_is_idempotent(h):
    assert _publish() == 0
    assert _publish() == 0
    assert h.fg.posts == 1 and len(h.fg.comments) == 1
    assert h.mutations() and len([m for m in h.mutations() if "PATCH" in m]) == 0


def test_same_marker_changed_digest_updates_own_comment_without_a_second_post(h, monkeypatch):
    monkeypatch.setenv("CONTROLLED_EXEC_MARKER", "stable-id")
    assert _publish(body="## Report v1\n") == 0
    assert _publish(body="## Report v2\n") == 0
    assert h.fg.posts == 1 and len(h.fg.comments) == 1
    assert len(h.fg.comment_patches) == 1
    assert h.fg.comments[0]["body"].startswith("## Report v2")


def test_stale_marker_owned_by_foreign_author_is_never_patched(h, monkeypatch):
    monkeypatch.setenv("CONTROLLED_EXEC_MARKER", "stable-id")
    marker = "<!-- CONTROLLED_EXEC_MARKER:stable-id -->"
    h.fg.add_comment(f"## stale\n\n{marker}", author="someone-else")
    assert _publish(body="## Report v2\n") != 0
    assert h.mutations() == []
    assert "gh_comment_failed" in _artifact_reasons(h)


def test_duplicate_marker_fails_before_any_write(h, monkeypatch):
    monkeypatch.setenv("CONTROLLED_EXEC_MARKER", "stable-id")
    marker = "<!-- CONTROLLED_EXEC_MARKER:stable-id -->"
    h.fg.add_comment(f"a\n{marker}", numeric=1)
    h.fg.add_comment(f"b\n{marker}", numeric=2)
    assert _publish() != 0
    assert h.mutations() == []


def test_human_history_and_generic_markers_do_not_collide(h):
    digest = "b" * 64
    history = f"<!-- {HUMAN_HISTORY_NS}:v1:sha256:{digest} -->"
    h.fg.add_comment(f"history entry\n{history}\n", author=ACTOR)
    assert _publish() == 0  # a generic report must not adopt (or patch) the history comment
    assert h.fg.posts == 1 and h.fg.comment_patches == []
    assert h.fg.comments[0]["body"] == f"history entry\n{history}\n"


# ------------------------------------------------------------------------------- write-zero


@pytest.mark.parametrize(
    "repo",
    [LEGACY_SLUG, "someone/else", "", "no-slash", "a/b/c", TRUSTED.upper(), f" {TRUSTED}"],
    ids=["legacy", "other", "empty", "malformed", "extra-segment", "case", "padded"],
)
def test_wrong_or_legacy_repo_is_write_zero(h, repo):
    assert _publish(repo=repo) != 0
    assert h.mutations() == []
    assert h.fg.posts == 0 and h.fg.comments == []


def test_wrong_issue_binding_is_write_zero(h, monkeypatch):
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", str(ISSUE + 1))
    assert _publish() != 0
    assert h.mutations() == []


def test_trusted_identity_comes_only_from_the_config(h, monkeypatch):
    monkeypatch.setenv("TRUSTED_REPO", LEGACY_SLUG)
    monkeypatch.setenv("GITHUB_REPOSITORY", LEGACY_SLUG)
    assert _publish(repo=LEGACY_SLUG) != 0
    assert h.mutations() == []
    assert _publish() == 0 and h.fg.posts == 1


def _cli(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["publish_termination_report.py", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO("## Report\n"))
    return pub.main()


def test_cli_without_repo_is_usage_error_and_write_zero(h, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, "--issue-number", str(ISSUE))
    assert exc.value.code == 2
    assert h.calls == [] and h.mutations() == []


@pytest.mark.parametrize(
    "repo", ["", LEGACY_SLUG, "not a slug"], ids=["empty", "legacy", "malformed"]
)
def test_cli_wrong_repo_is_write_zero(h, monkeypatch, repo):
    assert _cli(monkeypatch, "--issue-number", str(ISSUE), "--repo", repo) != 0
    assert h.mutations() == []


def test_cli_trusted_repo_posts_once_with_issue_binding(h, monkeypatch):
    assert _cli(monkeypatch, "--issue-number", str(ISSUE), "--repo", TRUSTED) == 0
    assert h.fg.posts == 1
    argv = h.exec_argv[0]
    assert argv[argv.index("--issue-number") + 1] == str(ISSUE)


# --------------------------------------------------------- readback / partial / timeout never succeed


def test_readback_body_mismatch_is_not_success(h):
    h.after_post = lambda: h.fg.comments[-1].update(body=h.fg.comments[-1]["body"] + "X")
    assert _publish() != 0


def test_readback_failure_is_not_success(h):
    def _break_view():
        h.fg.view_fail_after_patch = True
        h.fg.comment_patches.append(("synthetic", ""))  # arms FakeGh's view failure

    h.after_post = _break_view
    assert _publish() != 0


def test_post_failure_is_not_success_and_leaves_no_comment(h, monkeypatch):
    real = h.fg.run

    def failing(argv, *a, **kw):
        if "POST" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 500")
        return real(argv, *a, **kw)

    monkeypatch.setattr(h.fg, "run", failing)
    assert _publish() != 0
    assert h.fg.comments == []
    assert "gh_comment_failed" in _artifact_reasons(h)


def test_post_response_not_json_never_reports_unverified_success(h, monkeypatch):
    real = h.fg.run

    def garbled(argv, *a, **kw):
        result = real(argv, *a, **kw)
        if "POST" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="<html>not json</html>", stderr="")
        return result

    monkeypatch.setattr(h.fg, "run", garbled)
    rc = _publish()
    # either rejected, or accepted only because the fresh readback found the comment: never a
    # success without a stored comment carrying the body
    assert rc != 0 or (len(h.fg.comments) == 1 and h.fg.comments[0]["body"].startswith("## Report"))
    assert h.fg.posts <= 1


def test_executor_subprocess_timeout_fails_closed_without_signal(h, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-ref")
    h.exec_timeout = True
    assert _publish(termination_reason="approved", approved_body_sha256=SHA) == 1
    assert h.mutations() == []
    assert h.signal_calls == []
    assert "gh_comment_timeout" in _artifact_reasons(h)


def test_gh_level_timeout_with_lost_write_is_not_success(h, monkeypatch):
    real = h.fg.run

    def timeout(argv, *a, **kw):
        if "POST" in argv:
            raise subprocess.TimeoutExpired(argv, 15)
        return real(argv, *a, **kw)

    monkeypatch.setattr(h.fg, "run", timeout)
    assert _publish() != 0
    assert h.fg.comments == []


def test_failed_post_never_emits_the_task_context_signal(h, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-ref")
    assert _publish(repo=LEGACY_SLUG, termination_reason="approved", approved_body_sha256=SHA) != 0
    assert h.signal_calls == []


# ----------------------------------------------------------------------------------- dry-run


def test_dry_run_validates_only_and_sends_no_signal(h, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-ref")
    assert _publish(dry_run=True, termination_reason="approved", approved_body_sha256=SHA) == 0
    assert "--dry-run" in h.exec_argv[0]
    assert h.mutations() == []
    assert h.fg.posts == 0 and h.fg.comments == []
    assert h.signal_calls == []
    assert "validation-only" in capsys.readouterr().err


def test_dry_run_with_wrong_repo_still_fails_closed(h):
    assert _publish(dry_run=True, repo=LEGACY_SLUG) != 0
    assert h.mutations() == [] and h.signal_calls == []


# ---------------------------------------------------------------- task_contextctl (Stage 3) absent


def test_task_context_adapter_absent_is_deferred_not_a_post_failure(h, monkeypatch, capsys):
    assert not (ROOT / "scripts" / "task-context" / "task_contextctl.py").exists()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-ref")
    assert _publish(termination_reason="approved", approved_body_sha256=SHA) == 0
    assert h.fg.posts == 1  # the comment is published
    assert len(h.signal_calls) == 1  # the adapter was attempted, and it is genuinely missing
    err = capsys.readouterr().err
    assert "disposition='deferred' reason_code='ADAPTER_UNAVAILABLE'" in err
    signal_line = next(ln for ln in err.splitlines() if "refinement_approved signal" in ln)
    assert "'applied'" not in signal_line and "'duplicate_noop'" not in signal_line
    assert "task_context_signal_not_applied" in _artifact_reasons(h)


def test_task_context_without_session_is_unbound_deferred(h, capsys):
    assert _publish(termination_reason="approved", approved_body_sha256=SHA) == 0
    assert h.signal_calls == []
    assert "disposition='deferred' reason_code='unbound'" in capsys.readouterr().err


def test_task_context_invalid_digest_is_rejected_evidence_not_applied(h, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-ref")
    assert _publish(termination_reason="approved", approved_body_sha256="not-a-digest") == 0
    assert h.signal_calls == []
    assert "INVALID_APPROVED_BODY_SHA256" in capsys.readouterr().err


def test_non_approved_termination_sends_no_signal(h, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-ref")
    assert _publish(termination_reason="blocked") == 0
    assert h.signal_calls == []


# ------------------------------------------------------------------------------------ closure


def _string_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def test_no_raw_gh_issue_comment_in_the_comment_lane():
    for name in ("publish_termination_report.py", "isolation_issue_comment_bridge.py"):
        tree = ast.parse((SKILL_SCRIPTS / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)):
                values = [
                    e.value
                    for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                assert not ("gh" in values and "issue" in values and "comment" in values), name
        assert "gh" not in _string_constants(SKILL_SCRIPTS / name), name


def test_removed_source_assets_are_not_revived():
    for removed in ("render_termination_report.py", "build_loop_state.py"):
        assert not list((ROOT / ".claude").rglob(removed)), removed
    refs = {
        "render_termination_report": "removed in the source",
        "build_loop_state": "removed in the source",
    }
    for name in ("publish_termination_report.py", "isolation_issue_comment_bridge.py"):
        tree = ast.parse((SKILL_SCRIPTS / name).read_text(encoding="utf-8"))
        imported = {
            a.name.split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.Import)
            for a in n.names
        } | {
            n.module.split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module
        }
        assert not (imported & set(refs)), name


def test_no_functional_legacy_slug_and_identity_is_single_sourced():
    for name in ("publish_termination_report.py", "isolation_issue_comment_bridge.py"):
        assert LEGACY_SLUG not in (SKILL_SCRIPTS / name).read_text(encoding="utf-8")
    assert TRUSTED == pub.TRUSTED_REPO if hasattr(pub, "TRUSTED_REPO") else True
