"""Stage 1b-3 closure / false-green controls for the review producer + reviewer_transport.

Fixture + real-subprocess + fake `gh` only (no live GitHub, no LLM reviewer). Pins:
- the five migrated scripts import under `python -I`; a missing dependency or a `parents[N]`
  layout shift fails closed (never silently approves);
- `produce` returns approve for the canonical body, needs-fix for the broken one, and a tampered
  wire is invalid (positive / negative control);
- `--repo` is bound to the repository identity config BEFORE any `gh` call;
- retry is limited to spawn_failure / signal / capture_failure;
- the consumer (`decide_next_loop_action`) rejects stale / foreign / fixture transport results.

Evidence class: fixture / fake `gh`. Nothing here is live GitHub or live LLM evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".claude" / "skills" / "issue-refinement-loop"
SCRIPTS_REL = ".claude/skills/issue-refinement-loop/scripts"
PRODUCE_REL = f"{SCRIPTS_REL}/run_root_review_pipeline.py"
VALIDATE_REL = f"{SCRIPTS_REL}/validate_review_compact_output.py"
ROUTER_REL = f"{SCRIPTS_REL}/decide_next_loop_action.py"
TRUSTED_SLUG = json.loads((ROOT / "config" / "repository_identity.v1.json").read_text())[
    "trusted_repo_slug"
]
MIGRATED = (
    "run_root_review_pipeline",
    "reviewer_transport",
    "compact_review_result",
    "compact_author_result",
    "validate_review_compact_output",
)


FIXTURES = ROOT / "tests" / "stage1b" / "fixtures"
# Canonical approve / broken needs-fix bodies (extracted verbatim from the source
# test_root_review_canonical_delivery.py `_APPROVE_BODY` / `_NEEDS_FIX_BODY`).
APPROVE_BODY = (FIXTURES / "stage1b3_approve_body.md").read_text(encoding="utf-8")
NEEDS_FIX_BODY = (FIXTURES / "stage1b3_needs_fix_body.md").read_text(encoding="utf-8")


def _ignore(_d: str, names: list[str]) -> set[str]:
    return {n for n in names if n in {"tests", "__pycache__", "fixtures"}}


def _tree(tmp: Path) -> Path:
    """Minimal synthetic repo holding the producer closure (no tests, no fixtures)."""
    for rel in (
        ".claude/skills/create-issue/scripts",
        ".claude/skills/issue-contract-review/scripts",
        ".claude/skills/impl-review-loop/scripts",
        ".claude/skills/review-issue/scripts",
        ".claude/skills/review-issue/schemas",
        SCRIPTS_REL,
        "scripts/agent-ops",
        "scripts/agent-guards",
        "config",
        "docs/dev",
    ):
        shutil.copytree(ROOT / rel, tmp / rel, ignore=_ignore)
    return tmp


def _fake_gh(tmp: Path, body: str) -> tuple[Path, Path]:
    """`gh` stub: logs argv, returns a canned issue body. Never touches the network."""
    log = tmp / "gh.log"
    bindir = tmp / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    payload = json.dumps({"body": body})
    gh.write_text(f"#!/bin/sh\necho \"$@\" >> '{log}'\ncat <<'JSON'\n{payload}\nJSON\n")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return bindir, log


def _run(tree: Path, rel: str, *args: str, bindir: Path | None = None, stdin: str | None = None):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if bindir is not None:
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [sys.executable, "-I", str(tree / rel), *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=env,
        cwd=tree,
        check=False,
        timeout=300,
    )


def _produce(tmp_path: Path, body: str, issue: int, *extra: str):
    tree = _tree(tmp_path / "tree")
    bindir, log = _fake_gh(tmp_path, body)
    run = _run(tree, PRODUCE_REL, "produce", "--issue-number", str(issue), *extra, bindir=bindir)
    return tree, log, run


# --- AC-1: python -I import, missing dependency, layout shift -----------------------------------


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_script_imports_under_isolated_python(tmp_path: Path, name: str):
    tree = _tree(tmp_path / "tree")
    code = (
        "import importlib.util,sys;"
        f"p=sys.argv[1]+'/{SCRIPTS_REL}/{name}.py';"
        "s=importlib.util.spec_from_file_location('m',p);m=importlib.util.module_from_spec(s);"
        "sys.modules['m']=m;s.loader.exec_module(m)"
    )
    run = subprocess.run(
        [sys.executable, "-I", "-c", code, str(tree)], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize(
    "missing",
    ["reviewer_transport", "compact_review_result", "validate_review_compact_output"],
)
def test_missing_closure_module_fails_closed_not_degraded(tmp_path: Path, missing: str):
    tree = _tree(tmp_path / "tree")
    (tree / SCRIPTS_REL / f"{missing}.py").unlink()
    run = _run(tree, PRODUCE_REL, "--help")
    assert run.returncode != 0
    assert "ModuleNotFoundError" in run.stderr or "ImportError" in run.stderr


def test_missing_readiness_checker_never_yields_approve(tmp_path: Path):
    """A deleted downstream checker must surface as a failure, never as an approve."""
    tree = _tree(tmp_path / "tree")
    bindir, _log = _fake_gh(tmp_path, APPROVE_BODY)
    (tree / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py").unlink()
    run = _run(tree, PRODUCE_REL, "produce", "--issue-number", "7", bindir=bindir)
    assert '"verdict": "approve"' not in run.stdout
    assert run.returncode != 0 or '"status": "ok"' not in run.stdout


def test_layout_shift_of_scripts_dir_never_approves(tmp_path: Path):
    """`parents[N]` / `.parent` chains resolve the repo root: moving the scripts one level deeper
    must not silently resolve a wrong root and approve."""
    tree = _tree(tmp_path / "tree")
    scripts = tree / SCRIPTS_REL
    shifted = scripts.parent / "extra" / "scripts"
    shifted.parent.mkdir()
    scripts.rename(shifted)
    bindir, _log = _fake_gh(tmp_path, APPROVE_BODY)
    run = _run(
        tree,
        ".claude/skills/issue-refinement-loop/extra/scripts/run_root_review_pipeline.py",
        "produce",
        "--issue-number",
        "7",
        bindir=bindir,
    )
    assert '"verdict": "approve"' not in run.stdout
    assert run.returncode != 0 or '"status": "ok"' not in run.stdout


# --- Positive / negative control (fake gh boundary only) ----------------------------------------


def test_produce_canonical_body_approves_through_fake_gh(tmp_path: Path):
    _unused_tree, log, run = _produce(tmp_path, APPROVE_BODY, 7)
    assert run.returncode == 0, run.stdout + run.stderr
    out = json.loads(run.stdout)
    assert out["status"] == "ok"
    assert out["compact_result"]["verdict"] == "approve"
    assert out["compact_result"]["next_action"] == "proceed"
    assert out["canonical_step2_route"] == "step_2_5"
    # default --repo resolves to the trusted identity (no literal default), one gh read
    assert log.read_text().strip() == f"issue view 7 --repo {TRUSTED_SLUG} --json body"


def test_produce_broken_body_is_needs_fix(tmp_path: Path):
    _unused_tree, _log, run = _produce(tmp_path, NEEDS_FIX_BODY, 8)
    out = json.loads(run.stdout)
    assert out["compact_result"]["verdict"] == "needs-fix"
    assert out["merged_review_result"]["blocking_issues"]
    assert out["canonical_step2_route"] == "step_4"


def test_tampered_wire_is_invalid(tmp_path: Path):
    tree, _log, run = _produce(tmp_path, APPROVE_BODY, 9)
    lines = json.loads(run.stdout)["compact_result"]["stdout_lines"]
    wire = "\n".join(lines) + "\n"
    ok = _run(tree, VALIDATE_REL, "--issue-number", "9", stdin=wire)
    assert ok.returncode == 0 and json.loads(ok.stdout)["validation_status"] == "valid"
    for tampered in (
        wire.replace("approve", "needs-fix"),
        wire.replace("sha256:", "sha256:0", 1),
        wire + "EXTRA: line\n",
    ):
        bad = _run(tree, VALIDATE_REL, "--issue-number", "9", stdin=tampered)
        assert bad.returncode != 0, tampered
        assert json.loads(bad.stdout)["validation_status"] == "invalid"
    wrong_issue = _run(tree, VALIDATE_REL, "--issue-number", "10", stdin=wire)
    assert json.loads(wrong_issue.stdout)["validation_status"] == "invalid"


# --- Repository identity: bound before any gh call ----------------------------------------------


def test_explicit_foreign_repo_is_rejected_before_gh(tmp_path: Path):
    _unused_tree, log, run = _produce(
        tmp_path, APPROVE_BODY, 7, "--repo", "example-org/example-repo"
    )
    assert run.returncode == 2
    out = json.loads(run.stdout)
    assert out["status"] == "input_or_runtime_error"
    assert out["error_code"] == "repository_identity_mismatch"
    assert out["canonical_step2_route"] == "fail_closed_environment_or_integrity_failure"
    assert not log.exists(), "gh must not be invoked for an untrusted repository"


@pytest.mark.parametrize("breakage", ["missing", "malformed", "bad_slug", "extra_key"])
def test_unusable_identity_config_fails_closed_before_gh(tmp_path: Path, breakage: str):
    tree = _tree(tmp_path / "tree")
    bindir, log = _fake_gh(tmp_path, APPROVE_BODY)
    cfg = tree / "config" / "repository_identity.v1.json"
    if breakage == "missing":
        cfg.unlink()
    elif breakage == "malformed":
        cfg.write_text("{not json")
    elif breakage == "bad_slug":
        cfg.write_text(json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "x"}))
    else:
        cfg.write_text(
            json.dumps(
                {"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": TRUSTED_SLUG, "x": 1}
            )
        )
    run = _run(tree, PRODUCE_REL, "produce", "--issue-number", "7", bindir=bindir)
    assert run.returncode == 2
    assert json.loads(run.stdout)["error_code"] == "repository_identity_unavailable"
    assert not log.exists()


# --- reviewer_transport: closed retry matrix ----------------------------------------------------


def test_deterministic_retry_is_limited_to_transient_reason_codes():
    sys.path.insert(0, str(LOOP / "scripts"))
    try:
        import reviewer_transport as rt
    finally:
        sys.path.remove(str(LOOP / "scripts"))
    for code in ("spawn_failure", "signal", "capture_failure"):
        assert rt.retry_matrix(
            backend="deterministic", initial_session_ref=None, attempt=1, reason_code=code
        ), code
    for code in (
        "nonzero_exit",
        "malformed_output",
        "empty_output",
        "timeout",
        "inner_timeout",
        "artifact_validation_failure",
        "integrity_failure",
    ):
        assert (
            rt.retry_matrix(
                backend="deterministic", initial_session_ref=None, attempt=1, reason_code=code
            )
            is None
        ), code


# --- Consumer: stale / foreign / fixture transport results never become ok+approve --------------


BODY_SHA = "sha256:" + "d" * 64
INVOCATION = "inv-bound"


def _result(**override) -> dict:
    base = {
        "schema": "REVIEWER_TRANSPORT_RESULT_V1",
        "transport_status": "ok",
        "semantic_verdict": "approve",
        "invocation_id": INVOCATION,
        "backend": "deterministic",
        "issue_number": 7,
        "repository": TRUSTED_SLUG,
        "reviewed_body_sha256": BODY_SHA,
        "attempts": [
            {
                "transport_status": "ok",
                "semantic_verdict": "approve",
                "invocation_id": INVOCATION,
                "backend": "deterministic",
            }
        ],
    }
    base.update(override)
    return base


def _route(tmp_path: Path, result: dict, *, drop: str | None = None, **overrides):
    receipt = tmp_path / "transport.json"
    receipt.write_text(json.dumps(result))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"iteration": 1, "max_iterations": 3, "last_verdict": None}))
    flags = {
        "--issue-number": "7",
        "--repo": TRUSTED_SLUG,
        "--invocation-id": INVOCATION,
        "--expected-body-sha256": BODY_SHA,
    }
    flags.update(overrides)
    if drop:
        flags.pop(drop)
    argv = [
        sys.executable,
        "-I",
        str(ROOT / ROUTER_REL),
        "--loop-state-file",
        str(state),
        "--reviewer-transport-result-file",
        str(receipt),
    ]
    for key, value in flags.items():
        argv += [key, value]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_consumer_accepts_a_bound_real_review_result(tmp_path: Path):
    run = _route(tmp_path, _result())
    assert run.returncode != 4, run.stdout
    assert "environment_failure" not in run.stdout


@pytest.mark.parametrize(
    ("result_override", "flag_override", "reason"),
    [
        ({"invocation_id": "inv-stale"}, {}, "invocation_id_mismatch"),
        ({"reviewed_body_sha256": "sha256:" + "e" * 64}, {}, "reviewed_body_sha256_mismatch"),
        ({"issue_number": 8}, {}, "issue_number_mismatch"),
        ({"repository": "example-org/example-repo"}, {}, "repository_mismatch"),
        ({}, {"--repo": "example-org/example-repo"}, "repository_mismatch"),
        (
            {
                "backend": "fixture",
                "attempts": [
                    {"transport_status": "ok", "invocation_id": INVOCATION, "backend": "fixture"}
                ],
            },
            {},
            "backend_not_real_review",
        ),
        ({"backend": None}, {}, "backend_not_real_review"),
        ({"attempts": []}, {}, "attempt"),
        (
            {
                "attempts": [
                    {
                        "transport_status": "ok",
                        "invocation_id": "inv-other",
                        "backend": "deterministic",
                    }
                ]
            },
            {},
            "invocation_id_mismatch",
        ),
    ],
)
def test_consumer_rejects_stale_foreign_or_fixture_result(
    tmp_path: Path, result_override: dict, flag_override: dict, reason: str
):
    run = _route(tmp_path, _result(**result_override), **flag_override)
    assert run.returncode == 4, run.stdout
    assert "STATUS: environment_failure" in run.stdout
    assert f"reviewer_transport_result_unbound:{reason}" in run.stdout


@pytest.mark.parametrize("flag", ["--invocation-id", "--expected-body-sha256", "--issue-number"])
def test_consumer_without_expected_binding_fails_closed(tmp_path: Path, flag: str):
    run = _route(tmp_path, _result(), drop=flag)
    assert run.returncode == 4
    assert "reviewer_transport_result_unbound:expected_" in run.stdout


def test_consumer_environment_failure_result_still_escalates(tmp_path: Path):
    run = _route(
        tmp_path,
        {
            "schema": "REVIEWER_TRANSPORT_RESULT_V1",
            "transport_status": "environment_failure",
            "semantic_verdict": None,
        },
    )
    assert run.returncode == 4


def test_consumer_with_unusable_identity_config_fails_closed(tmp_path: Path):
    tree = _tree(tmp_path / "tree")
    (tree / "config" / "repository_identity.v1.json").unlink()
    receipt = tmp_path / "transport.json"
    receipt.write_text(json.dumps(_result()))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"iteration": 1, "max_iterations": 3, "last_verdict": None}))
    run = _run(
        tree,
        ROUTER_REL,
        "--loop-state-file",
        str(state),
        "--reviewer-transport-result-file",
        str(receipt),
        "--issue-number",
        "7",
        "--repo",
        TRUSTED_SLUG,
        "--invocation-id",
        INVOCATION,
        "--expected-body-sha256",
        BODY_SHA,
    )
    assert run.returncode == 4
    assert "repository_identity_unavailable" in run.stdout


def test_real_transport_fixture_backend_result_is_not_accepted_by_consumer(tmp_path: Path):
    """End to end: a result produced by the REAL transport with backend=fixture carries
    backend=fixture and the consumer must refuse it as review evidence."""
    sys.path.insert(0, str(LOOP / "scripts"))
    try:
        import reviewer_transport as rt
    finally:
        sys.path.remove(str(LOOP / "scripts"))
    result = rt.run_reviewer_transport(
        base_argv=[
            sys.executable,
            "-c",
            "import json; print(json.dumps({'verdict':'approve','blocking_issues':[]}))",
        ],
        command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2",
        backend="fixture",
        issue_number=7,
        repo=TRUSTED_SLUG,
        reviewed_body_sha256=BODY_SHA,
        artifact_root=tmp_path / "artifacts",
        invocation_id=INVOCATION,
        per_attempt_deadline=5,
        total_deadline=10,
    )
    assert result["transport_status"] == "ok" and result["backend"] == "fixture"
    run = _route(tmp_path, result)
    assert run.returncode == 4
    assert "reviewer_transport_result_unbound:backend_not_real_review" in run.stdout


# --- Hygiene / identity residue in the migrated scripts -----------------------------------------


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_scripts_carry_no_session_id_or_legacy_slug(name: str):
    text = (LOOP / "scripts" / f"{name}.py").read_text(encoding="utf-8")
    assert "session_id" not in text
    assert "/".join(("squne121", "loop-protocol")) not in text  # noqa: FLY002
