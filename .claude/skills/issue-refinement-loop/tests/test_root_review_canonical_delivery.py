"""Issue #2380 -- canonical Step 2 directly consumes `produce`'s own
root-verified `compact_result.verdict` / `compact_result.next_action` /
`verified_transport_artifact` and no longer relays the 11-line
`ISSUE_REVIEW_RESULT_COMPACT_V2` wire through the `issue-reviewer` child
agent.

AC1: `run_root_review_pipeline.py produce` (a REAL `_cmd_produce()` CLI
invocation -- only `fetch_and_pin_live_body` is monkeypatched to avoid a
live `gh` dependency; every downstream step, including the real checker
subprocess chain spawned via `reviewer_transport.run_reviewer_transport()`,
runs for real) returns `compact_result.verdict` / `compact_result.next_action`
that correctly route BOTH the approve and needs-fix cases, without going
through the `issue-reviewer` child agent's stdout at any point.

AC2: the canonical `produce` code path never calls `classify_child_stdout()`
or `retry_once_on_transport_failure()` -- proven both behaviorally (patching
both functions to raise AssertionError if invoked, then running the full
real `_cmd_produce()` roundtrip for both verdict branches) and via a static
source-shape check on `_cmd_produce`'s own source text (a regression guard
against a future edit silently reintroducing a call to either function or to
an `issue-reviewer` subprocess invocation).
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.util
import inspect
import io
import json
import sys
import textwrap
from pathlib import Path

import pytest
from _trusted_repo import TRUSTED_REPO  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
PIPELINE_SCRIPT = SCRIPTS_DIR / "run_root_review_pipeline.py"
SKILL_MD = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "SKILL.md"
REVIEW_ISSUE_SKILL_MD = ROOT / ".claude" / "skills" / "review-issue" / "SKILL.md"
COMMAND_REGISTRY_PY = SCRIPTS_DIR / "command_registry.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_pipeline_module():
    spec = importlib.util.spec_from_file_location("run_root_review_pipeline_canonical_delivery", PIPELINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_root_review_pipeline_canonical_delivery", module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_pipeline_module()
_REPO = TRUSTED_REPO

_APPROVE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2380 canonical delivery fixture (approve branch)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
canonical Step 2 directly consumes `compact_result.verdict` (Issue #2380).

## Acceptance Criteria

- [ ] AC1: fixture body is well-formed enough for check_issue_contract.py to
      synthesize a complete REVIEW_ISSUE_RESULT_V1 with verdict approve.

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true
```

## Allowed Paths

- fixture/e2e_produce_canonical_delivery_approve.md
"""

