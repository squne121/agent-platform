"""I-4-4D8 (#103) closure: the hook-free `pr-reviewer` and the publisher / handoff dependency audit.

Owner decision (policy A): the old Claude Code hook `pr_reviewer_guard.py` is NOT re-migrated and the
`hooks` reference in `pr-reviewer.md` is abolished. D4's "removing the hook fails" is therefore
inverted into "re-introducing the hook fails". This closure proves:

* AC1: `pr-reviewer.md` has no hook, keeps the read-only role contract and the original source pin
  (the 3 recorded `intentional_diff` entries reproduce the pinned source blob / SHA-256), and never
  claims a technical Bash write denial. Negative controls cover hook reintroduction, an editing
  tool grant, a rewritten source pin, rewritten D4 history and the like.
* AC2: the D4 to D7 records are in sync with the policy while their observed counts stay history.
* AC3: the hook-origin observation, the publisher dependency audit and the capability-vs-experience
  distinction are recorded concretely (UNKNOWN where unobserved) and request no automatic migration.
* AC4: the #101 handoff defects are reproduced against the REAL publisher code, not assumed.
* AC5: the lane and the measured counts.

Nothing here dispatches an Agent, calls GitHub, publishes a verdict or sends a Task Context signal.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
MANIFEST = DEV / "i4-4d8-hookless-reviewer-manifest.json"
D4_MANIFEST = DEV / "i4-4d4-skill-agents-readonly-manifest.json"
D5_MANIFEST = DEV / "i4-4d5-pr-review-consumers-manifest.json"
D6_MANIFEST = DEV / "i4-4d6-pr-review-core-manifest.json"
D7_MANIFEST = DEV / "i4-4d7-open-pr-reference-evaluator-manifest.json"
C2_MANIFEST = DEV / "i4-4c2-intake-capsule-manifest.json"
STAGE27_MANIFEST = DEV / "stage2-7-terminal-wiring-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
THIS = "tests/stage_i4/test_i4_4d8_hookless_reviewer.py"
D8_JOB = "i4-4d8-hookless-reviewer"
D8_MANIFEST_REL = "docs/dev/i4-4d8-hookless-reviewer-manifest.json"
REVIEWER = ".claude/agents/pr-reviewer.md"
JUDGE = ".claude/skills/pr-review-judge/SKILL.md"
IRL = ".claude/skills/impl-review-loop/SKILL.md"
PUBLISHER = ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
SOURCE_BLOB = "5a83c95aebf9a4af7f1739126c569b8800af0908"  # fixed by the D4 Issue, never edited
SOURCE_COMMIT = "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
D4_MOVED_SHA256 = "45d6e716c5b4b0f0aac7c48ac433a9376902e8f2398bce0872ca183142e02187"
HASH_PLACEHOLDER = "0123456789abcdef" * 4
ROLE = {
    "tools": ["Bash", "Read", "Grep", "Glob"],
    "disallowedTools": ["Edit", "Write", "MultiEdit"],
    "model": "sonnet",
    "effort": "high",
    "permissionMode": "dontAsk",
    "skills": ["pr-review-judge"],
}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_json(MANIFEST))


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    return yaml.safe_load(text.split("---\n", 2)[1]) or {}


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# --- problem functions (pure: they return messages, [] means OK) ------------------------------


def source_parity_problems(d4: dict[str, Any], root: Path) -> list[str]:
    """Reverse-apply the recorded intentional_diff and compare with the Issue-fixed source pin."""
    problems: list[str] = []
    rec = d4["documents"][REVIEWER]
    if rec["source_blob_sha1"] != SOURCE_BLOB:
        problems.append("source blob pin is not the Issue-fixed pin")
    if d4["source"]["commit"] != SOURCE_COMMIT:
        problems.append("source commit pin changed")
    path = root / REVIEWER
    if not path.is_file():
        return [*problems, "missing pr-reviewer.md"]
    moved = path.read_bytes()
    if hashlib.sha256(moved).hexdigest() != rec["moved_sha256"]:
        problems.append("moved_sha256 mismatch")
    if moved.count(b"\n") != rec["moved_lines"]:
        problems.append("moved_lines mismatch")
    text = moved.decode("utf-8")
    for edit in rec["intentional_diff"]:
        if text.count(edit["moved"]) != 1:
            problems.append("intentional edit not found exactly once")
            break
        text = text.replace(edit["moved"], edit["source"], 1)
    source = text.encode("utf-8")
    if hashlib.sha256(source).hexdigest() != rec["source_sha256"]:
        problems.append("source_sha256 not reproducible from the recorded edits")
    if _blob(source) != rec["source_blob_sha1"]:
        problems.append("source blob SHA not reproducible from the recorded edits")
    if not any("hooks" in e["what"] for e in rec["intentional_diff"]):
        problems.append("the hook removal is not recorded as an intentional_diff")
    return problems


def history_problems(d4: dict[str, Any]) -> list[str]:
    """The D4-time record must survive next to the new state."""
    problems: list[str] = []
    rec = d4["documents"][REVIEWER]
    hist = rec.get("historical_pre_d8")
    if not isinstance(hist, dict):
        return ["D4 history of pr-reviewer.md was dropped"]
    if hist.get("moved_sha256") != D4_MOVED_SHA256:
        problems.append("D4 moved_sha256 history was rewritten")
    if "pr_reviewer_guard.py" not in hist.get("frontmatter_hooks", ""):
        problems.append("the D4-time hooks frontmatter is no longer recorded")
    if "I-4-4D4" not in json.dumps(hist.get("intentional_diff", [])):
        problems.append("the D4-time intentional_diff was dropped")
    if rec["moved_sha256"] == hist["moved_sha256"]:
        problems.append(
            "current and D4 moved_sha256 are identical: the hook removal is not recorded"
        )
    return problems


def hookless_problems(d4: dict[str, Any], root: Path) -> list[str]:
    """pr-reviewer.md: no hook, role contract kept, no overclaim, Skills still not auto-invocable."""
    problems: list[str] = []
    path = root / REVIEWER
    if not path.is_file():
        return ["missing pr-reviewer.md"]
    fm = _frontmatter(path)
    text = path.read_text(encoding="utf-8")
    if "hooks" in fm:
        problems.append("pr-reviewer.md re-introduced a hooks declaration (not adopted, D8)")
    if re.search(r"(?m)^hooks:", text.split("---\n", 2)[1]):
        problems.append("pr-reviewer.md frontmatter text contains a hooks key")
    for key, expected in ROLE.items():
        if fm.get(key) != expected:
            problems.append(f"role contract changed: {key}")
    if set(fm.get("tools") or []) & {"Edit", "Write", "MultiEdit"}:
        problems.append("the read-only Agent gained an editing tool")
    if "技術的に拒否できるとは主張しない" not in text:
        problems.append("the no-technical-denial statement was removed")
    if re.search(r"hook[^。\n]{0,30}(が有効|は有効|により.{0,12}拒否される)", text):
        problems.append("the definition presents a hook as an active guard")
    if "I-4-4D4" not in text or "実 dispatch しない" not in text:
        problems.append("the unwired / no-dispatch note was removed")
    if d4["frontmatter"][REVIEWER].get("hooks_forbidden") is not True:
        problems.append("the D4 manifest no longer forbids the hook")
    if (root / ".claude/hooks/pr_reviewer_guard.py").exists():
        problems.append("the hook file exists: it is not migrated")
    for rel in (JUDGE, IRL):
        sk = root / rel
        if not sk.is_file() or _frontmatter(sk).get("disable-model-invocation") is not True:
            problems.append(f"disable-model-invocation was released: {rel}")
    judge = (root / JUDGE).read_text(encoding="utf-8") if (root / JUDGE).is_file() else ""
    if "`.claude/hooks/pr_reviewer_guard.py`、`docs/dev/workflow.md`" in judge:
        problems.append("the judge Skill still lists the hook as an unmigrated hard dependency")
    return problems


def sync_problems(
    d4: dict[str, Any], d5: dict[str, Any], d6: dict[str, Any], d7: dict[str, Any]
) -> list[str]:
    problems: list[str] = []
    for name, doc in (("D5", d5), ("D6", d6), ("D7", d7)):
        entries = [
            e
            for e in doc["unmigrated_references"]["hard_dependencies"]
            if "pr_reviewer_guard.py" in e["reference"]
        ]
        if len(entries) != 1:
            problems.append(f"{name}: the hook dependency record is missing")
            continue
        e = entries[0]
        if e.get("status") != "intentionally_not_adopted_by_d8":
            problems.append(f"{name}: the hook is still listed as a dependency to restore")
        if not e.get("historical_pre_d8", {}).get("owner"):
            problems.append(f"{name}: the pre-D8 owner history was dropped")
        if "migrate the hook" in e["rerun_condition"]:
            problems.append(f"{name}: rerun_condition still asks for the hook migration")
        if "D8" not in doc["handoff"].get("note_i_4_4d8", ""):
            problems.append(f"{name}: the D8 handoff note is missing")
    ref = [
        e
        for e in d4["unmigrated_references"]["later_i_4_4d_slices"]
        if "pr_reviewer_guard.py" in e["reference"]
    ]
    if len(ref) != 1 or ref[0].get("status") != "intentionally_not_adopted_by_d8":
        problems.append("D4: the hook reference is not reclassified")
    elif not ref[0].get("historical_pre_d8", {}).get("owner"):
        problems.append("D4: the pre-D8 owner history was dropped")
    for name, doc, want in (("D5", d5, (108, 108, 0)), ("D6", d6, (173, 173, 0))):
        t = doc["totals"]
        got = (t.get("source_collected", t.get("collected")), t["ran"], t["deferred"])
        if got != want:
            problems.append(f"{name}: observed totals changed: {got}")
    if d7["totals"]["ran"] != 194 or d7["totals"]["collected"] != 194:
        problems.append("D7: closure total changed")
    return problems


def provenance_problems(m: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    p = m["hook_provenance"]
    if set(p["origins"]) != {"project", "agent", "user", "plugin", "session"}:
        problems.append("origins are not all classified")
    for key in ("plugin", "session"):
        if p["origins"].get(key, {}).get("status") != "UNKNOWN":
            problems.append(f"unobserved origin asserted: {key}")
    for key, o in p["origins"].items():
        if o["status"] not in {"none", "observed", "UNKNOWN"} or not o["evidence"]:
            problems.append(f"origin without status / evidence: {key}")
    if "NOT evidence" not in p["conclusion"] or "inferred" not in p["conclusion"]:
        problems.append("the conclusion does not separate inference from the migrated hook")
    if not p["not_done"] or "bypass" not in p["not_done"]:
        problems.append("the no-change / no-bypass statement is missing")
    if re.search(r"/home/|/Users/", json.dumps(m)):
        problems.append("an absolute home path leaked into the record")
    return problems


def publisher_problems(m: dict[str, Any], stage27: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    a = m["publisher_audit"]
    want = {
        "canonical_repository_resolution",
        "closes_refs_reference_policy",
        "pr_body_validation",
        "japanese_content_validation",
        "idempotency_existing_pr_reuse",
        "pr_create_update_execution",
        "task_context_notification",
    }
    rows = {r["id"]: r for r in a["responsibilities"]}
    if set(rows) != want:
        problems.append("publisher responsibilities are incomplete")
    for rid, r in rows.items():
        if not (r.get("owner") and r.get("rerun_condition") and r.get("current_route")):
            problems.append(f"{rid}: owner / rerun_condition / current_route missing")
        if re.search(r"auto(matic)?(ally)? (re-)?migrat", json.dumps(r), re.IGNORECASE):
            problems.append(f"{rid}: requests an automatic migration")
    if rows["closes_refs_reference_policy"]["missing"] is not False:
        problems.append("the D7 evaluator is recorded as missing")
    if (
        "unavailable" not in a["capability_vs_experience"]
        or "unverified" not in a["capability_vs_experience"]
    ):
        problems.append("capability and experience are conflated")
    unavailable = set(stage27["mutation_routes"]["unavailable"])
    if not {"pr_create", "pr_edit", "pr_comment", "pr_update_branch"} <= unavailable:
        problems.append(
            "a pr_* route became available: the record is stale or a permission was added"
        )
    if stage27["mutation_routes"]["permission"] != "unverified":
        problems.append("GitHub write permission is no longer recorded as unverified")
    if m["evidence_boundary"]["pr_publisher_enabled"] is not False:
        problems.append("the evidence boundary claims an enabled publisher")
    return problems


def handoff_problems(m: dict[str, Any], root: Path) -> list[str]:
    problems: list[str] = []
    h = m["handoff_audit"]
    ids = {d["id"] for d in h["defects"]}
    if ids != {
        "approved_body_sha256_caller_format",
        "task_context_adapter_absent",
        "handoff_status_contradiction",
    }:
        problems.append("handoff defects are incomplete")
    for d in h["defects"]:
        for key in ("cause", "effect", "minimal_fix", "owner", "rerun_condition"):
            if not d.get(key):
                problems.append(f"{d['id']}: {key} missing")
    if len(h["not_done"]) != 3 or not any("re-sent" in x for x in h["not_done"]):
        problems.append("the no-resend / no-edit statement is missing")
    if m["evidence_boundary"]["task_context_signal_resent"] is not False:
        problems.append("the boundary claims a signal re-send")
    if (root / "scripts/task-context").exists():
        problems.append("scripts/task-context exists: the adapter-absent record is stale")
    return problems


def boundary_problems(m: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key, value in m["evidence_boundary"].items():
        if key != "kind" and value is not False:
            problems.append(f"evidence boundary overclaims: {key}")
    if m["hook_policy"]["bash_write_denial_claimed"] is not False:
        problems.append("a Bash write denial is claimed")
    if m["hook_policy"]["frontmatter_hooks_present"] is not False:
        problems.append("the hook policy records a hook")
    return problems


def lane_problems(ci_text: str, m: dict[str, Any]) -> list[str]:
    job = ((yaml.safe_load(ci_text) or {}).get("jobs") or {}).get(D8_JOB)
    if not isinstance(job, dict):
        return [f"CI lane missing: {D8_JOB}"]
    runs = "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
    problems: list[str] = []
    if f"run_stage1b1_tests.py {D8_MANIFEST_REL}" not in runs:
        problems.append("CI lane does not run its manifest")
    t = m["totals"]
    if f"{t['deferred']} deferred with reasons, {t['ran']} ran, 0 skipped" not in runs:
        problems.append("CI lane does not pin the manifest ran count")
    if "tests/stage_i4" not in runs:
        problems.append("CI lane does not run the stage_i4 closure tests")
    if "(skipped|xfailed|xpassed|deselected)" not in runs:
        problems.append("CI lane does not reject skipped / xfailed / xpassed / deselected")
    if "check_import_closure.py" not in runs:
        problems.append("CI lane does not run the import closure check")
    return problems


def _copy_tree(root: Path, dst: Path) -> None:
    for rel in (REVIEWER, JUDGE, IRL):
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dst / rel)


# --- AC1 ---------------------------------------------------------------------------------------


def test_ac1_pr_reviewer_has_no_hook_and_keeps_the_role_contract() -> None:
    d4 = _json(D4_MANIFEST)
    assert hookless_problems(d4, ROOT) == []
    fm = _frontmatter(ROOT / REVIEWER)
    assert "hooks" not in fm
    assert {k: fm[k] for k in ROLE} == ROLE


def test_ac1_original_source_pin_and_intentional_diff_reproduce_the_source() -> None:
    d4 = _json(D4_MANIFEST)
    assert source_parity_problems(d4, ROOT) == []
    rec = d4["documents"][REVIEWER]
    assert (rec["source_blob_sha1"], d4["source"]["commit"]) == (SOURCE_BLOB, SOURCE_COMMIT)
    assert len(rec["intentional_diff"]) == 3


def test_ac1_d4_history_is_kept_next_to_the_current_state() -> None:
    d4 = _json(D4_MANIFEST)
    assert history_problems(d4) == []
    hist = d4["documents"][REVIEWER]["historical_pre_d8"]
    assert hist["moved_sha256"] == D4_MOVED_SHA256
    assert hist["moved_lines"] == 89
    assert d4["frontmatter"][REVIEWER]["historical_pre_d8"]["hook_events"] == [
        "PreToolUse",
        "SessionStart",
        "PostToolUse",
    ]


def test_ac1_no_claude_hook_or_settings_file_exists_in_the_repository() -> None:
    assert not (ROOT / ".claude/hooks").exists()
    assert not (ROOT / ".claude/settings.json").exists()
    for agent in (ROOT / ".claude/agents").glob("*.md"):
        assert "hooks" not in _frontmatter(agent), agent.name


def test_ac1_d8_manifest_pins_match_the_files(manifest: dict[str, Any]) -> None:
    docs = manifest["documents"]
    data = (ROOT / REVIEWER).read_bytes()
    assert docs[REVIEWER]["current_moved_sha256"] == hashlib.sha256(data).hexdigest()
    assert docs[REVIEWER]["current_moved_lines"] == data.count(b"\n")
    assert docs[REVIEWER]["d4_moved_sha256"] == D4_MOVED_SHA256
    assert docs[REVIEWER]["source_blob_sha1"] == SOURCE_BLOB
    assert docs[REVIEWER]["unchanged_role_contract"] == ROLE
    judge = (ROOT / JUDGE).read_bytes()
    assert docs[JUDGE]["current_moved_sha256"] == hashlib.sha256(judge).hexdigest()
    assert manifest["owner_decision"]["policy"] == "A"
    assert boundary_problems(manifest) == []


def test_ac1_negative_hook_reintroduction_fails(tmp_path: Path) -> None:
    d4 = _json(D4_MANIFEST)
    _copy_tree(ROOT, tmp_path)
    assert hookless_problems(d4, tmp_path) == []
    reviewer = tmp_path / REVIEWER
    text = reviewer.read_text(encoding="utf-8")
    hooked = text.replace(
        "skills:\n  - pr-review-judge\n---\n",
        "skills:\n  - pr-review-judge\nhooks:\n  PreToolUse:\n    - {matcher: Bash, hooks: "
        "[{type: command, command: x/pr_reviewer_guard.py}]}\n---\n",
        1,
    )
    assert hooked != text
    reviewer.write_text(hooked, encoding="utf-8")
    assert any("re-introduced a hooks" in p for p in hookless_problems(d4, tmp_path))
    # each of the three historical events is rejected, not only PreToolUse
    for event in ("SessionStart", "PostToolUse"):
        reviewer.write_text(hooked.replace("PreToolUse", event), encoding="utf-8")
        assert any("hooks" in p for p in hookless_problems(d4, tmp_path)), event


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("tools:\n  - Bash\n", "tools:\n  - Edit\n  - Bash\n", "editing tool"),
        ("disallowedTools:\n  - Edit\n  - Write\n  - MultiEdit\n", "", "role contract changed"),
        (
            "permissionMode: dontAsk\n",
            "permissionMode: bypassPermissions\n",
            "role contract changed",
        ),
        ("skills:\n  - pr-review-judge\n", "", "role contract changed"),
        ("model: sonnet\n", "model: opus\n", "role contract changed"),
    ],
)
def test_ac1_negative_role_loosening_fails(tmp_path: Path, old: str, new: str, needle: str) -> None:
    d4 = _json(D4_MANIFEST)
    _copy_tree(ROOT, tmp_path)
    reviewer = tmp_path / REVIEWER
    text = reviewer.read_text(encoding="utf-8")
    assert old in text
    reviewer.write_text(text.replace(old, new, 1), encoding="utf-8")
    assert any(needle in p for p in hookless_problems(d4, tmp_path))


@pytest.mark.parametrize(
    "claim",
    [
        "この hook が有効で git commit を拒否する。",
        "Bash の変更コマンドは hook により拒否される。",
    ],
)
def test_ac1_negative_active_protection_claim_fails(tmp_path: Path, claim: str) -> None:
    d4 = _json(D4_MANIFEST)
    _copy_tree(ROOT, tmp_path)
    reviewer = tmp_path / REVIEWER
    reviewer.write_text(reviewer.read_text(encoding="utf-8") + f"\n{claim}\n", encoding="utf-8")
    assert any("active guard" in p for p in hookless_problems(d4, tmp_path))
    reviewer.write_text(
        reviewer.read_text(encoding="utf-8").replace(
            "技術的に拒否できるとは主張しない", "拒否できる"
        ),
        encoding="utf-8",
    )
    assert any("no-technical-denial" in p for p in hookless_problems(d4, tmp_path))


def test_ac1_negative_skill_temporary_disable_release_fails(tmp_path: Path) -> None:
    d4 = _json(D4_MANIFEST)
    _copy_tree(ROOT, tmp_path)
    for rel in (JUDGE, IRL):
        sk = tmp_path / rel
        original = sk.read_text(encoding="utf-8")
        sk.write_text(original.replace("disable-model-invocation: true\n", "", 1), encoding="utf-8")
        assert any(
            "disable-model-invocation was released" in p for p in hookless_problems(d4, tmp_path)
        )
        sk.write_text(original, encoding="utf-8")
    assert hookless_problems(d4, tmp_path) == []


def test_ac1_negative_source_pin_and_history_tampering_fail() -> None:
    d4 = _json(D4_MANIFEST)
    assert source_parity_problems(d4, ROOT) == [] and history_problems(d4) == []
    bad = copy.deepcopy(d4)
    bad["documents"][REVIEWER]["source_blob_sha1"] = "0" * 40
    assert any("Issue-fixed pin" in p for p in source_parity_problems(bad, ROOT))
    bad = copy.deepcopy(d4)
    bad["documents"][REVIEWER]["source_sha256"] = "0" * 64
    assert any("source_sha256 not reproducible" in p for p in source_parity_problems(bad, ROOT))
    bad = copy.deepcopy(d4)
    bad["documents"][REVIEWER]["intentional_diff"].pop(1)  # forget to record the hook removal
    assert source_parity_problems(bad, ROOT)
    bad = copy.deepcopy(d4)
    bad["documents"][REVIEWER]["historical_pre_d8"]["moved_sha256"] = HASH_PLACEHOLDER
    assert any("history was rewritten" in p for p in history_problems(bad))
    bad = copy.deepcopy(d4)
    del bad["documents"][REVIEWER]["historical_pre_d8"]
    assert any("history of pr-reviewer.md was dropped" in p for p in history_problems(bad))
    bad = copy.deepcopy(d4)
    bad["source"]["commit"] = "0" * 40
    assert any("source commit pin changed" in p for p in source_parity_problems(bad, ROOT))


def test_ac1_negative_d4_manifest_allowing_the_hook_again_fails(tmp_path: Path) -> None:
    d4 = copy.deepcopy(_json(D4_MANIFEST))
    _copy_tree(ROOT, tmp_path)
    d4["frontmatter"][REVIEWER]["hooks_forbidden"] = False
    assert any("no longer forbids" in p for p in hookless_problems(d4, tmp_path))


def test_ac1_the_d4_frontmatter_checker_itself_rejects_the_hook(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "_d4_for_d8", ROOT / "tests/stage_i4/test_i4_4d4_skill_agents_readonly.py"
    )
    assert spec is not None and spec.loader is not None
    d4mod = importlib.util.module_from_spec(spec)
    sys.modules["_d4_for_d8"] = d4mod
    spec.loader.exec_module(d4mod)
    d4 = _json(D4_MANIFEST)
    assert d4mod.frontmatter_problems(d4, ROOT) == []
    reviewer = ROOT / REVIEWER
    for rel in d4["frontmatter"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, tmp_path / rel)
    tmp_reviewer = tmp_path / REVIEWER
    text = reviewer.read_text(encoding="utf-8")
    tmp_reviewer.write_text(
        text.replace("---\n\n>", "hooks:\n  PreToolUse: []\n---\n\n>", 1), encoding="utf-8"
    )
    assert any("re-introduced a hook" in p for p in d4mod.frontmatter_problems(d4, tmp_path))


# --- AC2 ---------------------------------------------------------------------------------------


def test_ac2_d4_to_d7_records_are_in_sync_with_the_policy() -> None:
    d4, d5, d6, d7 = (_json(p) for p in (D4_MANIFEST, D5_MANIFEST, D6_MANIFEST, D7_MANIFEST))
    assert sync_problems(d4, d5, d6, d7) == []


def test_ac2_observed_counts_stay_history_and_c2_keeps_one_deferred(
    manifest: dict[str, Any],
) -> None:
    sync = manifest["earlier_slice_sync"]
    assert sync["d5_totals_kept"] == {"collected": 108, "ran": 108, "deferred": 0}
    assert sync["d6_totals_kept"] == {"collected": 173, "ran": 173, "deferred": 0}
    assert sync["d7_closure_ran_kept"] == 194
    assert sync["c2_kept"] == {"ran": 118, "deferred": 1}
    c2 = _json(C2_MANIFEST)
    assert len(c2["deferred_tests"]) == 1
    assert c2["totals"]["ran"] == 118


def test_ac2_d5_judge_note_no_longer_lists_the_hook_as_a_dependency() -> None:
    text = (ROOT / JUDGE).read_text(encoding="utf-8")
    assert "再移管しない" in text
    assert "`.claude/hooks/pr_reviewer_guard.py`、`docs/dev/workflow.md`" not in text
    d5 = _json(D5_MANIFEST)
    rec = d5["documents"][JUDGE]
    assert rec["moved_sha256"] == hashlib.sha256((ROOT / JUDGE).read_bytes()).hexdigest()
    assert rec["historical_pre_d8"]["moved_sha256"] != rec["moved_sha256"]


def test_ac2_negative_sync_regressions_fail() -> None:
    d4, d5, d6, d7 = (_json(p) for p in (D4_MANIFEST, D5_MANIFEST, D6_MANIFEST, D7_MANIFEST))
    for name, doc in (("D5", d5), ("D6", d6), ("D7", d7)):
        bad = copy.deepcopy(doc)
        entry = next(
            e
            for e in bad["unmigrated_references"]["hard_dependencies"]
            if "pr_reviewer_guard.py" in e["reference"]
        )
        entry["status"] = "open"
        args = {"D5": (d4, bad, d6, d7), "D6": (d4, d5, bad, d7), "D7": (d4, d5, d6, bad)}[name]
        assert any("still listed as a dependency" in p for p in sync_problems(*args)), name
        bad = copy.deepcopy(doc)
        entry = next(
            e
            for e in bad["unmigrated_references"]["hard_dependencies"]
            if "pr_reviewer_guard.py" in e["reference"]
        )
        del entry["historical_pre_d8"]
        args = {"D5": (d4, bad, d6, d7), "D6": (d4, d5, bad, d7), "D7": (d4, d5, d6, bad)}[name]
        assert any("history was dropped" in p for p in sync_problems(*args)), name
    bad = copy.deepcopy(d5)
    bad["totals"]["ran"] = 107
    assert any("D5: observed totals changed" in p for p in sync_problems(d4, bad, d6, d7))
    bad = copy.deepcopy(d6)
    bad["totals"]["deferred"] = 1
    assert any("D6: observed totals changed" in p for p in sync_problems(d4, d5, bad, d7))
    bad = copy.deepcopy(d7)
    bad["totals"]["ran"] = 193
    assert any("D7: closure total changed" in p for p in sync_problems(d4, d5, d6, bad))
    bad = copy.deepcopy(d4)
    bad["unmigrated_references"]["later_i_4_4d_slices"] = [
        e
        for e in bad["unmigrated_references"]["later_i_4_4d_slices"]
        if "pr_reviewer_guard.py" not in e["reference"]
    ]
    assert any("D4: the hook reference" in p for p in sync_problems(bad, d5, d6, d7))


def test_ac2_this_closure_adds_no_skip_xfail_or_deselect() -> None:
    text = (ROOT / THIS).read_text(encoding="utf-8")
    marks = "skip|skipif|xfail"
    forbidden = re.compile(r"pytest\.mark\.(" + marks + r")\b|pytest\.(skip|xfail|importorskip)\(")
    assert forbidden.search(text) is None


# --- AC3 ---------------------------------------------------------------------------------------


def test_ac3_hook_origin_is_classified_and_unobserved_parts_are_unknown(
    manifest: dict[str, Any],
) -> None:
    assert provenance_problems(manifest) == []
    origins = manifest["hook_provenance"]["origins"]
    assert origins["project"]["status"] == "none"
    assert origins["user"]["status"] == "observed"
    assert origins["plugin"]["status"] == origins["session"]["status"] == "UNKNOWN"


def test_ac3_the_project_really_has_no_project_scope_hook() -> None:
    # the "project: none" claim is checked against the repository, not just written down
    assert not (ROOT / ".claude/settings.json").exists()
    assert not (ROOT / ".claude/settings.local.json").exists()
    assert not (ROOT / ".claude/hooks").exists()


def test_ac3_publisher_audit_separates_what_exists_from_what_is_missing(
    manifest: dict[str, Any],
) -> None:
    stage27 = _json(STAGE27_MANIFEST)
    assert publisher_problems(manifest, stage27) == []
    audit = manifest["publisher_audit"]
    for rel in audit["not_present_now"]:
        assert not (ROOT / rel).exists(), rel
    for rel in audit["current_open_pr_files"]:
        assert (ROOT / rel).is_file(), rel
    missing = [r["id"] for r in audit["responsibilities"] if r["missing"]]
    assert "closes_refs_reference_policy" not in missing
    assert len(missing) == 6


def test_ac3_the_pr_routes_really_stay_unavailable() -> None:
    stage27 = _json(STAGE27_MANIFEST)
    routes = stage27["mutation_routes"]
    assert {"pr_create", "pr_edit", "pr_comment", "pr_update_branch"} <= set(routes["unavailable"])
    assert routes["permission"] == "unverified"
    assert not any(k.startswith("pr_") for k in routes["available"])


def test_ac3_negative_provenance_and_publisher_overclaims_fail(manifest: dict[str, Any]) -> None:
    stage27 = _json(STAGE27_MANIFEST)
    bad = copy.deepcopy(manifest)
    bad["hook_provenance"]["origins"]["plugin"]["status"] = "none"
    assert any("unobserved origin asserted: plugin" in p for p in provenance_problems(bad))
    bad = copy.deepcopy(manifest)
    del bad["hook_provenance"]["origins"]["session"]
    assert any("not all classified" in p for p in provenance_problems(bad))
    bad = copy.deepcopy(manifest)
    bad["hook_provenance"]["conclusion"] = "The rewrite was done by pr_reviewer_guard.py."
    assert any("does not separate" in p for p in provenance_problems(bad))
    bad = copy.deepcopy(manifest)
    bad["hook_provenance"]["not_done"] = "settings were edited"
    assert any("no-bypass" in p for p in provenance_problems(bad))
    bad = copy.deepcopy(manifest)
    bad["hook_provenance"]["observation"] += " /ho" + "me/someone/.claude/settings.json"
    assert any("absolute home path" in p for p in provenance_problems(bad))
    bad = copy.deepcopy(manifest)
    bad["publisher_audit"]["responsibilities"].pop()
    assert any("incomplete" in p for p in publisher_problems(bad, stage27))
    bad = copy.deepcopy(manifest)
    bad["publisher_audit"]["responsibilities"][0]["owner"] = ""
    assert any("owner / rerun_condition" in p for p in publisher_problems(bad, stage27))
    bad = copy.deepcopy(manifest)
    bad["publisher_audit"]["responsibilities"][0]["rerun_condition"] = (
        "automatically migrate at once"
    )
    assert any("automatic migration" in p for p in publisher_problems(bad, stage27))
    bad = copy.deepcopy(manifest)
    bad["publisher_audit"]["capability_vs_experience"] = (
        "gh pr create worked, so pr_create is enabled"
    )
    assert any("conflated" in p for p in publisher_problems(bad, stage27))
    bad = copy.deepcopy(stage27)
    bad["mutation_routes"]["unavailable"].remove("pr_create")
    assert any("became available" in p for p in publisher_problems(manifest, bad))
    bad = copy.deepcopy(stage27)
    bad["mutation_routes"]["permission"] = "verified"
    assert any("no longer recorded as unverified" in p for p in publisher_problems(manifest, bad))
    bad = copy.deepcopy(manifest)
    bad["evidence_boundary"]["pr_publisher_enabled"] = True
    assert any("enabled publisher" in p for p in publisher_problems(bad, stage27))


# --- AC4 ---------------------------------------------------------------------------------------


def _publisher_module() -> Any:
    scripts = str(ROOT / ".claude/skills/issue-refinement-loop/scripts")
    sys.path.insert(0, scripts)
    try:
        spec = importlib.util.spec_from_file_location("_ptr_for_d8", ROOT / PUBLISHER)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(scripts)


def test_ac4_a_prefixed_hash_is_rejected_by_the_real_publisher_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _publisher_module()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-for-test")
    prefixed = "sha256:" + HASH_PLACEHOLDER
    assert mod._emit_refinement_approved_signal(
        repo="o/r", issue_number=1, approved_body_sha256=prefixed
    ) == ("rejected_evidence", "INVALID_APPROVED_BODY_SHA256")
    upper = HASH_PLACEHOLDER.upper()
    assert mod._emit_refinement_approved_signal(
        repo="o/r", issue_number=1, approved_body_sha256=upper
    ) == ("rejected_evidence", "INVALID_APPROVED_BODY_SHA256")


def test_ac4_a_bare_hash_still_cannot_apply_while_the_adapter_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _publisher_module()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-for-test")
    ctl = ROOT / "scripts" / "task-context" / "task_contextctl.py"
    assert not ctl.exists()
    result = mod._emit_refinement_approved_signal(
        repo="o/r", issue_number=1, approved_body_sha256=HASH_PLACEHOLDER
    )
    # the correct format is not enough: the outcome is deferred, never applied, and nothing is sent
    assert result == ("deferred", "ADAPTER_UNAVAILABLE")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert mod._emit_refinement_approved_signal(
        repo="o/r", issue_number=1, approved_body_sha256=HASH_PLACEHOLDER
    ) == ("deferred", "unbound")


def test_ac4_the_signal_failure_does_not_roll_back_or_fail_the_publish() -> None:
    text = (ROOT / PUBLISHER).read_text(encoding="utf-8")
    assert "Never fail-closed, never rolled back" in text
    assert "task_context_signal_not_applied" in text


def test_ac4_the_policy_forbids_the_101_status_combination() -> None:
    policy = (
        ROOT / ".claude/skills/issue-refinement-loop/references/termination-policy.md"
    ).read_text(encoding="utf-8")
    assert "`metadata.title_prefix_ready == true` または" in policy
    assert "`result: skipped` または `evidence` 欠如" in policy
    assert (
        "| `auto_fixes.skipped` に 1 件以上 | `human_judgment_required` | `ask_human` |" in policy
    )
    assert "audit telemetry" in policy
    assert "authorize しない" in policy


def test_ac4_the_handoff_audit_is_recorded_with_owners_and_no_recovery(
    manifest: dict[str, Any],
) -> None:
    assert handoff_problems(manifest, ROOT) == []
    h = manifest["handoff_audit"]
    assert h["evidence"]["contract_snapshot"].endswith("#issuecomment-6766034634")
    assert h["evidence"]["termination_report"].endswith("#issuecomment-6766041114")
    assert "bare" in h["d103_refinement"]["approved_body_sha256_format"]
    assert "ADAPTER_UNAVAILABLE" in h["d103_refinement"]["task_context_signal"]
    assert "never authorizes implementation" in h["dispatch_evidence"]
    text = json.dumps(h)
    assert not re.search(r"re-?post(ed)? the #101|edit the #101 comment", text)


def test_ac4_negative_handoff_record_regressions_fail(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    bad = copy.deepcopy(manifest)
    bad["handoff_audit"]["defects"].pop()
    assert any("incomplete" in p for p in handoff_problems(bad, ROOT))
    bad = copy.deepcopy(manifest)
    bad["handoff_audit"]["defects"][0]["owner"] = ""
    assert any("owner missing" in p for p in handoff_problems(bad, ROOT))
    bad = copy.deepcopy(manifest)
    bad["handoff_audit"]["not_done"] = ["x", "y", "z"]
    assert any("no-resend" in p for p in handoff_problems(bad, ROOT))
    bad = copy.deepcopy(manifest)
    bad["evidence_boundary"]["task_context_signal_resent"] = True
    assert any("signal re-send" in p for p in handoff_problems(bad, ROOT))
    (tmp_path / "scripts/task-context").mkdir(parents=True)
    assert any("adapter-absent record is stale" in p for p in handoff_problems(manifest, tmp_path))


# --- AC5 ---------------------------------------------------------------------------------------


def _collect(target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_ac5_the_closure_test_count_is_measured_not_assumed(manifest: dict[str, Any]) -> None:
    r = _collect(THIS)
    ids = [ln for ln in r.stdout.splitlines() if "::" in ln]
    assert r.returncode == 0, r.stdout + r.stderr
    assert manifest["expected_collect"] == {THIS: len(ids)}
    assert manifest["expected_ran"] == {THIS: len(ids)}
    assert manifest["totals"]["collected"] == manifest["totals"]["ran"] == len(ids)
    assert manifest["totals"]["deferred"] == 0 == len(manifest["deferred_tests"])
    assert manifest["totals"]["skipped"] == manifest["totals"]["xfailed"] == 0


def test_ac5_the_lane_pins_the_manifest_and_rejects_skips(manifest: dict[str, Any]) -> None:
    text = CI.read_text(encoding="utf-8")
    assert lane_problems(text, manifest) == []
    assert any(
        "lane missing" in p for p in lane_problems(text.replace(D8_JOB, "other-job"), manifest)
    )
    assert any(
        "does not run its manifest" in p
        for p in lane_problems(
            text.replace("i4-4d8-hookless-reviewer-manifest.json | tee", "x.json | tee"), manifest
        )
    )
    pinned = f"0 deferred with reasons, {manifest['totals']['ran']} ran"
    assert any(
        "ran count" in p
        for p in lane_problems(text.replace(pinned, "0 deferred with reasons, 1 ran"), manifest)
    )
    assert any(
        "skipped" in p
        for p in lane_problems(
            text.replace("(skipped|xfailed|xpassed|deselected)", "(none)"), manifest
        )
    )


def test_ac5_required_files_exist_and_the_boundary_is_not_overclaimed(
    manifest: dict[str, Any],
) -> None:
    for rel in manifest["required_files"]:
        assert (ROOT / rel).is_file(), rel
    assert manifest["required_fixtures"] == []
    assert boundary_problems(manifest) == []
    bad = copy.deepcopy(manifest)
    bad["evidence_boundary"]["agent_dispatched"] = True
    assert any("overclaims: agent_dispatched" in p for p in boundary_problems(bad))
    bad = copy.deepcopy(manifest)
    bad["hook_policy"]["bash_write_denial_claimed"] = True
    assert any("Bash write denial" in p for p in boundary_problems(bad))
    bad = copy.deepcopy(manifest)
    bad["evidence_boundary"]["bash_write_denial_verified"] = True
    assert any("overclaims: bash_write_denial_verified" in p for p in boundary_problems(bad))
