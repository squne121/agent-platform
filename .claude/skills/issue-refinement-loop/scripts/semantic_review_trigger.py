#!/usr/bin/env python3
"""Deterministic applicability classifier for the Step 2.5 semantic design
review lane (Issue #2296).

This module answers exactly one question: given a fixed set of EXPLICIT
signals about the current review cycle, is a semantic (model-based) design
review APPLICABLE right now?  It does not compare the current Issue body to
any previous body -- there is no ``--previous-body-file`` input and no
before/after diff.  This is a deliberate, explicit renaming from the earlier
``semantic_review_required`` name: the classifier only tells you whether
semantic review APPLIES to the current signals, it does not (and cannot,
without a comparison baseline) prove that the body has materially CHANGED
(#2296 Design Decision Note, P0-2).

``semantic_review_applicable`` is ``True`` iff at least one of the following
explicit signals is true / non-zero / non-empty:

- ``user_requested``
- ``semantic_rewrite_requested``
- ``checker_gap_count`` (> 0)
- ``heuristic_concern_count`` (> 0)
- ``severity_tagged_anchor_findings`` (non-empty)
- ``owner_decision_conflict``
- ``cross_contract_change.schema`` / ``.protocol`` / ``.orchestration``

All-false / all-zero / all-empty -> ``semantic_review_applicable = False``
(skip; the Step 2.5 lane is not entered).

Input parsing is strict (#2296 fix_delta iteration 6, P1-3): boolean
fields must be actual JSON booleans (a string ``"false"``/``"true"`` is
rejected rather than being coerced by ``bool(...)``, which would otherwise
evaluate any non-empty string -- including the literal string ``"false"``
-- as truthy), count fields must be non-negative integers, and unknown
top-level / ``cross_contract_change`` keys are rejected. ``ValueError`` is
raised for any of these violations (fail-closed, no silent coercion).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "SEMANTIC_REVIEW_TRIGGER_RESULT_V1"

_CROSS_CONTRACT_CHANGE_KEYS = {"schema", "protocol", "orchestration"}
_TOP_LEVEL_KEYS = {
    "user_requested",
    "semantic_rewrite_requested",
    "checker_gap_count",
    "heuristic_concern_count",
    "severity_tagged_anchor_findings",
    "owner_decision_conflict",
    "cross_contract_change",
}


def _require_bool(raw: "dict[str, Any]", key: str, *, default: bool = False) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key!r} must be a JSON boolean, got {value!r}")
    return value


def _require_nonneg_int(raw: "dict[str, Any]", key: str, *, default: int = 0) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key!r} must be a JSON integer, got {value!r}")
    if value < 0:
        raise ValueError(f"{key!r} must be >= 0, got {value!r}")
    return value


def _require_str_list(raw: "dict[str, Any]", key: str) -> tuple:
    if key not in raw:
        return ()
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key!r} must be a JSON array of strings, got {value!r}")
    return tuple(value)


def _reject_unknown_keys(raw: "dict[str, Any]", allowed: "set[str]", *, context: str) -> None:
    extra = set(raw.keys()) - allowed
    if extra:
        raise ValueError(f"unknown key(s) in {context}: {sorted(extra)}")


@dataclass(frozen=True)
class CrossContractChange:
    schema: bool = False
    protocol: bool = False
    orchestration: bool = False

    def any_true(self) -> bool:
        return bool(self.schema or self.protocol or self.orchestration)

    @classmethod
    def from_dict(cls, raw: "dict[str, Any] | None") -> "CrossContractChange":
        raw = raw or {}
        if not isinstance(raw, dict):
            raise ValueError(f"cross_contract_change must be a JSON object, got {raw!r}")
        _reject_unknown_keys(raw, _CROSS_CONTRACT_CHANGE_KEYS, context="cross_contract_change")
        return cls(
            schema=_require_bool(raw, "schema"),
            protocol=_require_bool(raw, "protocol"),
            orchestration=_require_bool(raw, "orchestration"),
        )


@dataclass(frozen=True)
class SemanticReviewTriggerInput:
    user_requested: bool = False
    semantic_rewrite_requested: bool = False
    checker_gap_count: int = 0
    heuristic_concern_count: int = 0
    severity_tagged_anchor_findings: tuple = field(default_factory=tuple)
    owner_decision_conflict: bool = False
    cross_contract_change: CrossContractChange = field(default_factory=CrossContractChange)

    @classmethod
    def from_dict(cls, raw: "dict[str, Any]") -> "SemanticReviewTriggerInput":
        if not isinstance(raw, dict):
            raise ValueError(f"trigger input must be a JSON object, got {raw!r}")
        _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, context="trigger input")
        return cls(
            user_requested=_require_bool(raw, "user_requested"),
            semantic_rewrite_requested=_require_bool(raw, "semantic_rewrite_requested"),
            checker_gap_count=_require_nonneg_int(raw, "checker_gap_count"),
            heuristic_concern_count=_require_nonneg_int(raw, "heuristic_concern_count"),
            severity_tagged_anchor_findings=_require_str_list(
                raw, "severity_tagged_anchor_findings"
            ),
            owner_decision_conflict=_require_bool(raw, "owner_decision_conflict"),
            cross_contract_change=CrossContractChange.from_dict(
                raw.get("cross_contract_change")
            ),
        )


def _signal_breakdown(inp: SemanticReviewTriggerInput) -> "dict[str, bool]":
    return {
        "user_requested": inp.user_requested,
        "semantic_rewrite_requested": inp.semantic_rewrite_requested,
        "checker_gap_count": inp.checker_gap_count > 0,
        "heuristic_concern_count": inp.heuristic_concern_count > 0,
        "severity_tagged_anchor_findings": len(inp.severity_tagged_anchor_findings) > 0,
        "owner_decision_conflict": inp.owner_decision_conflict,
        "cross_contract_change": inp.cross_contract_change.any_true(),
    }


def evaluate_semantic_review_applicable(raw: "dict[str, Any]") -> "dict[str, Any]":
    """Pure function: explicit signals in, SEMANTIC_REVIEW_TRIGGER_RESULT_V1 out."""
    inp = SemanticReviewTriggerInput.from_dict(raw)
    breakdown = _signal_breakdown(inp)
    applicable = any(breakdown.values())
    triggered_by = sorted(name for name, value in breakdown.items() if value)
    return {
        "schema": SCHEMA,
        "semantic_review_applicable": applicable,
        "triggered_by": triggered_by,
        "signal_breakdown": breakdown,
    }


def build_semantic_review_trigger_input(
    *,
    user_requested: bool = False,
    semantic_rewrite_requested: bool = False,
    deterministic_checker_gaps: "list[Any] | None" = None,
    heuristic_concerns: "list[Any] | None" = None,
    anchor_comment_bodies: "list[str] | None" = None,
    owner_decision_conflict: bool = False,
    cross_contract_change: "dict[str, bool] | None" = None,
) -> "dict[str, Any]":
    """Canonical producer of ``SEMANTIC_REVIEW_TRIGGER_INPUT`` (#2296 P1-3).

    Constructs the trigger input strictly from already-computed, trusted
    artifacts -- the Step 2 deterministic checker's gap list, a heuristic
    concern list, raw anchor comment bodies (severity-tagged headings are
    extracted from these via ``scope_signal_delta.extract_severity_tags()``,
    kept deliberately separate from ``extract_directive_markers()``, see
    P1-4), and an explicit cross-contract-change flag dict -- never from
    ad-hoc hand-authored CLI JSON. Callers should prefer this function over
    constructing the ``--input-json`` payload by hand.
    """
    severity_tagged_anchor_findings: "list[str]" = []
    if anchor_comment_bodies:
        try:
            from scope_signal_delta import extract_severity_tags
        except ImportError:
            extract_severity_tags = None
        if extract_severity_tags is not None:
            for body in anchor_comment_bodies:
                severity_tagged_anchor_findings.extend(extract_severity_tags(body))

    return {
        "user_requested": bool(user_requested),
        "semantic_rewrite_requested": bool(semantic_rewrite_requested),
        "checker_gap_count": len(deterministic_checker_gaps or []),
        "heuristic_concern_count": len(heuristic_concerns or []),
        "severity_tagged_anchor_findings": sorted(set(severity_tagged_anchor_findings)),
        "owner_decision_conflict": bool(owner_decision_conflict),
        "cross_contract_change": dict(cross_contract_change or {}),
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", help="JSON file with explicit signals")
    parser.add_argument(
        "--input-json", help="Inline JSON string with explicit signals (overrides --input-file)"
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.input_json:
        raw = json.loads(args.input_json)
    elif args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        raw = json.load(sys.stdin)
    result = evaluate_semantic_review_applicable(raw)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
