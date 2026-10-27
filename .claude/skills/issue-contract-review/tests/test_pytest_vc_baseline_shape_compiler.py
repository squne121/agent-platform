"""
Tests for vc_baseline_shape_compiler.py (Issue #1285)

Covers:
  AC1: existing_test_file.py -k test_new_name -> rewrite hint (bare identifier only)
  AC2: existing_test_file.py::test_missing_name -> rewrite hint (simple function node-id only)
  AC3: complex -k expression / Allowed Paths outside -> not_autofixable
  AC4: missing_new_test_file.py::test_name -> already_canonical
  AC5: issue_contract_hygiene_autofix.py wiring + sha256 no_change on 2nd run
"""

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

COMPILER_SCRIPT = Path(__file__).parent.parent / "scripts" / "vc_baseline_shape_compiler.py"
AUTOFIX_SCRIPT = (
    Path(__file__).parent.parent.parent / "edit-issue" / "scripts" / "issue_contract_hygiene_autofix.py"
)


def _load_compiler():
    spec = importlib.util.spec_from_file_location("vc_baseline_shape_compiler", COMPILER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_body(vc_bash_block: str, allowed_paths: list[str]) -> str:
    allowed_paths_block = "\n".join(f"- {p}" for p in allowed_paths)
    return (
        "## Verification Commands\n"
        "\n"
        "```bash\n"
        f"{vc_bash_block}\n"
        "```\n"
        "\n"
        "## Allowed Paths\n"
        f"{allowed_paths_block}\n"
    )


# ---------------------------------------------------------------------------
# AC1: -k new test name on existing file -> rewrite
# ---------------------------------------------------------------------------


def test_ac1_dash_k_new_test_name_rewritten(tmp_path: Path):
    """GIVEN existing_test_file.py -k test_new_name (bare identifier, not in file) /
    WHEN compiled / THEN a missing-file node-id rewrite hint is produced."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    candidate = "some_dir/test_existing_new_test.py"
    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py -k test_new_name",
        allowed_paths=["some_dir/test_existing.py", candidate],
    )

    result = compiler.compile_body(body, tmp_path)

    assert result["schema"] == "vc_baseline_shape_compiler/v1"
    assert result["status"] == "changed"
    assert len(result["rewrites"]) == 1
    rw = result["rewrites"][0]
    assert rw["reason_code"] == "pytest_dash_k_new_test_on_existing_file"
    assert candidate in rw["suggested_command"]
    assert "::test_new_name" in rw["suggested_command"]
    assert "-k" not in rw["suggested_command"]


def test_ac1_dash_k_on_existing_test_name_is_not_rewritten(tmp_path: Path):
    """GIVEN -k selects a test name that already exists in the file /
    WHEN compiled / THEN no rewrite is produced (out of scope, not a baseline-fail shape)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py -k test_alpha",
        allowed_paths=["some_dir/test_existing.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "already_canonical"
    assert result["rewrites"] == []


# ---------------------------------------------------------------------------
# AC2: missing node-id on existing file -> rewrite
# ---------------------------------------------------------------------------


def test_ac2_missing_node_id_on_existing_file_rewritten(tmp_path: Path):
    """GIVEN existing_test_file.py::test_missing_name (simple function, not in file, verified
    via ast.parse) / WHEN compiled / THEN a missing-file node-id rewrite hint is produced."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    candidate = "some_dir/test_existing_new_test.py"
    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py::test_missing_name",
        allowed_paths=["some_dir/test_existing.py", candidate],
    )

    result = compiler.compile_body(body, tmp_path)

    assert result["status"] == "changed"
    assert len(result["rewrites"]) == 1
    rw = result["rewrites"][0]
    assert rw["reason_code"] == "pytest_missing_node_id_on_existing_file"
    assert rw["suggested_command"].endswith(f"{candidate}::test_missing_name")


def test_baseline_expect_fail_preserves_original_node_id(tmp_path: Path):
    """Issue #1434: explicit fail annotation preserves an intentional existing-file node-id."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")
    original_command = "pytest some_dir/test_existing.py::test_missing_name"

    direct_result = compiler.classify_pytest_command(
        original_command,
        tmp_path,
        {"some_dir/test_existing.py"},
        set(),
        baseline_expect="fail",
    )
    assert direct_result == {"status": compiler.STATUS_ALREADY_CANONICAL}

    body = _make_body(
        vc_bash_block=f"# baseline-expect: fail\n$ {original_command}",
        allowed_paths=["some_dir/test_existing.py"],
    )
    result = compiler.compile_body(body, tmp_path)

    assert result["status"] == "already_canonical"
    assert result["rewrites"] == []
    assert result["warnings"] == []


@pytest.mark.parametrize(
    "transparent_markers",
    [
        "# AC1\n# preflight-scope: runtime_only\n# vc-role: regression",
        "# preflight-scope: unsupported_scope",
    ],
)
def test_baseline_expect_fail_uses_shared_transparent_marker_grammar(
    tmp_path: Path, transparent_markers: str
):
    """A declared fail annotation survives only shared-parser transparent markers."""
    compiler = _load_compiler()
    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block=(
            "# baseline-expect: fail\n"
            f"{transparent_markers}\n"
            "$ pytest some_dir/test_existing.py::test_missing_name"
        ),
        allowed_paths=["some_dir/test_existing.py"],
    )

    assert compiler.compile_body(body, tmp_path)["status"] == "already_canonical"


@pytest.mark.parametrize(
    "scope_boundary",
    [
        "",
        "$ printf 'another command\\n'",
        "# AC1: invalid suffix",
        "# AC1 unexpected suffix",
        "# vc-role:",
    ],
)
def test_baseline_expect_fail_does_not_cross_shared_parser_scope_boundaries(
    tmp_path: Path, scope_boundary: str
):
    """Blank, command, and invalid marker boundaries revoke preservation."""
    compiler = _load_compiler()
    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")
    candidate = "some_dir/test_existing_new_test.py"

    body = _make_body(
        vc_bash_block=(
            "# baseline-expect: fail\n"
            f"{scope_boundary}\n"
            "$ pytest some_dir/test_existing.py::test_missing_name"
        ),
        allowed_paths=["some_dir/test_existing.py", candidate],
    )
    result = compiler.compile_body(body, tmp_path)

    assert result["status"] == "changed"
    assert result["rewrites"][0]["reason_code"] == compiler.REASON_MISSING_NODE_ID


@pytest.mark.parametrize("invalid_annotation", ["# baseline-expect: invalid", "# baseline-expect:"])
def test_invalid_or_empty_baseline_expect_does_not_preserve_node_id(
    tmp_path: Path, invalid_annotation: str
):
    """Invalid shared-parser values are not equivalent to `baseline-expect: fail`."""
    compiler = _load_compiler()
    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")
    candidate = "some_dir/test_existing_new_test.py"

    body = _make_body(
        vc_bash_block=(
            f"{invalid_annotation}\n"
            "$ pytest some_dir/test_existing.py::test_missing_name"
        ),
        allowed_paths=["some_dir/test_existing.py", candidate],
    )

    assert compiler.compile_body(body, tmp_path)["status"] == "changed"


@pytest.mark.parametrize(
    "node_id",
    [
        "TestFeature::test_missing_method",
        "test_missing_case[param]",
    ],
)
def test_baseline_expect_fail_preserves_complex_existing_file_node_ids(
    tmp_path: Path, node_id: str
):
    """The explicit escape hatch applies before the compiler's narrow node-id heuristic."""
    compiler = _load_compiler()
    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block=(
            "# baseline-expect: fail\n"
            f"$ pytest some_dir/test_existing.py::{node_id}"
        ),
        allowed_paths=["some_dir/test_existing.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "already_canonical"
    assert result["rewrites"] == []


@pytest.mark.parametrize(
    "node_id",
    [
        "TestFeature::test_missing_method",
        "test_missing_case[param]",
    ],
)
def test_annotation_free_complex_existing_file_node_ids_remain_not_autofixable(
    tmp_path: Path, node_id: str
):
    """Without an explicit valid annotation, complex node ids are never accepted."""
    compiler = _load_compiler()
    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block=f"$ pytest some_dir/test_existing.py::{node_id}",
        allowed_paths=["some_dir/test_existing.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


def test_ac2_existing_node_id_is_not_rewritten(tmp_path: Path):
    """GIVEN existing_test_file.py::test_alpha where test_alpha genuinely exists /
    WHEN compiled / THEN no rewrite (out of scope)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py::test_alpha",
        allowed_paths=["some_dir/test_existing.py"],
    )
    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "already_canonical"
    assert result["rewrites"] == []


# ---------------------------------------------------------------------------
# AC3: not_autofixable — complex -k expression / complex node-id
# ---------------------------------------------------------------------------


def test_ac3_not_autofixable_complex_expression(tmp_path: Path):
    """GIVEN a boolean -k expression (`test_a or test_b`) on an existing file /
    WHEN compiled / THEN not_autofixable (body untouched, no rewrite)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_a():\n    pass\n\n\ndef test_b():\n    pass\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block='$ pytest some_dir/test_existing.py -k "test_a or test_b"',
        allowed_paths=["some_dir/test_existing.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []
    assert len(result["warnings"]) == 1
    assert "pytest_dash_k_complex_expression" in result["warnings"][0]


def test_ac3_not_autofixable_class_selector_node_id(tmp_path: Path):
    """GIVEN a class::method node-id on an existing file /
    WHEN compiled / THEN not_autofixable (class/method selectors are out of scope)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text(
        "class TestFoo:\n    def test_bar(self):\n        pass\n", encoding="utf-8"
    )

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py::TestFoo::test_bar_missing",
        allowed_paths=["some_dir/test_existing.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


def test_ac3_not_autofixable_parametrized_node_id(tmp_path: Path):
    """GIVEN a parametrized node-id ([...]) on an existing file /
    WHEN compiled / THEN not_autofixable."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    pass\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py::test_alpha[case1]",
        allowed_paths=["some_dir/test_existing.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


def test_ac3_not_autofixable_when_no_safe_candidate_in_allowed_paths(tmp_path: Path):
    """GIVEN a rewritable shape but no candidate missing-file path listed in Allowed Paths /
    WHEN compiled / THEN not_autofixable (never invent a path outside Allowed Paths)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    pass\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py -k test_new_name",
        allowed_paths=["some_dir/test_existing.py"],  # no missing-file candidate listed
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


# ---------------------------------------------------------------------------
# AC4: missing_new_test_file.py::test_name -> already_canonical
# ---------------------------------------------------------------------------


def test_ac4_missing_file_node_id_already_canonical(tmp_path: Path):
    """GIVEN missing_new_test_file.py::test_name where the file does not exist on baseline /
    WHEN compiled / THEN already_canonical (no rewrite, hygiene autofix is a no-op)."""
    compiler = _load_compiler()

    body = _make_body(
        vc_bash_block="$ pytest some_dir/missing_new_test_file.py::test_name",
        allowed_paths=["some_dir/missing_new_test_file.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "already_canonical"
    assert result["rewrites"] == []
    assert result["warnings"] == []


def test_invalid_input_missing_vc_section(tmp_path: Path):
    """GIVEN a body without ## Verification Commands / WHEN compiled / THEN invalid_input."""
    compiler = _load_compiler()
    result = compiler.compile_body("## Outcome\nsomething\n", tmp_path)
    assert result["status"] == "invalid_input"
    assert result["errors"]


# ---------------------------------------------------------------------------
# Blocker 2 (PR #1305 review): Allowed Paths "directory allow" semantics —
# trailing-slash entries must be treated as a directory prefix, matching
# allowed_paths_review_gate.py's AllowedPathsMatcher, not bare string equality.
# ---------------------------------------------------------------------------


def test_allowed_paths_directory_prefix_allows_candidate(tmp_path: Path):
    """GIVEN Allowed Paths=['some_dir/'] (trailing-slash directory allow) /
    WHEN a candidate missing-file path lives under that directory /
    THEN the compiler treats it as allowed and emits a rewrite (changed)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py -k test_new_name",
        allowed_paths=["some_dir/"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "changed"
    assert len(result["rewrites"]) == 1
    assert "some_dir/test_existing_new_test.py::test_new_name" in result["rewrites"][0]["suggested_command"]


def test_allowed_paths_directory_prefix_rejects_outside_directory(tmp_path: Path):
    """GIVEN Allowed Paths=['other_dir/'] (a different directory prefix) /
    WHEN the candidate missing-file path lives under `some_dir/` /
    THEN it is not_autofixable (never invent a path outside Allowed Paths)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py -k test_new_name",
        allowed_paths=["other_dir/"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


# ---------------------------------------------------------------------------
# Blocker 5 (PR #1305 review): unsupported/ambiguous pytest CLI syntax must
# fail closed to not_autofixable rather than being silently mis-parsed.
# ---------------------------------------------------------------------------


def test_unsupported_cli_syntax_value_taking_flag_not_autofixable(tmp_path: Path):
    """GIVEN a pytest invocation using a value-taking flag this narrow parser
    does not understand (`--rootdir <path>`) / WHEN compiled / THEN
    not_autofixable (fail closed instead of mis-parsing the path arg)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest --rootdir some_dir some_dir/test_existing.py -k test_new_name",
        allowed_paths=["some_dir/test_existing.py", "some_dir/test_existing_new_test.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []
    assert "pytest_unsupported_cli_syntax" in result["warnings"][0]


def test_unsupported_cli_syntax_multiple_path_args_not_autofixable(tmp_path: Path):
    """GIVEN a pytest invocation with two positional path arguments /
    WHEN compiled / THEN not_autofixable (multiple test paths are outside
    this compiler's understood shape)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")
    other = pkg_dir / "test_other.py"
    other.write_text("def test_beta():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py some_dir/test_other.py -k test_new_name",
        allowed_paths=["some_dir/test_existing.py", "some_dir/test_other.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


# ---------------------------------------------------------------------------
# Blocker 6 (PR #1305 review): missing-file node-id must also be validated
# against the simple top-level function node-id shape; a missing file with a
# class/method or parametrized selector is not a canonical shape.
# ---------------------------------------------------------------------------


def test_missing_file_complex_node_id_not_autofixable(tmp_path: Path):
    """GIVEN missing_file.py::TestClass::test_method (missing file, but a
    class/method selector, not a simple top-level function node-id) /
    WHEN compiled / THEN not_autofixable, NOT already_canonical."""
    compiler = _load_compiler()

    body = _make_body(
        vc_bash_block="$ pytest some_dir/missing_new_test_file.py::TestClass::test_method",
        allowed_paths=["some_dir/missing_new_test_file.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


def test_missing_file_parametrized_node_id_not_autofixable(tmp_path: Path):
    """GIVEN missing_file.py::test_name[case1] (missing file, parametrized
    selector) / WHEN compiled / THEN not_autofixable, NOT already_canonical."""
    compiler = _load_compiler()

    body = _make_body(
        vc_bash_block="$ pytest some_dir/missing_new_test_file.py::test_name[case1]",
        allowed_paths=["some_dir/missing_new_test_file.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


# ---------------------------------------------------------------------------
# AC5: issue_contract_hygiene_autofix.py wiring + sha256 no_change on rerun
# ---------------------------------------------------------------------------


def _run_autofix(body: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(AUTOFIX_SCRIPT)],
        input=body,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def test_ac5_hygiene_autofix_applies_vc_shape_then_no_change():
    """GIVEN a contract body with a forbidden -k VC shape on a real repo test file /
    WHEN issue_contract_hygiene_autofix.py runs / THEN it rewrites the VC line to
    canonical form, and running it again on the repaired body is a sha256 no_change."""
    # Candidate must be a real (currently nonexistent) repo-relative path so the
    # compiler's "missing baseline file" safety check passes against the real repo tree.
    existing_repo_file = "some_dir_ac5_probe/test_existing_ac5_probe.py"
    candidate = "some_dir_ac5_probe/test_existing_ac5_probe_new_test.py"

    # Create the "existing" file relative to the real repo root (parents[3] of the
    # autofix script, i.e. .claude/skills/edit-issue/scripts/../../../..).
    repo_root = AUTOFIX_SCRIPT.parents[4]
    probe_dir = repo_root / "some_dir_ac5_probe"
    probe_dir.mkdir(exist_ok=True)
    probe_file = probe_dir / "test_existing_ac5_probe.py"
    probe_file.write_text("def test_alpha():\n    pass\n", encoding="utf-8")
    try:
        body = textwrap.dedent(
            f"""\
            ## Outcome
            Test outcome.

            ## Acceptance Criteria
            - [ ] AC1: file exists

            ## Stop Conditions
            - エラー時は停止する。

            ## Verification Commands

            ```bash
            # AC1
            $ pytest {existing_repo_file} -k test_new_ac5_probe
            ```

            ## Allowed Paths
            - {existing_repo_file}
            - {candidate}

            ## Runtime Verification Applicability
            ```yaml
            decision: not_applicable
            reason: "test"
            ```

            ## Delivery Rule
            1 Issue = 1 PR
            """
        )

        code1, out1, err1 = _run_autofix(body)
        assert code1 == 0, f"Expected exit 0 (repaired), got {code1}. stderr={err1}"
        assert candidate in out1
        assert "::test_new_ac5_probe" in out1
        assert "-k" not in out1.split("## Allowed Paths")[0].split("Verification Commands")[1]

        # Second run on the repaired body: sha256 guard -> no_change (exit 1)
        code2, out2, err2 = _run_autofix(out1)
        assert code2 == 1, f"Expected exit 1 (no_change) on 2nd run, got {code2}. stderr={err2}"
        assert "no_change" in err2
    finally:
        probe_file.unlink(missing_ok=True)
        try:
            probe_dir.rmdir()
        except OSError:
            pass


def test_parse_allowed_paths_no_path_marker_returns_empty():
    """AC12 (Issue #2783): canonical marker `(none)` and legacy marker
    `読み取り専用。リポジトリ変更なし（既定）` as the sole Allowed Paths entry
    both resolve to [] (previously this module had no annotation stripping
    at all, so both markers leaked as literal non-empty "path" strings)."""
    compiler = _load_compiler()

    lines_canonical = "## Allowed Paths\n\n- (none)\n".split("\n")
    assert compiler.parse_allowed_paths(lines_canonical) == []

    lines_legacy = "## Allowed Paths\n\n- 読み取り専用。リポジトリ変更なし（既定）\n".split("\n")
    assert compiler.parse_allowed_paths(lines_legacy) == []


# ---------------------------------------------------------------------------
# Issue #3020: canonical preflight-scope markers are preserved (never rewritten)
# ---------------------------------------------------------------------------


def _write_existing_test(tmp_path: Path) -> tuple[str, str]:
    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    (pkg_dir / "test_existing.py").write_text("def test_alpha():\n    assert True\n", encoding="utf-8")
    return "some_dir/test_existing.py", "some_dir/test_existing_new_test.py"


@pytest.mark.parametrize("scope", ["pr_review_only", "runtime_only"])
def test_scope_marker_canonical_pytest_vc_is_preserved(tmp_path: Path, scope: str):
    """GIVEN a pytest VC for a new -k test on an existing file carrying a canonical
    `# preflight-scope: <scope>` marker / WHEN compile_body runs / THEN it is not a
    rewrite target (no rewrites, no not_autofixable warning)."""
    compiler = _load_compiler()
    existing, candidate = _write_existing_test(tmp_path)
    body = _make_body(
        vc_bash_block=(
            f"# AC1\n# preflight-scope: {scope}\n$ pytest {existing} -k test_new_name"
        ),
        allowed_paths=[existing, candidate],
    )

    result = compiler.compile_body(body, tmp_path)

    assert result["status"] == "already_canonical"
    assert result["rewrites"] == []
    assert result["warnings"] == []


def test_scope_marker_complex_k_expression_is_preserved(tmp_path: Path):
    """GIVEN the exact Issue #3020 shape (`-k "a and b"` with pr_review_only) /
    WHEN compile_body runs / THEN no not_autofixable warning is raised and the VC is preserved."""
    compiler = _load_compiler()
    existing, candidate = _write_existing_test(tmp_path)
    body = _make_body(
        vc_bash_block=(
            "# AC2\n# preflight-scope: pr_review_only\n"
            f'$ uv run --locked pytest {existing} -q -k "scope_marker and preserved"'
        ),
        allowed_paths=[existing, candidate],
    )

    result = compiler.compile_body(body, tmp_path)

    assert result["status"] == "already_canonical"
    assert result["rewrites"] == []
    assert result["warnings"] == []


def test_scope_marker_preserved_in_mixed_body_only_unmarked_vc_rewritten(tmp_path: Path):
    """GIVEN a marked VC and an unmarked rewritable VC in one body / WHEN compile_body
    runs / THEN only the unmarked VC is rewritten (conventional behavior is kept) and
    the marked VC contributes neither a rewrite nor a warning."""
    compiler = _load_compiler()
    existing, candidate = _write_existing_test(tmp_path)
    body = _make_body(
        vc_bash_block=(
            "# AC1\n# preflight-scope: pr_review_only\n"
            f'$ pytest {existing} -k "test_a or test_b"\n'
            "# AC2\n"
            f"$ pytest {existing} -k test_new_name"
        ),
        allowed_paths=[existing, candidate],
    )

    result = compiler.compile_body(body, tmp_path)

    assert result["status"] == "changed"
    assert result["warnings"] == []
    assert len(result["rewrites"]) == 1
    assert result["rewrites"][0]["original_command"] == f"pytest {existing} -k test_new_name"


def test_scope_marker_unknown_or_detached_value_is_not_preserved(tmp_path: Path):
    """GIVEN an unknown scope value, or a canonical marker separated from the command by
    another line / WHEN compile_body runs / THEN the shared immediate-previous-line
    semantics apply and the VC is still a normal rewrite target."""
    compiler = _load_compiler()
    existing, candidate = _write_existing_test(tmp_path)
    for block in (
        f"# preflight-scope: bogus_scope\n$ pytest {existing} -k test_new_name",
        f"# preflight-scope: pr_review_only\n# AC1\n$ pytest {existing} -k test_new_name",
    ):
        body = _make_body(vc_bash_block=block, allowed_paths=[existing, candidate])
        result = compiler.compile_body(body, tmp_path)
        assert result["status"] == "changed", block
        assert len(result["rewrites"]) == 1, block


def test_scope_marker_preserved_reuses_shared_parser_not_local_regex():
    """GIVEN the compiler module / THEN the scope marker is extracted through the shared
    vc_contract_syntax parser (no compiler-local scope regex is introduced)."""
    compiler = _load_compiler()
    import vc_contract_syntax  # imported via the compiler's sys.path insertion

    assert compiler.parse_preflight_scope_marker_line is vc_contract_syntax.parse_preflight_scope_marker_line
    lines = ["# preflight-scope: runtime_only\n", "$ pytest a.py\n"]
    assert compiler.extract_scope_marker_for_command(lines, 1) == "runtime_only"
    assert compiler.extract_scope_marker_for_command(lines, 0) is None
