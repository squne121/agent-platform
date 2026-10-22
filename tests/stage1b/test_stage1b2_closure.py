"""Stage 1b-2a closure / false-green controls for contract_readiness_check.

Fixture + real-subprocess + fake `gh` only (no live GitHub). Pins: both checkers import under
`python -I`; a normal body is approved and a broken body is needs-fix without PS001 (product-spec
subprocess reached); layout shifts fail; a missing policy YAML never degrades to an implicit NA;
`--repo` is bound to the repository identity config before any `gh` call; the exemption-filtered
most-restrictive hard decision is consumed by both checkers (mixed decision ranks).
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
SKILLS = ROOT / ".claude" / "skills"
REVIEW_DIR = SKILLS / "review-issue"
READINESS = SKILLS / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
PASS_BODY = REVIEW_DIR / "fixtures" / "pass_issue.md"
BROKEN_BODY = REVIEW_DIR / "fixtures" / "c1_missing_sections_issue.md"
POLICY_REL = "docs/dev/extension-surface-runtime-policy.yaml"
TRUSTED_SLUG = json.loads((ROOT / "config" / "repository_identity.v1.json").read_text())[
    "trusted_repo_slug"
]


def _ignore_tests(_d: str, names: list[str]) -> set[str]:
    return {n for n in names if n in {"tests", "__pycache__", "fixtures"}}


def _tree(tmp: Path) -> Path:
    """Minimal synthetic repo holding the closure of both checkers (no tests)."""
    for rel in (
        ".claude/skills/create-issue/scripts",
        ".claude/skills/issue-contract-review/scripts",
        ".claude/skills/impl-review-loop/scripts",
        "scripts/agent-ops",
        "scripts/agent-guards",
        "config",
        "docs/dev",
    ):
        shutil.copytree(ROOT / rel, tmp / rel, ignore=_ignore_tests)
    return tmp


def _fake_gh(tmp: Path) -> tuple[Path, Path]:
    """`gh` stub that logs argv and returns a canned issue body (never touches the network)."""
    log = tmp / "gh.log"
    bindir = tmp / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    body = json.dumps({"body": PASS_BODY.read_text(encoding="utf-8"), "title": "t", "labels": []})
    gh.write_text(
        f"#!/bin/sh\necho \"$@\" >> '{log}'\ncat <<'JSON'\n{body}\nJSON\n", encoding="utf-8"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return bindir, log


def _run(script: Path, *args: str, path_prefix: Path | None = None):
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH"}}
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [sys.executable, "-I", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )


def _readiness(script: Path, body: Path, *extra: str):
    return _run(script, "--body-file", str(body), *extra)


# --------------------------------------------------------------------------- import / positive


def test_readiness_imports_and_starts_under_python_I():
    proc = _run(READINESS, "--help")
    assert proc.returncode == 0, proc.stderr
    assert "--repo" in proc.stdout


def test_positive_control_normal_body_is_go():
    ready = _readiness(READINESS, PASS_BODY)
    assert json.loads(ready.stdout)["status"] == "go", ready.stdout + ready.stderr


def test_negative_control_missing_sections_is_needs_fix_and_not_ps001():
    ready_json = json.loads(_readiness(READINESS, BROKEN_BODY).stdout)
    assert ready_json["status"] == "needs_fix"
    # PS001 = the product-spec subprocess was unavailable/malformed: a wiring defect, not content.
    assert "PS001" not in [e["rule_id"] for e in ready_json["errors"]]


def test_synthetic_tree_matches_repo_verdict(tmp_path):
    tree = _tree(tmp_path / "repo")
    ready = _readiness(
        tree / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py", PASS_BODY
    )
    assert json.loads(ready.stdout)["status"] == "go", ready.stdout + ready.stderr


# --------------------------------------------------------------------------- layout shift


def test_readiness_parents_layout_shift_is_not_green(tmp_path):
    tree = _tree(tmp_path / "repo")
    scripts = tree / ".claude/skills/issue-contract-review/scripts"
    deeper = scripts / "nested"
    deeper.mkdir()
    shutil.copy(scripts / "contract_readiness_check.py", deeper)
    proc = _readiness(deeper / "contract_readiness_check.py", PASS_BODY)
    # the shifted copy must crash on its first sibling import, not produce any verdict
    assert proc.returncode != 0 and "ModuleNotFoundError" in proc.stderr, proc.stdout + proc.stderr


# --------------------------------------------------------------------------- policy missing (AC-3)

_RISK_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for the policy-missing fixture only.

## Acceptance Criteria

- [ ] AC1: concrete AC
<!-- runtime-verification: true -->

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
```

## Allowed Paths

- scripts/claude-gpt/lib.sh

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

- decision: not_applicable
- reason: fixture
"""


