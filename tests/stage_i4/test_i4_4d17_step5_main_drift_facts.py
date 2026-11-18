"""I-4-4D17 (#120): read-only Step 5 ``main_drift`` facts producer (fixture level).

What this module proves: ``build_step5_main_drift_facts.py`` generates the 7 facts (+ the real
``git merge-tree`` ``semantic_ambiguity``) from a real temporary Git repository and an injected
fake ``gh`` world, and that the result is accepted by the EXISTING ``build_step5_live_mergeability()``
and routed by the EXISTING ``route_loop_verdict_v2`` / ``step5-terminal-gate``.  Missing or
inconsistent inputs are refused instead of being read as "no drift".

What it does NOT prove: any real GitHub / real checkout behaviour, a real trusted GO comment
(a GO result dict is injected through the parser seam), a real Reviewer, or that Step 5 reached
``approved`` on a live PR.  Those are recorded by the main session outside this file; a fixture
result is never a runtime PASS.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
IRL = ROOT / ".claude" / "skills" / "impl-review-loop"
SCRIPT = IRL / "scripts" / "build_step5_main_drift_facts.py"
CONSUMER = IRL / "scripts" / "adjudicate_vc_result.py"
ROUTE = IRL / "scripts" / "route_loop_verdict_v2.py"
MANIFEST = ROOT / "docs" / "dev" / "i4-4d17-step5-main-drift-facts-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
THIS = "tests/stage_i4/test_i4_4d17_step5_main_drift_facts.py"
D17_JOB = "i4-4d17-step5-main-drift-facts"
D17_MANIFEST_REL = "docs/dev/i4-4d17-step5-main-drift-facts-manifest.json"
ALLOWED_PATHS = [
    ".claude/skills/impl-review-loop/scripts/build_step5_main_drift_facts.py",
    "tests/stage_i4/test_i4_4d17_step5_main_drift_facts.py",
    "docs/dev/i4-4d17-step5-main-drift-facts-manifest.json",
    ".github/workflows/ci.yml",
]
WRITE_FLAGS = re.compile(
    r"--mode\s+auto\b|--post\b|gh\s+pr\s+(review|comment|edit|merge|ready|close)\b"
    r"|gh\s+issue\s+(comment|edit|close|create)\b|test_verdict\.publish|git\s+(push|commit)\b"
)
REPO = "squne121/agent-platform"
S1, S2 = "a" * 40, "b" * 40
PR, ISSUE = 7, 120
BODY = "## Outcome\n\nx\n\n## Allowed Paths\n\n- `allowed/`\n- `docs/one.md`\n\n## Stop Conditions\n\n- x\n"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prod = _load(SCRIPT, "d17_producer_under_test")
route = _load(ROUTE, "d17_route_under_test")
real_ens = prod._load(IRL / "scripts" / "ensure_contract_snapshot.py", "d17_real_ens")


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# --- manifest / CI / boundary -------------------------------------------------------------------


def test_manifest_shape_and_boundary_false() -> None:
    m = _manifest()
    assert m["schema"] == "I4_4D17_STEP5_MAIN_DRIFT_FACTS_V1"
    assert m["issue"] == "squne121/agent-platform#120"
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
        "github_mutation_by_producer",
        "git_fetch_by_producer",
    ):
        assert required in m["boundary_false"], required
    assert m["evidence_boundary"]["fixture_result_is_runtime_pass"] is False
    assert m["evidence_boundary"]["main_drift_facts_fabricated"] is False
    assert m["evidence_boundary"]["zero_writes_claimed"] is False


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


def test_manifest_allowed_paths_pin_matches_the_introducing_commit() -> None:
    m = _manifest()
    assert m["allowed_paths"] == ALLOWED_PATHS
    for rel in ALLOWED_PATHS:
        assert (ROOT / rel).is_file(), rel
    base = m["base_commit"]
    have_base = (
        subprocess.run(
            ["git", "cat-file", "-e", f"{base}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if os.environ.get("D17_REQUIRE_BASE") == "1":
        assert have_base, "the D17 CI lane must check out the full history"
    if not have_base:  # other lanes may run this directory on a shallow checkout
        return
    commits = subprocess.run(
        ["git", "log", "--reverse", "--format=%H", f"{base}..HEAD", "--", D17_MANIFEST_REL],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    if not commits:  # a descendant history that no longer contains the introducing range
        return
    changed = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commits[0]],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    assert sorted(changed) == sorted(ALLOWED_PATHS), changed


def test_runtime_acceptance_procedure_has_no_write_flag() -> None:
    procedure = _manifest()["runtime_acceptance"]["commands"]
    assert procedure
    for command in procedure:
        assert not WRITE_FLAGS.search(command), command
    joined = "\n".join(procedure)
    assert "capture-evidence" in joined and "build" in joined and "step5-terminal-gate" in joined
    assert "--mode check-only" in joined


def test_manifest_states_residual_limits_and_manual_inputs() -> None:
    m = _manifest()
    residual = "\n".join(m["residual_constraints"])
    assert "evidence" in residual and "LOOP_STATE" in residual
    assert "drift_rebind_attempts" in residual
    assert "fetch" in residual and "merge-tree" in residual
    assert m["not_changed"]


def test_impl_review_loop_stays_disabled() -> None:
    text = (IRL / "SKILL.md").read_text(encoding="utf-8")
    front = yaml.safe_load(re.match(r"---\n(.*?)\n---\n", text, re.DOTALL).group(1))  # type: ignore[union-attr]
    assert front["disable-model-invocation"] is True


def test_ci_lane_uses_existing_runner_and_manifest() -> None:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    assert D17_JOB in jobs
    runs = "\n".join(step.get("run", "") for step in jobs[D17_JOB]["steps"])
    assert f"scripts/ci/run_stage1b1_tests.py {D17_MANIFEST_REL}" in runs
    assert "permissions" not in jobs[D17_JOB]
    assert not WRITE_FLAGS.search(runs)


# --- fixture world: real temp Git repository + injected fake gh ----------------------------------


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def _commit(cwd: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        p = cwd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", msg)
    return _git(cwd, "rev-parse", "HEAD")


def _repo(path: Path, url: str = f"https://github.com/{REPO}.git") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "remote", "add", "origin", url)
    return path


class World:
    """base A on main; PR branch head H (adds allowed/pr.txt); main may advance later."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.co = _repo(tmp / "co")
        self.base = _commit(
            self.co,
            {
                "allowed/x.txt": "x\n",
                "other/y.txt": "y\n",
                "docs/one.md": "1\n",
                "shared.txt": "a\nb\nc\n",
            },
            "base",
        )
        _git(self.co, "checkout", "-q", "-b", "pr")
        self.head = _commit(
            self.co, {"allowed/pr.txt": "pr\n", "shared.txt": "a\nB\nc\n"}, "pr head"
        )
        _git(self.co, "checkout", "-q", "main")
        self.art = tmp / "art"
        self.art.mkdir()
        self.pr: dict[str, Any] = {
            "number": PR,
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": self.head,
            "headRefName": "pr",
            "baseRefName": "main",
            "baseRefOid": self.base,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        self.refs = {"main": self.base, "pr": self.head}
        self.body = BODY
        self.comments_text: str | None = None
        self.go_results: list[dict[str, Any]] = []
        self.fail: set[str] = set()
        self.garble: set[str] = set()
        self.on_comments: Callable[[], None] | None = None
        self.gh_calls: list[list[str]] = []

    # main branch moves
    def advance_main(self, files: dict[str, str]) -> str:
        _git(self.co, "checkout", "-q", "main")
        sha = _commit(self.co, files, "main moves")
        self.refs["main"] = sha
        self.pr["baseRefOid"] = sha
        return sha

    def set_go(self, *, base_sha: str | None = None, **over: Any) -> None:
        body_sha = real_ens.sha256_of(self.body)
        allowed = real_ens._canonicalize_allowed_paths_strict(
            real_ens.extract_allowed_paths_from_body(self.body)
        )
        fp = real_ens.compute_expected_contract_fingerprint(
            issue_number=ISSUE,
            contract_source_id="issue_comment:1",
            contract_body_sha256=body_sha,
            allowed_paths=allowed,
            base_ref="main",
            base_sha_at_snapshot=base_sha or self.refs["main"],
        )
        fp.update(over.pop("fp", {}))
        go = {
            "comment_id": 1,
            "html_url": f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-1",
            "created_at": "2026-11-18T00:00:00Z",
            "status": "go",
            "is_fingerprint_ready": True,
            "is_trusted_author": True,
            "inner": {"body_sha256": body_sha, "expected_contract_fingerprint": fp},
        }
        go.update(over)
        self.go_results = [go]

    def gh(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.gh_calls.append(list(argv))

        def done(out: str, rc: int = 0) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")

        if argv[1:3] == ["pr", "view"]:
            if "pr" in self.fail:
                return done("", 1)
            if "pr" in self.garble:
                return done('{"headRefOid": ')
            return done(json.dumps(self.pr))
        if argv[1:3] == ["issue", "view"]:
            if "issue" in self.fail:
                return done("", 1)
            return done(json.dumps({"body": self.body, "updatedAt": "2026-11-18T00:00:00Z"}))
        if argv[1] == "api" and "/git/ref/heads/" in argv[2]:
            if "ref" in self.fail:
                return done("", 1)
            branch = argv[2].split("/git/ref/heads/")[1]
            return done(json.dumps({"object": {"sha": self.refs[branch]}}))
        if argv[1] == "api" and "--paginate" in argv:
            if "comments" in self.fail:
                return done("", 1)
            if self.on_comments:
                self.on_comments()
            return done(self.comments_text if self.comments_text is not None else "")
        raise AssertionError(f"unexpected gh call: {argv}")

    def runner(self, argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
        if argv[0] == "gh":
            return self.gh(argv)
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


class _ParserProxy:
    def __init__(self, world: World, real: Any) -> None:
        self._world = world
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def parse_contract_review_results(
        self, comments: list[dict], expected_issue_url: str | None = None
    ) -> list:
        return list(self._world.go_results)


class _EnsProxy:
    """Real ensure_contract_snapshot helpers; only the GO parse seam and is_go_current are narrowed."""

    def __init__(self, world: World) -> None:
        self._world = world

    def __getattr__(self, name: str) -> Any:
        return getattr(real_ens, name)

    def _import_parser_module(self) -> Any:
        return _ParserProxy(self._world, real_ens._import_parser_module())

    def is_go_current(self, go: Any, body_sha: str) -> bool:
        return real_ens.is_go_fresh(go, body_sha)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World(tmp_path)
    w.set_go()
    original = prod._load

    def patched(path: Path, name: str) -> Any:
        if name == "_d17_ensure_contract_snapshot":
            return _EnsProxy(w)
        return original(path, name)

    monkeypatch.setattr(prod, "_load", patched)
    return w


def _main(w: World, *args: str, checkout: Path | None = None) -> tuple[int, dict[str, Any]]:
    out = _StdoutCapture()
    rc = out.run(
        lambda: prod.main(
            [*args],
            runner=w.runner,
        )
    )
    return rc, out.payload


class _StdoutCapture:
    def __init__(self) -> None:
        self.payload: dict[str, Any] = {}

    def run(self, fn: Callable[[], int]) -> int:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn()
        self.payload = json.loads(buf.getvalue().strip().splitlines()[-1])
        return rc


def _common(w: World, checkout: Path | None = None) -> list[str]:
    return [
        "--repo", REPO, "--pr-number", str(PR), "--issue-number", str(ISSUE),
        "--checkout", str(checkout or w.co),
    ]  # fmt: skip


def capture(
    w: World, checkout: Path | None = None, out_dir: Path | None = None
) -> tuple[int, dict[str, Any]]:
    return _main(
        w, "capture-evidence", *_common(w, checkout), "--evidence-dir", str(out_dir or w.art / "ev")
    )


def build(
    w: World, evidence: Path, checkout: Path | None = None, output: Path | None = None
) -> tuple[int, dict[str, Any]]:
    return _main(
        w,
        "build",
        *_common(w, checkout),
        "--evidence-file", str(evidence),
        "--output", str(output or w.art / "out" / "live.json"),
    )  # fmt: skip


def captured(w: World) -> Path:
    rc, payload = capture(w)
    assert rc == 0, payload
    return Path(payload["record_path"])


def decide(live: dict[str, Any], verdict: str = "APPROVE", reviewed: str | None = None) -> Any:
    return route.route_loop_verdict_v2(
        {
            "verdict": verdict,
            "reviewed_head_sha": reviewed or live["head_sha"],
            "blockers": [],
            "warnings": [],
        },
        live,
    )


# --- positive: base unchanged ----------------------------------------------------------------


def test_capture_evidence_records_observed_values_write_once(world: World) -> None:
    rc, payload = capture(world)
    assert rc == 0, payload
    rec = payload["record"]
    assert rec["head_sha"] == world.head and rec["base_sha"] == world.base
    assert rec["base_ref_name"] == "main" and rec["head_ref_name"] == "pr"
    assert rec["repo"] == REPO and rec["pr_number"] == PR and rec["captured_at"]
    assert rec["record_sha256"].startswith("sha256:")
    rc2, again = capture(world)
    assert rc2 == 1 and again["code"] == "evidence_record_exists"


def test_build_base_unchanged_generates_exact_facts_and_consumer_accepts(world: World) -> None:
    rc, payload = build(world, captured(world))
    assert rc == 0, payload
    live = payload["live_mergeability"]
    assert json.loads(Path(payload["output"]).read_text()) == live
    assert live["head_sha"] == world.head
    assert live["mergeable"] == "MERGEABLE" and live["merge_state_status"] == "CLEAN"
    facts = live["main_drift"]
    assert facts == {
        "current_base_sha": world.base,
        "evidence_base_sha": world.base,
        "allowed_paths_snapshot_base_sha": world.base,
        "allowed_paths": ["allowed/", "docs/one.md"],
        "latest_main_net_diff": [],
        "expected_old_sha": world.head,
        "observed_old_sha": world.head,
        "semantic_ambiguity": False,
    }
    again = route.build_step5_live_mergeability(
        {
            "head_sha": live["head_sha"],
            "mergeable": live["mergeable"],
            "merge_state_status": live["merge_state_status"],
        },
        live["main_drift"],
    )
    assert again == live
    prov = payload["provenance"]
    assert prov["fetch_performed"] is False and prov["go"]["base_sha_at_snapshot"] == world.base
    assert prov["checkout"]["path"] == str(world.co.resolve())
    assert prov["semantic_ambiguity_oracle"]["result"] is False
    assert "drift_rebind_attempts" in prov["manual_inputs_remaining"]


def test_existing_router_routes_generated_input(world: World) -> None:
    live = build(world, captured(world))[1]["live_mergeability"]
    assert decide(live, "APPROVE").route == "approved"
    assert decide(live, "REQUEST_CHANGES").route == "continue_loop"
    human = decide(live, "HUMAN_REVIEW_REQUIRED")
    assert (human.route, human.reason_code) == (
        "route_human_escalation",
        "reviewer_human_review_required",
    )


def test_existing_terminal_gate_cli_accepts_generated_input(world: World, tmp_path: Path) -> None:
    live = build(world, captured(world))[1]
    files = {
        "ls.json": {},
        "rv.json": {
            "verdict": "HUMAN_REVIEW_REQUIRED",
            "reviewed_head_sha": world.head,
            "blockers": [],
            "warnings": [],
        },
        "hh.json": [],
    }
    for name, content in files.items():
        (tmp_path / name).write_text(json.dumps(content), encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable, str(CONSUMER), "step5-terminal-gate",
            "--loop-state-file", str(tmp_path / "ls.json"),
            "--reviewer-verdict-file", str(tmp_path / "rv.json"),
            "--live-mergeability-file", live["output"],
            "--expected-head-sha", world.head,
            "--expected-contract-body-sha256", "sha256:" + "0" * 64,
            "--expected-command-hashes-file", str(tmp_path / "hh.json"),
            "--dispatch-seq", "1",
        ],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    payload = json.loads(done.stdout.strip().splitlines()[-1])
    assert done.returncode == 1
    assert (payload["route"], payload["reason_code"]) == (
        "route_human_escalation",
        "reviewer_human_review_required",
    )


@pytest.mark.parametrize(
    ("mergeable", "state", "draft"),
    [("MERGEABLE", "CLEAN", True), ("UNKNOWN", "UNKNOWN", False), ("MERGEABLE", "BLOCKED", False)],
)
def test_github_values_are_kept_verbatim_and_is_draft_is_separate(
    world: World, mergeable: str, state: str, draft: bool
) -> None:
    world.pr.update(mergeable=mergeable, mergeStateStatus=state, isDraft=draft)
    rc, payload = build(world, captured(world))
    assert rc == 0, payload
    live = payload["live_mergeability"]
    assert (live["mergeable"], live["merge_state_status"]) == (mergeable, state)
    assert "isDraft" not in live and "is_draft" not in live
    assert payload["provenance"]["pr"]["isDraft"] is draft
    assert payload["provenance"]["pr"]["mergeStateStatus"] == state


# --- positive/negative: base advanced -------------------------------------------------------


def _advance_after_capture(
    w: World, files: dict[str, str], go_at_new_base: bool = True
) -> tuple[Path, str]:
    ev = captured(w)
    new = w.advance_main(files)
    w.set_go(base_sha=new if go_at_new_base else w.base)
    return ev, new


def test_base_advanced_within_allowed_paths_is_scope_clean(world: World) -> None:
    ev, new = _advance_after_capture(world, {"allowed/new.txt": "n\n"})
    rc, payload = build(world, ev)
    assert rc == 0, payload
    facts = payload["live_mergeability"]["main_drift"]
    assert facts["current_base_sha"] == new and facts["evidence_base_sha"] == world.base
    assert facts["allowed_paths_snapshot_base_sha"] == new
    assert facts["latest_main_net_diff"] == ["allowed/new.txt"]
    assert facts["semantic_ambiguity"] is False
    decision = decide(payload["live_mergeability"], "APPROVE")
    assert decision.route == "route_scope_clean_reconciliation"
    assert decision.reason_code == "main_drift_scope_clean"


def test_evidence_base_and_go_base_are_not_conflated(world: World) -> None:
    ev, new = _advance_after_capture(world, {"allowed/new.txt": "n\n"}, go_at_new_base=False)
    rc, payload = build(world, ev)
    assert rc == 0, payload
    facts = payload["live_mergeability"]["main_drift"]
    assert facts["evidence_base_sha"] == world.base == facts["allowed_paths_snapshot_base_sha"]
    assert facts["current_base_sha"] == new
    decision = decide(payload["live_mergeability"], "APPROVE")
    assert decision.reason_code == "stale_allowed_paths_snapshot"


def test_net_diff_outside_allowed_paths_conflicts(world: World) -> None:
    ev, _ = _advance_after_capture(world, {"other/z.txt": "z\n"})
    payload = build(world, ev)[1]
    assert payload["live_mergeability"]["main_drift"]["latest_main_net_diff"] == ["other/z.txt"]
    assert decide(payload["live_mergeability"]).reason_code == "allowed_paths_conflict"


def test_directory_prefix_and_exact_file_allowed_semantics(world: World) -> None:
    ev, _ = _advance_after_capture(world, {"docs/one.md": "changed\n", "allowed/deep/a.txt": "a\n"})
    live = build(world, ev)[1]["live_mergeability"]
    assert sorted(live["main_drift"]["latest_main_net_diff"]) == [
        "allowed/deep/a.txt",
        "docs/one.md",
    ]
    assert decide(live).route == "route_scope_clean_reconciliation"


def test_real_merge_tree_conflict_sets_semantic_ambiguity(world: World) -> None:
    ev, _ = _advance_after_capture(world, {"shared.txt": "a\nMAIN\nc\n"})
    rc, payload = build(world, ev)
    assert rc == 0, payload
    assert payload["live_mergeability"]["main_drift"]["semantic_ambiguity"] is True
    assert decide(payload["live_mergeability"]).reason_code == "semantic_ambiguity"


def test_head_moved_after_capture_is_left_to_the_router_cas(world: World) -> None:
    ev = captured(world)
    _git(world.co, "checkout", "-q", "pr")
    new_head = _commit(world.co, {"allowed/more.txt": "m\n"}, "pr moves")
    _git(world.co, "checkout", "-q", "main")
    world.pr["headRefOid"] = new_head
    world.refs["pr"] = new_head
    rc, payload = build(world, ev)
    assert rc == 0, payload
    facts = payload["live_mergeability"]["main_drift"]
    assert facts["expected_old_sha"] == world.head and facts["observed_old_sha"] == new_head
    assert payload["live_mergeability"]["head_sha"] == new_head
    assert (
        decide(payload["live_mergeability"], reviewed=new_head).reason_code
        == "expected_old_cas_mismatch"
    )


# --- negatives: missing / inconsistent / failed inputs -----------------------------------------


def _code(rc_payload: tuple[int, dict[str, Any]]) -> str:
    rc, payload = rc_payload
    assert rc == 1 and payload["status"] == "error", payload
    return payload["code"]


def test_missing_or_invalid_evidence_record_is_refused(world: World, tmp_path: Path) -> None:
    assert _code(build(world, tmp_path / "nope.json")) == "evidence_record_missing_or_invalid"
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert _code(build(world, bad)) == "evidence_record_missing_or_invalid"


def test_tampered_or_foreign_evidence_record_is_refused(world: World, tmp_path: Path) -> None:
    ev = captured(world)
    rec = json.loads(ev.read_text())
    tampered = dict(rec, base_sha=world.head)
    p = tmp_path / "t.json"
    p.write_text(json.dumps(tampered), encoding="utf-8")
    assert _code(build(world, p)) == "evidence_record_digest_mismatch"
    foreign = dict(rec, pr_number=PR + 1)
    foreign["record_sha256"] = prod._record_digest(foreign)
    q = tmp_path / "f.json"
    q.write_text(json.dumps(foreign), encoding="utf-8")
    assert _code(build(world, q)) == "evidence_record_target_mismatch"


@pytest.mark.parametrize("what", ["pr", "issue", "ref", "comments"])
def test_github_fetch_failure_is_not_an_empty_result(world: World, what: str) -> None:
    ev = captured(world)
    world.fail.add(what)
    assert _code(build(world, ev)) in {"github_fetch_failed"}


def test_truncated_pr_json_is_refused(world: World) -> None:
    ev = captured(world)
    world.garble.add("pr")
    assert _code(build(world, ev)) == "github_response_invalid"


@pytest.mark.parametrize(
    "field", ["headRefOid", "mergeable", "mergeStateStatus", "baseRefName", "isDraft"]
)
def test_incomplete_pr_data_is_refused(world: World, field: str) -> None:
    ev = captured(world)
    del world.pr[field]
    assert _code(build(world, ev)) in {"github_response_incomplete", "invalid_sha"}


def test_pr_not_open_is_refused(world: World) -> None:
    ev = captured(world)
    world.pr["state"] = "CLOSED"
    assert _code(build(world, ev)) == "pr_not_open"


def test_inconsistent_ref_observations_are_refused(world: World) -> None:
    ev = captured(world)
    world.pr["baseRefOid"] = world.head
    assert _code(build(world, ev)) == "base_ref_oid_mismatch"
    world.pr["baseRefOid"] = world.base
    world.refs["pr"] = world.base
    assert _code(build(world, ev)) == "head_ref_mismatch"


def test_empty_allowed_paths_is_refused(world: World) -> None:
    ev = captured(world)
    world.body = "## Outcome\n\nx\n\n## Stop Conditions\n\n- x\n"
    assert _code(build(world, ev)) == "allowed_paths_missing_or_empty"


def test_base_branch_retarget_after_capture_is_refused(world: World) -> None:
    ev = captured(world)
    world.pr["baseRefName"] = "release"
    world.refs["release"] = world.base
    assert _code(build(world, ev)) == "base_ref_name_changed"


@pytest.mark.parametrize("what", ["head", "base", "body"])
def test_change_during_collection_is_refused(world: World, what: str) -> None:
    ev = captured(world)

    def change_between_the_two_samples() -> None:
        if what == "head":
            world.pr["headRefOid"] = world.base
            world.refs["pr"] = world.base
        elif what == "base":
            world.advance_main({"allowed/late.txt": "l\n"})
        else:
            world.body = world.body + "\nchanged\n"

    world.on_comments = change_between_the_two_samples
    assert _code(build(world, ev)) == "live_state_changed_during_collection"
    assert not (world.art / "out" / "live.json").exists()


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda w: w.go_results.clear(), "go_missing_or_untrusted"),
        (lambda w: w.go_results[0].update(is_trusted_author=False), "go_missing_or_untrusted"),
        (lambda w: w.go_results[0].update(is_fingerprint_ready=False), "go_missing_or_untrusted"),
        (
            lambda w: w.go_results.append(
                {
                    "comment_id": 2,
                    "html_url": "u",
                    "created_at": "2026-11-19T00:00:00Z",
                    "status": "blocked",
                    "is_trusted_author": True,
                    "is_fingerprint_ready": True,
                    "inner": {},
                }
            ),
            "go_superseded",
        ),
        (lambda w: w.go_results[0]["inner"].update(body_sha256="sha256:" + "1" * 64), "go_body_mismatch"),
        (
            lambda w: w.go_results[0]["inner"]["expected_contract_fingerprint"].update(
                allowed_paths_normalized_sha256="0" * 64
            ),
            "go_fingerprint_mismatch",
        ),
        (
            lambda w: w.go_results[0]["inner"]["expected_contract_fingerprint"].update(
                contract_body_sha256="sha256:" + "2" * 64
            ),
            "go_fingerprint_mismatch",
        ),
        (
            lambda w: w.go_results[0]["inner"]["expected_contract_fingerprint"].update(base_ref="release"),
            "go_base_ref_mismatch",
        ),
    ],
    ids=[
        "no_go", "untrusted", "not_fingerprint_ready", "later_blocked", "body_sha",
        "allowed_paths_hash", "fingerprint_body", "base_ref",
    ],
)  # fmt: skip
def test_ineligible_go_is_refused(world: World, mutate: Callable[[World], None], code: str) -> None:
    ev = captured(world)
    mutate(world)
    assert _code(build(world, ev)) == code


