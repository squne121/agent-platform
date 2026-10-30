"""Stage 2-6 (#36): child materialization lane (Step 4.5) - closure and false-green guards.

Evidence boundary: every GitHub interaction here is a fixture - the stateful fake `gh`
(`fixtures/fake_gh_create.py`) run as a real executable via `--gh`, or in-process recording
runners. No live GitHub read, auth or mutation is made, and none is claimed.

Test names carry the Issue AC tag (`ac1` .. `ac4`) so each AC's verification command is `-k acN`.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".claude" / "skills" / "create-issue" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = ROOT / "docs" / "dev" / "stage2-6-materialization-manifest.json"
MATERIALIZER = SCRIPTS / "materialize_child_issues.py"
TRUSTED = "squne121/agent-platform"
LEGACY_SLUG = "/".join(["squne121", "loop-protocol"])  # noqa: FLY002 - no literal for the slug scan

sys.path.insert(0, str(SCRIPTS))
import materialize_child_issues as m

_spec = importlib.util.spec_from_file_location(
    "_stage2_6_cic_conftest", ROOT / "tests" / "create-issue" / "conftest.py"
)
assert _spec is not None and _spec.loader is not None
_cic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cic)


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _install_fake_gh(tmp: Path, monkeypatch: pytest.MonkeyPatch, repo: str = TRUSTED):
    gh = tmp / "gh"
    src = (FIXTURES / "fake_gh_create.py").read_text(encoding="utf-8").split("\n", 1)[1]
    gh.write_text(f"#!{sys.executable}\n" + src, encoding="utf-8")
    gh.chmod(0o755)
    state = tmp / "gh_state.json"
    state.write_text(json.dumps({"repo": repo, "issues": [], "next_number": 700}), encoding="utf-8")
    log = tmp / "gh_calls.log"
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    return gh, state, log


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]


def _plan(**over) -> dict:
    plan = _cic._valid_plan()
    plan["repo"] = over.pop("repo", TRUSTED)
    plan["children"][0]["depends_on"] = over.pop("depends_on", [])
    plan.update(over)
    return plan


def _run_cli(tmp: Path, gh: Path, plan: dict) -> tuple[int, dict | None, str]:
    plan_file = tmp / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    cp = subprocess.run(
        [sys.executable, str(MATERIALIZER), "--plan-file", str(plan_file), "--gh", str(gh)],
        capture_output=True,
        text=True,
        check=False,
    )
    out = cp.stdout.strip().splitlines()
    return cp.returncode, (json.loads(out[-1]) if out else None), cp.stderr


# --------------------------------------------------------------------------- AC1 no raw gh create


def _raw_gh_create_sites(tree: ast.AST) -> list[int]:
    """Lines that build a `gh issue create` argv: a list/tuple holding "issue" then "create"."""
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            consts = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            for a, b in itertools.pairwise(consts):
                if (a, b) == ("issue", "create"):
                    sites.append(node.lineno)
    return sites


def test_ac1_materializer_source_has_no_raw_gh_issue_create() -> None:
    tree = ast.parse(MATERIALIZER.read_text(encoding="utf-8"))
    assert _raw_gh_create_sites(tree) == []


def test_ac1_negative_control_detector_flags_a_raw_create_mutant() -> None:
    mutant = 'import subprocess\nsubprocess.run(["gh", "issue", "create", "--title", "t"])\n'
    assert _raw_gh_create_sites(ast.parse(mutant)) == [2]
    mutant2 = 'args = ["issue", "create"]\n'
    assert _raw_gh_create_sites(ast.parse(mutant2)) == [1]


def test_ac1_creation_subprocess_targets_only_create_issue_txn() -> None:
    src = MATERIALIZER.read_text(encoding="utf-8")
    assert '_SCRIPT_DIR / "create_issue_txn.py"' in src
    assert "--issue-kind" in src and "--label-profile" in src
    # the only other gh verbs it shells are the parent view/edit pair (never create)
    verbs = set(re.findall(r'runners\.gh\(\["issue", "(\w+)"', src))
    assert verbs == {"view", "edit"}


def test_ac1_other_migrated_scripts_never_write_through_raw_gh() -> None:
    for name in ("plan_child_materialization.py", "check_issue_overlap.py"):
        tree = ast.parse((SCRIPTS / name).read_text(encoding="utf-8"))
        assert _raw_gh_create_sites(tree) == [], name
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        assert '"edit"' not in text and "'edit'" not in text, name
    assert "subprocess" not in (SCRIPTS / "mutation_route_matrix.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- AC3 false-green guards

BAD_REPOS = [
    LEGACY_SLUG,
    "squne121/other-repo",
    "other-owner/agent-platform",
    "SQUNE121/agent-platform",
    "squne121/agent-platform ",
    "o/r",
    "https://github.com/squne121/agent-platform",
]


@pytest.mark.parametrize("repo", BAD_REPOS)
def test_ac3_wrong_repo_cli_is_write_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: str
) -> None:
    gh, _state, log = _install_fake_gh(tmp_path, monkeypatch)
    rc, result, err = _run_cli(tmp_path, gh, _plan(repo=repo))
    assert rc == 2 and result is None and "repo_untrusted" in err
    assert _calls(log) == []


@pytest.mark.parametrize("repo", BAD_REPOS)
def test_ac3_wrong_repo_parent_patch_is_write_zero(repo: str) -> None:
    seen: list[list[str]] = []

    def gh(args):
        seen.append(args)
        return m.RunResult(0, "")

    res = m.apply_parent_checklist_patch(
        repo=repo,
        parent_issue=254,
        updates=[{"old_line": "- [ ] a", "new_line": "- [x] a"}],
        expected_body_sha256="sha256:" + "a" * 64,
        runners=m.Runners(gh=gh),
    )
    assert res.updated is False and res.error == "repo_untrusted"
    assert seen == []


def test_ac3_in_process_wrong_repo_never_reaches_any_runner() -> None:
    called: list[str] = []
    runners = m.Runners(
        validate=lambda *_a: called.append("validate") or m.RunResult(0, ""),
        create=lambda **_k: called.append("create") or m.RunResult(0, ""),
        gh=lambda _a: called.append("gh") or m.RunResult(0, ""),
    )
    with pytest.raises(m.PlanValidationError, match="repo_untrusted"):
        m.materialize(_plan(repo=LEGACY_SLUG), runners)
    assert called == []


def test_ac3_unapproved_relationships_are_rejected_by_the_txn_before_any_gh_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Implementation child + parent link + depends_on: the txn gate refuses every one."""
    gh, state, log = _install_fake_gh(tmp_path, monkeypatch)
    rc, result, _err = _run_cli(tmp_path, gh, _plan(depends_on=[948]))
    assert rc == 1 and result is not None
    assert result["status"] == "failed" and result["created_issues"] == []
    assert result["updated_parent"] is False and "not approved" in result["errors"][0]["error"]
    verbs = [tuple(c[:2]) for c in _calls(log)]
    assert ("issue", "create") not in verbs and ("issue", "edit") not in verbs
    assert not any(c[:2] == ["api", "graphql"] and "mutation" in " ".join(c) for c in _calls(log))
    assert json.loads(state.read_text(encoding="utf-8")).get("unexpected", []) == []


