"""
Issue #1488 AC5: boundary test proving the REAL producer
(`baseline_vc_preflight.py`) current-head envelope for a non-regression-gate
VC (rg) feeds into `adjudicate_vc_result.py` and resolves a baseline
`expected_fail` into `expected_fail_resolved_on_current_head`.

PR #1497 review (REQUEST_CHANGES) required a stronger E2E fixture that does
NOT rely on a hand-written baseline snapshot / diff summary: (1) create a
real baseline commit, (2) run the REAL baseline producer and save its actual
snapshot, (3) create a second commit that changes an Allowed Path, (4) run
the REAL current-head producer, (5) generate the diff summary from a REAL
`git diff` between the two commits, (6) feed everything into the
adjudicator, and (7) exercise negative cases: an out-of-scope (repo-external)
path, a quiet partial error (delete + match), and HEAD drift.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
ADJUDICATE_SCRIPT_PATH = (
    ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "adjudicate_vc_result.py"
)
PRODUCER_SCRIPT_PATH = (
    ROOT
    / ".claude"
    / "skills"
    / "issue-contract-review"
    / "scripts"
    / "baseline_vc_preflight.py"
)
ADJUDICATOR_PATH = ".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"
ADJUDICATOR_DIFF_SUMMARY = {
    "changed_paths": [ADJUDICATOR_PATH],
    "head_sha": "head-1",
    "pr_number": 1544,
}

_spec = importlib.util.spec_from_file_location(
    "adjudicate_vc_result_non_regression_gate", ADJUDICATE_SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)


def _commit_all(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()


def _run_producer(
    *,
    repo: Path,
    body_file: Path,
    evidence_mode: str | None = None,
    reviewed_head_sha: str | None = None,
) -> dict:
    argv = [
        sys.executable,
        str(PRODUCER_SCRIPT_PATH),
        "--body-file",
        str(body_file),
        "--cwd",
        str(repo),
        "--format",
        "json",
        "--issue",
        "1488",
        "--repo",
        "squne121/agent-platform",
    ]
    if evidence_mode is not None:
        argv += ["--evidence-mode", evidence_mode]
    if reviewed_head_sha is not None:
        argv += ["--reviewed-head-sha", reviewed_head_sha]
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert completed.stdout, f"producer emitted no stdout: {completed.stderr}"
    return json.loads(completed.stdout)


def _manual_vc_result(
    ac: str,
    *,
    command_hash: str,
    classification: str,
    decision: str,
    scope_class: str | None,
    exit_code: int | None,
) -> dict:
    item = {
        "ac": ac,
        "command_hash": command_hash,
        "classification": classification,
        "decision": decision,
        "category": "regression_gate",
        "scope_class": scope_class,
        "runner": "exec",
        "exit_code": exit_code,
        "failure_keys": [],
        "raw_command": "pytest tests/test_alpha.py",
    }
    if scope_class == "pr_review_only":
        item.update(
            {
                "category": "preflight_scope_pr_review_only",
                "runner": "skipped",
                "verification_owner": "pr-review-judge",
                "deferred_reason": "VC marked pr_review_only; verification deferred to PR review",
                "runtime_verification_required": False,
            }
        )
    return item


def _manual_contract_snapshot(items: list[dict]) -> dict:
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": "sha256:" + "b" * 64,
        "checks": {
            "vc_preflight": {
                "classifications": items,
            }
        },
    }


def _manual_current_payload(items: list[dict], *, head_sha: str, reviewed_head_sha: str) -> dict:
    return {
        "schema": "baseline_vc_preflight/v1",
        "issue": 1488,
        "generated_at": "2026-07-11T10:00:00Z",
        "status": "pass",
        "errors": [],
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "source": {"body_sha256": "sha256:" + "b" * 64},
        "head_sha": head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "results": items,
    }


def _manual_test_verdict(
    items: list[dict],
    *,
    head_sha: str,
    reviewed_head_sha: str,
    diff_head_sha: str,
    issue_number: int = 1488,
    pr_number: int = 1544,
    contract_body_sha256: str = "sha256:" + "b" * 64,
) -> dict:
    command_hashes = sorted(item["command_hash"] for item in items)
    artifact_payload = {
        "issue_number": issue_number,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "diff_head_sha": diff_head_sha,
        "contract_body_sha256": contract_body_sha256,
        "command_hashes": command_hashes,
    }
    return {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "producer_kind": "test-runner",
        "repository": "squne121/agent-platform",
        "issue_number": issue_number,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "diff_head_sha": diff_head_sha,
        "contract_body_sha256": contract_body_sha256,
        "run_id": "run-1544-1",
        "run_url": "https://example.invalid/runs/1544",
        "workflow_run_id": 1544,
        "workflow_run_attempt": 1,
        "check_run_id": 15440,
        "artifact": {
            "name": "test-verdict-machine",
            "artifact_digest": "sha256:" + "a" * 64,
            "url": "https://github.com/squne121/agent-platform/actions/runs/1544/artifacts/1",
        },
        "artifact_payload": artifact_payload,
        "artifact_payload_sha256": mod._sha256(mod._canonical_json(artifact_payload)),
        "result": "PASS",
        "verification_commands_pass": len(items),
        "verification_commands_fail": 0,
        "verification_skipped_count": 0,
        "runtime_ac_results": [
            {
                "ac": item["ac"],
                "command_hash": item["command_hash"],
                "exit_code": 0,
                "status": "pass",
                "fallback_detected": False,
                "human_review_required": False,
                "stop_condition_triggered": False,
            }
            for item in items
        ],
    }


def _git_diff_changed_paths(repo: Path, base_sha: str, head_sha: str) -> list[str]:
    """Real `git diff --name-only` between two commits (not hand-typed)."""
    output = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--name-only", base_sha, head_sha],
        text=True,
    )
    return [line for line in output.splitlines() if line.strip()]


def test_real_producer_non_regression_gate_current_pass_resolves_baseline_expected_fail(
    tmp_path: Path,
) -> None:
    """GIVEN a real baseline_vc_preflight.py current-head run over a non
    regression-gate rg VC that now matches (exit 0) WHEN its output is fed to
    adjudicate_vc_result() with a baseline expected_fail snapshot THEN the
    overall status is pass and the AC resolves via
    expected_fail_resolved_on_current_head (producer-certified, not a
    hand-written fixture)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()

    body = (
        "## Allowed Paths\n"
        "- tracked.txt\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "$ rg -q hello tracked.txt\n"
        "```\n"
    )
    # body_file lives OUTSIDE the repo so writing it does not dirty the
    # worktree under test.
    body_file = tmp_path / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    current_vc_result = _run_producer(
        repo=repo,
        body_file=body_file,
        evidence_mode="current-head",
        reviewed_head_sha=head,
    )

    # Sanity: the real producer must have certified this as a current PASS
    # envelope (Issue #1488's actual fix under test), not merely exited 0.
    assert current_vc_result["status"] == "pass"
    assert current_vc_result["errors"] == []
    assert current_vc_result["fallback_detected"] is False
    assert current_vc_result["human_review_required"] is False
    assert current_vc_result["stop_condition_triggered"] is False
    assert len(current_vc_result["results"]) == 1
    current_item = current_vc_result["results"][0]
    assert current_item["classification"] == "expected_pass"
    assert current_item["category"] == "expected_pass_resolved_on_current_head"
    assert current_item["exit_code"] == 0
    assert current_item["confidence"] == "high"
    assert current_item["certified_target_paths"] == ["tracked.txt"]

    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": current_vc_result["source"]["body_sha256"],
        "checks": {
            "vc_preflight": {
                "classifications": [
                    {
                        "ac": current_item["ac"],
                        "command_hash": current_item["command_hash"],
                        "exit_code": 1,
                        "category": "expected_baseline_fail",
                        "classification": "expected_fail",
                        "decision": "go",
                        "raw_command": current_item["raw_command"],
                    }
                ]
            }
        },
    }
    diff_summary = {
        "changed_paths": ["tracked.txt"],
        "head_sha": head,
    }
    allowed_paths = ["tracked.txt"]

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    assert len(result["per_ac"]) == 1
    assert result["per_ac"][0]["reason_code"] == "expected_fail_resolved_on_current_head"


