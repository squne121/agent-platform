"""Stage 2-3: edit-issue guard / hygiene layer + vc_baseline_shape_compiler (closure + controls).

Fixture evidence only: every subprocess runs a migrated script against synthetic bodies in a
temporary layout; no network, no `gh`, no real GitHub write. Nothing here is live GitHub evidence.

Pinned here (the rest of the lane is pinned by the moved source tests):
  AC1  isolated import (`python -I`), canonical-layout success vs layout-shift failure, no legacy
       repository identity in the moved files.
  AC2  compiler missing / unreadable / invalid -> hygiene exits 2 (fail-closed), never 0/1.
  AC3  hygiene fail-closed on a missing/crashing/hanging/garbage contract checker; C4/C9
       idempotence and SHA consistency; non-runtime classification boundaries; the compiler
       producer and hygiene consumer agree on the rewritten line.
  AC4  manifest facts (counts, parity hashes, deferrals) and the no-skip rule are re-derived from
       the files on disk, not trusted from prose.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / ".claude" / "skills"
GUARD = SKILLS / "edit-issue" / "scripts" / "guard-issue-body.py"
HYGIENE = SKILLS / "edit-issue" / "scripts" / "issue_contract_hygiene_autofix.py"
COMPILER = SKILLS / "issue-contract-review" / "scripts" / "vc_baseline_shape_compiler.py"
MANIFEST = ROOT / "docs" / "dev" / "stage2-3-edit-guards-manifest.json"
MOVED_TESTS = [
    SKILLS / "edit-issue" / "tests" / "test_guard_issue_body.py",
    SKILLS / "edit-issue" / "tests" / "test_guard_ac_vc_alignment.py",
    SKILLS / "edit-issue" / "tests" / "test_issue_contract_hygiene_autofix.py",
    SKILLS / "issue-contract-review" / "tests" / "test_pytest_vc_baseline_shape_compiler.py",
]
HOME_PREFIX = "/" + "home" + "/"
LEGACY_SLUG = "/".join(["squne121", "loop-protocol"])  # noqa: FLY002 - no literal for the slug scan


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- layout helpers

_LAYOUT_DIRS = (
    ".claude/skills/edit-issue/scripts",
    ".claude/skills/issue-contract-review/scripts",
    ".claude/skills/review-issue/scripts",
    ".claude/skills/create-issue/scripts",
    "scripts/agent-ops",
)


def _build_layout(
    dest: Path, *, stub_checker: str | None = None, drop: tuple[str, ...] = ()
) -> Path:
    """Copy the migrated script dirs into `dest` at the canonical depth (repo_root = parents[4])."""
    for rel in _LAYOUT_DIRS:
        shutil.copytree(
            ROOT / rel,
            dest / rel,
            ignore=shutil.ignore_patterns("__pycache__", "tests", "fixtures"),
        )
    # the guard resolves its issue templates from a `.git` ancestor
    (dest / ".git").mkdir()
    shutil.copytree(ROOT / ".github" / "ISSUE_TEMPLATE", dest / ".github" / "ISSUE_TEMPLATE")
    if stub_checker is not None:
        (dest / ".claude/skills/review-issue/scripts/check_issue_contract.py").write_text(
            stub_checker, encoding="utf-8"
        )
    for rel in drop:
        (dest / rel).unlink()
    return dest


def _hygiene_in(layout: Path) -> Path:
    return layout / ".claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py"


STUB_OK = "import json\nprint(json.dumps({'blocking_issues': []}))\n"

_RVA = textwrap.dedent("""\
    ## Runtime Verification Applicability
    ```yaml
    decision: not_applicable
    reason: "test"
    ```