def _recording_runners(txn_stdout: str, txn_rc: int):
    gh_calls: list[list[str]] = []

    def gh(args):
        gh_calls.append(args)
        return m.RunResult(0, "")

    runners = m.Runners(
        validate=lambda *_a: m.RunResult(0, "{}"),
        create=lambda **_k: m.RunResult(txn_rc, txn_stdout),
        gh=gh,
    )
    return runners, gh_calls


def _plan_with_parent_update() -> dict:
    plan = _plan()
    plan["parent"]["body_sha256"] = "sha256:" + "b" * 64
    plan["parent_body_updates"] = [{"old_line": "- [ ] a", "new_line": "- [x] a"}]
    return plan


@pytest.mark.parametrize(
    ("stdout", "rc"),
    [
        ('{"status":"partial_failure","issue_number":330,"issue_url":"u"}', 1),
        ('{"status":"dedupe","issue_number":331,"issue_url":"u"}', 1),
        ('{"status":"failure"}', 1),
        ("not json", 1),
        ("", 0),
        ('{"status":"success"}', 0),
    ],
)
def test_ac3_partial_duplicate_and_unparseable_txn_results_are_never_ok(
    stdout: str, rc: int
) -> None:
    runners, gh_calls = _recording_runners(stdout, rc)
    res = m.materialize(_plan_with_parent_update(), runners)
    assert res["status"] != "ok" and res["errors"]
    assert res["updated_parent"] is False
    assert gh_calls == [], "the parent must not be viewed or edited after a non-clean create"


def test_ac3_positive_control_clean_create_patches_the_parent_with_readback() -> None:
    old, new = "- [ ] a", "- [x] a"
    body = f"## Child Issues\n\n{old}\n\n## Next\n"
    state = {"body": body}
    seen: list[list[str]] = []

    def gh(args):
        seen.append(args)
        if args[:2] == ["issue", "view"]:
            return m.RunResult(0, state["body"] + "\n")
        if args[:2] == ["issue", "edit"]:
            state["body"] = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
            return m.RunResult(0, "")
        raise AssertionError(args)

    runners = m.Runners(
        validate=lambda *_a: m.RunResult(0, "{}"),
        create=lambda **_k: m.RunResult(
            0, '{"status":"success","issue_number":330,"issue_url":"u"}'
        ),
        gh=gh,
    )
    plan = _plan()
    plan["parent"]["body_sha256"] = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    plan["parent_body_updates"] = [{"old_line": old, "new_line": new}]
    res = m.materialize(plan, runners)
    assert res["status"] == "ok" and res["updated_parent"] is True and res["errors"] == []
    assert [tuple(a[:2]) for a in seen] == [("issue", "view"), ("issue", "edit"), ("issue", "view")]


