"""Stage 1b-2 closure / false-green controls for check_issue_contract + contract_readiness_check.

Fixture + real-subprocess + fake `gh` only (no live GitHub). Pins: both checkers import under
`python -I`; a normal body is approved and a broken body is needs-fix without PS001 (product-spec
subprocess reached); layout shifts fail; a missing policy YAML never degrades to an implicit NA;
`--repo` is bound to the repository identity config before any `gh` call; the exemption-filtered
most-restrictive hard decision is consumed by both checkers (mixed decision ranks).
"""

from __future__ import annotations

import importlib.util
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
CHECK = REVIEW_DIR / "scripts" / "check_issue_contract.py"
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
        ".claude/skills/review-issue/scripts",
        ".claude/skills/review-issue/schemas",
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


def _check(script: Path, body: Path):
    return _run(script, "--mode", "check", "--file", str(body), "--json")


def _codes(result: dict) -> list[str]:
    return [b.get("code") for b in result.get("blocking_issues", [])]


# --------------------------------------------------------------------------- import / positive


@pytest.mark.parametrize("script", [CHECK, READINESS], ids=["check_issue_contract", "readiness"])
def test_scripts_import_and_start_under_python_I(script):
    proc = _run(script, "--help")
    assert proc.returncode == 0, proc.stderr
    assert "--repo" in proc.stdout


def test_positive_control_normal_body_is_approved_by_both():
    check = _check(CHECK, PASS_BODY)
    ready = _readiness(READINESS, PASS_BODY)
    assert check.returncode == 0, check.stdout + check.stderr
    assert json.loads(check.stdout)["verdict"] == "approve"
    assert json.loads(ready.stdout)["status"] == "go"


def test_negative_control_missing_sections_is_needs_fix_and_not_ps001():
    check = _check(CHECK, BROKEN_BODY)
    ready = _readiness(READINESS, BROKEN_BODY)
    check_json = json.loads(check.stdout)
    ready_json = json.loads(ready.stdout)
    assert check_json["verdict"] == "needs-fix"
    assert ready_json["status"] == "needs_fix"
    # PS001 = the product-spec subprocess was unavailable/malformed: a wiring defect, not content.
    assert "PS001" not in json.dumps(check_json)
    assert "PS001" not in [e["rule_id"] for e in ready_json["errors"]]


def test_synthetic_tree_matches_repo_verdicts(tmp_path):
    tree = _tree(tmp_path / "repo")
    check = _check(tree / ".claude/skills/review-issue/scripts/check_issue_contract.py", PASS_BODY)
    ready = _readiness(
        tree / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py", PASS_BODY
    )
    assert json.loads(check.stdout)["verdict"] == "approve", check.stdout + check.stderr
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


def test_check_issue_contract_layout_shift_is_not_green(tmp_path):
    tree = _tree(tmp_path / "repo")
    scripts = tree / ".claude/skills/review-issue/scripts"
    deeper = scripts / "nested"
    deeper.mkdir()
    shutil.copy(scripts / "check_issue_contract.py", deeper)
    proc = _check(deeper / "check_issue_contract.py", PASS_BODY)
    # sibling skill dirs (create-issue / issue-contract-review / impl-review-loop) are no longer
    # reachable: it must crash loudly rather than turn into an approve.
    assert proc.returncode != 0 and "ModuleNotFoundError" in proc.stderr, proc.stdout + proc.stderr


def test_missing_sibling_product_spec_script_is_not_a_false_green(tmp_path):
    tree = _tree(tmp_path / "repo")
    (tree / ".claude/skills/issue-contract-review/scripts/check_product_spec_contract.py").unlink()
    proc = _check(tree / ".claude/skills/review-issue/scripts/check_issue_contract.py", PASS_BODY)
    result = json.loads(proc.stdout)
    assert result["verdict"] != "approve" or "PS001" in json.dumps(result)


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


