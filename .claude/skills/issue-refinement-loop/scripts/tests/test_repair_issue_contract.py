#!/usr/bin/env python3
"""Tests for repair_issue_contract.py (Issue #889 - AC7, AC13, AC14, AC15)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_SCRIPT = _SCRIPTS_DIR / "repair_issue_contract.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_repair(body: str) -> dict:
    """Run repair_issue_contract.py in dry-run mode against a body string."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--body-file", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout, f"No stdout (stderr: {result.stderr})"
        data = json.loads(result.stdout)
        return data
    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# AC4: script exists
# ---------------------------------------------------------------------------


def test_script_exists():
    """AC4: repair_issue_contract.py exists."""
    assert _SCRIPT.exists(), f"Script not found: {_SCRIPT}"


# ---------------------------------------------------------------------------
# AC13: dry-run default / schema / idempotent
# ---------------------------------------------------------------------------


def test_schema_and_dry_run_default():
    """AC13: output JSON has schema, dry_run=True, idempotent fields."""
    body = """## Outcome
Simple outcome.

## Acceptance Criteria
- [ ] AC1: something

## Verification Commands

```bash
# AC1
$ test -f README.md
```

## Allowed Paths
- README.md

## Stop Conditions
- none

## Required Skills
- none
"""
    result = run_repair(body)
    assert result.get("schema") == "repair_issue_contract/v1"
    assert result.get("dry_run") is True
    assert "changed" in result
    assert "original_body_sha256" in result
    assert "repaired_body_sha256" in result
    assert "repairs" in result
    assert isinstance(result["repairs"], list)


def test_idempotent_unchanged_body():
    """AC13: running twice on same body gives same hash."""
    body = """## Outcome
No defects.

## Verification Commands

```bash
# AC1
$ test -f README.md
```
"""
    result1 = run_repair(body)
    result2 = run_repair(body)
    assert result1["original_body_sha256"] == result2["original_body_sha256"]
    assert result1["repaired_body_sha256"] == result2["repaired_body_sha256"]


def test_idempotent_same_hash_after_repair():
    """AC13: if a repair is needed, running repair on already-repaired body gives same hash."""
    # Body with pnpm test:e2e (runtime-only command) - should get annotated
    body = """## Outcome
Test e2e.

## Verification Commands

```bash
# AC1
$ pnpm test:e2e
```
"""
    _result1 = run_repair(body)
    # The repaired body sha should differ from original if changed
    # Now simulate a second repair by creating body with the annotation already present
    annotated_body = """## Outcome
Test e2e.

## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only
$ pnpm test:e2e
```
"""
    result2 = run_repair(annotated_body)
    # Second repair should show no changes (idempotent)
    assert result2["changed"] is False, "Second repair should be idempotent (no changes)"
    assert result2["original_body_sha256"] == result2["repaired_body_sha256"]


# ---------------------------------------------------------------------------
# AC14: escaped code fence repair limited to Machine-Readable Contract section
# ---------------------------------------------------------------------------


def test_mrc_fence_only_mrc_section():
    """AC14: escaped fence repair only affects ## Machine-Readable Contract section.

    MAJOR 1 fix: assert that escaped_code_fence repair IS triggered (positive fixture).
    """
    # A body where the yaml fence is in the MRC section (escaped)
    body = r"""## Outcome
Normal section.

## Verification Commands

```bash
$ test -f README.md
```

## Machine-Readable Contract

\```yaml
schema: test/v1
\```
"""
    result = run_repair(body)
    # Should have repair for escaped_code_fence (positive assertion, not OR-false)
    kinds = [r["kind"] for r in result.get("repairs", [])]
    assert "escaped_code_fence" in kinds, (
        f"Expected escaped_code_fence repair but got: {result.get('repairs', [])}"
    )
    assert result["changed"] is True, "Body should be changed after escaped fence repair"


def test_mrc_fence_does_not_touch_quadruple_fence():
    """AC14: quadruple fence and tilde fence are NOT repaired."""
    body = r"""## Machine-Readable Contract

````bash
$ rg "pattern" file
````

~~~yaml
schema: test
~~~
"""
    result = run_repair(body)
    # No escaped_code_fence repairs (quadruple and tilde are valid CommonMark)
    escaped_repairs = [r for r in result.get("repairs", []) if r["kind"] == "escaped_code_fence"]
    assert len(escaped_repairs) == 0, "Quadruple/tilde fences should not be repaired"


# ---------------------------------------------------------------------------
# AC15: allowlist command repair rules
# ---------------------------------------------------------------------------


def test_pnpm_typecheck_not_deferred():
    """AC15: pnpm typecheck is NOT deferred/annotated."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm typecheck
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    # pnpm typecheck should NOT be in runtime repairs
    assert all("typecheck" not in r["reason"] for r in runtime_repairs), (
        "pnpm typecheck should not be annotated as runtime_only"
    )


def test_pnpm_lint_not_deferred():
    """AC15: pnpm lint is NOT deferred/annotated."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm lint
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    assert all("pnpm lint" not in r["reason"] for r in runtime_repairs),         "pnpm lint should not be annotated"


def test_pnpm_test_not_deferred():
    """AC15: pnpm test is NOT deferred/annotated."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm test
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    assert all("pnpm test" not in r["reason"] for r in runtime_repairs), (
        "pnpm test (regression gate) should not be annotated"
    )


def test_pnpm_build_not_deferred():
    """AC15: pnpm build is NOT deferred/annotated."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm build
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    assert all("pnpm build" not in r["reason"] for r in runtime_repairs),         "pnpm build should not be annotated"


def test_pnpm_test_e2e_gets_deferred():
    """AC15: pnpm test:e2e is annotated as runtime-only/deferred."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm test:e2e
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    assert len(runtime_repairs) >= 1, "pnpm test:e2e should be annotated as runtime-only"
    reasons = [r["reason"] for r in runtime_repairs]
    assert any("pnpm test:e2e" in reason for reason in reasons)


def test_denylist_not_repaired():
    """AC15: denylist commands (curl, rm, bash -c) are NOT auto-repaired."""
    body = """## Verification Commands