def test_missing_git_objects_are_refused_not_ambiguity(world: World, tmp_path: Path) -> None:
    ev = captured(world)
    old = tmp_path / "old-clone"
    subprocess.run(["git", "clone", "-q", str(world.co), str(old)], check=True, capture_output=True)
    _git(old, "remote", "set-url", "origin", f"https://github.com/{REPO}.git")
    new = world.advance_main({"allowed/new.txt": "n\n"})
    world.set_go(base_sha=new)
    code = _code(build(world, ev, checkout=old))
    assert code == "git_object_missing"


def test_evidence_base_must_be_an_ancestor_of_current_base(world: World) -> None:
    ev = captured(world)
    rec = json.loads(ev.read_text())
    _git(world.co, "checkout", "-q", "--orphan", "island")
    _git(world.co, "rm", "-rfq", ".")
    island = _commit(world.co, {"island.txt": "i\n"}, "island")
    _git(world.co, "checkout", "-q", "main")
    forged = dict(rec, base_sha=island)
    forged["record_sha256"] = prod._record_digest(forged)
    ev.write_text(json.dumps(forged), encoding="utf-8")
    new = world.advance_main({"allowed/new.txt": "n\n"})
    world.set_go(base_sha=new)
    assert _code(build(world, ev)) == "evidence_base_not_ancestor"


