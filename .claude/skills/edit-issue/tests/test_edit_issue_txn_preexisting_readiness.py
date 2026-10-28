"""Issue #2922: pre-existing readiness defect compatibility lane of
``edit_issue_txn.py``.

A CLOSED historical Issue whose live body already fails static readiness may
still receive a note-only (``## Notes for Reviewer``) update, provided the
readiness defect multiset stays exactly unchanged. gh mutation / readback and
guard / hygiene are fakes; most tests also fake the readiness checker with a
deterministic ``DEFECT <name>`` line scanner, and the integration test runs the
real ``contract_readiness_check.py --mode static``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import yaml


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import edit_issue_txn as txn  # noqa: E402


PRODUCTION_ROOT = Path(__file__).resolve().parents[4]
LIVE_UPDATED_AT = "2026-07-03T10:40:51Z"
NEEDS_FIX_ERROR = "readiness_needs_fix_without_resolution_evidence"
READINESS_ERROR = "guard_or_readiness_failed_before_mutation"


class _CP:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def repo_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tmp").mkdir()
    (root / "artifacts").mkdir()
    monkeypatch.setattr(txn, "REPO_ROOT", root)
    monkeypatch.setattr(txn, "CONTROLLED_EXEC", root / "scripts" / "controlled_exec.py")
    monkeypatch.setattr(txn, "GUARD_SCRIPT", root / "guard.py")
    monkeypatch.setattr(txn, "HYGIENE_SCRIPT", root / "hygiene.py")
    monkeypatch.setattr(txn, "READINESS_SCRIPT", root / "readiness.py")
    return root


# --- fake readiness checker -------------------------------------------------


def _fake_readiness_output(body: str) -> str:
    """One error per ``DEFECT <name>`` line; line numbers / fix_hint vary freely."""
    section = "(global)"
    errors: list[dict[str, Any]] = []
    for lineno, line in enumerate(body.splitlines(), start=1):
        if line.startswith("## "):
            section = line[3:].strip()
        match = re.match(r"DEFECT (\w+)", line)
        if match:
            errors.append(
                {
                    "rule_id": "FAKE001",
                    "category": "fake_category",
                    "section": section,
                    "line_start": lineno,
                    "line_end": lineno,
                    "minimal_context": [match.group(1)],
                    "fix_hint": f"fix at line {lineno}",
                }
            )
    return json.dumps({"schema": "ISSUE_CONTRACT_READINESS_RESULT_V1", "errors": errors})


def _fake_readiness_cp(args: list[str]) -> _CP:
    body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
    out = _fake_readiness_output(body)
    return _CP(1 if json.loads(out)["errors"] else 0, stdout=out)


# --- harness ----------------------------------------------------------------


def _body(*, notes: str | None = None, outcome: str = "Outcome text", defects: tuple[str, ...] = ()) -> str:
    defect_lines = "".join(f"DEFECT {name}\n" for name in defects)
    text = (
        "## Machine-Readable Contract\n\ncontract\n\n"
        f"## Outcome\n\n{outcome}\n\n"
        f"## Acceptance Criteria\n\n- [ ] AC1: x\n{defect_lines}\n"
        "## Verification Commands\n\nvc\n\n"
        "## Allowed Paths\n\n- a.py\n\n"
        "## Stop Conditions\n\n- s\n\n"
        "## Delivery Rule\n\n- d\n"
    )
    if notes is not None:
        text += f"\n## Notes for Reviewer\n\n{notes}\n"
    return text


def _payload(
    repo_tmp: Path,
    *,
    live_body: str,
    new_body: str,
    forwarded_status: str,
    title_required: bool = False,
) -> dict[str, Any]:
    (repo_tmp / "tmp" / "new_body.md").write_text(new_body, encoding="utf-8")
    readiness_result: dict[str, Any] = {
        "status": forwarded_status,
        "body_sha256": "sha256:old",
        "source_checks": ["contract_readiness_check.py --mode static"],
        "errors": [],
        "readiness_result_ref": "artifact.json",
    }
    return {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": 2565,
        "repo": "squne121/agent-platform",
        "new_body_file": "tmp/new_body.md",
        "readiness_forwarding_payload": {"readiness_result": readiness_result},
        "comment_mode": {"mode": "skip"},
        "expected_previous_body_sha256": txn._sha256_text(live_body),
        "expected_previous_updated_at": LIVE_UPDATED_AT,
        "title_update": {
            "required": title_required,
            "proposed_title": "new title" if title_required else None,
            "reason": "retitle" if title_required else None,
        },
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    repo_tmp: Path,
    *,
    live_body: str,
    state: Any = "CLOSED",
    readiness: Callable[[list[str], int], _CP] | None = None,
    hygiene: Callable[[list[str]], _CP] | None = None,
    readback_body: Callable[[str], str] | None = None,
    allow_children: bool = True,
) -> SimpleNamespace:
    """Install fakes. `readiness(args, call_index)` replaces the checker run."""
    env = SimpleNamespace(
        invoked=[],
        executor_inputs=[],
        children=[],
        fetches=0,
        remote={"body": live_body, "updatedAt": LIVE_UPDATED_AT},
        readiness_calls=0,
    )

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        env.fetches += 1
        # Single snapshot (`--json title,body,updatedAt,state`): the live state
        # comes from the same readback as title / body / updatedAt. `state=None`
        # models a readback whose `state` key is absent.
        issue: dict[str, Any] = {"title": "t", "body": env.remote["body"], "updatedAt": env.remote["updatedAt"]}
        if state is not None:
            issue["state"] = state
        return issue, ""

    def _run(args: list[str], **_kwargs: object) -> _CP:
        if not allow_children:
            pytest.fail(f"child process must not be started: {args}")
        if str(txn.GUARD_SCRIPT) in args:
            env.children.append("guard")
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            env.children.append("hygiene")
            return hygiene(args) if hygiene else _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            env.children.append("readiness")
            index = env.readiness_calls
            env.readiness_calls += 1
            return readiness(args, index) if readiness else _fake_readiness_cp(args)
        pytest.fail(f"unexpected command: {args}")

    def _invoke(command_id: str, _issue: int, _repo: str, input_ref: str) -> tuple[_CP, dict | None]:
        env.invoked.append(command_id)
        payload = json.loads((repo_tmp / input_ref).read_text(encoding="utf-8"))
        env.executor_inputs.append(payload)
        new_body = payload["new_body"]
        env.remote["body"] = readback_body(new_body) if readback_body else new_body
        env.remote["updatedAt"] = "2026-07-03T10:41:51Z"
        return _CP(0), {"new_body_sha256": txn._sha256_text(new_body)}

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)
    return env


LANES = pytest.mark.parametrize(
    ("forwarded_status", "reject_code"),
    [("needs_fix", NEEDS_FIX_ERROR), ("go", READINESS_ERROR)],
    ids=["needs_fix_forwarding", "candidate_static_readiness"],
)


def _run_lane(
    monkeypatch: pytest.MonkeyPatch,
    repo_tmp: Path,
    *,
    live_body: str,
    new_body: str,
    forwarded_status: str,
    **install_kwargs: Any,
) -> tuple[dict[str, Any], SimpleNamespace]:
    env = _install(monkeypatch, repo_tmp, live_body=live_body, **install_kwargs)
    payload = _payload(repo_tmp, live_body=live_body, new_body=new_body, forwarded_status=forwarded_status)
    return txn.run_transaction(payload), env


def _assert_rejected(result: dict[str, Any], env: SimpleNamespace, code: str) -> None:
    assert result["status"] == "failed_no_mutation"
    assert result["mutation_started"] is False
    assert result["body_update"]["attempted"] is False
    assert env.invoked == []
    assert result["errors"][0]["code"] == code


# --- AC1 --------------------------------------------------------------------


@LANES
@pytest.mark.parametrize("live_has_notes", [False, True], ids=["notes_added", "notes_extended"])
def test_preexisting_readiness_note_only_allowed(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, forwarded_status: str, reject_code: str, live_has_notes: bool
) -> None:
    live = _body(defects=("a", "b"), notes="old note" if live_has_notes else None)
    new = _body(defects=("a", "b"), notes="old note\n\nclarification: historical issue, not resumed")
    result, env = _run_lane(monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status)

    assert result["status"] == "ok", result["errors"]
    assert result["errors"] == []
    assert env.invoked == ["issue_content.update"]
    assert env.executor_inputs[0]["new_body"] == new
    assert env.fetches >= 2  # live readback + post-edit readback
    assert result["body_update"]["status"] == "ok"
    assert result["body_update"]["remote_current_body_sha256"] == txn._sha256_text(new)


# --- AC2 --------------------------------------------------------------------


@LANES
@pytest.mark.parametrize(
    ("live_defects", "live_notes", "new_defects", "new_notes", "allowed"),
    [
        ((), None, (), "note\nDEFECT added", False),  # new defect key
        (("a",), None, (), "note\nDEFECT a", False),  # same key, count increased
        (("a",), "old\nDEFECT b", ("a",), "old", False),  # subset repair of notes-resident defect
        (("a", "b"), None, ("a",), "note", False),  # partial repair outside notes (also surface change)
        (("a",), "x", ("a",), "x\n\n\n\nextra lines shift line numbers", True),  # line_start/fix_hint only
    ],
    ids=["new_defect", "count_increase", "subset_repair_notes", "partial_repair_body", "line_numbers_only"],
)
def test_preexisting_readiness_worsened_rejected(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    forwarded_status: str,
    reject_code: str,
    live_defects: tuple[str, ...],
    live_notes: str | None,
    new_defects: tuple[str, ...],
    new_notes: str,
    allowed: bool,
) -> None:
    live = _body(defects=live_defects, notes=live_notes)
    new = _body(defects=new_defects, notes=new_notes)
    result, env = _run_lane(monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status)
    if allowed:
        assert result["status"] == "ok", result["errors"]
        assert env.invoked == ["issue_content.update"]
    else:
        _assert_rejected(result, env, reject_code)


# --- AC3 --------------------------------------------------------------------


@pytest.mark.parametrize("state", ["OPEN", None], ids=["open", "state_absent"])
def test_preexisting_readiness_open_issue_unchanged(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")

    # needs_fix forwarding: rejection after the live state readback, no child process at all.
    result, env = _run_lane(
        monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status="needs_fix",
        state=state, allow_children=False,
    )
    _assert_rejected(result, env, NEEDS_FIX_ERROR)
    assert env.children == []

    # candidate static readiness failure: legacy rejection even with the same live defect.
    result, env = _run_lane(
        monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status="go", state=state
    )
    _assert_rejected(result, env, READINESS_ERROR)
    assert env.readiness_calls == 1  # only the candidate run; no live-body baseline run


# --- AC4 --------------------------------------------------------------------


@LANES
@pytest.mark.parametrize(
    "heading",
    [
        "Machine-Readable Contract",
        "Outcome",
        "Acceptance Criteria",
        "Verification Commands",
        "Allowed Paths",
        "Stop Conditions",
        "Delivery Rule",
    ],
)
def test_preexisting_readiness_note_surface_only(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, forwarded_status: str, reject_code: str, heading: str
) -> None:
    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note").replace(
        f"## {heading}\n\n", f"## {heading}\n\nsilently edited line\n", 1
    )
    assert new != live
    result, env = _run_lane(monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status)
    _assert_rejected(result, env, reject_code)


@LANES
def test_preexisting_readiness_note_surface_includes_hygiene_autofix(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, forwarded_status: str, reject_code: str
) -> None:
    # The raw candidate differs from live only in the notes section, but the
    # hygiene autofix rewrites Outcome: the comparison uses mutated_candidate.
    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")

    def _hygiene(args: list[str]) -> _CP:
        out = Path(args[args.index("--out-file") + 1])
        fixed = out.read_text(encoding="utf-8").replace("Outcome text", "Outcome text (autofixed)")
        out.write_text(fixed, encoding="utf-8")
        return _CP(1)

    result, env = _run_lane(
        monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status, hygiene=_hygiene
    )
    _assert_rejected(result, env, reject_code)
    assert env.readiness_calls <= 1  # note-surface mismatch short-circuits the live baseline run


# --- AC5 --------------------------------------------------------------------


@LANES
def test_preexisting_readiness_sha_and_readback_preserved(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, forwarded_status: str, reject_code: str
) -> None:
    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")

    # expected_previous_body_sha256 mismatch
    env = _install(monkeypatch, repo_tmp, live_body=live)
    payload = _payload(repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status)
    payload["expected_previous_body_sha256"] = txn._sha256_text("some other body")
    result = txn.run_transaction(payload)
    _assert_rejected(result, env, "stale_precondition_before_mutation")

    # expected_previous_updated_at mismatch
    env = _install(monkeypatch, repo_tmp, live_body=live)
    payload = _payload(repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status)
    payload["expected_previous_updated_at"] = "2020-01-01T00:00:00Z"
    result = txn.run_transaction(payload)
    _assert_rejected(result, env, "stale_precondition_before_mutation")

    # the lane success path still sends both preconditions to the executor
    result, env = _run_lane(monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status)
    assert result["status"] == "ok"
    executor_input = env.executor_inputs[0]
    assert executor_input["expected_previous_body_sha256"] == txn._sha256_text(live)
    assert executor_input["expected_previous_updated_at"] == LIVE_UPDATED_AT

    # post-edit readback mismatch is reported as failed_after_mutation
    result, env = _run_lane(
        monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status,
        readback_body=lambda written: written + "\ndrift",
    )
    assert result["status"] == "failed_after_mutation"
    assert result["mutation_started"] is True
    assert env.invoked == ["issue_content.update"]
    assert any(e["code"] == "final_readback_mismatch" or "readback" in e["code"] for e in result["errors"])


# --- AC6 --------------------------------------------------------------------


@pytest.mark.parametrize("forwarded_status", ["human_judgment", "input_or_runtime_error"])
@pytest.mark.parametrize("state", ["CLOSED", "OPEN"])
def test_preexisting_readiness_human_judgment_fail_closed(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, forwarded_status: str, state: str
) -> None:
    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")
    result, env = _run_lane(
        monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status,
        state=state, allow_children=False,
    )
    assert result["status"] == "human_judgment"
    assert result["mutation_started"] is False
    assert env.invoked == []
    assert env.fetches == 0  # fail-closed before any live readback
    assert result["errors"][0]["code"] == "readiness_forwarding_requires_human_judgment"


# --- AC7 --------------------------------------------------------------------


def _relationships(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "parent": {"action": "unchanged", "issue_number": None},
        "add_blocked_by": [],
        "remove_blocked_by": [],
        "add_blocking": [],
        "remove_blocking": [],
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("relationships", "expected"),
    [
        (None, True),
        (_relationships(), True),
        ({}, True),
        (_relationships(parent={"action": "set", "issue_number": 5}), False),
        (_relationships(parent={"action": "remove", "issue_number": None}), False),
        (_relationships(add_blocked_by=[7]), False),
        (_relationships(remove_blocked_by=[7]), False),
        (_relationships(add_blocking=[7]), False),
        (_relationships(remove_blocking=[7]), False),
    ],
)
def test_preexisting_readiness_relationship_noop_predicate(relationships: Any, expected: bool) -> None:
    assert txn._native_relationships_is_noop(relationships) is expected
    assert txn._compat_lane_static_preconditions_met({"required": False}, relationships) is expected
    assert txn._compat_lane_static_preconditions_met({"required": True}, relationships) is False


@LANES
@pytest.mark.parametrize(
    "variant",
    [
        "title",
        "parent_set",
        "parent_remove",
        "add_blocked_by",
        "remove_blocked_by",
        "add_blocking",
        "remove_blocking",
    ],
)
def test_preexisting_readiness_title_or_relationship_rejected(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, forwarded_status: str, reject_code: str, variant: str
) -> None:
    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")
    env = _install(monkeypatch, repo_tmp, live_body=live)
    payload = _payload(
        repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status, title_required=variant == "title"
    )
    if variant == "parent_set":
        payload["native_relationships"] = _relationships(parent={"action": "set", "issue_number": 5})
    elif variant == "parent_remove":
        payload["native_relationships"] = _relationships(parent={"action": "remove", "issue_number": None})
    elif variant != "title":
        payload["native_relationships"] = _relationships(**{variant: [7]})
    # Phase A (relationship preflight) is outside this lane's scope; stub it so the
    # candidate static readiness path is reachable and Phase B must never run.
    monkeypatch.setattr(txn, "_prepare_native_relationship", lambda *_a, **_k: (True, {}))
    monkeypatch.setattr(
        txn, "_execute_native_relationship", lambda *_a, **_k: pytest.fail("native mutation must not run")
    )
    result = txn.run_transaction(payload)
    _assert_rejected(result, env, reject_code)


# --- AC8 --------------------------------------------------------------------


def _real_checker_fixture(
    *, notes: str | None = None, allowed_paths: tuple[str, ...] = (".claude/hooks/foo.py",)
) -> str:
    """Historical-style body: hard-required runtime assertions without
    `runtime_assertion_bindings` (RUNTIMEASSERT001 on real policy)."""
    paths = "".join(f"- {p}\n" for p in allowed_paths)
    body = f"""## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "historical fixture"
