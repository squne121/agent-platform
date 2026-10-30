"""AC5: parent checklist patch only happens when body_sha256 matches, the old_line occurs
exactly once *inside* the ## Child Issues section, and post-edit read-back confirms it.
Any guard violation aborts with NO gh issue edit performed.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import materialize_child_issues as m

_GUARDS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agent-guards"
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))
import repo_identity

TRUSTED_REPO = repo_identity.load_trusted_repo_slug()

# old_line appears once in Child Issues AND once in Notes — section scoping must still
# resolve a single in-section match.
PARENT_BODY = (
    "# Parent\n"
    "\n"
    "## Child Issues\n"
    "\n"
    "- [ ] C254-2: validator parity（未起票）\n"
    "- [ ] C254-3: overlap gate（未起票）\n"
    "\n"
    "## Notes\n"
    "\n"
    "- [ ] C254-2: validator parity（未起票）\n"
)

OLD_LINE = "- [ ] C254-2: validator parity（未起票）"
NEW_LINE = "- [x] C254-2: validator parity #287"


def _sha(body_with_nl: str) -> str:
    # apply_parent_checklist_patch strips one trailing newline from the gh --jq body.
    # The canonical body_sha256 format is "sha256:<64 hex>".
    body = body_with_nl.removesuffix("\n")
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


class FakeGh:
    """Records gh invocations and emulates view/edit with persistent body state."""

    def __init__(self, body: str):
        self.body = body  # stored WITHOUT trailing newline
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "view"]:
            return m.RunResult(0, self.body + "\n")
        if args[:2] == ["issue", "edit"]:
            idx = args.index("--body-file")
            from pathlib import Path

            self.body = Path(args[idx + 1]).read_text(encoding="utf-8")
            return m.RunResult(0, "")
        return m.RunResult(1, "", "unexpected gh call")

    def edits(self):
        return [c for c in self.calls if c[:2] == ["issue", "edit"]]


def _updates():
    return [
        {
            "section": "Child Issues",
            "old_line": OLD_LINE,
            "new_line": NEW_LINE,
            "expected_match_count": 1,
        }
    ]


def test_successful_patch_section_scoped():
    gh = FakeGh(PARENT_BODY.rstrip("\n"))
    res = m.apply_parent_checklist_patch(
        repo=TRUSTED_REPO,
        parent_issue=254,
        updates=_updates(),
        expected_body_sha256=_sha(PARENT_BODY),
        runners=m.Runners(gh=gh),
    )
    assert res.updated is True, res.error
    assert len(gh.edits()) == 1
    # Only the Child Issues occurrence is replaced; the Notes occurrence remains.
    assert NEW_LINE in gh.body
    assert gh.body.count(OLD_LINE) == 1  # the Notes line is untouched


def test_sha_mismatch_aborts_without_edit():
    gh = FakeGh(PARENT_BODY.rstrip("\n"))
    res = m.apply_parent_checklist_patch(
        repo=TRUSTED_REPO,
        parent_issue=254,
        updates=_updates(),
        expected_body_sha256="sha256:" + "0" * 64,
        runners=m.Runners(gh=gh),
    )
    assert res.updated is False
    assert "sha256 mismatch" in res.error
    assert gh.edits() == []  # no mutation on guard failure


def test_duplicate_in_section_aborts():
    body = (
        "## Child Issues\n\n"
        f"{OLD_LINE}\n{OLD_LINE}\n"  # same line twice inside the section
    )
    gh = FakeGh(body)
    res = m.apply_parent_checklist_patch(
        repo=TRUSTED_REPO,
        parent_issue=254,
        updates=_updates(),
        expected_body_sha256=_sha(body + "\n"),
        runners=m.Runners(gh=gh),
    )
    assert res.updated is False
    assert "expected_match_count" in res.error
    assert gh.edits() == []


def test_old_line_only_outside_section_aborts():
    body = (
        "## Child Issues\n\n"
        "- [ ] C254-9: something else\n\n"
        "## Notes\n\n"
        f"{OLD_LINE}\n"  # only outside Child Issues
    )
    gh = FakeGh(body)
    res = m.apply_parent_checklist_patch(
        repo=TRUSTED_REPO,
        parent_issue=254,
        updates=_updates(),
        expected_body_sha256=_sha(body + "\n"),
        runners=m.Runners(gh=gh),
    )
    assert res.updated is False
    assert "expected_match_count" in res.error
    assert gh.edits() == []


def test_readback_failure_reported():
    # gh that "edits" but read-back never reflects the new line.
    class BadReadback(FakeGh):
        def __call__(self, args):
            self.calls.append(args)
            if args[:2] == ["issue", "view"]:
                return m.RunResult(0, PARENT_BODY)  # always original → new_line never appears
            if args[:2] == ["issue", "edit"]:
                return m.RunResult(0, "")
            return m.RunResult(1, "")

    gh = BadReadback(PARENT_BODY.rstrip("\n"))
    res = m.apply_parent_checklist_patch(
        repo=TRUSTED_REPO,
        parent_issue=254,
        updates=_updates(),
        expected_body_sha256=_sha(PARENT_BODY),
        runners=m.Runners(gh=gh),
    )
    assert res.updated is False
    assert "read-back" in res.error


def test_missing_sha_aborts_without_edit():
    # Blocker 2: body_sha256 is a hard precondition for patching.
    gh = FakeGh(PARENT_BODY.rstrip("\n"))
    res = m.apply_parent_checklist_patch(
        repo=TRUSTED_REPO,
        parent_issue=254,
        updates=_updates(),
        expected_body_sha256=None,
        runners=m.Runners(gh=gh),
    )
    assert res.updated is False
    assert "body_sha256 is required" in res.error
    assert gh.calls == []


def test_no_updates_is_noop():
    gh = FakeGh(PARENT_BODY.rstrip("\n"))
    res = m.apply_parent_checklist_patch(
        repo=TRUSTED_REPO,
        parent_issue=254,
        updates=[],
        expected_body_sha256=None,
        runners=m.Runners(gh=gh),
    )
    assert res.updated is False and res.error is None
    assert gh.calls == []  # nothing fetched or edited