def test_real_producer_non_regression_gate_test_f_current_pass_resolves_baseline_expected_fail(
    tmp_path: Path,
) -> None:
    """GIVEN a real baseline_vc_preflight.py current-head run over a
    non-regression-gate `test -f` VC that now succeeds (the file was created
    by the implementation) WHEN fed into adjudicate_vc_result() THEN the
    overall status is pass with expected_fail_resolved_on_current_head."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "implemented.txt").write_text("done\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "implemented.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()

    body = (
        "## Allowed Paths\n"
        "- implemented.txt\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "$ test -f implemented.txt\n"
        "```\n"
    )
    body_file = tmp_path / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    current_vc_result = _run_producer(
        repo=repo,
        body_file=body_file,
        evidence_mode="current-head",
        reviewed_head_sha=head,
    )

    assert current_vc_result["status"] == "pass"
    current_item = current_vc_result["results"][0]
    assert current_item["classification"] == "expected_pass"
    assert current_item["category"] == "expected_pass_resolved_on_current_head"

    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": current_vc_result["source"]["body_sha256"],
        "checks": {
            "vc_preflight": {
                "classifications": [
                    {
                        "ac": current_item["ac"],
                        "command_hash": current_item["command_hash"],
                        "exit_code": 1,
                        "category": "file_not_found_expected",
                        "classification": "expected_fail",
                        "decision": "go",
                        "raw_command": current_item["raw_command"],
                    }
                ]
            }
        },
    }
    diff_summary = {"changed_paths": ["implemented.txt"], "head_sha": head}
    allowed_paths = ["implemented.txt"]

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    assert result["per_ac"][0]["reason_code"] == "expected_fail_resolved_on_current_head"


def test_real_producer_baseline_mode_non_regression_gate_pass_stays_uncertified(
    tmp_path: Path,
) -> None:
    """GIVEN a real baseline_vc_preflight.py run in the DEFAULT baseline
    evidence-mode (not current-head) over the same rg VC that exits 0 THEN
    the producer envelope status is NOT pass, so adjudicate_vc_result() must
    never certify a current pass from it (AC1 cross-check via consumer)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)

    body = (
        "## Allowed Paths\n"
        "- tracked.txt\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "$ rg -q hello tracked.txt\n"
        "```\n"
    )
    body_file = tmp_path / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    current_vc_result = _run_producer(repo=repo, body_file=body_file)

    # AC1: baseline mode must NOT certify a non-regression-gate exit 0 VC.
    assert current_vc_result["status"] != "pass"
    assert current_vc_result["results"][0]["classification"] == "unexpected_pass"


