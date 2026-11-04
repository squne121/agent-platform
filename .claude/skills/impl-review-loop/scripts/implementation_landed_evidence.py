#!/usr/bin/env python3
"""Strict, read-only evidence for the impl-review-loop pre-Step-1 choke point.

`IMPLEMENTATION_SCOPE_COVERAGE_V1` is deliberately a PR-publication record,
not an intake-time reconstruction of historical Issue text.  This module owns
the normalizer used by both open-pr (producer) and intake (consumer).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

# Issue #2783: shared no-path marker predicate (`scripts/agent-ops/`). Pure
# policy library only -- not a grammar library import.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENT_OPS_DIR = _REPO_ROOT / "scripts" / "agent-ops"
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))
_AGENT_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))
from allowed_paths_policy import is_no_path_marker  # noqa: E402
from changed_file_matcher import AllowedPathsMatcher  # noqa: E402

EVIDENCE_SCHEMA = "IMPLEMENTATION_LANDED_EVIDENCE_V1"
COVERAGE_SCHEMA = "IMPLEMENTATION_SCOPE_COVERAGE_V1"
MANIFEST_SCHEMA = "IMPLEMENTATION_SCOPE_MANIFEST_V1"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$", re.I)
_FENCE_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.S | re.I)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _body_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_DIGEST_RE.fullmatch(value))


def _section(body: str, heading: str) -> str:
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$", body, re.M | re.I)
    if match is None:
        return ""
    following = _SECTION_RE.search(body, match.end())
    return body[match.end() : following.start() if following else len(body)]


def _list_items(text: str, *, acceptance: bool = False) -> list[str]:
    values: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*[-*]\s+(?:\[[ xX]\]\s*)?(.*\S)\s*$", line)
        if match is None:
            continue
        value = match.group(1).strip()
        if acceptance:
            value = re.sub(r"^AC\d+\s*:\s*", "", value, flags=re.I)
            value = re.sub(r"<!--.*?-->", "", value).strip()
        value = value.replace("`", "")
        if value:
            values.append(value)
    return sorted(set(values))


def _machine_contract(body: str) -> Mapping[str, Any]:
    for fence in _FENCE_RE.findall(body):
        try:
            import yaml  # PyYAML is already used by repository skill tooling.

            parsed = yaml.safe_load(fence)
        except Exception:
            continue
        if isinstance(parsed, Mapping) and "goal_ref" in parsed:
            return parsed
    return {}


def build_scope_manifest(issue_body: str) -> dict[str, Any]:
    """Return the exact semantic scope contract, excluding operational prose.

    Lists are sorted before serialization, so checkbox state and list ordering
    cannot turn a progress-only Issue edit into a scope expansion.
    """
    contract = _machine_contract(issue_body)
    return {
        "schema_version": MANIFEST_SCHEMA,
        "goal_ref": str(contract.get("goal_ref") or "").strip(),
        "change_kind": str(contract.get("change_kind") or "").strip(),
        "in_scope": _list_items(_section(issue_body, "In Scope")),
        "acceptance_criteria": _list_items(_section(issue_body, "Acceptance Criteria"), acceptance=True),
        # Issue #2783: a canonical `(none)` / legacy
        # `読み取り専用。リポジトリ変更なし（既定）` no-path marker entry
        # contributes 0 allowed_paths, unlike "In Scope" / "Acceptance
        # Criteria" bullets (where `_list_items()` above is intentionally
        # unmodified -- this filtering is Allowed-Paths-specific, not a
        # change to the shared list-item extractor).
        "allowed_paths": [
            p for p in _list_items(_section(issue_body, "Allowed Paths")) if not is_no_path_marker(p)
        ],
    }


def canonicalize_scope_manifest(scope: Any) -> dict[str, Any]:
    """Compatibility entry point; bodies receive the fixed semantic manifest."""
    manifest = build_scope_manifest(scope) if isinstance(scope, str) else scope
    if not isinstance(manifest, Mapping):
        manifest = {"invalid_scope": True}

    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): normalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
        if isinstance(value, (list, tuple, set, frozenset)):
            items = [normalize(item) for item in value]
            return sorted(
                items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
        return value

    normalized = normalize(manifest)
    return {"manifest": normalized, "content_sha256": _digest(normalized)}


def normalize_scope_coverage(immutable_merged_snapshot: Any, live_current_scope: Any) -> dict[str, Any]:
    merged = canonicalize_scope_manifest(immutable_merged_snapshot)
    current = canonicalize_scope_manifest(live_current_scope)
    exact = merged["content_sha256"] == current["content_sha256"]
    return {
        "schema": COVERAGE_SCHEMA,
        "immutable_merged_snapshot": merged,
        "live_current_scope": current,
        "exact_coverage": exact,
        "later_scope_expansion": not exact,
        "status": "covered_exactly" if exact else "later_scope_expansion",
    }


def build_scope_coverage_marker(*, issue_number: int, issue_body: str, pr_head_sha: str) -> dict[str, Any]:
    manifest = build_scope_manifest(issue_body)
    return {
        COVERAGE_SCHEMA: {
            "schema_version": COVERAGE_SCHEMA,
            "issue_number": issue_number,
            "issue_body_sha256": _body_digest(issue_body),
            "normalized_scope_manifest_sha256": _digest(manifest),
            "pr_head_sha": pr_head_sha,
            "scope_manifest": manifest,
        }
    }


def render_scope_coverage_marker(marker: Mapping[str, Any]) -> str:
    """Render a YAML block without adding a serialization dependency."""
    payload = marker.get(COVERAGE_SCHEMA) if isinstance(marker, Mapping) else None
    if not isinstance(payload, Mapping):
        raise ValueError("invalid scope coverage marker")
    # JSON is a YAML subset; using it for nested objects keeps canonical values
    # byte-for-byte visible and is accepted by the existing PR-body validator.
    lines = [f"{COVERAGE_SCHEMA}:"]
    for key in (
        "schema_version",
        "issue_number",
        "issue_body_sha256",
        "normalized_scope_manifest_sha256",
        "pr_head_sha",
    ):
        lines.append(f"  {key}: {json.dumps(payload.get(key), ensure_ascii=False)}")
    lines.append("  scope_manifest: " + json.dumps(payload.get("scope_manifest"), ensure_ascii=False, sort_keys=True))
    return "```yaml\n" + "\n".join(lines) + "\n```"


def _parse_marker(pr_body: str, *, issue_number: int) -> tuple[dict[str, Any] | None, list[str]]:
    matches: list[Any] = []
    # Issue #2811: a fence that names the marker schema but is not parseable YAML is a
    # malformed marker candidate, not an absent marker (fail-closed, never `marker_missing`).
    unparsable_candidates = 0
    for fence in _FENCE_RE.findall(pr_body or ""):
        try:
            import yaml

            parsed = yaml.safe_load(fence)
        except Exception:
            if COVERAGE_SCHEMA in fence:
                unparsable_candidates += 1
            continue
        if isinstance(parsed, Mapping) and COVERAGE_SCHEMA in parsed:
            matches.append(parsed[COVERAGE_SCHEMA])
    if unparsable_candidates:
        return None, ["scope_coverage_marker_ambiguous_or_invalid"]
    if not matches:
        return None, ["scope_coverage_marker_missing"]
    if len(matches) != 1 or not isinstance(matches[0], Mapping):
        return None, ["scope_coverage_marker_ambiguous_or_invalid"]
    marker = dict(matches[0])
    errors: list[str] = []
    if marker.get("schema_version") != COVERAGE_SCHEMA:
        errors.append("scope_coverage_schema_version_invalid")
    if marker.get("issue_number") != issue_number:
        errors.append("scope_coverage_issue_identity_mismatch")
    if not _valid_digest(marker.get("issue_body_sha256")):
        errors.append("scope_coverage_issue_body_digest_invalid")
    if not _valid_sha(marker.get("pr_head_sha")):
        errors.append("scope_coverage_pr_head_invalid")
    manifest = marker.get("scope_manifest")
    if not isinstance(manifest, Mapping) or _digest(dict(manifest)) != marker.get("normalized_scope_manifest_sha256"):
        errors.append("scope_coverage_manifest_digest_mismatch")
    return marker if not errors else None, errors


def coverage_from_pr_body(*, pr_body: str, issue_number: int, live_issue_body: str) -> dict[str, Any]:
    marker, errors = _parse_marker(pr_body, issue_number=issue_number)
    if marker is None:
        return {
            "status": "missing_marker" if errors == ["scope_coverage_marker_missing"] else "invalid",
            "errors": errors,
        }
    immutable = marker["scope_manifest"]
    coverage = normalize_scope_coverage(immutable, live_issue_body)
    coverage.update({"marker": marker, "errors": []})
    return coverage


def _allowed_paths_covered(allowed_paths: list[str], files: Any) -> bool:
    """Each entry must match a PR file; this is not a changed-file allowlist.

    Wildcard-free entries retain the legacy exact/directory-prefix behavior.
    Explicit wildcards use the shared matcher's safe segment grammar. Both
    branches normalize repo-relative paths and reject invalid inputs.
    """
    if not allowed_paths or not isinstance(files, list):
        return False
    paths = [
        normalized
        for file in files
        if isinstance(file, Mapping) and isinstance(file.get("path"), str)
        if (normalized := AllowedPathsMatcher.normalize_path(file["path"])) is not None
    ]
    for entry in allowed_paths:
        if not isinstance(entry, str):
            return False
        if "*" in entry:
            pattern = AllowedPathsMatcher.normalize_allowed_pattern(entry)
            if pattern is None or not any(AllowedPathsMatcher.matches_pattern(path, pattern) for path in paths):
                return False
        else:
            # A plain trailing slash retains the legacy directory-prefix rule;
            # strip exactly one so repeated slashes still fail normalization.
            plain = AllowedPathsMatcher.normalize_path(entry[:-1] if entry.endswith("/") else entry)
            if plain is None or not any(path == plain or path.startswith(plain + "/") for path in paths):
                return False
    return True


def _candidate_errors(candidate: Any, repo: str, issue_number: int) -> list[str]:
    if not isinstance(candidate, Mapping):
        return ["candidate_not_mapping"]
    errors: list[str] = []
    target = candidate.get("target")
    if not isinstance(target, Mapping) or target.get("repo") != repo or target.get("issue_number") != issue_number:
        errors.append("candidate_target_identity_mismatch")
    provenance = candidate.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("kind") not in {"closing_relation", "verified_cross_reference"}
        or provenance.get("verified") is not True
    ):
        errors.append("candidate_provenance_unverified")
    lifecycle = candidate.get("lifecycle")
    if lifecycle not in {"merged", "open", "draft", "closed_unmerged"}:
        errors.append("candidate_lifecycle_invalid")
    pr = candidate.get("pr")
    if not isinstance(pr, Mapping) or type(pr.get("number")) is not int or pr["number"] <= 0:
        errors.append("candidate_pr_identity_invalid")
    if lifecycle == "merged" and candidate.get("qualified_irrelevant_sibling") is not True:
        # research Issue #2761: a candidate qualified as an irrelevant
        # sibling cross-reference (#2750 carve-out) never has its main
        # ancestry compared to `main` in the first place (skip-by-
        # construction in `collect_candidate_inputs()`), so this check must
        # not apply to it -- otherwise an irrelevant sibling's deliberately-
        # unfetched ancestry would force the whole evidence set invalid.
        ancestry = candidate.get("main_ancestry")
        if (
            not _valid_sha(candidate.get("merge_oid"))
            or not isinstance(ancestry, Mapping)
            or ancestry.get("verified") is not True
        ):
            errors.append("merged_candidate_main_ancestry_unverified")
    return errors


def parse_implementation_landed_evidence(raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, ["evidence_invalid_json"]
    if not isinstance(raw, Mapping):
        return None, ["evidence_not_mapping"]
    result = dict(raw)
    if result.get("schema") != EVIDENCE_SCHEMA or result.get("schema_version") != 1:
        return None, ["evidence_schema_invalid"]
    return result, []


def validate_implementation_landed_evidence(
    raw: Any, *, repo: str, issue_number: int, now: dt.datetime | None = None
) -> dict[str, Any]:
    evidence, errors = parse_implementation_landed_evidence(raw)
    if evidence is None:
        return {"valid": False, "errors": errors, "evidence": None}
    target = evidence.get("target")
    if (
        not isinstance(target, Mapping)
        or target.get("repo") != repo
        or target.get("issue_number") != issue_number
        or not _valid_digest(target.get("body_sha256"))
    ):
        errors.append("target_identity_or_body_digest_invalid")
    freshness = evidence.get("freshness")
    if not isinstance(freshness, Mapping) or freshness.get("status") != "fresh":
        errors.append("evidence_stale_or_missing_freshness")
    if evidence.get("contradictory") is True:
        errors.append("evidence_contradictory")
    if not isinstance(evidence.get("candidates"), list):
        errors.append("candidates_not_list")
    else:
        for index, candidate in enumerate(evidence["candidates"]):
            errors.extend(f"candidate[{index}]:{err}" for err in _candidate_errors(candidate, repo, issue_number))
    rebind = evidence.get("decision_time_rebind")
    if rebind is not None and (not isinstance(rebind, Mapping) or rebind.get("status") != "fresh"):
        errors.append("freshness_rebind_failed")
    return {"valid": not errors, "errors": errors, "evidence": evidence}


def _coverage_for(candidate: Mapping[str, Any], evidence: Mapping[str, Any]) -> Mapping[str, Any] | None:
    local = candidate.get("scope_coverage")
    if isinstance(local, Mapping):
        return local
    global_coverage = evidence.get("scope_coverage")
    return global_coverage if isinstance(global_coverage, Mapping) else None


def _is_irrelevant_cross_reference(candidate: Mapping[str, Any]) -> bool:
    """#2750 bounded carve-out to the #2699 P1-1 fail-closed contract below.

    A `verified_cross_reference` candidate (never a `closing_relation`
    candidate -- callers must only apply this when no closing candidate is
    present) whose own durable `IMPLEMENTATION_SCOPE_COVERAGE_V1` marker
    names a *different* Issue, with no other marker error, is not ambiguous
    landing-authority evidence for the current target Issue -- it is a
    precedent/reference cross-reference that merely mentions the current
    Issue's PR/Issue graph without claiming to land it. Deliberately reads
    only the candidate-local `candidate["scope_coverage"]` (never
    `_coverage_for()`'s evidence-level fallback) so this qualification
    decision can never be satisfied by an unrelated global marker.

    Only fires when the marker parse failed for exactly one reason
    (`scope_coverage_issue_identity_mismatch`); any additional marker error
    (schema/digest/manifest/etc.) keeps the candidate in conflict counting
    (fail-closed), and a missing marker (markerless legacy candidate, #2119 /
    PR #2137 / #2699) is never treated as irrelevant by this check.
    """
    if candidate.get("provenance", {}).get("kind") != "verified_cross_reference":
        return False
    coverage = candidate.get("scope_coverage")
    if not isinstance(coverage, Mapping) or coverage.get("status") != "invalid":
        return False
    return list(coverage.get("errors") or []) == ["scope_coverage_issue_identity_mismatch"]


def _has_valid_same_issue_scope_coverage(candidate: Mapping[str, Any], issue_number: int) -> bool:
    """Candidate-local scope coverage derived from a valid same-Issue marker.

    Reads only `candidate["scope_coverage"]` (never the evidence-level
    fallback of `_coverage_for()`), so the decision can never be satisfied by
    an unrelated global coverage. A valid coverage has a coverage status
    (`covered_exactly` / `later_scope_expansion`, i.e. neither `invalid` nor
    `missing_marker`), no errors, and the parsed `marker` attached as a
    Mapping that names the current target Issue. A coverage whose `marker`
    key is absent (or not a Mapping) is NOT valid here (P3): the #2893
    carve-out must be backed by a positively verified same-Issue marker, never
    by a status string alone. This helper is #2893-carve-out-specific; the
    #2750 `_is_irrelevant_cross_reference()` and the markerless legacy
    open/draft path do not use it and are unchanged.
    """
    coverage = candidate.get("scope_coverage")
    if not isinstance(coverage, Mapping):
        return False
    if coverage.get("status") not in {"covered_exactly", "later_scope_expansion"} or coverage.get("errors"):
        return False
    marker = coverage.get("marker")
    return isinstance(marker, Mapping) and marker.get("issue_number") == issue_number


def _is_historical_merged_later_scope_candidate(candidate: Mapping[str, Any], issue_number: int) -> bool:
    """True only for a main-reachable merged candidate whose own valid
    same-Issue marker records `later_scope_expansion` (and not exact coverage)."""
    if (
        candidate.get("provenance", {}).get("kind") != "verified_cross_reference"
        or candidate.get("lifecycle") != "merged"
    ):
        return False
    if not _has_valid_same_issue_scope_coverage(candidate, issue_number):
        return False
    ancestry = candidate.get("main_ancestry")
    if (
        not isinstance(ancestry, Mapping)
        or ancestry.get("verified") is not True
        or ancestry.get("reachable") is not True
    ):
        return False
    coverage = candidate["scope_coverage"]
    return coverage.get("later_scope_expansion") is True and coverage.get("exact_coverage") is not True


def _is_current_exact_resume_candidate(candidate: Mapping[str, Any], issue_number: int) -> bool:
    """True only for a fresh open/draft candidate whose own valid same-Issue
    marker exactly covers the current scope."""
    return (
        candidate.get("provenance", {}).get("kind") == "verified_cross_reference"
        and candidate.get("lifecycle") in {"open", "draft"}
        and candidate.get("head_fresh") is True
        and _has_valid_same_issue_scope_coverage(candidate, issue_number)
        and candidate["scope_coverage"].get("exact_coverage") is True
    )


def _exclude_historical_merged_later_scope_expansion(
    qualified: list[dict[str, Any]], issue_number: int
) -> list[dict[str, Any]]:
    """#2893 historical merged later_scope_expansion carve-out.

    A second, bounded exclusion applied (after the #2750 irrelevant-sibling
    exclusion) to the local `qualified` list used for conflict counting and
    authority selection, only when no `closing_relation` candidate exists.
    A merged candidate is a *historical* candidate -- excluded from conflict
    counting, never from `evidence["candidates"]` (the evidence object is not
    mutated, so the candidate stays available for debug/audit) -- only when
    ALL of the following hold:

    - `provenance.kind == verified_cross_reference` and `lifecycle == merged`;
    - its own candidate-local `scope_coverage` is valid (status is not
      `invalid`/`missing_marker`, no errors) and carries a valid marker for
      the same target Issue;
    - `main_ancestry` is verified and reachable from current main;
    - `scope_coverage.later_scope_expansion is True` and
      `exact_coverage is not True`;

    AND, after removing every such historical candidate, exactly one
    qualified candidate remains and it is a current exact resume candidate:
    lifecycle `open`/`draft`, `verified_cross_reference`, `head_fresh is
    True`, a valid same-Issue marker, and `exact_coverage is True`.

    Non-change conditions (the list is returned untouched, i.e. existing
    fail-closed semantics): two or more current exact open/draft candidates
    (`qualified_candidate_conflict` stays); any other remaining candidate
    (e.g. closed-unmerged); a merged candidate that is exact-current,
    malformed/invalid, stale, identity-mismatched, markerless, or not
    main-reachable; a non-exact or stale open/draft candidate. This helper
    is never reached when a `closing_relation` candidate exists, adds no
    schema/registry/flag/override, and the surviving candidate still goes
    through the existing single-candidate open/draft path.
    """
    historical = [c for c in qualified if _is_historical_merged_later_scope_candidate(c, issue_number)]
    if not historical:
        return qualified
    remaining = [c for c in qualified if not any(c is h for h in historical)]
    if len(remaining) == 1 and _is_current_exact_resume_candidate(remaining[0], issue_number):
        return remaining
    return qualified


def derive_landing_disposition(
    raw: Any, *, repo: str, issue_number: int, now: dt.datetime | None = None
) -> dict[str, Any]:
    validated = validate_implementation_landed_evidence(raw, repo=repo, issue_number=issue_number, now=now)
    if not validated["valid"]:
        return {"disposition": "reconciliation_required", "reason_codes": validated["errors"], "candidate": None}
    evidence = validated["evidence"]
    candidates = evidence["candidates"]
    qualified = [c for c in candidates if c["lifecycle"] in {"merged", "open", "draft", "closed_unmerged"}]
    # Structured closing linkage has explicit GitHub landing semantics and is
    # therefore selected ahead of an otherwise valid timeline cross-reference.
    # Ambiguity is only among candidates at the chosen authority level.
    closing = [c for c in qualified if c.get("provenance", {}).get("kind") == "closing_relation"]
    if closing:
        qualified = closing
    else:
        # #2750 bounded carve-out: only applied when no `closing_relation`
        # candidate exists for the current target Issue. When a closing
        # candidate is present, its own identity mismatch stays a genuine
        # contradiction (fail-closed, unchanged from #2699 P1-1) -- this
        # branch never runs in that case. Excluding irrelevant sibling
        # cross-references here (before conflict counting, across every
        # qualified lifecycle: merged/open/draft/closed_unmerged) is what
        # prevents #2727-shaped false `qualified_candidate_conflict` stops.
        qualified = [c for c in qualified if not _is_irrelevant_cross_reference(c)]
        # #2893 historical merged later_scope_expansion carve-out: a second
        # bounded exclusion, same no-closing-candidate precondition (see
        # `_exclude_historical_merged_later_scope_expansion()`). It only
        # narrows this local list; `evidence["candidates"]` is untouched.
        qualified = _exclude_historical_merged_later_scope_expansion(qualified, issue_number)
    if len(qualified) > 1:
        return {
            "disposition": "reconciliation_required",
            "reason_codes": ["qualified_candidate_conflict"],
            "candidate": None,
        }
    if not qualified:
        return {
            "disposition": "ordinary_dispatch_or_explicit_recovery",
            "reason_codes": ["no_qualified_candidate"],
            "candidate": None,
        }
    candidate = qualified[0]
    lifecycle = candidate["lifecycle"]
    if lifecycle == "closed_unmerged":
        return {
            "disposition": "ordinary_dispatch_or_explicit_recovery",
            "reason_codes": ["closed_unmerged_candidate"],
            "candidate": candidate,
        }
    if lifecycle == "merged":
        # #2699 P1-1 (Finding E / Disposition Precedence step 1): a malformed
        # durable marker (schema/identity/digest invalid) is
        # `reconciliation_required` regardless of lifecycle, and this check
        # must run ahead of the merged-only ancestry/exact-coverage checks so
        # a malformed marker is never silently treated as "no marker" and
        # downgraded to `legacy_or_later_scope_expansion`. (#2750: this
        # candidate already survived the bounded irrelevant-cross-reference
        # carve-out above -- either it is a `closing_relation` candidate, or
        # its own marker error was not a lone `scope_coverage_issue_identity_mismatch`
        # -- so an invalid marker reaching this point stays fail-closed.)
        coverage = _coverage_for(candidate, evidence)
        if isinstance(coverage, Mapping) and coverage.get("status") == "invalid":
            return {
                "disposition": "reconciliation_required",
                "reason_codes": list(coverage.get("errors") or ["invalid_scope_marker"]),
                "candidate": candidate,
            }
        ancestry = candidate.get("main_ancestry")
        if not isinstance(ancestry, Mapping) or ancestry.get("reachable") is not True:
            return {
                "disposition": "ordinary_dispatch_or_explicit_recovery",
                "reason_codes": ["merged_candidate_not_on_current_main"],
                "candidate": candidate,
            }
        if (
            isinstance(coverage, Mapping)
            and coverage.get("exact_coverage") is True
            and coverage.get("later_scope_expansion") is False
        ):
            return {"disposition": "implementation_already_landed", "reason_codes": [], "candidate": candidate}
        return {
            "disposition": "ordinary_dispatch_or_explicit_recovery",
            "reason_codes": ["legacy_or_later_scope_expansion"],
            "candidate": candidate,
        }
    coverage = _coverage_for(candidate, evidence)
    if isinstance(coverage, Mapping) and coverage.get("status") == "invalid":
        return {
            "disposition": "reconciliation_required",
            "reason_codes": list(coverage.get("errors") or ["invalid_scope_marker"]),
            "candidate": candidate,
        }
    if isinstance(coverage, Mapping) and coverage.get("exact_coverage") is True:
        return {"disposition": "existing_pr_resume", "reason_codes": [], "candidate": candidate}
    if (
        (coverage is None or (isinstance(coverage, Mapping) and coverage.get("status") == "missing_marker"))
        and candidate.get("current_scope_ownership") is True
        and candidate.get("head_fresh") is True
    ):
        return {
            "disposition": "existing_pr_resume",
            "reason_codes": ["markerless_allowed_paths_coverage"],
            "candidate": candidate,
        }
    return {
        "disposition": "reconciliation_required",
        "reason_codes": ["open_draft_scope_ownership_not_exact"],
        "candidate": candidate,
    }


def _run(argv: list[str]) -> tuple[int, str, str]:
    cp = subprocess.run(argv, check=False, capture_output=True, text=True)
    return cp.returncode, cp.stdout, cp.stderr


_API_REPO_URL_RE = re.compile(r"^https?://[^/]+/repos/([^/]+/[^/]+?)(?:/.*)?$", re.I)


def _repo_from_api_url(url: str | None) -> str | None:
    """Extract `owner/repo` from a GitHub REST API URL, or None if absent."""
    if not isinstance(url, str) or not url:
        return None
    match = _API_REPO_URL_RE.match(url)
    return match.group(1) if match else None


def _json(run: Callable[[list[str]], tuple[int, str, str]], argv: list[str]) -> tuple[Any, bool]:
    rc, stdout, _ = run(argv)
    if rc:
        return None, False
    try:
        return json.loads(stdout), True
    except json.JSONDecodeError:
        return None, False


def collect_candidate_inputs(
    *,
    repo: str,
    issue_number: int,
    current_scope: Any,
    run_command: Callable[[list[str]], tuple[int, str, str]] = _run,
    max_candidates: int = 20,
) -> dict[str, Any]:
    """Collect bounded closing and timeline cross-reference candidates.

    A body mention is never treated as a cross-reference.  Timeline candidates
    must carry `source.issue.pull_request`, and are later reconciled against a
    fresh PR record; closing linkage remains preferred provenance.
    """
    issue_body = current_scope if isinstance(current_scope, str) else json.dumps(current_scope, ensure_ascii=False)
    commands: list[dict[str, Any]] = []

    def run(argv: list[str]) -> tuple[int, str, str]:
        rc, out, err = run_command(argv)
        commands.append({"argv": argv, "exit_code": rc})
        return rc, out, err

    rows, list_ok = _json(
        run,
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            str(max_candidates),
            "--json",
            "number,closingIssuesReferences",
        ],
    )
    timeline, timeline_ok = _json(
        run,
        [
            "gh",
            "api",
            "--paginate",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{repo}/issues/{issue_number}/timeline?per_page=100",
        ],
    )
    found: dict[int, str] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping) or type(row.get("number")) is not int:
                continue
            refs = row.get("closingIssuesReferences") or []
            if any(isinstance(ref, Mapping) and ref.get("number") == issue_number for ref in refs):
                found[row["number"]] = "closing_relation"
    if isinstance(timeline, list):
        for event in timeline:
            source = event.get("source") if isinstance(event, Mapping) else None
            source_issue = source.get("issue") if isinstance(source, Mapping) else None
            pr_info = source_issue.get("pull_request") if isinstance(source_issue, Mapping) else None
            number = source_issue.get("number") if isinstance(source_issue, Mapping) else None
            if not isinstance(pr_info, Mapping) or type(number) is not int or number <= 0:
                continue
            # #2699 P1-2 (Finding F): `source.issue.pull_request` alone only
            # proves the cross-referencing item is *a* PR, not that it lives
            # in the target repository. A same-numbered PR in an unrelated
            # repo must never be treated as a candidate for this Issue.
            candidate_repo = _repo_from_api_url(
                source_issue.get("repository_url") if isinstance(source_issue.get("repository_url"), str) else None
            ) or _repo_from_api_url(pr_info.get("url") if isinstance(pr_info.get("url"), str) else None)
            if candidate_repo is not None and candidate_repo.lower() != repo.lower():
                continue
            found.setdefault(number, "verified_cross_reference")
    candidates: list[dict[str, Any]] = []
    materialization_failures: list[int] = []
    for number, provenance_kind in list(found.items())[:max_candidates]:
        pr, ok = _json(
            run,
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,url,state,isDraft,mergedAt,mergeCommit,headRefOid,closingIssuesReferences,body,files",
            ],
        )
        if not ok or not isinstance(pr, Mapping):
            # #2699 P0-3 (Finding C): a discovered candidate whose detail
            # fetch fails must not silently collapse discovery into
            # "no candidate" (`no_qualified_candidate` -> possible
            # `ordinary_dispatch_or_explicit_recovery`/duplicate work).
            # Recording it here forces `contradictory` below, which
            # `derive_landing_disposition()` always routes to
            # `reconciliation_required` ahead of every lifecycle branch.
            materialization_failures.append(number)
            continue
        refs = pr.get("closingIssuesReferences") or []
        closing = any(isinstance(ref, Mapping) and ref.get("number") == issue_number for ref in refs)
        provenance_kind = "closing_relation" if closing else provenance_kind
        state = str(pr.get("state") or "").upper()
        lifecycle = (
            "merged"
            if pr.get("mergedAt")
            else "draft"
            if state == "OPEN" and pr.get("isDraft") is True
            else "open"
            if state == "OPEN"
            else "closed_unmerged"
        )
        candidate: dict[str, Any] = {
            "target": {"repo": repo, "issue_number": issue_number},
            "pr": {"number": number, "url": pr.get("url"), "head_sha": pr.get("headRefOid")},
            "provenance": {"kind": provenance_kind, "verified": True},
            "lifecycle": lifecycle,
            "head_fresh": lifecycle not in {"open", "draft"} or _valid_sha(pr.get("headRefOid")),
            "current_scope_ownership": False,
        }
        coverage = coverage_from_pr_body(
            pr_body=str(pr.get("body") or ""), issue_number=issue_number, live_issue_body=issue_body
        )
        candidate["scope_coverage"] = coverage
        # research Issue #2761 residual risk: qualification (the #2750
        # carve-out) must be decided BEFORE any target-specific
        # freshness/validation transport call is spent on this candidate, so
        # an irrelevant sibling's own transport failures never have a chance
        # to reach `materialization_failures`/`contradictory` or the merged
        # main-ancestry compare below. Only fires for a `verified_cross_
        # reference` candidate whose own durable marker already reduces to a
        # lone `scope_coverage_issue_identity_mismatch` at collection time --
        # `_is_irrelevant_cross_reference()`'s existing boundary (closing
        # relation, compound errors, markerless) is unchanged.
        candidate["qualified_irrelevant_sibling"] = _is_irrelevant_cross_reference(candidate)
        if lifecycle in {"open", "draft"} and coverage.get("status") == "missing_marker":
            candidate["current_scope_ownership"] = _allowed_paths_covered(
                build_scope_manifest(issue_body)["allowed_paths"], pr.get("files")
            )
        if lifecycle == "merged":
            merge_oid = (pr.get("mergeCommit") or {}).get("oid") if isinstance(pr.get("mergeCommit"), Mapping) else None
            candidate["merge_oid"] = merge_oid
            candidate["main_ancestry"] = {"verified": False, "reachable": False}
            if not candidate["qualified_irrelevant_sibling"] and _valid_sha(merge_oid):
                rc, out, _ = run(["gh", "api", f"repos/{repo}/compare/{merge_oid}...main", "--jq", ".status"])
                candidate["main_ancestry"] = {
                    "verified": rc == 0,
                    "reachable": rc == 0 and out.strip() in {"ahead", "identical"},
                }
        candidates.append(candidate)
    # #2699 AC9: the current main HEAD sha is one of the three collection-time
    # reference values (issue body / candidate head-or-merge-oid / main sha)
    # that `resolve_landing_disposition_with_freshness_rebind()` re-verifies
    # immediately before finalizing a disposition.
    main_rc, main_out, _ = run(["gh", "api", f"repos/{repo}/commits/main", "--jq", ".sha"])
    main_head_sha = main_out.strip() if main_rc == 0 else None
    if not _valid_sha(main_head_sha):
        main_head_sha = None
    status = "fresh" if list_ok and timeline_ok else "stale"
    return {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": 1,
        "target": {"repo": repo, "issue_number": issue_number, "body_sha256": _body_digest(issue_body)},
        "freshness": {"status": status},
        # #2699 P0-3 (Finding C): a discovered candidate whose materialization
        # (`gh pr view`) failed is evidence-invalid, not evidence-absent --
        # `validate_implementation_landed_evidence()` treats `contradictory`
        # as a hard error, which `derive_landing_disposition()` always
        # routes to `reconciliation_required` ahead of every other branch.
        "contradictory": bool(materialization_failures),
        "candidates": candidates,
        "scope_coverage": None,
        "discovery": {"bounded_max_candidates": max_candidates, "commands": commands},
        "current_scope_manifest": canonicalize_scope_manifest(issue_body),
        "decision_time_rebind": {"status": "fresh" if status == "fresh" else "stale"},
        "materialization_failures": materialization_failures,
        "main_head_sha": main_head_sha,
    }


# ---------------------------------------------------------------------------
# #2699 AC9: decision-time freshness rebind + bounded retry.
#
# `collect_candidate_inputs()` records the identity values that must stay
# stable through disposition finalization (issue body sha256, each
# candidate's head-or-merge-oid, current main HEAD sha). This section
# re-fetches those same three value classes live, immediately before a
# disposition is finalized, and bounded-retries (collect + evaluate) exactly
# once on drift before giving up with `reconciliation_required`
# (`reason_codes: ["freshness_rebind_failed"]`).
# ---------------------------------------------------------------------------

_FRESHNESS_REBIND_MAX_RETRIES = 1

# research Issue #2761: the decision-time per-candidate live re-fetch used by
# `_live_freshness_reference()` requests `body`/`closingIssuesReferences` in
# addition to the pre-existing identity fields, so a candidate previously
# qualified as an irrelevant sibling cross-reference (#2750 carve-out) can
# have its qualification independently re-derived from FRESH live data
# (never trusted from the collection-time snapshot alone -- AC1's "decision-
# time の live body 再解析").
_LIVE_CANDIDATE_REFRESH_FIELDS = "headRefOid,mergedAt,mergeCommit,body,closingIssuesReferences"


def _collection_time_reference(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """The collection-time snapshot of the values freshness-rebind protects."""
    candidates = evidence.get("candidates") if isinstance(evidence.get("candidates"), list) else []
    candidate_identity: dict[int, str | None] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        pr = candidate.get("pr")
        number = pr.get("number") if isinstance(pr, Mapping) else None
        if not isinstance(number, int):
            continue
        if candidate.get("lifecycle") == "merged":
            candidate_identity[number] = candidate.get("merge_oid")
        else:
            candidate_identity[number] = pr.get("head_sha") if isinstance(pr, Mapping) else None
    target = evidence.get("target") if isinstance(evidence.get("target"), Mapping) else {}
    return {
        "issue_body_sha256": target.get("body_sha256"),
        "main_head_sha": evidence.get("main_head_sha"),
        "candidate_identity": candidate_identity,
    }


def _is_historical_carveout_participant(candidate: Mapping[str, Any], issue_number: int) -> bool:
    """A candidate whose collection-time semantic state can feed the #2893
    carve-out (and therefore must be re-derived from live data at decision
    time): `verified_cross_reference`, lifecycle merged/open/draft, and a
    candidate-local valid same-Issue scope coverage."""
    provenance = candidate.get("provenance")
    return (
        isinstance(provenance, Mapping)
        and provenance.get("kind") == "verified_cross_reference"
        and candidate.get("lifecycle") in {"merged", "open", "draft"}
        and _has_valid_same_issue_scope_coverage(candidate, issue_number)
    )


def _historical_carveout_possible(candidates: list[Any], issue_number: int) -> bool:
    """True when the #2893 carve-out could apply to the collected evidence: no
    `closing_relation` candidate and at least one historical merged
    later_scope_expansion candidate."""
    mappings = [c for c in candidates if isinstance(c, Mapping) and isinstance(c.get("provenance"), Mapping)]
    if any(c["provenance"].get("kind") == "closing_relation" for c in mappings):
        return False
    return any(_is_historical_merged_later_scope_candidate(c, issue_number) for c in mappings)


def _live_freshness_reference(
    *,
    repo: str,
    issue_number: int,
    candidates: list[Any],
    run_command: Callable[[list[str]], tuple[int, str, str]],
) -> dict[str, Any]:
    """Live re-fetch of the same three value classes, taken right before
    finalizing a disposition. `ok` is False whenever any live fetch fails
    (transport failure is treated as non-fresh, never as a silent match) --
    EXCEPT for a candidate already qualified as an irrelevant sibling cross-
    reference (#2750 carve-out) at collection time (research Issue #2761):
    its head-identity DRIFT alone must not block the current target's
    disposition, once its irrelevance has been FRESHLY reconfirmed from this
    candidate's own successfully-fetched, fully-typed live `body`/
    `closingIssuesReferences` (AC1's "decision-time の live body 再解析" --
    never trusted from the collection-time snapshot alone, and never
    required to byte-match it). A transport/parse failure on THIS
    candidate's own live re-fetch, or a `body`/`closingIssuesReferences`
    field that is missing/None/wrong-type, is authority-UNCONFIRMED -- never
    silently treated as "still irrelevant" -- so it is NOT excluded from the
    freshness identity requirement and drives `ok=False` /
    `freshness_rebind_failed` like any other unconfirmed candidate (research
    Issue #2761 P1-1/P1-2).

    historical merged later_scope_expansion carve-out (#2893 P2): when that
    carve-out could apply (no `closing_relation` candidate, and a historical
    merged later_scope_expansion candidate exists at collection time), every
    carve-out participant (see `_is_historical_carveout_participant()`) also
    has its semantic state (scope coverage / closing relation) re-derived
    from this same already-fetched live `body`/`closingIssuesReferences`, so
    the carve-out decision is never made from a stale collection-time marker
    state. A participant whose live authority cannot be fully verified is
    recorded in `unconfirmed_qualified_irrelevant_numbers` (-> `ok=False` /
    `freshness_rebind_failed`, never fail-open). Participants are NEVER added
    to `excluded_candidate_numbers` (their identity is always required) and
    the PR body is never byte-compared: a prose-only change that leaves the
    marker/closing semantics unchanged keeps the disposition."""
    rc, out, _ = run_command(["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body"])
    issue_body_sha256: str | None = None
    live_issue_body: str | None = None
    if rc == 0:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = None
        body = payload.get("body") if isinstance(payload, Mapping) else None
        if isinstance(body, str):
            live_issue_body = body
            issue_body_sha256 = _body_digest(body)

    main_rc, main_out, _ = run_command(["gh", "api", f"repos/{repo}/commits/main", "--jq", ".sha"])
    main_head_sha = main_out.strip() if main_rc == 0 else None
    if not _valid_sha(main_head_sha):
        main_head_sha = None

    candidate_identity: dict[int, str | None] = {}
    excluded_candidate_numbers: set[int] = set()
    candidate_live_updates: dict[int, dict[str, Any]] = {}
    # research Issue #2761 P1-1/P1-2: a previously-qualified-irrelevant
    # candidate whose decision-time live authority (this candidate's own
    # `gh pr view` transport/parse, or its `body`/`closingIssuesReferences`
    # field shape) could not be fully verified. Tracked SEPARATELY from
    # `candidate_identity` because a malformed-but-present `body`/
    # `closingIssuesReferences` payload still carries a perfectly valid
    # `headRefOid`/`mergeCommit` identity -- relying on `candidate_identity`
    # alone (as a `None` proxy for "unconfirmed") would silently miss this
    # case and fail back open.
    unconfirmed_qualified_irrelevant_numbers: set[int] = set()
    carveout_refresh = _historical_carveout_possible(candidates, issue_number)
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        pr = candidate.get("pr")
        number = pr.get("number") if isinstance(pr, Mapping) else None
        if not isinstance(number, int):
            continue
        previously_qualified_irrelevant = candidate.get("qualified_irrelevant_sibling") is True
        c_rc, c_out, _ = run_command(
            ["gh", "pr", "view", str(number), "--repo", repo, "--json", _LIVE_CANDIDATE_REFRESH_FIELDS]
        )
        identity: str | None = None
        c_payload: Mapping[str, Any] | None = None
        if c_rc == 0:
            try:
                parsed = json.loads(c_out)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, Mapping):
                c_payload = parsed
                if parsed.get("mergedAt"):
                    merge_commit = parsed.get("mergeCommit")
                    identity = merge_commit.get("oid") if isinstance(merge_commit, Mapping) else None
                else:
                    identity = parsed.get("headRefOid")
        candidate_identity[number] = identity

        carveout_participant = (
            carveout_refresh
            and not previously_qualified_irrelevant
            and _is_historical_carveout_participant(candidate, issue_number)
        )
        if not previously_qualified_irrelevant and not carveout_participant:
            continue
        # research Issue #2761 P1-1/P1-2: a collection-time-qualified
        # irrelevant sibling's decision-time re-confirmation is only trusted
        # from FULLY verified fresh authority -- this candidate's own
        # `gh pr view` transport/parse succeeding AND yielding a `body`
        # string AND a `closingIssuesReferences` list. Any of those being
        # missing/None/wrong-type (or the current target's own live body
        # itself failing to fetch) means negative qualification cannot be
        # freshly re-derived: this candidate is UNKNOWN, never silently
        # re-confirmed as "still irrelevant". Recorded in
        # `unconfirmed_qualified_irrelevant_numbers`, which independently
        # forces `ok=False` below -- unlike the previous fail-open behavior,
        # this does NOT rely on `candidate_identity[number]` happening to be
        # `None` (a malformed `body`/`closingIssuesReferences` payload can
        # still carry a perfectly valid `headRefOid`/`mergeCommit`).
        if c_payload is None:
            unconfirmed_qualified_irrelevant_numbers.add(number)
            continue
        raw_body = c_payload.get("body")
        raw_refs = c_payload.get("closingIssuesReferences")
        if not isinstance(raw_body, str) or not isinstance(raw_refs, list):
            # Field missing/None/wrong-type is authority-UNCONFIRMED, never
            # equivalent to a verified empty `closingIssuesReferences: []`
            # (which legitimately means "confirmed: no closing relation").
            unconfirmed_qualified_irrelevant_numbers.add(number)
            continue
        if live_issue_body is None:
            # The current target's own live body could not be fetched, so
            # body-coverage cannot be freshly re-derived for anyone.
            # `issue_body_sha256` is already `None` here, which
            # independently drives `ok=False` below.
            unconfirmed_qualified_irrelevant_numbers.add(number)
            continue
        live_body = raw_body
        live_refs = raw_refs
        live_closing = any(isinstance(ref, Mapping) and ref.get("number") == issue_number for ref in live_refs)
        live_coverage = coverage_from_pr_body(
            pr_body=live_body, issue_number=issue_number, live_issue_body=live_issue_body
        )
        probe_candidate = {"provenance": candidate.get("provenance") or {}, "scope_coverage": live_coverage}
        # #2893 P2: a carve-out participant is re-derived but NEVER granted the
        # #2750 identity exemption (it was not a qualified irrelevant sibling
        # at collection time); identity comparison stays as before.
        still_irrelevant = (
            previously_qualified_irrelevant and (not live_closing) and _is_irrelevant_cross_reference(probe_candidate)
        )
        candidate_live_updates[number] = {
            "scope_coverage": live_coverage,
            "promote_closing_relation": live_closing,
            "still_irrelevant": still_irrelevant,
        }
        if still_irrelevant:
            excluded_candidate_numbers.add(number)
        # else: decision-time live data now shows current-target authority
        # (or a materially different marker state) -- the #2750 carve-out
        # must NOT apply anymore; this candidate's identity stays required
        # below, and `_apply_live_candidate_qualification_updates()` will
        # hand it back to normal (non-carve-out) disposition handling.

    ok = (
        issue_body_sha256 is not None
        and main_head_sha is not None
        and not unconfirmed_qualified_irrelevant_numbers
        and all(
            identity is not None
            for number, identity in candidate_identity.items()
            if number not in excluded_candidate_numbers
        )
    )
    return {
        "ok": ok,
        "unconfirmed_qualified_irrelevant_numbers": unconfirmed_qualified_irrelevant_numbers,
        "issue_body_sha256": issue_body_sha256,
        "main_head_sha": main_head_sha,
        "candidate_identity": candidate_identity,
        "excluded_candidate_numbers": excluded_candidate_numbers,
        "candidate_live_updates": candidate_live_updates,
    }


def _freshness_matches(
    collected: Mapping[str, Any],
    live: Mapping[str, Any],
    *,
    excluded_candidate_numbers: Any = frozenset(),
) -> bool:
    if not live.get("ok"):
        return False
    if collected.get("issue_body_sha256") != live.get("issue_body_sha256"):
        return False
    if collected.get("main_head_sha") != live.get("main_head_sha"):
        return False
    excluded = set(excluded_candidate_numbers or ())
    collected_identity = {
        number: value for number, value in (collected.get("candidate_identity") or {}).items() if number not in excluded
    }
    live_identity = {
        number: value for number, value in (live.get("candidate_identity") or {}).items() if number not in excluded
    }
    return collected_identity == live_identity


def _apply_live_candidate_qualification_updates(
    evidence: Mapping[str, Any], live_ref: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply `_live_freshness_reference()`'s decision-time re-derivation
    (research Issue #2761) onto `evidence["candidates"]` before
    `derive_landing_disposition()` runs, so its own `_is_irrelevant_cross_
    reference()` re-check (candidate-local `scope_coverage` + `provenance`)
    sees the FRESH decision-time classification rather than a stale
    collection-time one (AC1). A candidate reconfirmed still-irrelevant gets
    its `scope_coverage` refreshed (no behavior change). A candidate whose
    live data now shows current-target authority (closingIssuesReferences
    now names the current target) has its `provenance` promoted to
    `closing_relation` so the EXISTING closing-priority disposition logic
    takes over unchanged -- "existing target-authority handling proceeds
    normally" rather than a bespoke un-exclusion branch."""
    updates = live_ref.get("candidate_live_updates")
    if not updates:
        return dict(evidence)
    candidates = evidence.get("candidates")
    if not isinstance(candidates, list):
        return dict(evidence)
    updated_candidates: list[Any] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            updated_candidates.append(candidate)
            continue
        pr = candidate.get("pr")
        number = pr.get("number") if isinstance(pr, Mapping) else None
        refresh = updates.get(number) if isinstance(number, int) else None
        if refresh is None:
            updated_candidates.append(candidate)
            continue
        updated = dict(candidate)
        updated["scope_coverage"] = refresh["scope_coverage"]
        updated["qualified_irrelevant_sibling"] = bool(refresh.get("still_irrelevant"))
        if refresh.get("promote_closing_relation"):
            updated["provenance"] = {"kind": "closing_relation", "verified": True}
        updated_candidates.append(updated)
    new_evidence = dict(evidence)
    new_evidence["candidates"] = updated_candidates
    return new_evidence


def resolve_landing_disposition_with_freshness_rebind(
    *,
    repo: str,
    issue_number: int,
    current_scope: Any,
    run_command: Callable[[list[str]], tuple[int, str, str]] = _run,
    max_candidates: int = 20,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """AC9 production entry point: collect, verify freshness immediately
    before finalizing, bounded-retry once on drift, then finalize.

    This is the canonical replacement for a caller manually chaining
    `collect_candidate_inputs()` -> `derive_landing_disposition()` without a
    freshness check (`build_intake_capsule.py` uses this)."""
    evidence = collect_candidate_inputs(
        repo=repo,
        issue_number=issue_number,
        current_scope=current_scope,
        run_command=run_command,
        max_candidates=max_candidates,
    )
    for attempt in range(_FRESHNESS_REBIND_MAX_RETRIES + 1):
        collected_ref = _collection_time_reference(evidence)
        live_ref = _live_freshness_reference(
            repo=repo,
            issue_number=issue_number,
            candidates=evidence.get("candidates") or [],
            run_command=run_command,
        )
        excluded_candidate_numbers = live_ref.get("excluded_candidate_numbers") or set()
        if _freshness_matches(collected_ref, live_ref, excluded_candidate_numbers=excluded_candidate_numbers):
            evidence = _apply_live_candidate_qualification_updates(evidence, live_ref)
            evidence["decision_time_rebind"] = {"status": "fresh"}
            evidence["landing_disposition"] = derive_landing_disposition(
                evidence, repo=repo, issue_number=issue_number, now=now
            )
            return evidence
        if attempt >= _FRESHNESS_REBIND_MAX_RETRIES:
            evidence["decision_time_rebind"] = {"status": "stale"}
            evidence["landing_disposition"] = {
                "disposition": "reconciliation_required",
                "reason_codes": ["freshness_rebind_failed"],
                "candidate": None,
            }
            return evidence
        # Bounded retry: re-collect once before giving up (#2699 AC9).
        evidence = collect_candidate_inputs(
            repo=repo,
            issue_number=issue_number,
            current_scope=current_scope,
            run_command=run_command,
            max_candidates=max_candidates,
        )
    # Unreachable: the loop above always returns within
    # `_FRESHNESS_REBIND_MAX_RETRIES + 1` iterations.
    evidence["decision_time_rebind"] = {"status": "stale"}
    evidence["landing_disposition"] = {
        "disposition": "reconciliation_required",
        "reason_codes": ["freshness_rebind_failed"],
        "candidate": None,
    }
    return evidence


# ---------------------------------------------------------------------------
# #2699 Disposition Precedence step 2: reuse #2607's existing
# `route_loop_verdict_v2.py::resolve_already_satisfied_early_exit_decision()`
# rather than adding a new enum. This only overrides a landed-evidence
# result that itself could not establish landing authority (no qualified
# candidate, or only closed-unmerged candidates survive qualification) --
# `reconciliation_required` / `implementation_already_landed` /
# `existing_pr_resume` (Disposition Precedence steps 1/3/4/4b/4c) are never
# touched by this step.
# ---------------------------------------------------------------------------

_NO_LANDING_AUTHORITY_REASON_CODES = frozenset({"no_qualified_candidate", "closed_unmerged_candidate"})


def _load_route_loop_verdict_v2_module() -> Any | None:
    """Default (dependency-injection-free) production loader.

    #2713 AC4: `sys.modules[spec.name] = module` MUST be registered before
    `exec_module()` -- matching `route_loop_verdict_v2.py`'s own
    `resolve_pre_step1_landing_disposition()` pattern (that file, lines
    696-698). Without this registration, `route_loop_verdict_v2.py`'s
    `@dataclass(frozen=True)` classes (it uses
    `from __future__ import annotations`, so field annotations are
    strings) fail during `exec_module()`: CPython's `dataclasses`
    internals resolve stringified `ClassVar`/`InitVar` annotations via
    `sys.modules.get(cls.__module__)`, and when that lookup returns `None`
    (module not yet registered under its own `__name__`), it raises
    `AttributeError` while decorating the class. That exception was
    previously swallowed by the `except Exception: return None` below,
    silently making `apply_already_satisfied_precedence()` a permanent
    no-op through the default loader.

    #2713 AC10 (PR #2741 review, P2): register-before-exec must not destroy
    a same-named `sys.modules` entry that already existed BEFORE this loader
    ran. `prior_module` snapshots whatever was there first; on `exec_module()`
    failure, that original entry is restored (not merely `pop()`-ed) so only
    the entry this loader itself inserted is ever removed.
    """
    import importlib.util

    path = Path(__file__).resolve().with_name("route_loop_verdict_v2.py")
    spec = importlib.util.spec_from_file_location("route_loop_verdict_v2_for_evidence", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    prior_module = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if prior_module is not None:
            sys.modules[spec.name] = prior_module
        else:
            sys.modules.pop(spec.name, None)
        return None
    return module


def _load_adjudicate_vc_result_module() -> Any | None:
    """Default (dependency-injection-free) loader for
    `adjudicate_vc_result.py`, used solely to reuse its existing
    `adapt_test_verdict_to_current_vc_result()` TEST_VERDICT_MACHINE/v2
    validation (#2713 AC9 -- see
    `derive_base_ac_satisfied_from_verification_result()` below). Follows the
    same register-before-exec / restore-on-failure pattern as
    `_load_route_loop_verdict_v2_module()` above (#2713 AC10).
    """
    import importlib.util

    path = Path(__file__).resolve().with_name("adjudicate_vc_result.py")
    spec = importlib.util.spec_from_file_location("adjudicate_vc_result_for_evidence", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    prior_module = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if prior_module is not None:
            sys.modules[spec.name] = prior_module
        else:
            sys.modules.pop(spec.name, None)
        return None
    return module


def derive_pr_exists_from_landing_candidate(candidate: Mapping[str, Any] | None) -> bool:
    """Production source for `apply_already_satisfied_precedence()`'s
    `pr_exists` input (#2713 AC3).

    `pr_exists` is NOT "a PR existed at some point in history" -- it is
    "is the current landing-disposition candidate itself a target that a
    resume / conflict determination could act on", derived directly from
    the SAME qualified candidate `derive_landing_disposition()` already
    resolved (its `lifecycle` field). This mapping is total across the
    five PR fixture shapes this module recognizes:

    - open / draft: an active PR exists that a resume determination could
      act on -> True.
    - merged: a landed PR object exists. (In practice a merged candidate
      is normally intercepted upstream by `implementation_already_landed`
      / `legacy_or_later_scope_expansion` before
      `apply_already_satisfied_precedence()` is ever reached, but the
      mapping stays total for direct callers/tests.) -> True.
    - closed_unmerged: the PR was abandoned without landing. It is not a
      resumable or blocking artifact for `already_satisfied` purposes: a
      closed-unmerged attempt does not, by itself, mean the requirement is
      already satisfied, but it also does not preclude concluding
      `already_satisfied` from independent `base_ac_satisfied` evidence ->
      False. This is the explicit semantic #2713 calls out for
      documentation.
    - no candidate (`candidate is None`): nothing exists -> False.
    """
    if not isinstance(candidate, Mapping):
        return False
    return candidate.get("lifecycle") in {"merged", "open", "draft"}


def derive_base_ac_satisfied_from_verification_result(
    verification_result: Mapping[str, Any] | None,
    *,
    live_main_sha: str | None,
    adjudicate_vc_result_module: Any | None = None,
) -> bool | None:
    """Production source for `apply_already_satisfied_precedence()`'s
    `base_ac_satisfied` input (#2713 AC3).

    `verification_result` is a TEST_VERDICT_MACHINE/v2-shaped payload (the
    same shape `route_loop_verdict_v2.py::build_already_satisfied_evidence()`
    consumes as `base_test_verdict` and the same shape
    `.claude/agents/test-runner.md` reports): `{"schema":
    "TEST_VERDICT_MACHINE/v2", "head_sha": <sha>, "contract_body_sha256":
    <sha256>, "runtime_ac_results": [{"ac": <id>, "command_hash": <sha256>,
    "status": "pass"|"fail"|"skip", "exit_code": <int>,
    "fallback_detected": <bool>, "human_review_required": <bool>,
    "stop_condition_triggered": <bool>}, ...]}`, representing an independent
    Verification-Commands evaluation of current main -- never a
    caller-asserted boolean.

    #2713 AC9 (PR #2741 review, P1-2): rather than re-implementing a second,
    weaker TEST_VERDICT_MACHINE/v2 validator, this reuses
    `adjudicate_vc_result.py::adapt_test_verdict_to_current_vc_result()` --
    the SAME adapter `adjudicate_vc_result()`'s production callers already
    trust -- and only inspects its validated output. That adapter already
    enforces: `schema == "TEST_VERDICT_MACHINE/v2"`, non-empty `head_sha` /
    `contract_body_sha256`, and (per `runtime_ac_results[]` entry)
    non-empty `ac` identity and `command_hash`; any of those missing is
    reported back via its `errors` list. A malformed/incomplete payload
    (missing AC identity, missing command_hash, or an unparseable schema)
    therefore yields `None` here -- undeterminable -- rather than being
    coerced into `True`.

    Returns `None` (undeterminable) whenever the result cannot be trusted:
    no `verification_result` supplied, no `live_main_sha` to cross-check
    freshness against, the adapter itself reports validation errors, the
    adapter's `head_sha` does not match `live_main_sha` (a stale/mismatched
    run is never silently trusted -- mirrors
    `build_already_satisfied_evidence()`'s own `base_ac_satisfied` freshness
    gate), or the adapted `results` list is missing/empty. Callers MUST
    treat `None` as "do not apply precedence" (pass the existing landing
    disposition through unchanged) rather than defaulting to a fixed
    `True`/`False` (#2713 In Scope).

    Returns `False` (determinately not satisfied) whenever the adapted,
    fresh result carries ANY of: an aggregate `fallback_detected` /
    `human_review_required` / `stop_condition_triggered` flag, or a
    `runtime_ac_results` entry whose `status != "pass"` (this rejects
    `SKIP`/`PARTIAL`/`fail` entries), `exit_code != 0`, or a per-entry
    `fallback_detected` / `human_review_required` /
    `stop_condition_triggered` flag (#2713 AC9).

    Only a well-formed, fresh, wholly-clean result yields `True`.
    """
    if not isinstance(verification_result, Mapping):
        return None
    if not isinstance(live_main_sha, str) or not live_main_sha:
        return None

    module = adjudicate_vc_result_module or _load_adjudicate_vc_result_module()
    if module is None:
        return None

    converted, adapt_errors = module.adapt_test_verdict_to_current_vc_result(dict(verification_result))
    if converted is None or adapt_errors:
        return None
    if converted.get("head_sha") != live_main_sha:
        return None

    results = converted.get("results")
    if not isinstance(results, list) or not results:
        return None

    if converted.get("fallback_detected") or converted.get("human_review_required") or converted.get(
        "stop_condition_triggered"
    ):
        return False

    for entry in results:
        if not isinstance(entry, Mapping):
            return False
        if entry.get("status") != "pass":
            return False
        if entry.get("exit_code") != 0:
            return False
        if (
            entry.get("fallback_detected")
            or entry.get("human_review_required")
            or entry.get("stop_condition_triggered")
        ):
            return False
    return True


def apply_already_satisfied_precedence(
    landing_result: Mapping[str, Any],
    *,
    next_action_route: str,
    product_spec_routing_action: str,
    pr_exists: bool,
    base_ac_satisfied: bool,
    route_loop_verdict_v2_module: Any | None = None,
) -> dict[str, Any]:
    """Disposition Precedence step 1 -> step 2 composition (AC4).

    `landing_result` is `derive_landing_disposition()`'s output. When (and
    only when) it could not establish landing authority
    (`ordinary_dispatch_or_explicit_recovery` with `no_qualified_candidate`
    or `closed_unmerged_candidate`), this re-evaluates the existing #2607
    `resolve_already_satisfied_early_exit_decision()` and, if it fires,
    returns the `already_satisfied` route instead. Every other landing
    disposition (`reconciliation_required`, `implementation_already_landed`,
    `existing_pr_resume`, and any other `ordinary_dispatch_or_explicit_recovery`
    reason) passes through unchanged -- step 1 always wins over step 2.
    """
    disposition = landing_result.get("disposition")
    reason_codes = landing_result.get("reason_codes") or []
    no_landing_authority = disposition == "ordinary_dispatch_or_explicit_recovery" and any(
        code in _NO_LANDING_AUTHORITY_REASON_CODES for code in reason_codes
    )
    if not no_landing_authority:
        return dict(landing_result)

    module = route_loop_verdict_v2_module or _load_route_loop_verdict_v2_module()
    if module is None:
        return dict(landing_result)

    decision = module.resolve_already_satisfied_early_exit_decision(
        next_action_route=next_action_route,
        product_spec_routing_action=product_spec_routing_action,
        pr_exists=pr_exists,
        base_ac_satisfied=base_ac_satisfied,
    )
    if decision.get("early_exit") is not True:
        return dict(landing_result)

    return {
        "disposition": "already_satisfied",
        "reason_codes": ["already_satisfied_no_pr_created"],
        "candidate": landing_result.get("candidate"),
        "already_satisfied_decision": decision,
    }
