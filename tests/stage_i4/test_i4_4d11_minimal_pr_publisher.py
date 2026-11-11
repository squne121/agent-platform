"""I-4-4D11 (#108): the minimal interactive `open-pr` caller, exercised through its real CLI.

Every behavioural test runs `open_pr.py` as a subprocess inside a throwaway Git repository whose
`origin` is a local bare repository (rewritten from the trusted GitHub URL with `insteadOf`), with a
fake `gh` first on PATH that logs every invocation. No real GitHub call, token or PR is involved, so
nothing here verifies live PR authority: a live create stays UNVERIFIED until it is actually run.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
SKILL_DIR = ROOT / ".claude" / "skills" / "open-pr"
SCRIPT = SKILL_DIR / "scripts" / "open_pr.py"
EVALUATOR = SKILL_DIR / "scripts" / "validate_pr_body.py"
MANIFEST_PATH = DEV / "i4-4d11-minimal-pr-publisher-manifest.json"
THIS = "tests/stage_i4/test_i4_4d11_minimal_pr_publisher.py"
REPO = "squne121/agent-platform"
ISSUE = 108
BRANCH = "i108/feat"
BODY = (
    "## 概要\n\nRefs #60\n\n現行の日本語見出し構成を保つ本文です。\n\n## 検証\n\n- テスト実行済み\n"
)
A3_ISSUE = "## Runtime Verification Applicability\n\ndecision: not_applicable\nreason: x\n"
A2_ISSUE = (
    "## Runtime Verification Applicability\n\ndecision: deferred\ndeferred_destination: later\n"
    "destination_type: phase\ndestination_ref: post-merge-live-evidence\n"
    "deferred_verification_condition: live run\n"
)
NO_RVA_ISSUE = "## Outcome\n\nnothing about verification\n"
READS = {("issue", "view"), ("pr", "list"), ("pr", "view")}

FAKE_GH = r"""#!__PYTHON__
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ["FAKE_GH_LOG"])
with log.open("a") as f:
    f.write(json.dumps(args) + "\n")
state_path = Path(os.environ["FAKE_GH_STATE"])
st = json.loads(state_path.read_text())
key = " ".join(args[:2])
opt = lambda n: args[args.index(n) + 1] if n in args else None
mode = st.get("fail", {}).get(key)
if mode == "auth":
    sys.stderr.write("To get started with GitHub CLI, please run:  gh auth login\n"); sys.exit(4)
if mode == "error":
    sys.stderr.write("HTTP 502\n"); sys.exit(1)
if mode == "malformed":
    print("<html>not json</html>"); sys.exit(0)
if key == "issue view":
    print(json.dumps(st["issue"]))
elif key == "pr list":
    print(st["list_raw"] if st.get("list_raw") is not None else json.dumps(st["prs"]))
elif key == "pr view":
    pr = next((p for p in st["prs"] if str(p["number"]) == args[2]), None)
    if pr is None:
        sys.stderr.write("no such pr\n"); sys.exit(1)
    print(json.dumps({**pr, **st.get("view_override", {})}))
elif key == "pr create":
    create = st.get("create", "ok")
    if create == "error":
        sys.stderr.write("GraphQL: permission denied\n"); sys.exit(1)
    number = len(st["prs"]) + 5
    pr = {"number": number, "url": "https://github.com/" + opt("--repo") + "/pull/" + str(number),
          "state": "OPEN", "isDraft": "--draft" in args, "headRefName": opt("--head"),
          "headRefOid": st["head_sha"], "baseRefName": opt("--base"), "isCrossRepository": False,
          "headRepositoryOwner": {"login": "squne121"}, "body": Path(opt("--body-file")).read_text()}
    if create != "nothing_created":
        st["prs"].append(pr)
        state_path.write_text(json.dumps(st))
    if create == "created_but_failed":
        sys.stderr.write("timeout after create\n"); sys.exit(1)
    if create != "no_url":
        print(pr["url"])
elif args[0] == "api":
    data = st.get("api", {}).get(args[1])
    if data is None:
        sys.stderr.write("404\n"); sys.exit(1)
    print(json.dumps(data))
else:
    sys.stderr.write("unexpected gh call\n"); sys.exit(99)
