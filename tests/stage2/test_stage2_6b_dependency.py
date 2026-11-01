"""Stage 2-6b (#36): dependency materialization lane - edit-txn delegation and write-zero guards.

Evidence boundary: GitHub is never contacted. The only `gh` on PATH is a fixture that logs every
call and fails closed (`fixtures/fake_gh_txn.py`); real subprocesses run in a scratch copy of the
repo. No live relationship / dependency mutation is made, and none is claimed.

Test names carry the Issue AC tag (`ac2` / `ac3`) so each AC's verification command is `-k acN`.
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

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude" / "skills" / "issue-refinement-loop"
MODULE = SKILL / "scripts" / "dependency_materializer.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRUSTED = "squne121/agent-platform"
LEGACY_SLUG = "/".join(["squne121", "loop-protocol"])  # noqa: FLY002 - no literal for the slug scan

sys.path.insert(0, str(SKILL / "scripts"))
import dependency_materializer as dm

BAD_REPOS = [
    LEGACY_SLUG,
    "squne121/other-repo",
    "other-owner/agent-platform",
    "SQUNE121/agent-platform",
    "squne121/agent-platform ",
    "o/r",
]


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


class Harness:
    """A scratch repo copy plus a fail-closed logging `gh` first on PATH."""

    def __init__(self, tmp: Path) -> None:
        self.root = _copy_project(tmp / "proj")
        gh = tmp / "bin" / "gh"
        gh.parent.mkdir()
        src = (FIXTURES / "fake_gh_txn.py").read_text(encoding="utf-8").split("\n", 1)[1]
        gh.write_text(f"#!{sys.executable}\n" + src, encoding="utf-8")
        gh.chmod(0o755)
        self.state = tmp / "gh_state.json"
        self.state.write_text(json.dumps({"repo": TRUSTED, "issue": {}}), encoding="utf-8")
        self.log = tmp / "gh_calls.log"
        self.env = {
            **{k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN"}},
            "PATH": f"{gh.parent}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_STATE": str(self.state),
            "FAKE_GH_LOG": str(self.log),
        }

    @property
    def gh_calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.root / MODULE.relative_to(ROOT)), *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )


@pytest.fixture()
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


# --------------------------------------------------------------------------- AC2 delegation


def _subprocess_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "subprocess"
    ]


def _gh_literal_sites(tree: ast.AST) -> list[int]:
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "gh"]


def test_ac2_module_has_one_subprocess_and_it_targets_edit_issue_txn_only() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    calls = _subprocess_calls(tree)
    assert len(calls) == 1
    first_arg = ast.unparse(calls[0].args[0])
    assert "edit_txn.SCRIPT_PATH" in first_arg and "--input-file" in first_arg
    assert _gh_literal_sites(tree) == [], "no direct `gh` invocation in the module"


def test_ac2_negative_control_detector_flags_a_direct_gh_mutant() -> None:
    mutant = ast.parse('import subprocess\nsubprocess.run(["gh", "api", "-X", "POST", "x"])\n')
    assert _gh_literal_sites(mutant) == [2]
    assert len(_subprocess_calls(mutant)) == 1


def test_ac2_reads_and_writes_go_through_edit_issue_txn_helpers() -> None:
    src = MODULE.read_text(encoding="utf-8")
    for helper in (
        "_relationship_capability_preflight",
        "_fetch_native_relationships",
        "_fetch_issue",
    ):
        assert f"edit_txn.{helper}(" in src
    assert "NATIVE_RELATIONSHIPS_KEYS" in src


def test_ac2_materialize_delta_through_the_real_delegate_is_never_ok_and_writes_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in harness.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(dm, "REPO_ROOT", harness.root)
    monkeypatch.setattr(
        dm, "EDIT_ISSUE_SCRIPTS_DIR", harness.root / ".claude/skills/edit-issue/scripts"
    )

    class _Txn:
        SCRIPT_PATH = harness.root / ".claude/skills/edit-issue/scripts/edit_issue_txn.py"
        NATIVE_RELATIONSHIPS_KEYS = frozenset({"add_blocked_by", "remove_blocked_by"})

    monkeypatch.setattr(dm, "_load_edit_issue_txn", lambda: _Txn)
    res = dm.materialize_dependencies(
        target_issue_number=5,
        repo=TRUSTED,
        desired_predecessors=[3],
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=lambda *_a: ({"blocked_by": []}, ""),
        fetch_issue_content=lambda *_a: ({"body": "## Blocked By\n- #3\n", "updatedAt": "t"}, ""),
    )
    assert res["status"] != "ok" and res["native_relationship_materialized"] is False
    assert res["edit_txn_status"] == "failed_no_mutation"
    assert "native_relationships_unapproved" in json.dumps(res["errors"])
    assert harness.gh_calls == [], "the unapproved relationship path reached gh"


# --------------------------------------------------------------------------- AC3 false-green guards


@pytest.mark.parametrize("repo", BAD_REPOS)
def test_ac3_wrong_repo_materialize_is_write_zero_in_process(repo: str) -> None:
    touched: list[str] = []
    res = dm.materialize_dependencies(
        target_issue_number=5,
        repo=repo,
        desired_predecessors=[3],
        capability_preflight=lambda: touched.append("preflight") or (True, ""),
        fetch_live_snapshot=lambda *_a: touched.append("snapshot") or ({"blocked_by": []}, ""),
        fetch_issue_content=lambda *_a: touched.append("content") or ({"body": ""}, ""),
        invoke_edit_issue_txn=lambda *_a, **_k: touched.append("txn") or {},
    )
    assert touched == []
    assert res["status"] == "blocked" and res["native_relationship_materialized"] is False
    assert res["errors"][0]["code"] == "repo_untrusted"


@pytest.mark.parametrize("repo", BAD_REPOS)
def test_ac3_wrong_repo_cli_makes_zero_gh_calls(harness: Harness, repo: str) -> None:
    cp = harness.run(
        "materialize", "--target-issue", "5", "--repo", repo, "--desired-predecessors", "3"
    )
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert cp.returncode == 1 and result["status"] == "blocked"
    assert harness.gh_calls == []


def test_ac3_wrong_repo_gate_is_a_persistent_block_with_zero_gh_calls(harness: Harness) -> None:
    decision = harness.root / "decision.json"
    decision.write_text(
        json.dumps(
            {
                "relations": [
                    {
                        "source_issue_number": 5,
                        "target_issue_number": 3,
                        "relation_type": "depends_on",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    cp = harness.run(
        "gate",
        "--target-issue",
        "5",
        "--repo",
        LEGACY_SLUG,
        "--current-decision-file",
        str(decision),
    )
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert cp.returncode == 1 and result["decision"] == "block_persistent"
    assert harness.gh_calls == []


def _txn_result(status: str, observed: list[int]) -> dict:
    return {
        "schema": "ISSUE_EDIT_TXN_RESULT_V1",
        "status": status,
        "native_relationships": {
            "attempted": True,
            "status": "ok",
            "after": {"blocked_by": observed},
            "errors": [],
        },
        "errors": [],
    }


def _materialize(observed: list[int], status: str = "ok") -> dict:
    return dm.materialize_dependencies(
        target_issue_number=5,
        repo=TRUSTED,
        desired_predecessors=[3],
        capability_preflight=lambda: (True, ""),
        fetch_live_snapshot=lambda *_a: ({"blocked_by": []}, ""),
        fetch_issue_content=lambda *_a: ({"body": "b", "updatedAt": "t"}, ""),
        invoke_edit_issue_txn=lambda *_a, **_k: _txn_result(status, observed),
    )


def test_ac3_positive_control_executor_ok_with_confirming_readback_is_ok() -> None:
    res = _materialize(observed=[3])
    assert res["status"] == "ok" and res["native_relationship_materialized"] is True


@pytest.mark.parametrize("observed", [[], [4], [3, 4]])
def test_ac3_executor_ok_with_a_drifted_readback_is_never_ok(observed: list[int]) -> None:
    res = _materialize(observed=observed)
    assert res["status"] != "ok" and res["native_relationship_materialized"] is False
    assert res["failure_class"] == "readback-mismatch"


def test_ac3_partial_executor_failure_is_never_ok() -> None:
    res = _materialize(observed=[3], status="failed_after_mutation")
    assert res["status"] != "ok" and res["native_relationship_materialized"] is False


def test_ac3_body_only_blocked_by_is_detected_as_false_green() -> None:
    body = "## Blocked By\n- #3\n"
    flagged, reason = dm.detect_body_only_false_green(
        body, native_relationship_attempted=False, capability_available=True
    )
    assert flagged and reason == "body_only_predecessor_mutation_without_native_materialization"
    attempted, _ = dm.detect_body_only_false_green(
        body, native_relationship_attempted=True, capability_available=True
    )
    assert attempted is False