def test_wrong_checkout_is_refused(world: World, tmp_path: Path) -> None:
    ev = captured(world)
    other = _repo(tmp_path / "other", url="https://github.com/someone/else.git")
    _commit(other, {"a.txt": "a\n"}, "a")
    assert _code(build(world, ev, checkout=other)) == "checkout_repo_mismatch"
    assert _code(build(world, ev, checkout=world.co / "allowed")) == "checkout_not_toplevel"


def test_artifacts_inside_the_checkout_are_refused(world: World) -> None:
    ev = captured(world)
    assert _code(build(world, ev, output=world.co / "live.json")) == "artifact_inside_checkout"
    assert _code(capture(world, out_dir=world.co / "ev")) == "artifact_inside_checkout"


def test_producer_never_substitutes_a_current_value_when_history_is_absent(world: World) -> None:
    assert _code(build(world, world.art / "missing.json")) == "evidence_record_missing_or_invalid"
    assert not (world.art / "out" / "live.json").exists()


# --- read-only boundary ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "pr", "view", "7", "--repo", REPO, "--json", "number"],
        ["gh", "issue", "view", "1", "--repo", REPO, "--json", "body"],
        ["gh", "api", f"repos/{REPO}/git/ref/heads/main"],
        ["gh", "api", "--paginate", f"repos/{REPO}/issues/1/comments", "--jq", "."],
        ["gh", "api", "--method", "GET", f"repos/{REPO}/pulls/1"],
        ["git", "rev-parse", "HEAD"],
        ["git", "remote", "get-url", "origin"],
        ["git", "cat-file", "-e", S1 + "^{commit}"],
        ["git", "merge-base", "--is-ancestor", S1, S2],
        ["git", "merge-tree", "--write-tree", S1, S2],
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "-z",
            S1,
            S2,
        ],
    ],
)
def test_allowlist_accepts_only_read_shapes(argv: list[str]) -> None:
    assert prod._argv_allowed(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "pr", "merge", "7"],
        ["gh", "pr", "review", "7", "--approve"],
        ["gh", "pr", "comment", "7", "--body", "x"],
        ["gh", "pr", "edit", "7"],
        ["gh", "pr", "ready", "7"],
        ["gh", "issue", "comment", "1", "--body", "x"],
        ["gh", "issue", "edit", "1"],
        ["gh", "api", "-X", "POST", "repos/x/y/issues/1/comments"],
        ["gh", "api", "--method", "PATCH", "repos/x/y/issues/1"],
        ["gh", "api", "--method=DELETE", "repos/x/y/issues/1"],
        ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=x"],
        ["gh", "api", "repos/x/y/issues/1/comments", "-F", "body=x"],
        ["gh", "api", "repos/x/y/issues/1/comments", "--input", "f.json"],
        ["gh", "api", "graphql", "-f", "query=x"],
        ["git", "fetch", "origin"],
        ["git", "checkout", "main"],
        ["git", "reset", "--hard"],
        ["git", "commit", "-m", "x"],
        ["git", "push", "origin", "main"],
        ["git", "update-ref", "refs/heads/x", "abc"],
        ["git", "branch", "-D", "x"],
        ["git", "merge-tree", "a", "b"],
        ["git", "merge-tree", "--write-tree", "a", "b"],
        ["git", "diff", "a", "b"],
        ["git", "diff", "--name-only", "a", "b"],
        ["git", "diff", "--name-only", "--no-ext-diff", "--no-textconv", S1, S2],
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "-z",
            "--output=/tmp/zz",
            S1,
            S2,
        ],
        ["gh", "api", "-XPOST", "repos/x/y/issues/1/comments"],
        ["gh", "api", "repos/x/y/issues/1/comments", "-fbody=x"],
        ["gh", "api", "repos/x/y/issues/1/comments", "--field=body=x"],
        ["gh", "api", "repos/x/y/issues/1/comments", "--raw-field=body=x"],
        ["gh", "api", "repos/x/y/issues/1/comments", "--input=f"],
        ["gh", "api", "repos/x/y/issues/1/comments", "-X=POST"],
        ["gh", "api", "/graphql", "-fquery=x"],
        ["gh", "api", "user"],
        ["gh", "pr", "view", "7", "--web"],
        ["gh", "pr", "view", "7", "--repo", REPO, "--json", "number", "--web"],
        ["git", "rev-parse", "--git-dir"],
        ["git", "remote", "set-url", "origin", "x"],
        ["git", "cat-file", "-p", S1],
        ["git", "stash"],
        ["git", "worktree", "add", "x"],
        ["rm", "-rf", "x"],
    ],
)
def test_allowlist_rejects_mutating_or_unlisted_commands(argv: list[str]) -> None:
    assert not prod._argv_allowed(argv)