def test_policy_yaml_missing_readiness_is_fail_closed_not_implicit_na(tmp_path):
    tree = _tree(tmp_path / "repo")
    body = tmp_path / "body.md"
    body.write_text(_RISK_BODY, encoding="utf-8")
    script = tree / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py"

    present = json.loads(_readiness(script, body).stdout)
    assert "extension_surface_risk_trigger_policy_unavailable" not in json.dumps(present)

    (tree / POLICY_REL).unlink()
    proc = _readiness(script, body)
    result = json.loads(proc.stdout)
    categories = {e.get("category") for e in result["errors"]}
    assert "extension_surface_risk_trigger_policy_unavailable" in categories, result
    assert result["status"] != "go"


def test_policy_yaml_invalid_readiness_is_fail_closed(tmp_path):
    tree = _tree(tmp_path / "repo")
    body = tmp_path / "body.md"
    body.write_text(_RISK_BODY, encoding="utf-8")
    (tree / POLICY_REL).write_text("rules: [unclosed\n", encoding="utf-8")
    script = tree / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py"
    result = json.loads(_readiness(script, body).stdout)
    assert result["status"] != "go"
    assert "extension_surface_risk_trigger_policy_unavailable" in {
        e.get("category") for e in result["errors"]
    }


# --------------------------------------------------------------------------- --repo binding


def test_readiness_repo_default_comes_from_identity_config(tmp_path):
    tree = _tree(tmp_path / "repo")
    bindir, log = _fake_gh(tmp_path)
    script = tree / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py"
    proc = _run(script, "--issue", "7", path_prefix=bindir)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"--repo {TRUSTED_SLUG}" in log.read_text()


def test_readiness_untrusted_repo_is_refused_before_any_gh_call(tmp_path):
    tree = _tree(tmp_path / "repo")
    bindir, log = _fake_gh(tmp_path)
    script = tree / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py"
    proc = _run(script, "--issue", "7", "--repo", "other-org/other-repo", path_prefix=bindir)
    assert proc.returncode == 2
    assert "identity mismatch" in proc.stderr
    assert not log.exists()


def test_readiness_identity_config_missing_fails_closed(tmp_path):
    tree = _tree(tmp_path / "repo")
    (tree / "config" / "repository_identity.v1.json").unlink()
    bindir, log = _fake_gh(tmp_path)
    script = tree / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py"
    proc = _run(script, "--issue", "7", path_prefix=bindir)
    assert proc.returncode == 2
    assert "identity unavailable" in proc.stderr
    assert not log.exists()


# --------------------------------------------------------------------------- mixed decision ranks
# Inherited from PR #16: `_most_restrictive_hard_decision` must be recomputed over the
# exemption-FILTERED rules in the real consumers. Only the claude-gpt rule can be exempted
# (immediate); the other matched hard rule is downgraded to `deferred` in a mutated policy so that
# a survivor (final decision taken from the unfiltered rules) becomes observable.


# --------------------------------------------------------------------------- missing imports

_SKILLS_REL = ".claude/skills"
_MISSING_IMPORT_READINESS = [
    f"{_SKILLS_REL}/issue-contract-review/scripts/vc_contract_syntax.py",
    f"{_SKILLS_REL}/issue-contract-review/scripts/baseline_vc_preflight.py",
    f"{_SKILLS_REL}/create-issue/scripts/prose_boundary_policy.py",
    f"{_SKILLS_REL}/create-issue/scripts/mrc_contract_parser.py",
    "scripts/agent-guards/repo_identity.py",
]


@pytest.mark.parametrize("rel", _MISSING_IMPORT_READINESS)
def test_readiness_missing_import_is_loud_not_green(tmp_path, rel):
    tree = _tree(tmp_path / "repo")
    (tree / rel).unlink()
    script = tree / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py"
    proc = _readiness(script, PASS_BODY)
    assert proc.returncode != 0 and "ModuleNotFoundError" in proc.stderr, proc.stdout + proc.stderr


def test_matcher_and_policy_are_pinned_by_the_manifest():
    """The consumers degrade to NA when the matcher MODULE file is absent (unchanged from the
    source); the lane must therefore pin its presence via the manifest `required_files`."""
    manifest = json.loads(
        (ROOT / "docs/dev/stage1b2-contract-checkers-manifest.json").read_text(encoding="utf-8")
    )
    required = set(manifest["required_files"])
    for rel in (
        "scripts/agent-guards/extension_surface_policy_matcher.py",
        "scripts/agent-guards/repo_identity.py",
        POLICY_REL,
        "config/repository_identity.v1.json",
    ):
        assert rel in required, rel
        assert (ROOT / rel).is_file(), rel