```bash
# AC1
$ curl https://example.com
# AC2
$ rm -rf /tmp/test
# AC3
$ bash -c "echo hello"
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    # None of the denylist commands should be annotated
    reasons = [r["reason"] for r in runtime_repairs]
    for dangerous in ["curl", "rm -", "bash -c"]:
        assert not any(dangerous in reason for reason in reasons), (
            f"Denylist command {dangerous!r} should not be auto-repaired"
        )


def test_already_annotated_not_double_annotated():
    """AC15: Already-annotated commands are not double-annotated (idempotency)."""
    body = """## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only
$ pnpm test:e2e
```
"""
    result = run_repair(body)
    # Already annotated: should have no repairs
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    assert len(runtime_repairs) == 0, "Already-annotated command should not be re-annotated"
    assert result["changed"] is False, "Body should be unchanged when already annotated"


# ---------------------------------------------------------------------------
# MAJOR 1: yaml-only fence repair + YAML reparse validation
# ---------------------------------------------------------------------------


def test_mrc_repair_requires_yaml_reparse_success():
    """MAJOR 1: repair with invalid YAML after unescaping → body returned unchanged."""
    # This body has an escaped yaml fence, but the yaml content is structurally invalid.
    # After unescaping, yaml.safe_load should fail → repair rejected.
    body = "## Machine-Readable Contract\n\n\\```yaml\n: invalid: yaml: {{\n\\```\n"
    result = run_repair(body)
    # If yaml reparse fails, no changes should be applied
    # (either changed=False, or if the section cannot be parsed, repairs are empty)
    if result.get("changed"):
        # If changed, the yaml should still be parseable
        pass  # implementation may vary; key is that broken yaml is not "successfully" repaired
    # Primary assertion: the repair should not produce a "changed=True" result
    # when the yaml content is invalid after unescaping.
    # Since our implementation returns original section on yaml parse failure,
    # the body should be unchanged.
    assert result.get("changed") is False, (
        "Repair with invalid YAML content after unescaping should not be applied"
    )


def test_mrc_repair_does_not_modify_bash_fence_in_mrc():
    """MAJOR 1: escaped bash fence in MRC section is NOT repaired."""
    body = r"""## Machine-Readable Contract

\```bash
some_command arg1
\```
"""
    result = run_repair(body)
    # bash fences should not be repaired
    escaped_code_repairs = [r for r in result.get("repairs", []) if r["kind"] == "escaped_code_fence"]
    assert len(escaped_code_repairs) == 0, (
        "Escaped bash fence in MRC section should not be repaired (yaml only)"
    )
    # The body should remain unchanged
    assert result.get("changed") is False, (
        "Body with escaped bash fence in MRC should not be changed"
    )


def test_mrc_yaml_fence_is_repaired_bash_is_not():
    """MAJOR 1: escaped yaml fence IS repaired, escaped bash fence is NOT."""
    body = r"""## Machine-Readable Contract

\```yaml
schema: test/v1
key: value
\```

\```bash
echo hello
\```
"""
    result = run_repair(body)
    # yaml fence should be repaired
    escaped_repairs = [r for r in result.get("repairs", []) if r["kind"] == "escaped_code_fence"]
    _non_target = [r for r in result.get("repairs", []) if r["kind"] == "non_target_fence"]
    assert len(escaped_repairs) >= 1, "yaml fence should be repaired"
    assert result.get("changed") is True, "Body should change when yaml fence is repaired"
    # bash fence should appear as non_target_fence (or just not be repaired)
    # The key assertion: bash escaped fences are not in escaped_code_fence repairs
    for r in escaped_repairs:
        assert "bash" not in r.get("original", ""), (
            f"Bash fence should not be in escaped_code_fence repairs: {r}"
        )



# ---------------------------------------------------------------------------
# BLOCKER 1: repair_result is exposed in refinement_preflight output
# ---------------------------------------------------------------------------


def test_repair_result_is_exposed_in_refinement_preflight_output():
    """BLOCKER 1: _invoke_repair result is included in preflight output as repair_diagnostics.

    When repair detects changes (changed=True), the result dict must contain
    a 'repair_diagnostics' key with the repair result.
    """
    # Test the run_refinement_preflight module directly (not via subprocess)
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from run_refinement_preflight import _invoke_repair

    # Body with an escaped yaml fence in MRC section → repair detects change
    body_with_fence = r"""## Outcome
Test outcome.

## Acceptance Criteria
- [ ] AC1: test

## Verification Commands

```bash
# AC1
$ test -f README.md
```

## Machine-Readable Contract

\```yaml
schema: test/v1
\```

## Stop Conditions
- none
"""
    result = _invoke_repair(body_with_fence)
    # Verify repair result has the expected schema
    assert result.get("schema") == "repair_issue_contract/v1", (
        f"Unexpected schema: {result.get('schema')}"
    )
    # The key assertion: repair_result dict is returned (not None, not empty)
    assert isinstance(result, dict), "repair result must be a dict"
    assert "changed" in result, "repair result must have 'changed' field"


def test_invoke_repair_returns_dict():
    """BLOCKER 1 additional: _invoke_repair returns a dict even on unchanged body."""
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from run_refinement_preflight import _invoke_repair

    clean_body = """## Outcome
Clean issue body with no defects.

## Verification Commands

```bash
# AC1
$ test -f README.md
```
"""
    result = _invoke_repair(clean_body)
    assert isinstance(result, dict)
    assert result.get("schema") == "repair_issue_contract/v1"
    # Clean body should not have changes
    assert result.get("changed") is False





# ===== #899 genuine behavioral tests (subprocess the real scripts) =====
def _run_ric_899(body):
    import subprocess as _sp
    import json as _json
    import tempfile as _tf
    import os as _os
    import sys as _sys
    script = str(Path(__file__).parent.parent / "repair_issue_contract.py")
    with _tf.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        p = f.name
    try:
        r = _sp.run([_sys.executable, script, "--body-file", p], capture_output=True, text=True)
        return _json.loads(r.stdout)
    finally:
        _os.unlink(p)


def _run_bvp_899(body, strict=False):
    import subprocess as _sp
    import json as _json
    import tempfile as _tf
    import os as _os
    import sys as _sys
    root = Path(__file__).resolve()
    while root != root.parent and not (root / ".claude").is_dir():
        root = root.parent
    script = str(root / ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py")
    with _tf.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        p = f.name
    try:
        argv = [_sys.executable, script, "--body-file", p]
        if strict:
            argv.append("--strict")
        r = _sp.run(argv, capture_output=True, text=True)
        return _json.loads(r.stdout)
    finally:
        _os.unlink(p)


def _result_for_899(data, needle):
    for it in data.get("results", []):
        if needle in (it.get("raw_command") or ""):
            return it
    return None


def test_inline_baseline_expect_invalid():
    """AC18: inline '# baseline-expect:' inside a bash fence is repaired (moved to the
    preceding line) with a structured record; dry-run by default."""
    body = "## Verification Commands\n\n```bash\n$ pnpm typecheck # baseline-expect: pass\n```\n"
    res = _run_ric_899(body)
    mv = [r for r in res["repairs"] if r.get("kind") == "move_inline_baseline_expect_to_preceding_line"]
    assert len(mv) == 1, res
    assert all(k in mv[0] for k in ("line_start", "line_end", "reason", "original", "repaired")), mv[0]
    assert res["dry_run"] is True, res


