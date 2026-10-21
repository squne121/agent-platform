"""
test_function_definition_order.py

Regression tests for Issue #1334: run_refinement_preflight.py used to define
`_build_scope_delta_authority_evidence` AFTER both its first call site and the
`if __name__ == "__main__":` trigger, causing a guaranteed `NameError` at
script execution time (the module-level `if __name__ == "__main__": main()`
line ran `main()` -> `run_preflight()` -> `_build_scope_delta_authority_evidence(...)`
before the module had reached the `def _build_scope_delta_authority_evidence`
statement further down in the file).

VC rg keywords verified in this file:
  - test_definition_before_first_call  (AC1)
  - test_definition_before_main_trigger (AC2)
  - test_direct_cli_anchor_comment_path_has_no_nameerror (AC3)

Note on the direct CLI subprocess test (PR #1339 human review, Scope Delta):
A prior revision of this file added
`test_direct_cli_anchor_comment_path_has_no_nameerror`, but it was found to be
vacuous and removed: `anchor_comments` items accepted by
`refinement_preflight_input.schema.json` were restricted to `id` / `body` /
`issue_url` only (`additionalProperties: false`), while
`_build_anchor_comment_state()` normalizes those items into an
`anchor_comment` record that requires several additional non-null string
fields (`html_url`, `api_url`, `user_login`, `author_association`,
`comment_created_at`, `comment_updated_at`) per
`loop_state.schema.json#/definitions/anchor_comment`. Because the input
schema could not carry those fields, any schema-conformant fixture was
guaranteed to fail `_build_anchor_comment_state()`'s normalization schema
check (`ANCHOR_COMMENT_SCHEMA_INVALID`) before the call chain ever reached
`_build_scope_delta_authority_evidence`.

This was resolved (human-approved Scope Delta) by extending
`refinement_preflight_input.schema.json`'s `anchor_comments` item schema with
optional `html_url` / `url` / `user` / `author_association` / `created_at` /
`updated_at` fields -- a backward-compatible extension (all new fields are
optional; existing minimal `id`/`body`/`issue_url` fixtures remain valid, and
`_load_schema`-mocked tests such as
`test_anchor_comment_preflight_normalization.py` are unaffected since they
bypass input-schema validation entirely). The fixture below
(`_make_direct_cli_fixture`) now supplies all of those fields, so the
subprocess invocation below reaches `_build_anchor_comment_state()` ->
(schema-valid normalization) -> `_build_scope_delta_authority_evidence(...)`
for real, under genuine `__name__ == "__main__"` conditions.

Self-verification performed for this reintroduction:
  - Running the real (fixed) script against the schema-conformant fixture
    below produces `STATUS: pass` (or `warn`), NOT `STATUS: blocked` with
    `ANCHOR_COMMENT_SCHEMA_INVALID` -- proving the call chain actually
    reaches `_build_scope_delta_authority_evidence` this time (see
    `test_direct_cli_anchor_comment_path_reaches_target_function` below,
    which asserts `ANCHOR_COMMENT_SCHEMA_INVALID` and `INPUT_SCHEMA_INVALID`
    are both absent from stdout).
  - Running the same fixture against a deliberately broken copy of the
    script (function definition moved back after both its call site and the
    `__main__` trigger) reproduces a `NameError` / non-zero exit outside
    {0,1,2,3} -- confirming this test is an ungameable regression guard for
    the definition-order bug, not merely a schema-validation smoke test.

The two AST-based tests below (`test_definition_before_first_call`,
`test_definition_before_main_trigger`) do not depend on runtime
reachability at all -- they inspect the module's source structure directly.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
TARGET_SCRIPT = SCRIPTS_DIR / "run_refinement_preflight.py"

TARGET_FUNCTION_NAME = "_build_scope_delta_authority_evidence"


def _parse_target_ast() -> ast.Module:
    source = TARGET_SCRIPT.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(TARGET_SCRIPT))


def _find_function_def_line(tree: ast.Module, name: str) -> int:
    """Return the line number of the top-level `def <name>(...)` statement."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node.lineno
    raise AssertionError(f"function definition not found: {name}")


def _find_first_call_line(tree: ast.Module, name: str) -> int:
    """Return the line number of the first `name(...)` call anywhere in the module."""
    call_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
            call_lines.append(node.lineno)
    if not call_lines:
        raise AssertionError(f"no call site found for: {name}")
    return min(call_lines)


def _find_main_trigger_line(tree: ast.Module) -> int:
    """Return the line number of the top-level `if __name__ == "__main__":` statement."""
    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            ):
                return node.lineno
    raise AssertionError('no top-level `if __name__ == "__main__":` trigger found')