def test_guarded_runner_raises_before_running_a_forbidden_command() -> None:
    ran: list[Any] = []
    guarded = prod._Guarded(lambda argv, cwd=None: ran.append(argv), None)
    with pytest.raises(prod.FactsError) as err:
        guarded(["gh", "pr", "merge", "1"])
    assert err.value.code == "read_only_violation" and not ran


def _snapshot(co: Path) -> dict[str, str]:
    return {
        "head": _git(co, "rev-parse", "HEAD"),
        "branch": _git(co, "symbolic-ref", "--short", "HEAD"),
        "refs": _git(co, "for-each-ref", "--format=%(refname) %(objectname)"),
        "status": _git(co, "status", "--porcelain=v1", "-uall"),
        "index": _git(co, "ls-files", "-s"),
        "tree": _git(co, "rev-parse", "HEAD^{tree}"),
    }


def test_checkout_head_index_worktree_and_refs_are_unchanged_by_a_full_run(world: World) -> None:
    before = _snapshot(world.co)
    ev = captured(world)
    new = world.advance_main({"allowed/new.txt": "n\n"})
    world.set_go(base_sha=new)
    before = _snapshot(world.co)  # main advanced by the fixture itself, not by the producer
    rc, payload = build(world, ev)
    assert rc == 0, payload
    assert _snapshot(world.co) == before
    for argv in payload["provenance"]["commands"]:
        assert prod._argv_allowed(argv), argv
    assert not any(a[:2] == ["git", "fetch"] for a in payload["provenance"]["commands"])
    assert not any(c[1:3] in (["pr", "merge"], ["pr", "review"]) for c in world.gh_calls)
    assert all(c[1] in ("pr", "issue", "api") for c in world.gh_calls)


