"""Issue #2207 AC8: `run_root_review_pipeline._cmd_produce()` derives an
invocation-local `ReviewBudget` from the pinned body via the canonical VC
plan / budget-formula modules, and passes its `per_attempt_seconds` /
`total_seconds` EXPLICITLY to `reviewer_transport.run_reviewer_transport()`
-- `reviewer_transport.py`'s own module-level fallback constants
(`PER_ATTEMPT_DEADLINE_SECONDS` / `TOTAL_DEADLINE_SECONDS`) must remain
UNCHANGED by this wiring.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REFINEMENT_SCRIPTS = Path(__file__).parent.parent / "scripts"
if str(REFINEMENT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(REFINEMENT_SCRIPTS))
import reviewer_transport as transport  # noqa: E402
import run_root_review_pipeline as pipeline  # noqa: E402

CONTRACT_REVIEW_SCRIPTS = (
    Path(__file__).resolve().parents[4] / ".claude" / "skills" / "issue-contract-review" / "scripts"
)
if str(CONTRACT_REVIEW_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CONTRACT_REVIEW_SCRIPTS))
import contract_readiness_check  # noqa: E402
import baseline_vc_preflight  # noqa: E402
from _trusted_repo import TRUSTED_REPO  # noqa: E402

derive_review_budget = contract_readiness_check.derive_review_budget
compute_canonical_vc_plan = baseline_vc_preflight.compute_canonical_vc_plan

# A body whose canonical plan has launch_upper_bound >= 3 (three DISTINCT
# non-pure commands, never dedup-replayed), so the derived per-attempt/total
# deadlines DIFFER from the N<=2 compatibility values (480s/520s) -- proving
# the wiring is genuinely invocation-local, not merely echoing the static
# fallback.
_HIGH_N_BODY = """## Verification Commands

```bash
$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_a.py -q
$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_b.py -q
$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_c.py -q
```
"""

# A body whose canonical plan has launch_upper_bound <= 2 (N<=2 compatibility).
_LOW_N_BODY = """## Verification Commands

```bash
$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_a.py -q
```
"""


def _make_produce_args(issue_number: int = 999901, repo: str = TRUSTED_REPO) -> argparse.Namespace:
    return argparse.Namespace(issue_number=issue_number, repo=repo)


def test_root_pipeline_passes_invocation_local_deadline_to_transport(monkeypatch):
    """AC8: `_cmd_produce()` computes the SAME canonical plan / `ReviewBudget`
    for the pinned body and passes `per_attempt_deadline` /
    `total_deadline` EXPLICITLY to `run_reviewer_transport()`, DIFFERING
    from the N<=2 compatibility values for a high-N body -- and
    `reviewer_transport.py`'s own module fallback constants are untouched."""
    plan = compute_canonical_vc_plan(_HIGH_N_BODY)
    # Issue #2207 OWNER P1-3 (PR #2221 REQUEST_CHANGES): the budget
    # denominator is `command_occurrence_count` per the Issue #2207
    # Outcome/AC5 contract, not `launch_upper_bound`.
    expected_budget = derive_review_budget(plan["command_occurrence_count"], policy_cap=plan["policy_cap"])
    assert plan["command_occurrence_count"] >= 3
    assert expected_budget.per_attempt_seconds != transport.PER_ATTEMPT_DEADLINE_SECONDS
    assert expected_budget.total_seconds != transport.TOTAL_DEADLINE_SECONDS

    captured: dict = {}

    def fake_fetch_and_pin_live_body(issue_number, repo, **kwargs):
        return _HIGH_N_BODY, pipeline.sha256_of(_HIGH_N_BODY), None

    def fake_run_reviewer_transport(**kwargs):
        captured.update(kwargs)
        # Short-circuit: force the early "transport failure" return path in
        # `_cmd_produce()` so this test does not need to mock the rest of
        # the (unrelated) downstream artifact-readback pipeline.
        return {"transport_status": "environment_failure", "invocation_id": "fixture-invocation-2207"}

    monkeypatch.setattr(pipeline, "fetch_and_pin_live_body", fake_fetch_and_pin_live_body)
    monkeypatch.setattr(pipeline._reviewer_transport, "run_reviewer_transport", fake_run_reviewer_transport)

    exit_code = pipeline._cmd_produce(_make_produce_args())

    assert exit_code == 2
    assert captured, "run_reviewer_transport() was never called"
    assert captured["per_attempt_deadline"] == expected_budget.per_attempt_seconds
    assert captured["total_deadline"] == expected_budget.total_seconds

    # reviewer_transport.py's own module-level fallback constants MUST NOT
    # be mutated by this wiring (Issue #2207 explicit requirement).
    assert transport.PER_ATTEMPT_DEADLINE_SECONDS == 480
    assert transport.TOTAL_DEADLINE_SECONDS == 520


def test_low_n_body_wiring_matches_current_production_values(monkeypatch):
    """AC5/AC8 cross-check: a low-N body's invocation-local deadline passed
    to the transport equals the CURRENT production fallback values exactly
    (480s/520s) -- the wiring is a strict generalization, not a behavior
    change for the common case."""
    captured: dict = {}

    def fake_fetch_and_pin_live_body(issue_number, repo, **kwargs):
        return _LOW_N_BODY, pipeline.sha256_of(_LOW_N_BODY), None

    def fake_run_reviewer_transport(**kwargs):
        captured.update(kwargs)
        return {"transport_status": "environment_failure", "invocation_id": "fixture-invocation-2207-low"}

    monkeypatch.setattr(pipeline, "fetch_and_pin_live_body", fake_fetch_and_pin_live_body)
    monkeypatch.setattr(pipeline._reviewer_transport, "run_reviewer_transport", fake_run_reviewer_transport)

    exit_code = pipeline._cmd_produce(_make_produce_args())

    assert exit_code == 2
    assert captured["per_attempt_deadline"] == 480
    assert captured["total_deadline"] == 520


def test_policy_ceiling_exceeded_rejects_before_any_checker_subprocess(monkeypatch):
    """AC7 (wired end-to-end): a body whose canonical plan exceeds the fixed
    policy ceiling is rejected by `_cmd_produce()` BEFORE
    `run_reviewer_transport()` (and therefore before any checker
    subprocess) is ever invoked."""
    # A body with more non-pure occurrences than the policy cap.
    lines = "\n".join(
        f"$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_{i}.py -q"
        for i in range(50)
    )
    over_cap_body = f"## Verification Commands\n\n```bash\n{lines}\n```\n"
    plan = compute_canonical_vc_plan(over_cap_body)
    assert plan["command_occurrence_count"] > plan["policy_cap"]

    def fake_fetch_and_pin_live_body(issue_number, repo, **kwargs):
        return over_cap_body, pipeline.sha256_of(over_cap_body), None

    calls = {"n": 0}

    def fake_run_reviewer_transport(**kwargs):
        calls["n"] += 1
        return {"transport_status": "ok", "invocation_id": "should-not-be-called"}

    monkeypatch.setattr(pipeline, "fetch_and_pin_live_body", fake_fetch_and_pin_live_body)
    monkeypatch.setattr(pipeline._reviewer_transport, "run_reviewer_transport", fake_run_reviewer_transport)

    exit_code = pipeline._cmd_produce(_make_produce_args())

    assert exit_code == 2
    assert calls["n"] == 0, "run_reviewer_transport() must NOT be called when the policy ceiling is exceeded"