def test_definition_before_first_call() -> None:
    """GIVEN run_refinement_preflight.py
    WHEN the module is parsed via AST
    THEN the def line of _build_scope_delta_authority_evidence must come before
    its first call site's line number (else the call would raise NameError when
    executed as a module-level side effect before the def is reached).
    """
    tree = _parse_target_ast()
    def_line = _find_function_def_line(tree, TARGET_FUNCTION_NAME)
    call_line = _find_first_call_line(tree, TARGET_FUNCTION_NAME)
    assert def_line < call_line, (
        f"{TARGET_FUNCTION_NAME} def (line {def_line}) must be defined before its "
        f"first call site (line {call_line})"
    )


def test_definition_before_main_trigger() -> None:
    """GIVEN run_refinement_preflight.py
    WHEN the module is parsed via AST
    THEN the def line of _build_scope_delta_authority_evidence must come before
    the `if __name__ == "__main__":` trigger, so the function is always defined
    by the time `main()` could possibly invoke it.
    """
    tree = _parse_target_ast()
    def_line = _find_function_def_line(tree, TARGET_FUNCTION_NAME)
    main_trigger_line = _find_main_trigger_line(tree)
    assert def_line < main_trigger_line, (
        f"{TARGET_FUNCTION_NAME} def (line {def_line}) must be defined before the "
        f'`if __name__ == "__main__":` trigger (line {main_trigger_line})'
    )


