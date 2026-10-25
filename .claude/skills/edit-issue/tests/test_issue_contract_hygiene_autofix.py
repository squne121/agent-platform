"""
Tests for issue_contract_hygiene_autofix.py

Tests cover:
  - C4: $ prefix added to command lines in fenced bash blocks within Verification Commands
  - C9: ## Runtime Verification Applicability section inserted when missing + non-runtime paths
  - body_sha256 guard: exit 1 when body unchanged
  - exit 2 cases: runtime paths / unknown paths / missing Allowed Paths with missing C9
  - exit 2 case: non-C4/C9 blockers detected by check_issue_contract.py
  - Combined C4+C9 repair
"""

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path


SCRIPT = (
    Path(__file__).parent.parent / "scripts" / "issue_contract_hygiene_autofix.py"
)


def _load_autofix_module():
    """Import issue_contract_hygiene_autofix.py as a module for monkeypatching.

    The other tests in this file invoke the script as a subprocess; the
    stream-separation / fail-closed regression tests need to monkeypatch
    ``subprocess.run`` on the module itself, so we import it directly here.
    """
    spec = importlib.util.spec_from_file_location("_hygiene_autofix_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# ---------------------------------------------------------------------------
# Minimal complete contract body helpers
# ---------------------------------------------------------------------------
# To pass check_issue_contract.py's non-C4/C9 blocker gate, fixture bodies must
# include the minimum required sections (Outcome, Acceptance Criteria, Stop Conditions,
# Verification Commands, Allowed Paths) with at least one $ command in VC.
#
# _MINIMAL_CONTRACT_SUFFIX is appended to test bodies that need to satisfy the
# structural requirements without overriding what we're testing.

_MINIMAL_SUFFIX = textwrap.dedent("""\
    ## Outcome
    Test outcome.

    ## Acceptance Criteria
    - [ ] AC1: file exists

    ## Stop Conditions
    - エラー時は停止する。
""")


def _wrap_body(vc_block: str, allowed_paths: str, rva_block: str = "", suffix: str = _MINIMAL_SUFFIX) -> str:
    """Assemble a minimal valid contract body."""
    parts = [suffix]
    parts.append(f"## Verification Commands\n\n{vc_block}\n")
    parts.append(f"## Allowed Paths\n{allowed_paths}\n")
    if rva_block:
        parts.append(rva_block)
    parts.append("## Delivery Rule\n1 Issue = 1 PR\n")
    return "\n".join(parts)


def run_autofix(body: str, extra_args: list[str] | None = None) -> tuple[int, str, str]:
    """Run autofix script with given body as stdin. Returns (exit_code, stdout, stderr)."""
    args = [sys.executable, str(SCRIPT)] + (extra_args or [])
    result = subprocess.run(
        args,
        input=body,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def run_autofix_file(body: str, tmp_path: Path) -> tuple[int, str, str]:
    """Run autofix script with --body-file and --out-file."""
    in_file = tmp_path / "body_in.md"
    out_file = tmp_path / "body_out.md"
    in_file.write_text(body, encoding="utf-8")
    args = [
        sys.executable,
        str(SCRIPT),
        "--body-file",
        str(in_file),
        "--out-file",
        str(out_file),
    ]
    result = subprocess.run(args, capture_output=True, text=True)
    if out_file.exists():
        return result.returncode, out_file.read_text(encoding="utf-8"), result.stderr
    return result.returncode, "", result.stderr


# ---------------------------------------------------------------------------
# Shared fixture bodies (complete contract structure)
# ---------------------------------------------------------------------------

_RVA_BLOCK = textwrap.dedent("""\
    ## Runtime Verification Applicability
    ```yaml
    decision: not_applicable
    reason: "test"
    ```
""")

# C4 tests: body already has RVA so C9 doesn't trigger
C4_BODY_NEEDS_REPAIR = _wrap_body(
    vc_block=textwrap.dedent("""\
        ```bash
        # AC1
        test -f .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py
        ```"""),
    allowed_paths="- .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py\n",
    rva_block=_RVA_BLOCK,
)

C4_BODY_ALREADY_FIXED = _wrap_body(
    vc_block=textwrap.dedent("""\
        ```bash
        # AC1
        $ test -f .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py
        ```"""),
    allowed_paths="- .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py\n",
    rva_block=_RVA_BLOCK,
)

# C9 tests: body has $ prefix already (no C4 issue), but missing RVA
C9_BODY_MISSING_RVA = _wrap_body(
    vc_block=textwrap.dedent("""\
        ```bash
        # AC1
        $ test -f .claude/agents/issue-author.md
        ```"""),
    allowed_paths="- .claude/agents/issue-author.md\n- .claude/skills/edit-issue/SKILL.md\n",
    rva_block="",
)

C9_BODY_WITH_RUNTIME_PATHS = _wrap_body(
    vc_block=textwrap.dedent("""\
        ```bash
        # AC1
        $ test -f src/main.ts
        ```"""),
    allowed_paths="- src/main.ts\n- .claude/agents/issue-author.md\n",
    rva_block="",
)


# ---------------------------------------------------------------------------
# C4 Tests
# ---------------------------------------------------------------------------

def test_c4_adds_dollar_prefix_to_command_line():
    """GIVEN a fenced bash VC block with a command missing $ / WHEN autofix runs / THEN $ prefix is added."""
    code, out, err = run_autofix(C4_BODY_NEEDS_REPAIR)
    assert code == 0, f"Expected exit 0 (repaired), got {code}. stderr={err}"
    assert "$ test -f" in out


def test_c4_does_not_add_prefix_to_comment_line():
    """GIVEN a # comment line in bash block / WHEN autofix runs / THEN comment line is unchanged."""
    code, out, err = run_autofix(C4_BODY_NEEDS_REPAIR)
    assert "# AC1" in out
    assert "$ # AC1" not in out


def test_c4_skips_already_prefixed_line():
    """GIVEN a line already prefixed with $ / WHEN autofix runs / THEN no double-prefix."""
    code, out, err = run_autofix(C4_BODY_ALREADY_FIXED)
    # Body already fixed → no C4 repair → exit 1 (no_change)
    assert code == 1, f"Expected exit 1 (no_change), got {code}. stderr={err}"
    assert "$ $ " not in out


def test_c4_skips_shell_variable_assignment():
    """GIVEN a shell variable assignment line in bash block / WHEN autofix runs / THEN no $ prefix."""
    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            ISSUE_NUMBER=573
            test -f some_file
            ```"""),
        allowed_paths="- .claude/agents/issue-author.md\n",
        rva_block=_RVA_BLOCK,
    )
    code, out, err = run_autofix(body)
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    assert "$ ISSUE_NUMBER" not in out
    assert "ISSUE_NUMBER=573" in out
    assert "$ test -f" in out


def test_c4_only_in_vc_section():
    """GIVEN bash blocks outside ## Verification Commands / WHEN autofix runs / THEN those are not modified."""
    # Construct body with a Background section containing a bash block
    body = textwrap.dedent("""\
        ## Outcome
        Test outcome.

        ## Acceptance Criteria
        - [ ] AC1: file exists

        ## Stop Conditions
        - エラー時は停止する。

        ## Background

        ```bash
        some_command_outside_vc
        ```

        ## Verification Commands

        ```bash
        # AC1
        test -f some_file
        ```

        ## Allowed Paths
        - .claude/agents/issue-author.md

        ## Runtime Verification Applicability
        ```yaml
        decision: not_applicable
        reason: "test"
        ```

        ## Delivery Rule
        1 Issue = 1 PR
    """)
    code, out, err = run_autofix(body)
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    # Outside VC section: not modified
    assert "$ some_command_outside_vc" not in out
    # Inside VC section: modified
    assert "$ test -f some_file" in out


# ---------------------------------------------------------------------------
# C9 Tests
# ---------------------------------------------------------------------------

def test_c9_inserts_rva_section_for_non_runtime_paths():
    """GIVEN missing RVA section with non-runtime Allowed Paths / WHEN autofix runs / THEN RVA section inserted."""
    code, out, err = run_autofix(C9_BODY_MISSING_RVA)
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    assert "## Runtime Verification Applicability" in out
    assert "decision: not_applicable" in out


def test_c9_inserted_before_delivery_rule():
    """GIVEN missing RVA / WHEN autofix runs / THEN RVA section appears before ## Delivery Rule."""
    code, out, err = run_autofix(C9_BODY_MISSING_RVA)
    assert code == 0
    rva_pos = out.find("## Runtime Verification Applicability")
    delivery_pos = out.find("## Delivery Rule")
    assert rva_pos != -1
    assert delivery_pos != -1
    assert rva_pos < delivery_pos


def test_c9_skips_runtime_paths_returns_exit2():
    """GIVEN missing RVA section with runtime path (src/) / WHEN autofix runs / THEN exit 2 (not_autofixable)."""
    code, out, err = run_autofix(C9_BODY_WITH_RUNTIME_PATHS)
    # Runtime paths → not safe to autofix → exit 2
    assert code == 2, f"Expected exit 2 (not_autofixable), got {code}. stderr={err}"
    assert "## Runtime Verification Applicability" not in out


def test_c9_unknown_path_returns_exit2():
    """GIVEN missing RVA section with .github/workflows path (not in whitelist) / WHEN autofix runs / THEN exit 2."""
    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            $ test -f .github/workflows/ci.yml
            ```"""),
        allowed_paths="- .github/workflows/ci.yml\n",
        rva_block="",
    )
    code, out, err = run_autofix(body)
    # .github/workflows is not in the non-runtime whitelist → exit 2
    assert code == 2, f"Expected exit 2 (unknown path), got {code}. stderr={err}"
    assert "## Runtime Verification Applicability" not in out


def test_c9_missing_allowed_paths_returns_exit2():
    """GIVEN missing RVA section and missing ## Allowed Paths section / WHEN autofix runs / THEN exit 2."""
    body = textwrap.dedent("""\
        ## Outcome
        Test outcome.

        ## Acceptance Criteria
        - [ ] AC1: file exists

        ## Stop Conditions
        - エラー時は停止する。

        ## Verification Commands

        ```bash
        # AC1
        $ test -f .claude/agents/issue-author.md
        ```

        ## Delivery Rule
        1 Issue = 1 PR
    """)
    code, out, err = run_autofix(body)
    # No Allowed Paths section → cannot safely classify → exit 2
    assert code == 2, f"Expected exit 2 (missing Allowed Paths), got {code}. stderr={err}"
    assert "## Runtime Verification Applicability" not in out


def test_c9_no_duplicate_if_already_present():
    """GIVEN existing RVA section / WHEN autofix runs / THEN no duplicate section added."""
    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            $ test -f .claude/agents/issue-author.md
            ```"""),
        allowed_paths="- .claude/agents/issue-author.md\n",
        rva_block=textwrap.dedent("""\
            ## Runtime Verification Applicability
            ```yaml
            decision: not_applicable
            reason: "already present"
            ```
        """),
    )
    code, out, err = run_autofix(body)
    # No changes needed → exit 1
    assert code == 1, f"Expected exit 1 (no_change), got {code}. stderr={err}"
    assert out.count("## Runtime Verification Applicability") == 0  # stdout empty on exit 1


# ---------------------------------------------------------------------------
# Combined C4 + C9 repair
# ---------------------------------------------------------------------------

def test_combined_c4_and_c9_repair():
    """GIVEN both C4 (missing $) and C9 (missing RVA) issues / WHEN autofix runs / THEN both repaired."""
    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            test -f .claude/agents/issue-author.md
            ```"""),
        allowed_paths="- .claude/agents/issue-author.md\n- .claude/skills/edit-issue/SKILL.md\n",
        rva_block="",
    )
    code, out, err = run_autofix(body)
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    assert "$ test -f" in out
    assert "## Runtime Verification Applicability" in out
    assert "decision: not_applicable" in out


# ---------------------------------------------------------------------------
# sha256 guard (no_change)
# ---------------------------------------------------------------------------

def test_no_change_returns_exit1():
    """GIVEN body with no issues / WHEN autofix runs / THEN exit 1 (no_change)."""
    code, out, err = run_autofix(C4_BODY_ALREADY_FIXED)
    assert code == 1, f"Expected exit 1, got {code}. stderr={err}"
    assert "no_change" in err


def test_sha256_guard_idempotent(tmp_path: Path):
    """GIVEN repaired body / WHEN autofix runs again / THEN exit 1 (already repaired = no_change)."""
    # First run: should repair
    code1, out1, _ = run_autofix(C4_BODY_NEEDS_REPAIR)
    assert code1 == 0

    # Second run on repaired body: should be no_change
    code2, out2, err2 = run_autofix(out1)
    assert code2 == 1, f"Expected exit 1 (no_change on second run), got {code2}. stderr={err2}"


# ---------------------------------------------------------------------------
# --body-file / --out-file interface
# ---------------------------------------------------------------------------

def test_body_file_and_out_file_interface(tmp_path: Path):
    """GIVEN --body-file and --out-file args / WHEN autofix runs / THEN output written to out-file."""
    code, out, err = run_autofix_file(C4_BODY_NEEDS_REPAIR, tmp_path)
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    assert "$ test -f" in out


# ---------------------------------------------------------------------------
# Continuation line handling
# ---------------------------------------------------------------------------

def test_c4_skips_continuation_lines():
    """GIVEN a multi-line command with backslash continuation / WHEN autofix runs / THEN continuation lines not
        prefixed.
    """
    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            some_command \\
              --arg1 \\
              --arg2
            ```"""),
        allowed_paths="- .claude/agents/issue-author.md\n",
        rva_block=_RVA_BLOCK,
    )
    code, out, err = run_autofix(body)
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    assert "$ some_command \\" in out
    # Continuation lines should not be prefixed
    assert "$   --arg1" not in out
    assert "$   --arg2" not in out


