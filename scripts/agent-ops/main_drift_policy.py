"""Side-effect-free main-drift evidence policy (extracted from loop-protocol
`route_loop_verdict_v2.py` lines 191-291 at tag pre-split-agent-platform-20261020).

Only `classify_main_drift` and its types are carried over; the impl-review-loop
routing (I-4) is not migrated. Pure: no gh, git, network or subprocess calls.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

_MAIN_DRIFT_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


@dataclass(frozen=True)
class MainDriftDecision:
    route: str
    reason_code: str | None
    evidence_epoch: Mapping[str, object] | None
    reverify: Mapping[str, bool]
    reusable_evidence: Mapping[str, object | None]


# Bounded outer drift-rebind budget (Issue #2102 P0-B). Independent of
# LOOP_STATE.iteration / implementation_iteration_delta: main-drift-driven
# rebinds never spend the implementation iteration budget, but they are not
# unbounded either -- an actively churning main must not livelock the loop.
# The bound mirrors the precedent set by #2039 (pre-dispatch rebase budget
# capped at 1 retry, post-dispatch retry budget starts at 0 and never blind
# retries) and #1023 (bounded no-progress state distinct from the ordinary
# iteration counter).
DEFAULT_MAX_DRIFT_REBIND_ATTEMPTS = 2


def classify_main_drift(
    *,
    current_base_sha: str,
    evidence_base_sha: str,
    allowed_paths_snapshot_base_sha: str,
    allowed_paths: Iterable[str],
    latest_main_net_diff: Iterable[str],
    expected_head_sha: str,
    observed_head_sha: str,
    expected_old_sha: str,
    observed_old_sha: str,
    semantic_ambiguity: bool = False,
    behind_fast_path_eligible: bool = False,
    owner: str = "implementation",
    drift_rebind_attempts: int = 0,
    max_drift_rebind_attempts: int = DEFAULT_MAX_DRIFT_REBIND_ATTEMPTS,
) -> MainDriftDecision:
    """Classify base-bound evidence freshness without performing mutation."""
    values = (
        current_base_sha,
        evidence_base_sha,
        allowed_paths_snapshot_base_sha,
        expected_head_sha,
        observed_head_sha,
        expected_old_sha,
        observed_old_sha,
    )
    if any(not isinstance(value, str) or not _MAIN_DRIFT_SHA.fullmatch(value) for value in values):
        return MainDriftDecision("hard_stop", "base_sha_fingerprint_mismatch", None, {}, {})
    if expected_head_sha != observed_head_sha:
        return MainDriftDecision("hard_stop", "expected_head_cas_mismatch", None, {}, {})
    if expected_old_sha != observed_old_sha:
        return MainDriftDecision("hard_stop", "expected_old_cas_mismatch", None, {}, {})
    if current_base_sha == evidence_base_sha:
        return MainDriftDecision(
            "fresh",
            None,
            {"base_sha": current_base_sha, "epoch": 0, "owner": owner},
            {"snapshot": False, "ci": False, "review": False},
            {"snapshot": "current", "ci": "current", "review": "current"},
        )
    if allowed_paths_snapshot_base_sha != current_base_sha:
        return MainDriftDecision("hard_stop", "stale_allowed_paths_snapshot", None, {}, {})
    if semantic_ambiguity:
        return MainDriftDecision("hard_stop", "semantic_ambiguity", None, {}, {})
    if any(
        not any(
            path == allowed or (allowed.endswith("/") and path.startswith(allowed))
            for allowed in allowed_paths
        )
        for path in set(latest_main_net_diff)
    ):
        return MainDriftDecision("hard_stop", "allowed_paths_conflict", None, {}, {})
    if (
        not isinstance(drift_rebind_attempts, int)
        or isinstance(drift_rebind_attempts, bool)
        or drift_rebind_attempts < 0
    ):
        return MainDriftDecision("hard_stop", "main_drift_context_invalid", None, {}, {})
    if drift_rebind_attempts >= max_drift_rebind_attempts:
        # Deterministic, machine-decidable stop -- NOT a semantic-judgment
        # failure. The caller must persist drift_rebind_attempts (e.g. in
        # LOOP_STATE) across cycles for this bound to be effective; this
        # function itself performs no persistence (no side effects).
        return MainDriftDecision(
            "hard_stop", "concurrent_base_churn_budget_exhausted", None, {}, {}
        )

    common = {
        "evidence_epoch": {
            "base_sha": current_base_sha,
            "epoch": 1,
            "owner": owner,
            "implementation_iteration_delta": 0,
            "drift_rebind_attempts": drift_rebind_attempts + 1,
            "max_drift_rebind_attempts": max_drift_rebind_attempts,
        },
        "reverify": {"snapshot": True, "ci": True, "review": True},
        "reusable_evidence": {"snapshot": None, "ci": None, "review": None},
    }
    if behind_fast_path_eligible:
        return MainDriftDecision("behind_fast_path", "main_drift_scope_clean", **common)
    return MainDriftDecision("scope_clean_reconciliation", "main_drift_scope_clean", **common)
