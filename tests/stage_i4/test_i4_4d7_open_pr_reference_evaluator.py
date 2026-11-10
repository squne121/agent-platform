"""I-4-4D7 (#101) closure: the open-pr single reference evaluator (`validate_pr_body.py`).

`validate_pr_body.py` is migrated byte-for-byte from the pinned source blob. Its two pure entry
points are exercised through the REAL Python CLI with temporary body / facts files:

* `--evaluate-reference-policy`        -> `Closes` / `Refs` decision (closing_required,
  nonclosing_required, fail_closed) plus the body verdict;
* `--evaluate-native-auto-close-risk`  -> manual closing relation / adopted merge message risk
  (clear, blocked, fail_closed, not_applicable).

The source CLI returns exit code 0 for EVERY structured result (a stop is still exit 0). Every test
therefore asserts the JSON `decision` / `body_verdict` / `status` / `reason_code`, never the exit
code alone; the naive "exit 0 means allowed" consumer is kept as a negative control.

The decision matrix mirrors the source reference-policy and native test matrices
(`test_given_reference_policy_*`, `test_given_native_*`) without weakening any expectation. The
large source `test_validate_pr_body.py` is NOT migrated here (it needs the unmigrated PR template and
lint fixtures) and is not counted as run. This closure is not a Skill invocation, a Reviewer
dispatch, a verdict publish, a Final Gate or a live GitHub result.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "docs" / "dev"
MANIFEST = DEV / "i4-4d7-open-pr-reference-evaluator-manifest.json"
D3_MANIFEST = DEV / "i4-4d3-step4-step5-manifest.json"
D4_MANIFEST = DEV / "i4-4d4-skill-agents-readonly-manifest.json"
D5_MANIFEST = DEV / "i4-4d5-pr-review-consumers-manifest.json"
D6_MANIFEST = DEV / "i4-4d6-pr-review-core-manifest.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
THIS = "tests/stage_i4/test_i4_4d7_open_pr_reference_evaluator.py"
D7_JOB = "i4-4d7-open-pr-reference-evaluator"
D7_MANIFEST_REL = "docs/dev/i4-4d7-open-pr-reference-evaluator-manifest.json"
OPEN_PR = ".claude/skills/open-pr"
SCRIPT_REL = f"{OPEN_PR}/scripts/validate_pr_body.py"
SCRIPT = ROOT / SCRIPT_REL
SOURCE_COMMIT = "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
# Pins fixed by the Issue contract (#101).
SOURCE_BLOB = "9ab611caea3b006132d53782d08670ee978bf923"
SOURCE_TEST_BLOB = "61592bd77da739f172aa87836b403c2dbc522d08"
# Not migrated in D7: they stay absent (no empty stub) and keep an owner in the manifest.
STILL_ABSENT = (
    f"{OPEN_PR}/SKILL.md",
    f"{OPEN_PR}/scripts/open_pr.py",
    f"{OPEN_PR}/scripts/update_pr.py",
    f"{OPEN_PR}/scripts/tests/test_validate_pr_body.py",
    ".github/pull_request_template.md",
    ".claude/hooks/pr_reviewer_guard.py",
    ".claude/skills/implement-issue",
)
DISABLED_SKILLS = (
    (".claude/skills/pr-review-judge/SKILL.md", "pr-review-judge"),
    (".claude/skills/impl-review-loop/SKILL.md", "impl-review-loop"),
)
CONSUMER_DOCS = (
    ".claude/skills/pr-review-judge/SKILL.md",
    ".claude/skills/impl-review-loop/steps/step-5-feedback-and-termination.md",
)
SKIP_RE = re.compile(
    r"pytest\.(mark\.)?(skip|skipif|xfail)|@pytest\.mark\.(skip|xfail)|import" + "orskip"
)

REPO = "squne121/agent-platform"
ISSUE = 330
PR = 2896
FENCE3 = chr(96) * 3
A1_REF_URL = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-777"
A1_LINE = f"Reference-Decision: {A1_REF_URL}"
ISSUE_BASE = f"https://github.com/{REPO}/issues"
RVA = "## Runtime Verification Applicability\n\n"
A3_BODY = RVA + "- decision: immediate\n- reason: x\n"
A2_BODY = (
    RVA + "- decision: deferred\n"
    "- reason: merge 後の live evidence\n"
    "- deferred_destination:\n"
    "    - destination_type: phase\n"
    "    - destination_ref: post-merge-live-evidence\n"
    "- deferred_verification_condition: merge 後に取得する\n"
)
A2_NESTED_HEADING_BODY = (
    RVA + "- decision: deferred\n"
    "### 詳細\n"
    "- deferred_destination:\n"
    "    - destination_type: phase\n"
    "    - destination_ref: post-merge-live-evidence\n"
    "- deferred_verification_condition: merge 後に取得する\n"
)
DEFERRED_OTHER_BODY = A2_BODY.replace("post-merge-live-evidence", "phase-5")
NO_SECTION = "## Outcome\n\nnone\n"
NOTES_330 = "\n## Notes\n\n- Related issue: #330\n"
NOTES_OTHER = "\n## Notes\n\n- Related issue: #331\n"

REFERENCE_RESULT_KEYS = {
    "decision",
    "level",
    "reason_code",
    "repo",
    "issue_number",
    "pr_number",
    "pr_body_sha256",
    "effective_kind",
    "body_verdict",
    "body_reason",
}
REFERENCE_REASON_CODES = {
    "issue_closed",
    "a1_explicit_decision",
    "a1_decision_invalid",
    "a1_decision_ambiguous",
    "a2_contract_deferred",
    "a3_close_ready",
    "runtime_applicability_unresolved",
    "facts_invalid",
}
NATIVE_RESULT_KEYS = {
    "status",
    "reason_code",
    "decision",
    "level",
    "repo",
    "issue_number",
    "pr_number",
    "adopted_message_sha256",
    "findings",
}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def manifest() -> dict[str, Any]:
    return copy.deepcopy(_json(MANIFEST))


def _collect(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def frontmatter_value(path: Path, key: str) -> str | None:
    text = path.read_text(encoding="utf-8")
    block = text.split("---", 2)[1] if text.startswith("---") else ""
    for line in block.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return None


# --- real CLI drivers (isolated tmp_path files; no network, no GitHub, no gh) ---------------


def _comment(**overrides: Any) -> dict[str, Any]:
    comment = {
        "url": A1_REF_URL,
        "id": 777,
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE}",
        "author_association": "OWNER",
        "body": f"REFERENCE_DECISION_V1: nonclosing issue=#{ISSUE}\n",
    }
    comment.update(overrides)
    return comment


def _facts(
    state: str = "OPEN", comment: Any = None, pr_number: Any = None, repo: str = REPO
) -> dict[str, Any]:
    return {"repo": repo, "issue_state": state, "pr_number": pr_number, "decision_comment": comment}


_DEFAULT = object()


def _argv(script: Path, tmp_path: Path, pr_bytes: bytes, facts: Any, issue_body: str, *, omit=()):
    body_file = tmp_path / "body.md"
    body_file.write_bytes(pr_bytes)
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps(facts), encoding="utf-8")
    issue_file = tmp_path / "issue.md"
    issue_file.write_text(issue_body, encoding="utf-8")
    argv = [
        sys.executable,
        str(script),
        "--body-file",
        str(body_file),
        "--linked-issue",
        str(ISSUE),
    ]
    if "issue" not in omit:
        argv += ["--linked-issue-body-file", str(issue_file)]
    if "facts" not in omit:
        argv += ["--reference-facts-file", str(facts_file)]
    return argv


def run_reference(
    tmp_path: Path,
    pr_body: str | bytes,
    *,
    state: str = "OPEN",
    issue_body: str = A3_BODY,
    comment: Any = None,
    pr_number: Any = None,
    facts: Any = _DEFAULT,
    omit: tuple[str, ...] = (),
    script: Path = SCRIPT,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Real `--evaluate-reference-policy`; the structured result is always exit 0 + JSON."""
    pr_bytes = pr_body if isinstance(pr_body, bytes) else pr_body.encode("utf-8")
    wire = _facts(state, comment, pr_number) if facts is _DEFAULT else facts
    argv = _argv(script, tmp_path, pr_bytes, wire, issue_body, omit=omit)
    proc = subprocess.run(
        [*argv, "--evaluate-reference-policy"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stderr == "", proc.stderr
    return proc, json.loads(proc.stdout)


def _native_facts(**overrides: Any) -> dict[str, Any]:
    """Squash-only repository (a live setting, not a hardcoded assumption) with a clean PR."""
    facts: dict[str, Any] = {
        "repo": REPO,
        "closing_relations": [],
        "closing_relations_complete": True,
        "merge_settings": {
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "squash_merge_commit_title": "PR_TITLE",
            "squash_merge_commit_message": "PR_BODY",
        },
        "merge_method": None,
        "pr_title": "feat: 参照と close の分離",
        "pr_body": "## Summary\n\nRefs #330\n",
        "commit_messages": ["feat: 実装\n\nbody"],
        "final_squash_message": None,
    }
    for key, value in overrides.items():
        if key == "merge_settings":
            value = {**facts["merge_settings"], **value}
        facts[key] = value
    return facts


def run_native(
    tmp_path: Path,
    native_facts: Any,
    *,
    pr_body: str = "Refs #330\n",
    issue_body: str = A2_BODY,
    state: str = "OPEN",
    comment: Any = None,
    omit_native: bool = False,
    script: Path = SCRIPT,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Real `--evaluate-native-auto-close-risk`; every status is exit 0 + JSON."""
    native_file = tmp_path / "native.json"
    native_file.write_text(
        native_facts if isinstance(native_facts, str) else json.dumps(native_facts),
        encoding="utf-8",
    )
    argv = _argv(
        script,
        tmp_path,
        pr_body.encode("utf-8"),
        _facts(state, comment, PR),
        issue_body,
    )
    argv.append("--evaluate-native-auto-close-risk")
    if not omit_native:
        argv += ["--native-close-facts-file", str(native_file)]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=120)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stderr == "", proc.stderr
    return proc, json.loads(proc.stdout)


def naive_exit_code_consumer(proc: subprocess.CompletedProcess[str]) -> bool:
    """The WRONG consumer: treats exit 0 as an allow decision (kept as a negative control)."""
    return proc.returncode == 0


def strict_reference_consumer(result: dict[str, Any]) -> bool:
    """Allow only a structured valid body verdict under a non-fail_closed decision."""
    return result["decision"] != "fail_closed" and result["body_verdict"] == "valid"


def strict_native_consumer(result: dict[str, Any]) -> bool:
    """`clear` is the only status that proves the absence of a native auto-close path."""
    return result["status"] == "clear"


# --- the reference-policy matrix (mirrors the source matrix 1:1) ---------------------------

D_A3 = ("closing_required", "A3", "a3_close_ready")
D_A2 = ("nonclosing_required", "A2", "a2_contract_deferred")
D_A1 = ("nonclosing_required", "A1", "a1_explicit_decision")
D_CLOSED = ("nonclosing_required", "CLOSED", "issue_closed")
CLOSING_OK = ("closing", "valid", "ok")
REFS_OK = ("non-closing", "valid", "ok")
REPAIR = ("non-closing", "repair", "closing_missing")
FORBIDDEN = ("closing", "block", "closing_forbidden")
NONE_MISSING = ("none", "block", "reference_missing")
NOTES_MISSING = ("notes-only", "block", "reference_missing")
NOTES_VALID = ("notes-only", "valid", "ok")
OTHER_NONE = ("none", "block", "closing_for_other")
OTHER_REFS = ("non-closing", "block", "closing_for_other")
NOT_EVALUATED = ("none", "block", "not_evaluated")
UNRESOLVED = ("fail_closed", None, "runtime_applicability_unresolved")
A1_INVALID = ("fail_closed", None, "a1_decision_invalid")
A1_AMBIGUOUS = ("fail_closed", None, "a1_decision_ambiguous")
OPEN_A3 = {"state": "OPEN", "issue_body": A3_BODY}
OPEN_A2 = {"state": "OPEN", "issue_body": A2_BODY}
CLOSED = {"state": "CLOSED", "issue_body": A3_BODY}


def _a1(**comment_overrides: Any) -> dict[str, Any]:
    return {**OPEN_A3, "comment": _comment(**comment_overrides)}


def _open(issue_body: str, **extra: Any) -> dict[str, Any]:
    return {"state": "OPEN", "issue_body": issue_body, **extra}


REFERENCE_POLICY_MATRIX: list[tuple[str, dict[str, Any], str, tuple, tuple]] = [
    # --- authority rows A3 / A2 with the body mapping ---------------------------------------
    ("a3_closes", OPEN_A3, "Closes #330", D_A3, CLOSING_OK),
    ("a3_refs_only_repair", OPEN_A3, "Refs #330", D_A3, REPAIR),
    ("a3_none", OPEN_A3, "no reference", D_A3, NONE_MISSING),
    ("a3_notes_only", OPEN_A3, NOTES_330, D_A3, NOTES_MISSING),
    ("a3_notes_other_number", OPEN_A3, NOTES_OTHER, D_A3, NONE_MISSING),
    ("a3_closes_and_refs_coexist", OPEN_A3, "Refs #330\nCloses #330", D_A3, CLOSING_OK),
    ("a3_duplicate_closes", OPEN_A3, "Closes #330\nCloses #330", D_A3, CLOSING_OK),
    (
        "a3_not_applicable",
        _open(RVA + "decision: not_applicable\n"),
        "Closes #330",
        D_A3,
        CLOSING_OK,
    ),
    ("a3_deferred_other_destination", _open(DEFERRED_OTHER_BODY), "Closes #330", D_A3, CLOSING_OK),
    ("a2_refs", OPEN_A2, "Refs #330", D_A2, REFS_OK),
    ("a2_refs_duplicate", OPEN_A2, "Refs #330\nrefs #330", D_A2, REFS_OK),
    ("a2_closes_forbidden", OPEN_A2, "Closes #330", D_A2, FORBIDDEN),
    ("a2_closes_and_refs_forbidden", OPEN_A2, "Refs #330\nCloses #330", D_A2, FORBIDDEN),
    ("a2_none", OPEN_A2, "nothing", D_A2, NONE_MISSING),
    ("a2_notes_only", OPEN_A2, NOTES_330, D_A2, NOTES_MISSING),
    ("a2_nested_heading_ok", _open(A2_NESTED_HEADING_BODY), "Refs #330", D_A2, REFS_OK),
    # --- every GitHub closing keyword and every target grammar ------------------------------
    *[
        (f"a3_keyword_{kw.replace(':', '_colon')}", OPEN_A3, f"{kw} #330", D_A3, CLOSING_OK)
        for kw in (
            "Close",
            "Closes",
            "Closed",
            "Fix",
            "Fixes",
            "Fixed",
            "Resolve",
            "Resolves",
            "Resolved",
            "CLOSES",
            "fixes:",
        )
    ],
    ("a3_cross_repo_form", OPEN_A3, f"Fixes {REPO}#330", D_A3, CLOSING_OK),
    ("a3_issue_url_form", OPEN_A3, f"Resolves {ISSUE_BASE}/330", D_A3, CLOSING_OK),
    ("a2_fixes_forbidden", OPEN_A2, "Fixes #330\nRefs #330", D_A2, FORBIDDEN),
    # --- closing for another issue / repository / prefix collision --------------------------
    ("other_number", OPEN_A3, "Closes #331", D_A3, OTHER_NONE),
    (
        "other_number_with_target",
        OPEN_A3,
        "Closes #330\nFixes #331",
        D_A3,
        ("closing", "block", "closing_for_other"),
    ),
    ("other_repository", OPEN_A3, "Closes other/repo#330", D_A3, OTHER_NONE),
    ("prefix_collision_longer", OPEN_A3, "Closes #3301", D_A3, OTHER_NONE),
    ("prefix_collision_shorter", OPEN_A3, "Closes #33", D_A3, OTHER_NONE),
    ("refs_prefix_collision_is_not_a_reference", OPEN_A2, "Refs #3301", D_A2, NONE_MISSING),
    ("other_blocks_valid_refs", OPEN_A2, "Refs #330\nCloses #331", D_A2, OTHER_REFS),
    # --- ordinary prose that merely looks like a keyword is ignored, never fail_closed ------
    (
        "prose_then_closes",
        OPEN_A3,
        "fixes the typo\nresolved by the earlier change\ncloses the loop\nCloses #330",
        D_A3,
        CLOSING_OK,
    ),
    (
        "prose_only_is_none",
        OPEN_A3,
        "fixes the typo\nresolved by the earlier change\ncloses the loop",
        D_A3,
        NONE_MISSING,
    ),
    ("prose_with_refs_under_a2", OPEN_A2, "closes the loop\nRefs #330", D_A2, REFS_OK),
    # --- code fence / quote / inline code handling (two separate decisions) -----------------
    (
        "a2_closing_in_fence_blocked",
        OPEN_A2,
        f"Refs #330\n{FENCE3}\nCloses #330\n{FENCE3}\n",
        D_A2,
        FORBIDDEN,
    ),
    ("a2_closing_in_quote_blocked", OPEN_A2, "Refs #330\n> Fixes #330\n", D_A2, FORBIDDEN),
    (
        "other_in_fence_detected",
        OPEN_A2,
        f"Refs #330\n{FENCE3}\nFixes #331\n{FENCE3}\n",
        D_A2,
        OTHER_REFS,
    ),
    (
        "a3_closing_only_in_fence_repair",
        OPEN_A3,
        f"{FENCE3}\nCloses #330\n{FENCE3}\n",
        D_A3,
        ("closing", "repair", "closing_missing"),
    ),
    (
        "a3_closing_only_in_fence_with_outside_refs",
        OPEN_A3,
        f"Refs #330\n{FENCE3}\nCloses #330\n{FENCE3}\n",
        D_A3,
        ("closing", "repair", "closing_missing"),
    ),
    (
        "a3_closing_only_in_quote_repair",
        OPEN_A3,
        "> Closes #330\n",
        D_A3,
        ("closing", "repair", "closing_missing"),
    ),
    (
        "a3_fence_closing_plus_real_closing",
        OPEN_A3,
        f"{FENCE3}\nCloses #330\n{FENCE3}\nCloses #330",
        D_A3,
        CLOSING_OK,
    ),
    ("refs_in_fence_not_counted", OPEN_A2, f"{FENCE3}\nRefs #330\n{FENCE3}\n", D_A2, NONE_MISSING),
    ("refs_in_quote_not_counted", OPEN_A2, "> Refs #330\n", D_A2, NONE_MISSING),
    ("refs_in_inline_code_not_counted", OPEN_A2, "see `Refs #330`", D_A2, NONE_MISSING),
    # --- CLOSED Issue: dedicated mapping, no authority evaluation ---------------------------
    ("closed_refs", CLOSED, "Refs #330", D_CLOSED, REFS_OK),
    ("closed_closes_forbidden", CLOSED, "Closes #330", D_CLOSED, FORBIDDEN),
    (
        "closed_closing_in_fence_forbidden",
        CLOSED,
        f"{FENCE3}\nFixes #330\n{FENCE3}\n",
        D_CLOSED,
        FORBIDDEN,
    ),
    ("closed_notes_only_valid", CLOSED, NOTES_330, D_CLOSED, NOTES_VALID),
    ("closed_none", CLOSED, "nothing", D_CLOSED, NONE_MISSING),
    ("closed_notes_other_number", CLOSED, NOTES_OTHER, D_CLOSED, NONE_MISSING),
    ("closed_closing_for_other", CLOSED, "Refs #330\nCloses #331", D_CLOSED, OTHER_REFS),
    (
        "closed_ignores_invalid_a1_line",
        CLOSED,
        "Refs #330\nReference-Decision: not-a-url",
        D_CLOSED,
        REFS_OK,
    ),
    # --- A1 explicit decision ----------------------------------------------------------------
    ("a1_valid_over_a3", _a1(), f"Refs #330\n{A1_LINE}", D_A1, REFS_OK),
    (
        "a1_valid_without_applicability_section",
        _open(NO_SECTION, comment=_comment()),
        f"Refs #330\n{A1_LINE}",
        D_A1,
        REFS_OK,
    ),
    ("a1_valid_with_closing_forbidden", _a1(), f"Closes #330\n{A1_LINE}", D_A1, FORBIDDEN),
    ("a1_valid_member", _a1(author_association="MEMBER"), f"Refs #330\n{A1_LINE}", D_A1, REFS_OK),
    (
        "a1_valid_collaborator",
        _a1(author_association="COLLABORATOR"),
        f"Refs #330\n{A1_LINE}",
        D_A1,
        REFS_OK,
    ),
    ("a1_ignored_when_indented", _a1(), f"Closes #330\n  {A1_LINE}", D_A3, CLOSING_OK),
    # --- A2 / A3 unresolved -> fail_closed --------------------------------------------------
    ("applicability_missing", _open(NO_SECTION), "Refs #330", UNRESOLVED, NOT_EVALUATED),
    (
        "applicability_duplicated",
        _open(A3_BODY + "\n" + A3_BODY),
        "Refs #330",
        UNRESOLVED,
        NOT_EVALUATED,
    ),
    (
        "applicability_only_in_fence",
        _open(f"{FENCE3}\n{A2_BODY}{FENCE3}\n"),
        "Refs #330",
        UNRESOLVED,
        NOT_EVALUATED,
    ),
    (
        "applicability_decision_not_in_enum",
        _open(RVA + "- decision: maybe\n"),
        "Refs #330",
        UNRESOLVED,
        NOT_EVALUATED,
    ),
    (
        "applicability_decision_missing",
        _open(RVA + "- reason: x\n"),
        "Refs #330",
        UNRESOLVED,
        NOT_EVALUATED,
    ),
    (
        "applicability_decision_repeated",
        _open(RVA + "- decision: immediate\n- decision: deferred\n"),
        "Refs #330",
        UNRESOLVED,
        NOT_EVALUATED,
    ),
    (
        "applicability_deferred_without_destination",
        _open(RVA + "- decision: deferred\n- deferred_verification_condition: x\n"),
        "Refs #330",
        UNRESOLVED,
        NOT_EVALUATED,
    ),
    (
        "applicability_deferred_without_condition",
        _open(A2_BODY.replace("- deferred_verification_condition: merge 後に取得する\n", "")),
        "Refs #330",
        UNRESOLVED,
        NOT_EVALUATED,
    ),
    # --- A1 present-but-invalid never degrades to A2 / A3 -----------------------------------
    (
        "a1_invalid_comment_missing",
        {**OPEN_A2, "comment": None},
        f"Refs #330\n{A1_LINE}",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_url_shape",
        {**OPEN_A2, "comment": _comment()},
        "Refs #330\nReference-Decision: https://example.com/x",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_other_issue_number",
        _a1(),
        f"Refs #330\nReference-Decision: {ISSUE_BASE}/331#issuecomment-777",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_other_repository",
        _a1(),
        "Refs #330\nReference-Decision: https://github.com/other/repo/issues/330#issuecomment-777",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_comment_url_mismatch",
        _a1(url=A1_REF_URL + "0"),
        f"Refs #330\n{A1_LINE}",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_comment_id_mismatch",
        _a1(id=778),
        f"Refs #330\n{A1_LINE}",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_comment_issue_url_mismatch",
        _a1(issue_url=f"https://api.github.com/repos/{REPO}/issues/331"),
        f"Refs #330\n{A1_LINE}",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_insufficient_authority",
        _a1(author_association="CONTRIBUTOR"),
        f"Refs #330\n{A1_LINE}",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_marker_missing",
        _a1(body="no marker"),
        f"Refs #330\n{A1_LINE}",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_marker_for_other_issue",
        _a1(body="REFERENCE_DECISION_V1: nonclosing issue=#331"),
        f"Refs #330\n{A1_LINE}",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_marker_twice",
        _a1(
            body="REFERENCE_DECISION_V1: nonclosing issue=#330\n"
            "REFERENCE_DECISION_V1: nonclosing issue=#330"
        ),
        f"Refs #330\n{A1_LINE}",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_invalid_marker_not_a_whole_line",
        _a1(body="note REFERENCE_DECISION_V1: nonclosing issue=#330"),
        f"Refs #330\n{A1_LINE}",
        A1_INVALID,
        NOT_EVALUATED,
    ),
    (
        "a1_ambiguous_two_different_lines",
        _a1(),
        f"Refs #330\n{A1_LINE}\n{A1_LINE}9",
        A1_AMBIGUOUS,
        NOT_EVALUATED,
    ),
    (
        "a1_ambiguous_same_url_twice",
        _a1(),
        f"Refs #330\n{A1_LINE}\n{A1_LINE}",
        A1_AMBIGUOUS,
        NOT_EVALUATED,
    ),
    (
        "a1_ambiguous_inside_a_fence_still_counts",
        {**OPEN_A2, "comment": _comment()},
        f"Refs #330\n{A1_LINE}\n{FENCE3}\n{A1_LINE}\n{FENCE3}\n",
        A1_AMBIGUOUS,
        NOT_EVALUATED,
    ),
]


def _matrix_params() -> list[Any]:
    ids = [case[0] for case in REFERENCE_POLICY_MATRIX]
    assert len(ids) == len(set(ids)), "duplicate matrix id"
    return [pytest.param(*case[1:], id=case[0]) for case in REFERENCE_POLICY_MATRIX]


# =============================================================================================
# AC1: source parity, intentional diff, owner inventory, static contract with the consumers
# =============================================================================================


def document_problems(m: dict[str, Any], root: Path) -> list[str]:
    """The migrated script equals the pinned source blob; the recorded diff is empty."""
    problems: list[str] = []
    entry = m["documents"].get(SCRIPT_REL)
    path = root / SCRIPT_REL
    if entry is None or not path.is_file():
        return [f"migrated script missing or not recorded: {SCRIPT_REL}"]
    data = path.read_bytes()
    if entry["source_blob_sha1"] != SOURCE_BLOB:
        problems.append("manifest source blob differs from the Issue-fixed pin")
    if git_blob_sha1(data) != SOURCE_BLOB:
        problems.append("moved script is not the pinned source blob (git blob sha1)")
    if hashlib.sha256(data).hexdigest() != entry["moved_sha256"]:
        problems.append("moved sha256 differs from the manifest")
    if entry["moved_sha256"] != entry["source_sha256"] or entry.get("verbatim") is not True:
        problems.append("script is recorded as modified, but a minimal verbatim move was required")
    if entry["intentional_diff"] != []:
        problems.append("an intentional diff is recorded for a verbatim move")
    if (
        len(data.decode("utf-8").splitlines()) != entry["moved_lines"]
        or entry["moved_lines"] != (entry["source_lines"])
    ):
        problems.append("line counts differ from the manifest")
    return problems


def test_ac1_script_reproduces_the_pinned_source_blob(manifest: dict[str, Any]) -> None:
    assert document_problems(manifest, ROOT) == []
    assert manifest["source"]["commit"] == SOURCE_COMMIT
    assert manifest["source"]["read_only"] is True
    assert manifest["source_test_scope"]["source_test_blob_sha1"] == SOURCE_TEST_BLOB


def test_ac1_source_tampering_is_detected(manifest: dict[str, Any], tmp_path: Path) -> None:
    dest = tmp_path / SCRIPT_REL
    dest.parent.mkdir(parents=True)
    shutil.copy(SCRIPT, dest)
    assert document_problems(manifest, tmp_path) == []
    original = dest.read_bytes()
    dest.write_bytes(original + b"# tampered\n")
    assert any("pinned source blob" in p for p in document_problems(manifest, tmp_path))
    # a weakened decision table (an A2 `Closes` accepted as valid) is also a source change
    weakened = original.replace(
        b'return kind, "block", "closing_forbidden"', b'return kind, "valid", "ok"'
    )
    assert weakened != original
    dest.write_bytes(weakened)
    assert any("pinned source blob" in p for p in document_problems(manifest, tmp_path))
    dest.write_bytes(original)
    forged = copy.deepcopy(manifest)
    forged["documents"][SCRIPT_REL]["intentional_diff"] = [{"moved": "x"}]
    assert any("intentional diff" in p for p in document_problems(forged, tmp_path))
    forged = copy.deepcopy(manifest)
    forged["documents"][SCRIPT_REL]["source_blob_sha1"] = "0" * 40
    assert any("Issue-fixed pin" in p for p in document_problems(forged, tmp_path))


def test_ac1_script_has_no_github_or_network_io() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__",
        "argparse",
        "hashlib",
        "json",
        "re",
        "sys",
        "dataclasses",
        "pathlib",
        "typing",
        "yaml",
        "importlib",
    }, imported
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("subprocess", "urllib", "socket", "requests", "os.system"):
        assert forbidden not in text, forbidden


def test_ac1_cli_flags_match_what_the_consumers_document() -> None:
    help_text = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert help_text.returncode == 0, help_text.stderr
    flags = (
        "--evaluate-reference-policy",
        "--evaluate-native-auto-close-risk",
        "--reference-facts-file",
        "--native-close-facts-file",
        "--linked-issue-body-file",
        "--body-file",
        "--linked-issue",
    )
    for flag in flags:
        assert flag in help_text.stdout, flag
    judge = (ROOT / CONSUMER_DOCS[0]).read_text(encoding="utf-8")
    for flag in flags[:4]:
        assert flag in judge, flag
    step5 = (ROOT / CONSUMER_DOCS[1]).read_text(encoding="utf-8")
    assert "--evaluate-reference-policy" in step5
    assert "--evaluate-native-auto-close-risk" in step5
    # the documented entry-point path is the migrated one
    for text in (judge, step5):
        assert SCRIPT_REL in text
    # the exact fields the consumers read exist in the real output
    for field in ("decision", "level", "reason_code", "body_verdict", "body_reason"):
        assert field in REFERENCE_RESULT_KEYS and f"`{field}`" in judge


def test_ac1_unmigrated_dependencies_stay_absent_and_owned(manifest: dict[str, Any]) -> None:
    deps = manifest["unmigrated_references"]["hard_dependencies"]
    assert deps
    for e in deps:
        for key in ("reference", "owner", "reason", "rerun_condition"):
            assert e.get(key), (key, e.get("reference"))
    text = "\n".join(e["reference"] for e in deps)
    for needed in (
        "SKILL.md",
        "open_pr.py",
        "update_pr.py",
        "pull_request_template.md",
        "test_validate_pr_body.py",
        "pr_reviewer_guard.py",
    ):
        assert needed in text, needed
    for rel in STILL_ABSENT:
        assert not (ROOT / rel).exists(), rel
    files = sorted(
        p.relative_to(ROOT).as_posix() for p in (ROOT / OPEN_PR).rglob("*") if p.is_file()
    )
    assert files == [SCRIPT_REL], files  # no empty stub stands in for an unmigrated file


def test_ac1_no_skill_is_enabled_by_the_migration(manifest: dict[str, Any]) -> None:
    for rel, name in DISABLED_SKILLS:
        path = ROOT / rel
        assert frontmatter_value(path, "name") == name
        assert frontmatter_value(path, "disable-model-invocation") == "true", rel
    boundary = manifest["evidence_boundary"]
    for key, value in boundary.items():
        if key == "kind":
            continue
        assert value is False, key


def test_ac1_source_test_scope_maps_the_decision_matrices(manifest: dict[str, Any]) -> None:
    scope = manifest["source_test_scope"]
    assert scope["full_source_suite_migrated"] is False
    assert scope["full_source_suite_counted_as_run"] is False
    mapped = scope["mapped_source_tests"]
    names = {e["source_test"] for e in mapped}
    for needed in (
        "test_given_reference_policy_matrix_when_validated_then_accept_or_reject",
        "test_given_reference_facts_wire_when_evaluated_then_exact_keys_and_body_bytes_hash",
        "test_given_malformed_reference_facts_when_evaluated_then_facts_invalid",
        "test_given_valid_refs_without_native_close_path_when_checked_then_clear_with_adopted_message_hash",
        "test_given_native_facts_file_when_cli_runs_then_json_with_exit_zero_for_every_status",
    ):
        assert needed in names, needed
    # every mapped entry names the D7 test prefix that covers it, and that prefix is collected
    collected = _collect(THIS).stdout
    for e in mapped:
        assert e["d7_tests"], e
        assert re.search(re.escape(e["d7_tests"]), collected), e
    not_covered = scope["not_covered_source_tests"]
    assert not_covered
    for e in not_covered:
        for key in ("source_test", "reason", "owner", "rerun_condition"):
            assert e.get(key), (key, e)


# =============================================================================================
# AC2: --evaluate-reference-policy through the real CLI
# =============================================================================================


@pytest.mark.parametrize("kwargs,pr_body,decision,body", _matrix_params())
def test_ac2_reference_policy_matrix_through_the_real_cli(
    tmp_path: Path, kwargs: dict[str, Any], pr_body: str, decision: tuple, body: tuple
) -> None:
    proc, result = run_reference(tmp_path, pr_body, **kwargs)
    assert set(result) == REFERENCE_RESULT_KEYS
    assert (result["decision"], result["level"], result["reason_code"]) == decision, result
    assert result["reason_code"] in REFERENCE_REASON_CODES
    if decision[0] == "fail_closed":
        # fail_closed is always a stop: block / not_evaluated, level null, yet still exit 0
        assert (result["body_verdict"], result["body_reason"]) == ("block", "not_evaluated")
        assert proc.returncode == 0
        assert strict_reference_consumer(result) is False
        return
    assert (result["effective_kind"], result["body_verdict"], result["body_reason"]) == body, result
    assert strict_reference_consumer(result) is (body[1] == "valid")


def test_ac2_wire_keys_body_bytes_hash_and_binding(tmp_path: Path) -> None:
    crlf_body = b"Closes #330\r\nsecond line\r\n"
    proc, result = run_reference(tmp_path, crlf_body)
    assert proc.returncode == 0
    assert set(result) == REFERENCE_RESULT_KEYS
    assert (result["decision"], result["level"]) == ("closing_required", "A3")
    assert result["repo"] == REPO and result["issue_number"] == ISSUE
    assert result["pr_number"] is None
    # sha256 of the exact bytes (CRLF kept), lowercase hex, not of a newline-normalized string
    assert result["pr_body_sha256"] == hashlib.sha256(crlf_body).hexdigest()
    assert result["pr_body_sha256"] != hashlib.sha256(crlf_body.replace(b"\r\n", b"\n")).hexdigest()
    assert result["pr_body_sha256"] == result["pr_body_sha256"].lower()
    assert (result["effective_kind"], result["body_verdict"], result["body_reason"]) == CLOSING_OK
    # the hash binds the decision to one body: a changed body yields another hash
    _, changed = run_reference(tmp_path, crlf_body + b"x")
    assert changed["pr_body_sha256"] != result["pr_body_sha256"]
    # pr_number from the facts is carried through
    _, with_pr = run_reference(tmp_path, "Closes #330", pr_number=PR)
    assert with_pr["pr_number"] == PR


def test_ac2_a_stop_is_still_exit_zero_so_the_exit_code_cannot_authorise(tmp_path: Path) -> None:
    cases = [
        run_reference(tmp_path, "Refs #330", issue_body="no section"),  # fail_closed
        run_reference(tmp_path, "Refs #330", issue_body=A3_BODY),  # closing_required + repair
        run_reference(tmp_path, "Closes #330", issue_body=A2_BODY),  # forbidden -> block
        run_reference(tmp_path, "Closes #331", issue_body=A3_BODY),  # closing for other -> block
    ]
    assert [proc.returncode for proc, _ in cases] == [0, 0, 0, 0]
    verdicts = [(r["decision"], r["body_verdict"], r["body_reason"]) for _, r in cases]
    assert verdicts == [
        ("fail_closed", "block", "not_evaluated"),
        ("closing_required", "repair", "closing_missing"),
        ("nonclosing_required", "block", "closing_forbidden"),
        ("closing_required", "block", "closing_for_other"),
    ]
    # the wrong consumer accepts all of them; the structured consumer accepts none
    assert all(naive_exit_code_consumer(proc) for proc, _ in cases)
    assert not any(strict_reference_consumer(r) for _, r in cases)
    # the right pairs are accepted by the structured consumer
    assert strict_reference_consumer(run_reference(tmp_path, "Closes #330")[1])
    assert strict_reference_consumer(run_reference(tmp_path, "Refs #330", issue_body=A2_BODY)[1])


@pytest.mark.parametrize("omitted", ["facts", "issue"])
def test_ac2_missing_input_flag_is_facts_invalid(tmp_path: Path, omitted: str) -> None:
    proc, result = run_reference(tmp_path, "Refs #330", omit=(omitted,))
    assert proc.returncode == 0
    assert (result["decision"], result["reason_code"]) == ("fail_closed", "facts_invalid")
    assert set(result) == REFERENCE_RESULT_KEYS
    assert (result["body_verdict"], result["body_reason"]) == ("block", "not_evaluated")


def test_ac2_unreadable_body_is_the_only_exit_two(tmp_path: Path) -> None:
    unreadable = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--body-file",
            str(tmp_path / "nope.md"),
            "--linked-issue",
            str(ISSUE),
            "--evaluate-reference-policy",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert unreadable.returncode == 2 and unreadable.stdout == ""
    assert "Cannot read body file" in unreadable.stderr
    # a non-UTF-8 body is a structured stop, not an exception
    _, result = run_reference(tmp_path, b"Closes #330 \xff\xfe")
    assert (result["decision"], result["reason_code"]) == ("fail_closed", "facts_invalid")


MALFORMED_REFERENCE_FACTS = {
    "unknown_key": lambda f: {**f, "extra": 1},
    "missing_repo": lambda f: {k: v for k, v in f.items() if k != "repo"},
    "state_merged": lambda f: {**f, "issue_state": "MERGED"},
    "state_null": lambda f: {**f, "issue_state": None},
    "state_list": lambda f: {**f, "issue_state": []},
    "state_object": lambda f: {**f, "issue_state": {}},
    "state_bool": lambda f: {**f, "issue_state": True},
    "state_int": lambda f: {**f, "issue_state": 5},
    "pr_zero": lambda f: {**f, "pr_number": 0},
    "pr_bool": lambda f: {**f, "pr_number": True},
    "pr_string": lambda f: {**f, "pr_number": "7"},
    "repo_not_a_repo": lambda f: {**f, "repo": "not-a-repo"},
    "repo_int": lambda f: {**f, "repo": 5},
    "updated_at_is_never_a_fact": lambda f: {**f, "updated_at": "2026-10-03T00:00:00Z"},
    "comment_unknown_key": lambda f: {**f, "decision_comment": {**_comment(), "extra": 1}},
    "comment_missing_body": lambda f: {
        **f,
        "decision_comment": {k: v for k, v in _comment().items() if k != "body"},
    },
    "comment_id_string": lambda f: {**f, "decision_comment": {**_comment(), "id": "777"}},
    "comment_id_bool": lambda f: {**f, "decision_comment": {**_comment(), "id": True}},
    "comment_not_an_object": lambda f: {**f, "decision_comment": "comment"},
    "facts_is_a_list": lambda f: ["not", "an", "object"],
    "facts_is_null": lambda f: None,
}


@pytest.mark.parametrize(
    "mutate", list(MALFORMED_REFERENCE_FACTS.values()), ids=list(MALFORMED_REFERENCE_FACTS)
)
def test_ac2_malformed_authority_facts_are_a_structured_stop(tmp_path: Path, mutate: Any) -> None:
    facts = mutate(_facts(comment=_comment()))
    proc, result = run_reference(tmp_path, "Refs #330", facts=facts)
    assert proc.returncode == 0
    assert (result["decision"], result["level"], result["reason_code"]) == (
        "fail_closed",
        None,
        "facts_invalid",
    )
    assert set(result) == REFERENCE_RESULT_KEYS
    assert strict_reference_consumer(result) is False


def test_ac2_non_json_facts_file_is_facts_invalid(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Refs #330", encoding="utf-8")
    facts_file = tmp_path / "facts.json"
    facts_file.write_text("{not json", encoding="utf-8")
    issue_file = tmp_path / "issue.md"
    issue_file.write_text(A2_BODY, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--body-file",
            str(body_file),
            "--linked-issue",
            str(ISSUE),
            "--linked-issue-body-file",
            str(issue_file),
            "--reference-facts-file",
            str(facts_file),
            "--evaluate-reference-policy",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert (result["decision"], result["reason_code"]) == ("fail_closed", "facts_invalid")


def test_ac2_wrong_linked_issue_number_is_not_accepted(tmp_path: Path) -> None:
    # the body references #330 but the caller asks about #331: the reference is for another Issue
    body_file = tmp_path / "body.md"
    body_file.write_text("Closes #330", encoding="utf-8")
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps(_facts()), encoding="utf-8")
    issue_file = tmp_path / "issue.md"
    issue_file.write_text(A3_BODY, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--body-file",
            str(body_file),
            "--linked-issue",
            "331",
            "--linked-issue-body-file",
            str(issue_file),
            "--reference-facts-file",
            str(facts_file),
            "--evaluate-reference-policy",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert (result["decision"], result["issue_number"]) == ("closing_required", 331)
    assert (result["effective_kind"], result["body_verdict"], result["body_reason"]) == OTHER_NONE
    assert strict_reference_consumer(result) is False


# =============================================================================================
# AC3: --evaluate-native-auto-close-risk through the real CLI
# =============================================================================================

MANUAL_RELATION = [{"number": ISSUE, "repository": REPO}]


def test_ac3_clear_with_a_bound_adopted_message_hash(tmp_path: Path) -> None:
    for kwargs in (
        {},
        {"issue_body": A3_BODY, "comment": _comment(), "pr_body": f"Refs #330\n{A1_LINE}\n"},
    ):
        proc, result = run_native(tmp_path, _native_facts(), **kwargs)
        assert proc.returncode == 0
        assert set(result) == NATIVE_RESULT_KEYS
        assert (result["status"], result["reason_code"]) == ("clear", "no_native_auto_close_path")
        assert result["findings"] == []
        assert result["decision"] == "nonclosing_required" and result["level"] in {"A1", "A2"}
        expected = hashlib.sha256(
            "feat: 参照と close の分離\n\n## Summary\n\nRefs #330\n".encode()
        ).hexdigest()
        assert result["adopted_message_sha256"] == expected
        assert result["pr_number"] == PR


@pytest.mark.parametrize("lane", ["a2", "a1"])
def test_ac3_manual_relation_blocks_even_though_the_body_is_valid_refs(
    tmp_path: Path, lane: str
) -> None:
    issue_body, comment = (A2_BODY, None) if lane == "a2" else (A3_BODY, _comment())
    pr_body = "Refs #330\n" + (f"{A1_LINE}\n" if comment else "")
    _, policy = run_reference(tmp_path, pr_body, issue_body=issue_body, comment=comment)
    assert (policy["decision"], policy["body_verdict"]) == ("nonclosing_required", "valid")
    facts = _native_facts(closing_relations=[{"number": ISSUE, "repository": REPO.upper()}])
    proc, result = run_native(
        tmp_path, facts, pr_body=pr_body, issue_body=issue_body, comment=comment
    )
    assert proc.returncode == 0
    assert (result["status"], result["reason_code"]) == ("blocked", "native_relation_present")
    assert result["findings"][0] == {
        "kind": "native_relation",
        "method": "any",
        "source": "closingIssuesReferences",
    }
    # the exit-code consumer would have called the same PR safe
    assert naive_exit_code_consumer(proc) and not strict_native_consumer(result)


def test_ac3_unrelated_relation_or_keyword_does_not_block_the_target(tmp_path: Path) -> None:
    unrelated = _native_facts(
        closing_relations=[
            {"number": ISSUE + 1, "repository": REPO},  # other Issue, same repository
            {"number": ISSUE, "repository": "someone/else"},  # same number, other repository
        ],
        pr_body="Closes #331\nFixes someone/else#330\nRefs #330\n",
        commit_messages=["fix: x\n\nResolves #999"],
    )
    _, result = run_native(tmp_path, unrelated)
    assert (result["status"], result["findings"]) == ("clear", [])


KEYWORDS = ["close", "Closes", "closed", "fix", "FIXES", "fixed", "resolve", "resolves", "Resolved"]
TARGETS = ["#330", f"{REPO}#330", f"https://github.com/{REPO}/issues/330"]


@pytest.mark.parametrize("keyword", KEYWORDS)
@pytest.mark.parametrize("target", TARGETS)
def test_ac3_effective_squash_message_keyword_blocks(
    tmp_path: Path, keyword: str, target: str
) -> None:
    # PR_BODY setting: the PR body is the adopted squash body
    facts = _native_facts(pr_body=f"## Summary\n\nRefs #330\n\n{keyword}: {target}\n")
    _, result = run_native(tmp_path, facts)
    assert (result["status"], result["reason_code"]) == (
        "blocked",
        "effective_message_closing_keyword",
    )
    assert result["findings"] == [
        {"kind": "effective_message", "method": "squash", "source": "squash_body"}
    ]


def test_ac3_only_the_adopted_squash_text_is_inspected(tmp_path: Path) -> None:
    kw_commit = ["fix: a\n\nFixes #330"]

    def status(facts: dict[str, Any]) -> dict[str, Any]:
        return run_native(tmp_path, facts)[1]

    # PR_BODY / BLANK: the commit history is not adopted, so a keyword there is not a blocker
    for message_setting in ("PR_BODY", "BLANK"):
        facts = _native_facts(
            merge_settings={"squash_merge_commit_message": message_setting},
            commit_messages=kw_commit,
        )
        assert status(facts)["status"] == "clear", message_setting
    # COMMIT_MESSAGES: the commits are adopted
    adopted = _native_facts(
        merge_settings={"squash_merge_commit_message": "COMMIT_MESSAGES"}, commit_messages=kw_commit
    )
    assert status(adopted)["reason_code"] == "effective_message_closing_keyword"
    # BLANK ignores the PR body, so a PR body keyword is no longer adopted
    blank = _native_facts(
        merge_settings={"squash_merge_commit_message": "BLANK"}, pr_body="Closes #330\n"
    )
    assert status(blank)["status"] == "clear"
    # title settings: PR_TITLE vs COMMIT_OR_PR_TITLE (one commit headline wins; several -> PR title)
    titled = {"pr_title": "docs: x", "commit_messages": ["Closes #330"]}
    assert status(_native_facts(**titled))["status"] == "clear"
    single = status(
        _native_facts(merge_settings={"squash_merge_commit_title": "COMMIT_OR_PR_TITLE"}, **titled)
    )
    assert (single["reason_code"], single["findings"][0]["source"]) == (
        "effective_message_closing_keyword",
        "squash_title",
    )
    several = _native_facts(
        merge_settings={"squash_merge_commit_title": "COMMIT_OR_PR_TITLE"},
        pr_title="docs: x",
        commit_messages=["Closes #330", "second"],
    )
    assert status(several)["status"] == "clear"
    # a PR title with a keyword is adopted under PR_TITLE
    assert status(_native_facts(pr_title="Fixes #330"))["findings"][0]["source"] == "squash_title"


def test_ac3_final_squash_message_overrides_the_settings_derivation(tmp_path: Path) -> None:
    # the human changed the final message in the merge UI: that exact text is what is checked
    changed = _native_facts(final_squash_message={"title": "feat: x", "body": "Resolves #330"})
    _, result = run_native(tmp_path, changed)
    assert (result["status"], result["findings"][0]["source"]) == ("blocked", "squash_body")
    # a verified clean message is reported with a hash the merge step can compare
    clean = _native_facts(final_squash_message={"title": "feat: x", "body": "Refs #330"})
    _, result = run_native(tmp_path, clean)
    assert result["status"] == "clear"
    assert result["adopted_message_sha256"] == hashlib.sha256(b"feat: x\n\nRefs #330").hexdigest()
    # a clean derivation from settings does not vouch for a different final message
    _, derived = run_native(tmp_path, _native_facts())
    assert result["adopted_message_sha256"] != derived["adopted_message_sha256"]
    # an override whose text changes after a clear result changes the hash (merge-time recheck)
    reworded = _native_facts(final_squash_message={"title": "feat: x", "body": "Refs #330 again"})
    _, again = run_native(tmp_path, reworded)
    assert again["adopted_message_sha256"] != result["adopted_message_sha256"]


def test_ac3_merge_method_differences_use_the_live_settings(tmp_path: Path) -> None:
    commit_kw = ["fix: a\n\nFixes #330"]
    merge_enabled = {"allow_merge_commit": True}
    # merge commit enabled and no method chosen: every enabled method is checked
    _, result = run_native(
        tmp_path, _native_facts(merge_settings=merge_enabled, commit_messages=commit_kw)
    )
    assert (result["status"], result["findings"]) == (
        "blocked",
        [{"kind": "effective_message", "method": "merge", "source": "commit_message"}],
    )
    # an explicit squash method does not inspect commit history that squash does not adopt
    _, squash = run_native(
        tmp_path,
        _native_facts(
            merge_settings=merge_enabled, merge_method="squash", commit_messages=commit_kw
        ),
    )
    assert squash["status"] == "clear"
    # rebase keeps the commit messages
    rebase = _native_facts(
        merge_settings={"allow_squash_merge": False, "allow_rebase_merge": True},
        commit_messages=commit_kw,
    )
    _, rebased = run_native(tmp_path, rebase)
    assert rebased["findings"] == [
        {"kind": "effective_message", "method": "rebase", "source": "commit_message"}
    ]
    # a method the repository does not allow is fail-closed, as is a repository with no method
    _, not_allowed = run_native(tmp_path, _native_facts(merge_method="rebase"))
    assert (not_allowed["status"], not_allowed["reason_code"]) == (
        "fail_closed",
        "merge_method_not_allowed",
    )
    _, none_allowed = run_native(
        tmp_path, _native_facts(merge_settings={"allow_squash_merge": False})
    )
    assert (none_allowed["status"], none_allowed["reason_code"]) == (
        "fail_closed",
        "merge_method_not_allowed",
    )


MALFORMED_NATIVE_FACTS = [
    ("not_an_object", lambda f: None, "facts_invalid"),
    ("empty_list", lambda f: [], "facts_invalid"),
    ("unknown_key", lambda f: {**f, "extra": 1}, "facts_invalid"),
    (
        "missing_merge_settings",
        lambda f: {k: v for k, v in f.items() if k != "merge_settings"},
        "facts_invalid",
    ),
    ("repo_list", lambda f: {**f, "repo": []}, "facts_invalid"),
    ("repo_mismatch", lambda f: {**f, "repo": "other/repo"}, "facts_invalid"),
    ("relations_object", lambda f: {**f, "closing_relations": {}}, "facts_invalid"),
    (
        "relation_number_string",
        lambda f: {**f, "closing_relations": [{"number": "330", "repository": REPO}]},
        "facts_invalid",
    ),
    (
        "relation_number_bool",
        lambda f: {**f, "closing_relations": [{"number": True, "repository": REPO}]},
        "facts_invalid",
    ),
    (
        "relation_repo_list",
        lambda f: {**f, "closing_relations": [{"number": 330, "repository": []}]},
        "facts_invalid",
    ),
    (
        "relation_missing_repo",
        lambda f: {**f, "closing_relations": [{"number": 330}]},
        "facts_invalid",
    ),
    ("complete_not_bool", lambda f: {**f, "closing_relations_complete": "yes"}, "facts_invalid"),
    (
        "relations_incomplete",
        lambda f: {**f, "closing_relations_complete": False},
        "relations_incomplete",
    ),
    ("method_list", lambda f: {**f, "merge_method": []}, "facts_invalid"),
    ("method_unknown", lambda f: {**f, "merge_method": "ff"}, "facts_invalid"),
    ("title_null", lambda f: {**f, "pr_title": None}, "facts_invalid"),
    ("body_int", lambda f: {**f, "pr_body": 5}, "facts_invalid"),
    ("commits_string", lambda f: {**f, "commit_messages": "x"}, "facts_invalid"),
    ("commits_int_item", lambda f: {**f, "commit_messages": [1]}, "facts_invalid"),
    (
        "final_missing_body",
        lambda f: {**f, "final_squash_message": {"title": "x"}},
        "facts_invalid",
    ),
    ("final_string", lambda f: {**f, "final_squash_message": "x"}, "facts_invalid"),
    (
        "allow_not_bool",
        lambda f: {**f, "merge_settings": {**f["merge_settings"], "allow_squash_merge": "true"}},
        "facts_invalid",
    ),
    (
        "squash_title_list",
        lambda f: {**f, "merge_settings": {**f["merge_settings"], "squash_merge_commit_title": []}},
        "facts_invalid",
    ),
    (
        "settings_unknown_key",
        lambda f: {**f, "merge_settings": {**f["merge_settings"], "extra": 1}},
        "facts_invalid",
    ),
    (
        "squash_title_unknown_enum",
        lambda f: {
            **f,
            "merge_settings": {**f["merge_settings"], "squash_merge_commit_title": "SOMETHING_NEW"},
        },
        "squash_settings_invalid",
    ),
    (
        "squash_message_null",
        lambda f: {
            **f,
            "merge_settings": {**f["merge_settings"], "squash_merge_commit_message": None},
        },
        "squash_settings_invalid",
    ),
]


@pytest.mark.parametrize(
    "mutate,reason",
    [pytest.param(m, r, id=i) for i, m, r in MALFORMED_NATIVE_FACTS],
)
def test_ac3_malformed_or_incomplete_native_facts_fail_closed(
    tmp_path: Path, mutate: Any, reason: str
) -> None:
    proc, result = run_native(tmp_path, mutate(_native_facts()))
    assert proc.returncode == 0
    assert (result["status"], result["reason_code"]) == ("fail_closed", reason)
    assert set(result) == NATIVE_RESULT_KEYS and result["findings"] == []
    assert strict_native_consumer(result) is False


def test_ac3_lanes_without_an_open_promise_and_policy_pass_through(tmp_path: Path) -> None:
    # A3: the PR is supposed to close the Issue, so a native relation is not a risk
    facts = _native_facts(closing_relations=MANUAL_RELATION)
    _, a3 = run_native(tmp_path, facts, pr_body="Closes #330\n", issue_body=A3_BODY)
    assert (a3["status"], a3["reason_code"]) == ("not_applicable", "closing_required_lane")
    assert a3["decision"] == "closing_required" and a3["level"] == "A3"
    # CLOSED: there is no OPEN state left to preserve
    _, closed = run_native(tmp_path, facts, pr_body="Refs #330\n", state="CLOSED")
    assert (closed["status"], closed["reason_code"]) == ("not_applicable", "issue_closed")
    assert closed["level"] == "CLOSED"
    # a fail_closed reference policy (an A1 line without a usable comment) never becomes clear
    _, stopped = run_native(tmp_path, _native_facts(), pr_body=f"Refs #330\n{A1_LINE}\n")
    assert (stopped["status"], stopped["reason_code"]) == (
        "fail_closed",
        "reference_policy_fail_closed",
    )
    _, unresolved = run_native(tmp_path, _native_facts(), issue_body="no section")
    assert (unresolved["status"], unresolved["reason_code"]) == (
        "fail_closed",
        "reference_policy_fail_closed",
    )


def test_ac3_cli_prints_json_with_exit_zero_for_every_status(tmp_path: Path) -> None:
    seen: dict[str, subprocess.CompletedProcess[str]] = {}
    results: dict[str, dict[str, Any]] = {}
    for name, native, kwargs in (
        ("clear", _native_facts(), {}),
        ("blocked", _native_facts(closing_relations=MANUAL_RELATION), {}),
        ("fail_closed", _native_facts(closing_relations_complete=False), {}),
        ("not_applicable", _native_facts(), {"pr_body": "Closes #330\n", "issue_body": A3_BODY}),
    ):
        seen[name], results[name] = run_native(tmp_path, native, **kwargs)
    assert {n: r["status"] for n, r in results.items()} == {
        "clear": "clear",
        "blocked": "blocked",
        "fail_closed": "fail_closed",
        "not_applicable": "not_applicable",
    }
    assert {p.returncode for p in seen.values()} == {0}  # the exit code carries no decision
    # only `clear` proves the absence of a native auto-close path
    assert [n for n, r in results.items() if strict_native_consumer(r)] == ["clear"]
    assert all(naive_exit_code_consumer(p) for p in seen.values())  # the wrong consumer says yes x4
    # a non-JSON or missing native facts file is facts_invalid, still exit 0
    for kwargs in (
        {"native_facts": "{not json"},
        {"native_facts": _native_facts(), "omit_native": True},
    ):
        proc, result = run_native(
            tmp_path, kwargs["native_facts"], omit_native=kwargs.get("omit_native", False)
        )
        assert proc.returncode == 0
        assert (result["status"], result["reason_code"]) == ("fail_closed", "facts_invalid")


# =============================================================================================
# AC4: D3-D6 synchronisation, history, negative controls
# =============================================================================================

ABSENCE_PATTERNS = (
    re.compile(r"\.claude/skills/open-pr[\"']"),
    re.compile(r"\"skills\"\s*/\s*\"open-pr\"\s*[,)]"),
)


def stale_absence_problems(root: Path) -> list[str]:
    """No earlier slice may still assert that the whole `open-pr` directory does not exist."""
    problems: list[str] = []
    for path in sorted((root / "tests" / "stage_i4").glob("test_i4_4d[3-6]_*.py")):
        text = path.read_text(encoding="utf-8")
        if any(p.search(text) for p in ABSENCE_PATTERNS):
            problems.append(f"stale open-pr directory absence assertion in {path.name}")
    return problems


def history_sync_problems(m: dict[str, Any]) -> list[str]:
    """D5 / D6 keep their measured values and historical records, now with migrated_by_d7."""
    problems: list[str] = []
    d5, d6 = _json(D5_MANIFEST), _json(D6_MANIFEST)
    t5, t6 = d5["totals"], d6["totals"]
    if (t5["source_collected"], t5["deferred"], t5["ran"], t5["skipped"], t5["xfailed"]) != (
        108,
        0,
        108,
        0,
        0,
    ):
        problems.append("D5 totals are not 108 collected / 0 deferred / 108 ran")
    if (t6["source_collected"], t6["deferred"], t6["ran"], t6["skipped"], t6["xfailed"]) != (
        173,
        0,
        173,
        0,
        0,
    ):
        problems.append("D6 totals are not 173 collected / 0 deferred / 173 ran")
    for name, doc in (("D5", d5), ("D6", d6)):
        migrated = doc.get("migrated_by_d7", {})
        if migrated.get("files") != [SCRIPT_REL] or migrated.get("manifest") != D7_MANIFEST_REL:
            problems.append(f"{name} migrated_by_d7 does not name the script and the D7 manifest")
        hist = doc.get("historical_pre_d7", {}).get("hard_dependencies", [])
        if not hist or "validate_pr_body.py" not in json.dumps(hist):
            problems.append(f"{name} historical_pre_d7 (the pre-D7 unmigrated record) was lost")
        live = "\n".join(e["reference"] for e in doc["unmigrated_references"]["hard_dependencies"])
        if "open-pr" not in live or "validate_pr_body.py was migrated" not in live:
            problems.append(f"{name} live record does not state what D7 migrated")
        for e in doc["unmigrated_references"]["hard_dependencies"]:
            if e["reference"].startswith(OPEN_PR) and not (e["owner"] and e["rerun_condition"]):
                problems.append(f"{name} open-pr record lost its owner / rerun_condition")
    sync = m["earlier_slice_sync"]
    if sync["d5_totals_kept"] != {"collected": 108, "ran": 108, "deferred": 0}:
        problems.append("D7 manifest lost the D5 108 / 108 / 0 record")
    if sync["d6_totals_kept"] != {"collected": 173, "ran": 173, "deferred": 0}:
        problems.append("D7 manifest lost the D6 173 / 173 / 0 record")
    d4m = _json(D4_MANIFEST)
    hist4 = d4m.get("historical_pre_d5", {}).get("totals", {})
    if (hist4.get("source_collected"), hist4.get("deferred"), hist4.get("ran")) != (108, 1, 107):
        problems.append("D4 historical 108 / 1 / 107 was lost")
    d3 = _json(D3_MANIFEST)
    if not d3["unmigrated_references"]["later_i_4_4d_slices"]:
        problems.append("D3 historical unmigrated record was lost")
    c2 = _json(DEV / "i4-4c2-intake-capsule-manifest.json")
    if (c2["totals"]["ran"], c2["totals"]["deferred"]) != (118, 1):
        problems.append("C2 residual deferred (118 ran / 1 deferred) changed")
    c2_text = json.dumps(c2["deferred_tests"]).lower()
    if "live_github" not in c2_text and "real_github" not in c2_text:
        problems.append("C2 live canary deferral lost its identity")
    return problems


def test_ac4_earlier_slices_no_longer_assert_the_whole_directory_is_absent() -> None:
    assert stale_absence_problems(ROOT) == []
    for slice_id in ("d3", "d4", "d5", "d6"):
        (path,) = (ROOT / "tests/stage_i4").glob(f"test_i4_4{slice_id}_*.py")
        text = path.read_text(encoding="utf-8")
        assert "open-pr/SKILL.md" in text or '"open-pr" / "SKILL.md"' in text, slice_id


def test_ac4_stale_absence_assertion_is_detected(tmp_path: Path) -> None:
    stage = tmp_path / "tests" / "stage_i4"
    stage.mkdir(parents=True)
    for src in (ROOT / "tests/stage_i4").glob("test_i4_4d[3-6]_*.py"):
        shutil.copy(src, stage / src.name)
    assert stale_absence_problems(tmp_path) == []
    d5 = stage / "test_i4_4d5_pr_review_consumers.py"
    d5.write_text(
        d5.read_text("utf-8").replace(
            '".claude/skills/open-pr/SKILL.md",', '".claude/skills/open-pr",', 1
        )
    )
    assert any("d5_pr" in p or "4d5" in p for p in stale_absence_problems(tmp_path))
    d3 = stage / "test_i4_4d3_step4_step5.py"
    d3.write_text(d3.read_text("utf-8") + '\nx = ROOT / ".claude" / "skills" / "open-pr",\n')
    assert len(stale_absence_problems(tmp_path)) == 2


def test_ac4_d5_d6_history_and_residual_deferrals_are_kept(manifest: dict[str, Any]) -> None:
    assert history_sync_problems(manifest) == []


def test_ac4_history_loss_and_count_tampering_are_detected(
    manifest: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_json = _json

    def tampered(mutate: Any, target: Path) -> Any:
        def fake(path: Path) -> dict[str, Any]:
            doc = real_json(path)
            if path == target:
                mutate(doc)
            return doc

        return fake

    cases = (
        (D5_MANIFEST, lambda d: d.pop("historical_pre_d7"), "D5 historical_pre_d7"),
        (D6_MANIFEST, lambda d: d.pop("migrated_by_d7"), "D6 migrated_by_d7"),
        (D5_MANIFEST, lambda d: d["totals"].update(ran=107), "D5 totals"),
        (D6_MANIFEST, lambda d: d["totals"].update(source_collected=172), "D6 totals"),
        (
            D6_MANIFEST,
            lambda d: [e.update(owner="") for e in d["unmigrated_references"]["hard_dependencies"]],
            "D6 open-pr record lost its owner",
        ),
        (D4_MANIFEST, lambda d: d["historical_pre_d5"]["totals"].update(ran=108), "D4 historical"),
    )
    for target, mutate, expected in cases:
        monkeypatch.setitem(globals(), "_json", tampered(mutate, target))
        assert any(expected in p for p in history_sync_problems(manifest)), expected
    monkeypatch.setitem(globals(), "_json", real_json)
    lost = copy.deepcopy(manifest)
    lost["earlier_slice_sync"]["d6_totals_kept"] = {"collected": 1, "ran": 1, "deferred": 0}
    assert any("173" in p for p in history_sync_problems(lost))


# =============================================================================================
# AC5: closure counts, lane, no skip, boundary
# =============================================================================================


def source_test_problems(m: dict[str, Any], root: Path) -> list[str]:
    problems: list[str] = []
    for rel in m["test_files"]:
        text = (root / rel).read_text(encoding="utf-8") if (root / rel).is_file() else ""
        if SKIP_RE.search(text):
            problems.append(f"skip / xfail marker in a closure test file: {rel}")
    if m["deferred_tests"]:
        problems.append("a deferred test is recorded")
    if set(m["test_files"]) != {THIS} or m["expected_collect"] != m["expected_ran"]:
        problems.append("test_files / expected_collect / expected_ran are inconsistent")
    t = m["totals"]
    if (t["deferred"], t["skipped"], t["xfailed"], t["xpassed"], t["deselected"]) != (
        0,
        0,
        0,
        0,
        0,
    ):
        problems.append("totals carry a deferred / skipped / xfailed / xpassed / deselected test")
    if t["collected"] != t["ran"] or t["ran"] != m["expected_ran"][THIS]:
        problems.append("collected / ran differ from the manifest expectation")
    return problems


def lane_problems(ci_text: str, m: dict[str, Any]) -> list[str]:
    import yaml

    job = ((yaml.safe_load(ci_text) or {}).get("jobs") or {}).get(D7_JOB)
    if not isinstance(job, dict):
        return [f"CI lane missing: {D7_JOB}"]
    runs = "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
    problems: list[str] = []
    if f"run_stage1b1_tests.py {D7_MANIFEST_REL}" not in runs:
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


def boundary_problems(m: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    b = m["evidence_boundary"]
    for key in (
        "live_e2e",
        "agent_dispatched",
        "skill_production_invocable",
        "skill_preload_effective",
        "verdict_published",
        "final_gate_wired",
        "loop_handoff_result_v1_wired",
        "github_live_operations",
        "github_mutation_by_evaluator",
        "git_ref_mutation",
        "open_pr_skill_enabled",
        "pr_publisher_enabled",
        "closing_relation_mutated",
        "full_source_suite_run",
        "disable_model_invocation_released",
    ):
        if b.get(key) is not False:
            problems.append(f"evidence boundary overclaims: {key}")
    if "exit" not in m["exit_code_contract"]["rule"].lower():
        problems.append("the exit-code contract is not recorded")
    return problems


def test_ac5_the_closure_test_count_is_measured_not_assumed(manifest: dict[str, Any]) -> None:
    r = _collect(THIS)
    ids = [ln for ln in r.stdout.splitlines() if "::" in ln]
    assert r.returncode == 0, r.stdout + r.stderr
    assert manifest["expected_collect"] == {THIS: len(ids)}
    assert manifest["totals"]["collected"] == len(ids)
    assert manifest["lane_totals"]["closure_tests_ran"] == len(ids)
    # the matrix and malformed-facts parametrisation is part of the count, not a loop in one test
    assert sum("test_ac2_reference_policy_matrix_through_the_real_cli" in i for i in ids) == len(
        REFERENCE_POLICY_MATRIX
    )
    assert sum(
        "test_ac3_malformed_or_incomplete_native_facts_fail_closed" in i for i in ids
    ) == len(MALFORMED_NATIVE_FACTS)
    assert sum("test_ac3_effective_squash_message_keyword_blocks" in i for i in ids) == len(
        KEYWORDS
    ) * len(TARGETS)


def test_ac5_no_skip_xfail_or_deferred_test_is_recorded(manifest: dict[str, Any]) -> None:
    assert source_test_problems(manifest, ROOT) == []
    bad = copy.deepcopy(manifest)
    bad["deferred_tests"] = [{"id": "x"}]
    assert any("deferred test" in p for p in source_test_problems(bad, ROOT))
    bad = copy.deepcopy(manifest)
    bad["totals"]["skipped"] = 1
    assert any("skipped" in p for p in source_test_problems(bad, ROOT))
    bad = copy.deepcopy(manifest)
    bad["totals"]["ran"] += 1
    assert any("differ" in p for p in source_test_problems(bad, ROOT))


def test_ac5_a_skip_marker_in_the_closure_file_is_detected(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    fake = tmp_path / THIS
    fake.parent.mkdir(parents=True)
    fake.write_text("@pytest.mark." + "skip\ndef test_x():\n    pass\n", encoding="utf-8")
    assert any("skip / xfail marker" in p for p in source_test_problems(manifest, tmp_path))


def test_ac5_d7_ci_lane_pins_the_manifest_counts(manifest: dict[str, Any]) -> None:
    text = CI.read_text(encoding="utf-8")
    assert lane_problems(text, manifest) == []
    ran = manifest["totals"]["ran"]
    assert any(
        "ran count" in p
        for p in lane_problems(
            text.replace(f"0 deferred with reasons, {ran} ran", "0 deferred with reasons, 1 ran"),
            manifest,
        )
    )
    assert any(
        "lane missing" in p for p in lane_problems(text.replace(D7_JOB, "other-job"), manifest)
    )
    assert any(
        "skipped" in p
        for p in lane_problems(
            text.replace("(skipped|xfailed|xpassed|deselected)", "(x)"), manifest
        )
    )


def test_ac5_evidence_boundary_is_not_overclaimed(manifest: dict[str, Any]) -> None:
    assert boundary_problems(manifest) == []
    overclaim = copy.deepcopy(manifest)
    overclaim["evidence_boundary"]["pr_publisher_enabled"] = True
    assert any("pr_publisher_enabled" in p for p in boundary_problems(overclaim))
    assert manifest["exit_code_contract"]["structured_fields"] == [
        "decision",
        "body_verdict",
        "status",
        "reason_code",
    ]


def test_ac5_rollback_and_handoff_are_recorded(manifest: dict[str, Any]) -> None:
    assert "Revert the single D7 PR" in manifest["rollback"]
    assert "D8" in manifest["handoff"]["next"]
    assert manifest["base"]["commit"] == "6e6effdb457270f1c66c51817e2be93aae17c73e"