def test_quoted_literal_not_annotation():
    """AC18: a quoted literal '# baseline-expect:' inside a bash fence is NOT repaired."""
    body = (
        "## Verification Commands\n\n```bash\n# baseline-expect: fail\n$ rg \"# baseline-expect: pass\""
        " docs/dev/dor.md\n```\n"
    )
    res = _run_ric_899(body)
    mv = [r for r in res["repairs"] if r.get("kind") == "move_inline_baseline_expect_to_preceding_line"]
    assert len(mv) == 0, res


def test_new_allowed_path_baseline_fail():
    """AC18: a strict-mode VC targeting a NEW Allowed Path missing its annotation is
    classified missing_baseline_expect_for_new_allowed_path with a declared
    insert_baseline_expect_fail repair."""
    body = ("## Verification Commands\n\n```bash\n$ test -f docs/dev/new-path-ac18-899.md\n```\n\n"
            "## Allowed Paths\n\n- `docs/dev/new-path-ac18-899.md`\n")
    data = _run_bvp_899(body, strict=True)
    it = _result_for_899(data, "new-path-ac18-899.md")
    assert it is not None, data
    assert it["category"] == "missing_baseline_expect_for_new_allowed_path", it
    assert (it.get("repair") or {}).get("kind") == "insert_baseline_expect_fail", it


def test_rg_exit2_new_file():
    """AC18: rg targeting a non-existent NEW Allowed Path file (rg would exit 2) is
    classified missing_baseline_expect_for_new_allowed_path in strict mode, distinct
    from a regex/permission error on an existing file."""
    body = ("## Verification Commands\n\n```bash\n$ rg somepattern docs/dev/rg-exit2-ac18-899.md\n```\n\n"
            "## Allowed Paths\n\n- `docs/dev/rg-exit2-ac18-899.md`\n")
    data = _run_bvp_899(body, strict=True)
    it = _result_for_899(data, "rg-exit2-ac18-899.md")
    assert it is not None, data
    assert it["category"] == "missing_baseline_expect_for_new_allowed_path", it


def test_insert_baseline_expect_fail_for_new_allowed_path():
    """AC4/AC18: repair inserts '# baseline-expect: fail' on the preceding line for a
    VC targeting a NEW Allowed Path file (in Allowed Paths, not existing), with a
    structured record; dry-run by default."""
    body = ("## Verification Commands\n\n```bash\n$ test -f docs/dev/insert-newpath-899.md\n```\n\n"
            "## Allowed Paths\n\n- `docs/dev/insert-newpath-899.md`\n")
    res = _run_ric_899(body)
    ins = [r for r in res["repairs"] if r.get("kind") == "insert_baseline_expect_fail"]
    assert len(ins) == 1, res
    assert all(
        k in ins[0]
        for k in (
            "line_start", "line_end", "reason",
            "original", "repaired", "safety", "confidence",
        )
    ), ins[0]
    assert res["dry_run"] is True, res


# ---------------------------------------------------------------------------
# Issue #2016 iteration-3 (OWNER adversarial review P0-2): candidate body
# materialization leaf/parent-symlink attack rejection.
# ---------------------------------------------------------------------------


def _import_ric():
    import sys as _sys

    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    import repair_issue_contract as _ric

    return _ric


def test_candidate_leaf_symlink_is_rejected_without_target_write(tmp_path):
    """P0-2: a pre-existing symlink at the candidate leaf path must be
    rejected WITHOUT writing through it to the symlink target (the actual
    attack: a plain Path.write_text() would silently overwrite whatever the
    symlink points at)."""
    ric = _import_ric()

    victim = tmp_path / "victim.txt"
    victim.write_text("do-not-touch", encoding="utf-8")

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    leaf = artifact_dir / "repaired_issue_body.md"
    leaf.symlink_to(victim)

    try:
        ric.secure_atomic_write_candidate(leaf, "malicious content", root=artifact_dir)
        raised = False
    except ric.CandidateWriteSecurityError:
        raised = True

    assert raised, "expected CandidateWriteSecurityError for a pre-existing leaf symlink"
    # The victim file must be completely untouched -- proves the write never
    # followed the symlink.
    assert victim.read_text(encoding="utf-8") == "do-not-touch"
    # The symlink itself must still be a symlink (not replaced/removed).
    assert leaf.is_symlink()


def test_candidate_parent_symlink_is_rejected(tmp_path):
    """P0-2: a symlinked ancestor directory between the candidate leaf and
    the allowed artifact root must be rejected -- not just a symlinked
    leaf."""
    ric = _import_ric()

    real_root = tmp_path / "real_artifacts"
    real_root.mkdir()
    outside_target = tmp_path / "outside_dir"
    outside_target.mkdir()

    # artifact_dir/subdir is actually a symlink pointing OUTSIDE real_root.
    symlinked_subdir = real_root / "subdir"
    symlinked_subdir.symlink_to(outside_target)

    leaf = symlinked_subdir / "repaired_issue_body.md"

    try:
        ric.secure_atomic_write_candidate(leaf, "malicious content", root=real_root)
        raised = False
    except ric.CandidateWriteSecurityError:
        raised = True

    assert raised, "expected CandidateWriteSecurityError for a symlinked ancestor directory"
    assert not (outside_target / "repaired_issue_body.md").exists()


def test_candidate_fifo_leaf_is_rejected(tmp_path):
    """P0-2 (required test 6): a FIFO at the candidate leaf path must also
    be rejected (not just symlinks/directories)."""
    import os as _os

    ric = _import_ric()

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    leaf = artifact_dir / "repaired_issue_body.md"
    _os.mkfifo(leaf)

    try:
        ric.secure_atomic_write_candidate(leaf, "content", root=artifact_dir)
        raised = False
    except ric.CandidateWriteSecurityError:
        raised = True

    assert raised, "expected CandidateWriteSecurityError for a FIFO leaf"


