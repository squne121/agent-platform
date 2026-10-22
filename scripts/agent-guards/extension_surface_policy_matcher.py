#!/usr/bin/env python3
"""
extension_surface_policy_matcher.py

Shared evaluator (Issue #2290) that reads
``docs/dev/extension-surface-runtime-policy.yaml`` (schema_version v2) and
determines whether an Issue's declared ``## Allowed Paths`` are syntactic
candidates for one or more of the policy's risk-trigger rules.

Scope (Issue #2290 In Scope / Out of Scope -- see Issue body):

- This module performs **candidate discovery only** -- a deterministic,
  syntactic comparison between the declared Allowed Paths and the policy's
  ``selectors[].source_scope: project`` ``path_globs``. It never performs a
  semantic diff judgment of actual PR changes (that consumer is explicitly
  Out of Scope for Issue #2290; see the ``pr-review-judge`` / impl-review-loop
  follow-up referenced in the Issue body).
- Exact Allowed Path entries (no ``*`` character) are matched directly
  against a rule's ``path_globs`` using the same matcher-v2 segment grammar
  as ``scripts/agent-guards/changed_file_matcher.py``'s ``AllowedPathsMatcher``
  (``*`` = one path segment, ``**`` = zero or more segments).
- Wildcard Allowed Path entries are intentionally **not** run through a full
  glob-intersection engine. Instead this module performs a conservative
  literal/static-prefix comparison: it is not permissible to design this in
  a way that risks missing a real risk-trigger candidate (false negative);
  over-flagging a candidate that a human later dismisses (false positive) is
  the accepted, safe side of the trade-off.

Consumers (``.claude/skills/review-issue/scripts/check_issue_contract.py`` and
``.claude/skills/issue-contract-review/scripts/contract_readiness_check.py``)
MUST dynamically load this module via ``importlib`` -- mirroring the existing
pattern in
``.claude/skills/issue-contract-review/scripts/declared_path_overlap.py``,
which dynamically loads ``scripts/agent-guards/changed_file_matcher.py`` the
same way -- rather than introducing a new static import boundary / Python
package (Issue #2290 "Notes for Reviewer").
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

_MODULE_DIR = Path(__file__).resolve().parent
# parents: [0]=scripts, [1]=<repo root>
_REPO_ROOT = _MODULE_DIR.parents[1]
_DEFAULT_POLICY_YAML_PATH = _REPO_ROOT / "docs" / "dev" / "extension-surface-runtime-policy.yaml"

if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from changed_file_matcher import AllowedPathsMatcher  # noqa: E402

SCHEMA_POLICY_EVALUATION = "EXTENSION_SURFACE_POLICY_EVALUATION_V1"
SCHEMA_RISK_TRIGGER_VERDICT = "EXTENSION_SURFACE_RISK_TRIGGER_VERDICT_V1"
SCHEMA_RUNTIME_ASSERTION_BINDING_COVERAGE = "RUNTIME_ASSERTION_BINDING_COVERAGE_RESULT_V1"

# Mirrors `_RVA_IMMEDIATE_REQUIRED_FIELDS` in
# `.claude/skills/issue-contract-review/scripts/contract_readiness_check.py`'s
# `check_rva_immediate_fields()` -- centralised here so
# `check_issue_contract.py` (review-issue) does not reimplement the field
# list independently (Issue #2290 Current Validated Scope, 4th bullet).
RVA_IMMEDIATE_REQUIRED_FIELDS = [
    "applicable_acs",
    "execution_environment",
    "skip_conditions",
    "fallback_policy",
    "artifact_requirements",
]

# `resolution.final_decision: most_restrictive` in the policy YAML resolves
# to this fixed rank ordering (see the YAML's own `resolution:` comment):
# immediate > deferred > not_applicable, most restrictive first.
DECISION_RANK: dict[str, int] = {
    "not_applicable": 0,
    "deferred": 1,
    "immediate": 2,
}


class PolicyLoadError(RuntimeError):
    """Raised when the policy YAML cannot be loaded, does not parse to a
    mapping, or does not satisfy the cheap structural contract this module
    depends on (PR #2335 OWNER review fix_delta, Issue #2290 P1-2).

    This is intentionally NOT a full ``jsonschema.validate()`` run against
    ``docs/dev/extension-surface-runtime-policy.schema.json`` (that already
    happens separately in
    ``docs/dev/tests/test_extension_surface_runtime_policy_schema.py``).
    This is a cheap, targeted check of only the fields this module's own
    logic reads, so that a malformed/incompatible policy file fails closed
    with an explicit, distinguishable error instead of silently degrading
    to "no match" behaviour that looks identical to a legitimately clean
    Allowed Paths set.
    """


# `resolution` values this module's evaluation logic actually implements.
# A policy YAML declaring any other `resolution.multiple_matches` /
# `resolution.final_decision` value is not safely interpretable by this
# evaluator and must fail closed rather than silently mis-evaluate.
_SUPPORTED_RESOLUTION_MULTIPLE_MATCHES = {"evaluate_all"}
_SUPPORTED_RESOLUTION_FINAL_DECISION = {"most_restrictive"}


def _validate_policy_contract(data: dict[str, Any], path: Path) -> None:
    """Cheap structural validation of the fields this module depends on.

    Raises ``PolicyLoadError`` (not a bare assertion) so callers can
    distinguish "policy unavailable" from a normal "no candidate match"
    result (Issue #2290 P1-2).
    """
    if data.get("schema_version") != "v2":
        raise PolicyLoadError(
            f"policy yaml at {path} has unsupported schema_version "
            f"{data.get('schema_version')!r} (expected 'v2')"
        )

    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise PolicyLoadError(f"policy yaml at {path} has an empty or missing 'rules' list")

    for rule in rules:
        if isinstance(rule, dict):
            _validate_issue_time_exemption_property(rule)

    resolution = data.get("resolution")
    if not isinstance(resolution, dict):
        raise PolicyLoadError(f"policy yaml at {path} is missing a 'resolution' mapping")
    multiple_matches = resolution.get("multiple_matches")
    if multiple_matches not in _SUPPORTED_RESOLUTION_MULTIPLE_MATCHES:
        raise PolicyLoadError(
            f"policy yaml at {path} declares unsupported "
            f"resolution.multiple_matches {multiple_matches!r} "
            f"(supported: {sorted(_SUPPORTED_RESOLUTION_MULTIPLE_MATCHES)})"
        )
    final_decision_strategy = resolution.get("final_decision")
    if final_decision_strategy not in _SUPPORTED_RESOLUTION_FINAL_DECISION:
        raise PolicyLoadError(
            f"policy yaml at {path} declares unsupported "
            f"resolution.final_decision {final_decision_strategy!r} "
            f"(supported: {sorted(_SUPPORTED_RESOLUTION_FINAL_DECISION)})"
        )


# `unknown_surface_policy.decision` / `.gate` values this module's evaluator
# actually implements. Mirrors `docs/dev/extension-surface-runtime-policy
# .schema.json`'s `unknown_surface_policy.decision` (`enum: [human_judgment]`)
# and `.gate` (narrowed to `const: advisory`, Issue #2339 PR #2370 OWNER
# review fix_delta P1-3 -- `gate: block` is no longer a supported production
# value; a policy declaring any other value is not safely interpretable and
# must fail closed rather than silently mis-evaluate or be read as dead
# metadata).
_EXPECTED_UNKNOWN_SURFACE_POLICY_DECISION = "human_judgment"
_EXPECTED_UNKNOWN_SURFACE_POLICY_GATE = "advisory"


def _extract_unknown_surface_policy(data: dict[str, Any]) -> dict[str, Any]:
    """Cheap structural validation + extraction of the full
    ``unknown_surface_policy`` mapping (``decision`` / ``gate`` /
    ``project_candidate_path_globs``) (Issue #2339 AC9; PR #2370 OWNER
    review fix_delta P1-1/P1-3).

    Called from ``evaluate_allowed_paths`` for *every* policy source (both
    the default ``load_policy()`` path and a ``policy=`` dict passed
    directly by a caller/test), so a malformed ``unknown_surface_policy``
    fails closed with a distinguishable ``PolicyLoadError`` regardless of
    how the policy mapping was constructed -- it must never silently
    degrade to "no candidate perimeter" (which would look identical to a
    legitimately clean/empty candidate perimeter and risk silently
    approving what should have been an advisory finding, Issue #2339 AC9).

    ``unknown_surface_policy`` is REQUIRED here (PR #2370 OWNER review
    fix_delta P1-1 -- the previous "optional at this cheap-validation layer"
    design let a missing/``None`` ``unknown_surface_policy`` or a missing
    ``project_candidate_path_globs`` key silently fall through to "no
    candidate perimeter" instead of failing closed, contradicting
    ``docs/dev/extension-surface-runtime-policy.schema.json`` which already
    declares all three of ``decision`` / ``gate`` /
    ``project_candidate_path_globs`` as required). Every fixture policy
    passed through this function -- including synthetic/minimal test
    fixtures -- must declare a structurally valid ``unknown_surface_policy``;
    this is intentionally NOT made optional again to "fix" a failing
    fixture (fixtures are the ones that must be updated, not this
    validation).

    ``decision`` and ``gate`` are validated against the single production
    value each currently supports (see the module-level
    ``_EXPECTED_UNKNOWN_SURFACE_POLICY_*`` constants above) rather than left
    unread as dead metadata (P1-3): an unsupported value is exactly as
    unsafe to silently ignore as a malformed
    ``project_candidate_path_globs`` list.
    """
    unknown_surface_policy = data.get("unknown_surface_policy")
    if not isinstance(unknown_surface_policy, dict):
        raise PolicyLoadError(
            f"policy declares 'unknown_surface_policy' as "
            f"{type(unknown_surface_policy).__name__ if unknown_surface_policy is not None else 'missing/None'}, "
            "expected a mapping (Issue #2339 AC9 / PR #2370 P1-1: a missing or non-mapping "
            "'unknown_surface_policy' must fail closed as policy-unavailable, not silently "
            "degrade to 'no candidate perimeter')"
        )

    decision = unknown_surface_policy.get("decision")
    if decision != _EXPECTED_UNKNOWN_SURFACE_POLICY_DECISION:
        raise PolicyLoadError(
            f"policy declares 'unknown_surface_policy.decision' as {decision!r}, expected "
            f"{_EXPECTED_UNKNOWN_SURFACE_POLICY_DECISION!r} (PR #2370 P1-1: an unsupported/missing "
            "decision value must fail closed rather than be silently unread)"
        )

    gate = unknown_surface_policy.get("gate")
    if gate != _EXPECTED_UNKNOWN_SURFACE_POLICY_GATE:
        raise PolicyLoadError(
            f"policy declares 'unknown_surface_policy.gate' as {gate!r}, expected "
            f"{_EXPECTED_UNKNOWN_SURFACE_POLICY_GATE!r} (PR #2370 P1-1/P1-3: an unsupported/missing "
            "gate value must fail closed rather than be silently unread)"
        )

    raw_globs = unknown_surface_policy.get("project_candidate_path_globs")
    if not isinstance(raw_globs, list) or not raw_globs:
        raise PolicyLoadError(
            "policy declares 'unknown_surface_policy.project_candidate_path_globs' as "
            f"{raw_globs!r}, expected a non-empty list of non-empty, matcher-v2-valid glob "
            "strings (Issue #2339 AC9: malformed unknown_surface_policy must fail closed as "
            "policy-unavailable, not silently approve)"
        )
    for glob in raw_globs:
        if not isinstance(glob, str) or not glob:
            raise PolicyLoadError(
                "policy declares an entry in 'unknown_surface_policy.project_candidate_path_globs' "
                f"as {glob!r}, expected a non-empty string (PR #2370 P1-1: fail closed rather than "
                "silently skip an invalid candidate glob)"
            )
        if AllowedPathsMatcher.normalize_allowed_pattern(glob) is None:
            raise PolicyLoadError(
                f"policy declares 'unknown_surface_policy.project_candidate_path_globs' entry "
                f"{glob!r} which is not a valid matcher-v2 glob (PR #2370 P1-1: an invalid glob "
                "must fail closed rather than be silently read as 'continue'/never match)"
            )

    return {
        "decision": decision,
        "gate": gate,
        "project_candidate_path_globs": raw_globs,
    }


def load_policy(policy_path: Optional[Path] = None) -> dict[str, Any]:
    """Load, parse, and cheaply validate the extension-surface risk-trigger
    policy YAML. Raises ``PolicyLoadError`` (never returns a partially
    unusable mapping) if the file cannot be read/parsed, does not parse to
    a mapping, or fails the cheap structural contract in
    ``_validate_policy_contract`` (Issue #2290 P1-2)."""
    path = policy_path or _DEFAULT_POLICY_YAML_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        raise PolicyLoadError(f"failed to read policy yaml at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyLoadError(f"failed to parse policy yaml at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyLoadError(f"policy yaml at {path} did not parse to a mapping")
    _validate_policy_contract(data, path)
    return data


def _static_prefix_segments(normalized_glob: str) -> list[str]:
    """Segments of a normalized path/glob before its first wildcard segment."""
    prefix: list[str] = []
    for segment in normalized_glob.split("/"):
        if segment in ("*", "**"):
            break
        prefix.append(segment)
    return prefix


def _one_is_prefix_of_other(a: list[str], b: list[str]) -> bool:
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[: len(shorter)] == shorter


_DEFAULT_ISSUE_TIME_ENFORCEMENT = "hard"


def _project_path_globs(rule: dict[str, Any]) -> list[tuple[str, str]]:
    """``(path_glob, issue_time_enforcement)`` pairs from a rule's
    ``selectors[].source_scope: project`` entries.

    ``runtime_resolved_only`` selectors (user/managed/plugin/session/cli
    source_scope) carry no ``path_globs`` and are intentionally excluded --
    they cannot be evaluated against repository-relative Allowed Paths
    (Issue #2290 In Scope: ``selectors[].source_scope: project`` only).

    Each project selector may declare its own ``issue_time_enforcement``
    (``hard`` / ``advisory``, Issue #2356). A selector that omits the field
    is treated as ``hard`` -- the required runtime fallback for existing
    rules that predate this field (e.g. ``claude-gpt-lifecycle-invocation-change``).
    Pairing the enforcement value with each glob (instead of returning a
    flat ``list[str]``) preserves selector identity so callers can derive
    advisory/hard per-glob rather than relying on a separate hardcoded
    constant (Issue #2356; supersedes the removed ``CANDIDATE_ONLY_PATH_GLOBS``
    frozenset from Issue #2290 / PR #2335).
    """
    pairs: list[tuple[str, str]] = []
    for selector in rule.get("selectors", []) or []:
        if selector.get("source_scope") != "project":
            continue
        raw_issue_time_enforcement = selector.get("issue_time_enforcement")
        if raw_issue_time_enforcement is None:
            issue_time_enforcement = _DEFAULT_ISSUE_TIME_ENFORCEMENT
        elif raw_issue_time_enforcement in ("hard", "advisory"):
            issue_time_enforcement = raw_issue_time_enforcement
        else:
            raise PolicyLoadError(
                "project selector declares unsupported issue_time_enforcement "
                f"{raw_issue_time_enforcement!r} (expected 'hard', 'advisory', or omitted; "
                "a malformed value must fail closed rather than silently degrade to "
                "'advisory', PR #2359 OWNER review fix_delta, Issue #2356)"
            )
        for path_glob in selector.get("path_globs", []) or []:
            pairs.append((path_glob, issue_time_enforcement))
    return pairs


def _selector_remainder_after_prefix(normalized_selector: str, prefix_len: int) -> list[str]:
    """Segments of ``normalized_selector`` after its first ``prefix_len`` segments."""
    return normalized_selector.split("/")[prefix_len:]


def _is_single_file_at_any_depth_selector(normalized_selector: str, selector_prefix: list[str]) -> bool:
    """True if, past its static prefix, ``normalized_selector`` is exactly one
    ``**`` segment followed by exactly one literal terminal segment and
    nothing else (e.g. ``.claude/skills/**/SKILL.md``).

    This pattern shape means "a specific named file, located directly under
    whatever the ``**`` expands to" -- by this repository's own Claude Code
    skill convention (see this module's docstring: ``SKILL.md`` always sits
    directly under ``.claude/skills/<name>/``, never nested inside a further
    named subdirectory such as ``tests/``, ``fixtures/`` or ``schemas/``).
    Selectors with a *different* trailing shape (e.g.
    ``.claude/skills/**/scripts/**``, which ends in another ``**`` and thus
    describes an entire subtree, not a single fixed-depth file) do not
    qualify.
    """
    remainder = _selector_remainder_after_prefix(normalized_selector, len(selector_prefix))
    return len(remainder) == 2 and remainder[0] == "**" and remainder[1] not in ("*", "**")


def match_allowed_path_entry(entry: str, rule: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return a match-info dict if ``entry`` is a candidate for ``rule``, else ``None``.

    A rule may declare more than one project selector, each pairing its
    ``path_globs`` with its own ``issue_time_enforcement`` (e.g.
    ``skill-invocation-procedure-or-contract-change`` declares a ``hard``
    selector for ``.claude/skills/**/SKILL.md`` and a separate ``advisory``
    selector for ``.claude/skills/**/scripts/**``, Issue #2356). For a
    *wildcard* Allowed Path entry, the conservative static-prefix comparison
    can match several of the rule's globs at once with equal confidence
    (e.g. an entry whose prefix is ``.claude/skills/foo`` is an equally
    valid conservative match against both globs above). Picking only the
    first glob encountered is an ordering artifact, not a judgment -- if
    picking the "wrong" glob silently turns an advisory (candidate-discovery-
    only) match into a hard-block match, the gate self-contradicts on its
    own declared Allowed Paths (PR #2335 OWNER review fix_delta, Issue
    #2290 P0 re-fix). To stay on the documented "conservative" side (false
    negatives are acceptable, false positives are not; but a hard block
    triggered purely by glob-iteration order is itself a false positive
    here), a wildcard entry that ambiguously matches both an advisory glob
    and a hard glob within the same rule is resolved to the advisory match.
    An entry that matches only advisory globs, or only hard globs, is
    unambiguous and keeps its natural classification.
    """
    normalized_entry_pattern = AllowedPathsMatcher.normalize_allowed_pattern(entry)
    if normalized_entry_pattern is None:
        return None
    is_wildcard = "*" in normalized_entry_pattern

    matches: list[dict[str, Any]] = []
    for path_glob, issue_time_enforcement in _project_path_globs(rule):
        normalized_selector = AllowedPathsMatcher.normalize_allowed_pattern(path_glob)
        if normalized_selector is None:
            continue

        if not is_wildcard:
            # Exact Allowed Path: direct matcher-v2 file-vs-pattern match
            # against the selector glob (Issue #2290 In Scope, bullet 1).
            normalized_file = AllowedPathsMatcher.normalize_path(entry)
            if normalized_file is None:
                continue
            if AllowedPathsMatcher.matches_pattern(normalized_file, normalized_selector):
                # An exact repo-relative path is unambiguous: it cannot
                # simultaneously live under two structurally distinct
                # globs of the same rule in practice, so the first match
                # found is authoritative (unaffected by the wildcard
                # ambiguity handling below).
                return {
                    "match_kind": "exact",
                    "allowed_path_entry": entry,
                    "path_glob": path_glob,
                    "issue_time_enforcement": issue_time_enforcement,
                }
            continue

        # Wildcard Allowed Path: conservative literal/static-prefix
        # candidate detection only -- NOT a full glob-intersection engine
        # (Issue #2290 In Scope, bullet 1). Either prefix being a prefix of
        # the other means the two path spaces *could* overlap once the
        # wildcard segments are expanded; this over-flags rather than
        # silently missing a candidate. Unlike the exact-path case above,
        # evaluate every glob in the rule (not just the first hit) so
        # ambiguous multi-glob matches can be detected below.
        entry_prefix = _static_prefix_segments(normalized_entry_pattern)
        selector_prefix = _static_prefix_segments(normalized_selector)
        if not _one_is_prefix_of_other(entry_prefix, selector_prefix):
            continue

        if len(entry_prefix) > len(selector_prefix) + 1 and _is_single_file_at_any_depth_selector(
            normalized_selector, selector_prefix
        ):
            # The entry's static prefix drills more than one segment past
            # this selector's own static prefix, while the selector names a
            # single specific file located directly under whatever its
            # "**" expands to (e.g. "SKILL.md" at the skill root). Per this
            # repository's Claude Code skill convention, such files are
            # never nested inside a further named subdirectory (tests/,
            # fixtures/, schemas/, ...), so a wildcard entry scoped to such
            # a subdirectory cannot conservatively be a candidate for this
            # selector -- unlike ``.claude/skills/**/scripts/**``-shaped
            # selectors (an entire-subtree pattern, unaffected by this
            # check), which is why this only applies to single-file-shaped
            # selectors (PR #2335 second OWNER review fix_delta, Issue
            # #2290 P0 re-fix).
            continue

        matches.append(
            {
                "match_kind": "conservative_wildcard_prefix",
                "allowed_path_entry": entry,
                "path_glob": path_glob,
                "issue_time_enforcement": issue_time_enforcement,
            }
        )

    if not matches:
        return None

    advisory_hits = [m for m in matches if m["issue_time_enforcement"] == "advisory"]
    hard_hits = [m for m in matches if m["issue_time_enforcement"] == "hard"]
    if advisory_hits and hard_hits:
        # Ambiguous: the same wildcard entry's conservative prefix matched
        # both an advisory selector and a hard selector within this rule,
        # with no basis to prefer one over the other. Resolve to the
        # advisory match rather than the hard match to avoid a
        # glob-iteration-order-dependent hard block.
        return advisory_hits[0]

    return matches[0]


def _matches_candidate_perimeter(entry: str, candidate_path_globs: list[str]) -> Optional[str]:
    """Return the first ``unknown_surface_policy.project_candidate_path_globs``
    entry that ``entry`` is a conservative candidate for, else ``None``.

    Mirrors ``match_allowed_path_entry``'s exact-match / conservative
    static-prefix-overlap comparison against a rule's ``path_globs``, but
    against the flat candidate-perimeter glob list instead of a rule's
    selectors (the candidate perimeter has no ``source_scope`` /
    ``issue_time_enforcement`` concept -- Issue #2339 AC11: it is a
    project-local-only, repository-relative glob list, never resolved
    against user/managed/plugin/session/cli source_scope surfaces).
    """
    normalized_entry_pattern = AllowedPathsMatcher.normalize_allowed_pattern(entry)
    if normalized_entry_pattern is None:
        return None
    is_wildcard = "*" in normalized_entry_pattern

    for glob in candidate_path_globs:
        normalized_glob = AllowedPathsMatcher.normalize_allowed_pattern(glob)
        if normalized_glob is None:
            continue

        if not is_wildcard:
            normalized_file = AllowedPathsMatcher.normalize_path(entry)
            if normalized_file is None:
                continue
            if AllowedPathsMatcher.matches_pattern(normalized_file, normalized_glob):
                return glob
            continue

        entry_prefix = _static_prefix_segments(normalized_entry_pattern)
        glob_prefix = _static_prefix_segments(normalized_glob)
        if _one_is_prefix_of_other(entry_prefix, glob_prefix):
            return glob

    return None


def evaluate_allowed_paths(
    allowed_path_entries: list[str],
    policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Evaluate declared Allowed Paths against every rule in the policy.

    Applies the policy's ``resolution.multiple_matches: evaluate_all`` /
    ``resolution.final_decision: most_restrictive`` semantics (Issue #2290
    In Scope, bullet 2) and derives the union of required
    ``verification_profiles`` from the matched rules (Issue #2290 In Scope,
    bullet 3).

    Issue #2339: each declared Allowed Path entry is additionally
    classified into exactly one of ``matched_rule`` / ``unclassified_candidate``
    / ``ordinary`` (``path_classifications``), and any ``unclassified_candidate``
    entry (a ``unknown_surface_policy.project_candidate_path_globs`` match
    with no known rule selector match) contributes a non-blocking
    ``advisories`` message. ``unclassified_candidate`` entries never
    contribute to ``final_decision`` -- only ``matched_rule`` entries with
    ``enforcement: hard`` do (unchanged from Issue #2290/#2356 semantics).
    """
    policy_data = policy if policy is not None else load_policy()
    rules = policy_data.get("rules", []) or []
    unknown_surface_policy = _extract_unknown_surface_policy(policy_data)
    candidate_path_globs = unknown_surface_policy["project_candidate_path_globs"]
    # PR #2370 P1-3: `decision` / `gate` are read (not dead metadata) and
    # surfaced verbatim on every `unclassified_candidate` classification
    # below via `policy_action`, but never merged into `final_decision`
    # (Issue #2339 AC8) -- `human_judgment` never appears as a
    # `final_decision` value.
    policy_action = {"decision": unknown_surface_policy["decision"], "gate": unknown_surface_policy["gate"]}

    matched_rules: list[dict[str, Any]] = []
    verification_profiles: set[str] = set()
    final_decision: Optional[str] = None
    entries_with_rule_hit: set[str] = set()

    for rule in rules:
        match_hits: list[dict[str, Any]] = []
        for entry in allowed_path_entries:
            hit = match_allowed_path_entry(entry, rule)
            if hit is not None:
                match_hits.append(hit)
                entries_with_rule_hit.add(entry)
        if not match_hits:
            continue

        hard_hits = [hit for hit in match_hits if hit["issue_time_enforcement"] == "hard"]
        enforcement = "hard" if hard_hits else "advisory"

        rule_decision = rule.get("default_decision")
        rule_profile = rule.get("verification_profile")
        matched_rules.append(
            {
                "rule_id": rule.get("id"),
                "default_decision": rule_decision,
                "verification_profile": rule_profile,
                "matches": match_hits,
                "enforcement": enforcement,
            }
        )
        if rule_profile:
            verification_profiles.add(rule_profile)
        # Advisory-only matches (every hit's selector declares
        # issue_time_enforcement: advisory) are candidate discovery
        # signals, not a hard block -- they never contribute to
        # final_decision (Issue #2290 P0 fix delta, PR #2335; derivation
        # migrated from the removed CANDIDATE_ONLY_PATH_GLOBS constant to
        # selector-declared issue_time_enforcement by Issue #2356).
        if enforcement == "hard" and rule_decision in DECISION_RANK:
            if final_decision is None or DECISION_RANK[rule_decision] > DECISION_RANK[final_decision]:
                final_decision = rule_decision

    # Issue #2339: classify every declared entry that matched no rule as
    # either `unclassified_candidate` (a project_candidate_path_globs hit)
    # or `ordinary` (no candidate glob hit either). `matched_rule` entries
    # keep their existing `matched_rules` representation above; this pass
    # only adds the three-way per-entry classification view, it does not
    # change any existing matched-rule semantics.
    path_classifications: list[dict[str, Any]] = []
    advisories: list[str] = []
    for entry in allowed_path_entries:
        if entry in entries_with_rule_hit:
            path_classifications.append({"entry": entry, "classification": "matched_rule"})
            continue
        candidate_glob = _matches_candidate_perimeter(entry, candidate_path_globs)
        if candidate_glob is not None:
            path_classifications.append(
                {
                    "entry": entry,
                    "classification": "unclassified_candidate",
                    "candidate_path_glob": candidate_glob,
                    "policy_action": policy_action,
                }
            )
            advisories.append(
                f"Allowed Path entry '{entry}' matches the project-local extension candidate "
                f"perimeter glob '{candidate_glob}' (unknown_surface_policy.project_candidate_path_globs) "
                "but no known extension-surface risk-trigger rule selector. This is a non-blocking "
                "advisory -- verdict: approve, final_decision unaffected -- surfaced so a human can "
                "confirm whether this is a genuine new extension surface (Issue #2339)."
            )
        else:
            path_classifications.append({"entry": entry, "classification": "ordinary"})

    return {
        "schema": SCHEMA_POLICY_EVALUATION,
        "matched_rules": matched_rules,
        "has_match": bool(matched_rules),
        "final_decision": final_decision,
        "verification_profiles": sorted(verification_profiles),
        "resolution": {
            "multiple_matches": (policy_data.get("resolution") or {}).get("multiple_matches"),
            "final_decision_strategy": (policy_data.get("resolution") or {}).get("final_decision"),
        },
        "path_classifications": path_classifications,
        "advisories": advisories,
    }


def find_missing_rva_immediate_fields(rva_section_text: str) -> list[str]:
    """Return required-field names missing from an ``immediate`` RVA section.

    Mirrors the field-presence semantics of
    ``.claude/skills/issue-contract-review/scripts/contract_readiness_check.py``'s
    ``check_rva_immediate_fields()`` (simple ``^\\s*<field>:`` regex match per
    required field), centralised here so callers do not reimplement it
    independently (Issue #2290 Current Validated Scope, 4th bullet).
    """
    missing: list[str] = []
    for field_name in RVA_IMMEDIATE_REQUIRED_FIELDS:
        pattern = re.compile(rf"^\s*{re.escape(field_name)}\s*:", re.MULTILINE)
        if not pattern.search(rva_section_text or ""):
            missing.append(field_name)
    return missing


# ---------------------------------------------------------------------------
# Issue-time comment-only exemption (Issue #2961, OWNER decision Option B)
# ---------------------------------------------------------------------------
#
# A rule may opt in (policy side) to a narrowly scoped, structure-only
# exemption from its ``default_decision`` hard requirement at Issue time via
# the optional rule-level property ``issue_time_exemption``. Only
# ``claude-gpt-lifecycle-invocation-change`` opts in. The Issue declares
# ``executable_semantics_unchanged: {rule: <rule-id>, ac: AC<N>}`` in its
# Runtime Verification Applicability section.
#
# This module verifies the STRUCTURE of the declaration and of the VC only
# (a ``git diff`` VC that names each exempted exact file path). The existence
# of such a VC is NOT proof that executable semantics are unchanged: the real
# acceptance evidence is the PR-time VC execution and review. That residual
# risk is accepted by design (Issue #2961).
#
# Both ``evaluate_issue_risk_trigger()`` and
# ``evaluate_runtime_assertion_binding_coverage()`` call the SAME helper
# below, so EXTSURF001 and RUNTIMEASSERT001 can never disagree.

ISSUE_TIME_EXEMPTION_DECLARATION_KEY = "executable_semantics_unchanged"
ISSUE_TIME_EXEMPTION_MODE = "exact_file_path_git_diff_vc"
_ISSUE_TIME_EXEMPTION_DECLARATION_KEYS = frozenset({"rule", "ac"})
# Policy-side opt-in boundary; mirrors the JSON Schema (Issue #2961).
ISSUE_TIME_EXEMPTION_OPT_IN_RULE_ID = "claude-gpt-lifecycle-invocation-change"
_ISSUE_TIME_EXEMPTION_POLICY_KEYS = frozenset({"declaration_key", "mode"})
_ISSUE_TIME_EXEMPTION_GLOB_CHARS = frozenset("*?[]{}")
# Carrier identity shared by readiness `category` and review-issue
# `non_blocking_improvements[].code` (information only, never blocking).
ISSUE_TIME_EXEMPTION_CARRIER_CODE = "extension_surface_issue_time_exemption_applied"


def build_ac_vc_commands(vc_commands: Optional[Iterable[Any]]) -> dict[str, tuple[str, ...]]:
    """Pure helper: ``{AC digit: (VC command body, ...)}`` from canonical
    parsed VC entries (Issue #2961).

    ``vc_commands`` is duck-typed: any iterable of objects exposing
    ``ac_refs`` (labels such as ``"AC1"``) and ``command`` -- in practice
    ``vc_contract_syntax.VcParseResult.commands`` from the existing canonical
    parser. No new parser is introduced and no raw Issue body is re-parsed
    here. Entries without a canonical ``AC<N>`` reference are ignored.
    """
    mapping: dict[str, list[str]] = {}
    for entry in vc_commands or ():
        command = getattr(entry, "command", None)
        if not isinstance(command, str):
            continue
        for ref in getattr(entry, "ac_refs", None) or ():
            if not isinstance(ref, str):
                continue
            match = _AC_TOKEN_RE.match(ref.strip())
            if match is None:
                continue
            mapping.setdefault(match.group(1), []).append(command)
    return {digit: tuple(commands) for digit, commands in mapping.items()}


def _is_exact_file_path_entry(entry: str) -> bool:
    """Fixed predicate (Issue #2961 In Scope 2): no glob character, no
    trailing ``/``, and a final path segment that contains ``.`` (an
    extension-bearing basename)."""
    if not isinstance(entry, str) or not entry:
        return False
    if any(ch in _ISSUE_TIME_EXEMPTION_GLOB_CHARS for ch in entry):
        return False
    if entry.endswith("/"):
        return False
    last_segment = entry.rsplit("/", 1)[-1]
    return "." in last_segment and last_segment not in (".", "..")


def _command_is_git_diff_for_path(command: str, path: str) -> bool:
    """``shlex.split`` tokenisation (pure string processing, nothing is
    executed): the first two tokens are ``git`` ``diff`` AND ``path`` appears
    as a token. ``rg 'git diff'`` / ``echo git diff`` never qualify."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] != "diff":
        return False
    return path in tokens[2:]


def _validate_issue_time_exemption_property(rule: dict[str, Any]) -> bool:
    """Runtime mirror of the JSON Schema boundary for ``issue_time_exemption``
    (Issue #2961, OWNER review PR #2965).

    The schema allows the property only on
    ``ISSUE_TIME_EXEMPTION_OPT_IN_RULE_ID`` and closes its key set to
    ``{declaration_key, mode}``. The matcher does not run a JSON Schema
    validator, so this check keeps runtime and schema semantics identical:
    any other rule carrying the property, an extra / missing key, or an
    unexpected value fails closed with ``PolicyLoadError``.

    Returns True iff the rule carries a valid opt-in."""
    raw = rule.get("issue_time_exemption")
    if raw is None:
        return False
    if (
        rule.get("id") != ISSUE_TIME_EXEMPTION_OPT_IN_RULE_ID
        or not isinstance(raw, dict)
        or set(raw.keys()) != _ISSUE_TIME_EXEMPTION_POLICY_KEYS
        or raw.get("declaration_key") != ISSUE_TIME_EXEMPTION_DECLARATION_KEY
        or raw.get("mode") != ISSUE_TIME_EXEMPTION_MODE
    ):
        raise PolicyLoadError(
            f"rule {rule.get('id')!r} declares an unsupported issue_time_exemption "
            f"{raw!r} (only rule {ISSUE_TIME_EXEMPTION_OPT_IN_RULE_ID!r} may opt in, with "
            f"exactly declaration_key {ISSUE_TIME_EXEMPTION_DECLARATION_KEY!r} / "
            f"mode {ISSUE_TIME_EXEMPTION_MODE!r}; Issue #2961)"
        )
    return True


def _rule_issue_time_exemption_opt_in(rule: dict[str, Any]) -> bool:
    """True iff the policy rule opted in via ``issue_time_exemption``.

    A present-but-unsupported value fails closed with ``PolicyLoadError``
    (a policy integrity defect, never an Issue defect)."""
    return _validate_issue_time_exemption_property(rule)


def evaluate_issue_time_exemptions(
    matched_rules: list[dict[str, Any]],
    policy: dict[str, Any],
    rva_section_text: str,
    ac_section_text: Optional[str],
    ac_vc_commands: Optional[dict[str, tuple[str, ...]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Shared exemption decision used by BOTH risk-trigger and binding
    coverage evaluators (Issue #2961).

    Returns ``(exemptions, rejections)``. ``exemptions`` is
    ``[{"rule_id", "ac", "paths"}]`` (applied exemptions only). ``rejections``
    explains, in human-readable text, why a *present* declaration was not
    applied (diagnostic only; it never changes any verdict). All of the
    following must hold, otherwise nothing is exempted (fail-closed):

    1. the rule opted in via ``issue_time_exemption``
    2. every Allowed Path entry matching the rule is an exact file path
    3. the declaration is well-formed (closed key set ``rule`` / ``ac``)
    4. the referenced AC exists in ``ac_section_text``
    5. ``ac_vc_commands`` has a canonical ``# AC<N>`` entry for that AC
    6. every exact path has a ``git diff`` VC (see
       ``_command_is_git_diff_for_path``) under that AC

    ``ac_vc_commands is None`` (old call sites, parser unavailable) never
    exempts. Structure only: not proof that executable semantics are
    unchanged.
    """
    raw = _extract_rva_yaml_field(rva_section_text, ISSUE_TIME_EXEMPTION_DECLARATION_KEY)
    if raw is None:
        return [], []

    key = ISSUE_TIME_EXEMPTION_DECLARATION_KEY
    if ac_vc_commands is None:
        return [], [f"{key} not applied: ac_vc_commands was not provided (fail-closed)"]
    if not isinstance(raw, dict) or set(raw.keys()) != _ISSUE_TIME_EXEMPTION_DECLARATION_KEYS:
        return [], [
            f"{key} not applied: declaration must be a mapping with exactly the keys "
            f"{sorted(_ISSUE_TIME_EXEMPTION_DECLARATION_KEYS)}"
        ]
    rule_id = raw.get("rule")
    ac_label = raw.get("ac")
    ac_match = _AC_TOKEN_RE.match(ac_label.strip()) if isinstance(ac_label, str) else None
    if not isinstance(rule_id, str) or not rule_id.strip() or ac_match is None:
        return [], [f"{key} not applied: 'rule' must be a rule id and 'ac' must be 'AC<N>'"]
    rule_id = rule_id.strip()
    ac_digit = ac_match.group(1)

    matched = next((r for r in matched_rules if r.get("rule_id") == rule_id), None)
    if matched is None or matched.get("enforcement") != "hard":
        return [], [f"{key} not applied: rule {rule_id!r} is not a hard-matched rule of this Issue"]
    policy_rule = next((r for r in (policy.get("rules") or []) if r.get("id") == rule_id), None)
    if policy_rule is None or not _rule_issue_time_exemption_opt_in(policy_rule):
        return [], [f"{key} not applied: rule {rule_id!r} has no issue_time_exemption opt-in"]

    paths: list[str] = []
    for hit in matched.get("matches") or []:
        entry = hit.get("allowed_path_entry")
        if entry not in paths:
            paths.append(entry)
    non_exact = [p for p in paths if not _is_exact_file_path_entry(p)]
    if not paths or non_exact:
        return [], [
            f"{key} not applied: every Allowed Path matching {rule_id!r} must be an exact file "
            f"path (not exact: {non_exact})"
        ]

    if ac_digit not in extract_ac_numbers(ac_section_text or ""):
        return [], [f"{key} not applied: AC{ac_digit} does not exist in the Acceptance Criteria"]
    commands = [c for c in (ac_vc_commands.get(ac_digit) or ()) if isinstance(c, str)]
    if not commands:
        return [], [f"{key} not applied: AC{ac_digit} has no canonical '# AC{ac_digit}' VC command"]
    for path in paths:
        candidates = {path}
        normalized = AllowedPathsMatcher.normalize_path(path)
        if normalized:
            candidates.add(normalized)
        if not any(_command_is_git_diff_for_path(c, cand) for c in commands for cand in candidates):
            return [], [
                f"{key} not applied: AC{ac_digit} has no 'git diff' VC command naming {path!r}"
            ]

    return [{"rule_id": rule_id, "ac": f"AC{ac_digit}", "paths": sorted(paths)}], []


def format_issue_time_exemption_lines(exemptions: list[dict[str, Any]]) -> list[str]:
    """One non-blocking carrier evidence line per applied exemption, shared by
    both consumers so the text cannot drift between them (Issue #2961)."""
    return [
        f"{e['rule_id']}: exempted via {ISSUE_TIME_EXEMPTION_DECLARATION_KEY}; "
        f"ac={e['ac']}; paths={', '.join(e['paths'])}"
        for e in exemptions
    ]


def _matched_rules_without_exempted(
    matched_rules: list[dict[str, Any]], exemptions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    exempted_ids = {e["rule_id"] for e in exemptions}
    return [r for r in matched_rules if r.get("rule_id") not in exempted_ids]


def _most_restrictive_hard_decision(matched_rules: list[dict[str, Any]]) -> Optional[str]:
    """Same derivation as ``evaluate_allowed_paths`` ``final_decision`` over
    the given (possibly exemption-filtered) matched rules."""
    final_decision: Optional[str] = None
    for rule in matched_rules:
        decision = rule.get("default_decision")
        if rule.get("enforcement") == "hard" and decision in DECISION_RANK:
            if final_decision is None or DECISION_RANK[decision] > DECISION_RANK[final_decision]:
                final_decision = decision
    return final_decision


def evaluate_issue_risk_trigger(
    allowed_path_entries: list[str],
    declared_decision: Optional[str],
    rva_section_text: str,
    policy: Optional[dict[str, Any]] = None,
    ac_section_text: Optional[str] = None,
    ac_vc_commands: Optional[dict[str, tuple[str, ...]]] = None,
) -> dict[str, Any]:
    """High-level verdict shared verbatim by both consumers.

    Both ``check_issue_contract.py`` (review-issue) and
    ``contract_readiness_check.py`` (issue-contract-review) call this single
    function so that, given the same fixture body, they return the same
    verdict (Issue #2290 AC7 parity requirement) -- the parity is structural
    (same function, same inputs), not independently re-derived.

    ``verdict`` is ``needs_fix`` when either:
      - the declared Allowed Paths have a policy candidate match whose
        ``final_decision`` (most-restrictive) outranks the Issue's declared
        Runtime Verification Applicability ``decision`` (AC1 / AC2), or
      - ``declared_decision == "immediate"`` but the RVA section is missing
        one or more of the required immediate fields (AC6).

    Issue #2961: ``ac_section_text`` / ``ac_vc_commands`` (optional; see
    ``build_ac_vc_commands``) let a rule that opted in via
    ``issue_time_exemption`` be excluded from the final decision when the
    Issue carries a well-formed ``executable_semantics_unchanged``
    declaration (see ``evaluate_issue_time_exemptions``). Other matched
    rules are never exempted. ``ac_vc_commands=None`` never exempts. Applied
    exemptions are reported under ``issue_time_exemptions`` (key present only
    when an exemption was applied).
    """
    policy_evaluation = evaluate_allowed_paths(allowed_path_entries, policy=policy)
    reasons: list[str] = []
    policy_data = policy if policy is not None else load_policy()

    declared_rank = DECISION_RANK.get(declared_decision) if declared_decision else None
    exemptions, exemption_rejections = evaluate_issue_time_exemptions(
        policy_evaluation["matched_rules"],
        policy_data,
        rva_section_text,
        ac_section_text,
        ac_vc_commands,
    )
    if exemptions:
        effective_matched_rules = _matched_rules_without_exempted(
            policy_evaluation["matched_rules"], exemptions
        )
        final_decision = _most_restrictive_hard_decision(effective_matched_rules)
    else:
        effective_matched_rules = policy_evaluation["matched_rules"]
        final_decision = policy_evaluation["final_decision"]

    if policy_evaluation["has_match"] and final_decision is not None:
        final_rank = DECISION_RANK[final_decision]
        if declared_rank is None or declared_rank < final_rank:
            matched_rule_ids = [r["rule_id"] for r in effective_matched_rules]
            reasons.append(
                "declared Allowed Paths overlap with extension-surface risk-trigger "
                f"policy rule(s) {matched_rule_ids} whose most-restrictive default_decision "
                f"is '{final_decision}', but the Issue declares "
                f"decision: '{declared_decision}'."
            )

    missing_fields: list[str] = []
    if declared_decision == "immediate":
        missing_fields = find_missing_rva_immediate_fields(rva_section_text)
        if missing_fields:
            reasons.append(
                "decision: immediate but the Runtime Verification Applicability "
                "contract's required immediate fields are missing: "
                + ", ".join(missing_fields)
            )

    verdict = "needs_fix" if reasons else "approve"
    result: dict[str, Any] = {
        "schema": SCHEMA_RISK_TRIGGER_VERDICT,
        "verdict": verdict,
        "reasons": reasons,
        "policy_evaluation": policy_evaluation,
        "missing_rva_immediate_fields": missing_fields,
        # Issue #2339: `unclassified_candidate` advisories (non-blocking --
        # never contribute to `reasons`/`verdict`; verdict stays `approve`
        # when reasons is empty even if advisories is non-empty, AC6).
        "advisories": policy_evaluation.get("advisories", []),
    }
    if exemptions:
        # Issue #2961: carrier for applied exemptions (non-blocking).
        result["issue_time_exemptions"] = exemptions
    if exemption_rejections:
        # Diagnostic only: never contributes to `reasons` / `verdict`.
        result["issue_time_exemption_rejections"] = exemption_rejections
    return result


# ---------------------------------------------------------------------------
# Runtime Verification profile assertion binding coverage (Issue #2771)
# ---------------------------------------------------------------------------
#
# This gate guarantees STRUCTURAL COMPLETENESS ONLY (structural_completeness_
# not_semantic_sufficiency): it verifies that every hard-required
# (verification_profile_id, assertion_id) composite pair -- derived purely
# from `matched_rules[].enforcement == "hard"`, never from an advisory-only
# match (PR #2370's non-blocking advisory semantics are not re-introduced as
# a blocker from this new angle) -- has an explicit `runtime_assertion_
# bindings` declaration in the Issue's Runtime Verification Applicability
# section. Whether the *bound* VC/AC actually proves the assertion's
# semantic postcondition (semantic sufficiency) is explicitly NOT judged
# here and remains the responsibility of existing semantic review
# (`pr-review-judge` etc.) -- see Issue #2771 Outcome / AC10.
#
# `.claude/skills/review-issue/scripts/check_issue_contract.py` and
# `.claude/skills/issue-contract-review/scripts/contract_readiness_check.py`
# both call the functions below (never reimplement composite-identity
# derivation independently) so the two consumers cannot drift (Issue #2771
# In Scope, "review-issue と issue-contract-review の双方で同一 shared
# evaluator を使い parity を維持する").


def derive_required_runtime_assertions(
    matched_rules: list[dict[str, Any]],
    policy: dict[str, Any],
) -> set[tuple[str, str]]:
    """Derive the required ``(verification_profile_id, assertion_id)``
    composite set from ``matched_rules`` (as returned by
    ``evaluate_allowed_paths()``), restricted to profiles that have at least
    one ``enforcement == "hard"`` matched rule (Issue #2771 AC1/AC2).

    Grouping is by ``verification_profile`` id, not by individual matched
    rule: if the *same* profile is reached by more than one matched rule
    (possible when a policy fixture declares two distinct rules that both
    reference the same profile), the profile's assertions are required as
    soon as *any* one of those rules is ``enforcement == "hard"`` -- an
    all-advisory set of rules for a profile keeps that profile entirely out
    of the required set (AC2). This is a profile-level decision; there is no
    per-assertion partial-hard/partial-advisory split within a single
    profile's own ``assertions[]`` list.

    Raises ``PolicyLoadError`` -- the same exception class this module
    already uses to fail closed on a malformed policy document -- for a
    *policy* integrity defect: a matched rule referencing a
    ``verification_profile`` id that is not defined in the policy's
    top-level ``verification_profiles`` mapping (a dangling profile
    reference), or a profile whose ``assertions[]`` list declares the same
    ``id`` more than once. Both are defects in
    ``docs/dev/extension-surface-runtime-policy.yaml`` itself, not in the
    Issue being reviewed, and Issue #2771 AC6 requires this module to keep
    that distinction explicit rather than silently reporting a policy defect
    as if it were an author-fixable Issue ``needs_fix``.
    """
    profiles = policy.get("verification_profiles")
    if not isinstance(profiles, dict):
        raise PolicyLoadError(
            "policy yaml is missing a 'verification_profiles' mapping (required to derive "
            "hard-required runtime assertion coverage, Issue #2771)"
        )

    referenced_profile_ids: set[str] = set()
    hard_profile_ids: set[str] = set()
    for rule in matched_rules:
        profile_id = rule.get("verification_profile")
        if not profile_id:
            continue
        referenced_profile_ids.add(profile_id)
        if rule.get("enforcement") == "hard":
            hard_profile_ids.add(profile_id)

    required: set[tuple[str, str]] = set()
    for profile_id in referenced_profile_ids:
        profile = profiles.get(profile_id)
        if not isinstance(profile, dict):
            raise PolicyLoadError(
                f"matched rule references verification_profile {profile_id!r} which is not "
                "defined in the policy's 'verification_profiles' mapping (dangling profile "
                "reference -- a policy integrity failure, not an Issue defect, Issue #2771 AC6)"
            )
        assertions = profile.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise PolicyLoadError(
                f"verification_profile {profile_id!r} declares no non-empty 'assertions' list "
                "(policy integrity failure, Issue #2771 AC6)"
            )
        seen_assertion_ids: set[str] = set()
        assertion_ids: list[str] = []
        for assertion in assertions:
            assertion_id = assertion.get("id") if isinstance(assertion, dict) else None
            if not assertion_id:
                raise PolicyLoadError(
                    f"verification_profile {profile_id!r} declares a malformed assertion entry "
                    "missing a non-empty 'id' (policy integrity failure, Issue #2771 AC6)"
                )
            if assertion_id in seen_assertion_ids:
                raise PolicyLoadError(
                    f"verification_profile {profile_id!r} declares duplicate assertion id "
                    f"{assertion_id!r} (policy integrity failure -- not an Issue defect, "
                    "Issue #2771 AC6)"
                )
            seen_assertion_ids.add(assertion_id)
            assertion_ids.append(assertion_id)

        if profile_id in hard_profile_ids:
            for assertion_id in assertion_ids:
                required.add((profile_id, assertion_id))

    return required


def _line_indent(line: str) -> int:
    """Count of leading whitespace characters (spaces/tabs) on ``line``."""
    return len(line) - len(line.lstrip(" \t"))


def _strip_unquoted_yaml_comment(text: str) -> str:
    """Strip a trailing unquoted YAML comment (``# ...``) from ``text``.

    Issue #2771 PR #2780 OWNER F1 review: a field key line's trailing
    explanatory comment (``runtime_assertion_bindings: # このACで両方を検証
    する``) must never be mistaken for that field's actual inline value --
    the previous implementation treated the raw, un-stripped remainder of
    the line as the inline value, so a comment-only suffix made a normal
    block-form binding disappear (parsed as a single truncated line instead
    of the field plus its nested block).

    Only a ``#`` that starts the string or is preceded by whitespace, and
    that is not inside a single- or double-quoted string, begins a comment
    (the same informal subset of the YAML comment rule PyYAML itself
    applies to plain scalars). This is intentionally a narrow, local
    heuristic scoped to this line-based field extractor -- not a general
    YAML tokenizer.
    """
    in_single = False
    in_double = False
    for index, ch in enumerate(text):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if index == 0 or text[index - 1] in (" ", "\t"):
                return text[:index]
    return text


def _extract_rva_yaml_field(rva_section_text: str, field_name: str) -> Optional[Any]:
    """Best-effort extraction of a single top-level RVA field's value.

    The ``## Runtime Verification Applicability`` section is not guaranteed
    to be a single well-formed YAML document as a whole -- existing Issue
    bodies mix bullet-list prose lines (``- decision: immediate``) with bare
    ``key: value`` lines, both inside and outside a ```` ```yaml ```` fence
    (e.g. ``- decision: immediate`` followed by ``applicable_acs: [AC1]`` at
    the same indentation is not one valid YAML document). To stay robust
    against this without inventing a new markup convention, this function
    locates only ``field_name``'s own line plus its nested block (lines
    indented strictly deeper than the field, blank lines included) and
    parses *that* isolated slice as its own tiny YAML document -- never the
    whole section. Returns ``None`` if the field is absent or its isolated
    slice fails to parse (never raises -- a missing/malformed field degrades
    to "absent", the same as every other RVA field check in this codebase).

    Issue #2771 PR #2780 OWNER F1 review fixed 3 false-negative patterns
    beyond the original "strictly deeper indent" design:

    - A trailing YAML comment on the field's own key line
      (``field_name: # comment``) is stripped before deciding whether an
      inline value is present (see ``_strip_unquoted_yaml_comment``).
    - A ``field_name:`` key followed by a block sequence at the *same*
      indentation (the shape PyYAML's own ``yaml.safe_dump()`` emits for a
      mapping value that is a list -- valid YAML; a sequence item does not
      need to be indented deeper than its mapping key) is now recognised as
      that field's nested block, provided the key line itself was matched
      in its plain (non-bullet) form. This same-indent continuation is
      deliberately NOT applied when the key itself was bullet-prefixed
      (``- field_name:``), because in that authoring style a *sibling*
      field at the same indentation is also written as a ``- other_field:``
      bullet -- applying the same-indent rule there would swallow the next,
      unrelated field into this one's value.
    - A ``field_name`` key that is itself written as a Markdown/YAML bullet
      item (``- field_name: value``), matching the existing authoring
      convention already used for other RVA fields such as ``- decision:``
      / ``- reason:``.
    """
    if not rva_section_text:
        return None
    lines = rva_section_text.splitlines()
    escaped_field = re.escape(field_name)
    plain_pattern = re.compile(rf"^([ \t]*){escaped_field}\s*:[ \t]*(.*)$")
    bullet_pattern = re.compile(rf"^([ \t]*)-[ \t]+{escaped_field}\s*:[ \t]*(.*)$")
    for index, line in enumerate(lines):
        is_bullet = False
        match = plain_pattern.match(line)
        if match is None:
            match = bullet_pattern.match(line)
            if match is None:
                continue
            is_bullet = True
        indent = len(match.group(1))
        inline_value = _strip_unquoted_yaml_comment(match.group(2)).strip()
        if inline_value:
            candidate = f"{field_name}: {inline_value}"
        else:
            # The key line itself is reconstructed fresh (never the raw
            # ``line``) so a bullet prefix (``- field_name:``) or a
            # comment-only suffix on the key line can never leak into the
            # isolated candidate parsed below -- only the nested nxt lines'
            # own (already comment-free by construction) indentation is
            # preserved verbatim.
            block_lines = [f"{field_name}:"]
            for nxt in lines[index + 1:]:
                if not nxt.strip():
                    block_lines.append(nxt)
                    continue
                nxt_indent = _line_indent(nxt)
                if nxt_indent > indent:
                    block_lines.append(nxt)
                    continue
                if (
                    not is_bullet
                    and nxt_indent == indent
                    and nxt.lstrip(" \t").startswith("- ")
                ):
                    block_lines.append(nxt)
                    continue
                break
            candidate = "\n".join(block_lines)
        try:
            parsed = yaml.safe_load(candidate)
        except yaml.YAMLError:
            return None
        if isinstance(parsed, dict) and field_name in parsed:
            return parsed[field_name]
        return None
    return None


_AC_TOKEN_RE = re.compile(r"^AC(\d+)$")


def extract_applicable_acs(rva_section_text: str) -> set[str]:
    """Digit-only AC numbers declared in the RVA section's ``applicable_acs``
    field (e.g. ``{"3", "5"}`` for ``applicable_acs: [AC3, AC5]``). Returns
    an empty set if the field is absent or not a list of ``AC<N>`` strings."""
    raw = _extract_rva_yaml_field(rva_section_text, "applicable_acs")
    if not isinstance(raw, list):
        return set()
    result: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            m = _AC_TOKEN_RE.match(item.strip())
            if m:
                result.add(m.group(1))
    return result


_AC_TASK_LIST_ITEM_RE = re.compile(r"^[ \t]*-[ \t]*\[[ xX]\][ \t]*AC(\d+)\b")


def extract_ac_numbers(ac_section_text: str) -> set[str]:
    """Digit-only AC numbers actually DECLARED as a task-list item's own AC
    label (e.g. ``- [ ] AC3: ...``) in the Acceptance Criteria section text.

    Issue #2771 PR #2780 OWNER F3 review: an earlier version of this
    function used a body-wide ``AC(\\d+)`` regex, which also matched
    ``AC<N>``-shaped substrings appearing in prose explaining a *previous*
    proposal (e.g. ``旧案のAC99は参考情報であり...``), a URL fragment, a
    filename, or a fenced code example -- none of which declare a real
    Acceptance Criterion. That made a binding's referential-validity check
    (``ac_not_found``) pass for a nonexistent AC as long as its number
    happened to appear anywhere in the section's text.

    This is the same known false-positive shape independently tracked for
    the ``review-issue`` C5 checker's own AC-number collection (Issue
    #1712) -- this fix is intentionally scoped ONLY to this function's own
    local AC-number extraction, not a fix to C5 itself and not a new
    general-purpose Markdown parser.

    Only the first ``AC<N>``-shaped token immediately following a
    task-list checkbox marker (``- [ ]`` / ``- [x]`` / ``- [X]``) on a
    non-fenced line counts as that item's own declared AC number -- any
    further ``AC<N>``-shaped text later in the same line (prose, examples)
    is ignored, and lines inside fenced code blocks are excluded entirely
    (a fenced example illustrating what an AC line looks like does not
    itself declare a real AC).
    """
    result: set[str] = set()
    in_fence = False
    for line in (ac_section_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _AC_TASK_LIST_ITEM_RE.match(line)
        if match:
            result.add(match.group(1))
    return result


_RUNTIME_VERIFICATION_TAG_RE = re.compile(r"<!--\s*runtime-verification:\s*true\s*-->")


def decision_level_runtime_verification_tag_consistent(
    declared_decision: Optional[str], ac_section_text: str
) -> bool:
    """Mirrors ``check_c11_decision_tag_consistency``'s existing decision-vs-
    tag pass/fail semantics (a *decision-level*, whole-Acceptance-Criteria-
    section aggregate: ``decision: immediate`` requires at least one
    ``<!-- runtime-verification: true -->`` tag somewhere in the section;
    ``not_applicable``/``deferred`` require none). Centralised here so the
    runtime assertion binding coverage gate reuses this exact existing tag
    mechanism (Issue #2771 AC5 point 3) instead of introducing a new
    per-assertion tagging convention."""
    has_rv_tag = bool(_RUNTIME_VERIFICATION_TAG_RE.search(ac_section_text or ""))
    if declared_decision == "immediate":
        return has_rv_tag
    if declared_decision in ("not_applicable", "deferred"):
        return not has_rv_tag
    return True


# Issue #2852: assertion-level applicability (`disposition`).
#
# Closed enum of how a hard-required (profile, assertion) candidate is treated
# by *this* Issue's change. This is a STRUCTURAL classification only -- it is
# never runtime PASS evidence, and the evaluator result deliberately exposes no
# field that could be read as one (structural_completeness_not_runtime_pass):
#
# - ``dispositive``: this Issue's own runtime AC verifies the assertion (the
#   pre-#2852 behaviour; ``ac`` is required). A legacy 3-field entry that omits
#   ``disposition`` is classified here with ``disposition_source:
#   legacy_default`` -- an internal compatibility reading, not "verified".
# - ``non_dispositive_readiness_compat``: another EXISTING AC owns the
#   substantive verification (``demonstrated_by: AC<N>``, plus a non-empty
#   ``reason``); never counted as runtime PASS and never given an ``ac``.
# - ``not_applicable``: the change surface has no such behaviour (non-empty
#   ``reason``; no ``ac`` / ``demonstrated_by`` so a fictional reference can
#   never be written).
#
# Whether a ``reason`` / ``demonstrated_by`` is *semantically sufficient* is
# not judged here (existing semantic review owns that); machine validation is
# a non-empty ``reason`` and an existing AC with a canonical ``# AC<N>`` VC
# reference only.
DISPOSITION_DISPOSITIVE = "dispositive"
DISPOSITION_NON_DISPOSITIVE_READINESS_COMPAT = "non_dispositive_readiness_compat"
DISPOSITION_NOT_APPLICABLE = "not_applicable"
RUNTIME_ASSERTION_DISPOSITIONS = (
    DISPOSITION_DISPOSITIVE,
    DISPOSITION_NON_DISPOSITIVE_READINESS_COMPAT,
    DISPOSITION_NOT_APPLICABLE,
)
DISPOSITION_SOURCE_EXPLICIT = "explicit"
DISPOSITION_SOURCE_LEGACY_DEFAULT = "legacy_default"

# Legacy (disposition omitted) closed key set -- unchanged from #2771.
_RUNTIME_ASSERTION_BINDING_ALLOWED_KEYS = frozenset({"profile", "assertion", "ac"})

# Closed key set per explicit disposition (Issue #2852 contract shape).
_RUNTIME_ASSERTION_BINDING_ALLOWED_KEYS_BY_DISPOSITION: dict[str, frozenset[str]] = {
    DISPOSITION_DISPOSITIVE: frozenset({"profile", "assertion", "ac", "disposition"}),
    DISPOSITION_NON_DISPOSITIVE_READINESS_COMPAT: frozenset(
        {"profile", "assertion", "disposition", "demonstrated_by", "reason"}
    ),
    DISPOSITION_NOT_APPLICABLE: frozenset({"profile", "assertion", "disposition", "reason"}),
}


def _reason_problem(index: int, entry: dict[str, Any]) -> Optional[str]:
    """Distinct malformed description for a missing / non-string / empty
    ``reason`` (Issue #2852 AC2: the missing cause is reported distinctly)."""
    prefix = f"runtime_assertion_bindings[{index}] (disposition {entry.get('disposition')!r})"
    if "reason" not in entry:
        return f"{prefix} is missing a non-empty 'reason'"
    reason = entry["reason"]
    if not isinstance(reason, str):
        return f"{prefix} declares a non-string 'reason' ({type(reason).__name__}), expected a non-empty string"
    if not reason.strip():
        return f"{prefix} declares an empty 'reason', expected a non-empty string"
    return None


def parse_runtime_assertion_bindings(
    rva_section_text: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Parse the canonical ``runtime_assertion_bindings`` wire format list.

    Canonical shapes (Issue #2771 In Scope, extended by Issue #2852): a list of
    mappings. The legacy form has exactly the three keys ``profile`` /
    ``assertion`` / ``ac`` (``disposition`` omitted; classified ``dispositive``
    with ``disposition_source: legacy_default``). An explicit ``disposition``
    selects one of three closed key sets (see
    ``_RUNTIME_ASSERTION_BINDING_ALLOWED_KEYS_BY_DISPOSITION``) -- never a
    duplicated ``vc:``/command-text field (the binding only records *which* AC
    owns or demonstrates the assertion; the VC command text itself lives in the
    existing ``## Verification Commands`` section and is cross-checked
    separately via the existing C5 / ``parse_verification_commands_section``
    parser).

    Returns ``(bindings, malformed_entry_descriptions)``. Each returned binding
    dict has ``profile`` / ``assertion`` (verbatim strings), ``disposition``
    (explicit entries only; its absence means the legacy 3-field form), and by
    disposition: ``ac`` (digit-only, e.g. ``"3"`` for ``ac: AC3``;
    dispositive), ``demonstrated_by`` (digit-only; compat) and ``reason``
    (stripped original text; compat / not_applicable). A raw
    ``runtime_assertion_bindings`` value that is present but not a list, or an
    individual entry that is not a mapping, has an out-of-enum ``disposition``,
    declares a key outside its disposition's closed set, or is
    missing/malformed in a required field, is reported as a human-readable
    string in ``malformed_entry_descriptions`` rather than silently skipped (an
    Issue-side declaration defect) -- never raised as a policy integrity
    failure, since a malformed *declaration* is always something the Issue
    author can fix.
    """
    raw = _extract_rva_yaml_field(rva_section_text, "runtime_assertion_bindings")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [
            "'runtime_assertion_bindings' is present but is not a list "
            f"(got {type(raw).__name__})"
        ]

    bindings: list[dict[str, str]] = []
    malformed: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            malformed.append(f"runtime_assertion_bindings[{index}] is not a mapping")
            continue

        if "disposition" in entry:
            disposition = entry["disposition"]
            if not isinstance(disposition, str) or disposition not in RUNTIME_ASSERTION_DISPOSITIONS:
                malformed.append(
                    f"runtime_assertion_bindings[{index}] declares disposition {disposition!r}, "
                    f"expected one of {list(RUNTIME_ASSERTION_DISPOSITIONS)}"
                )
                continue
            disposition_source = DISPOSITION_SOURCE_EXPLICIT
            allowed_keys = _RUNTIME_ASSERTION_BINDING_ALLOWED_KEYS_BY_DISPOSITION[disposition]
        else:
            disposition = DISPOSITION_DISPOSITIVE
            disposition_source = DISPOSITION_SOURCE_LEGACY_DEFAULT
            allowed_keys = _RUNTIME_ASSERTION_BINDING_ALLOWED_KEYS

        extra_keys = set(entry.keys()) - allowed_keys
        if extra_keys:
            malformed.append(
                f"runtime_assertion_bindings[{index}] declares unexpected key(s) "
                f"{sorted(extra_keys, key=str)} for disposition {disposition!r} "
                f"(closed key set is {sorted(allowed_keys)}, Issue #2771 / #2852)"
            )
            continue
        profile = entry.get("profile")
        assertion = entry.get("assertion")
        if not isinstance(profile, str) or not profile.strip():
            malformed.append(f"runtime_assertion_bindings[{index}] is missing a non-empty 'profile'")
            continue
        if not isinstance(assertion, str) or not assertion.strip():
            malformed.append(f"runtime_assertion_bindings[{index}] is missing a non-empty 'assertion'")
            continue

        # A legacy 3-field entry keeps its pre-#2852 parsed shape (profile /
        # assertion / ac only); an explicit entry additionally carries its
        # `disposition`. `disposition_source` is derived from that presence
        # (`_binding_disposition`), never stored twice.
        binding: dict[str, str] = {
            "profile": profile.strip(),
            "assertion": assertion.strip(),
        }
        if disposition_source == DISPOSITION_SOURCE_EXPLICIT:
            binding["disposition"] = disposition

        if disposition == DISPOSITION_DISPOSITIVE:
            ac = entry.get("ac")
            if not isinstance(ac, str) or not _AC_TOKEN_RE.match(ac.strip()):
                malformed.append(
                    f"runtime_assertion_bindings[{index}] declares 'ac' as {ac!r}, expected 'AC<N>' form"
                )
                continue
            binding["ac"] = _AC_TOKEN_RE.match(ac.strip()).group(1)
        elif disposition == DISPOSITION_NON_DISPOSITIVE_READINESS_COMPAT:
            demonstrated_by = entry.get("demonstrated_by")
            if "demonstrated_by" not in entry:
                malformed.append(
                    f"runtime_assertion_bindings[{index}] (disposition {disposition!r}) is missing "
                    "'demonstrated_by' (an existing 'AC<N>' reference)"
                )
                continue
            if not isinstance(demonstrated_by, str) or not demonstrated_by.strip():
                malformed.append(
                    f"runtime_assertion_bindings[{index}] declares an empty or non-string "
                    f"'demonstrated_by' ({demonstrated_by!r}), expected 'AC<N>' form"
                )
                continue
            token = _AC_TOKEN_RE.match(demonstrated_by.strip())
            if token is None:
                malformed.append(
                    f"runtime_assertion_bindings[{index}] declares 'demonstrated_by' as "
                    f"{demonstrated_by!r}, expected 'AC<N>' form (test path / node-id references "
                    "are not accepted)"
                )
                continue
            problem = _reason_problem(index, entry)
            if problem:
                malformed.append(problem)
                continue
            binding["demonstrated_by"] = token.group(1)
            binding["reason"] = entry["reason"].strip()
        else:  # DISPOSITION_NOT_APPLICABLE
            problem = _reason_problem(index, entry)
            if problem:
                malformed.append(problem)
                continue
            binding["reason"] = entry["reason"].strip()

        bindings.append(binding)
    return bindings, malformed


def _binding_disposition(binding: dict[str, str]) -> tuple[str, str]:
    """``(disposition, disposition_source)`` of a parsed binding: an explicit
    ``disposition`` key is ``explicit``; its absence is the legacy 3-field
    form, read as ``dispositive`` with ``legacy_default`` (internal
    compatibility only -- never "verified")."""
    if "disposition" in binding:
        return binding["disposition"], DISPOSITION_SOURCE_EXPLICIT
    return DISPOSITION_DISPOSITIVE, DISPOSITION_SOURCE_LEGACY_DEFAULT


def _classified_binding_view(binding: dict[str, str]) -> dict[str, Any]:
    """Public, evidence-free view of one parsed binding (Issue #2852 AC1/AC6).

    Only the classification and the author-declared references are exposed
    (``ac`` for dispositive, ``demonstrated_by`` for compat, ``reason`` for
    compat / not_applicable). No field here means "executed", "observed" or
    "verified" -- the declaration is structural only.
    """
    disposition, disposition_source = _binding_disposition(binding)
    view: dict[str, Any] = {
        "profile": binding["profile"],
        "assertion": binding["assertion"],
        "disposition": disposition,
        "disposition_source": disposition_source,
    }
    if "ac" in binding:
        view["ac"] = f"AC{binding['ac']}"
    if "demonstrated_by" in binding:
        view["demonstrated_by"] = f"AC{binding['demonstrated_by']}"
    if "reason" in binding:
        view["reason"] = binding["reason"]
    return view


def evaluate_runtime_assertion_binding_coverage(
    allowed_path_entries: list[str],
    rva_section_text: str,
    ac_section_text: str,
    ac_vc_refs: set[str],
    policy: Optional[dict[str, Any]] = None,
    ac_vc_commands: Optional[dict[str, tuple[str, ...]]] = None,
) -> dict[str, Any]:
    """High-level verdict shared verbatim by both consumers (Issue #2771,
    mirrors ``evaluate_issue_risk_trigger``'s existing cross-consumer parity
    pattern for Issue #2290).

    This function performs STRUCTURAL COMPLETENESS checking only
    (structural_completeness_not_semantic_sufficiency): it verifies that
    every hard-required ``(verification_profile_id, assertion_id)`` pair is
    explicitly bound to a real, referentially-valid Acceptance Criterion via
    ``runtime_assertion_bindings``. It does not, and cannot, judge whether
    the bound VC actually *proves* the assertion's semantic postcondition
    (that remains existing semantic review's responsibility, Issue #2771
    Outcome / AC9 / AC10).

    Issue #2852: each binding carries a closed-enum ``disposition``
    (``dispositive`` / ``non_dispositive_readiness_compat`` /
    ``not_applicable``; legacy 3-field entries are ``dispositive`` with
    ``disposition_source: legacy_default``). Every disposition declares its
    own required key, but only that key: ``not_applicable`` / compat never
    hide another required assertion's ``missing`` nor any unknown /
    duplicate / malformed / invalid finding. The result exposes the
    classification (``classified_bindings`` and per-disposition assertion
    lists) and NO runtime-PASS-evidence field. ``evaluate_issue_risk_trigger``
    is intentionally unaffected (Issue-level decision requirements stay).

    ``ac_vc_refs`` is the caller-supplied, digit-only set of AC numbers that
    the ``## Verification Commands`` section references via a canonical
    ``# AC<N>`` comment (the same set already produced by the existing
    ``vc_contract_syntax.parse_verification_commands_section`` / C5 parser
    both consumers already import) -- passed in rather than re-parsed here
    so this module does not need a new cross-directory import dependency on
    top of its existing ``changed_file_matcher`` sibling import.

    Raises ``PolicyLoadError`` (propagated from
    ``derive_required_runtime_assertions``) for a policy integrity defect;
    callers MUST catch this separately from the returned ``verdict`` so an
    Issue-side declaration defect (``needs_fix``, returned normally) is
    never confused with a policy-side defect the Issue author cannot fix
    (Issue #2771 AC6).
    """
    policy_data = policy if policy is not None else load_policy()
    policy_evaluation = evaluate_allowed_paths(allowed_path_entries, policy=policy_data)
    # Issue #2961: the SAME shared exemption helper as
    # `evaluate_issue_risk_trigger()`; an exempted rule is excluded from the
    # required-assertion derivation only (other rules stay hard).
    exemptions, exemption_rejections = evaluate_issue_time_exemptions(
        policy_evaluation["matched_rules"],
        policy_data,
        rva_section_text,
        ac_section_text,
        ac_vc_commands,
    )
    required = derive_required_runtime_assertions(
        _matched_rules_without_exempted(policy_evaluation["matched_rules"], exemptions)
        if exemptions
        else policy_evaluation["matched_rules"],
        policy_data,
    )

    declared_decision: Optional[str] = None
    decision_match = re.search(r"decision:\s*(\S+)", rva_section_text or "")
    if decision_match:
        declared_decision = decision_match.group(1).strip()

    bindings, malformed_entries = parse_runtime_assertion_bindings(rva_section_text)

    ac_numbers = extract_ac_numbers(ac_section_text)
    applicable_acs = extract_applicable_acs(rva_section_text)
    tag_consistent = decision_level_runtime_verification_tag_consistent(
        declared_decision, ac_section_text
    )

    declared_key_counts: dict[tuple[str, str], int] = {}
    for binding in bindings:
        key = (binding["profile"], binding["assertion"])
        declared_key_counts[key] = declared_key_counts.get(key, 0) + 1
    # Every disposition declares its (profile, assertion) key, but a
    # not_applicable / compat declaration only covers THAT key: it can never
    # hide another required assertion's `missing`, nor any
    # unknown / duplicate / malformed / invalid finding (Issue #2852 AC4/AC7).
    declared_keys = set(declared_key_counts.keys())

    missing = sorted(required - declared_keys)
    unknown = sorted(declared_keys - required)
    duplicate = sorted(key for key, count in declared_key_counts.items() if count > 1)

    invalid_ac_bindings: list[dict[str, Any]] = []
    invalid_demonstrated_by_bindings: list[dict[str, Any]] = []
    for binding in bindings:
        disposition, _source = _binding_disposition(binding)
        if disposition == DISPOSITION_DISPOSITIVE:
            ac_digit = binding["ac"]
            reasons: list[str] = []
            if ac_digit not in ac_numbers:
                reasons.append("ac_not_found")
            if ac_digit not in applicable_acs:
                reasons.append("ac_not_in_applicable_acs")
            if not tag_consistent:
                reasons.append("runtime_verification_tag_inconsistent")
            if ac_digit not in ac_vc_refs:
                reasons.append("ac_missing_vc_reference")
            if reasons:
                invalid_ac_bindings.append(
                    {
                        "profile": binding["profile"],
                        "assertion": binding["assertion"],
                        "ac": f"AC{ac_digit}",
                        "reasons": reasons,
                    }
                )
        elif disposition == DISPOSITION_NON_DISPOSITIVE_READINESS_COMPAT:
            # `demonstrated_by` must name an EXISTING AC that owns a canonical
            # `# AC<N>` Verification Commands reference. It is deliberately NOT
            # required to be in `applicable_acs` / carry the runtime tag (the
            # other AC is an ordinary deterministic test, not a runtime AC),
            # and no test-path / node-id resolver is involved.
            demonstrated_digit = binding["demonstrated_by"]
            reasons = []
            if demonstrated_digit not in ac_numbers:
                reasons.append("demonstrated_by_ac_not_found")
            if demonstrated_digit not in ac_vc_refs:
                reasons.append("demonstrated_by_ac_missing_vc_reference")
            if reasons:
                invalid_demonstrated_by_bindings.append(
                    {
                        "profile": binding["profile"],
                        "assertion": binding["assertion"],
                        "demonstrated_by": f"AC{demonstrated_digit}",
                        "reasons": reasons,
                    }
                )

    reasons_out: list[str] = []
    if missing:
        reasons_out.append(
            "missing runtime_assertion_bindings for hard-required (profile, assertion) pairs: "
            + ", ".join(f"{p}/{a}" for p, a in missing)
        )
    if unknown:
        reasons_out.append(
            "declared runtime_assertion_bindings reference (profile, assertion) pairs that are "
            "not hard-required: " + ", ".join(f"{p}/{a}" for p, a in unknown)
        )
    if duplicate:
        reasons_out.append(
            "duplicate runtime_assertion_bindings declared for the same (profile, assertion) key "
            "(1 key = 1 binding; declaring the same key more than once is invalid regardless of "
            "disposition or whether the bound ac matches): " + ", ".join(f"{p}/{a}" for p, a in duplicate)
        )
    reasons_out.extend(malformed_entries)
    for entry in invalid_ac_bindings:
        reasons_out.append(
            f"runtime_assertion_bindings entry {entry['profile']}/{entry['assertion']} -> "
            f"{entry['ac']} is invalid: {', '.join(entry['reasons'])}"
        )
    for entry in invalid_demonstrated_by_bindings:
        reasons_out.append(
            f"runtime_assertion_bindings entry {entry['profile']}/{entry['assertion']} "
            f"(disposition {DISPOSITION_NON_DISPOSITIVE_READINESS_COMPAT}) -> demonstrated_by "
            f"{entry['demonstrated_by']} is invalid: {', '.join(entry['reasons'])}"
        )

    verdict = "needs_fix" if reasons_out else "approve"

    classified = [_classified_binding_view(b) for b in bindings]

    def _assertions_for(disposition: str) -> list[str]:
        return [
            f"{c['profile']}/{c['assertion']}" for c in classified if c["disposition"] == disposition
        ]

    result: dict[str, Any] = {
        "schema": SCHEMA_RUNTIME_ASSERTION_BINDING_COVERAGE,
        "verdict": verdict,
        # STRUCTURAL completeness only: no field of this result is runtime
        # PASS evidence (Issue #2852 AC1 / structural checker PASS != runtime
        # verification PASS).
        "structural_completeness_only": True,
        "required_assertions": sorted(f"{p}/{a}" for p, a in required),
        "declared_bindings": [
            {k: v for k, v in c.items() if k in ("profile", "assertion", "ac")} for c in classified
        ],
        "classified_bindings": classified,
        "dispositive_assertions": _assertions_for(DISPOSITION_DISPOSITIVE),
        "non_dispositive_readiness_compat_assertions": _assertions_for(
            DISPOSITION_NON_DISPOSITIVE_READINESS_COMPAT
        ),
        "not_applicable_assertions": _assertions_for(DISPOSITION_NOT_APPLICABLE),
        "missing": [f"{p}/{a}" for p, a in missing],
        "unknown": [f"{p}/{a}" for p, a in unknown],
        "duplicate": [f"{p}/{a}" for p, a in duplicate],
        "malformed_binding_entries": malformed_entries,
        "invalid_ac_bindings": invalid_ac_bindings,
        "invalid_demonstrated_by_bindings": invalid_demonstrated_by_bindings,
        "reasons": reasons_out,
    }
    if exemptions:
        result["issue_time_exemptions"] = exemptions
    if exemption_rejections:
        result["issue_time_exemption_rejections"] = exemption_rejections
    return result


# ---------------------------------------------------------------------------
# Disposition classification carrier (Issue #2852 AC9 / AC10)
# ---------------------------------------------------------------------------

# One identity for the non-blocking carrier on BOTH sides: the readiness error
# `category` and the review-issue `non_blocking_improvements[].code`.
RUNTIME_ASSERTION_DISPOSITION_CARRIER_CODE = "runtime_assertion_disposition_classification"


def _normalize_carrier_reason(reason: str) -> str:
    """Collapse every whitespace run (including newlines) to one space so one
    binding is always exactly one carrier line. ``;`` is left untouched:
    ``reason`` is the LAST field of the line, so a ``;`` inside it cannot
    shift any earlier field (the line parses with fixed-order leading tokens
    and ``reason=(.*)$``)."""
    return " ".join(reason.split())


def format_runtime_assertion_disposition_carrier_lines(evaluation: dict[str, Any]) -> list[str]:
    """Project one ``evaluate_runtime_assertion_binding_coverage()`` result
    into the non-blocking carrier ``evidence`` lines (Issue #2852 AC9).

    One line per classified binding::

        <profile>/<assertion>: disposition=<v>; source=<explicit|legacy_default>;
        demonstrated_by=<AC<N>|->; reason=<text|->

    (a single physical line). Returns ``[]`` unless the structural evaluation
    APPROVED -- the carrier never decorates a ``needs_fix`` result -- and at
    least one binding is explicit / not_applicable / compat (a purely legacy
    3-field input keeps its pre-#2852 output unchanged). Shared by both
    checkers so the text cannot drift between them. Carries classification
    only; it is not runtime PASS evidence.
    """
    if evaluation.get("verdict") != "approve":
        return []
    classified = evaluation.get("classified_bindings") or []
    if not any(
        c["disposition_source"] == DISPOSITION_SOURCE_EXPLICIT
        or c["disposition"] != DISPOSITION_DISPOSITIVE
        for c in classified
    ):
        return []
    lines: list[str] = []
    for c in classified:
        reason = c.get("reason")
        lines.append(
            f"{c['profile']}/{c['assertion']}: disposition={c['disposition']}; "
            f"source={c['disposition_source']}; "
            f"demonstrated_by={c.get('demonstrated_by') or '-'}; "
            f"reason={_normalize_carrier_reason(reason) if reason else '-'}"
        )
    return lines