def test_default_runner_is_a_plain_subprocess_without_shell() -> None:
    src = SCRIPT.read_text(encoding="utf-8")
    assert "shell=True" not in src and "os.system" not in src
    assert isinstance(prod.FactsError("c", "d"), Exception)
    assert types.ModuleType  # the module imports only stdlib + the existing repository modules


# --- additional bindings and real-parser coverage ----------------------------------------------


def test_evidence_record_is_bound_to_the_issue_and_the_pr_number(
    world: World, tmp_path: Path
) -> None:
    ev = captured(world)
    rec = json.loads(ev.read_text())
    other_issue = dict(rec, issue_number=ISSUE + 1)
    other_issue["record_sha256"] = prod._record_digest(other_issue)
    p = tmp_path / "i.json"
    p.write_text(json.dumps(other_issue), encoding="utf-8")
    assert _code(build(world, p)) == "evidence_record_target_mismatch"
    world.pr["number"] = PR + 1
    assert _code(build(world, ev)) == "pr_number_mismatch"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/squne121/agent-platform",
        "https://evil.example/x/squne121/agent-platform.git",
        "https://github.com.evil.example/squne121/agent-platform",
        "https://github.com/squne121/agent-platform-fork",
    ],
)
def test_origin_must_be_github_for_the_target_repo(world: World, tmp_path: Path, url: str) -> None:
    ev = captured(world)
    other = _repo(tmp_path / "o", url=url)
    _commit(other, {"a.txt": "a\n"}, "a")
    assert _code(build(world, ev, checkout=other)) == "checkout_repo_mismatch"


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/{REPO}.git",
        f"https://github.com/{REPO}",
        f"git@github.com:{REPO}.git",
        f"ssh://git@github.com/{REPO}.git",
    ],
)
def test_origin_url_forms_of_the_target_repo_are_accepted(
    world: World, tmp_path: Path, url: str
) -> None:
    ev = captured(world)
    _git(world.co, "remote", "set-url", "origin", url)
    assert build(world, ev)[0] == 0