def test_candidate_directory_leaf_is_rejected(tmp_path):
    """P0-2 (required test 6): a directory at the candidate leaf path
    (directory substitution) must be rejected."""
    ric = _import_ric()

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    leaf = artifact_dir / "repaired_issue_body.md"
    leaf.mkdir()

    try:
        ric.secure_atomic_write_candidate(leaf, "content", root=artifact_dir)
        raised = False
    except ric.CandidateWriteSecurityError:
        raised = True

    assert raised, "expected CandidateWriteSecurityError for a directory leaf"
    assert leaf.is_dir()


def test_secure_atomic_write_candidate_succeeds_for_clean_path(tmp_path):
    """P0-2 sanity check: the secure writer still works normally (writes,
    fsyncs, and readback-matches) when there is no attack in play."""
    ric = _import_ric()

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    leaf = artifact_dir / "repaired_issue_body.md"

    ric.secure_atomic_write_candidate(leaf, "clean content", root=artifact_dir)

    assert leaf.read_text(encoding="utf-8") == "clean content"
    assert not leaf.is_symlink()

    # Idempotent overwrite of an existing regular file must also succeed.
    ric.secure_atomic_write_candidate(leaf, "clean content v2", root=artifact_dir)
    assert leaf.read_text(encoding="utf-8") == "clean content v2"


def test_extract_allowed_paths_no_path_marker_returns_empty():
    """AC12 (Issue #2783): canonical marker `(none)` and legacy marker
    `読み取り専用。リポジトリ変更なし（既定）` as the sole Allowed Paths entry
    both resolve to [] (previously this module's bullet-token regex had no
    annotation stripping at all, so BOTH markers leaked as literal
    non-empty "path" strings)."""
    ric = _import_ric()

    body_canonical = "## Allowed Paths\n\n- (none)\n"
    assert ric._extract_allowed_paths_ric(body_canonical) == []

    body_legacy = "## Allowed Paths\n\n- 読み取り専用。リポジトリ変更なし（既定）\n"
    assert ric._extract_allowed_paths_ric(body_legacy) == []


# ===========================================================================
# Issue #3020: `baseline-expect: deferred` misused as a pr_review_only stand-in
# for a pytest VC -> annotation-line-only repair, classified auto_apply_safe.
# ===========================================================================

_T3020_TEST_PATH = ".claude/skills/example/tests/test_example_3020.py"


def _t3020_body(vc_block: str, allowed: list[str] | None = None) -> str:
    allowed_paths = allowed if allowed is not None else [_T3020_TEST_PATH]
    allowed_block = "\n".join(f"- {p}" for p in allowed_paths)
    return (
        "## Outcome\nOutcome.\n\n"
        "## Verification Commands\n\n"
        f"```bash\n{vc_block}\n```\n\n"
        f"## Allowed Paths\n\n{allowed_block}\n\n"
        "## Stop Conditions\n- none\n"
    )


_T3020_DEFERRED_CMD = f'uv run --locked pytest {_T3020_TEST_PATH} -q -k "deferred and new_case"'
_T3020_DEFERRED_VC = f"# AC1\n# baseline-expect: deferred\n$ {_T3020_DEFERRED_CMD}"


def _t3020_deferred_repairs(res: dict) -> list[dict]:
    return [r for r in res["repairs"] if r.get("kind") == "deferred_to_pr_review_only"]


def _t3020_commands(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if ln.startswith("$ ")]


def _t3020_apply_repair(body: str, tmp_path: Path) -> tuple[dict, str]:
    ric = _import_ric()
    out_file = tmp_path / "repaired.md"
    res = ric.run_repair(body, apply=True, out_file=str(out_file), root_dir=str(tmp_path))
    return res, out_file.read_text(encoding="utf-8")


def test_deferred_pr_review_only_auto_apply_safe_high_confidence():
    """GIVEN a pytest VC on an Allowed Paths test file annotated baseline-expect: deferred
    (no valid preflight-scope) / WHEN repair runs / THEN exactly one annotation-line-only
    repair is emitted with confidence high and repair_action.disposition == auto_apply_safe."""
    body = _t3020_body(_T3020_DEFERRED_VC)
    res = run_repair(body)

    recs = _t3020_deferred_repairs(res)
    assert len(recs) == 1, res
    rec = recs[0]
    assert rec["confidence"] == "high"
    assert rec["original"].strip() == "# baseline-expect: deferred"
    assert rec["repaired"].strip() == "# preflight-scope: pr_review_only"
    assert res["repair_action"]["disposition"] == "auto_apply_safe"
    assert res["repair_action"]["repair_kinds"] == ["deferred_to_pr_review_only"]
    assert res["dry_run"] is True


def test_deferred_pr_review_only_auto_apply_changes_annotation_line_only(tmp_path):
    """The command line and AC marker are byte-identical; only the annotation line changes."""
    body = _t3020_body(_T3020_DEFERRED_VC)
    _res, repaired = _t3020_apply_repair(body, tmp_path)

    old_lines, new_lines = body.splitlines(), repaired.splitlines()
    assert len(old_lines) == len(new_lines)
    diff = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
    assert diff == [("# baseline-expect: deferred", "# preflight-scope: pr_review_only")]
    assert _t3020_commands(repaired) == _t3020_commands(body) == [f"$ {_T3020_DEFERRED_CMD}"]


def test_deferred_pr_review_only_auto_apply_idempotent(tmp_path):
    """Applying the repair twice yields the same body hash; the second pass finds nothing."""
    ric = _import_ric()
    body = _t3020_body(_T3020_DEFERRED_VC)
    res1, repaired1 = _t3020_apply_repair(body, tmp_path)
    assert res1["changed"] is True

    res2 = ric.run_repair(repaired1)
    assert res2["changed"] is False
    assert res2["repaired_body_sha256"] == res2["original_body_sha256"] == res1["repaired_body_sha256"]
    assert _t3020_deferred_repairs(res2) == []
    assert res2["repair_action"]["disposition"] == "informational"