# ---------------------------------------------------------------------------
# check_non_c4_c9_blockers caller-contract regression tests (#598)
#
# These tests monkeypatch subprocess.run on the imported module to fix the
# caller contract with check_issue_contract.py --json:
#   - stdout/stderr are captured separately (capture_output=True, text=True);
#     stderr is NOT merged into stdout (no stderr=subprocess.STDOUT).
#   - JSON is parsed from stdout only; stderr diagnostics never affect parsing.
#   - When stdout is not valid JSON, the function fails CLOSED
#     (return True, ["check_error"]) instead of failing open (return False, []).
# ---------------------------------------------------------------------------


def test_check_non_c4_c9_blockers_stream_separation(monkeypatch):
    """GIVEN check_non_c4_c9_blockers / WHEN it invokes check_issue_contract.py /
    THEN it uses capture_output=True, text=True and does NOT merge stderr into stdout."""
    module = _load_autofix_module()
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout='{"blocking_issues": []}', stderr=""
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    has_other, codes = module.check_non_c4_c9_blockers("## Outcome\nx\n")

    # Stream-separation contract: capture_output=True, text=True, no stderr merge.
    assert seen["kwargs"]["capture_output"] is True
    assert "stderr" not in seen["kwargs"]
    assert seen["kwargs"]["text"] is True
    # Argument contract: the consumer must actually invoke check_issue_contract.py
    # in --json mode (otherwise the stream-separation guarantee is meaningless).
    # Pin the argv shape so dropping --json / --file is caught as a regression.
    assert seen["args"][0] == sys.executable
    assert seen["args"][1] == module.CHECK_ISSUE_CONTRACT_SCRIPT
    assert "--file" in seen["args"]
    assert "--json" in seen["args"]
    # Valid JSON with empty blocking_issues → no other blockers.
    assert has_other is False
    assert codes == []