def test_real_producer_pr_review_only_current_head_envelope_is_adjudicated(
    tmp_path: Path,
) -> None:
    """A real producer skip is excluded only after its routing evidence is complete."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    head = _commit_all(repo, "initial")
    body_file = tmp_path / "issue-1540-pr-review-only-body.md"
    body_file.write_text(
        "## Allowed Paths\n"
        "- tracked.txt\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "# preflight-scope: pr_review_only\n"
        "$ rg -q fixture tracked.txt\n"
        "```\n",
        encoding="utf-8",
    )
    current_vc_result = _run_producer(
        repo=repo,
        body_file=body_file,
        evidence_mode="current-head",
        reviewed_head_sha=head,
    )

    assert current_vc_result["status"] == "pass"
    current_item = current_vc_result["results"][0]
    assert current_item["scope_class"] == "pr_review_only"
    assert current_item["classification"] == "skipped"
    assert current_item["category"] == "preflight_scope_pr_review_only"
    assert current_item["verification_owner"] == "pr-review-judge"
    assert current_item["runtime_verification_required"] is False

    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": current_vc_result["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": [current_item]}},
    }
    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary={
            "changed_paths": ["tracked.txt"],
            "head_sha": head,
            "pr_number": 1544,
        },
        allowed_paths=["tracked.txt"],
        test_verdict=_manual_test_verdict(
            [current_item],
            head_sha=head,
            reviewed_head_sha=head,
            diff_head_sha=head,
            issue_number=current_vc_result["issue"],
            contract_body_sha256=current_vc_result["source"]["body_sha256"],
        ),
    )

    assert result["overall_status"] == "pass"
    assert result["per_ac"][0]["reason_code"] == "pr_review_only_runtime_evidence_pass"


def test_test_verdict_v2_provenance_rejects_tampered_artifact_payload() -> None:
    """GIVEN a producer-authorized skip WHEN artifact binding drifts THEN PASS is denied."""
    item = _manual_vc_result(
        "AC8",
        command_hash="sha256:" + "8" * 64,
        classification="skipped",
        decision="go",
        scope_class="pr_review_only",
        exit_code=None,
    )
    snapshot = _manual_contract_snapshot([item])
    current = _manual_current_payload([item], head_sha="head-8", reviewed_head_sha="head-8")
    verdict = _manual_test_verdict(
        [item], head_sha="head-8", reviewed_head_sha="head-8", diff_head_sha="head-8"
    )
    verdict["artifact_payload"]["head_sha"] = "stale-head"

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary={"changed_paths": [ADJUDICATOR_PATH], "head_sha": "head-8", "pr_number": 1544},
        allowed_paths=[ADJUDICATOR_PATH],
        test_verdict=verdict,
    )

    assert result["overall_status"] == "indeterminate"
    assert result["errors"] == ["test_verdict_artifact_digest_mismatch"]


def test_test_verdict_v2_provenance_requires_github_artifact_digest() -> None:
    """A payload self-hash cannot substitute for Actions artifact metadata."""
    item = _manual_vc_result(
        "AC8",
        command_hash="sha256:" + "8" * 64,
        classification="skipped",
        decision="go",
        scope_class="pr_review_only",
        exit_code=None,
    )
    snapshot = _manual_contract_snapshot([item])
    current = _manual_current_payload([item], head_sha="head-8", reviewed_head_sha="head-8")
    verdict = _manual_test_verdict(
        [item], head_sha="head-8", reviewed_head_sha="head-8", diff_head_sha="head-8"
    )
    verdict["artifact"].pop("artifact_digest")

    result = mod.adjudicate_vc_result(
        contract_snapshot=snapshot,
        current_vc_result=current,
        diff_summary={"changed_paths": [ADJUDICATOR_PATH], "head_sha": "head-8", "pr_number": 1544},
        allowed_paths=[ADJUDICATOR_PATH],
        test_verdict=verdict,
    )

    assert result["overall_status"] == "indeterminate"
    assert result["errors"] == ["test_verdict_artifact_digest_invalid"]


def test_per_ac_coverage_rejects_empty_pass() -> None:
    """GIVEN a direct result construction WHEN PASS has no AC coverage THEN it fails closed."""
    result = mod._result(
        overall_status="pass",
        per_ac=[],
        rerun_required=False,
        source_integrity={},
        evidence_refs=[],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["errors"] == ["pass_requires_per_ac_coverage"]


# ---------------------------------------------------------------------------
# PR #1497 review: "テスト設計の修正方針" — full E2E fixture with real
# baseline commit, real baseline producer snapshot, a second commit that
# changes the Allowed Path, a real current-head producer run, and a real
# `git diff`-derived diff_summary. Plus negative cases: external path, quiet
# partial error (delete + match), and HEAD drift.
# ---------------------------------------------------------------------------


def test_e2e_real_baseline_and_current_head_producer_pipeline_resolves_via_git_diff(
    tmp_path: Path,
) -> None:
    """(1) real baseline commit, (2) real baseline producer snapshot saved,
    (3) a second commit changes tracked.txt (Allowed Path) so the VC now
    matches, (4) real current-head producer run, (5) diff_summary generated
    from a REAL `git diff` between the two commits, (6) fed into the
    adjudicator THEN overall_status is pass via
    expected_fail_resolved_on_current_head. Nothing here is a hand-written
    snapshot/diff -- every producer output and diff is real."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    body = (
        "## Allowed Paths\n"
        "- tracked.txt\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "$ rg -q hello tracked.txt\n"
        "```\n"
    )
    body_file = tmp_path / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    # (1) baseline commit: tracked.txt exists but does not yet satisfy the VC.
    (repo / "tracked.txt").write_text("placeholder\n", encoding="utf-8")
    base_sha = _commit_all(repo, "baseline")

    # (2) real baseline producer run + saved snapshot.
    baseline_result = _run_producer(repo=repo, body_file=body_file)
    assert baseline_result["results"][0]["classification"] == "expected_fail"
    baseline_item = baseline_result["results"][0]

    # (3) a second commit changes the Allowed Path so the VC now matches.
    (repo / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    head_sha = _commit_all(repo, "implement AC1")

    # (4) real current-head producer run.
    current_vc_result = _run_producer(
        repo=repo,
        body_file=body_file,
        evidence_mode="current-head",
        reviewed_head_sha=head_sha,
    )
    assert current_vc_result["status"] == "pass"

    # (5) diff_summary generated from a REAL git diff.
    changed_paths = _git_diff_changed_paths(repo, base_sha, head_sha)
    assert changed_paths == ["tracked.txt"]
    diff_summary = {"changed_paths": changed_paths, "head_sha": head_sha}

    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": baseline_result["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": [baseline_item]}},
    }

    # (6) fed into the adjudicator.
    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    assert result["per_ac"][0]["reason_code"] == "expected_fail_resolved_on_current_head"