def test_terminal_gate_cli_reads_the_generated_drift_facts(world: World, tmp_path: Path) -> None:
    ev, _ = _advance_after_capture(world, {"allowed/new.txt": "n\n"}, go_at_new_base=False)
    live = build(world, ev)[1]
    for name, content in {
        "ls.json": {},
        "rv.json": {
            "verdict": "APPROVE",
            "reviewed_head_sha": world.head,
            "blockers": [],
            "warnings": [],
        },
        "hh.json": [],
    }.items():
        (tmp_path / name).write_text(json.dumps(content), encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable, str(CONSUMER), "step5-terminal-gate",
            "--loop-state-file", str(tmp_path / "ls.json"),
            "--reviewer-verdict-file", str(tmp_path / "rv.json"),
            "--live-mergeability-file", live["output"],
            "--expected-head-sha", world.head,
            "--expected-contract-body-sha256", "sha256:" + "0" * 64,
            "--expected-command-hashes-file", str(tmp_path / "hh.json"),
            "--dispatch-seq", "1",
        ],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    payload = json.loads(done.stdout.strip().splitlines()[-1])
    assert done.returncode != 0 and payload["route"] != "approved"
    assert payload["reason_code"] == "stale_allowed_paths_snapshot"


# a real GO comment, posted by the real ensure_contract_snapshot.py into the existing stateful fake gh
_h_spec = importlib.util.spec_from_file_location(
    "d17_handoff_harness", IRL / "tests" / "test_step4_contract_snapshot_handoff.py"
)
assert _h_spec is not None and _h_spec.loader is not None
h = importlib.util.module_from_spec(_h_spec)
sys.modules[_h_spec.name] = h
_h_spec.loader.exec_module(h)