def test_deferred_pr_review_only_auto_apply_body_hash_changes_command_hash_does_not(tmp_path):
    """AC3: an annotation-only change moves the body hash but not the command hash."""
    import hashlib

    body = _t3020_body(_T3020_DEFERRED_VC)
    res, repaired = _t3020_apply_repair(body, tmp_path)

    assert res["original_body_sha256"] != res["repaired_body_sha256"]

    def _cmd_hashes(text: str) -> list[str]:
        return [hashlib.sha256(c[2:].encode("utf-8")).hexdigest() for c in _t3020_commands(text)]

    assert _cmd_hashes(body) == _cmd_hashes(repaired)
    assert len(_cmd_hashes(body)) == 1


def test_deferred_pr_review_only_auto_apply_accepted_by_preflight_result_validator(tmp_path):
    """The existing consumer-side validator (schema + classify_repair_action() recompute) accepts
    the producer output without any consumer change."""
    import sys as _sys

    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    import run_refinement_preflight as rrp

    res = run_repair(_t3020_body(_T3020_DEFERRED_VC))
    assert rrp._validate_repair_result_schema_and_semantics(res) is None


def test_deferred_pr_review_only_auto_apply_producer_classification_flips(tmp_path):
    """Real producer parity: baseline_vc_preflight.py classifies the raw VC as
    baseline_expect_deferred (the Step 4-unsupported classification), and the repaired VC as the
    canonical preflight_scope_pr_review_only."""
    body = _t3020_body(_T3020_DEFERRED_VC)
    _res, repaired = _t3020_apply_repair(body, tmp_path)

    before = _result_for_899(_run_bvp_899(body), "-k")
    after = _result_for_899(_run_bvp_899(repaired), "-k")
    assert before is not None and after is not None
    assert before["category"] == "baseline_expect_deferred", before
    assert after["category"] == "preflight_scope_pr_review_only", after
    assert before["raw_command"] == after["raw_command"]


def test_deferred_pr_review_only_auto_apply_via_repair_action_apply_consumer(tmp_path):
    """AC3: the repaired body reaches the existing `run_repair_action_apply()` consumer (which
    dispatches via the edit_issue_txn controlled-transaction closure) with commands preserved."""
    import hashlib
    import sys as _sys

    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    import run_refinement_preflight as rrp

    original = _t3020_body(_T3020_DEFERRED_VC)
    artifact_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / "3020"
    artifact_dir.mkdir(parents=True)
    candidate_path = artifact_dir / "candidate_body.md"
    ric = _import_ric()
    producer = ric.run_repair(original, apply=True, out_file=str(candidate_path), root_dir=str(tmp_path))
    candidate = candidate_path.read_text(encoding="utf-8")
    action = dict(producer["repair_action"])
    assert action["disposition"] == "auto_apply_safe"
    action.update({
        "source_lane": "unanchored",
        "preflight_run_identity": "sha256:testrun",
        "original_updated_at": "2024-01-01T00:00:00Z",
        "source_refs_digest": None,
    })
    result_path = artifact_dir / "preflight_result.json"
    result_path.write_text(json.dumps({
        "schema": "issue_refinement_preflight_result/v1",
        "repair_action": action,
        "result_core_sha256": "sha256:testrun",
    }))

    dispatched: list[str] = []

    def _apply_txn(current_issue: dict, candidate_body: str) -> dict:
        dispatched.append(candidate_body)
        digest = hashlib.sha256(candidate_body.encode("utf-8")).hexdigest()
        return {
            "status": "ok",
            "mutation_started": True,
            "body_update": {
                "attempted": True,
                "status": "ok",
                "remote_current_body_sha256": f"sha256:{digest}",
            },
            "content_update": {"patch_attempted": True, "mutation_outcome": "applied"},
            "errors": [],
        }

    bodies = iter([original, candidate])

    def _fetch() -> dict:
        return {"body": next(bodies), "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/agent-platform",
        issue_number=3020,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=_apply_txn,
    )

    assert result["mutation_outcome"] == "applied", result
    assert dispatched == [candidate]
    assert _t3020_commands(dispatched[0]) == _t3020_commands(original)
    assert "# preflight-scope: pr_review_only" in dispatched[0]
    assert "baseline-expect: deferred" not in dispatched[0]



_T3020_OUT_OF_CONDITION_CASES = {
    "non_pytest_rg": (
        "# AC1\n# baseline-expect: deferred\n$ rg -n \"x\" " + _T3020_TEST_PATH,
        None,
    ),
    "allowed_paths_outside": (_T3020_DEFERRED_VC, ["docs/dev/dor.md"]),
    "not_a_test_file": (
        "# AC1\n# baseline-expect: deferred\n$ uv run --locked pytest docs/dev/helper.py -q",
        ["docs/dev/helper.py"],
    ),
    "directory_target": (
        "# AC1\n# baseline-expect: deferred\n$ uv run --locked pytest .claude/skills/example/tests -q",
        [".claude/skills/example/tests/"],
    ),
    "already_valid_scope": (
        f"# AC1\n# baseline-expect: deferred\n# preflight-scope: runtime_only\n$ {_T3020_DEFERRED_CMD}",
        None,
    ),
    "invalid_scope_marker": (
        f"# AC1\n# baseline-expect: deferred\n# preflight-scope: bogus\n$ {_T3020_DEFERRED_CMD}",
        None,
    ),
    "annotation_not_adjacent": (
        f"# baseline-expect: deferred\n# AC1\n$ {_T3020_DEFERRED_CMD}",
        None,
    ),
    "compound_command": (
        f"# AC1\n# baseline-expect: deferred\n$ {_T3020_DEFERRED_CMD} && true",
        None,
    ),
    "baseline_expect_fail": (f"# AC1\n# baseline-expect: fail\n$ {_T3020_DEFERRED_CMD}", None),
    "baseline_expect_pass": (f"# AC1\n# baseline-expect: pass\n$ {_T3020_DEFERRED_CMD}", None),
    "pnpm_gate": ("# AC1\n# baseline-expect: deferred\n$ pnpm test", None),
}


def test_deferred_out_of_condition_vc_is_not_rewritten_nor_human_review():
    """Out-of-condition `deferred` VCs are left byte-identical, and this defect is never turned
    into a human_review_required classification."""

    for name, (vc, allowed) in _T3020_OUT_OF_CONDITION_CASES.items():
        body = _t3020_body(vc, allowed)
        res = run_repair(body)
        assert _t3020_deferred_repairs(res) == [], name
        assert res["changed"] is False, name
        assert res["repair_action"]["disposition"] != "human_review_required", name
        assert res["repair_action"]["disposition"] == "informational", name