def test_check_non_c4_c9_blockers_stderr_diagnostic_ignored(monkeypatch):
    """GIVEN stdout=<valid JSON> and stderr=<diagnostic warning> /
    WHEN check_non_c4_c9_blockers runs / THEN it parses stdout only and succeeds."""
    module = _load_autofix_module()

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout='{"blocking_issues": []}',
            stderr="[WARN] DeprecationWarning: datetime.utcnow() is deprecated\n",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    has_other, codes = module.check_non_c4_c9_blockers("## Outcome\nx\n")

    # Diagnostics on stderr must NOT affect JSON parsing of stdout.
    assert has_other is False
    assert codes == []


def test_check_non_c4_c9_blockers_stderr_diagnostic_with_real_blocker(monkeypatch):
    """GIVEN stderr=<diagnostic warning> and stdout=<valid JSON with a non-C4/C9 blocker> /
    WHEN check_non_c4_c9_blockers runs / THEN it parses stdout only and still reports the
    blocker — locking the junction between stdout-only parsing and blocker detection so
    stderr noise neither suppresses nor fabricates a blocker."""
    module = _load_autofix_module()

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout='{"blocking_issues": ["unrelated blocker message for testing"]}',
            stderr="[WARN] DeprecationWarning: datetime.utcnow() is deprecated\n",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    has_other, codes = module.check_non_c4_c9_blockers("## Outcome\nx\n")

    # stderr diagnostics are ignored; the stdout blocker is still surfaced.
    assert has_other is True
    assert codes == ["unrelated blocker message for testing"]


