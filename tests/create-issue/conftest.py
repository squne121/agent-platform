"""Shared fixtures/helpers for create-issue child-materialization tests (#946)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "create-issue"

sys.path.insert(0, str(SCRIPTS_DIR))

# The trusted repository slug comes from repo_identity.py only (no slug constant in tests).
_GUARDS_DIR = REPO_ROOT / "scripts" / "agent-guards"
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))
import repo_identity

TRUSTED_REPO = repo_identity.load_trusted_repo_slug()


def _valid_child() -> dict:
    return {
        "child_id": "C254-3",
        "title": "実装: overlap gate を追加する",
        "kind": "implementation",
        "action": "create_issue",
        "depends_on": [948],
        "allowed_paths": ["src/foo.ts", "tests/foo.test.ts"],
        "acceptance_criteria": ["AC1", "AC2"],
        "verification_commands": {
            "AC1": "uv run pytest tests/foo_test.py -q",
            "AC2": "pnpm test",
        },
        "label_profile": "standard",
        "sections": {
            "Runtime Verification Applicability": "- decision: not_applicable\n- reason: 静的検証のみで完結するため",
            "Outcome": "overlap gate が追加され Vitest が PASS する状態",
            "In Scope": "- overlap gate 実装\n- 対応する unit test",
            "Current Validated Scope": "- src/foo.ts に gate を追加",
            "Parent Goal Ref": "- Goal: delivery-rollup\n- Desired Destination: child 統合",
            "ac_text": {"AC1": "gate が overlap を検出する", "AC2": "既存テストが破壊されない"},
        },
    }


def _clear_overlap() -> dict:
    # status=clear must carry preflight provenance (High 2 hardening).
    return {
        "status": "clear",
        "source": "check_issue_overlap.py",
        "helper_version": "1.0.0",
        "input_sha256": "sha256:" + "a" * 64,
        "checked_at": "2026-06-17T00:00:00Z",
        "verdict": "safe_new_issue",
    }


def _valid_plan() -> dict:
    return {
        "schema_version": 2,
        "repo": TRUSTED_REPO,
        "parent": {
            "issue_number": 254,
            "parent_mode": "delivery-rollup",
            "closure_mode": "child-complete",
        },
        "issue_lookup": {"complete": True},
        "children": [_valid_child()],
        "overlap": _clear_overlap(),
        "parent_body_updates": [],
    }


@pytest.fixture
def clear_overlap():
    return copy.deepcopy(_clear_overlap())


@pytest.fixture
def valid_child():
    return copy.deepcopy(_valid_child())


@pytest.fixture
def valid_plan():
    return copy.deepcopy(_valid_plan())


@pytest.fixture
def parent_body_fixture() -> str:
    return (FIXTURES_DIR / "parent_body_with_child_issues.md").read_text(encoding="utf-8")