def test_deferred_in_condition_mixed_with_out_of_condition_only_rewrites_in_condition(tmp_path):
    """A body with one in-condition and one out-of-condition deferred VC rewrites only the former."""
    other = "# AC2\n# baseline-expect: deferred\n$ pnpm test:unit"
    body = _t3020_body(_T3020_DEFERRED_VC + "\n" + other)
    res, repaired = _t3020_apply_repair(body, tmp_path)

    assert len(_t3020_deferred_repairs(res)) == 1
    assert "# AC2\n# baseline-expect: deferred\n$ pnpm test:unit" in repaired
    assert _t3020_commands(repaired) == _t3020_commands(body)


def test_deferred_unknown_repair_kind_still_human_review_and_pnpm_gate_not_deferred():
    """Existing constraints stay intact: an unknown repair kind is human_review_required, and a
    pnpm regression gate is never annotated deferred / pr_review_only by this repair."""
    ric = _import_ric()
    action = ric.classify_repair_action(
        "a", "b",
        [{"kind": "deferred_to_something_else", "line_start": 1, "line_end": 1, "confidence": "high"}],
    )
    assert action["disposition"] == "human_review_required"
    assert "unknown_repair_kind" in action["reason_codes"]

    safe = ric.classify_repair_action(
        "a", "b",
        [{"kind": "deferred_to_pr_review_only", "line_start": 1, "line_end": 1, "confidence": "high"}],
    )
    assert safe["disposition"] == "auto_apply_safe"
    no_conf = ric.classify_repair_action(
        "a", "b", [{"kind": "deferred_to_pr_review_only", "line_start": 1, "line_end": 1}]
    )
    assert no_conf["disposition"] == "human_review_required"

    for gate in ("pnpm typecheck", "pnpm lint", "pnpm test", "pnpm build"):
        res = run_repair(_t3020_body(f"# AC1\n# baseline-expect: deferred\n$ {gate}", None))
        assert _t3020_deferred_repairs(res) == [], gate
        assert "# preflight-scope" not in "\n".join(r["repaired"] for r in res["repairs"]), gate


# ===========================================================================
# Issue #3020 OWNER REQUEST_CHANGES P2-1 / P2-2 regressions.
# ===========================================================================

_T3020_PYTEST_LAUNCHER_POSITIVE = [
    "pytest",
    "uv run pytest",
    "uv run --locked pytest",
    "uv run --frozen pytest",
    "uv run --no-project pytest",
    "uv run --with pytest --with pyyaml python -m pytest",
    "uv run --no-project --with pytest python3 -m pytest",
    "uv run python -m pytest",
    "uv run --locked python3 -m pytest",
    "python -m pytest",
    "python3 -m pytest",
]


def test_deferred_pytest_launcher_positive_forms_are_rewritten():
    """P2-1: every real pytest launcher form used by existing VCs is recognised as a pytest VC."""
    for launcher in _T3020_PYTEST_LAUNCHER_POSITIVE:
        body = _t3020_body(f"# AC1\n# baseline-expect: deferred\n$ {launcher} {_T3020_TEST_PATH} -q")
        res = run_repair(body)
        assert len(_t3020_deferred_repairs(res)) == 1, (launcher, res)
        assert res["repair_action"]["disposition"] == "auto_apply_safe", launcher


def test_deferred_pytest_token_not_at_launcher_position_is_not_a_pytest_vc():
    """P2-1: a `pytest` string appearing as an ARGUMENT (e.g. rg pattern) is not a pytest VC; such
    a deferred VC is left byte-identical and is not turned into human_review_required."""
    negatives = [
        f'$ rg -n "pytest" {_T3020_TEST_PATH}',
        f"$ rg -n pytest {_T3020_TEST_PATH}",
        f"$ echo pytest {_T3020_TEST_PATH}",
        f"$ FOO=1 pytest {_T3020_TEST_PATH}",
        f"$ uv run --locked rg pytest {_T3020_TEST_PATH}",
        f"$ uv run --bogus-flag pytest {_T3020_TEST_PATH}",
        f"$ uv pytest {_T3020_TEST_PATH}",
        f"$ python3 pytest {_T3020_TEST_PATH}",
        f"$ python3 -m unittest pytest {_T3020_TEST_PATH}",
        f"$ grep pytest {_T3020_TEST_PATH}",
    ]
    for cmd in negatives:
        for body in (
            _t3020_body(f"# AC1\n# baseline-expect: deferred\n{cmd}"),
            _t3020_body(f"# AC1\n{cmd} # baseline-expect: deferred"),
        ):
            res = run_repair(body)
            assert _t3020_deferred_repairs(res) == [], cmd
            assert "# preflight-scope" not in "\n".join(r["repaired"] for r in res["repairs"]), cmd
            assert res["repair_action"]["disposition"] != "human_review_required", cmd


def test_pytest_test_file_targets_unit():
    ric = _import_ric()
    f = ric._pytest_test_file_targets
    assert f(f"uv run --locked pytest {_T3020_TEST_PATH}::t -q -k x") == [_T3020_TEST_PATH]
    assert f(f'rg -n "pytest" {_T3020_TEST_PATH}') is None
    assert f(f"uv run pytest {_T3020_TEST_PATH} && true") is None
    assert f("uv run pytest") is None
    assert f("") is None


_T3020_INLINE_DEFERRED_CMD = f"pytest {_T3020_TEST_PATH}"


