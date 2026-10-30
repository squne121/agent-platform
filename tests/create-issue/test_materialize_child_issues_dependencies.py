"""AC4: depends_on is mapped to create_issue_txn --dependency (with read-back), and
free-form / non-integer dependencies are fail-closed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import materialize_child_issues as m
import pytest

_GUARDS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agent-guards"
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))
import repo_identity

TRUSTED_REPO = repo_identity.load_trusted_repo_slug()


def _ok_create(**kw):
    return m.RunResult(
        0, '{"status": "success", "issue_number": 330, "issue_url": "https://x/330"}'
    )


def _ok_validate(body, kind, title):
    return m.RunResult(0, '{"status": "pass"}')


def test_depends_on_passed_to_create_runner(valid_plan):
    captured = {}

    def create(**kw):
        captured.update(kw)
        return _ok_create(**kw)

    runners = m.Runners(validate=_ok_validate, create=create, gh=lambda a: m.RunResult(0, ""))
    valid_plan["children"][0]["depends_on"] = [948, 700]
    res = m.materialize(valid_plan, runners)
    assert res["status"] == "ok"
    assert captured["dependencies"] == [948, 700]


def test_default_create_runner_emits_dependency_flags(monkeypatch):
    captured = {}

    def fake_run(argv, *a, **kw):
        captured["argv"] = argv

        class R:
            returncode = 0
            stdout = '{"status":"success","issue_number":1,"issue_url":"u"}'
            stderr = ""

        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    m._default_create_runner(
        repo=TRUSTED_REPO,
        title="実装: x",
        body="b",
        kind="implementation",
        label_profile="standard",
        dependencies=[948, 700],
        parent_issue=254,
        gh_bin="gh",
    )
    argv = captured["argv"]
    # Each dependency maps to a --dependency flag routed through create_issue_txn.py.
    dep_positions = [i for i, v in enumerate(argv) if v == "--dependency"]
    assert len(dep_positions) == 2
    dep_values = {argv[i + 1] for i in dep_positions}
    assert dep_values == {"948", "700"}
    assert str(argv[1]).endswith("create_issue_txn.py")


@pytest.mark.parametrize("bad", ["#948", "948", 9.5, True])
def test_free_form_dependency_fails_closed(valid_plan, bad):
    valid_plan["children"][0]["depends_on"] = [bad]
    with pytest.raises(m.PlanValidationError, match="depends_on"):
        m.validate_plan(valid_plan)


def test_create_issue_txn_has_readback_for_dependencies():
    # AC4 read-back guarantee is provided by create_issue_txn._readback_dependencies,
    # which the materializer relies on (creation flows only through the txn).
    import create_issue_txn as txn

    assert hasattr(txn, "_readback_dependencies")