# C3 (`check_c3_ac_checkbox_format`) requires every AC line to use the
# `- [ ]` checkbox prefix; this fixture intentionally omits it so the REAL
# checker chain (not a hand-written fixture) produces a genuine
# `verdict: needs-fix` / `NEXT_ACTION: request_changes` result.
_NEEDS_FIX_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2380 canonical delivery fixture (needs-fix branch)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
canonical Step 2 directly consumes `compact_result.verdict` (Issue #2380).

## Acceptance Criteria

AC1: fixture body intentionally omits the `- [ ]` checkbox prefix so the
real checker chain's C3 check fails, producing a genuine needs-fix verdict.

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true
```

## Allowed Paths

- fixture/e2e_produce_canonical_delivery_needs_fix.md
"""


def _run_real_produce(tmp_path: Path, monkeypatch, *, body: str, issue_number: int) -> dict:
    """Run a REAL `_cmd_produce()` invocation (real checker subprocess chain
    via `reviewer_transport.run_reviewer_transport()`); only the live GitHub
    body fetch is replaced with a pinned fixture, matching the existing
    `test_root_review_pipeline_readback_v2_ssot.py` E2E pattern."""
    body_sha256 = _PIPELINE.sha256_of(body)
    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)

    def _fake_fetch(issue_number_, repo, timeout_seconds=15):
        return body, body_sha256, None

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch)

    args = argparse.Namespace(issue_number=issue_number, repo=_REPO)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _PIPELINE._cmd_produce(args)
    out = json.loads(buf.getvalue())
    assert rc == 0, out
    assert out["status"] == "ok", out
    return out


# ---------------------------------------------------------------------------
# AC1: canonical Step 2 directly consumes `compact_result.verdict` /
# `compact_result.next_action` for BOTH approve and needs-fix, without the
# `issue-reviewer` child agent's stdout ever entering the picture.
# ---------------------------------------------------------------------------


def test_given_real_produce_approve_body_when_run_then_compact_result_routes_approve_directly(
    tmp_path: Path, monkeypatch
):
    out = _run_real_produce(tmp_path, monkeypatch, body=_APPROVE_BODY, issue_number=2380001)

    compact_result = out["compact_result"]
    assert compact_result["verdict"] == "approve"
    assert compact_result["next_action"] == "proceed"
    assert out["merged_review_result"]["verdict"] == "approve"

    # `verified_transport_artifact` (REVIEWER_COMPACT_ARTIFACT_V2) is the
    # canonical artifact reference canonical Step 2 uses -- it must be
    # self-consistent with `compact_result.artifact_path`.
    vta = out["verified_transport_artifact"]
    assert vta["schema"] == "REVIEWER_COMPACT_ARTIFACT_V2"
    assert str(Path(vta["root"]) / vta["relative_path"]) == compact_result["artifact_path"]


def test_given_real_produce_needs_fix_body_when_run_then_compact_result_routes_needs_fix_directly(
    tmp_path: Path, monkeypatch
):
    out = _run_real_produce(tmp_path, monkeypatch, body=_NEEDS_FIX_BODY, issue_number=2380002)

    compact_result = out["compact_result"]
    assert compact_result["verdict"] == "needs-fix"
    assert compact_result["next_action"] == "request_changes"
    assert out["merged_review_result"]["verdict"] == "needs-fix"
    assert out["merged_review_result"]["blocking_issues"], "expected at least one blocking issue"


# ---------------------------------------------------------------------------
# AC2: the canonical `produce` code path never calls `classify_child_stdout()`
# / `retry_once_on_transport_failure()`, and never invokes the
# `issue-reviewer` child agent.
# ---------------------------------------------------------------------------


def _forbid(name: str):
    def _raise(*args, **kwargs):
        raise AssertionError(f"canonical Step 2 delivery path must not call {name}()")

    return _raise


@pytest.mark.parametrize(
    "body,issue_number",
    [
        (_APPROVE_BODY, 2380003),
        (_NEEDS_FIX_BODY, 2380004),
    ],
    ids=["approve", "needs-fix"],
)
def test_given_real_produce_when_run_then_classify_child_stdout_and_retry_are_never_called(
    tmp_path: Path, monkeypatch, body: str, issue_number: int
):
    monkeypatch.setattr(_PIPELINE, "classify_child_stdout", _forbid("classify_child_stdout"))
    monkeypatch.setattr(_PIPELINE, "retry_once_on_transport_failure", _forbid("retry_once_on_transport_failure"))

    # If either forbidden function were reached, `_run_real_produce()` would
    # raise AssertionError instead of returning `status: ok`.
    out = _run_real_produce(tmp_path, monkeypatch, body=body, issue_number=issue_number)
    assert out["status"] == "ok"


def test_cmd_produce_source_never_references_classify_child_stdout_retry_or_issue_reviewer_agent():
    """Static regression guard (Issue #2380 AC2): `_cmd_produce()`\'s own
    source text must not reference `classify_child_stdout`,
    `retry_once_on_transport_failure`, or spawn the `issue-reviewer` agent --
    a future edit that silently reintroduces the legacy relay/retry/child
    invocation into the canonical `produce` code path is caught here even if
    it happened to be behaviorally unreachable in the tests above."""
    source = inspect.getsource(_PIPELINE._cmd_produce)
    assert "classify_child_stdout" not in source
    assert "retry_once_on_transport_failure" not in source
    assert "issue-reviewer" not in source
    assert "issue_reviewer" not in source


def test_cmd_produce_result_is_self_sufficient_for_canonical_step2_routing(tmp_path: Path, monkeypatch):
    """AC1: `produce`\'s JSON output alone (no further child-agent round trip)
    must carry everything canonical Step 2 needs to route: `verdict`,
    `next_action`, and the verified artifact reference."""
    out = _run_real_produce(tmp_path, monkeypatch, body=_APPROVE_BODY, issue_number=2380005)
    compact_result = out["compact_result"]
    for key in ("verdict", "next_action", "artifact_path", "reviewed_body_sha256", "attempt_id"):
        assert key in compact_result, f"canonical Step 2 routing needs compact_result.{key}"
    for key in ("root", "relative_path", "sha256", "schema", "invocation_id", "attempt"):
        assert key in out["verified_transport_artifact"], (
            f"canonical Step 2 routing needs verified_transport_artifact.{key}"
        )


# ---------------------------------------------------------------------------
# SKILL.md static consistency (Issue #2380 AC6-adjacent): canonical Step 2\'s
# routing table text must key off `compact_result.verdict` directly and must
# state issue-reviewer is not invoked for canonical routing.
# ---------------------------------------------------------------------------


def test_skill_md_step2_routing_table_reads_compact_result_verdict_directly():
    text = SKILL_MD.read_text(encoding="utf-8")
    start = text.index("### Step 2: レビュー")
    end = text.index("### Step 2.5")
    step2_text = text[start:end]

    assert "compact_result.verdict: approve" in step2_text
    assert "compact_result.verdict: needs-fix" in step2_text


def test_skill_md_step2_states_issue_reviewer_not_invoked_for_canonical_routing():
    text = SKILL_MD.read_text(encoding="utf-8")
    start = text.index("### Step 2: レビュー")
    end = text.index("### Step 2.5")
    step2_text = text[start:end]

    assert "canonical Step 2 では起動されない" in step2_text
    assert "classify_child_stdout()" in step2_text
    assert "retry_once_on_transport_failure()" in step2_text


# ---------------------------------------------------------------------------
# P0-2 (Issue #2380 / OWNER PR #2386 review): canonical Step 2's routing pure
# function must key off the FULL `(status, verdict, next_action)` triple, not
# `verdict` alone -- a prior iteration silently ignored `next_action` and
# would have misrouted the valid `needs-fix` + `human_judgment_required`
# combination into the ordinary rewrite loop.
# ---------------------------------------------------------------------------


def test_route_canonical_step2_result_approve_proceed_routes_step_2_5(tmp_path: Path, monkeypatch):
    """Issue #2389: a deterministic approve routes to Step 2.5 (semantic
    design review applicability), not directly to Step 4.5 -- this proves
    the routing table itself no longer bypasses that applicability gate."""
    out = _run_real_produce(tmp_path, monkeypatch, body=_APPROVE_BODY, issue_number=2380010)
    assert _PIPELINE.route_canonical_step2_result(out) == _PIPELINE.STEP_2_5


def test_route_canonical_step2_result_needs_fix_request_changes_routes_step_4(tmp_path: Path, monkeypatch):
    out = _run_real_produce(tmp_path, monkeypatch, body=_NEEDS_FIX_BODY, issue_number=2380011)
    assert _PIPELINE.route_canonical_step2_result(out) == _PIPELINE.STEP_4


def test_route_canonical_step2_result_human_judgment_required_routes_step5_for_exact_triple_only():
    """The existing compact schema (`review-issue/SKILL.md` "Producer I/O
    ownership" section) allows `verdict: needs-fix` + `next_action:
    human_judgment_required` -- an unresolvable environment/timeout
    condition, NOT a body-rewrite-fixable contract defect. The prior
    `verdict`-only routing table would have sent this into the ordinary
    rewrite loop (Step 4); the fix routes the EXACT `verdict: needs-fix` +
    `next_action: human_judgment_required` triple to Step 5.

    OWNER PR #2391 review P1-1: a loose match on `next_action` alone (i.e.
    ignoring `verdict`) would ALSO match the inconsistent combination
    `verdict: approve` + `next_action: human_judgment_required`, which must
    fail closed instead -- see
    `test_route_canonical_step2_result_inconsistent_combos_fail_closed`
    below, which now covers that case."""
    result = {"status": "ok", "compact_result": {"verdict": "needs-fix", "next_action": "human_judgment_required"}}
    assert _PIPELINE.route_canonical_step2_result(result) == _PIPELINE.STEP_5_HUMAN_JUDGMENT_REQUIRED


@pytest.mark.parametrize(
    "result",
    [
        {"status": "ok", "compact_result": {"verdict": "approve", "next_action": "request_changes"}},
        {"status": "ok", "compact_result": {"verdict": "needs-fix", "next_action": "proceed"}},
        {"status": "ok", "compact_result": {}},
        {"status": "input_or_runtime_error", "compact_result": {}},
        {"status": "input_or_runtime_error", "compact_result": {"verdict": "approve", "next_action": "proceed"}},
        # OWNER PR #2391 review P1-1: a loose match on `next_action` alone
        # (ignoring `verdict`) would incorrectly route this inconsistent
        # combination to STEP_5_HUMAN_JUDGMENT_REQUIRED; the exact-triple
        # match must fail closed instead.
        {"status": "ok", "compact_result": {"verdict": "approve", "next_action": "human_judgment_required"}},
        # OWNER PR #2391 review P0-2: `input_or_runtime_error` with a KNOWN
        # producer error_code must NOT be routed to
        # STEP_5_HUMAN_JUDGMENT_REQUIRED -- Issue #2054's environment_failure
        # separation contract is preserved, not superseded.
        {"status": "input_or_runtime_error", "error_code": "reviewer_transport_environment_failure"},
        {"status": "input_or_runtime_error", "error_code": "artifact_readback_failed"},
    ],
    ids=[
        "approve_plus_request_changes",
        "needs_fix_plus_proceed",
        "ok_status_empty_compact_result",
        "non_ok_status_empty_compact_result",
        "non_ok_status_otherwise_valid_pair",
        "approve_plus_human_judgment_required_loose_match",
        "known_reviewer_transport_environment_failure_stays_fail_closed",
        "known_artifact_readback_failed_stays_fail_closed",
    ],
)
def test_route_canonical_step2_result_inconsistent_combos_fail_closed(result: dict):
    assert (
        _PIPELINE.route_canonical_step2_result(result)
        == _PIPELINE.FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE
    )


def _function_body_source_without_docstring(func) -> str:
    """Returns just the CODE of a function (no docstring, no signature/name),
    so a forbidden-substring check does not false-positive on legitimate
    prose in the function's own explanatory docstring."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    func_def = tree.body[0]
    assert isinstance(func_def, (ast.FunctionDef, ast.AsyncFunctionDef))
    body = func_def.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def test_route_canonical_step2_result_is_pure_and_never_invokes_issue_reviewer():
    """P0-3: the canonical CONSUMER route itself (not just `_cmd_produce()`)
    must be a pure `(status, verdict, next_action) -> target` mapping with NO
    invocation of the `issue-reviewer` agent, subprocess, or any callback --
    this static check on `route_canonical_step2_result()`'s own CODE (its
    docstring is deliberately excluded -- it legitimately discusses
    `issue-reviewer` in prose) is what actually proves the "producer ->
    issue-reviewer handoff" the prior test suite failed to exercise cannot
    occur through this consumer path."""
    body_source = _function_body_source_without_docstring(_PIPELINE.route_canonical_step2_result)
    for forbidden in (
        "issue-reviewer",
        "issue_reviewer",
        "subprocess",
        "Agent(",
        "classify_child_stdout",
        "retry_once_on_transport_failure",
    ):
        assert forbidden not in body_source, f"unexpected {forbidden!r} in route_canonical_step2_result() body"


def test_given_real_produce_output_when_routed_through_pure_router_end_to_end_then_no_agent_handoff_occurs(
    tmp_path: Path, monkeypatch
):
    """P0-3 canonical consumer path: feed a REAL `_cmd_produce()` roundtrip's
    OWN output into `route_canonical_step2_result()` -- the actual canonical
    Step 2 consumer, not a hand-rolled fixture dict -- for BOTH verdict
    branches. This is the missing link the prior AC1/AC2 tests never
    exercised: they proved `_cmd_produce()` itself never calls the legacy
    relay/classify functions, but never proved the CONSUMER of its output
    (this routing function) reaches the correct canonical target without any
    `issue-reviewer` handoff in between."""
    approve_out = _run_real_produce(tmp_path, monkeypatch, body=_APPROVE_BODY, issue_number=2380012)
    needs_fix_out = _run_real_produce(tmp_path, monkeypatch, body=_NEEDS_FIX_BODY, issue_number=2380013)
    assert _PIPELINE.route_canonical_step2_result(approve_out) == _PIPELINE.STEP_2_5
    assert _PIPELINE.route_canonical_step2_result(needs_fix_out) == _PIPELINE.STEP_4


# ---------------------------------------------------------------------------
# P0-1/P0-3 (Issue #2380 / OWNER PR #2386 review): whole-file / cross-file
# instruction-surface consistency. The pre-fix_delta static tests above only
# ever sliced "### Step 2" .. "### Step 2.5" out of SKILL.md, so they could
# never detect the SAME SKILL.md's own top-of-file "Loop Structure" diagram
# (or the sibling agent/registry/pipeline files) contradicting it. This
# regression guard checks the FULL text of every flagged active instruction
# surface, not just one narrow section of one file.
# ---------------------------------------------------------------------------

import re  # noqa: E402

_LEGACY_RELAY_REGRESSION_PHRASES: dict[Path, list[str]] = {
    SKILL_MD: [
        # pre-fix_delta top-of-file Loop Structure diagram routed through the
        # issue-reviewer agent instead of directly consuming `produce`.
        "issue-reviewer→root_review_pipeline(V2delegate)→ISSUE_REVIEW_RESULT_COMPACT_V2",
        # pre-fix_delta claim that the orchestrator trusts the issue-reviewer
        # AGENT's own self-reported VERDICT directly (as opposed to the
        # root-verified `compact_result.verdict` `produce` itself returns).
        "orchestratorは`issue-reviewer`のVERDICTを独立に再計算せず直接信頼する",
    ],
    REVIEW_ISSUE_SKILL_MD: [
        "`invoked_as_loop:true`（`issue-reviewer`経由）",
    ],
    COMMAND_REGISTRY_PY: [
        "itonlyrelays`compact_result.stdout_lines`verbatim.Thisisa",
    ],
}


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text)


@pytest.mark.parametrize(
    "surface_path",
    list(_LEGACY_RELAY_REGRESSION_PHRASES.keys()),
    ids=[p.name for p in _LEGACY_RELAY_REGRESSION_PHRASES],
)
def test_instruction_surfaces_no_longer_contain_legacy_relay_regression_phrases(surface_path: Path):
    """Cross-file consistency guard (Issue #2380 P0-1/P0-3): none of the
    specific legacy issue-reviewer-relay imperative sentences OWNER PR #2386
    review flagged may reappear, on ANY of the flagged active instruction
    surfaces -- not just the narrow "### Step 2" slice of one file the
    pre-existing static tests checked."""
    normalized = _normalize_whitespace(surface_path.read_text(encoding="utf-8"))
    for phrase in _LEGACY_RELAY_REGRESSION_PHRASES[surface_path]:
        assert phrase not in normalized, f"{surface_path}: found regression phrase {phrase!r}"


def test_pipeline_docstring_no_longer_claims_issue_reviewer_is_a_stdout_lines_consumer():
    """P0-1: `run_root_review_pipeline.py`'s own module docstring previously
    described `compact_result.stdout_lines` as being handed to the read-only
    `issue-reviewer` child as part of the CANONICAL `produce` contract, and
    separately listed the `issue-reviewer` agent as a "Consumer" of the
    persisted artifact schema alongside the orchestrator. Both claims
    contradicted the Issue #2380 canonical direct-consume routing."""
    source = PIPELINE_SCRIPT.read_text(encoding="utf-8")
    normalized = _normalize_whitespace(source)
    for phrase in (
        "sotheorchestratorcanhandthemtotheread-only`issue-reviewer`childasanexplicit,already-produced,",
        "read-onlyinput(thechildrelaysthemverbatim;itneverinvokes`compact_review_result.py`itself).",
        "andthe`issue-reviewer`read-onlychild(inputonly,neverawriter).",
    ):
        assert phrase not in normalized, f"run_root_review_pipeline.py: found regression phrase {phrase!r}"
