"""I-4-2: contract snapshot publisher closure (#66).

Offline checks are fixture evidence. The `live_evidence` / `idempotent_evidence` tests only check
that the manifest's `live_acceptance` record (written from a real GitHub POST/PATCH/GET run on the
disposable verification issue) is complete and self-consistent; they never touch the network.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "dev" / "i4-2-snapshot-publisher-manifest.json"
ICR = ".claude/skills/issue-contract-review"
SCRIPTS = [
    ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py",
    ".claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py",
    f"{ICR}/scripts/run_contract_review_once.py",
]
TRUSTED_PUBLISHER_ID = 63350259


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.mark.parametrize("rel", SCRIPTS)
def test_isolated_import(rel: str) -> None:
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('m',{rel!r});"
        "m=importlib.util.module_from_spec(s);sys.modules['m']=m;s.loader.exec_module(m)"
    )
    r = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr


def test_manifest_required_files_and_parity() -> None:
    m = _manifest()
    assert set(SCRIPTS) | {f"{ICR}/scripts/check_blockers.sh"} <= set(m["required_files"])
    for rel, info in m["source_parity"].items():
        digest = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        assert digest == info["moved_sha256"], rel
        if info["verbatim"]:
            assert info["source_sha256"] == info["moved_sha256"], rel
        else:
            assert info.get("intentional_diff"), rel


def test_no_functional_legacy_slug_or_marker() -> None:
    for root in (".claude/skills/impl-review-loop", ".claude/skills/pr-review-judge", ICR):
        r = subprocess.run(
            [sys.executable, "-I", "scripts/ci/check_no_functional_slug.py", root],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stdout + r.stderr
    for rel in [*SCRIPTS, f"{ICR}/scripts/check_blockers.sh"]:
        assert "loop-protocol:" not in (ROOT / rel).read_text(encoding="utf-8"), rel


def test_publish_is_an_approved_live_command() -> None:
    sys.path.insert(0, str(ROOT / "scripts" / "agent-guards"))
    try:
        import controlled_skill_mutation_exec as ex
    finally:
        sys.path.pop(0)
    assert "contract_snapshot.publish" in ex.APPROVED_LIVE_COMMAND_IDS


def test_live_evidence() -> None:
    live = _manifest()["live_acceptance"]
    for key in (
        "repo",
        "verification_issue",
        "comment_id",
        "comment_url",
        "author",
        "post_status_first_run",
        "post_patch_observed",
        "comment_body_sha256",
        "issue_body_sha256",
        "fingerprint",
        "canonical_parser",
    ):
        assert live.get(key) not in (None, ""), key
    assert live["repo"] == "squne121/agent-platform"
    assert live["author"]["id"] == TRUSTED_PUBLISHER_ID
    assert live["post_status_first_run"] == "posted"
    assert live["post_patch_observed"] is True
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", live["comment_body_sha256"])
    assert live["comment_url"].endswith(f"#issuecomment-{live['comment_id']}")
    fp = live["fingerprint"]
    assert fp["contract_source_id"] == str(live["comment_id"])
    assert fp["issue_number"] == live["verification_issue"]
    assert fp["contract_body_sha256"] == live["issue_body_sha256"]
    assert live["canonical_parser"] == {"status": "go", "trusted": True, "authoritative_go": True}


def test_idempotent_evidence() -> None:
    rerun = _manifest()["live_acceptance"]["rerun"]
    assert rerun["post_status"] == "not_requested"
    assert rerun["snapshot_count_before"] == rerun["snapshot_count_after"] == 1
    assert rerun["comment_updated_at_before"] == rerun["comment_updated_at_after"]
    assert rerun["comment_body_sha256_before"] == rerun["comment_body_sha256_after"]