def test_e2e_negative_repo_external_path_never_resolves_via_pipeline(tmp_path: Path) -> None:
    """(7) negative case: a VC targeting a repo-EXTERNAL path (outside
    Allowed Paths / the repo tree) must never resolve to a certified current
    PASS through the full real pipeline, even if the external file exists."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    sentinel = tmp_path / "vc-sentinel"

    body = (
        "## Allowed Paths\n"
        "- src/feature.ts\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        f"# AC1\n"
        f"$ test -f {sentinel}\n"
        "```\n"
    )
    body_file = tmp_path / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    (repo / "src").mkdir()
    (repo / "src" / "feature.ts").write_text("// placeholder\n", encoding="utf-8")
    base_sha = _commit_all(repo, "baseline")

    baseline_result = _run_producer(repo=repo, body_file=body_file)
    assert baseline_result["results"][0]["classification"] == "expected_fail"

    # "别プロセスが作成" -- the sentinel is created OUTSIDE the repo/commit
    # entirely; the implementation commit itself does not touch it.
    sentinel.write_text("external state\n", encoding="utf-8")
    (repo / "src" / "feature.ts").write_text("// implemented\n", encoding="utf-8")
    head_sha = _commit_all(repo, "implement AC1 (unrelated to sentinel)")

    current_vc_result = _run_producer(
        repo=repo,
        body_file=body_file,
        evidence_mode="current-head",
        reviewed_head_sha=head_sha,
    )
    # The producer itself must refuse to certify: exit 0 on a repo-external
    # path is never promoted (Blocker 1).
    assert current_vc_result["status"] != "pass"
    assert current_vc_result["results"][0]["classification"] == "unexpected_pass"

    changed_paths = _git_diff_changed_paths(repo, base_sha, head_sha)
    diff_summary = {"changed_paths": changed_paths, "head_sha": head_sha}
    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": baseline_result["source"]["body_sha256"],
        "checks": {
            "vc_preflight": {"classifications": [baseline_result["results"][0]]}
        },
    }

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=["src/feature.ts"],
    )
    assert result["overall_status"] != "pass"
    assert result["blocking"] is True


def test_e2e_negative_quiet_partial_error_delete_and_match_never_resolves(
    tmp_path: Path,
) -> None:
    """(7) negative case: reproduces the reviewer's exact Blocker 2 scenario
    through the full real pipeline -- baseline has two files without the
    needle; the implementation commit deletes one and adds the needle to the
    other, producing a quiet `rg -q` partial success (exit 0 + missing-path
    stderr) that must never resolve to a certified current PASS."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    body = (
        "## Allowed Paths\n"
        "- deleted.txt\n"
        "- changed.txt\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "$ rg -q needle deleted.txt changed.txt\n"
        "```\n"
    )
    body_file = tmp_path / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    (repo / "deleted.txt").write_text("placeholder\n", encoding="utf-8")
    (repo / "changed.txt").write_text("placeholder\n", encoding="utf-8")
    base_sha = _commit_all(repo, "baseline")

    baseline_result = _run_producer(repo=repo, body_file=body_file)
    assert baseline_result["results"][0]["classification"] == "expected_fail"

    (repo / "deleted.txt").unlink()
    (repo / "changed.txt").write_text("needle\n", encoding="utf-8")
    head_sha = _commit_all(repo, "delete one file, add needle to the other")

    current_vc_result = _run_producer(
        repo=repo,
        body_file=body_file,
        evidence_mode="current-head",
        reviewed_head_sha=head_sha,
    )
    # The producer itself must refuse to certify this quiet partial success.
    assert current_vc_result["status"] != "pass"
    current_item = current_vc_result["results"][0]
    assert current_item["exit_code"] == 0
    assert current_item["classification"] == "unexpected_pass"

    changed_paths = _git_diff_changed_paths(repo, base_sha, head_sha)
    diff_summary = {"changed_paths": changed_paths, "head_sha": head_sha}
    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": baseline_result["source"]["body_sha256"],
        "checks": {
            "vc_preflight": {"classifications": [baseline_result["results"][0]]}
        },
    }

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=["deleted.txt", "changed.txt"],
    )
    assert result["overall_status"] != "pass"
    assert result["blocking"] is True