"""


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.work = tmp_path / "work"
        self.bare = tmp_path / "origin.git"
        self.bin = tmp_path / "bin"
        self.state_path = tmp_path / "gh-state.json"
        self.log_path = tmp_path / "gh-log.jsonl"
        self.body_path = tmp_path / "body.md"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH.replace("__PYTHON__", sys.executable), encoding="utf-8")
        gh.chmod(0o755)
        self.log_path.write_text("")
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.work.mkdir()
        _git(self.work, "init", "-q", "-b", "main")
        _git(self.work, "remote", "add", "origin", f"https://github.com/{REPO}.git")
        _git(self.work, "config", f"url.{self.bare}.insteadOf", f"https://github.com/{REPO}.git")
        (self.work / "README.md").write_text("base\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "base")
        _git(self.work, "push", "-q", "origin", "main")
        _git(self.work, "checkout", "-q", "-b", BRANCH)
        (self.work / "feature.txt").write_text("feature\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "feature")
        _git(self.work, "push", "-q", "origin", BRANCH)
        self.head = _git(self.work, "rev-parse", "HEAD")
        self.body_path.write_text(BODY, encoding="utf-8")
        self.set_state(
            issue={"number": ISSUE, "state": "OPEN", "body": A3_ISSUE}, prs=[], head_sha=self.head
        )

    def set_state(self, **updates: Any) -> None:
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        state.update(updates)
        self.state_path.write_text(json.dumps(state))

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GH_", "GITHUB_"))}
        env.update(
            PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            FAKE_GH_STATE=str(self.state_path),
            FAKE_GH_LOG=str(self.log_path),
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
        )
        return env

    def run(self, *extra: str, publish: bool = True, title: str = "feat: 最小 PR caller (#108)"):
        argv = [
            sys.executable,
            str(SCRIPT),
            "--pr-title",
            title,
            "--linked-issue",
            str(ISSUE),
            "--pr-body-file",
            str(self.body_path),
            *(["--publish", "yes"] if publish else []),
            *extra,
        ]
        proc = subprocess.run(
            argv,
            cwd=self.work,
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return proc, out

    def calls(self) -> list[list[str]]:
        return [json.loads(ln) for ln in self.log_path.read_text().splitlines() if ln]

    def writes(self) -> list[list[str]]:
        """Every gh call that is not one of the three read subcommands or an `api` GET of a comment."""
        found = []
        for args in self.calls():
            if tuple(args[:2]) in READS:
                continue
            if args[0] == "api" and len(args) == 2 and "/issues/comments/" in args[1]:
                continue
            found.append(args)
        return found

    def creates(self) -> list[list[str]]:
        return [a for a in self.calls() if a[:2] == ["pr", "create"]]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def mk_pr(rig: Rig, **over: Any) -> dict[str, Any]:
    pr = {
        "number": 5,
        "url": f"https://github.com/{REPO}/pull/5",
        "state": "OPEN",
        "isDraft": True,
        "headRefName": BRANCH,
        "headRefOid": rig.head,
        "baseRefName": "main",
        "isCrossRepository": False,
        "headRepositoryOwner": {"login": "squne121"},
        "body": BODY + f"\nCloses #{ISSUE}\n",
    }
    pr.update(over)
    return pr


def stopped(proc: subprocess.CompletedProcess[str], out: dict[str, Any], code: str) -> None:
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert out["status"] == "stopped" and out["code"] == code, out


# =============================================================================================
# AC1: wrong repo / branch / head, gh read failure and missing auth are write-zero stops
# =============================================================================================


def test_ac1_publish_without_explicit_approval_stops_before_any_gh_call(rig: Rig) -> None:
    proc, out = rig.run(publish=False)
    stopped(proc, out, "E_APPROVAL_MISSING")
    assert rig.calls() == []


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/other/agent-platform.git",
        "https://github.com/Squne121/agent-platform.git",  # an alias/case variant never widens trust
        "https://github.com/squne121/agent-platform-fork.git",
        "https://example.com/squne121/agent-platform.git",
    ],
)
def test_ac1_untrusted_origin_stops_with_zero_gh_calls(rig: Rig, url: str) -> None:
    _git(rig.work, "remote", "set-url", "origin", url)
    proc, out = rig.run()
    stopped(proc, out, "E_ORIGIN_MISMATCH")
    assert rig.calls() == []


def test_ac1_branch_and_head_drift_stop_with_zero_gh_calls(rig: Rig) -> None:
    (rig.work / "scratch.txt").write_text("dirty\n")
    proc, out = rig.run()
    stopped(proc, out, "E_WORKTREE_DIRTY")
    (rig.work / "scratch.txt").unlink()

    _git(rig.work, "commit", "-q", "--allow-empty", "-m", "local only")
    proc, out = rig.run()
    stopped(proc, out, "E_HEAD_NOT_PUSHED")

    proc, out = rig.run("--branch", "some/other")
    stopped(proc, out, "E_BRANCH_UNEXPECTED")

    _git(rig.work, "checkout", "-q", "main")
    proc, out = rig.run()
    stopped(proc, out, "E_BRANCH_UNEXPECTED")
    _git(rig.work, "checkout", "-q", "--detach")
    proc, out = rig.run()
    stopped(proc, out, "E_BRANCH_UNEXPECTED")
    assert rig.calls() == []


def test_ac1_unusable_identity_config_stops(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / ".claude/skills/open-pr/scripts").mkdir(parents=True)
    (tree / "scripts/agent-guards").mkdir(parents=True)
    shutil.copy(SCRIPT, tree / ".claude/skills/open-pr/scripts/open_pr.py")
    shutil.copy(ROOT / "scripts/agent-guards/repo_identity.py", tree / "scripts/agent-guards/")
    body = tmp_path / "b.md"
    body.write_text(BODY)
    proc = subprocess.run(
        [sys.executable, str(tree / ".claude/skills/open-pr/scripts/open_pr.py"), "--pr-title", "t",
         "--linked-issue", "1", "--pr-body-file", str(body), "--publish", "yes"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["code"] == "E_REPO_IDENTITY_UNAVAILABLE"


@pytest.mark.parametrize("failing", ["issue view", "pr list"])
@pytest.mark.parametrize("mode", ["auth", "error", "malformed"])
def test_ac1_gh_read_failures_never_become_no_pr(rig: Rig, failing: str, mode: str) -> None:
    rig.set_state(fail={failing: mode})
    proc, out = rig.run()
    assert proc.returncode == 1 and out["status"] == "stopped", (proc.stdout, proc.stderr)
    assert out["code"] in {"E_LINKED_ISSUE_STATE_UNKNOWN", "E_PR_LOOKUP_FAILED"}
    assert rig.writes() == [] and rig.creates() == []
    assert not any(c[:2] == ["pr", "create"] for c in rig.calls())


def test_ac1_issue_payload_for_another_number_or_state_is_rejected(rig: Rig) -> None:
    for issue in (
        {"number": 9, "state": "OPEN", "body": A3_ISSUE},
        {"number": ISSUE, "state": "MERGED", "body": A3_ISSUE},
        {"number": ISSUE, "state": "OPEN", "body": None},
    ):
        rig.set_state(issue=issue)
        proc, out = rig.run()
        stopped(proc, out, "E_LINKED_ISSUE_STATE_UNKNOWN")
    assert rig.creates() == []


def test_ac1_gh_always_gets_the_trusted_repo_explicitly(rig: Rig) -> None:
    proc, _ = rig.run()
    assert proc.returncode == 0, proc.stderr
    for args in rig.calls():
        if args[0] in {"issue", "pr"}:
            assert args[args.index("--repo") + 1] == REPO, args


# =============================================================================================
# AC2: every D7 evaluator branch is consumed; the final body is re-evaluated
# =============================================================================================


def _created_body(rig: Rig) -> str:
    return rig.state()["prs"][-1]["body"]


def _evaluate(body: str, issue_body: str, state: str = "OPEN", pr_number: int | None = 5) -> dict:
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    import tempfile

    with tempfile.TemporaryDirectory(dir=tmp) as d:
        d = Path(d)
        (d / "b.md").write_text(body)
        (d / "i.md").write_text(issue_body)
        facts = {
            "repo": REPO,
            "issue_state": state,
            "pr_number": pr_number,
            "decision_comment": None,
        }
        (d / "f.json").write_text(json.dumps(facts))
        proc = subprocess.run(
            [sys.executable, str(EVALUATOR), "--evaluate-reference-policy", "--body-file",
             str(d / "b.md"), "--linked-issue", str(ISSUE), "--linked-issue-body-file",
             str(d / "i.md"), "--reference-facts-file", str(d / "f.json")],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
    return json.loads(proc.stdout)


def test_ac2_closing_required_appends_closes_and_keeps_the_body_and_parent_refs(rig: Rig) -> None:
    proc, out = rig.run()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    body = _created_body(rig)
    assert body.startswith(BODY) and body.endswith(f"\nCloses #{ISSUE}\n")
    assert "Refs #60" in body and "Closes #60" not in body  # the parent is never closed
    assert out["reference"]["decision"] == "closing_required"
    assert (
        out["reference"]["effective_kind"] == "closing"
        and out["reference"]["body_verdict"] == "valid"
    )
    # the created body is itself valid under an independent evaluator run
    again = _evaluate(body, A3_ISSUE)
    assert again["body_verdict"] == "valid" and again["pr_body_sha256"] == out["final_body_sha256"]


def test_ac2_a_valid_caller_reference_is_preserved_byte_for_byte(rig: Rig) -> None:
    own = BODY + f"\nCloses #{ISSUE}\n"
    rig.body_path.write_text(own)
    proc, _out = rig.run()
    assert proc.returncode == 0 and _created_body(rig) == own


def test_ac2_refs_only_body_in_a_closing_lane_is_repaired_to_closes(rig: Rig) -> None:
    rig.body_path.write_text(BODY + f"\nRefs #{ISSUE}\n")
    proc, out = rig.run()
    assert proc.returncode == 0, proc.stdout
    assert out["reference"]["effective_kind"] == "closing"
    assert _created_body(rig).endswith(f"\nCloses #{ISSUE}\n")


@pytest.mark.parametrize(
    ("issue", "state"),
    [(A2_ISSUE, "OPEN"), (A3_ISSUE, "CLOSED")],
    ids=["a2_deferred", "closed_issue"],
)
def test_ac2_nonclosing_required_appends_refs_and_never_closes(
    rig: Rig, issue: str, state: str
) -> None:
    rig.set_state(issue={"number": ISSUE, "state": state, "body": issue})
    proc, out = rig.run()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    body = _created_body(rig)
    assert body.endswith(f"\nRefs #{ISSUE}\n") and f"Closes #{ISSUE}" not in body
    assert out["reference"]["decision"] == "nonclosing_required"
    assert out["reference"]["effective_kind"] == "non-closing"


def test_ac2_an_intended_refs_is_never_auto_promoted_to_closes(rig: Rig) -> None:
    rig.set_state(issue={"number": ISSUE, "state": "OPEN", "body": A2_ISSUE})
    own = BODY + f"\nRefs #{ISSUE}\n"
    rig.body_path.write_text(own)
    proc, _ = rig.run()
    assert proc.returncode == 0 and _created_body(rig) == own


@pytest.mark.parametrize(
    ("issue", "extra", "reason"),
    [
        (NO_RVA_ISSUE, "", "runtime_applicability_unresolved"),  # fail_closed
        (A3_ISSUE, "\nCloses #999\n", "closing_for_other"),  # block
        (A2_ISSUE, f"\nCloses #{ISSUE}\n", "closing_forbidden"),  # block
    ],
    ids=["fail_closed", "block_other_issue", "block_closing_in_nonclosing_lane"],
)
def test_ac2_fail_closed_and_block_never_reach_create(
    rig: Rig, issue: str, extra: str, reason: str
) -> None:
    rig.set_state(issue={"number": ISSUE, "state": "OPEN", "body": issue})
    rig.body_path.write_text(BODY + extra)
    proc, out = rig.run()
    stopped(proc, out, "E_REFERENCE_POLICY_BLOCKED")
    assert reason in out["detail"]
    assert rig.creates() == [] and rig.writes() == []


def test_ac2_evaluator_exit_zero_alone_is_not_permission(rig: Rig) -> None:
    # the evaluator exits 0 for fail_closed too; the caller must read the JSON, not the exit code
    result = _evaluate(BODY, NO_RVA_ISSUE)
    assert result["decision"] == "fail_closed"
    rig.set_state(issue={"number": ISSUE, "state": "OPEN", "body": NO_RVA_ISSUE})
    proc, _out = rig.run()
    assert proc.returncode == 1 and rig.creates() == []


def _a1_state(rig: Rig, association: str) -> str:
    url = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-555"
    rig.set_state(
        api={
            f"repos/{REPO}/issues/comments/555": {
                "html_url": url,
                "id": 555,
                "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE}",
                "author_association": association,
                "body": f"人間の決定\nREFERENCE_DECISION_V1: nonclosing issue=#{ISSUE}\n",
            }
        }
    )
    return f"Reference-Decision: {url}\n"


def test_ac2_a1_explicit_decision_comment_is_fetched_and_judged_by_the_evaluator(rig: Rig) -> None:
    line = _a1_state(rig, "OWNER")
    rig.body_path.write_text(BODY + f"\n{line}")
    proc, out = rig.run()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert out["reference"]["level"] == "A1" and out["reference"]["effective_kind"] == "non-closing"
    assert _created_body(rig).endswith(f"Refs #{ISSUE}\n")


def test_ac2_an_untrusted_or_missing_a1_comment_fails_closed(rig: Rig) -> None:
    line = _a1_state(rig, "NONE")
    rig.body_path.write_text(BODY + f"\n{line}")
    proc, out = rig.run()
    stopped(proc, out, "E_REFERENCE_POLICY_BLOCKED")
    rig.set_state(api={})
    proc, out = rig.run()
    stopped(proc, out, "E_REFERENCE_POLICY_BLOCKED")
    assert rig.creates() == []


def test_ac2_no_legacy_template_or_japanese_ratio_is_enforced(rig: Rig) -> None:
    # an English, template-less body still passes: only the reference policy gates the caller
    rig.body_path.write_text("Plain English summary without any legacy template headings.\n")
    proc, _out = rig.run()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    source = SCRIPT.read_text(encoding="utf-8")
    for legacy in ("validate_japanese_content", "pull_request_template", "LP05", "ratio"):
        assert legacy not in source, legacy


def test_ac2_dry_run_reads_but_writes_nothing_and_needs_no_publish_flag(rig: Rig) -> None:
    proc, out = rig.run("--dry-run", publish=False)
    assert proc.returncode == 0 and out["status"] == "dry_run" and out["writes"] == 0, proc.stderr
    assert rig.creates() == [] and rig.writes() == []
    assert (
        out["final_body_sha256"]
        == hashlib.sha256((BODY + f"\nCloses #{ISSUE}\n").encode()).hexdigest()
    )


# =============================================================================================
# AC3: reuse of the same repo/head/base OPEN PR; ambiguous lookups never create or edit
# =============================================================================================


def test_ac3_second_run_reuses_the_pr_and_creates_nothing(rig: Rig) -> None:
    proc, first = rig.run()
    assert proc.returncode == 0 and first["status"] == "created"
    proc, second = rig.run()
    assert proc.returncode == 0 and second["status"] == "reused", (proc.stdout, proc.stderr)
    assert second["url"] == first["url"] and second["writes"] == 0
    assert second["body_matches_requested"] is True
    assert len(rig.creates()) == 1 and len(rig.state()["prs"]) == 1
    assert len(rig.writes()) == 1  # the single create is the only write ever made


def test_ac3_an_existing_pr_is_read_back_independently(rig: Rig) -> None:
    rig.set_state(prs=[mk_pr(rig)])
    proc, out = rig.run()
    assert proc.returncode == 0 and out["status"] == "reused" and out["pr_number"] == 5
    assert ["pr", "view", "5"] == rig.calls()[-1][:3] or any(
        c[:3] == ["pr", "view", "5"] for c in rig.calls()
    )
    assert rig.creates() == [] and rig.writes() == []


def test_ac3_a_reused_pr_with_a_different_but_valid_body_is_reported_not_overwritten(
    rig: Rig,
) -> None:
    rig.set_state(prs=[mk_pr(rig, body="## 概要\n\n別の説明\n\nCloses #108\n")])
    proc, out = rig.run()
    assert proc.returncode == 0 and out["body_matches_requested"] is False
    assert rig.writes() == []


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("list_failure", "E_PR_LOOKUP_FAILED"),
        ("malformed", "E_PR_LOOKUP_FAILED"),
        ("not_a_list", "E_PR_LOOKUP_AMBIGUOUS"),
        ("truncated", "E_PR_LOOKUP_AMBIGUOUS"),
        ("two_prs", "E_PR_LOOKUP_AMBIGUOUS"),
        ("foreign_owner", "E_PR_FOREIGN_HEAD"),
        ("cross_repository", "E_PR_FOREIGN_HEAD"),
        ("other_base", "E_PR_BASE_MISMATCH"),
        ("stale_head_sha", "E_PR_READBACK_MISMATCH"),
        ("closed_state_on_readback", "E_PR_READBACK_MISMATCH"),
        ("cross_issue", "E_REFERENCE_POLICY_BLOCKED"),
        ("no_reference", "E_REFERENCE_POLICY_BLOCKED"),
    ],
)
def test_ac3_ambiguous_foreign_or_conflicting_prs_stop_with_no_write(
    rig: Rig, scenario: str, code: str
) -> None:
    pr = mk_pr(rig)
    if scenario == "list_failure":
        rig.set_state(fail={"pr list": "error"})
    elif scenario == "malformed":
        rig.set_state(fail={"pr list": "malformed"})
    elif scenario == "not_a_list":
        rig.set_state(list_raw=json.dumps({"number": 5}))
    elif scenario == "truncated":
        rig.set_state(
            list_raw=json.dumps([mk_pr(rig, number=n, headRefName=f"x{n}") for n in range(100)])
        )
    elif scenario == "two_prs":
        rig.set_state(prs=[pr, mk_pr(rig, number=6)])
    elif scenario == "foreign_owner":
        rig.set_state(prs=[mk_pr(rig, headRepositoryOwner={"login": "stranger"})])
    elif scenario == "cross_repository":
        rig.set_state(prs=[mk_pr(rig, isCrossRepository=True)])
    elif scenario == "other_base":
        rig.set_state(prs=[mk_pr(rig, baseRefName="release")])
    elif scenario == "stale_head_sha":
        rig.set_state(prs=[mk_pr(rig, headRefOid="0" * 40)])
    elif scenario == "closed_state_on_readback":
        rig.set_state(prs=[pr], view_override={"state": "CLOSED"})
    elif scenario == "cross_issue":
        rig.set_state(prs=[mk_pr(rig, body="## 概要\n\nCloses #999\n")])
    elif scenario == "no_reference":
        rig.set_state(prs=[mk_pr(rig, body="## 概要\n\n参照なし\n")])
    proc, out = rig.run()
    stopped(proc, out, code)
    assert rig.creates() == [] and rig.writes() == []


def test_ac3_a_pr_for_a_different_head_does_not_block_a_new_create(rig: Rig) -> None:
    rig.set_state(prs=[mk_pr(rig, number=3, headRefName="someone/else")])
    proc, out = rig.run()
    assert proc.returncode == 0 and out["status"] == "created"
    assert len(rig.creates()) == 1


# =============================================================================================
# AC4: create with --draft/--body-file, after-create readback, and unknown-outcome negatives
# =============================================================================================


def test_ac4_create_uses_draft_body_file_and_explicit_repo_head_base_then_reads_back(
    rig: Rig,
) -> None:
    proc, out = rig.run()
    assert proc.returncode == 0 and out["status"] == "created", (proc.stdout, proc.stderr)
    (create,) = rig.creates()
    assert create[create.index("--repo") + 1] == REPO
    assert create[create.index("--head") + 1] == BRANCH
    assert create[create.index("--base") + 1] == "main"
    assert "--draft" in create and "--body-file" in create and "--body" not in create
    assert out["url"] == f"https://github.com/{REPO}/pull/5" and out["writes"] == 1
    calls = rig.calls()
    index = calls.index(create)
    assert any(c[:3] == ["pr", "view", "5"] for c in calls[index + 1 :])  # fresh GET after create
    assert out["reference"]["pr_number"] == 5


def test_ac4_create_failure_is_not_retried_and_nothing_is_claimed(rig: Rig) -> None:
    rig.set_state(create="error")
    proc, out = rig.run()
    stopped(proc, out, "E_CREATE_FAILED")
    assert out["probe"] == "absent" and len(rig.creates()) == 1


def test_ac4_an_unparseable_create_result_is_an_unknown_outcome(rig: Rig) -> None:
    rig.set_state(create="no_url")
    proc, out = rig.run()
    stopped(proc, out, "E_CREATE_OUTCOME_UNKNOWN")
    assert out["probe"] == "found" and len(rig.creates()) == 1  # reported, never re-created


def test_ac4_a_create_that_failed_after_creating_is_found_but_not_trusted(rig: Rig) -> None:
    rig.set_state(create="created_but_failed")
    proc, out = rig.run()
    stopped(proc, out, "E_CREATE_FAILED")
    assert out["probe"] == "found" and out["probe_url"].endswith("/pull/5")
    assert len(rig.creates()) == 1


def test_ac4_auth_failure_at_create_writes_nothing_and_does_not_retry(rig: Rig) -> None:
    rig.set_state(fail={"pr create": "auth"})
    proc, out = rig.run()
    stopped(proc, out, "E_CREATE_FAILED")
    assert "auth" in out["detail"].lower() and len(rig.creates()) == 1 and rig.state()["prs"] == []


@pytest.mark.parametrize(
    "override",
    [{"isDraft": False}, {"body": "tampered\n"}, {"headRefOid": "1" * 40}, {"baseRefName": "dev"}],
    ids=["not_draft", "body_changed", "head_sha", "base"],
)
def test_ac4_after_create_readback_mismatch_is_reported_as_a_stop(rig: Rig, override: dict) -> None:
    rig.set_state(view_override=override)
    proc, out = rig.run()
    assert proc.returncode == 1 and out["status"] == "stopped", (proc.stdout, proc.stderr)
    assert out["code"] in {"E_PR_READBACK_MISMATCH", "E_REFERENCE_POLICY_BLOCKED"}
    assert len(rig.creates()) == 1


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("open_pr_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ in the Skill dir
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = keep
    return module


def test_ac4_timeouts_on_create_and_on_reads_are_stops_without_a_second_create(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    real = module._run
    attempts: list[list[str]] = []

    def hanging(argv: list[str], timeout: int = 60) -> Any:
        if argv[:3] == ["gh", "pr", "create"]:
            attempts.append(argv)
            raise subprocess.TimeoutExpired(argv, timeout)
        return real(argv, timeout)

    monkeypatch.setattr(module, "_run", hanging)
    monkeypatch.setenv("PATH", rig.env()["PATH"])
    monkeypatch.setenv("FAKE_GH_STATE", str(rig.state_path))
    monkeypatch.setenv("FAKE_GH_LOG", str(rig.log_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.chdir(rig.work)
    args = module.argparse.Namespace(
        pr_title="t", linked_issue=ISSUE, pr_body_file=str(rig.body_path), publish="yes",
        dry_run=False, branch=None,
    )  # fmt: skip
    with pytest.raises(module.Stop) as caught:
        module.run(args)
    assert (
        caught.value.code == "E_CREATE_OUTCOME_UNKNOWN" and caught.value.extra["probe"] == "absent"
    )
    assert len(attempts) == 1 and rig.creates() == []

    def hanging_read(argv: list[str], timeout: int = 60) -> Any:
        if argv[:3] == ["gh", "issue", "view"]:
            raise subprocess.TimeoutExpired(argv, timeout)
        return real(argv, timeout)

    monkeypatch.setattr(module, "_run", hanging_read)
    with pytest.raises(module.Stop) as caught:
        module.run(args)
    assert caught.value.code == "E_LINKED_ISSUE_STATE_UNKNOWN"


def test_ac4_live_pr_creation_is_unverified_by_this_suite(rig: Rig) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["evidence_boundary"]["live_pr_created_by_tests"] is False
    assert manifest["evidence_boundary"]["live_pr_authority_verified"] is False
    assert not any(re.search(r"github\.com/.+/pull/\d+", a) for c in rig.calls() for a in c)


def test_ac4_guard_rejects_empty_or_escaped_bodies(rig: Rig) -> None:
    module = _load_module()
    for bad in ("", "x", 'text \\" quote', "cost \\$5"):
        with pytest.raises(module.Stop):
            module.guard_body_file(bad)
    module.guard_body_file(BODY)


# =============================================================================================
# AC5: no new mutation actor, no controlled pr_* route, no hook; the disabled skills stay disabled
# =============================================================================================

FORBIDDEN_GH = ("edit", "merge", "comment", "update-branch", "ready", "close", "review", "reopen")


def test_ac5_the_caller_contains_no_other_github_mutation_or_credential_handling() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for sub in FORBIDDEN_GH:
        assert not re.search(rf'"pr",\s*"{sub}"', source), sub
    for needle in (
        "--force",
        '"push"',
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "auth login",
        "--admin",
        "--auto",
    ):
        assert needle not in source, needle
    assert source.count('"create"') == 1  # the single `gh pr create` call site
    assert not re.search(r"[\"']pr_(create|edit|comment|update_branch)[\"']", source)


def test_ac5_controlled_pr_routes_stay_unavailable_and_permission_unverified() -> None:
    stage27 = json.loads((DEV / "stage2-7-terminal-wiring-manifest.json").read_text("utf-8"))
    routes = stage27["mutation_routes"]
    assert {"pr_create", "pr_edit", "pr_comment", "pr_update_branch"} <= set(routes["unavailable"])
    assert routes["permission"] == "unverified"
    assert not any(k.startswith("pr_") for k in routes["available"])


def test_ac5_skill_is_manual_only_and_the_reviewer_skills_stay_disabled() -> None:
    def frontmatter(path: Path) -> str:
        return path.read_text(encoding="utf-8").split("---")[1]

    assert "disable-model-invocation: true" in frontmatter(SKILL_DIR / "SKILL.md")
    for rel in ("pr-review-judge", "impl-review-loop"):
        fm = frontmatter(ROOT / ".claude" / "skills" / rel / "SKILL.md")
        assert "disable-model-invocation: true" in fm, rel
    assert not (ROOT / ".claude/hooks").exists()
    assert not (ROOT / ".claude/settings.json").exists()
    assert not (ROOT / "scripts/task-context").exists()
    pr_reviewer = (ROOT / ".claude/agents/pr-reviewer.md").read_text(encoding="utf-8")
    assert "hooks:" not in pr_reviewer


def test_ac5_the_unmigrated_update_pr_and_template_stay_absent() -> None:
    for rel in (
        ".claude/skills/open-pr/scripts/update_pr.py",
        ".github/pull_request_template.md",
        ".claude/skills/implement-issue",
    ):
        assert not (ROOT / rel).exists(), rel
    files = sorted(
        p.relative_to(SKILL_DIR).as_posix()
        for p in SKILL_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    assert files == ["SKILL.md", "scripts/open_pr.py", "scripts/validate_pr_body.py"], files


def test_ac5_the_d7_evaluator_is_reused_unchanged() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    pin = manifest["reused_unchanged"][".claude/skills/open-pr/scripts/validate_pr_body.py"]
    blob = subprocess.run(
        ["git", "hash-object", str(EVALUATOR)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert blob == pin == "9ab611caea3b006132d53782d08670ee978bf923"
    assert "validate_pr_body.py" in SCRIPT.read_text(encoding="utf-8")
    assert "--evaluate-reference-policy" in SCRIPT.read_text(encoding="utf-8")


# =============================================================================================
# AC6: historical closure is current, source pins survive, counts are measured, no skips
# =============================================================================================

SKIP_RE = re.compile(
    r"pytest\.(mark\.)?(skip|skipif|xfail)|@pytest\.mark\.(skip|xfail)|import" + "orskip"
)
BOUNDARY_FALSE = (
    "live_pr_created_by_tests",
    "live_pr_authority_verified",
    "controlled_pr_route_registered",
    "bot_or_broker_permission_verified",
    "reviewer_dispatched",
    "pr_review_judge_enabled",
    "impl_review_loop_enabled",
    "final_gate_wired",
    "live_e2e",
    "task_context_restored",
    "legacy_hook_restored",
    "merge_or_auto_merge",
)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return _json(MANIFEST_PATH)


def boundary_problems(m: dict[str, Any]) -> list[str]:
    return [f"overclaims {k}" for k in BOUNDARY_FALSE if m["evidence_boundary"].get(k) is not False]


def history_problems(root: Path = ROOT) -> list[str]:
    """Each earlier closure keeps its original record and now says D11 migrated the caller."""
    problems: list[str] = []
    d11 = "docs/dev/i4-4d11-minimal-pr-publisher-manifest.json"
    migrated = [".claude/skills/open-pr/SKILL.md", ".claude/skills/open-pr/scripts/open_pr.py"]
    for name in (
        "i4-4d3-step4-step5",
        "i4-4d4-skill-agents-readonly",
        "i4-4d5-pr-review-consumers",
        "i4-4d6-pr-review-core",
        "i4-4d7-open-pr-reference-evaluator",
        "i4-4d8-hookless-reviewer",
    ):
        doc = _json(root / "docs" / "dev" / f"{name}-manifest.json")
        sync = doc.get("migrated_by_d11", {})
        if sync.get("files") != migrated or sync.get("manifest") != d11:
            problems.append(
                f"{name}: migrated_by_d11 does not name the caller and the D11 manifest"
            )
        if not doc.get("historical_pre_d11"):
            problems.append(f"{name}: the pre-D11 record (historical_pre_d11) was lost")
        if sync.get("still_unmigrated") != [".claude/skills/open-pr/scripts/update_pr.py"]:
            problems.append(f"{name}: update_pr.py must stay recorded as unmigrated")
    return problems


def test_ac6_manifest_pins_scope_sources_and_boundaries(manifest: dict[str, Any]) -> None:
    assert manifest["issue"] == "squne121/agent-platform#108"
    assert manifest["tracker"] == "squne121/agent-platform#60"
    assert manifest["owner_decision"]["option"] == "A"
    assert manifest["source"]["tag"] == "pre-split-agent-platform-20261020"
    assert manifest["source"]["commit"] == "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
    assert boundary_problems(manifest) == []
    assert manifest["deferred_tests"] == []
    totals = manifest["totals"]
    assert (totals["skipped"], totals["xfailed"], totals["xpassed"], totals["deselected"]) == (
        0,
        0,
        0,
        0,
    )
    for rel in manifest["required_files"] + manifest["test_files"]:
        assert (ROOT / rel).is_file(), rel


def test_ac6_overclaims_in_the_boundary_are_detected(manifest: dict[str, Any]) -> None:
    for key in BOUNDARY_FALSE:
        bad = json.loads(json.dumps(manifest))
        bad["evidence_boundary"][key] = True
        assert boundary_problems(bad) == [f"overclaims {key}"]
    missing = json.loads(json.dumps(manifest))
    del missing["evidence_boundary"]["live_e2e"]
    assert boundary_problems(missing)


def test_ac6_d3_to_d8_records_are_synchronized_without_losing_history() -> None:
    assert history_problems() == []
    d7 = _json(DEV / "i4-4d7-open-pr-reference-evaluator-manifest.json")
    assert d7["source"]["commit"] if "source" in d7 else True
    text = json.dumps(d7)
    assert "9ab611caea3b006132d53782d08670ee978bf923" in text  # the transferred source pin survives


def test_ac6_a_lost_history_or_update_pr_claim_is_detected(tmp_path: Path) -> None:
    shutil.copytree(DEV, tmp_path / "docs" / "dev")
    assert history_problems(tmp_path) == []
    path = tmp_path / "docs/dev/i4-4d5-pr-review-consumers-manifest.json"
    doc = _json(path)
    del doc["historical_pre_d11"]
    path.write_text(json.dumps(doc))
    assert any("historical_pre_d11" in p for p in history_problems(tmp_path))
    path = tmp_path / "docs/dev/i4-4d6-pr-review-core-manifest.json"
    doc = _json(path)
    doc["migrated_by_d11"]["still_unmigrated"] = []
    path.write_text(json.dumps(doc))
    assert any("update_pr.py" in p for p in history_problems(tmp_path))


def _collect(*files: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", *files],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )  # fmt: skip


def test_ac6_the_closure_test_count_is_measured_not_assumed(manifest: dict[str, Any]) -> None:
    ids = [ln for ln in _collect(THIS).stdout.splitlines() if "::" in ln]
    assert manifest["expected_collect"][THIS] == len(ids)
    assert manifest["expected_ran"][THIS] == len(ids)
    assert manifest["totals"]["collected"] == sum(manifest["expected_collect"].values())
    assert manifest["totals"]["ran"] == sum(manifest["expected_ran"].values())


def test_ac6_this_file_adds_no_skip_xfail_or_deselect() -> None:
    assert SKIP_RE.search(Path(__file__).read_text(encoding="utf-8")) is None


def test_ac6_ci_has_a_d11_lane_bound_to_the_manifest(manifest: dict[str, Any]) -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "i4-4d11-minimal-pr-publisher:" in ci
    assert "docs/dev/i4-4d11-minimal-pr-publisher-manifest.json" in ci
    totals = manifest["totals"]
    assert f"0 deferred with reasons, {totals['ran']} ran, 0 skipped" in ci
    assert not re.search(
        r"^\s*permissions:",
        ci.split("i4-4d11-minimal-pr-publisher:")[1].split("\n  i4-")[0],
        re.MULTILINE,
    )