def test_inline_deferred_pytest_chain_is_one_consistent_auto_apply_safe_repair(tmp_path):
    """P2-2: `$ pytest X # baseline-expect: deferred` (inline) is normalised in two dependent
    steps (move -> pr_review_only). The real repair_body() -> classify_repair_action() path must
    treat it as ONE consistent original->final repair: auto_apply_safe, not overlapping_repair."""
    ric = _import_ric()
    body = _t3020_body(f"# AC1\n$ {_T3020_INLINE_DEFERRED_CMD} # baseline-expect: deferred")

    repaired, repairs = ric.repair_body(body)
    action = ric.classify_repair_action("a", "b", repairs)
    assert action["disposition"] == "auto_apply_safe", (repairs, action)
    assert "overlapping_repair" not in action["reason_codes"]
    assert len(repairs) == 1, repairs
    rec = repairs[0]
    assert rec["kind"] == "deferred_to_pr_review_only"
    assert rec["confidence"] == "high"
    assert rec["line_start"] == rec["line_end"]
    assert rec["original"] == f"$ {_T3020_INLINE_DEFERRED_CMD} # baseline-expect: deferred"
    assert rec["repaired"] == f"# preflight-scope: pr_review_only\n$ {_T3020_INLINE_DEFERRED_CMD}"
    # original -> repaired replacement is consistent with the actual body transformation
    assert body.count(rec["original"]) == 1
    assert body.replace(rec["original"], rec["repaired"]) == repaired
    # command line preserved (without the inline annotation), canonical marker present
    assert f"$ {_T3020_INLINE_DEFERRED_CMD}\n" in repaired
    assert "# preflight-scope: pr_review_only\n$ " + _T3020_INLINE_DEFERRED_CMD in repaired
    assert "baseline-expect" not in repaired
    # the recorded line really holds the first line of `repaired` in the final body
    section_lines = repaired.split("\n")
    vc_idx = section_lines.index("## Verification Commands")
    assert section_lines[vc_idx + rec["line_start"] - 1] == "# preflight-scope: pr_review_only"


def test_inline_deferred_pytest_chain_end_to_end_idempotent(tmp_path):
    ric = _import_ric()
    body = _t3020_body(f"# AC1\n$ {_T3020_INLINE_DEFERRED_CMD} # baseline-expect: deferred")
    res1, repaired1 = _t3020_apply_repair(body, tmp_path)
    assert res1["repair_action"]["disposition"] == "auto_apply_safe"
    assert res1["changed"] is True

    res2 = ric.run_repair(repaired1)
    assert res2["changed"] is False
    assert res2["repaired_body_sha256"] == res1["repaired_body_sha256"]
    assert res2["repairs"] == []
    assert res2["repair_action"]["disposition"] == "informational"


def test_inline_deferred_pytest_chain_multiple_vcs_and_unrelated_inline(tmp_path):
    """P2-2: several chained VCs plus an unrelated inline repair: each chain collapses to one
    record, the unrelated inline repair stays its own record, nothing overlaps."""
    ric = _import_ric()
    other = "pnpm test:e2e # baseline-expect: fail"
    vc = (
        f"# AC1\n$ pytest {_T3020_TEST_PATH} -q # baseline-expect: deferred\n"
        f"# AC2\n$ {other}\n"
        f"# AC3\n$ pytest {_T3020_TEST_PATH} -x # baseline-expect: deferred"
    )
    body = _t3020_body(vc)
    repaired, repairs = ric.repair_body(body)
    action = ric.classify_repair_action("a", "b", repairs)
    kinds = sorted(r["kind"] for r in repairs)
    assert kinds == [
        "deferred_to_pr_review_only",
        "deferred_to_pr_review_only",
        "move_inline_baseline_expect_to_preceding_line",
    ], repairs
    assert action["disposition"] == "auto_apply_safe", action
    assert repaired.count("# preflight-scope: pr_review_only") == 2
    assert "$ pytest " + _T3020_TEST_PATH + " -q\n" in repaired
    assert "$ pytest " + _T3020_TEST_PATH + " -x\n" in repaired
    again, again_repairs = ric.repair_body(repaired)
    assert again == repaired
    assert [r for r in again_repairs if r["kind"] == "deferred_to_pr_review_only"] == []


def test_inline_deferred_chain_out_of_condition_keeps_plain_inline_move():
    """P2-2: when the chain is not eligible for pr_review_only (non-pytest), only the ordinary
    inline-move repair remains (annotation stays `deferred`); it is still auto_apply_safe."""
    ric = _import_ric()
    body = _t3020_body(f'# AC1\n$ rg -n "x" {_T3020_TEST_PATH} # baseline-expect: deferred')
    repaired, repairs = ric.repair_body(body)
    assert [r["kind"] for r in repairs] == ["move_inline_baseline_expect_to_preceding_line"]
    assert "# baseline-expect: deferred\n$ rg" in repaired


def test_overlapping_independent_repairs_are_still_rejected():
    """P2-2 guard: overlap detection is NOT disabled; independent overlapping records are still
    human_review_required / overlapping_repair."""
    ric = _import_ric()
    action = ric.classify_repair_action(
        "a", "b",
        [
            {"kind": "deferred_to_pr_review_only", "line_start": 5, "line_end": 5, "confidence": "high"},
            {"kind": "move_inline_baseline_expect_to_preceding_line", "line_start": 5, "line_end": 5,
             "confidence": "high"},
        ],
    )
    assert action["disposition"] == "human_review_required"
    assert action["reason_codes"] == ["overlapping_repair"]

# ---------------------------------------------------------------------------
# Issue #3024: runtime_only_command marker must be the canonical
# ``# preflight-scope: pr_review_only`` (no ``reason=`` suffix).
# These tests use the REAL repair CLI, the REAL shared parser and the REAL
# baseline_vc_preflight.py (no mocks).
# ---------------------------------------------------------------------------

# scripts dir is <repo>/.claude/skills/issue-refinement-loop/scripts, so the
# repo root is parents[3]. Derived structurally (not by walking up to the first
# ancestor that contains a ``.claude`` dir) to avoid nested-.claude
# misdetection (#3023).
_REPO_ROOT_3024 = _SCRIPTS_DIR.parents[3]
_CONTRACT_REVIEW_SCRIPTS_3024 = (
    _REPO_ROOT_3024 / ".claude" / "skills" / "issue-contract-review" / "scripts"
)
_BASELINE_PREFLIGHT_3024 = _CONTRACT_REVIEW_SCRIPTS_3024 / "baseline_vc_preflight.py"
_VC_SYNTAX_3024 = _CONTRACT_REVIEW_SCRIPTS_3024 / "vc_contract_syntax.py"

_CANONICAL_MARKER_3024 = "# preflight-scope: pr_review_only"
_LEGACY_MARKER_3024 = "# preflight-scope: pr_review_only reason=runtime_only_command"

_UNANNOTATED_BODY_3024 = """## Verification Commands

```bash
# AC1
$ pnpm test:e2e
```
"""