def test_ac3_stale_parent_body_is_not_edited() -> None:
    body = "## Child Issues\n\n- [ ] a\n"
    seen: list[list[str]] = []

    def gh(args):
        seen.append(args)
        return m.RunResult(0, body + "\n")

    res = m.apply_parent_checklist_patch(
        repo=TRUSTED,
        parent_issue=254,
        updates=[{"old_line": "- [ ] a", "new_line": "- [x] a"}],
        expected_body_sha256="sha256:" + "0" * 64,
        runners=m.Runners(gh=gh),
    )
    assert res.updated is False and "mismatch" in (res.error or "")
    assert [a[:2] for a in seen] == [["issue", "view"]]


def test_ac3_readback_mismatch_is_not_success() -> None:
    old, new = "- [ ] a", "- [x] a"
    body = f"## Child Issues\n\n{old}\n"
    seen: list[list[str]] = []

    def gh(args):
        seen.append(args)
        return m.RunResult(0, body + "\n")  # the edit "succeeds" but the body never changes

    res = m.apply_parent_checklist_patch(
        repo=TRUSTED,
        parent_issue=254,
        updates=[{"old_line": old, "new_line": new}],
        expected_body_sha256="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        runners=m.Runners(gh=gh),
    )
    assert res.updated is False and "read-back" in (res.error or "")


# --------------------------------------------------------------------------- AC4 manifest


def test_ac4_manifest_schema_counts_and_zero_skip_contract() -> None:
    mf = _manifest()
    assert mf["schema"] == "STAGE2_6_MATERIALIZATION_MANIFEST_V1"
    assert mf["issue"] == "squne121/agent-platform#36"
    assert mf["source"]["commit"] == "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
    assert set(mf["test_files"]) == set(mf["expected_collect"]) == set(mf["expected_ran"])
    for e in mf["deferred_tests"]:
        assert all(e.get(k) for k in ("id", "owner_stage", "reason", "rerun_condition"))
    assert sum(mf["expected_collect"].values()) - len(mf["deferred_tests"]) == sum(
        mf["expected_ran"].values()
    )
    assert "live_github_evidence" in mf["fixture_live_evidence_boundary"]
    assert "none" in mf["fixture_live_evidence_boundary"]["live_github_evidence"]


def test_ac4_manifest_hashes_match_the_migrated_files() -> None:
    mf = _manifest()
    for rel, entry in mf["source_parity"].items():
        data = (ROOT / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["moved_sha256"], rel
        assert len(data.decode("utf-8").splitlines()) == entry["moved_lines"], rel


def test_ac4_every_required_and_test_file_exists_and_no_skip_markers() -> None:
    mf = _manifest()
    for rel in [*mf["required_files"], *mf["required_fixtures"], *mf["test_files"]]:
        assert (ROOT / rel).is_file(), rel
    skip = re.compile(r"pytest\.mark\.(skip|xfail|skipif)|pytest\.(skip|xfail)\(|importorskip")
    for rel in mf["test_files"]:
        if rel == str(Path(__file__).relative_to(ROOT)):
            continue  # this file names the markers in the pattern above
        assert not skip.search((ROOT / rel).read_text(encoding="utf-8")), rel


def test_ac4_runner_rejects_a_manifest_with_a_wrong_collect_count(tmp_path: Path) -> None:
    mf = _manifest()
    first = mf["test_files"][0]
    mf["expected_collect"][first] += 1
    mf["expected_ran"][first] += 1
    bad = ROOT / "docs" / "dev" / "_stage2_6_negative_control_manifest.json"
    try:
        bad.write_text(json.dumps(mf), encoding="utf-8")
        cp = subprocess.run(
            [sys.executable, "-I", "scripts/ci/run_stage1b1_tests.py", str(bad.relative_to(ROOT))],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        bad.unlink(missing_ok=True)
    assert cp.returncode == 1 and "collect mismatch" in cp.stdout


def test_ac4_legacy_slug_is_not_hardcoded_in_migrated_files() -> None:
    mf = _manifest()
    for rel in [*mf["source_parity"], *mf["test_files"]]:
        if rel.endswith(".py"):
            assert LEGACY_SLUG not in (ROOT / rel).read_text(encoding="utf-8"), rel