def test_check_non_c4_c9_blockers_fail_closed_on_non_json(monkeypatch):
    """GIVEN stdout=<diagnostic text + JSON> (contract violated) /
    WHEN check_non_c4_c9_blockers runs / THEN it fails CLOSED with (True, ["check_error"])."""
    module = _load_autofix_module()

    def fake_run(args, **kwargs):
        # Diagnostics leaked into stdout, breaking JSON purity.
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout='[WARN] some diagnostic leaked\n{"blocking_issues": []}',
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    has_other, codes = module.check_non_c4_c9_blockers("## Outcome\nx\n")

    # Fail-closed: keep the non-C4/C9 blocker guard active, do not fail open.
    assert has_other is True
    assert codes == ["check_error"]


# ---------------------------------------------------------------------------
# Blocker 3 (PR #1305 review): VC baseline shape repair fails CLOSED when the
# compiler module cannot be loaded (this repair is a completion condition of
# Issue #1285, not optional/best-effort).
# ---------------------------------------------------------------------------


def test_repair_vc_baseline_shape_load_failure_fails_closed(monkeypatch):
    """GIVEN vc_baseline_shape_compiler.py cannot be loaded (import/load error) /
    WHEN repair_vc_baseline_shape() runs / THEN it fails closed
    (VcShapeResult.LOAD_FAILED), never silently skipping the repair."""
    module = _load_autofix_module()

    def fake_loader():
        raise ImportError("compiler module intentionally broken for this test")

    monkeypatch.setattr(module, "_load_vc_baseline_shape_compiler", fake_loader)

    lines = [
        "## Verification Commands\n",
        "\n",
        "```bash\n",
        "$ pytest some_dir/test_existing.py -k test_new_name\n",
        "```\n",
    ]
    new_lines, repaired, status, reason = module.repair_vc_baseline_shape(lines, ".")

    assert status == module.VcShapeResult.LOAD_FAILED
    assert repaired is False
    assert new_lines == lines
    assert reason is not None