def _load_checker(tree: Path):
    path = tree / ".claude/skills/review-issue/scripts/check_issue_contract.py"
    spec = importlib.util.spec_from_file_location("check_issue_contract_tree_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_policy_yaml_missing_check_issue_contract_is_not_pass(tmp_path):
    tree = _tree(tmp_path / "repo")
    (tree / POLICY_REL).unlink()
    checker = _load_checker(tree)
    result, issues = checker.check_c14_extension_surface_risk_trigger(_RISK_BODY, "implementation")
    # By design (unchanged from the source) a policy-unavailable C14 is a visible, non-blocking
    # WARN in check_issue_contract; contract_readiness_check is the strict (human_judgment) side.
    assert result == checker.CheckResult.WARN
    assert any("policy unavailable" in str(i) for i in issues), issues


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


def test_check_issue_contract_untrusted_repo_is_refused_before_any_gh_call(tmp_path):
    tree = _tree(tmp_path / "repo")
    bindir, log = _fake_gh(tmp_path)
    script = tree / ".claude/skills/review-issue/scripts/check_issue_contract.py"
    proc = _run(
        script,
        "--mode",
        "check",
        "--issue",
        "7",
        "--repo",
        "other-org/other-repo",
        "--json",
        path_prefix=bindir,
    )
    assert proc.returncode == 2
    assert "identity mismatch" in proc.stderr
    assert not log.exists()


def test_check_issue_contract_trusted_repo_reaches_gh(tmp_path):
    tree = _tree(tmp_path / "repo")
    bindir, log = _fake_gh(tmp_path)
    script = tree / ".claude/skills/review-issue/scripts/check_issue_contract.py"
    proc = _run(
        script,
        "--mode",
        "check",
        "--issue",
        "7",
        "--repo",
        TRUSTED_SLUG,
        "--json",
        path_prefix=bindir,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"--repo {TRUSTED_SLUG}" in log.read_text()


# --------------------------------------------------------------------------- mixed decision ranks
# Inherited from PR #16: `_most_restrictive_hard_decision` must be recomputed over the
# exemption-FILTERED rules in the real consumers. Only the claude-gpt rule can be exempted
# (immediate); the other matched hard rule is downgraded to `deferred` in a mutated policy so that
# a survivor (final decision taken from the unfiltered rules) becomes observable.

_LIB_SH = "scripts/claude-gpt/lib.sh"
_OTHER_PATH = ".claude/hooks/some_hook.py"
_OTHER_RULE = "hook-lifecycle-matcher-or-handler-change"


_DECLARATION_AC1 = (
    "- executable_semantics_unchanged: {rule: claude-gpt-lifecycle-invocation-change, ac: AC1}"
)
_MIXED_BODY_TEMPLATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "comment-only exemption fixture"
change_kind: workflow
```

## Parent Issue

none

## Outcome

`scripts/claude-gpt/lib.sh` の comment を現行 semantics に合わせて修正する。

## Parent Goal Ref

- Goal: comment-only 変更の readiness false blocker 解消
- Desired Destination: 単独改善

## Current Validated Scope

- `scripts/claude-gpt/lib.sh` の comment のみを変更する

## Remaining Parent Gaps

なし

## In Scope

- `scripts/claude-gpt/lib.sh` の comment 修正

## Out of Scope

- 実行時挙動の変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: `lib.sh` の変更が comment 行のみである
- [ ] AC2: `lib.sh` の shell 構文が壊れていない

## Verification Commands

```bash
{vc_lines}
# AC2
$ bash -n scripts/claude-gpt/lib.sh
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

- decision: not_applicable
- reason: comment のみの変更で実行時挙動は変わらない
{declaration}

## Allowed Paths

{allowed_paths}
"""


def _mixed_body(declared: str) -> str:
    body = _MIXED_BODY_TEMPLATE.format(
        allowed_paths=f"- {_LIB_SH}\n- {_OTHER_PATH}",
        declaration=_DECLARATION_AC1,
        vc_lines=f"# AC1\n$ git diff origin/main -- {_LIB_SH}",
    )
    return body.replace("decision: not_applicable", f"decision: {declared}")


def _patched_matcher(monkeypatch, loader_owner, other_rank: str):
    matcher_path = ROOT / "scripts/agent-guards/extension_surface_policy_matcher.py"
    spec = importlib.util.spec_from_file_location("matcher_mixed_rank_under_test", matcher_path)
    matcher = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = matcher
    spec.loader.exec_module(matcher)
    original = matcher.load_policy

    def mutated(*a, **k):
        policy = original(*a, **k)
        rule = next(r for r in policy["rules"] if r["id"] == _OTHER_RULE)
        rule["default_decision"] = other_rank
        return policy

    monkeypatch.setattr(matcher, "load_policy", mutated)
    monkeypatch.setattr(loader_owner, "_load_extension_surface_policy_matcher", lambda: matcher)


def _readiness_module():
    spec = importlib.util.spec_from_file_location("readiness_mixed_rank_under_test", READINESS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _consumer_findings(kind: str, monkeypatch, body: str, other_rank: str) -> bool:
    """True when the consumer flags the body (needs-fix)."""
    if kind == "review-issue":
        mod = _load_checker(ROOT)
        _patched_matcher(monkeypatch, mod, other_rank)
        res, _ = mod.check_c14_extension_surface_risk_trigger(body, "implementation")
        return res == mod.CheckResult.FAIL
    mod = _readiness_module()
    _patched_matcher(monkeypatch, mod, other_rank)
    return bool(mod.check_extension_surface_risk_trigger(body))


@pytest.mark.parametrize("kind", ["review-issue", "readiness"])
def test_partial_exemption_uses_remaining_rule_rank_positive(kind, monkeypatch):
    """claude-gpt (immediate) exempted; remaining hard rule is `deferred`: declaring `deferred`
    is sufficient. A survivor using the unfiltered `immediate` would wrongly flag this."""
    assert not _consumer_findings(kind, monkeypatch, _mixed_body("deferred"), "deferred")


@pytest.mark.parametrize("kind", ["review-issue", "readiness"])
def test_partial_exemption_still_requires_remaining_rule_rank_negative(kind, monkeypatch):
    """Same body, declared `not_applicable` (< remaining `deferred`) must still be flagged:
    the exemption of one rule must not erase the other hard rule."""
    assert _consumer_findings(kind, monkeypatch, _mixed_body("not_applicable"), "deferred")


@pytest.mark.parametrize("kind", ["review-issue", "readiness"])
def test_partial_exemption_equal_rank_remaining_rule_negative(kind, monkeypatch):
    """Remaining hard rule is `immediate`: declaring `deferred` is insufficient even though the
    claude-gpt rule is exempted."""
    assert _consumer_findings(kind, monkeypatch, _mixed_body("deferred"), "immediate")


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


_MISSING_IMPORT_CHECK = [
    f"{_SKILLS_REL}/create-issue/scripts/prose_boundary_policy.py",
    f"{_SKILLS_REL}/create-issue/scripts/mrc_contract_parser.py",
    f"{_SKILLS_REL}/create-issue/scripts/path_classification.py",
    f"{_SKILLS_REL}/impl-review-loop/scripts/evaluate_product_spec_gate.py",
]


@pytest.mark.parametrize("rel", _MISSING_IMPORT_CHECK)
def test_check_issue_contract_missing_import_is_not_approve(tmp_path, rel):
    # NOTE: vc_contract_syntax.py is NOT listed: check_issue_contract wraps it in
    # try/except ImportError and degrades (unchanged from the source; recorded in the manifest
    # `accepted_degrade`).
    tree = _tree(tmp_path / "repo")
    (tree / rel).unlink()
    script = tree / ".claude/skills/review-issue/scripts/check_issue_contract.py"
    proc = _check(script, PASS_BODY)
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_product_spec_failure_becomes_needs_fix_through_check_issue_contract():
    """Guards `_apply_product_spec_check`: a real product-spec failure (docs/product scope
    without a spec delta) must block, while every non-product-spec check stays green."""
    body = REVIEW_DIR / "fixtures" / "ps_docs_product_without_spec_delta_issue.md"
    proc = _check(CHECK, body)
    result = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert result["verdict"] == "needs-fix"
    assert any("mixed_scope" in str(b) for b in result["blocking_issues"]), result[
        "blocking_issues"
    ]
    # PS001 is the rule id of a genuine product-spec finding here; wiring failures read differently.
    assert "failed closed" not in json.dumps(result)
    assert "import failed" not in json.dumps(result)
    failed = [k for k, v in result["deterministic_checks"].items() if v == "fail"]
    assert failed == [], failed


# --------------------------------------------------------------------------- C14 WARN vs blocking_issues


class _PolicyUnavailableMatcher:
    """Stand-in matcher whose policy cannot be loaded (every shared-evaluator entrypoint raises)."""

    class PolicyLoadError(Exception):
        pass

    @classmethod
    def _boom(cls, *args, **kwargs):
        raise cls.PolicyLoadError("synthetic unusable policy for C14 WARN test")

    evaluate_issue_risk_trigger = _boom
    evaluate_allowed_paths = _boom
    evaluate_runtime_assertion_binding_coverage = _boom


def _c14_findings(result):
    return [
        f
        for f in result.findings
        if f["deterministic_domain_key"] == "extension_surface_risk_trigger"
    ]


def _assert_verdict_blocking_consistent(result):
    """approve/ok must never coexist with blocking issues, and vice versa."""
    if result.verdict == "approve":
        assert result.status == "ok"
        assert result.blocking_issues == []
        assert not result.structured_blockers
    else:
        assert result.verdict == "needs-fix"
        assert result.blocking_issues or result.structured_blockers


def test_c14_policy_unavailable_warn_is_non_blocking_and_keeps_diagnostics(monkeypatch):
    mod = _load_checker(ROOT)
    body = PASS_BODY.read_text(encoding="utf-8")
    monkeypatch.setattr(
        mod, "_load_extension_surface_policy_matcher", lambda: _PolicyUnavailableMatcher
    )

    result = mod.run_checks(body)

    assert result.deterministic_checks.C14_extension_surface_risk_trigger == mod.CheckResult.WARN
    assert result.verdict == "approve"
    assert result.blocking_issues == []
    _assert_verdict_blocking_consistent(result)
    # diagnostics survive as a non-blocking finding (not dropped, not blocking)
    findings = _c14_findings(result)
    assert len(findings) == 1
    assert findings[0]["blocking"] is False
    assert "policy unavailable" in findings[0]["message"]
    assert "synthetic unusable policy" in findings[0]["message"]


def test_c14_valid_policy_pass_body_has_no_c14_blocker():
    mod = _load_checker(ROOT)
    result = mod.run_checks(PASS_BODY.read_text(encoding="utf-8"))

    assert result.deterministic_checks.C14_extension_surface_risk_trigger != mod.CheckResult.WARN
    assert result.verdict == "approve"
    assert _c14_findings(result) == []
    _assert_verdict_blocking_consistent(result)


def test_c14_fail_still_blocks_with_matching_blocking_issues(monkeypatch):
    """Negative control: only FAIL blocks. Guards against over-correcting WARN handling."""
    mod = _load_checker(ROOT)
    _patched_matcher(monkeypatch, mod, "immediate")

    result = mod.run_checks(_mixed_body("deferred"))

    assert result.deterministic_checks.C14_extension_surface_risk_trigger == mod.CheckResult.FAIL
    assert result.verdict == "needs-fix"
    findings = _c14_findings(result)
    assert findings and all(f["blocking"] is True for f in findings)
    assert [f["message"] for f in findings] == [
        m for m in result.blocking_issues if m in {f["message"] for f in findings}
    ]
    _assert_verdict_blocking_consistent(result)