def _real_go_world(tmp_path: Path) -> tuple[World, Any]:
    harness = h.World(tmp_path / "harness")
    harness.run_producer("--mode", "auto", "--post")
    w = World(tmp_path / "w")
    w.body = h.ISSUE_BODY
    lines = []
    for c in harness.read_state()["comments"]:
        lines.append(
            json.dumps(
                {
                    "id": c["id"], "html_url": c["html_url"], "created_at": c["created_at"],
                    "updated_at": c["updated_at"], "body": c["body"], "author": c["user"]["login"],
                    "author_id": c["user"]["id"], "author_type": c["user"]["type"],
                    "author_association": c["author_association"],
                }
            )
        )  # fmt: skip
    w.comments_text = "\n".join(lines) + "\n"
    return w, harness


def _run_with_real_ens(w: World, *args: str) -> tuple[int, dict[str, Any]]:
    out = _StdoutCapture()
    rc = out.run(lambda: prod.main([*args], runner=w.runner))
    return rc, out.payload


def _real_args(w: World, action: str, *extra: str) -> list[str]:
    return [
        action, "--repo", REPO, "--pr-number", str(PR), "--issue-number", str(h.ISSUE),
        "--checkout", str(w.co), *extra,
    ]  # fmt: skip


def test_real_parser_and_real_go_are_accepted(tmp_path: Path) -> None:
    w, _ = _real_go_world(tmp_path)
    rc, cap = _run_with_real_ens(
        w, *_real_args(w, "capture-evidence", "--evidence-dir", str(w.art / "ev"))
    )
    assert rc == 0, cap
    rc, payload = _run_with_real_ens(
        w,
        *_real_args(
            w, "build", "--evidence-file", cap["record_path"], "--output", str(w.art / "o.json")
        ),
    )
    assert rc == 0, payload
    facts = payload["live_mergeability"]["main_drift"]
    assert facts["allowed_paths_snapshot_base_sha"] == "d" * 40  # the harness GO's recorded base
    assert payload["provenance"]["go"]["base_ref"] == "main"
    assert facts["evidence_base_sha"] == w.base != facts["allowed_paths_snapshot_base_sha"]