def test_repair_vc_baseline_shape_no_vc_section_is_safe_noop(monkeypatch):
    """GIVEN a body with no ## Verification Commands section at all /
    WHEN repair_vc_baseline_shape() runs / THEN it is a safe no-op
    (VcShapeResult.NO_VC_SECTION) without even attempting to load the
    compiler module."""
    module = _load_autofix_module()

    def fail_loader():
        raise AssertionError("compiler must not be loaded when there is no VC section")

    monkeypatch.setattr(module, "_load_vc_baseline_shape_compiler", fail_loader)

    lines = ["## Outcome\n", "Something.\n"]
    new_lines, repaired, status, reason = module.repair_vc_baseline_shape(lines, ".")

    assert status == module.VcShapeResult.NO_VC_SECTION
    assert repaired is False
    assert new_lines == lines
    assert reason is None


def test_main_fails_closed_on_vc_shape_compiler_load_failure(monkeypatch, tmp_path: Path):
    """GIVEN VC_BASELINE_SHAPE_COMPILER_SCRIPT points at a nonexistent file /
    WHEN main() runs (in-process) on a body containing a pytest VC command /
    THEN main() returns exit code 2 (fail-closed) instead of the previous
    fail-open [WARN]+continue behavior (Issue #1305 review Blocker 3)."""
    import io

    module = _load_autofix_module()
    monkeypatch.setattr(
        module, "VC_BASELINE_SHAPE_COMPILER_SCRIPT", str(tmp_path / "does_not_exist.py")
    )

    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            $ pytest .claude/agents/issue-author.md -k test_new_name
            ```"""),
        allowed_paths="- .claude/agents/issue-author.md\n",
        rva_block=_RVA_BLOCK,
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))

    exit_code = module.main()
    assert exit_code == 2


# ---------------------------------------------------------------------------
# Blocker 4 (PR #1305 review): repair order is C4 -> VC shape -> C9 -> sha256
# guard, so C4 (adding $ prefixes) runs before VC shape detection (which only
# scans $-prefixed lines), and a C9 not_autofixable failure never lets a VC
# shape partial rewrite leak into stdout.
# ---------------------------------------------------------------------------


def _make_blocker4_probe(repo_root: Path, leaf_name: str) -> tuple[Path, str, str]:
    """Create a real probe test file under `scripts/<leaf_name>/` (a
    non-runtime-whitelisted prefix, so C9 repair can classify it) for VC
    shape repair to target.

    Returns (probe_file_path, existing_repo_relative_path,
    candidate_repo_relative_path).
    """
    probe_dir = repo_root / "scripts" / leaf_name
    probe_dir.mkdir(exist_ok=True)
    probe_file = probe_dir / f"test_existing_{leaf_name}.py"
    probe_file.write_text("def test_alpha():\n    pass\n", encoding="utf-8")
    existing_repo_file = f"scripts/{leaf_name}/test_existing_{leaf_name}.py"
    candidate = f"scripts/{leaf_name}/test_existing_{leaf_name}_new_test.py"
    return probe_file, existing_repo_file, candidate


def test_combined_c4_vc_shape_c9_repaired_together():
    """GIVEN a body needing C4 ($ prefix), a VC baseline shape rewrite, and
    C9 (missing RVA) all at once / WHEN autofix runs / THEN all three
    repairs are applied together in a single pass, proving VC shape
    detection (which only recognizes $-prefixed lines) runs AFTER C4 adds
    the missing $ prefix (Issue #1305 review Blocker 4)."""
    repo_root = SCRIPT.parents[4]
    probe_file, existing_repo_file, candidate = _make_blocker4_probe(
        repo_root, "blocker4_combined_probe"
    )
    try:
        body = _wrap_body(
            vc_block=textwrap.dedent(f"""\
                ```bash
                # AC1
                test -f {existing_repo_file}
                pytest {existing_repo_file} -k test_new_blocker4_combined
                ```"""),
            allowed_paths=f"- {existing_repo_file}\n- {candidate}\n",
            rva_block="",
        )
        code, out, err = run_autofix(body)
        assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
        assert "$ test -f" in out
        assert candidate in out
        assert "::test_new_blocker4_combined" in out
        assert "## Runtime Verification Applicability" in out
        assert "decision: not_applicable" in out
        assert "c4=True" in err
        assert "vc_shape=True" in err
        assert "c9=True" in err

        # 2nd run on the repaired body: everything is now canonical/present
        # -> sha256 guard -> no_change (exit 1). This also exercises the
        # idempotency requirement from Blocker 4.
        code2, out2, err2 = run_autofix(out)
        assert code2 == 1, f"Expected exit 1 (no_change) on 2nd run, got {code2}. stderr={err2}"
    finally:
        probe_file.unlink(missing_ok=True)
        try:
            probe_file.parent.rmdir()
        except OSError:
            pass  # never remove the shared "scripts/" parent directory


def test_vc_shape_only_repair_reported_alone():
    """GIVEN a body needing ONLY the VC baseline shape rewrite (C4/$-prefix
    and C9/RVA are both already satisfied) / WHEN autofix runs / THEN only
    vc_shape is reported as repaired."""
    repo_root = SCRIPT.parents[4]
    probe_file, existing_repo_file, candidate = _make_blocker4_probe(
        repo_root, "blocker4_vcshape_only_probe"
    )
    try:
        body = _wrap_body(
            vc_block=textwrap.dedent(f"""\
                ```bash
                # AC1
                $ pytest {existing_repo_file} -k test_new_blocker4_vcshape_only
                ```"""),
            allowed_paths=f"- {existing_repo_file}\n- {candidate}\n",
            rva_block=_RVA_BLOCK,
        )
        code, out, err = run_autofix(body)
        assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
        assert candidate in out
        assert "c4=False" in err
        assert "vc_shape=True" in err
        assert "c9=False" in err
    finally:
        probe_file.unlink(missing_ok=True)
        try:
            probe_file.parent.rmdir()
        except OSError:
            pass  # never remove the shared "scripts/" parent directory


def test_c9_not_autofixable_suppresses_vc_shape_partial_output():
    """GIVEN a body where VC shape repair WOULD succeed but C9 is
    not_autofixable (a runtime path is present in Allowed Paths) /
    WHEN autofix runs / THEN main() exits 2 and stdout is empty — the VC
    shape rewrite must never leak out as a partial/silent success when a
    later-stage repair fails closed (Issue #1305 review Blocker 4)."""
    repo_root = SCRIPT.parents[4]
    probe_file, existing_repo_file, candidate = _make_blocker4_probe(
        repo_root, "blocker4_c9blocked_probe"
    )
    try:
        body = _wrap_body(
            vc_block=textwrap.dedent(f"""\
                ```bash
                # AC1
                $ pytest {existing_repo_file} -k test_new_blocker4_c9blocked
                ```"""),
            # src/ is a runtime path prefix -> C9 not_autofixable.
            allowed_paths=f"- {existing_repo_file}\n- {candidate}\n- src/main.ts\n",
            rva_block="",
        )
        code, out, err = run_autofix(body)
        assert code == 2, f"Expected exit 2 (C9 not_autofixable), got {code}. stderr={err}"
        assert out == "", "stdout must be empty; no partial VC shape rewrite may leak out"
        assert candidate not in out
    finally:
        probe_file.unlink(missing_ok=True)
        try:
            probe_file.parent.rmdir()
        except OSError:
            pass  # never remove the shared "scripts/" parent directory


# ---------------------------------------------------------------------------
# High risk (PR #1305 review): compile_body() status == "changed" together
# with not_autofixable warnings on OTHER lines in the same body must not be
# partially applied — autofix fails closed instead.
# ---------------------------------------------------------------------------


def test_vc_shape_mixed_changed_and_not_autofixable_fails_closed():
    """GIVEN a body with two pytest VC lines where one is safely rewritable
    (changed) and the other is not_autofixable (complex -k expression) /
    WHEN autofix runs / THEN it fails closed (exit 2) rather than silently
    applying only the safe rewrite and leaving the other line broken."""
    repo_root = SCRIPT.parents[4]
    probe_file, existing_repo_file, candidate = _make_blocker4_probe(
        repo_root, "blocker4_mixed_probe"
    )
    try:
        body = _wrap_body(
            vc_block=textwrap.dedent(f"""\
                ```bash
                # AC1
                $ pytest {existing_repo_file} -k test_new_blocker4_mixed
                # AC2
                $ pytest {existing_repo_file} -k "test_alpha or test_beta"
                ```"""),
            allowed_paths=f"- {existing_repo_file}\n- {candidate}\n",
            rva_block=_RVA_BLOCK,
        )
        code, out, err = run_autofix(body)
        assert code == 2, f"Expected exit 2 (mixed changed+not_autofixable), got {code}. stderr={err}"
        assert out == ""
        assert "vc_shape_mixed_changed_and_warnings" in err
    finally:
        probe_file.unlink(missing_ok=True)
        try:
            probe_file.parent.rmdir()
        except OSError:
            pass  # never remove the shared "scripts/" parent directory


def test_parse_allowed_paths_no_path_marker_returns_empty():
    """AC12 (Issue #2783): canonical marker `(none)` and legacy marker
    `読み取り専用。リポジトリ変更なし（既定）` as the sole Allowed Paths entry
    both resolve to [] (the missing-section sentinel `None` is reserved for
    "no ## Allowed Paths section at all", unaffected by this fix). Before
    Issue #2783 this module had no annotation stripping at all, so both
    markers leaked as literal non-empty "path" strings."""
    autofix = _load_autofix_module()

    lines_canonical = "## Allowed Paths\n\n- (none)\n\n## Stop Conditions\n".split("\n")
    assert autofix.parse_allowed_paths(lines_canonical) == []

    lines_legacy = (
        "## Allowed Paths\n\n- 読み取り専用。リポジトリ変更なし（既定）\n\n## Stop Conditions\n"
    ).split("\n")
    assert autofix.parse_allowed_paths(lines_legacy) == []


# ---------------------------------------------------------------------------
# Issue #3020: canonical preflight-scope markers are preserved through the real
# hygiene path (issue_contract_hygiene_autofix.py -> repair_vc_baseline_shape()
# -> vc_baseline_shape_compiler.compile_body()).
# ---------------------------------------------------------------------------


def _cleanup_probe(probe_file: Path) -> None:
    probe_file.unlink(missing_ok=True)
    try:
        probe_file.parent.rmdir()
    except OSError:
        pass  # never remove the shared "scripts/" parent directory


def _vc_section(text: str) -> str:
    return text.split("## Verification Commands", 1)[1].split("## Allowed Paths", 1)[0]


def _command_lines(text: str) -> list[str]:
    return [ln for ln in _vc_section(text).splitlines() if ln.lstrip().startswith("$ ")]


def _ac_command_pairs(text: str) -> list[tuple[str, str]]:
    """(AC label, command) pairs in order, taking the last `# AC<N>` seen before each command."""
    pairs: list[tuple[str, str]] = []
    current = ""
    for ln in _vc_section(text).splitlines():
        stripped = ln.strip()
        if stripped.startswith("# AC"):
            current = stripped
        elif stripped.startswith("$ "):
            pairs.append((current, stripped))
    return pairs