def test_module_imports_without_nameerror() -> None:
    """GIVEN run_refinement_preflight.py
    WHEN the module is loaded via importlib.util (module-level code executed)
    THEN no NameError is raised and the target function is defined on the module.
    """
    spec = importlib.util.spec_from_file_location(
        "run_refinement_preflight_definition_order_check", TARGET_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except NameError as exc:  # pragma: no cover - regression guard
        raise AssertionError(f"module import raised NameError: {exc}") from exc

    assert hasattr(module, TARGET_FUNCTION_NAME), (
        f"expected module attribute {TARGET_FUNCTION_NAME!r} after import"
    )


# ---------------------------------------------------------------------------
# Direct CLI subprocess regression test (PR #1339 review fix_delta, Scope Delta)
#
# The import-only test above (test_module_imports_without_nameerror) loads
# the module via importlib.util under a *different* module name, so
# `__name__ == "__main__"` never evaluates to True and the
# `if __name__ == "__main__": main()` trigger never actually runs. That means
# it never exercises `main()` -> `run_preflight()` ->
# `_build_scope_delta_authority_evidence(...)`, which is precisely the call
# chain that raised NameError in the originally reported bug (a real
# `python3 run_refinement_preflight.py --anchor-comment-url ...` invocation).
#
# This test instead launches the script as a real subprocess (so `__name__`
# really is `"__main__"`), using `--fixture` + `--anchor-comment-url` so the
# anchor-comment code path is exercised deterministically without any
# GitHub API / `gh` dependency. The fixture below is schema-conformant under
# the extended `refinement_preflight_input.schema.json` (Scope Delta), so it
# actually reaches `_build_scope_delta_authority_evidence` instead of being
# rejected earlier by `ANCHOR_COMMENT_SCHEMA_INVALID`.
# ---------------------------------------------------------------------------

_DIRECT_CLI_ISSUE_NUMBER = 99991334
_DIRECT_CLI_REPO = "testowner/testrepo"
_DIRECT_CLI_COMMENT_ID = 88881334
_DIRECT_CLI_ANCHOR_URL = (
    f"https://github.com/{_DIRECT_CLI_REPO}/issues/{_DIRECT_CLI_ISSUE_NUMBER}"
    f"#issuecomment-{_DIRECT_CLI_COMMENT_ID}"
)

_DIRECT_CLI_VALID_ISSUE_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#1"
```

## Parent Issue

#1

## Parent Goal Ref

- Goal: Test goal

## Current Validated Scope

- scripts/example.py

## Remaining Parent Gaps

- [ ] Nothing remaining

## Outcome

Add `scripts/example.py`.

## In Scope

- scripts/example.py

## Out of Scope

- Unrelated changes

## Acceptance Criteria

- [ ] AC1: Script exists.

## Verification Commands

```bash
uv run python3 scripts/example.py
```

## Allowed Paths

- scripts/example.py

## Stop Conditions

- Allowed Paths 外の変更が必要な場合

## Required Skills

なし
"""


def _repo_root_for_test() -> Path:
    """Walk up from TARGET_SCRIPT to find the .git root (mirrors the
    wrapper's own `_find_repo_root` so the test can locate/clean up the
    artifact directory the real subprocess run writes to)."""
    current = TARGET_SCRIPT.resolve().parent
    for _ in range(10):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise AssertionError("could not locate repo root from TARGET_SCRIPT")


def _make_direct_cli_fixture() -> dict:
    return {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": _DIRECT_CLI_ISSUE_NUMBER,
        "repo": _DIRECT_CLI_REPO,
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {
            "number": _DIRECT_CLI_ISSUE_NUMBER,
            "title": "Direct CLI regression fixture (#1334 / PR #1339)",
            "body": _DIRECT_CLI_VALID_ISSUE_BODY,
            "labels": [],
        },
        "comments": [],
        "anchor_comment_urls": [_DIRECT_CLI_ANCHOR_URL],
        "anchor_comments": [
            {
                "id": _DIRECT_CLI_COMMENT_ID,
                "body": "Freeform human review comment exercising the anchor path.",
                "issue_url": (
                    f"https://api.github.com/repos/{_DIRECT_CLI_REPO}/issues/"
                    f"{_DIRECT_CLI_ISSUE_NUMBER}"
                ),
                "author_association": "OWNER",
                "user": {"login": "reviewer", "type": "User"},
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "html_url": (
                    f"https://github.com/{_DIRECT_CLI_REPO}/issues/"
                    f"{_DIRECT_CLI_ISSUE_NUMBER}#issuecomment-{_DIRECT_CLI_COMMENT_ID}"
                ),
                "url": (
                    f"https://api.github.com/repos/{_DIRECT_CLI_REPO}/issues/comments/"
                    f"{_DIRECT_CLI_COMMENT_ID}"
                ),
            }
        ],
    }


def _run_direct_cli(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    fixture_path = tmp_path / "direct_cli_fixture.json"
    fixture_path.write_text(
        json.dumps(_make_direct_cli_fixture()), encoding="utf-8"
    )

    repo_root = _repo_root_for_test()
    artifact_dir = (
        repo_root
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / str(_DIRECT_CLI_ISSUE_NUMBER)
    )

    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(TARGET_SCRIPT),
                "--issue-number",
                str(_DIRECT_CLI_ISSUE_NUMBER),
                "--repo",
                _DIRECT_CLI_REPO,
                "--fixture",
                str(fixture_path),
                "--anchor-comment-url",
                _DIRECT_CLI_ANCHOR_URL,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir, ignore_errors=True)
    return proc


def test_direct_cli_anchor_comment_path_has_no_nameerror(tmp_path) -> None:
    """GIVEN run_refinement_preflight.py invoked as a real `python3 <script>`
    subprocess (not imported) with `--fixture` + `--anchor-comment-url`
    WHEN the anchor-comment code path runs (which calls
    `_build_scope_delta_authority_evidence`)
    THEN stdout/stderr must not contain a `NameError` or `Traceback`, and the
    process must exit with one of the wrapper's own documented exit codes
    (0=pass, 1=warn, 2=blocked, 3=environment_failure) rather than the
    generic exit code 1 Python uses for an uncaught exception.
    """
    proc = _run_direct_cli(tmp_path)
    combined_output = (proc.stdout or "") + (proc.stderr or "")

    assert "NameError" not in combined_output, (
        "direct CLI invocation raised NameError "
        f"(exit={proc.returncode}):\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "Traceback" not in combined_output, (
        "direct CLI invocation produced a Traceback "
        f"(exit={proc.returncode}):\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert proc.returncode in (0, 1, 2, 3), (
        f"unexpected exit code {proc.returncode}; the wrapper only ever exits "
        "0 (pass) / 1 (warn) / 2 (blocked) / 3 (environment_failure) -- a "
        "code outside this range signals an unhandled interpreter-level "
        f"exception; stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "STATUS:" in proc.stdout, (
        "expected the wrapper's compact STATUS: stdout projection to be "
        f"printed; stdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_direct_cli_anchor_comment_path_reaches_target_function(tmp_path) -> None:
    """GIVEN the schema-conformant direct CLI fixture above
    WHEN run_refinement_preflight.py actually runs the anchor-comment path
    THEN the output must NOT be blocked by ANCHOR_COMMENT_SCHEMA_INVALID or
    INPUT_SCHEMA_INVALID -- proving the call chain really reaches
    `_build_scope_delta_authority_evidence` (the NameError call site) rather
    than failing earlier in schema normalization (which would make this test
    vacuous with respect to the #1334 regression, identically whether or not
    the definition-order bug is present).
    """
    proc = _run_direct_cli(tmp_path)
    assert "ANCHOR_COMMENT_SCHEMA_INVALID" not in proc.stdout, (
        "fixture unexpectedly failed anchor_comment normalization -- this "
        "would make the NameError regression guard above vacuous; "
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "INPUT_SCHEMA_INVALID" not in proc.stdout, (
        "fixture unexpectedly failed refinement_preflight_input schema "
        f"validation; stdout={proc.stdout}\nstderr={proc.stderr}"
    )
