"""I-4-4B1: VC adjudication (`adjudicate_vc_result`) closure (#77).

Fixture evidence only: pure adjudication plus a temporary git repository.  Nothing here touches
the network, `gh`, or any live GitHub state, and none of it is a Final Gate / live E2E result.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "i4-4b1-vc-adjudication-manifest.json"
IRL = ".claude/skills/impl-review-loop"
SCRIPT = f"{IRL}/scripts/adjudicate_vc_result.py"
SCHEMA = "schemas/test-verdict-producer-receipt.schema.json"
TERMINAL_TESTS = f"{IRL}/tests/test_independent_vc_terminal_gate_regression.py"
SHA = "a" * 40


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture
def adj() -> Any:
    module = _load(SCRIPT, "adjudicate_vc_result_i4_4b1_closure")
    yield module
    sys.modules.pop("adjudicate_vc_result_i4_4b1_closure", None)


def test_isolated_import() -> None:
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('m',{SCRIPT!r});"
        "m=importlib.util.module_from_spec(s);sys.modules['m']=m;s.loader.exec_module(m)"
    )
    r = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr


def test_manifest_required_files_and_parity() -> None:
    m = _manifest()
    assert {SCRIPT, SCHEMA} <= set(m["required_files"])
    assert set(m["test_files"]) <= set(m["source_parity"])
    for rel, info in m["source_parity"].items():
        digest = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        assert digest == info["moved_sha256"], rel
        if info["verbatim"]:
            assert info["source_sha256"] == info["moved_sha256"], rel
        else:
            assert info.get("intentional_diff"), rel
    for rel in (SCRIPT, SCHEMA):  # the script and the existing schema move byte-for-byte
        assert m["source_parity"][rel]["verbatim"] is True, rel
    for rel in m["required_files"] + m["test_files"]:
        assert (ROOT / rel).is_file(), rel


def test_no_functional_legacy_slug() -> None:
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/check_no_functional_slug.py", IRL],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_reused_dependencies_exist() -> None:
    """The adjudicator path-loads these already-migrated modules; none may be re-implemented."""
    for rel in (
        f"{IRL}/scripts/route_loop_verdict_v2.py",
        f"{IRL}/scripts/ensure_contract_snapshot.py",
        ".claude/skills/issue-contract-review/scripts/contract_review_result_parser.py",
        ".claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py",
        ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py",
    ):
        assert (ROOT / rel).is_file(), rel


def test_deferred_source_tests_are_recorded_not_silenced() -> None:
    m = _manifest()
    assert m["deferred_tests"] == [], "nothing is deselected/skipped at run time"
    assert m["deferred_source_tests"], "unmigrated source tests must be listed"
    owners = {e["owner_stage"].split(" ")[0] for e in m["deferred_source_tests"]}
    assert owners <= {"I-4-4C", "I-4-4D"}
    for entry in m["deferred_source_tests"]:
        assert all(entry.get(k) for k in ("id", "reason", "owner_stage", "rerun_condition")), entry
        rel_file, name = entry["id"].split("::")
        text = (ROOT / IRL / "tests" / rel_file).read_text(encoding="utf-8")
        assert name not in text, (rel_file, name)  # removed from the migrated file, never skipped
    for rel in m["test_files"]:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|pytest\.xfail\(", text), (
            rel
        )


def test_manifest_counts() -> None:
    m = _manifest()
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", *m["test_files"]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    actual: dict[str, int] = {}
    for line in r.stdout.splitlines():
        if "::" in line:
            actual[line.split("::")[0]] = actual.get(line.split("::")[0], 0) + 1
    assert actual == m["expected_collect"]
    assert m["expected_collect"] == m["expected_ran"]
    assert sum(m["expected_ran"].values()) == m["totals"]["ran"]


def test_fixture_only_evidence_boundary() -> None:
    m = _manifest()["evidence_boundary"]
    assert m["live_e2e"] is False
    assert m["final_gate_wired"] is False
    assert m["loop_handoff_result_v1_wired"] is False
    assert m["test_verdict_publish_enabled"] is False
    assert m["merge_tree_oracle_real_evidence"] is False


def test_producer_receipt_schema_is_the_existing_schema_and_loads(adj: Any) -> None:
    schema = json.loads((ROOT / SCHEMA).read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert adj._PRODUCER_RECEIPT_SCHEMA_PATH == ROOT / SCHEMA
    validator, error = adj._get_producer_receipt_validator()
    assert error is None and validator is not None
    # a receipt that is not an object / lacks required members must be rejected by the schema
    assert not validator.is_valid({})
    assert not validator.is_valid("receipt")


def test_missing_receipt_schema_never_yields_a_validator(adj: Any, tmp_path: Path) -> None:
    adj._PRODUCER_RECEIPT_VALIDATOR = None
    adj._PRODUCER_RECEIPT_SCHEMA_PATH = tmp_path / "absent.schema.json"
    validator, error = adj._get_producer_receipt_validator()
    assert validator is None
    assert error is not None and error.startswith("producer_receipt_schema_unreadable")
    (tmp_path / "broken.schema.json").write_text("{not json", encoding="utf-8")
    adj._PRODUCER_RECEIPT_SCHEMA_PATH = tmp_path / "broken.schema.json"
    validator, error = adj._get_producer_receipt_validator()
    assert validator is None
    assert error is not None and error.startswith("producer_receipt_schema_unreadable")


def test_reviewer_approve_alone_is_not_terminal_approval(adj: Any) -> None:
    """No Step 4 VC gate was opened: a router APPROVE must not become a terminal approval."""
    reviewer = {"verdict": "APPROVE", "reviewed_head_sha": SHA, "blockers": []}
    live = {"head_sha": SHA, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"}
    exit_code, term = adj.step5_terminal_gate(
        {},
        reviewer,
        live,
        expected_head_sha=SHA,
        expected_contract_body_sha256="sha256:" + "b" * 64,
        expected_command_hashes=["sha256:" + "c" * 64],
        dispatch_seq=1,
    )
    assert exit_code != 0
    assert term.get("route") != "approved"


def test_semantic_ambiguity_without_the_merge_tree_oracle_fails_closed(
    adj: Any, tmp_path: Path
) -> None:
    """`pr_head_replay_publish_exec.py` (the real oracle) is unmigrated (I-4-4C): fail closed.

    This is the migrated-stack counterpart of the source test
    `test_terminal_gate_resolves_semantic_ambiguity`, which needs the real oracle.  It proves
    the fail-closed branch only; it is not evidence that the real oracle works.
    """
    t = _load(TERMINAL_TESTS, "terminal_tests_i4_4b1_closure")
    try:
        oracle = ROOT / "scripts/agent-ops/pr_head_replay_publish_exec.py"
        assert not oracle.exists(), "oracle migrated: replace this test with the source test"

        def git(repo: Path, *args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True
            ).stdout.strip()

        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("base\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "base")
        branch = git(repo, "symbolic-ref", "--short", "HEAD")
        git(repo, "checkout", "-q", "-b", "candidate")
        (repo / "f.txt").write_text("candidate edit\n")
        git(repo, "commit", "-q", "-am", "candidate")
        candidate = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", branch)
        (repo / "f.txt").write_text("main edit\n")
        git(repo, "commit", "-q", "-am", "main")
        current = git(repo, "rev-parse", "HEAD")

        live = t._live_mergeability(t.HEAD_A)
        live["main_drift"] = {
            "current_base_sha": current,
            "evidence_base_sha": candidate,
            "allowed_paths_snapshot_base_sha": current,
            "allowed_paths": ["f.txt"],
            "latest_main_net_diff": ["f.txt"],
            "expected_old_sha": current,
            "observed_old_sha": current,
        }
        state: dict[str, Any] = {}
        common = {
            "expected_head_sha": t.HEAD_A,
            "expected_contract_body_sha256": t.BODY_A,
            "expected_command_hashes": t._hashes(False),
        }
        exit_code, _ = t.mod.step4_adjudicate(
            state,
            test_verdict=t._test_verdict(t.HEAD_A, t.BODY_A, False),
            contract_snapshot=t._contract_snapshot(t.BODY_A, False),
            diff_summary=t._diff_summary(t.HEAD_A),
            allowed_paths=t.ALLOWED_PATHS,
            expected_issue_number=t.ISSUE_NUMBER,
            expected_pr_number=t.PR_NUMBER,
            **common,
        )
        assert exit_code == 0
        exit_code, term = t.mod.step5_terminal_gate(
            state, t._reviewer_verdict(t.HEAD_A), live, cwd=repo, dispatch_seq=1, **common
        )
        assert exit_code == 1
        assert term["route"] == "fail_closed"
        assert term["reason_code"] == "semantic_ambiguity_oracle_unavailable"
    finally:
        sys.modules.pop("terminal_tests_i4_4b1_closure", None)


NON_REGRESSION_TESTS = f"{IRL}/tests/test_adjudicate_vc_result_non_regression_gate.py"


def _receipt_case(adj: Any, nr: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    head = "d" * 40
    command_hash = "sha256:" + "1" * 64
    items = [{"ac": "AC1", "command_hash": command_hash}]
    verdict = nr._manual_test_verdict(
        items, head_sha=head, reviewed_head_sha=head, diff_head_sha=head
    )
    kwargs = {
        "contract_snapshot": {"body_sha256": "sha256:" + "b" * 64},
        "current_vc_result": {"issue": 1488, "head_sha": head, "reviewed_head_sha": head},
        "diff_summary": {"pr_number": 1544, "head_sha": head},
        "expected_keys": {("AC1", command_hash)},
    }
    return verdict, kwargs


def test_require_producer_receipt_fails_closed_behaviourally(adj: Any) -> None:
    """With the receipt opt-in a verdict never passes on a missing/invalid receipt or schema."""
    nr = _load(NON_REGRESSION_TESTS, "non_regression_tests_i4_4b1_closure")
    try:
        verdict, kwargs = _receipt_case(adj, nr)
        # legacy self-attested path is unchanged (no opt-in -> bound verdict is accepted)
        assert adj._test_verdict_binding_error(verdict, **kwargs) is None
        check = adj._test_verdict_binding_error
        assert (
            check(verdict, require_producer_receipt=True, **kwargs)
            == "test_verdict_producer_receipt_missing"
        )
        verdict["producer_receipt"] = {}
        assert (
            check(verdict, require_producer_receipt=True, **kwargs)
            == "test_verdict_producer_receipt_schema_invalid"
        )
        # schema file missing -> validator unavailable (never a pass)
        adj._PRODUCER_RECEIPT_VALIDATOR = None
        adj._PRODUCER_RECEIPT_SCHEMA_PATH = ROOT / "schemas" / "absent.schema.json"
        reason = check(verdict, require_producer_receipt=True, **kwargs)
        assert reason is not None
        assert reason.startswith("test_verdict_producer_receipt_validator_unavailable:")
    finally:
        sys.modules.pop("non_regression_tests_i4_4b1_closure", None)


def test_require_producer_receipt_without_jsonschema_fails_closed(
    adj: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    nr = _load(NON_REGRESSION_TESTS, "non_regression_tests_i4_4b1_closure2")
    try:
        verdict, kwargs = _receipt_case(adj, nr)
        verdict["producer_receipt"] = {}
        adj._PRODUCER_RECEIPT_VALIDATOR = None
        monkeypatch.setitem(sys.modules, "jsonschema", None)
        reason = adj._test_verdict_binding_error(verdict, require_producer_receipt=True, **kwargs)
        assert reason is not None
        assert reason.startswith("test_verdict_producer_receipt_validator_unavailable:")
    finally:
        sys.modules.pop("non_regression_tests_i4_4b1_closure2", None)