def test_scope_marker_pr_review_only_vc_preserved_with_other_repairs():
    """GIVEN a pr_review_only-marked pytest VC (new -k on an existing repo test file) next to a
    C4 repair target / WHEN the real autofix script runs / THEN C4 is applied but the marked VC's
    command, AC mapping, target path and marker line are unchanged and vc_shape is not applied;
    a second run is a no_change."""
    repo_root = SCRIPT.parents[4]
    probe_file, existing_repo_file, candidate = _make_blocker4_probe(
        repo_root, "issue3020_scope_preserved_probe"
    )
    try:
        marked = f"pytest {existing_repo_file} -k test_new_issue3020_scope_preserved"
        body = _wrap_body(
            vc_block=textwrap.dedent(f"""\
                ```bash
                # AC1
                test -f {existing_repo_file}
                # AC2
                # preflight-scope: pr_review_only
                $ {marked}
                ```"""),
            allowed_paths=f"- {existing_repo_file}\n- {candidate}\n",
            rva_block=_RVA_BLOCK,
        )
        code, out, err = run_autofix(body)
        assert code == 0, f"Expected exit 0 (C4 repaired), got {code}. stderr={err}"
        assert "c4=True" in err
        assert "vc_shape=False" in err
        assert f"$ {marked}" in out
        assert "# preflight-scope: pr_review_only" in out
        assert candidate not in _vc_section(out)
        assert ("# AC2", f"$ {marked}") in _ac_command_pairs(out)
        assert f"$ test -f {existing_repo_file}" in _command_lines(out)

        code2, _out2, err2 = run_autofix(out)
        assert code2 == 1 and "no_change" in err2
    finally:
        _cleanup_probe(probe_file)


