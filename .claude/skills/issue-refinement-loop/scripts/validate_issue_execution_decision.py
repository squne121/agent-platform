#!/usr/bin/env python3
"""
validate_issue_execution_decision.py

Standalone, import-safe normative validator for ISSUE_EXECUTION_DECISION_V1
(#1677 AC10/AC11/AC12/AC13).

This module is the canonical authority for both:
  - validate_schema(): closed JSON Schema validation against
    schemas/issue_execution_decision_v1.schema.json (required keys, types,
    enums, additionalProperties:false, format:date-time when the installed
    jsonschema supports it).
  - validate_semantics(): cross-field graph invariants the JSON Schema alone
    cannot express (ordering, uniqueness, endpoint existence, self-edges,
    conflicting parallel edges, depends_on cycles, target/predecessor/state/
    completeness agreement, identity/node/digest cross-consistency).

validate_issue_execution_decision() runs validate_schema() first and only
runs validate_semantics() if the schema passes (schema-first, per PR #1767
owner review P1-1) -- callers that need the previous combined-check
signature can keep calling this function unchanged.

Consumers (producer/preflight, LOOP_STATE builder, handoff/termination
report producer, downstream consumers such as decide_next_loop_action.py)
MUST import this module rather than re-deriving the invariants, and MUST
fail closed if the import itself fails (#1677 AC12).

This module intentionally has NO dependency on plan_refinement_loop.py (or
any other sibling script) to avoid import cycles, since plan_refinement_loop
imports FROM this module.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

try:
    import jsonschema as _jsonschema

    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive
    _jsonschema = None
    _JSONSCHEMA_AVAILABLE = False


_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "issue_execution_decision_v1.schema.json"
_schema_cache: dict[str, Any] | None = None


def _load_schema() -> dict[str, Any]:
    global _schema_cache
    if _schema_cache is None:
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            _schema_cache = json.load(f)
    return _schema_cache


def _sha256(text: str) -> str:
    """Compute SHA256 of text (mirrors plan_refinement_loop._sha256)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(obj: Any) -> str:
    """Canonical JSON (mirrors plan_refinement_loop._canonical_json)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha256_prefixed(text: str) -> str:
    return "sha256:" + _sha256(text)


ISSUE_EXECUTION_DECISION_SCHEMA_VERSION = "ISSUE_EXECUTION_DECISION_V1"

ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY = {
    "semantic_reclassification": "forbidden",
    "freshness_validation": "required",
    "stale_action": "rerun_issue_refinement",
}

_VALID_RELATION_TYPES = frozenset({"depends_on", "duplicate", "absorb", "supersedes", "coordinates"})
_VALID_EXECUTION_STATES = frozenset({"selected", "deferred", "blocked", "duplicate"})
_DUPLICATE_LIKE_RELATIONS = frozenset({"duplicate", "absorb"})


def validate_schema(decision: Any) -> list[str]:
    """
    Validate `decision` against the closed JSON Schema
    (schemas/issue_execution_decision_v1.schema.json).

    Returns a list of violation strings (empty == valid). Never raises: a
    missing jsonschema installation or unreadable schema file is itself
    reported as a violation (fail-closed for callers that only check
    `if violations:`).
    """
    if not _JSONSCHEMA_AVAILABLE:
        return ["jsonschema_not_available"]
    try:
        schema = _load_schema()
        validator_cls = _jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        format_checker = _jsonschema.FormatChecker()
        validator = validator_cls(schema, format_checker=format_checker)
        violations = []
        for error in validator.iter_errors(decision):
            path_str = "/".join(str(p) for p in error.path) if error.path else "<root>"
            violations.append(f"schema_violation:{path_str}:{error.message}")
        return violations
    except Exception as exc:
        return [f"schema_validation_error:{type(exc).__name__}:{str(exc)[:500]}"]


def _is_valid_issue_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_semantics(decision: dict[str, Any]) -> list[str]:
    """
    Normative semantic validator for ISSUE_EXECUTION_DECISION_V1 (#1677 AC11,
    hardened per PR #1767 owner review P0-3.4/P1-1).

    Returns a list of violation reason strings (empty == valid). Checks
    cross-field / graph invariants that the closed JSON Schema
    (schemas/issue_execution_decision_v1.schema.json) cannot express:
    ordering, node/relation uniqueness, endpoint existence, self-edges,
    conflicting parallel edges, depends_on cycles, target/predecessor
    agreement, state semantics, completeness gating, identity/node/digest
    cross-consistency. Never raises for malformed/type-invalid input --
    schema-invalid shapes are reported as violations, not exceptions (P1-1).

    Callers (planner/preflight, build_loop_state.py, handoff parser,
    termination report, downstream consumers) MUST use this function as the
    single semantic-validation authority instead of re-deriving invariants.
    """
    try:
        return _validate_semantics_impl(decision)
    except Exception as exc:  # pragma: no cover - defensive fail-closed net
        # P1-1: a type-invalid instance (unhashable issue_number, mixed
        # int/string endpoints, malformed predecessor list, etc.) must
        # surface as a violation, never as an uncaught exception in a
        # downstream consumer that calls this as the sole validation gate.
        return [f"internal_validator_error:{type(exc).__name__}:{exc}"]


def _validate_semantics_impl(decision: dict[str, Any]) -> list[str]:
    violations: list[str] = []

    if not isinstance(decision, dict):
        return ["not_a_mapping"]

    schema_version = decision.get("schema_version")
    identity = decision.get("identity")
    nodes = decision.get("nodes")
    relations = decision.get("relations")
    execution = decision.get("execution")
    completeness = decision.get("completeness")
    downstream_policy = decision.get("downstream_policy")

    if schema_version != ISSUE_EXECUTION_DECISION_SCHEMA_VERSION:
        violations.append(f"unknown_schema_version:{schema_version}")

    if not isinstance(nodes, list) or not isinstance(relations, list):
        return violations + ["missing_nodes_or_relations"]
    if not isinstance(execution, dict) or not isinstance(completeness, dict):
        return violations + ["missing_execution_or_completeness"]
    if not isinstance(identity, dict):
        violations.append("missing_identity")
        identity = {}
    if not isinstance(downstream_policy, dict):
        violations.append("missing_downstream_policy")
        downstream_policy = {}

    # --- node uniqueness + ordering (type-safe: reject unhashable/malformed
    # issue_number before it ever reaches set()/sorted()) ---
    node_numbers: list[int] = []
    node_body_sha_by_number: dict[int, Any] = {}
    for n in nodes:
        if not isinstance(n, dict) or not _is_valid_issue_number(n.get("issue_number")):
            violations.append("malformed_node")
            continue
        num = n["issue_number"]
        node_numbers.append(num)
        node_body_sha_by_number[num] = n.get("body_sha256")

    if len(node_numbers) != len(set(node_numbers)):
        violations.append("duplicate_node")
    if node_numbers != sorted(node_numbers):
        violations.append("nodes_not_sorted")

    node_set = set(node_numbers)

    # --- relation ordering + uniqueness + endpoint + self-edge (type-safe) ---
    relation_tuples = []
    depends_on_edges: list[tuple[int, int]] = []
    seen_unordered_pairs: dict[frozenset, set[str]] = {}
    # Relation types that are mutually exclusive on the same unordered node
    # pair -- a pair cannot simultaneously be e.g. duplicate AND depends_on,
    # or duplicate AND supersedes (PR #1767 owner review, P1-1).
    _mutually_exclusive_relations = frozenset({"depends_on", "duplicate", "absorb", "supersedes"})

    for r in relations:
        if not isinstance(r, dict):
            violations.append("malformed_relation")
            continue
        src = r.get("source_issue_number")
        tgt = r.get("target_issue_number")
        rtype = r.get("relation_type")

        if not _is_valid_issue_number(src) or not _is_valid_issue_number(tgt):
            violations.append("malformed_relation_endpoint")
            continue
        if rtype not in _VALID_RELATION_TYPES:
            violations.append(f"unknown_relation_type:{rtype}")
            continue
        if src == tgt:
            violations.append(f"self_edge:{src}")
        if src not in node_set or tgt not in node_set:
            violations.append(f"unknown_endpoint:{src}->{tgt}")

        relation_tuples.append((src, tgt, rtype))
        if rtype == "depends_on":
            depends_on_edges.append((src, tgt))

        pair = frozenset({src, tgt})
        seen_unordered_pairs.setdefault(pair, set()).add(rtype)

    if len(relation_tuples) != len(set(relation_tuples)):
        violations.append("duplicate_relation")
    if relation_tuples != sorted(relation_tuples):
        violations.append("relations_not_sorted")

    for pair, rtypes in seen_unordered_pairs.items():
        exclusive_hits = rtypes & _mutually_exclusive_relations
        if len(exclusive_hits) > 1:
            violations.append(f"conflicting_parallel_edge:{sorted(pair)}:{sorted(exclusive_hits)}")

    # --- depends_on cycle detection (general graph, not just pairwise) ---
    adjacency: dict[int, list[int]] = {}
    for src, tgt in depends_on_edges:
        adjacency.setdefault(src, []).append(tgt)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in node_set}

    def _has_cycle(start: int) -> bool:
        stack = [(start, iter(adjacency.get(start, [])))]
        color[start] = GRAY
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if color.get(nxt, WHITE) == GRAY:
                    return True
                if color.get(nxt, WHITE) == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(adjacency.get(nxt, []))))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()
        return False

    for n in list(node_set):
        if color.get(n, WHITE) == WHITE:
            if _has_cycle(n):
                violations.append("depends_on_cycle")
                break

    # --- execution / predecessor agreement ---
    target = execution.get("target_issue_number")
    predecessors = execution.get("predecessors")
    state = execution.get("state")
    defer_reason = execution.get("defer_reason")

    if not _is_valid_issue_number(target):
        violations.append("malformed_execution_target")
    elif target not in node_set:
        violations.append("execution_target_not_in_nodes")

    # Convention: relation(source_issue_number=X, target_issue_number=Y,
    # relation_type="depends_on") reads as "X depends_on Y" (Y must complete
    # before X proceeds). Predecessors of execution.target_issue_number are
    # therefore the *targets* of depends_on edges whose *source* is the
    # execution target (i.e. what the target issue itself depends on).
    expected_predecessors = sorted({tgt for (src, tgt) in depends_on_edges if src == target})
    predecessors_valid = isinstance(predecessors, list) and all(_is_valid_issue_number(p) for p in predecessors)
    if not predecessors_valid:
        violations.append("malformed_predecessors")
    elif sorted(set(predecessors)) != expected_predecessors:
        violations.append("predecessors_do_not_match_depends_on_edges")

    if state not in _VALID_EXECUTION_STATES:
        violations.append(f"unknown_execution_state:{state}")

    issues_complete = completeness.get("issues_complete")
    dependencies_complete = completeness.get("dependencies_complete")
    unresolved_references = completeness.get("unresolved_references")
    incomplete = issues_complete is not True or dependencies_complete is not True or bool(unresolved_references)

    # P1-1: unresolved_references node correspondence -- every referenced
    # issue number must at least be a known node (otherwise it references
    # nothing the graph can act on).
    if isinstance(unresolved_references, list):
        for ref in unresolved_references:
            if _is_valid_issue_number(ref) and ref not in node_set and ref != target:
                violations.append(f"unresolved_reference_not_in_nodes:{ref}")

    if predecessors_valid and state == "selected" and predecessors:
        violations.append("selected_state_with_predecessors")
    if state == "selected" and incomplete:
        violations.append("selected_state_with_incomplete_evidence")

    if state in ("deferred", "blocked") and not defer_reason:
        violations.append(f"{state}_state_missing_defer_reason")

    # PR #1767 owner review (P1-1): 'blocked' without any predecessor is
    # meaningless under this schema (predecessors is the only blocking
    # mechanism modeled) -- must use 'deferred' instead for non-predecessor
    # blockers.
    if state == "blocked" and predecessors_valid and not predecessors:
        violations.append("blocked_state_without_predecessors")

    # PR #1767 owner review (P1-1): 'deferred' must not carry an unresolved
    # depends_on predecessor -- that combination means the target IS
    # actionably blocked on something, which is 'blocked', not 'deferred'.
    if state == "deferred" and predecessors_valid and predecessors:
        violations.append("deferred_state_with_depends_on_predecessor")

    if state == "duplicate":
        has_duplicate_relation = any(
            rtype in _DUPLICATE_LIKE_RELATIONS and (src == target or tgt == target)
            for (src, tgt, rtype) in relation_tuples
        )
        if not has_duplicate_relation:
            violations.append("duplicate_state_without_duplicate_relation")

    # --- downstream_policy semantic check (defense in depth; schema also
    # enforces this via const, but the standalone semantic validator should
    # not silently accept a policy-shaped dict with the wrong values if ever
    # called before schema validation) ---
    if downstream_policy != ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY:
        violations.append("downstream_policy_mismatch")

    # --- identity / node / digest cross-consistency (PR #1767 owner review,
    # P0-3.4) ---
    identity_target = identity.get("target_issue_number")
    identity_body_sha = identity.get("target_body_sha256")
    declared_digest = identity.get("collection_digest")

    if _is_valid_issue_number(identity_target) and _is_valid_issue_number(target):
        if identity_target != target:
            violations.append("identity_target_execution_target_mismatch")
    if _is_valid_issue_number(identity_target) and identity_target not in node_set:
        violations.append("identity_target_not_in_nodes")
    if (
        _is_valid_issue_number(identity_target)
        and identity_target in node_body_sha_by_number
        and node_body_sha_by_number[identity_target] != identity_body_sha
    ):
        violations.append("identity_target_body_sha256_node_mismatch")

    recomputed_digest = _sha256_prefixed(
        _canonical_json(
            {
                "nodes": nodes,
                "relations": relations,
                "execution": execution,
                "completeness": completeness,
                "downstream_policy": downstream_policy,
            }
        )
    )
    if declared_digest != recomputed_digest:
        violations.append("collection_digest_mismatch")

    return violations


# ---------------------------------------------------------------------------
# Combined schema-first validator (backward-compatible entry point; PR #1767
# owner review P1-1: semantics are only evaluated once the schema itself is
# closed-valid, since semantic checks assume well-typed input).
# ---------------------------------------------------------------------------


def validate_issue_execution_decision(decision: Any) -> list[str]:
    """
    Schema-first combined validator: validate_schema() then, only if the
    schema passes, validate_semantics(). Returns a list of violation strings
    (empty == valid). This is the single entry point producer/LOOP_STATE
    builder/handoff-termination-report producer/downstream consumers should
    call (#1677 AC12).
    """
    schema_violations = validate_schema(decision)
    if schema_violations:
        return schema_violations
    return validate_semantics(decision)


# ---------------------------------------------------------------------------
# Legacy adapter + migration envelope (#1677 AC13)
# ---------------------------------------------------------------------------

_LEGACY_TARGET_STATE_MAP = {
    "planned": "deferred",
    "selected": "selected",
    "deferred": "deferred",
    "blocked": "blocked",
    "duplicate": "duplicate",
}

LEGACY_SCHEMA_IDENTIFIERS = [
    "graph.nodes/graph.edges",
    "execution.target_state/predecessor_issue_numbers/reason_codes",
]

MIGRATION_PHASES = frozenset({"dual_write", "equivalence", "dual_read", "new_authoritative", "legacy_removed"})


def adapt_legacy_graph_to_v1(
    legacy: dict[str, Any],
    *,
    target_issue_number: int,
    target_body_sha256: str,
    generated_at: str,
) -> dict[str, Any]:
    """
    Adapt the legacy `graph.nodes/graph.edges` +
    `execution.target_state/predecessor_issue_numbers/reason_codes` shape
    into canonical ISSUE_EXECUTION_DECISION_V1 (#1677 Migration/compatibility
    section, AC13). The legacy shape is accepted as an adapter INPUT only;
    the canonical output always normalizes to V1 -- this function never
    returns legacy-shaped output.

    Mapping (per Issue #1677's frozen contract):
      edges[].relation            -> relations[].relation_type
      execution.target_state      -> execution.state ('planned' -> 'deferred')
      predecessor_issue_numbers   -> execution.predecessors
      reason_codes                -> execution.defer_reason (joined)
    """
    graph = legacy.get("graph") or {}
    legacy_nodes = graph.get("nodes") or []
    legacy_edges = graph.get("edges") or []
    legacy_execution = legacy.get("execution") or {}

    nodes = sorted(
        (
            {"issue_number": n["issue_number"], "body_sha256": n["body_sha256"]}
            for n in legacy_nodes
            if isinstance(n, dict) and "issue_number" in n and "body_sha256" in n
        ),
        key=lambda n: n["issue_number"],
    )

    relations = sorted(
        (
            {
                "source_issue_number": e["source_issue_number"],
                "target_issue_number": e["target_issue_number"],
                "relation_type": e["relation"],
                "evidence": e.get("evidence") or ["legacy_adapter:no_evidence_recorded"],
            }
            for e in legacy_edges
            if isinstance(e, dict)
            and "source_issue_number" in e
            and "target_issue_number" in e
            and e.get("relation") in _VALID_RELATION_TYPES
        ),
        key=lambda r: (r["source_issue_number"], r["target_issue_number"], r["relation_type"]),
    )

    legacy_target_state = legacy_execution.get("target_state")
    state = _LEGACY_TARGET_STATE_MAP.get(legacy_target_state, "deferred")
    predecessors = sorted(set(legacy_execution.get("predecessor_issue_numbers") or []))
    # A mapped 'deferred' with actual pending predecessors is actionably
    # blocked, not merely deferred (must agree with validate_semantics'
    # deferred_state_with_depends_on_predecessor invariant, PR #1767 owner
    # review P1-1) -- promote to 'blocked' rather than emit an artifact the
    # shared validator itself would reject.
    if state == "deferred" and predecessors:
        state = "blocked"
    reason_codes = legacy_execution.get("reason_codes") or []
    defer_reason = "; ".join(reason_codes) if reason_codes else None
    if state != "selected" and not defer_reason:
        defer_reason = "legacy_adapter: no reason_codes recorded"

    nodes_by_number = {n["issue_number"]: n for n in nodes}
    if target_issue_number not in nodes_by_number:
        nodes = sorted(
            nodes + [{"issue_number": target_issue_number, "body_sha256": target_body_sha256}],
            key=lambda n: n["issue_number"],
        )

    completeness = {
        "issues_complete": bool(legacy.get("issues_complete", state == "selected")),
        "dependencies_complete": bool(legacy.get("dependencies_complete", state == "selected")),
        "unresolved_references": sorted(set(legacy.get("unresolved_references") or [])),
    }

    execution = {
        "state": state,
        "target_issue_number": target_issue_number,
        "predecessors": predecessors,
        "defer_reason": defer_reason,
    }

    collection_digest = _sha256_prefixed(
        _canonical_json(
            {
                "nodes": nodes,
                "relations": relations,
                "execution": execution,
                "completeness": completeness,
                "downstream_policy": ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY,
            }
        )
    )

    return {
        "schema_version": ISSUE_EXECUTION_DECISION_SCHEMA_VERSION,
        "identity": {
            "target_issue_number": target_issue_number,
            "target_body_sha256": target_body_sha256,
            "generated_at": generated_at,
            "collection_digest": collection_digest,
        },
        "nodes": nodes,
        "relations": relations,
        "execution": execution,
        "downstream_policy": dict(ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY),
        "completeness": completeness,
    }


def compute_equivalence(legacy_digest: "str | None", new_digest: str) -> str:
    """
    Compare a legacy-adapter-derived digest against the canonical V1 digest.

    Returns "equivalent" | "not_equivalent" | "not_applicable" (when there is
    no legacy digest to compare, e.g. a fresh V1-only decision).
    """
    if legacy_digest is None:
        return "not_applicable"
    return "equivalent" if legacy_digest == new_digest else "not_equivalent"


def build_migration_envelope(
    *,
    phase: str,
    legacy_digest: "str | None",
    new_digest: str,
    producer_version: str,
    consumer_capability: list[str],
) -> dict[str, Any]:
    """Build the additive `migration` envelope block (#1677 AC13)."""
    return {
        "phase": phase,
        "legacy_digest": legacy_digest,
        "new_digest": new_digest,
        "equivalence_result": compute_equivalence(legacy_digest, new_digest),
        "producer_version": producer_version,
        "consumer_capability": list(consumer_capability),
    }


def validate_migration(envelope: Any) -> list[str]:
    """
    Semantic validation for the `migration` envelope (#1677 AC13).

    Returns a list of violation strings (empty == valid). Fail-closed rule:
    when phase == "equivalence", legacy_digest and new_digest MUST match
    (equivalence_result must be "equivalent"); a mismatch is rejected rather
    than silently accepted, since "equivalence" phase means legacy and V1
    are claimed to describe the identical decision.
    """
    if not isinstance(envelope, dict):
        return ["migration_envelope_not_a_mapping"]

    violations: list[str] = []
    phase = envelope.get("phase")
    if phase not in MIGRATION_PHASES:
        violations.append(f"unknown_migration_phase:{phase}")

    legacy_digest = envelope.get("legacy_digest")
    new_digest = envelope.get("new_digest")
    equivalence_result = envelope.get("equivalence_result")

    recomputed = compute_equivalence(legacy_digest, new_digest)
    if equivalence_result != recomputed:
        violations.append(f"equivalence_result_mismatch:declared={equivalence_result}:recomputed={recomputed}")

    if phase == "equivalence" and recomputed != "equivalent":
        violations.append("equivalence_phase_digest_mismatch_fail_closed")

    return violations


def build_provenance(
    *,
    scope_rollup_result: "dict[str, Any] | None",
    semantic_decision_sha256: str,
    artifact_sha256: str,
    policy_version: str = "1.0.0",
    producer_name: str = "plan_refinement_loop",
    producer_version: str = "1.0.0",
    collector_name: str = "run_refinement_preflight",
    collector_version: str = "1.0.0",
    canonicalization_id: str = "legacy_v1_canonical_json",
) -> dict[str, Any]:
    """Build the additive `provenance` block (#1677 AC10)."""
    source_manifest_sha256 = (
        _sha256_prefixed(_canonical_json(scope_rollup_result)) if isinstance(scope_rollup_result, dict) else None
    )
    return {
        "policy_version": policy_version,
        "producer": {"name": producer_name, "version": producer_version},
        "collector": {"name": collector_name, "version": collector_version},
        "canonicalization_id": canonicalization_id,
        "digests": {
            "source_manifest_sha256": source_manifest_sha256,
            "semantic_decision_sha256": semantic_decision_sha256,
            "artifact_sha256": artifact_sha256,
        },
        "legacy_compatibility": {
            "legacy_schema_identifiers": list(LEGACY_SCHEMA_IDENTIFIERS),
            "supported_consumer_versions": [ISSUE_EXECUTION_DECISION_SCHEMA_VERSION],
        },
    }


# ---------------------------------------------------------------------------
# Handoff ref projection (#1677 AC5/AC12 Scope Delta)
# ---------------------------------------------------------------------------


def project_issue_execution_decision_ref(
    issue_execution_decision: "dict[str, Any] | None",
) -> "dict[str, Any] | None":
    """
    Project a full ISSUE_EXECUTION_DECISION_V1 down to the small
    'issue_execution_decision_ref' reference embedded in
    LOOP_HANDOFF_RESULT_V1 (#1677 AC5). Carries only what downstream
    freshness validation needs: schema_version, target_issue_number, and
    collection_digest -- the same digest that reached LOOP_STATE_V1 via
    build_loop_state().

    Returns None when issue_execution_decision is absent/malformed (the
    caller then omits issue_execution_decision_ref from the handoff, rather
    than emitting a partial/misleading reference).

    Canonical home: this module (validate_issue_execution_decision.py), so
    every consumer that needs to build the ref (build_loop_state.py,
    render_termination_report.py) shares one implementation (#1677 AC12).
    """
    if not isinstance(issue_execution_decision, dict):
        return None
    identity = issue_execution_decision.get("identity")
    if not isinstance(identity, dict):
        return None
    target_issue_number = identity.get("target_issue_number")
    collection_digest = identity.get("collection_digest")
    schema_version = issue_execution_decision.get("schema_version")
    if not isinstance(target_issue_number, int) or not isinstance(collection_digest, str):
        return None
    if schema_version != ISSUE_EXECUTION_DECISION_SCHEMA_VERSION:
        return None
    return {
        "schema_version": schema_version,
        "target_issue_number": target_issue_number,
        "collection_digest": collection_digest,
    }
