"""Stage 2-7 (#37): SKILL.md reference closure, lane aggregation, route table, Pilot-1 handoff.

Pure static / local checks (plus one real-subprocess negative control). No network, no GitHub call.

  AC2  every file SKILL.md references resolves, or is classified in the manifest (deferred with
       owner_stage / reason / rerun_condition, removed-in-source, placeholder, or resolved elsewhere);
       an unclassified reference is a missing hard dependency and fails.
  AC3  the per-lane collect / ran / deferred totals recorded in the manifest equal the sums of the
       existing stage manifests, and every lane manifest is accounted for.
  AC4  the Pilot-1 handoff document carries the execution conditions (it never runs Pilot-1).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
SKILL_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop"
MANIFEST_PATH = DEV / "stage2-7-terminal-wiring-manifest.json"
HANDOFF_PATH = DEV / "stage2-7-pilot1-handoff.md"
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

_EXT = r"(?:py|md|json|sh|yaml|yml)"


def skill_references() -> set[str]:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    refs = set(re.findall(rf"`([^`\s]+\.{_EXT})(?:#[^`]*)?`", text))
    refs |= set(re.findall(rf"\]\(([^)\s#]+\.(?:py|md|json))", text))
    refs |= set(
        re.findall(
            rf"(?<![\w/.\-])((?:references|scripts|schemas|\.claude|docs)/[\w./\-]+\.{_EXT})", text
        )
    )
    return refs


def resolves(ref: str) -> bool:
    candidates = [
        ref,
        f".claude/skills/issue-refinement-loop/{ref}",
        f".claude/skills/issue-refinement-loop/scripts/{ref}",
        f".claude/skills/issue-refinement-loop/references/{ref}",
        f".claude/skills/{ref}",
    ]
    return any((ROOT / c).exists() for c in candidates)


INV = MANIFEST["skill_reference_inventory"]


def test_ac2_inventory_total_matches_the_current_skill_md() -> None:
    assert len(skill_references()) == INV["total"]


def test_ac2_every_reference_is_resolved_or_classified_no_missing_hard_dependency() -> None:
    classified = (
        {e["id"] for e in INV["deferred"]}
        | set(INV["removed_in_source"])
        | set(INV["placeholders"])
        | set(INV["resolved_elsewhere"])
    )
    unresolved = {r for r in skill_references() if not resolves(r)}
    missing_hard = sorted(unresolved - classified)
    assert missing_hard == []
    # no classification may hide a reference that actually resolves, or name one SKILL.md dropped
    assert sorted(classified - unresolved) == []


def test_ac2_deferred_entries_carry_owner_reason_and_rerun_condition() -> None:
    assert INV["deferred"], "expected the deferred set to be recorded"
    for entry in INV["deferred"]:
        assert all(entry.get(k) for k in ("id", "owner_stage", "reason", "rerun_condition")), entry


def test_ac2_resolved_elsewhere_points_at_real_files() -> None:
    for ref, real in INV["resolved_elsewhere"].items():
        assert (ROOT / real).is_file(), (ref, real)


def test_ac2_removed_in_source_assets_are_absent_from_the_source_tag_and_described_as_removed() -> (
    None
):
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for name in INV["removed_in_source"]:
        assert Path(name).name in text
    assert "撤去" in text


def test_ac2_migrated_reference_docs_and_thin_entrypoint_test_exist() -> None:
    for rel in MANIFEST["migrated_in_2_7"]:
        assert (ROOT / rel).is_file(), rel
    index = (SKILL_DIR / "references" / "index.md").read_text(encoding="utf-8")
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for doc in sorted((SKILL_DIR / "references").glob("*.md")):
        if doc.name != "index.md":
            # the index is the source file verbatim; a doc it omits must be linked from SKILL.md
            assert doc.name in index or f"references/{doc.name}" in skill, doc.name


def test_ac2_unassigned_assets_are_each_decided() -> None:
    decided = {e["id"] for e in MANIFEST["asset_decisions"]}
    assert {
        "owner_reaction_decision",
        "apply_triage_label_transition",
        "retrospective_candidate_handoff",
        "milestone_rollup",
        "scope_rollup",
    } <= decided
    for e in MANIFEST["asset_decisions"]:
        assert e["decision"] in {"migrated", "deferred"}, e
        assert e["reason"], e
        if e["decision"] == "deferred":
            assert e["owner_stage"] and e["rerun_condition"], e


def test_ac2_negative_control_absent_owner_reaction_script_fails_closed_never_approved() -> None:
    """owner_reaction_decision.py is deferred. The preflight launches it as a fresh subprocess and treats
    any failure as `None` ("not approved"), so its absence can never become an implicit approval."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import run_refinement_preflight as p;"
        "assert not p.OWNER_REACTION_DECISION_SCRIPT.exists();"
        "r = p._run_owner_reaction_decision_fresh(repo='o/r', issue_number=1, owner_user_id=1,"
        " preview_binding_file='/nonexistent', timeout=30.0);"
        "print('RESULT', r)"
    )
    cp = subprocess.run(
        [sys.executable, "-I", "-c", code, str(SKILL_DIR / "scripts")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "RESULT None" in cp.stdout


# ------------------------------------------------------------------------- AC3 lane aggregation


def _lane_numbers(path: Path) -> tuple[int, int, int]:
    m = json.loads(path.read_text(encoding="utf-8"))
    collect = m.get("expected_collect") or m.get("expected_collect_total") or {}
    ran = m.get("expected_ran") or {}
    deferred = m.get("deferred_tests") or []
    return sum(collect.values()), sum(ran.values()), len(deferred)


def test_ac3_every_lane_manifest_is_accounted_for_and_totals_match() -> None:
    lanes = MANIFEST["lane_totals"]["lanes"]
    on_disk = {
        p.name
        for p in DEV.glob("*manifest.json")
        if p.name not in {"refinement-migration-manifest.json", MANIFEST_PATH.name}
    }
    assert on_disk == set(lanes), sorted(on_disk ^ set(lanes))
    total = [0, 0, 0]
    for name, rec in lanes.items():
        got = _lane_numbers(DEV / name)
        if name == "stage0-test-manifest.json":
            # stage 0 pins collect counts only; its run is a separate runner (run_stage0_tests.py)
            got = (got[0], got[0], got[2])
        assert [rec["collect"], rec["ran"], rec["deferred"]] == list(got), name
        for i in range(3):
            total[i] += got[i]
    own = _lane_numbers(MANIFEST_PATH)
    assert [MANIFEST["lane_totals"]["collect"], MANIFEST["lane_totals"]["ran"]] == [
        total[0] + own[0],
        total[1] + own[1],
    ]
    assert MANIFEST["lane_totals"]["deferred"] == total[2] + own[2]


def test_ac3_this_manifest_pins_its_own_files_and_has_no_deselect_or_skip_escape() -> None:
    assert MANIFEST["deferred_tests"] == []
    assert set(MANIFEST["test_files"]) == set(MANIFEST["expected_collect"])
    assert MANIFEST["expected_collect"] == MANIFEST["expected_ran"]
    for rel in MANIFEST["test_files"]:
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"pytest\.mark\.(skip|xfail|skipif)|pytest\.(skip|xfail)\(", src), rel


# ------------------------------------------------------------------------- route table


def test_route_table_in_the_manifest_matches_the_preflight_and_consumers_exist() -> None:
    sys.path.insert(0, str(ROOT / "scripts" / "agent-ops"))
    import workflow_capability_preflight as wcp

    routes = MANIFEST["mutation_routes"]
    assert wcp._KNOWN_OPERATION_ROUTES == frozenset(routes["available"])
    for op, consumer in routes["available"].items():
        assert (ROOT / consumer).is_file(), (op, consumer)
        assert wcp._operation_route(op) == "available"
    for op in routes["unavailable"]:
        assert wcp._operation_route(op) == "unavailable"
        assert op in wcp._MUTATION_OPERATION_NAMES
    assert routes["permission"] == "unverified"


def test_skill_md_note_is_updated_and_no_longer_claims_every_route_is_unavailable() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "移管状況 (Stage 2-7)" in text
    assert "移管状況 (Stage 1a-2)" not in text
    assert "mutation route は常に `unavailable`" not in text
    assert len(text.splitlines()) <= 500


# ------------------------------------------------------------------------- AC4 Pilot-1 handoff


@pytest.mark.parametrize(
    "needle",
    [
        "docs-only",
        "before",
        "after",
        "digest",
        "readback",
        "status",
        "decision",
        "実施しない",
        "#46",
        "#45",
    ],
)
def test_ac4_pilot1_handoff_document_records_each_execution_condition(needle: str) -> None:
    assert needle in HANDOFF_PATH.read_text(encoding="utf-8")


def test_ac4_manifest_pilot1_handoff_lists_the_required_conditions() -> None:
    handoff = MANIFEST["pilot1_handoff"]
    assert handoff["pilot_executed_in_this_change"] is False
    assert {
        "dedicated_docs_only_issue",
        "before_after_digest",
        "readback",
        "status_decision",
    } <= set(handoff["conditions"])