change_kind: workflow
```

## Outcome

Historical outcome sentence describing the delivered hook behavior in concrete terms.

## Acceptance Criteria

- [ ] AC1: concrete AC 1 <!-- runtime-verification: true -->

## Verification Commands

```bash
# AC1
$ rg -n 'concrete' file1.py
```

## Allowed Paths

{paths}
## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

```yaml
decision: immediate
applicable_acs:
  - AC1
execution_environment:
  cli_tools:
    - python3
skip_conditions:
  - "none"
fallback_policy:
  fallback_success_is_pass: false
artifact_requirements:
  - "artifacts/out.json"
```

## Required Skills

none
"""
    if notes is not None:
        body += f"\n## Notes for Reviewer\n\n{notes}\n"
    return body


def _real_checker_cp(args: list[str]) -> subprocess.CompletedProcess[str]:
    real_script = PRODUCTION_ROOT / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py"
    swapped = [str(real_script) if a == str(txn.READINESS_SCRIPT) else a for a in args]
    return subprocess.run(swapped, capture_output=True, text=True, shell=False, cwd=str(PRODUCTION_ROOT), timeout=120)


def _real_readiness(args: list[str], _index: int) -> subprocess.CompletedProcess[str]:
    return _real_checker_cp(args)


def _real_defects(body: str, tmp: Path) -> Counter:
    path = tmp / "tmp" / "probe.md"
    path.write_text(body, encoding="utf-8")
    cp = _real_checker_cp([sys.executable, str(txn.READINESS_SCRIPT), "--body-file", str(path), "--mode", "static"])
    counter = txn._defect_multiset_from_checker_output(cp.returncode, cp.stdout)
    assert counter is not None, cp.stderr
    return counter


@LANES
def test_preexisting_readiness_real_checker_integration(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, forwarded_status: str, reject_code: str
) -> None:
    live = _real_checker_fixture()
    new = _real_checker_fixture(notes="過去実装を再開しない旨の注記（履歴 Issue の clarification のみ）。")

    # The real checker reports the historical RUNTIMEASSERT001 on both bodies,
    # and the multiset (ignoring line numbers) is identical.
    live_defects = _real_defects(live, repo_tmp)
    assert any(key[0] == '"RUNTIMEASSERT001"' for key in live_defects)
    assert live_defects == _real_defects(new, repo_tmp)

    # End to end through the transaction with the real checker for both runs.
    env = _install(monkeypatch, repo_tmp, live_body=live, readiness=_real_readiness)
    payload = _payload(repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status)
    result = txn.run_transaction(payload)
    assert result["status"] == "ok", result["errors"]
    assert env.invoked == ["issue_content.update"]
    assert env.readiness_calls == 2  # candidate + live baseline

    # An extra hard-required assertion (subagent lifecycle surface) is a worsened
    # defect set: detected by the real checker's multiset.
    worsened_paths = (".claude/hooks/foo.py", "scripts/agent-ops/foo.py")
    worsened = _real_defects(_real_checker_fixture(allowed_paths=worsened_paths), repo_tmp)
    assert worsened != live_defects
    assert not (worsened - live_defects) == Counter()

    # Real-checker worsening reached through the transaction is rejected as well:
    # the candidate edits Allowed Paths (note-surface violation) in addition.
    env = _install(monkeypatch, repo_tmp, live_body=live, readiness=_real_readiness)
    bad = _real_checker_fixture(notes="note", allowed_paths=worsened_paths)
    payload = _payload(repo_tmp, live_body=live, new_body=bad, forwarded_status=forwarded_status)
    result = txn.run_transaction(payload)
    _assert_rejected(result, env, reject_code)


def _broken_output(kind: str) -> _CP:
    return {
        "non_json": _CP(1, stdout="Traceback: boom"),
        "exit_2": _CP(2, stdout=_fake_readiness_output(_body(defects=("a",)))),
        "exit_3": _CP(3, stdout=_fake_readiness_output(_body(defects=("a",)))),
        "exit_4": _CP(4, stdout=_fake_readiness_output(_body(defects=("a",)))),
        "json_array": _CP(1, stdout="[]"),
        "errors_missing": _CP(1, stdout=json.dumps({"schema": "X"})),
        "errors_not_list": _CP(1, stdout=json.dumps({"errors": "oops"})),
        "error_not_object": _CP(1, stdout=json.dumps({"errors": ["oops"]})),
    }[kind]


@pytest.mark.parametrize(
    "kind",
    ["non_json", "exit_2", "exit_3", "exit_4", "json_array", "errors_missing", "errors_not_list", "error_not_object"],
)
@pytest.mark.parametrize("broken", ["candidate", "live_baseline"])
def test_preexisting_readiness_checker_output_fail_closed(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, kind: str, broken: str
) -> None:
    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")
    broken_index = 0 if broken == "candidate" else 1

    def _readiness(args: list[str], index: int) -> _CP:
        return _broken_output(kind) if index == broken_index else _fake_readiness_cp(args)

    result, env = _run_lane(
        monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status="needs_fix", readiness=_readiness
    )
    _assert_rejected(result, env, NEEDS_FIX_ERROR)


# --- AC9 --------------------------------------------------------------------


def _result_schema() -> dict[str, Any]:
    docs = (PRODUCTION_ROOT / "docs" / "dev" / "agent-skill-boundaries.md").read_text(encoding="utf-8")
    section = docs.split("### ISSUE_EDIT_TXN_RESULT_V1", 1)[1]
    return yaml.safe_load(section.split("```yaml", 1)[1].split("```", 1)[0])


def _key_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _key_tree(v) for k, v in value.items()}
    return None


@LANES
def test_preexisting_readiness_result_schema_unchanged(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, forwarded_status: str, reject_code: str
) -> None:
    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")
    lane_result, _ = _run_lane(
        monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status
    )
    assert lane_result["status"] == "ok"
    assert lane_result["errors"] == []

    # A regular (defect-free, go-forwarded) success defines the baseline key tree.
    clean_live = _body()
    clean_new = _body(notes="note")
    baseline, _ = _run_lane(monkeypatch, repo_tmp, live_body=clean_live, new_body=clean_new, forwarded_status="go")
    assert baseline["status"] == "ok"
    assert _key_tree(lane_result) == _key_tree(baseline)
    assert set(lane_result) == set(baseline)

    # The canonical doc schema's required top-level keys are all still present
    # (the doc itself predates the additive native_relationships block, so it is
    # only used as a required-key floor, not as a closed validator).
    assert set(_result_schema()["required"]) <= set(lane_result)
    assert lane_result["schema"] == txn.RESULT_SCHEMA


# --- live state comes from the single `_fetch_issue` snapshot ---------------


@LANES
@pytest.mark.parametrize(
    "state_kwargs",
    [{"state": None}, {"state": ""}, {"state": 1}],
    ids=["state_missing", "state_empty", "state_not_str"],
)
def test_preexisting_readiness_state_readback_failure_fail_closed(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    forwarded_status: str,
    reject_code: str,
    state_kwargs: dict[str, Any],
) -> None:
    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")
    result, env = _run_lane(
        monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status, **state_kwargs
    )
    _assert_rejected(result, env, reject_code)
    if forwarded_status == "needs_fix":
        assert env.children == []  # original rejection: no child process at all


@pytest.mark.parametrize("forwarded_status", ["needs_fix", "go"])
def test_preexisting_readiness_state_reuses_snapshot_no_extra_readback(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, forwarded_status: str
) -> None:
    # The compat lane decision adds no readback of its own: the same number of
    # `_fetch_issue` snapshots (live + post-edit) is taken as for a clean edit.
    clean_live = _body()
    clean_new = _body(notes="note")
    clean_result, clean_env = _run_lane(
        monkeypatch, repo_tmp, live_body=clean_live, new_body=clean_new, forwarded_status="go"
    )
    assert clean_result["status"] == "ok", clean_result["errors"]

    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")
    result, env = _run_lane(monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status)
    assert result["status"] == "ok", result["errors"]
    assert env.fetches == clean_env.fetches


# --- forwarded needs_fix + CLOSED + unchanged body (reviewer warning 2) -------


def test_preexisting_readiness_needs_fix_closed_unchanged_body_current_behavior(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the current minimal behavior: a CLOSED Issue whose body is
    byte-identical to live (body_change_requested is False) is not rejected by
    the needs_fix guard and is not mutated either -- the transaction reports
    `no_change` with no body update, no error and no executor invocation."""
    live = _body(defects=("a",), notes="existing note")
    result, env = _run_lane(
        monkeypatch, repo_tmp, live_body=live, new_body=live, forwarded_status="needs_fix"
    )
    assert result["status"] == "no_change", result["errors"]
    assert result["errors"] == []
    assert result["mutation_started"] is False
    assert result["body_update"]["attempted"] is False
    assert env.invoked == []


# --- AC11: single snapshot readback -----------------------------------------

_REAL_FETCH_ISSUE = txn._fetch_issue


@pytest.mark.parametrize(
    ("forwarded_status", "reject_code"),
    [("needs_fix", NEEDS_FIX_ERROR), ("go", READINESS_ERROR)],
    ids=["needs_fix_forwarding", "candidate_static_readiness"],
)
@pytest.mark.parametrize("snapshot_state", ["OPEN", "CLOSED"])
def test_preexisting_readiness_single_snapshot_fetch_issue(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    forwarded_status: str,
    reject_code: str,
    snapshot_state: str,
) -> None:
    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")
    env = _install(monkeypatch, repo_tmp, live_body=live)
    # Run the real `_fetch_issue` on top of a recording gh-level fake.
    monkeypatch.setattr(txn, "_fetch_issue", _REAL_FETCH_ISSUE)
    child_runner = txn._run_command
    gh_argvs: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> _CP:
        if "view" in args and "issue" in args:
            gh_argvs.append(list(args[1:]))
            return _CP(
                0,
                stdout=json.dumps(
                    {
                        "title": "t",
                        "body": env.remote["body"],
                        "updatedAt": env.remote["updatedAt"],
                        # `state` here is the only place the live state can come from.
                        "state": snapshot_state,
                    }
                ),
            )
        return child_runner(args, **kwargs)

    monkeypatch.setattr(txn, "_run_command", _run)
    payload = _payload(repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status)
    result = txn.run_transaction(payload)

    json_args = [argv[argv.index("--json") + 1] for argv in gh_argvs]
    assert json_args and all(value == "title,body,updatedAt,state" for value in json_args), json_args
    assert "state" not in json_args  # no standalone `--json state` request is ever issued
    assert not hasattr(txn, "_fetch_issue_state")
    if snapshot_state == "OPEN":
        _assert_rejected(result, env, reject_code)
        assert len(gh_argvs) == 1  # the single live readback decided the rejection
    else:
        # CLOSED comes from that same readback and the compat lane allows the note-only edit.
        assert result["status"] == "ok", result["errors"]
        assert env.invoked


# --- AC12: CommonMark fence semantics for the Notes boundary -----------------

# Each tail is the Notes content after a `note` line; `{x}` is the item under
# `## Allowed Paths`. `real_h2=True` means that `## Allowed Paths` line is a real
# (non-code) H2 section after Notes; changing it must be refused. `real_h2=False`
# means it is code / prose inside the Notes section; changing it is note-only.
_BOUNDARY_CASES = [
    pytest.param("    ```\n\n## Allowed Paths\n\n- {x}\n", True, id="indent4_backtick_not_fence"),
    pytest.param("    ~~~\n\n## Allowed Paths\n\n- {x}\n", True, id="indent4_tilde_not_fence"),
    pytest.param("text\n\n  ## Allowed Paths\n\n- {x}\n", True, id="indent2_h2_is_real_h2"),
    pytest.param("text\n\n   ## Allowed Paths\n\n- {x}\n", True, id="indent3_h2_is_real_h2"),
    pytest.param("```\ncode\n```\n\n## Allowed Paths\n\n- {x}\n", True, id="closed_fence_then_real_h2"),
    pytest.param(
        "~~~\n```\n## Allowed Paths\n\n- inner\n~~~\n\n## Allowed Paths\n\n- {x}\n",
        True,
        id="tilde_fence_not_closed_by_backtick",
    ),
    pytest.param("````\ncode\n`````\n\n## Allowed Paths\n\n- {x}\n", True, id="four_backtick_closed_by_five"),
    pytest.param("````\n```\n## Allowed Paths\n\n- {x}\n````\n", False, id="four_backtick_not_closed_by_three"),
    pytest.param("```\n~~~\n## Allowed Paths\n\n- {x}\n```\n", False, id="backtick_fence_not_closed_by_tilde"),
    pytest.param("   ```\n## Allowed Paths\n\n- {x}\n   ```\n", False, id="indent3_backtick_is_fence"),
    pytest.param("    ## Allowed Paths\n\n- {x}\n", False, id="indent4_h2_is_not_h2"),
]


