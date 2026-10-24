"""Stage 2-1a closure / false-green controls for controlled_skill_mutation_policy.

Fixture only: no `gh`, no network, no GitHub write. Pins:
- the policy imports under `python -I` and its trusted slug IS the repository identity config
  (no second slug definition); every GitHub-mutating registry entry binds that slug;
- an unusable identity config (missing / malformed / wrong schema / bad slug) or a layout shift
  makes the import fail closed instead of defaulting to some slug;
- the legacy and malformed repo slugs are rejected by every request validator and no registry
  entry accepts them (positive control: the trusted slug is accepted);
- the executor argv gate still rejects shell metacharacters / unknown flags (no widening);
- the closure manifest pins the files, deferred refs and the executor-less state honestly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARDS = ROOT / "scripts" / "agent-guards"
POLICY = GUARDS / "controlled_skill_mutation_policy.py"
IDENTITY_CONFIG = ROOT / "config" / "repository_identity.v1.json"
MANIFEST = ROOT / "docs" / "dev" / "stage2-1a-policy-manifest.json"
TRUSTED = json.loads(IDENTITY_CONFIG.read_text())["trusted_repo_slug"]
# Same owner, different repository: the shape a legacy / sibling slug has. The literal legacy
# slug must not appear in a tracked file (check_no_functional_slug).
LEGACY = TRUSTED.split("/")[0] + "/some-other-repository"

if str(GUARDS) not in sys.path:
    sys.path.insert(0, str(GUARDS))

import controlled_skill_mutation_policy as policy

_IMPORT_CODE = (
    "import sys; sys.path.insert(0, {path!r}); "
    "import controlled_skill_mutation_policy as p; print(p.TRUSTED_REPO)"
)


def _isolated(code: str, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        cwd=str(cwd or ROOT),
        timeout=60,
        check=False,
    )


def _copy_layout(tmp_path: Path, *, config: str | None) -> Path:
    """Replica of the repo layout: <root>/scripts/agent-guards/{policy,repo_identity}.py."""
    guards = tmp_path / "scripts" / "agent-guards"
    guards.mkdir(parents=True)
    shutil.copy(POLICY, guards / POLICY.name)
    shutil.copy(GUARDS / "repo_identity.py", guards / "repo_identity.py")
    if config is not None:
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "repository_identity.v1.json").write_text(config)
    return guards


def _import_in(guards: Path):
    return _isolated(_IMPORT_CODE.format(path=str(guards)), cwd=guards)


def test_isolated_import_and_slug_is_identity_config() -> None:
    r = _isolated(_IMPORT_CODE.format(path=str(GUARDS)))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == TRUSTED
    assert policy.TRUSTED_REPO == TRUSTED


def test_no_second_slug_definition_in_policy_source() -> None:
    src = POLICY.read_text()
    assert LEGACY not in src
    assert "loop" + "-protocol" not in src
    assert TRUSTED not in src  # the slug is read from config, never written here


def test_layout_replica_with_valid_config_imports(tmp_path: Path) -> None:
    guards = _copy_layout(tmp_path, config=IDENTITY_CONFIG.read_text())
    r = _import_in(guards)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == TRUSTED


@pytest.mark.parametrize(
    "config",
    [
        None,  # layout shift: config not where parents[2] expects it
        "not json",
        "[]",
        json.dumps({"schema": "REPOSITORY_IDENTITY_V1"}),
        json.dumps({"schema": "WRONG", "trusted_repo_slug": "a/b"}),
        json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "no-slash"}),
        json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "a/../b"}),
        json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": ""}),
        json.dumps({"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": "a/b", "x": 1}),
    ],
    ids=[
        "missing",
        "malformed_json",
        "not_object",
        "missing_slug",
        "schema_mismatch",
        "slug_no_slash",
        "slug_dotdot",
        "slug_empty",
        "extra_key",
    ],
)
def test_unusable_identity_config_fails_closed_at_import(tmp_path: Path, config) -> None:
    guards = _copy_layout(tmp_path, config=config)
    r = _import_in(guards)
    assert r.returncode != 0
    assert "RepositoryIdentityError" in r.stderr
    assert r.stdout.strip() == ""


def test_every_github_mutation_entry_binds_the_trusted_slug() -> None:
    bound = [
        (cid, entry["github_mutation"]["requires_repo"])
        for cid, entry in policy.CONTROLLED_SKILL_MUTATION_COMMAND_POLICY.items()
        if "requires_repo" in entry.get("github_mutation", {})
    ]
    assert len(bound) == 8, bound
    assert all(slug == TRUSTED for _, slug in bound), bound
    assert LEGACY not in json.dumps(policy.CONTROLLED_SKILL_MUTATION_COMMAND_POLICY)


def _valid_requests() -> dict:
    """One fully valid request per validator (shapes taken from the policy's exact key sets)."""
    return {
        "comment": (
            policy.validate_isolation_issue_comment_request,
            {
                "schema": policy.ISOLATION_ISSUE_COMMENT_REQUEST_SCHEMA,
                "issue_number": 42,
                "repo": TRUSTED,
                "comment_body": "hello <!-- m -->",
                "marker": "<!-- m -->",
            },
        ),
        "dependency_remove": (
            policy.validate_issue_dependency_remove_input,
            {
                "schema": policy.ISSUE_DEPENDENCY_REMOVE_INPUT_SCHEMA,
                "issue_number": 42,
                "repo": TRUSTED,
                "target_blocker_number": 7,
                "expected_blocked_issue_node_id": "ISSUE_NODE_A",
                "expected_blocker_node_id": "ISSUE_NODE_B",
                "expected_blocked_by_numbers": [7],
                "expected_pre_mutation_snapshot_sha256": "sha256:" + "a" * 64,
                "idempotency_key": f"{TRUSTED}:42:7:abc",
            },
        ),
        "relationship_update": (
            policy.validate_issue_relationship_update_input,
            {
                "schema": policy.ISSUE_RELATIONSHIP_UPDATE_INPUT_SCHEMA,
                "issue_number": 42,
                "repo": TRUSTED,
                "expected_before": {"parent": None, "blocked_by": [], "blocking": []},
                "parent": {"action": "set", "issue_number": 7},
                "add_blocked_by": [],
                "remove_blocked_by": [],
                "add_blocking": [],
                "remove_blocking": [],
                "idempotency_key": f"{TRUSTED}:42:relationship:abc",
            },
        ),
    }


@pytest.mark.parametrize("name", ["comment", "dependency_remove", "relationship_update"])
def test_request_validators_bind_repo_positive_and_negative(name: str) -> None:
    validator, valid = _valid_requests()[name]
    # Positive control: the otherwise-valid request passes against the trusted slug...
    assert validator(valid, 42, TRUSTED) == ""
    # ...and fails with the repo-specific reason for every non-trusted slug (not for some other
    # incidental defect), in either direction of the cross-check.
    for bad in (LEGACY, "", "not-a-slug", TRUSTED + "x", TRUSTED.upper()):
        err = validator({**valid, "repo": bad}, 42, TRUSTED)
        assert "repo_mismatch" in err, (name, bad, err)
        assert "repo_mismatch" in validator(valid, 42, bad), (name, bad)


def test_executor_argv_gate_is_not_widened() -> None:
    base = (
        "python3 scripts/agent-guards/controlled_skill_mutation_exec.py "
        f"--command-id issue_comment.publish --issue-number 42 --repo {TRUSTED}"
    )
    ok_tail = " --input-file artifacts/42/issue-metadata/in.json"
    assert policy.is_controlled_skill_mutation_exec_command(base + ok_tail, str(ROOT))  # control
    for bad_suffix in ("; echo pwned", " | cat", " --unknown-flag x", f" --repo={LEGACY}"):
        assert not policy.is_controlled_skill_mutation_exec_command(
            base + ok_tail + bad_suffix, str(ROOT)
        )
    # The executor itself is not migrated yet (Stage 2-1b): resolution is path-only.
    assert policy.EXECUTOR_SCRIPT == "scripts/agent-guards/controlled_skill_mutation_exec.py"
    assert not (GUARDS / "controlled_skill_mutation_exec.py").exists()


def test_manifest_pins_files_and_honest_deferrals() -> None:
    m = json.loads(MANIFEST.read_text())
    assert m["source"]["commit"] == "6fa07b6a0ab932a62f08db6a8d521c0dfded2075"
    for rel in m["required_files"]:
        assert (ROOT / rel).is_file(), rel
    assert set(m["test_files"]) == set(m["expected_collect"]) == set(m["expected_ran"])
    assert m["deferred_tests"] == []
    refs = {d["script"]: d["owner_stage"] for d in m["deferred_script_refs"]}
    assert set(refs) == {
        "controlled_skill_mutation_exec.py",
        "ensure_contract_snapshot.py",
        "materialize_issue_scope_snapshot.py",
    }
    assert refs["ensure_contract_snapshot.py"].startswith("I-4")
    stage1a = json.loads((ROOT / "docs/dev/stage1a-core-manifest.json").read_text())
    listed = {d["script"] for d in stage1a["import_closure"]["deferred_script_refs"]}
    assert set(refs) <= listed
    assert "docs/dev/stage2-1a-policy-manifest.json" in stage1a["later_stage_test_manifests"]