""")


def _body(vc: str, allowed: str, rva: str = "") -> str:
    return "\n".join(
        [
            "## Outcome\nTest outcome.\n",
            "## Acceptance Criteria\n- [ ] AC1: file exists\n",
            "## Stop Conditions\n- stop on error.\n",
            f"## Verification Commands\n\n```bash\n{vc}\n```\n",
            f"## Allowed Paths\n{allowed}\n",
            rva,
            "## Delivery Rule\n1 Issue = 1 PR\n",
        ]
    )


C4_BODY = _body("# AC1\ntest -f docs/dev/x.md", "- docs/dev/x.md\n", _RVA)
C9_BODY = _body("# AC1\n$ test -f docs/dev/x.md", "- docs/dev/x.md\n- scripts/y.py\n")


def _hygiene(layout: Path, body: str):
    return _run([sys.executable, "-I", str(_hygiene_in(layout))], input=body)


@pytest.fixture()
def layout(tmp_path: Path) -> Path:
    return _build_layout(tmp_path / "ok", stub_checker=STUB_OK)


# --------------------------------------------------------------------------- AC1: import / layout


@pytest.mark.parametrize("script", [GUARD, HYGIENE, COMPILER], ids=lambda p: p.name)
def test_isolated_import_without_pytest_state(script: Path):
    """`python -I` (no cwd / PYTHONPATH / site leakage) imports each script by path."""
    code = (
        "import importlib.util,sys;"
        "s=importlib.util.spec_from_file_location('m',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print('IMPORT_OK')"
    )
    r = _run(
        [sys.executable, "-I", "-W", "ignore::SyntaxWarning", "-c", code, str(script)], cwd="/"
    )
    assert r.returncode == 0, r.stderr
    assert "IMPORT_OK" in r.stdout


@pytest.mark.parametrize("script", [GUARD, HYGIENE, COMPILER], ids=lambda p: p.name)
def test_canonical_layout_copy_runs(tmp_path: Path, script: Path):
    lay = _build_layout(tmp_path / "ok", stub_checker=STUB_OK)
    moved = lay / script.relative_to(ROOT)
    r = _run([sys.executable, "-I", str(moved), "--help"])
    assert r.returncode == 0, r.stderr
    assert "usage" in r.stdout.lower()


@pytest.mark.parametrize("script", [GUARD, HYGIENE, COMPILER], ids=lambda p: p.name)
def test_layout_shift_is_a_hard_failure_not_a_silent_degrade(tmp_path: Path, script: Path):
    """The scripts derive the repo root from `parents[4]`; one level shallower must not run."""
    shallow = tmp_path / "a" / "b"
    shallow.mkdir(parents=True)
    moved = shutil.copy(script, shallow / script.name)
    r = _run([sys.executable, "-I", moved, "--help"])
    assert r.returncode != 0
    assert "usage" not in r.stdout.lower()


def test_one_level_too_deep_resolves_wrong_root_and_fails(tmp_path: Path):
    lay = _build_layout(tmp_path / "ok", stub_checker=STUB_OK)
    deep = lay / ".claude/skills/edit-issue/scripts/extra"
    deep.mkdir()
    moved = shutil.copy(_hygiene_in(lay), deep / HYGIENE.name)
    r = _run([sys.executable, "-I", moved, "--help"])
    assert r.returncode != 0, (
        "a deeper copy resolves parents[4] to a dir without allowed_paths_policy"
    )


def test_no_legacy_repository_identity_in_moved_files():
    files = [GUARD, HYGIENE, COMPILER, *MOVED_TESTS, Path(__file__), MANIFEST]
    for f in files:
        text = f.read_text(encoding="utf-8")
        if f in (Path(__file__), MANIFEST):
            text = text.replace(LEGACY_SLUG, "")  # this file / manifest name the slug to forbid it
        assert LEGACY_SLUG not in text, f
        assert HOME_PREFIX not in text, f


# --------------------------------------------------------------------------- AC2: compiler loading


def test_compiler_missing_fails_closed_exit_2(tmp_path: Path):
    lay = _build_layout(
        tmp_path / "ok",
        stub_checker=STUB_OK,
        drop=(".claude/skills/issue-contract-review/scripts/vc_baseline_shape_compiler.py",),
    )
    body = _body("# AC1\n$ pytest docs/dev/test_x.py -k test_new", "- docs/dev/test_x.py\n", _RVA)
    r = _hygiene(lay, body)
    assert r.returncode == 2
    assert "vc_shape_compiler_load_failed" in r.stderr
    assert r.stdout == ""


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"def broken(:\n",
        b"raise RuntimeError('boom')\n",
        b"\xff\xfe\x00bad-bytes",
        b"def compile_body(body, root):\n    raise ValueError('x')\n",
        b"def compile_body(body, root):\n    return None\n",
        b"compile_body = 1\n",
    ],
    ids=[
        "empty",
        "syntax-error",
        "raises-at-import",
        "undecodable",
        "compile-raises",
        "non-dict",
        "not-callable",
    ],
)
def test_compiler_unloadable_or_invalid_fails_closed(tmp_path: Path, payload: bytes):
    lay = _build_layout(tmp_path / "ok", stub_checker=STUB_OK)
    (
        lay / ".claude/skills/issue-contract-review/scripts/vc_baseline_shape_compiler.py"
    ).write_bytes(payload)
    body = _body("# AC1\n$ pytest docs/dev/test_x.py -k test_new", "- docs/dev/test_x.py\n", _RVA)
    r = _hygiene(lay, body)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert r.stdout == ""


def test_compiler_load_failure_without_vc_section_is_a_safe_noop(tmp_path: Path):
    """No `## Verification Commands` heading -> nothing to canonicalize; the load is not needed."""
    lay = _build_layout(
        tmp_path / "ok",
        stub_checker=STUB_OK,
        drop=(".claude/skills/issue-contract-review/scripts/vc_baseline_shape_compiler.py",),
    )
    body = "## Outcome\nx\n\n## Allowed Paths\n- docs/dev/x.md\n"
    r = _hygiene(lay, body)
    assert "vc_shape_compiler_load_failed" not in r.stderr


def test_compiler_cli_invalid_and_missing_input_exit_2(tmp_path: Path):
    r = _run([sys.executable, "-I", str(COMPILER), "--body-file", str(tmp_path / "absent.md")])
    assert r.returncode == 2
    r = _run([sys.executable, "-I", str(COMPILER)], input="")
    assert r.returncode in (0, 2)  # empty body: never a crash traceback
    assert "Traceback" not in r.stderr


# --------------------------------------------------------------------------- AC3: hygiene controls


def test_c4_repair_is_idempotent_and_sha_consistent(layout: Path):
    r = _hygiene(layout, C4_BODY)
    assert r.returncode == 0, r.stderr
    assert "$ test -f docs/dev/x.md" in r.stdout
    m = re.search(
        r"original_sha256=([0-9a-f]{16})\.\.\.\s+new_sha256=([0-9a-f]{16})\.\.\.", r.stderr
    )
    assert m, r.stderr
    assert m.group(1) == hashlib.sha256(C4_BODY.encode()).hexdigest()[:16]
    assert m.group(2) == hashlib.sha256(r.stdout.encode()).hexdigest()[:16]
    again = _hygiene(layout, r.stdout)
    assert again.returncode == 1
    assert "no_change" in again.stderr
    assert again.stdout == ""


def test_c9_repair_inserts_rva_only_for_non_runtime_paths_then_is_idempotent(layout: Path):
    r = _hygiene(layout, C9_BODY)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("## Runtime Verification Applicability") == 1
    assert "decision: not_applicable" in r.stdout
    again = _hygiene(layout, r.stdout)
    assert again.returncode == 1


@pytest.mark.parametrize(
    "paths",
    [
        "- src/main.ts\n",
        "- docs/dev/x.md\n- tests/unit/test_a.py\n",
        "- .github/workflows/ci.yml\n",
        "- pyproject.toml\n",
        "- docs/dev/x.md\n- ../etc/passwd\n",
    ],
    ids=["runtime", "mixed-runtime", "ci-unknown", "unknown", "traversal-unknown"],
)
def test_c9_runtime_or_unknown_paths_fail_closed_with_no_output(layout: Path, paths: str):
    r = _hygiene(layout, _body("# AC1\n$ test -f docs/dev/x.md", paths))
    assert r.returncode == 2, r.stderr
    assert r.stdout == ""
    assert "C9 autofix not safe" in r.stderr


def test_c9_missing_allowed_paths_section_fails_closed(layout: Path):
    body = "## Verification Commands\n\n```bash\n$ test -f docs/dev/x.md\n```\n"
    r = _hygiene(layout, body)
    assert r.returncode == 2
    assert r.stdout == ""


def test_non_runtime_classification_boundaries():
    h = _load(HYGIENE, "hyg_cls")
    for ok in (
        "docs/dev/x.md",
        "scripts/a.py",
        ".claude/skills/edit-issue/SKILL.md",
        ".claude/skills/edit-issue/tests/test_a.py",
        ".claude/agents/some-agent.md",
    ):
        assert h.is_known_non_runtime_path(ok) and not h.is_runtime_path(ok), ok
    for rt in ("src/a.ts", "tests/a.py", "assets/x.png", "public/i.html", "dist/b.js"):
        assert h.is_runtime_path(rt), rt
        assert not h.is_known_non_runtime_path(rt), rt
    # not whitelisted, not runtime: unknown -> must not be treated as non-runtime
    for unknown in (
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "docs",
        "scriptsx/a.py",
        "documents/a.md",
        ".claude/settings.json",
    ):
        assert not h.is_known_non_runtime_path(unknown), unknown
    assert h.all_paths_non_runtime(["docs/a.md", "src/b.ts"])[0] is False
    assert h.all_paths_non_runtime([])[0] is True


CHECKER_FAILURES = {
    "missing": None,
    "crash-empty-stdout": "import sys\nsys.exit(3)\n",
    "garbage-stdout": "print('not json at all')\n",
    "traceback-leak": "import sys\nprint('Traceback leaked')\nsys.stderr.write('x')\nsys.exit(1)\n",
    "other-blocker": (
        "import json\nprint(json.dumps({'blocking_issues': ['未知のブロッカー: something else']}))\n"
    ),
}


@pytest.mark.parametrize("kind", list(CHECKER_FAILURES), ids=list(CHECKER_FAILURES))
def test_contract_checker_failure_modes_never_yield_a_repair(tmp_path: Path, kind: str):
    stub = CHECKER_FAILURES[kind]
    lay = _build_layout(tmp_path / "ok", stub_checker=stub)
    if stub is None:
        (lay / ".claude/skills/review-issue/scripts/check_issue_contract.py").unlink()
    r = _hygiene(lay, C4_BODY)
    assert r.returncode == 2, (kind, r.returncode, r.stderr)
    assert r.stdout == ""


def test_contract_checker_timeout_is_fail_closed(monkeypatch):
    h = _load(HYGIENE, "hyg_timeout")

    def boom(*a, **kw):
        assert kw.get("timeout") == h.CHECK_ISSUE_CONTRACT_TIMEOUT_SEC
        raise subprocess.TimeoutExpired(a[0], kw["timeout"])

    monkeypatch.setattr(h.subprocess, "run", boom)
    assert h.check_non_c4_c9_blockers(C4_BODY) == (True, ["check_error"])


def test_contract_checker_stdout_json_only_stderr_is_separate(tmp_path: Path):
    """Diagnostics on stderr must not break the stdout-JSON contract or the repaired body."""
    stub = (
        "import json,sys\nsys.stderr.write('diag\\n')\nprint(json.dumps({'blocking_issues': []}))\n"
    )
    lay = _build_layout(tmp_path / "ok", stub_checker=stub)
    r = _hygiene(lay, C4_BODY)
    assert r.returncode == 0, r.stderr
    assert "diag" not in r.stdout
    assert r.stdout.startswith("## Outcome")


def test_c4_c9_blockers_are_filtered_other_blockers_are_not(monkeypatch):
    h = _load(HYGIENE, "hyg_filter")

    class P:
        returncode = 1

        def __init__(self, blocking):
            self.stdout = json.dumps({"blocking_issues": blocking})

    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: P(["C4: 実行可能コマンド", "C9 RVA"]))
    assert h.check_non_c4_c9_blockers(C4_BODY) == (False, [])
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: P(["C4: x", "C7 something else"]))
    assert h.check_non_c4_c9_blockers(C4_BODY) == (True, ["C7 something else"])


def test_hygiene_unreadable_input_and_unwritable_output_exit_2(layout: Path, tmp_path: Path):
    h = _hygiene_in(layout)
    r = _run([sys.executable, "-I", str(h), "--body-file", str(tmp_path / "absent.md")])
    assert r.returncode == 2 and r.stdout == ""
    src = tmp_path / "in.md"
    src.write_text(C4_BODY, encoding="utf-8")
    r = _run(
        [
            sys.executable,
            "-I",
            str(h),
            "--body-file",
            str(src),
            "--out-file",
            str(tmp_path / "nodir" / "out.md"),
        ]
    )
    assert r.returncode == 2


def test_hygiene_out_file_matches_stdout_mode(layout: Path, tmp_path: Path):
    h = _hygiene_in(layout)
    src, out = tmp_path / "in.md", tmp_path / "out.md"
    src.write_text(C4_BODY, encoding="utf-8")
    f = _run([sys.executable, "-I", str(h), "--body-file", str(src), "--out-file", str(out)])
    s = _hygiene(layout, C4_BODY)
    assert f.returncode == s.returncode == 0
    assert f.stdout == ""
    assert out.read_text(encoding="utf-8") == s.stdout


@pytest.mark.parametrize("body", ["", "\n\n", "\x00\x01", "## Outcome\n", "```bash\n"], ids=repr)
def test_hygiene_empty_malformed_or_unsafe_body_never_crashes(layout: Path, body: str):
    r = _hygiene(layout, body)
    assert "Traceback" not in r.stderr
    assert r.returncode in (1, 2)
    assert r.stdout == ""


# --------------------------------------------------------------------------- AC3: producer/consumer


def test_compiler_producer_and_hygiene_consumer_agree_on_the_rewrite(tmp_path: Path):
    """Hygiene applies exactly the compiler's `suggested_command`, at the compiler's line."""
    lay = _build_layout(tmp_path / "ok", stub_checker=STUB_OK)
    leaf = lay / "scripts" / "probe"
    leaf.mkdir()
    (leaf / "test_existing.py").write_text("def test_alpha():\n    pass\n", encoding="utf-8")
    vc = "# AC1\n$ pytest scripts/probe/test_existing.py -k test_new_thing"
    allowed = "- scripts/probe/test_existing.py\n- scripts/probe/test_existing_new_test.py\n"
    body = _body(vc, allowed, _RVA)

    comp = _load(
        lay / ".claude/skills/issue-contract-review/scripts/vc_baseline_shape_compiler.py", "cmp"
    )
    res = comp.compile_body(body, lay)
    assert res["status"] == "changed" and not res.get("warnings")
    (rw,) = res["rewrites"]

    r = _hygiene(lay, body)
    assert r.returncode == 0, r.stderr
    assert "vc_shape=True" in r.stderr
    assert r.stdout.splitlines()[rw["line_number"] - 1] == f"$ {rw['suggested_command']}"
    assert "::test_new_thing" in r.stdout
    assert _hygiene(lay, r.stdout).returncode == 1  # canonical now


def test_compiler_mixed_rewritable_and_unfixable_is_unsafe_for_hygiene(tmp_path: Path):
    lay = _build_layout(tmp_path / "ok", stub_checker=STUB_OK)
    leaf = lay / "scripts" / "probe"
    leaf.mkdir()
    (leaf / "test_existing.py").write_text("def test_alpha():\n    pass\n", encoding="utf-8")
    vc = (
        "$ pytest scripts/probe/test_existing.py -k test_new_thing\n"
        "$ pytest scripts/probe/test_existing.py -k 'a or b'"
    )
    allowed = "- scripts/probe/test_existing.py\n- scripts/probe/test_existing_new_test.py\n"
    body = _body(vc, allowed, _RVA)
    comp = _load(
        lay / ".claude/skills/issue-contract-review/scripts/vc_baseline_shape_compiler.py", "cmp2"
    )
    res = comp.compile_body(body, lay)
    assert res["status"] == "changed" and res["warnings"]
    r = _hygiene(lay, body)
    assert r.returncode == 2
    assert "vc_shape_mixed_changed_and_warnings" in r.stderr
    assert r.stdout == ""


def test_compiler_reuses_shared_vc_contract_syntax_not_a_private_grammar():
    text = COMPILER.read_text(encoding="utf-8")
    assert "from vc_contract_syntax import" in text
    assert "extract_baseline_expect_annotation" in text
    assert (SKILLS / "issue-contract-review" / "scripts" / "vc_contract_syntax.py").is_file()
    assert "from allowed_paths_policy import is_no_path_marker" in text


# --------------------------------------------------------------------------- AC3: guard controls

_IMPL_OK = textwrap.dedent("""\
    ## Outcome
    Something observable.

    ## Acceptance Criteria
    - [ ] AC1: a thing is true

    ## Verification Commands
    ```bash
    # AC1
    $ test -f docs/dev/x.md
    ```