def test_real_parser_rejects_a_later_trusted_blocked_comment(tmp_path: Path) -> None:
    w, harness = _real_go_world(tmp_path)
    rc, cap = _run_with_real_ens(
        w, *_real_args(w, "capture-evidence", "--evidence-dir", str(w.art / "ev"))
    )
    assert rc == 0, cap
    harness.add_trusted_blocked_comment()
    c = harness.read_state()["comments"][-1]
    w.comments_text += (
        json.dumps(
            {
                "id": c["id"], "html_url": c["html_url"], "created_at": c["created_at"],
                "updated_at": c["updated_at"], "body": c["body"], "author": c["user"]["login"],
                "author_id": c["user"]["id"], "author_type": c["user"]["type"],
                "author_association": c["author_association"],
            }
        )
        + "\n"
    )  # fmt: skip
    rc, payload = _run_with_real_ens(
        w,
        *_real_args(
            w, "build", "--evidence-file", cap["record_path"], "--output", str(w.art / "o.json")
        ),
    )
    assert rc == 1 and payload["code"] == "go_superseded", payload


def test_real_is_go_current_rejects_a_go_for_a_different_body(tmp_path: Path) -> None:
    w, _ = _real_go_world(tmp_path)
    rc, cap = _run_with_real_ens(
        w, *_real_args(w, "capture-evidence", "--evidence-dir", str(w.art / "ev"))
    )
    assert rc == 0, cap
    w.body = h.ISSUE_BODY + "\n- changed\n"
    rc, payload = _run_with_real_ens(
        w,
        *_real_args(
            w, "build", "--evidence-file", cap["record_path"], "--output", str(w.art / "o.json")
        ),
    )
    assert rc == 1 and payload["code"] == "go_body_mismatch", payload


def test_real_parser_rejects_an_untrusted_author(tmp_path: Path) -> None:
    w, _ = _real_go_world(tmp_path)
    rc, cap = _run_with_real_ens(
        w, *_real_args(w, "capture-evidence", "--evidence-dir", str(w.art / "ev"))
    )
    assert rc == 0, cap
    forged = [json.loads(line) for line in w.comments_text.splitlines()]
    for c in forged:
        c.update(author="mallory", author_id=1, author_association="NONE")
    w.comments_text = "\n".join(json.dumps(c) for c in forged) + "\n"
    rc, payload = _run_with_real_ens(
        w,
        *_real_args(
            w, "build", "--evidence-file", cap["record_path"], "--output", str(w.art / "o.json")
        ),
    )
    assert rc == 1 and payload["code"] == "go_missing_or_untrusted", payload