def test_e2e_negative_head_drift_never_resolves_via_pipeline(tmp_path: Path) -> None:
    """(7) negative case: HEAD drift between the certified current-head run
    and the diff_summary's head_sha must fail closed (indeterminate),
    through the full real pipeline."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    body = (
        "## Allowed Paths\n"
        "- tracked.txt\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "$ rg -q hello tracked.txt\n"
        "```\n"
    )
    body_file = tmp_path / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    (repo / "tracked.txt").write_text("placeholder\n", encoding="utf-8")
    base_sha = _commit_all(repo, "baseline")
    baseline_result = _run_producer(repo=repo, body_file=body_file)

    (repo / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    head_sha = _commit_all(repo, "implement AC1")

    current_vc_result = _run_producer(
        repo=repo,
        body_file=body_file,
        evidence_mode="current-head",
        reviewed_head_sha=head_sha,
    )
    assert current_vc_result["status"] == "pass"

    # HEAD drift: diff_summary claims a DIFFERENT head_sha than the one the
    # current-head producer actually certified.
    drifted_diff_summary = {"changed_paths": ["tracked.txt"], "head_sha": base_sha}
    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": baseline_result["source"]["body_sha256"],
        "checks": {
            "vc_preflight": {"classifications": [baseline_result["results"][0]]}
        },
    }

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=drifted_diff_summary,
        allowed_paths=["tracked.txt"],
    )
    assert result["overall_status"] != "pass"
    assert result["blocking"] is True


def test_non_regression_scope_pr_review_only_skip_item_is_excluded_from_regression_comparison() -> None:
    baseline = _manual_contract_snapshot(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="expected_fail",
                decision="go",
                scope_class=None,
                exit_code=1,
            ),
            _manual_vc_result(
                "AC2",
                command_hash="sha256:" + "c" * 64,
                classification="skipped",
                decision="go",
                scope_class="pr_review_only",
                exit_code=1,
            ),
        ]
    )
    current = _manual_current_payload(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="expected_pass",
                decision="go",
                scope_class=None,
                exit_code=0,
            ),
            _manual_vc_result(
                "AC2",
                command_hash="sha256:" + "c" * 64,
                classification="skipped",
                decision="go",
                scope_class="pr_review_only",
                exit_code=1,
            ),
        ],
        head_sha="head-1",
        reviewed_head_sha="head-1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=ADJUDICATOR_DIFF_SUMMARY,
        allowed_paths=[ADJUDICATOR_PATH],
        test_verdict=_manual_test_verdict(
            baseline["checks"]["vc_preflight"]["classifications"],
            head_sha="head-1",
            reviewed_head_sha="head-1",
            diff_head_sha="head-1",
        ),
    )

    assert result["overall_status"] == "pass"
    assert len(result["per_ac"]) == 1
    assert result["per_ac"][0]["ac"] == "AC1"
    assert result["per_ac"][0]["reason_code"] == "expected_fail_resolved_on_current_head"


def test_non_regression_scope_pr_review_only_skip_missing_scope_is_not_excluded() -> None:
    baseline = _manual_contract_snapshot(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="skipped",
                decision="go",
                scope_class="runtime_only",
                exit_code=1,
            ),
        ]
    )
    current = _manual_current_payload(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="expected_pass",
                decision="go",
                scope_class="runtime_only",
                exit_code=0,
            )
        ],
        head_sha="head-1",
        reviewed_head_sha="head-1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=ADJUDICATOR_DIFF_SUMMARY,
        allowed_paths=[ADJUDICATOR_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["unsupported_baseline_classification:AC1"]


def test_non_regression_scope_pr_review_only_skip_wrong_decision_is_not_excluded() -> None:
    baseline = _manual_contract_snapshot(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="skipped",
                decision="blocked",
                scope_class="pr_review_only",
                exit_code=1,
            ),
        ]
    )
    current = _manual_current_payload(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="expected_pass",
                decision="go",
                scope_class="pr_review_only",
                exit_code=0,
            )
        ],
        head_sha="head-1",
        reviewed_head_sha="head-1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=ADJUDICATOR_DIFF_SUMMARY,
        allowed_paths=[ADJUDICATOR_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["unsupported_baseline_classification:AC1"]


def test_current_head_runtime_evidence_is_required_for_pr_review_only_only_payload() -> None:
    baseline = _manual_contract_snapshot(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="skipped",
                decision="go",
                scope_class="pr_review_only",
                exit_code=1,
            ),
        ]
    )
    current = _manual_current_payload(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="skipped",
                decision="go",
                scope_class="pr_review_only",
                exit_code=None,
            )
        ],
        head_sha="head-1",
        reviewed_head_sha="head-1",
    )

    certified = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=ADJUDICATOR_DIFF_SUMMARY,
        allowed_paths=[ADJUDICATOR_PATH],
        test_verdict=_manual_test_verdict(
            baseline["checks"]["vc_preflight"]["classifications"],
            head_sha="head-1",
            reviewed_head_sha="head-1",
            diff_head_sha="head-1",
        ),
    )
    assert certified["overall_status"] == "pass"
    assert certified["per_ac"][0]["reason_code"] == "pr_review_only_runtime_evidence_pass"

    current["reviewed_head_sha"] = "head-0"
    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=ADJUDICATOR_DIFF_SUMMARY,
        allowed_paths=[ADJUDICATOR_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["test_verdict_missing"]


def test_pr_review_only_rejects_incomplete_evidence() -> None:
    baseline = _manual_contract_snapshot(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="skipped",
                decision="go",
                scope_class="pr_review_only",
                exit_code=None,
            ),
        ]
    )
    current = _manual_current_payload(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="expected_pass",
                decision="go",
                scope_class="pr_review_only",
                exit_code=0,
            )
        ],
        head_sha="head-1",
        reviewed_head_sha="head-1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=ADJUDICATOR_DIFF_SUMMARY,
        allowed_paths=[ADJUDICATOR_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["pr_review_only_current_authorization_mismatch:AC1"]


def test_pr_review_only_requires_complete_coverage_rejects_missing_skip() -> None:
    baseline_items = [
        _manual_vc_result(
            "AC1",
            command_hash="sha256:" + "a" * 64,
            classification="expected_fail",
            decision="go",
            scope_class=None,
            exit_code=1,
        ),
        _manual_vc_result(
            "AC2",
            command_hash="sha256:" + "c" * 64,
            classification="skipped",
            decision="go",
            scope_class="pr_review_only",
            exit_code=None,
        ),
    ]
    current = _manual_current_payload(
        [
            _manual_vc_result(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                classification="expected_pass",
                decision="go",
                scope_class=None,
                exit_code=0,
            )
        ],
        head_sha="head-1",
        reviewed_head_sha="head-1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=_manual_contract_snapshot(baseline_items),
        current_vc_result=current,
        diff_summary=ADJUDICATOR_DIFF_SUMMARY,
        allowed_paths=[ADJUDICATOR_PATH],
        test_verdict=_manual_test_verdict(
            baseline_items,
            head_sha="head-1",
            reviewed_head_sha="head-1",
            diff_head_sha="head-1",
        ),
    )

    assert result["overall_status"] != "pass"
    assert result["errors"] == ["pr_review_only_coverage_mismatch"]


def test_pr_review_only_requires_complete_coverage_rejects_missing_regular_vc() -> None:
    baseline_items = [
        _manual_vc_result(
            "AC1",
            command_hash="sha256:" + "a" * 64,
            classification="expected_fail",
            decision="go",
            scope_class=None,
            exit_code=1,
        ),
        _manual_vc_result(
            "AC2",
            command_hash="sha256:" + "c" * 64,
            classification="skipped",
            decision="go",
            scope_class="pr_review_only",
            exit_code=None,
        ),
    ]
    current = _manual_current_payload(
        [baseline_items[1]],
        head_sha="head-1",
        reviewed_head_sha="head-1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=_manual_contract_snapshot(baseline_items),
        current_vc_result=current,
        diff_summary=ADJUDICATOR_DIFF_SUMMARY,
        allowed_paths=[ADJUDICATOR_PATH],
        test_verdict=_manual_test_verdict(
            baseline_items,
            head_sha="head-1",
            reviewed_head_sha="head-1",
            diff_head_sha="head-1",
        ),
    )

    assert result["overall_status"] != "pass"
    assert result["errors"] == ["baseline_current_mapping_mismatch"]


def test_pr_review_only_requires_test_verdict_binding() -> None:
    items = [
        _manual_vc_result(
            "AC1",
            command_hash="sha256:" + "a" * 64,
            classification="skipped",
            decision="go",
            scope_class="pr_review_only",
            exit_code=None,
        )
    ]
    baseline = _manual_contract_snapshot(items)
    current = _manual_current_payload(items, head_sha="head-1", reviewed_head_sha="head-1")

    missing = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=ADJUDICATOR_DIFF_SUMMARY,
        allowed_paths=[ADJUDICATOR_PATH],
    )
    assert missing["overall_status"] != "pass"
    assert missing["errors"] == ["test_verdict_missing"]

    mismatched = _manual_test_verdict(
        items,
        head_sha="wrong-head",
        reviewed_head_sha="head-1",
        diff_head_sha="head-1",
    )
    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=ADJUDICATOR_DIFF_SUMMARY,
        allowed_paths=[ADJUDICATOR_PATH],
        test_verdict=mismatched,
    )
    assert result["overall_status"] != "pass"
    assert result["errors"] == ["test_verdict_head_sha_mismatch"]


# --- Issue #88 AC5/AC6: same-loop reuse + happy-path invocation counts ---
# Step4AdjudicationCache (an in-memory, process-scoped object) was removed
# in the Issue #88 fix_delta (Blocker 2): it could not survive across
# separate CLI invocations of this script and had no API to seed a
# Step-2-produced adjudication before Step 4 evaluates the gate. Reuse is
# now expressed as a plain LOOP_STATE.vc_adjudication mapping via
# step4_persist_vc_adjudication() / step4_gate_from_loop_state(), which is
# YAML/JSON-serializable and reusable across process boundaries. These
# tests simulate a loop iteration directly against those two functions so
# the reuse/no-double-execution behavior is proven without any new schema,
# hook, or publisher.


def _ac5_ac6_fixture_head_sha() -> str:
    return "head-ac5-ac6"


def _ac5_ac6_command_hash() -> str:
    return "sha256:" + "7" * 64


def _ac5_ac6_body_sha256() -> str:
    return "sha256:" + "e" * 64


def _ac5_ac6_baseline_and_current():
    baseline_item = {
        "ac": "AC1",
        "command_hash": _ac5_ac6_command_hash(),
        "failure_keys": [{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
        "exit_code": 4,
        "category": "regression_gate",
        "classification": "expected_fail",
        "decision": "blocked",
        "raw_command": "pytest tests/test_alpha.py",
        "raw_stdout": "",
        "raw_stderr": "",
    }
    current_item = {**baseline_item, "exit_code": 0, "failure_keys": []}
    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": _ac5_ac6_body_sha256(),
        "checks": {"vc_preflight": {"classifications": [baseline_item]}},
    }
    current_vc_result = {
        "schema": "baseline_vc_preflight/v1",
        "generated_at": "2026-08-23T00:00:00Z",
        "status": "pass",
        "errors": [],
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "source": {"body_sha256": _ac5_ac6_body_sha256()},
        "results": [current_item],
        "head_sha": _ac5_ac6_fixture_head_sha(),
        "reviewed_head_sha": _ac5_ac6_fixture_head_sha(),
    }
    diff_summary = {
        "changed_paths": [ADJUDICATOR_PATH],
        "head_sha": _ac5_ac6_fixture_head_sha(),
    }
    return contract_snapshot, current_vc_result, diff_summary


def _run_step4_loop_iteration(loop_state, run_test_runner_and_adjudicate):
    """Simulate a single Step 4 loop iteration against a plain
    LOOP_STATE.vc_adjudication mapping: reuse-or-run test-runner, persist a
    valid adjudication into loop_state, then evaluate the gate and spawn
    pr-reviewer only when it opens."""
    lookup = mod.step4_gate_from_loop_state(
        loop_state,
        expected_head_sha=_ac5_ac6_fixture_head_sha(),
        expected_contract_body_sha256=_ac5_ac6_body_sha256(),
        expected_command_hashes=[_ac5_ac6_command_hash()],
    )
    if lookup["reused"]:
        return lookup, True

    adjudication_result = run_test_runner_and_adjudicate()
    mod.step4_persist_vc_adjudication(
        loop_state,
        head_sha=_ac5_ac6_fixture_head_sha(),
        contract_body_sha256=_ac5_ac6_body_sha256(),
        command_hashes=[_ac5_ac6_command_hash()],
        adjudication_result=adjudication_result,
    )
    decision = mod.evaluate_step4_vc_gate(
        adjudication_result,
        expected_head_sha=_ac5_ac6_fixture_head_sha(),
        expected_contract_body_sha256=_ac5_ac6_body_sha256(),
        expected_command_hashes=[_ac5_ac6_command_hash()],
    )
    return decision, False


def test_ac5_same_binding_tuple_reused_without_reexecuting_test_runner():
    contract_snapshot, current_vc_result, diff_summary = _ac5_ac6_baseline_and_current()
    test_runner_calls = {"count": 0}

    def _run_test_runner_and_adjudicate():
        test_runner_calls["count"] += 1
        return mod.adjudicate_vc_result(
            contract_snapshot=contract_snapshot,
            current_vc_result=current_vc_result,
            diff_summary=diff_summary,
            allowed_paths=[ADJUDICATOR_PATH],
        )

    # GIVEN an empty LOOP_STATE (no persisted vc_adjudication entries yet)
    loop_state: dict = {}

    first_decision, first_reused = _run_step4_loop_iteration(
        loop_state, _run_test_runner_and_adjudicate
    )
    assert first_decision["invoke_pr_reviewer"] is True
    assert first_reused is False
    assert test_runner_calls["count"] == 1

    # WHEN the same binding tuple is evaluated again within the same loop
    second_decision, second_reused = _run_step4_loop_iteration(
        loop_state, _run_test_runner_and_adjudicate
    )

    # THEN the LOOP_STATE-persisted adjudication is reused and test-runner
    # is not re-executed
    assert second_decision["invoke_pr_reviewer"] is True
    assert second_reused is True
    assert test_runner_calls["count"] == 1


def test_ac6_happy_path_fixture_test_runner_once_pr_reviewer_once():
    contract_snapshot, current_vc_result, diff_summary = _ac5_ac6_baseline_and_current()
    test_runner_calls = {"count": 0}
    pr_reviewer_calls = {"count": 0}

    def _run_test_runner_and_adjudicate():
        test_runner_calls["count"] += 1
        return mod.adjudicate_vc_result(
            contract_snapshot=contract_snapshot,
            current_vc_result=current_vc_result,
            diff_summary=diff_summary,
            allowed_paths=[ADJUDICATOR_PATH],
        )

    loop_state: dict = {}

    # GIVEN a single happy-path binding tuple loop iteration
    decision, _ = _run_step4_loop_iteration(loop_state, _run_test_runner_and_adjudicate)
    if decision["invoke_pr_reviewer"]:
        pr_reviewer_calls["count"] += 1

    # THEN test-runner ran exactly once and pr-reviewer ran exactly once
    assert test_runner_calls["count"] == 1
    assert pr_reviewer_calls["count"] == 1


def test_persist_only_stores_valid_adjudication_result():
    # GIVEN an adjudication result that does NOT satisfy evaluate_step4_vc_gate
    # for its own asserted binding (blocking True)
    contract_snapshot, current_vc_result, diff_summary = _ac5_ac6_baseline_and_current()
    blocking_result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=[ADJUDICATOR_PATH],
    )
    blocking_result = {**blocking_result, "blocking": True}
    loop_state: dict = {}

    # WHEN Step 2 attempts to persist it into LOOP_STATE
    mod.step4_persist_vc_adjudication(
        loop_state,
        head_sha=_ac5_ac6_fixture_head_sha(),
        contract_body_sha256=_ac5_ac6_body_sha256(),
        command_hashes=[_ac5_ac6_command_hash()],
        adjudication_result=blocking_result,
    )

    # THEN nothing is stored (Issue #88 AC5: only a valid adjudication is reused)
    assert loop_state.get("vc_adjudication", {}) == {}


def test_command_hash_reorder_is_treated_as_stale_binding():
    # GIVEN a valid adjudication bound to commands in order [A, B]
    hash_a = "sha256:" + "a" * 64
    hash_b = "sha256:" + "b" * 64
    adjudication = {
        "schema": mod.SCHEMA_NAME,
        "schema_version": mod.SCHEMA_VERSION,
        "overall_status": "pass",
        "blocking": False,
        "rerun_required": False,
        "per_ac": [
            {
                "ac": "AC1",
                "status": "pass",
                "blocking": False,
                "command_hash": hash_a,
                "failure_keys": [],
                "reason_code": "expected_pass_still_passes",
                "summary": "ok",
            },
            {
                "ac": "AC2",
                "status": "pass",
                "blocking": False,
                "command_hash": hash_b,
                "failure_keys": [],
                "reason_code": "expected_pass_still_passes",
                "summary": "ok",
            },
        ],
        "evidence_refs": [],
        "source_integrity": {
            "head_sha": _ac5_ac6_fixture_head_sha(),
            "contract_body_sha256": _ac5_ac6_body_sha256(),
            "evidence_fresh": True,
            "evidence_complete": True,
        },
        "errors": [],
        "artifact_ref": None,
        "artifact_digest": None,
        "stdout_truncated": False,
        "omitted_fields": [],
    }

    # WHEN Step 4 evaluates the gate expecting the same commands reordered [B, A]
    decision = mod.evaluate_step4_vc_gate(
        adjudication,
        expected_head_sha=_ac5_ac6_fixture_head_sha(),
        expected_contract_body_sha256=_ac5_ac6_body_sha256(),
        expected_command_hashes=[hash_b, hash_a],
    )

    # THEN the gate treats it as a command mismatch (stale binding), even
    # though the set of command hashes is identical (Issue #88 fix_delta
    # Blocker 3)
    assert decision["invoke_pr_reviewer"] is False
    assert decision["reason_code"] == "command_mismatch"