""")


def _guard(layout: Path, body: str, *args: str, cwd=None):
    g = layout / ".claude/skills/edit-issue/scripts/guard-issue-body.py"
    return _run([sys.executable, "-I", str(g), "--body-file", str(layout / "b.md"), *args], cwd=cwd)


def test_guard_missing_input_file_and_missing_template_dir_do_not_pass(tmp_path: Path):
    lay = _build_layout(tmp_path / "ok")
    r = _run(
        [
            sys.executable,
            "-I",
            str(lay / ".claude/skills/edit-issue/scripts/guard-issue-body.py"),
            "--body-file",
            str(tmp_path / "absent.md"),
            "--issue-kind",
            "implementation",
        ]
    )
    assert r.returncode != 0
    (lay / "b.md").write_text(_IMPL_OK, encoding="utf-8")
    shutil.rmtree(lay / ".github")  # template dir gone -> template guard must not silently pass
    r = _guard(lay, _IMPL_OK, "--issue-kind", "implementation")
    assert r.returncode != 0, r.stdout
    assert '"passed": true' not in r.stdout.lower()


def test_guard_unknown_issue_kind_is_rejected(tmp_path: Path):
    lay = _build_layout(tmp_path / "ok")
    (lay / "b.md").write_text(_IMPL_OK, encoding="utf-8")
    r = _guard(lay, _IMPL_OK, "--issue-kind", "../../etc/passwd")
    assert r.returncode != 0


def test_guard_empty_body_fails(tmp_path: Path):
    lay = _build_layout(tmp_path / "ok")
    (lay / "b.md").write_text("", encoding="utf-8")
    r = _guard(lay, "", "--issue-kind", "implementation")
    assert r.returncode != 0


def test_guard_template_dir_resolution_requires_git_ancestor(tmp_path: Path):
    g = _load(GUARD, "guard_mod")
    assert g.resolve_template_dir().name == "ISSUE_TEMPLATE"
    lay = _build_layout(tmp_path / "ok")
    shutil.rmtree(lay / ".git")
    moved = _load(lay / ".claude/skills/edit-issue/scripts/guard-issue-body.py", "guard_nogit")
    # tmp_path sits under a directory tree that may itself be a checkout; only assert the
    # no-silent-default contract: either a real ISSUE_TEMPLATE dir is found or it raises.
    try:
        found = moved.resolve_template_dir()
    except FileNotFoundError:
        return
    assert found.is_dir() and found.name == "ISSUE_TEMPLATE"


# --------------------------------------------------------------------------- AC4: manifest / rules


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_parity_hashes_match_files_on_disk():
    m = _manifest()
    assert m["source"] == {
        "repository": LEGACY_SLUG,
        "tag": "pre-split-agent-platform-20261020",
        "commit": "6fa07b6a0ab932a62f08db6a8d521c0dfded2075",
    }
    for rel, p in m["source_parity"].items():
        assert _sha(ROOT / rel) == p["moved_sha256"], rel
        assert (p["source_sha256"] == p["moved_sha256"]) is p["verbatim"], rel
        if not p["verbatim"]:
            assert p["intentional_diff"], rel


def test_manifest_runs_every_moved_file_with_zero_deferred_and_matches_scope():
    m = _manifest()
    moved = {str(p.relative_to(ROOT)) for p in MOVED_TESTS}
    assert moved <= set(m["test_files"])
    assert m["expected_collect"] == m["expected_ran"]
    assert m["deferred_tests"] == []
    assert m["source_inventory"]["skipped"] == 0
    for f in ("guard-issue-body.py", "issue_contract_hygiene_autofix.py"):
        assert f".claude/skills/edit-issue/scripts/{f}" in m["required_files"]
    assert (
        ".claude/skills/issue-contract-review/scripts/vc_baseline_shape_compiler.py"
        in m["required_files"]
    )


def test_manifest_does_not_adopt_stage_34_or_stage3_assets():
    """#34 (edit_issue_txn / issue-editor) and Stage 3 assets are named, owned and absent."""
    m = _manifest()
    for rel in (
        ".claude/skills/edit-issue/scripts/edit_issue_txn.py",
        ".claude/skills/edit-issue/scripts/backup-and-parse-issue.py",
        ".claude/agents/issue-author.md",
        ".claude/skills/edit-issue/tests/test_edit_issue_txn.py",
        ".claude/skills/edit-issue/tests/live_canary_parent_readback.sh",
    ):
        assert not (ROOT / rel).exists(), f"{rel} must not be migrated by this slice"
    named = json.dumps(m["deferred_ownership"]) + json.dumps(m["not_adopted_source_tests"])
    for needle in ("edit_issue_txn", "issue-author", "live_canary", "backup-and-parse-issue"):
        assert needle in named, needle
    for e in m["deferred_ownership"] + m["not_adopted_source_tests"]:
        assert e["owner_stage"] and e["reason"] and e["rerun_condition"], e