@LANES
@pytest.mark.parametrize(("tail", "real_h2"), _BOUNDARY_CASES)
def test_preexisting_readiness_note_boundary_commonmark_fence_semantics(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    forwarded_status: str,
    reject_code: str,
    tail: str,
    real_h2: bool,
) -> None:
    def _with(x: str) -> str:
        return _body(defects=("a",), notes="note\n\n" + tail.format(x=x))

    live = _with("a.py")
    candidate = _with("b.py")

    # Pure boundary predicate.
    assert txn._only_notes_section_differs(live, candidate) is (not real_h2)
    assert txn._only_notes_section_differs(live, live) is True

    # End-to-end: the readiness defect multiset is identical (a single
    # `DEFECT a`) for live and candidate, so only the boundary decision can reject.
    live_defects = txn._defect_multiset_from_checker_output(1, _fake_readiness_output(live))
    candidate_defects = txn._defect_multiset_from_checker_output(1, _fake_readiness_output(candidate))
    assert live_defects == candidate_defects

    result, env = _run_lane(
        monkeypatch, repo_tmp, live_body=live, new_body=candidate, forwarded_status=forwarded_status
    )
    if real_h2:
        _assert_rejected(result, env, reject_code)
    else:
        assert result["status"] == "ok", result["errors"]
        assert env.invoked


