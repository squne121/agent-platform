"""Issue #2837 fix_delta P1-B: side-effect-free VC metadata extraction.

`adjudicate_vc_result.py extract-vc-metadata --body-file <live body>` must give
the same ordered `(ac, line, raw_command, command_hash)` triples that
`baseline_vc_preflight.py` produces in `results[]` (same shared parser, same
AC labeling, same `sha256:<hex>` hash) while NEVER starting a subprocess. The
baseline executor is parse-and-run, so it cannot be used as a metadata source.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts" / "adjudicate_vc_result.py"
BASELINE_PATH = ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"
STEP2_DOC = ROOT / ".claude" / "skills" / "impl-review-loop" / "steps" / "step-2-verification.md"
STEP4_DOC = ROOT / ".claude" / "skills" / "impl-review-loop" / "steps" / "step-4-pr-review.md"

_spec = importlib.util.spec_from_file_location("adjudicate_vc_result_extract_vc_metadata_tests", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


BODY = textwrap.dedent(
    """\
    # Title

    ## Outcome
    x

    ## Verification Commands

    ```bash
    # AC10, AC2, AC3
    $ echo multi
    $ echo unlabeled
    # AC1
    $ echo one
    ```

    ```bash
    # AC4
    $ echo one
    ```

    ## Allowed Paths
    - README.md
    """
)

EXPECTED_ORDER = [
    ("AC2,AC3,AC10", "echo multi"),
    ("AC_UNKNOWN", "echo unlabeled"),
    ("AC1", "echo one"),
    ("AC4", "echo one"),
]


def _extract(body: str) -> tuple[int, dict]:
    return mod.extract_vc_metadata(body)


def test_extract_orders_commands_and_labels_like_baseline():
    rc, payload = _extract(BODY)

    assert rc == 0 and payload["status"] == "ok"
    assert [(row["ac"], row["raw_command"]) for row in payload["commands"]] == EXPECTED_ORDER
    # Duplicate literal commands keep their own position (one entry per source line).
    hashes = payload["command_hashes"]
    assert hashes == [row["command_hash"] for row in payload["commands"]]
    assert hashes[2] == hashes[3] and len(hashes) == 4
    assert all(h.startswith("sha256:") and len(h) == len("sha256:") + 64 for h in hashes)

    baseline = mod._load_baseline_module()
    section = baseline.extract_verification_commands_section(BODY)
    tuples, _parse = baseline._command_entries_from_shared_parser(section)
    assert [
        (ac or "AC_UNKNOWN", command, line, f"sha256:{baseline.compute_command_hash(command)}")
        for (ac, command, line, *_rest) in tuples
    ] == [(r["ac"], r["raw_command"], r["line"], r["command_hash"]) for r in payload["commands"]]


def test_extract_matches_baseline_cli_results_fields(tmp_path):
    """Parity with the real baseline executor's `results[]` (it runs the inert
    `echo` commands here; that is the thing the extractor must not need)."""
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")

    baseline = subprocess.run(
        [sys.executable, str(BASELINE_PATH), "--body-file", str(body_file), "--cwd", str(tmp_path), "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    baseline_json = json.loads(baseline.stdout)
    baseline_rows = [
        (r["ac"], r["line"], r["raw_command"], r["command_hash"]) for r in baseline_json["results"]
    ]

    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "extract-vc-metadata", "--body-file", str(body_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    cli = json.loads(completed.stdout)
    assert [(r["ac"], r["line"], r["raw_command"], r["command_hash"]) for r in cli["commands"]] == baseline_rows
    assert cli["command_hashes"] == [row[3] for row in baseline_rows]


def test_extract_surfaces_static_errors_like_baseline(tmp_path):
    non_dollar = BODY.replace("$ echo one\n", "echo one\n", 1)
    rc, payload = _extract(non_dollar)
    assert rc == 2 and payload["status"] == "blocked"
    assert payload["commands"] == [] and payload["command_hashes"] == []
    assert any(row["kind"] == "non_dollar_command" for row in payload["static_errors"])
    assert payload["errors"] == ["VC004_NON_DOLLAR_COMMAND"]

    rc, payload = _extract("# Title\n\n## Outcome\nx\n")
    assert rc == 2 and payload["errors"] == ["VC001_NO_VERIFICATION_COMMANDS_SECTION"]

    rc, payload = _extract("## Verification Commands\n\n```bash\n# AC1\n```\n")
    assert rc == 2 and payload["errors"] == ["VC002_NO_COMMANDS_EXTRACTED"]

    # CLI: unreadable body file is an exit-2 structured failure, not a traceback.
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "extract-vc-metadata", "--body-file", str(tmp_path / "absent.md")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["status"] == "blocked"


def test_extract_requires_body_file():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "extract-vc-metadata"], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 2
    assert "--body-file" in completed.stderr


def test_extract_starts_no_subprocess_in_process(monkeypatch):
    calls: list[str] = []

    def _forbid(name):
        def _raise(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not be called during extraction")

        return _raise

    import os

    monkeypatch.setattr(subprocess, "Popen", _forbid("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
    monkeypatch.setattr(subprocess, "check_output", _forbid("subprocess.check_output"))
    monkeypatch.setattr(os, "system", _forbid("os.system"))
    monkeypatch.setattr(os, "popen", _forbid("os.popen"))
    monkeypatch.setattr(os, "fork", _forbid("os.fork"))
    monkeypatch.setattr(os, "posix_spawn", _forbid("os.posix_spawn"))
    # Force a fresh module load so import-time behaviour is covered as well.
    monkeypatch.setattr(mod, "_BASELINE_MODULE", None)

    rc, payload = _extract(BODY)

    assert rc == 0 and payload["status"] == "ok"
    assert calls == []


def test_extract_cli_starts_no_subprocess(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")
    driver = textwrap.dedent(
        f"""\
        import importlib.util, io, json, os, subprocess, sys
        calls = []
        def forbid(name):
            def _raise(*a, **k):
                calls.append(name)
                raise AssertionError(name)
            return _raise
        subprocess.Popen = forbid("subprocess.Popen")
        subprocess.run = forbid("subprocess.run")
        os.system = forbid("os.system")
        os.popen = forbid("os.popen")
        os.fork = forbid("os.fork")
        os.posix_spawn = forbid("os.posix_spawn")
        spec = importlib.util.spec_from_file_location("adj_cli_driver", {str(SCRIPT_PATH)!r})
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
        rc = m.main(["extract-vc-metadata", "--body-file", {str(body_file)!r}])
        sys.stderr.write(json.dumps({{"rc": rc, "calls": calls}}))
        """
    )
    completed = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    meta = json.loads(completed.stderr.strip().splitlines()[-1])
    assert meta == {"rc": 0, "calls": []}
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert [(r["ac"], r["raw_command"]) for r in payload["commands"]] == EXPECTED_ORDER


def test_docs_use_parse_only_extractor_instead_of_baseline_executor():
    step2 = STEP2_DOC.read_text(encoding="utf-8")
    step4 = STEP4_DOC.read_text(encoding="utf-8")
    for doc in (step2, step4):
        assert "extract-vc-metadata" in doc
        assert "baseline_vc_preflight.py --body-file <live body> --format json" not in doc
