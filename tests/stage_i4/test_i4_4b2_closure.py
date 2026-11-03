"""I-4-4B2: remaining VC adjudication / Step 4 source tests closure (#79).

Fixture evidence only: real CLI subprocesses, a fake `gh` shim and temporary git repositories.
Nothing here touches the network or any live GitHub state, and none of it is a Final Gate /
live E2E result.  `adjudicate_vc_result.py` and the producer receipt schema are I-4-4B1 assets and
are only re-used here.
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
MANIFEST = ROOT / "docs" / "dev" / "i4-4b2-vc-step4-tests-manifest.json"
B1_MANIFEST = ROOT / "docs" / "dev" / "i4-4b1-vc-adjudication-manifest.json"
IRL = ".claude/skills/impl-review-loop"
TESTS = f"{IRL}/tests"
TERMINAL_TESTS = f"{TESTS}/test_independent_vc_terminal_gate_regression.py"
SKIP_MARKERS = re.compile(r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|pytest\.xfail\(")
ALLOWED_DEFERRED_OWNERS = {"I-4-4D"}


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def parity_problems(m: dict[str, Any], root: Path) -> list[str]:
    """Source/moved SHA-256 and intentional_diff consistency of every moved test file."""
    problems: list[str] = []
    if set(m["test_files"]) != set(m["source_parity"]):
        problems.append("test_files != source_parity keys")
    for rel, info in m["source_parity"].items():
        path = root / rel
        if not path.is_file():
            problems.append(f"missing: {rel}")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != info["moved_sha256"]:
            problems.append(f"moved sha mismatch: {rel}")
        if info["verbatim"] and info["source_sha256"] != info["moved_sha256"]:
            problems.append(f"verbatim but sha differs: {rel}")
        if not info["verbatim"] and not info.get("intentional_diff"):
            problems.append(f"non-verbatim without intentional_diff: {rel}")
    return problems


def deferred_problems(m: dict[str, Any], b1: dict[str, Any], tests_dir: Path) -> list[str]:
    """Deferred source tests are recorded (owner, reason, rerun) and absent from the moved files."""
    problems: list[str] = []
    seen: set[str] = set()
    b1_ids = {e["id"] for e in b1["deferred_source_tests"]}
    for e in m["deferred_source_tests"]:
        if not all(e.get(k) for k in ("id", "reason", "owner_stage", "rerun_condition", "cases")):
            problems.append(f"incomplete deferred entry: {e.get('id')}")
            continue
        if e["owner_stage"].split(" ")[0] not in ALLOWED_DEFERRED_OWNERS:
            problems.append(f"unknown owner: {e['id']}")
        if e["id"] in seen or e["id"] in b1_ids:
            problems.append(f"duplicate with itself or the B1 manifest: {e['id']}")
        seen.add(e["id"])
        rel_file, name = e["id"].split("::")
        path = tests_dir / rel_file
        if not path.is_file():
            problems.append(f"deferred file is not a moved file: {e['id']}")
        elif re.search(
            rf"^def {re.escape(name)}\b", path.read_text(encoding="utf-8"), re.MULTILINE
        ):
            problems.append(
                f"deferred test still defined (restore it or drop the entry): {e['id']}"
            )
    if {Path(f).name for f in b1["test_files"]} & {Path(f).name for f in m["test_files"]}:
        problems.append("a test file is claimed by both B1 and B2")
    t = m["totals"]
    if (
        sum(e["cases"] for e in m["deferred_source_tests"])
        != t["removed_to_deferred_source_tests_cases"]
    ):
        problems.append("deferred case count != totals")
    if len(m["deferred_source_tests"]) != t["removed_functions"]:
        problems.append("deferred function count != totals")
    if (
        t["ran"] + t["removed_to_deferred_source_tests_cases"]
        != t["source_collected_before_removal"]
    ):
        problems.append("ran + removed != source collected")
    return problems


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


def test_manifest_required_files_and_parity() -> None:
    m = _manifest()
    for rel in m["required_files"] + m["test_files"]:
        assert (ROOT / rel).is_file(), rel
    assert parity_problems(m, ROOT) == []
    b1 = json.loads(B1_MANIFEST.read_text(encoding="utf-8"))
    # the adjudicator and the receipt schema are B1 assets: re-used byte-for-byte, never re-migrated
    for rel in (
        f"{IRL}/scripts/adjudicate_vc_result.py",
        "schemas/test-verdict-producer-receipt.schema.json",
    ):
        assert rel in m["required_files"] and rel not in m["source_parity"]
        digest = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        assert digest == b1["source_parity"][rel]["moved_sha256"], rel


def test_no_functional_legacy_slug() -> None:
    r = subprocess.run(
        [sys.executable, "-I", "scripts/ci/check_no_functional_slug.py", IRL],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_deferred_source_tests_are_recorded_not_silenced() -> None:
    m = _manifest()
    b1 = json.loads(B1_MANIFEST.read_text(encoding="utf-8"))
    assert m["deferred_tests"] == [], "nothing is deselected/skipped at run time"
    assert m["deferred_source_tests"], "unmigrated source tests must be listed"
    assert deferred_problems(m, b1, ROOT / TESTS) == []
    for rel in m["test_files"]:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not SKIP_MARKERS.search(text), rel


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
    for key in (
        "live_e2e",
        "final_gate_wired",
        "loop_handoff_result_v1_wired",
        "test_verdict_publish_enabled",
        "merge_tree_oracle_real_evidence",
        "github_live_operations",
    ):
        assert m[key] is False, key


def test_reused_dependencies_exist() -> None:
    """The migrated tests drive these already-migrated scripts; none may be re-implemented here."""
    for rel in _manifest()["required_files"]:
        assert (ROOT / rel).is_file(), rel


# --- Step 4 closed gate -> no terminal approval (behavioural, real functions) -------------------


@pytest.fixture
def terminal() -> Any:
    module = _load(TERMINAL_TESTS, "terminal_tests_i4_4b2_closure")
    yield module
    sys.modules.pop("terminal_tests_i4_4b2_closure", None)


def _step4(t: Any, state: dict[str, Any], *, adjudicated_head: str) -> int:
    code, _ = t.mod.step4_adjudicate(
        state,
        test_verdict=t._test_verdict(t.HEAD_A, t.BODY_A, False),
        contract_snapshot=t._contract_snapshot(t.BODY_A, False),
        diff_summary=t._diff_summary(t.HEAD_A),
        allowed_paths=t.ALLOWED_PATHS,
        expected_issue_number=t.ISSUE_NUMBER,
        expected_pr_number=t.PR_NUMBER,
        expected_head_sha=adjudicated_head,
        expected_contract_body_sha256=t.BODY_A,
        expected_command_hashes=t._hashes(False),
    )
    return code


def _terminal(t: Any, state: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return t.mod.step5_terminal_gate(
        state,
        t._reviewer_verdict(t.HEAD_A),
        t._live_mergeability(t.HEAD_A),
        expected_head_sha=t.HEAD_A,
        expected_contract_body_sha256=t.BODY_A,
        expected_command_hashes=t._hashes(False),
        dispatch_seq=1,
    )


def test_positive_control_open_gate_reaches_terminal_approval(terminal: Any) -> None:
    """Without this the closed-gate assertions below could pass for an unrelated reason."""
    state: dict[str, Any] = {}
    assert _step4(terminal, state, adjudicated_head=terminal.HEAD_A) == 0
    code, term = _terminal(terminal, state)
    assert code == 0 and term["route"] == "approved", term


def test_stale_head_closes_step4_and_reviewer_approve_cannot_reach_terminal_approval(
    terminal: Any,
) -> None:
    state: dict[str, Any] = {}
    assert _step4(terminal, state, adjudicated_head=terminal.HEAD_B) != 0
    code, term = _terminal(terminal, state)
    assert code != 0 and term.get("route") != "approved", term


def test_terminal_approval_without_any_step4_gate_is_refused(terminal: Any) -> None:
    code, term = _terminal(terminal, {})
    assert code != 0 and term.get("route") != "approved", term