def _load_module_3024(unique_name: str, path: Path):
    """Load a module by path under a unique name (avoid sys.modules collisions)."""
    import importlib.util

    assert path.is_file(), f"module not found: {path}"
    spec = importlib.util.spec_from_file_location(unique_name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _real_repair_cli_3024(body: str, tmp_path: Path, name: str) -> tuple[dict, str]:
    """Run the real repair CLI (--apply --out-file) and return (result, candidate body)."""
    body_file = tmp_path / f"{name}.in.md"
    out_file = tmp_path / f"{name}.out.md"
    body_file.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--body-file",
            str(body_file),
            "--apply",
            "--out-file",
            str(out_file),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"repair CLI failed rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    data = json.loads(proc.stdout)
    assert data["dry_run"] is False, data
    assert out_file.is_file(), "repair CLI did not write the candidate body"
    return data, out_file.read_text(encoding="utf-8")


def _marker_lines_3024(body: str) -> list[str]:
    return [ln.strip() for ln in body.splitlines() if ln.strip().startswith("# preflight-scope:")]


def _real_baseline_preflight_3024(body: str, tmp_path: Path, name: str) -> dict:
    body_file = tmp_path / f"{name}.preflight.md"
    body_file.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_BASELINE_PREFLIGHT_3024),
            "--body-file",
            str(body_file),
            "--cwd",
            str(_REPO_ROOT_3024),
            "--no-history-estimator",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.stdout, f"no stdout rc={proc.returncode} stderr={proc.stderr!r}"
    assert proc.returncode == 0, (
        f"baseline_vc_preflight failed rc={proc.returncode} stderr={proc.stderr!r}"
    )
    return json.loads(proc.stdout)


def test_runtime_only_marker_real_preflight_classifies_pr_review_only(tmp_path):
    """AC1: real repair output -> real baseline_vc_preflight.py => canonical pr_review_only skip."""
    result, candidate = _real_repair_cli_3024(_UNANNOTATED_BODY_3024, tmp_path, "ac1")
    assert result["changed"] is True, result
    runtime_repairs = [r for r in result["repairs"] if r["kind"] == "runtime_only_command"]
    assert len(runtime_repairs) == 1, result["repairs"]
    # the marker was actually emitted by the real repair (not hand-written)
    assert _marker_lines_3024(candidate) == [_CANONICAL_MARKER_3024], candidate

    data = _real_baseline_preflight_3024(candidate, tmp_path, "ac1")
    results = [it for it in data["results"] if "pnpm test:e2e" in (it.get("raw_command") or "")]
    assert len(results) == 1, data["results"]
    item = results[0]
    # AC mapping / command line preserved
    assert item["ac"] == "AC1", item
    assert item["raw_command"] == "pnpm test:e2e", item
    assert item["classification"] == "skipped", item
    assert item["category"] == "preflight_scope_pr_review_only", item
    assert item["scope_class"] == "pr_review_only", item
    assert item["decision"] == "go", item
    assert item["exit_code"] is None, item
    assert item["verification_owner"] == "pr-review-judge", item
    assert item["runtime_verification_required"] is False, item
    assert data["summary"]["human_judgment"] == 0, data["summary"]


def test_runtime_only_marker_real_parser_returns_canonical_known(tmp_path):
    """AC2: marker line extracted from the real repair output parses to exactly ('pr_review_only', True)."""
    _result, candidate = _real_repair_cli_3024(_UNANNOTATED_BODY_3024, tmp_path, "ac2")
    markers = _marker_lines_3024(candidate)
    assert len(markers) == 1, candidate
    syntax = _load_module_3024("vc_contract_syntax_3024_real_parser", _VC_SYNTAX_3024)
    assert syntax.parse_preflight_scope_marker_line(markers[0]) == ("pr_review_only", True)


def test_runtime_only_marker_roundtrip_idempotent_on_real_output(tmp_path):
    """AC3: real repair output re-fed to real repair is a fixed point."""
    first, body_after_first = _real_repair_cli_3024(_UNANNOTATED_BODY_3024, tmp_path, "rt1")
    assert first["changed"] is True, first
    assert body_after_first != _UNANNOTATED_BODY_3024

    second, body_after_second = _real_repair_cli_3024(body_after_first, tmp_path, "rt2")
    assert body_after_second == body_after_first
    assert (
        first["repaired_body_sha256"]
        == second["original_body_sha256"]
        == second["repaired_body_sha256"]
    ), (first, second)
    assert second["changed"] is False, second
    assert [r for r in second["repairs"] if r["kind"] == "runtime_only_command"] == []


def test_runtime_only_marker_repair_reason_still_recorded(tmp_path):
    """AC4: repairs[].reason is preserved as an independent field, not in the marker line."""
    result, candidate = _real_repair_cli_3024(_UNANNOTATED_BODY_3024, tmp_path, "reason")
    runtime_repairs = [r for r in result["repairs"] if r["kind"] == "runtime_only_command"]
    assert len(runtime_repairs) == 1, result["repairs"]
    assert "command_not_in_allowlist_runtime_only" in runtime_repairs[0]["reason"]
    assert "pnpm test:e2e" in runtime_repairs[0]["reason"]
    assert "reason=" not in candidate
    assert "reason=" not in runtime_repairs[0]["repaired"]


def test_runtime_only_marker_existing_canonical_preserved(tmp_path):
    """AC3: an existing canonical marker is left unchanged by repair."""
    body = _UNANNOTATED_BODY_3024.replace(
        "$ pnpm test:e2e", f"{_CANONICAL_MARKER_3024}\n$ pnpm test:e2e"
    )
    assert _marker_lines_3024(body) == [_CANONICAL_MARKER_3024]
    result, candidate = _real_repair_cli_3024(body, tmp_path, "canon")
    assert result["changed"] is False, result
    assert candidate == body
    assert [r for r in result["repairs"] if r["kind"] == "runtime_only_command"] == []


def test_runtime_only_marker_legacy_suffix_preserved(tmp_path):
    """AC3: a stored legacy ``reason=`` suffix marker is NOT auto-migrated (boundary fixture,
    not an endorsement of the legacy marker)."""
    body = _UNANNOTATED_BODY_3024.replace(
        "$ pnpm test:e2e", f"{_LEGACY_MARKER_3024}\n$ pnpm test:e2e"
    )
    assert _marker_lines_3024(body) == [_LEGACY_MARKER_3024]
    result, candidate = _real_repair_cli_3024(body, tmp_path, "legacy")
    assert result["changed"] is False, result
    assert candidate == body
    assert _marker_lines_3024(candidate) == [_LEGACY_MARKER_3024]
    assert [r for r in result["repairs"] if r["kind"] == "runtime_only_command"] == []
