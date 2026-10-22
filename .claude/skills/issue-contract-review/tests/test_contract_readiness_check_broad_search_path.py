"""
tests/test_contract_readiness_check_broad_search_path.py

Issue #1406 (PR #1412 review, Blocker 2): `_PREFLIGHT_CATEGORY_TO_READINESS`
must map `broad_search_path_unbounded` to `needs_fix` (body-author-fixable
VC-scope problem), not fall through to the default `human_judgment`. This
mapping is what lets `reviewer_claim_replay.py`'s
`broad_search_path_unbounded` taxonomy entry receive a real readiness error
(with the correct producer-shape `source_payload`) instead of a
hand-rolled fixture that hides the producer/consumer mismatch.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_CRC_PATH = _SCRIPTS_DIR / "contract_readiness_check.py"

spec = importlib.util.spec_from_file_location(
    "contract_readiness_check_broad_search_path", _CRC_PATH
)
assert spec is not None and spec.loader is not None
_crc_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_crc_mod)  # type: ignore[union-attr]


def _broad_path_preflight_result(*, decision: str = "blocked", scope_class: str = "baseline_fail_expected") -> dict:
    return {
        "schema": "baseline_vc_preflight/v1",
        "source": {"kind": "body_file", "body_sha256": "sha256:body-a"},
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "line": 30,
                "raw_command": 'rg -n "pattern" .',
                "command_hash": "sha256:command-broad-path",
                "classification": "blocked",
                "category": "broad_search_path_unbounded",
                "decision": decision,
                "scope_class": scope_class,
                "confidence": "high",
            }
        ],
        "errors": [],
    }


def test_broad_search_path_unbounded_mapped_to_needs_fix():
    assert (
        _crc_mod._PREFLIGHT_CATEGORY_TO_READINESS.get("broad_search_path_unbounded")
        == "needs_fix"
    )


def test_map_preflight_result_to_errors_aggregates_needs_fix():
    errors, aggregate = _crc_mod.map_preflight_result_to_errors(
        _broad_path_preflight_result()
    )
    assert aggregate == "needs_fix"
    assert len(errors) == 1
    error = errors[0]
    assert error["rule_id"] == "VCP_BROAD_SEARCH_PATH_UN"
    assert error["source_check"] == "baseline_vc_preflight"
    assert error["category"] == "broad_search_path_unbounded"


def test_map_preflight_result_to_errors_includes_producer_source_payload():
    """Blocker 2: the generated error's source_payload must carry the exact
    producer-shape fields reviewer_claim_replay.py's strict matcher checks."""
    errors, _ = _crc_mod.map_preflight_result_to_errors(_broad_path_preflight_result())
    payload = errors[0]["source_payload"]
    assert payload["classification"] == "blocked"
    assert payload["category"] == "broad_search_path_unbounded"
    assert payload["decision"] == "blocked"
    assert payload["scope_class"] == "baseline_fail_expected"


def test_broad_search_path_unbounded_human_judgment_decision_not_downgraded():
    """decision == human_judgment must stay human_judgment (never collapse
    to needs_fix), matching the MUST NOT collapse invariant documented on
    map_preflight_result_to_errors()."""
    preflight = _broad_path_preflight_result()
    preflight["results"][0]["decision"] = "human_judgment"
    errors, aggregate = _crc_mod.map_preflight_result_to_errors(preflight)
    assert aggregate == "human_judgment"
    assert len(errors) == 1