# --- boundary SSOT unavailable (reviewer warning: FileNotFoundError leak) -----


@LANES
def test_preexisting_readiness_note_boundary_ssot_unavailable_fail_closed(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, forwarded_status: str, reject_code: str
) -> None:
    # Force the shared prose boundary SSOT to be missing on disk. A cached
    # module from an earlier test must not mask the failure; monkeypatch
    # restores the previous sys.modules entry (or absence) afterwards.
    monkeypatch.delitem(sys.modules, txn._PROSE_BOUNDARY_POLICY_MODULE_NAME, raising=False)
    monkeypatch.setattr(txn, "_PROSE_BOUNDARY_POLICY_RELPATH", Path("missing") / "prose_boundary_policy.py")

    live = _body(defects=("a",))
    new = _body(defects=("a",), notes="note")
    # (1) a missing SSOT file fails closed instead of raising FileNotFoundError.
    assert txn._only_notes_section_differs(live, new) is False
    assert txn._PROSE_BOUNDARY_POLICY_MODULE_NAME not in sys.modules

    # (2) the compatibility lane is rejected before any mutation.
    result, env = _run_lane(monkeypatch, repo_tmp, live_body=live, new_body=new, forwarded_status=forwarded_status)
    _assert_rejected(result, env, reject_code)