def test_scope_marker_runtime_only_simple_k_vc_preserved():
    """GIVEN a runtime_only-marked pytest VC with a bare new `-k test_x` (a shape the compiler
    would otherwise rewrite) / WHEN autofix runs on a body with no other repair / THEN the body
    is unchanged (exit 1 no_change), not rewritten to a missing-file node-id."""
    repo_root = SCRIPT.parents[4]
    probe_file, existing_repo_file, candidate = _make_blocker4_probe(
        repo_root, "issue3020_scope_runtime_probe"
    )
    try:
        body = _wrap_body(
            vc_block=textwrap.dedent(f"""\
                ```bash
                # AC1
                # preflight-scope: runtime_only
                $ pytest {existing_repo_file} -k test_new_issue3020_runtime
                ```"""),
            allowed_paths=f"- {existing_repo_file}\n- {candidate}\n",
            rva_block=_RVA_BLOCK,
        )
        code, out, err = run_autofix(body)
        assert code == 1, f"Expected exit 1 (no_change), got {code}. stderr={err}"
        assert "no_change" in err
        assert "vc_shape_mixed" not in err
    finally:
        _cleanup_probe(probe_file)


def test_scope_marker_preserved_in_mixed_body_without_unsafe_mixed():
    """GIVEN a pr_review_only-marked VC with a complex `-k` plus an unmarked rewritable VC /
    WHEN autofix runs / THEN no UNSAFE_MIXED fail-closed is raised (the marked VC is not a
    not_autofixable warning), only the unmarked VC is rewritten, and the marked command is
    unchanged. Unmarked legacy VCs are still rewritten as before."""
    repo_root = SCRIPT.parents[4]
    probe_file, existing_repo_file, candidate = _make_blocker4_probe(
        repo_root, "issue3020_scope_mixed_probe"
    )
    try:
        marked = f'pytest {existing_repo_file} -k "test_alpha or test_beta"'
        body = _wrap_body(
            vc_block=textwrap.dedent(f"""\
                ```bash
                # AC1
                # preflight-scope: pr_review_only
                $ {marked}
                # AC2
                $ pytest {existing_repo_file} -k test_new_issue3020_mixed
                ```"""),
            allowed_paths=f"- {existing_repo_file}\n- {candidate}\n",
            rva_block=_RVA_BLOCK,
        )
        code, out, err = run_autofix(body)
        assert code == 0, f"Expected exit 0 (no UNSAFE_MIXED), got {code}. stderr={err}"
        assert "vc_shape_mixed_changed_and_warnings" not in err
        assert "vc_shape=True" in err
        pairs = _ac_command_pairs(out)
        assert ("# AC1", f"$ {marked}") in pairs
        assert ("# AC2", f"$ pytest {candidate}::test_new_issue3020_mixed") in pairs
        assert "# preflight-scope: pr_review_only" in out
    finally:
        _cleanup_probe(probe_file)