def test_issue_author_references_are_fixture_text_only():
    """No moved script/test opens `.claude/agents/issue-author.md`; it is a path string used for
    the `.claude/agents/` non-runtime-prefix classification, so no Agent migration is needed."""
    for f in (GUARD, HYGIENE, COMPILER, *MOVED_TESTS):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name in {"open", "Path", "os.path.join", "read_text"} or name.endswith(
                    ".read_text"
                ):
                    assert "issue-author" not in ast.unparse(node), (f.name, ast.unparse(node)[:80])
    assert not (ROOT / ".claude" / "agents" / "issue-author.md").exists()


def test_document_excerpt_is_derived_not_invented():
    """The moved tests/scripts assert no section of docs/dev/agent-skill-boundaries.md, so no
    excerpt exists; the manifest says so and the full document is not migrated."""
    m = _manifest()
    ex = m["document_excerpt"]
    assert ex["path"] == "docs/dev/agent-skill-boundaries.md"
    assert ex["required_sections"] == []
    assert ex["created"] is False
    assert not (ROOT / ex["path"]).exists()
    anchor = re.compile(r"agent-skill-boundaries\.md#")
    for f in (GUARD, HYGIENE, COMPILER, *MOVED_TESTS):
        assert not anchor.search(f.read_text(encoding="utf-8")), f
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):  # any code path that would READ the doc
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(
                ("read_text", "open")
            ):
                assert "agent-skill-boundaries" not in ast.unparse(node)


def test_moved_tests_have_no_skip_xfail_or_importorskip():
    for f in MOVED_TESTS:
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            banned = {"skip", "skipif", "xfail", "importorskip"}
            if (
                isinstance(node, ast.Attribute)
                and node.attr in banned
                and ast.unparse(node).startswith("pytest.")
            ):
                raise AssertionError(f"{f.name}:{node.lineno} uses {ast.unparse(node)}")


def test_stage1a_manifest_lists_this_lane_and_scans_edit_issue_scripts():
    m = json.loads((ROOT / "docs/dev/stage1a-core-manifest.json").read_text(encoding="utf-8"))
    assert "docs/dev/stage2-3-edit-guards-manifest.json" in m["later_stage_test_manifests"]
    ic = m["import_closure"]
    assert ".claude/skills/edit-issue/scripts" in ic["scan_dirs"]
    assert ".claude/skills/edit-issue/scripts" in ic["closure_dirs"]
    assert m["not_migrated_source_tests"]["count"] == len(m["not_migrated_source_tests"]["files"])


def test_ci_has_a_dedicated_lane_for_this_manifest():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "run_stage1b1_tests.py docs/dev/stage2-3-edit-guards-manifest.json" in ci
    assert "stage2-3-edit-guards:" in ci
