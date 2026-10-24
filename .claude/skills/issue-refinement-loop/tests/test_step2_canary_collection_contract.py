"""Issue #2903: collection / standalone-runtime contract of the opt-in Step 2
producer smoke canary (`test_step2_execution_surface_canary.py`).

Each case launches a hermetic CHILD pytest (``sys.executable -m pytest``,
the repo's existing child-pytest style) against the REAL canary module (a
symlink to the production file, never a copy of its decision logic) and
asserts on the child's exit code, stdout lines and node-level outcomes
(junit xml).

What a child's exit 77 / ``SKIP:`` means: the canary was an explicit
standalone runtime invocation that was unavailable. The PARENT regression
PASSES because the child expressed SKIP correctly. It is never a live
producer PASS (Runtime Verification Applicability: not_applicable).

Child environment: every case builds its environment explicitly. Nothing of
``PYTEST_*`` / ``LOOP_CANARY_*`` is inherited from the parent, plugin
autoload is disabled, and only the plugins passed with ``-p`` are active.
Opt-in cases never touch the network or a real producer: a ``-p`` plugin
installs test doubles for ``shutil.which`` / ``subprocess.run`` /
``subprocess.Popen`` inside the CHILD process only and logs every boundary
crossing to a JSONL file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

_CANARY_FILE = Path(__file__).resolve().parent / "test_step2_execution_surface_canary.py"
_CANARY_NODE = "test_ac9_producer_smoke_canary_against_issue_2584"
_ENABLE_ENV_VAR = "LOOP_CANARY_STEP2_LIVE_ENABLE"
_PLUGIN_NAME = "canary_collection_doubles"
_CHILD_TIMEOUT_SECONDS = 180

# Test-double plugin loaded ONLY into the child pytest. Behaviour is selected
# with CANARY_DOUBLE_MODE; every boundary crossing is appended to
# CANARY_DOUBLE_LOG. Non-matching calls are delegated to the real objects so
# pytest / xdist internals are unaffected.
_DOUBLES_PLUGIN_SOURCE = textwrap.dedent(
    '''
    import json
    import os
    import shutil
    import subprocess

    _MODE = os.environ.get("CANARY_DOUBLE_MODE", "auth_ok")
    _LOG = os.environ["CANARY_DOUBLE_LOG"]

    _real_which = shutil.which
    _real_run = subprocess.run
    _real_popen = subprocess.Popen


    def _log(event, argv=None):
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, "argv": argv}) + "\\n")


    def _which(name, *args, **kwargs):
        if name == "gh":
            _log("which_gh")
            return None if _MODE == "which_none" else "/fake/bin/gh"
        return _real_which(name, *args, **kwargs)


    def _run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and list(argv)[1:3] == ["auth", "status"]:
            _log("gh_auth_status", list(argv))
            if _MODE == "auth_timeout":
                raise subprocess.TimeoutExpired(list(argv), 15)
            if _MODE == "auth_oserror":
                raise OSError("fake: gh not executable")
            if _MODE == "auth_runtime_error":
                raise RuntimeError("fake: gh auth exploded")
            if _MODE == "auth_nonzero":
                return subprocess.CompletedProcess(list(argv), 1, "", "not logged in")
            return subprocess.CompletedProcess(list(argv), 0, "ok", "")
        return _real_run(argv, *args, **kwargs)


    class _FakeProcess:
        pid = 2**22 + 12345  # never signalled: the fake never times out
        returncode = 0

        def wait(self, timeout=None):
            return 0

        def communicate(self, timeout=None):
            return "not-json-from-fake-producer", ""


    def _popen(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and any("run_root_review_pipeline.py" in str(a) for a in argv):
            _log("popen", [str(a) for a in argv])
            if _MODE == "popen_runtime_error":
                raise RuntimeError("fake: producer launch exploded")
            if _MODE == "popen_timeout":
                raise subprocess.TimeoutExpired([str(a) for a in argv], 1)
            return _FakeProcess()
        return _real_popen(argv, *args, **kwargs)


    shutil.which = _which
    subprocess.run = _run
    subprocess.Popen = _popen
    '''
)

_INDEPENDENT_FAIL_SOURCE = (
    "def test_independent_failure():\n    assert False, 'independent failure must stay visible'\n"
)
_LATER_PASS_SOURCE = "def test_later_node_runs():\n    assert True\n"


@dataclass
class ChildResult:
    returncode: int
    stdout: str
    stderr: str
    outcomes: dict[str, str]  # junit testcase name -> passed | failed | skipped
    doubles_log: list[dict]

    @property
    def stdout_lines(self) -> list[str]:
        return self.stdout.splitlines()

    def has_skip_line(self) -> bool:
        return any(line.startswith("SKIP:") for line in self.stdout_lines)

    def events(self, name: str) -> list[dict]:
        return [entry for entry in self.doubles_log if entry["event"] == name]


@dataclass
class Layout:
    root: Path
    suite: Path  # directory holding independent fail + canary symlink + later pass
    canary_link: Path
    fail_file: Path
    later_file: Path


def _sanitized_parent_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "LOOP_CANARY_", "CANARY_DOUBLE_"))
    }
    return env


def _build_child_env(root: Path, extra: dict[str, str] | None, *, doubles: bool) -> dict[str, str]:
    env = _sanitized_parent_env()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["RUNTIME_VERIFICATION_ARTIFACT_DIR"] = str(root / "artifacts")
    if doubles:
        env["PYTHONPATH"] = str(root / "plugins")
        env["CANARY_DOUBLE_LOG"] = str(root / "doubles.jsonl")
    else:
        env.pop("PYTHONPATH", None)
    env.update(extra or {})
    return env


def _read_outcomes(junit_path: Path) -> dict[str, str]:
    if not junit_path.is_file():
        return {}
    outcomes: dict[str, str] = {}
    for case in ET.parse(junit_path).getroot().iter("testcase"):
        if case.find("skipped") is not None:
            outcome = "skipped"
        elif case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        else:
            outcome = "passed"
        outcomes[case.get("name", "")] = outcome
    return outcomes


def _run_child(
    layout: Layout,
    args: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
    doubles: bool = True,
    xdist: bool = False,
    use_clean_env: bool = True,
    parent_env: dict[str, str] | None = None,
    quiet: bool = True,
) -> ChildResult:
    root = layout.root
    junit = root / "junit.xml"
    if junit.exists():
        junit.unlink()
    log = root / "doubles.jsonl"
    if log.exists():
        log.unlink()
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str(root / "pytest.ini"),
        "--rootdir",
        str(root),
        "-p",
        "no:cacheprovider",
        f"--junitxml={junit}",
    ]
    if quiet:
        cmd.append("-q")
    if doubles:
        cmd += ["-p", _PLUGIN_NAME]
    if xdist:
        cmd += ["-p", "xdist.plugin"]
    cmd += args
    env = _build_child_env(root, env_extra, doubles=doubles) if use_clean_env else dict(parent_env or {})
    completed = subprocess.run(
        cmd,
        cwd=str(cwd or root),
        env=env,
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    doubles_log: list[dict] = []
    if log.is_file():
        doubles_log = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    return ChildResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        outcomes=_read_outcomes(junit),
        doubles_log=doubles_log,
    )


@pytest.fixture()
def layout(tmp_path: Path) -> Layout:
    root = tmp_path / "child"
    suite = root / "suite"
    (root / "plugins").mkdir(parents=True)
    suite.mkdir()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "plugins" / f"{_PLUGIN_NAME}.py").write_text(_DOUBLES_PLUGIN_SOURCE, encoding="utf-8")
    canary_link = suite / _CANARY_FILE.name
    canary_link.symlink_to(_CANARY_FILE)
    fail_file = suite / "test_aaa_independent_failure.py"
    fail_file.write_text(_INDEPENDENT_FAIL_SOURCE, encoding="utf-8")
    later_file = suite / "test_zzz_later_node.py"
    later_file.write_text(_LATER_PASS_SOURCE, encoding="utf-8")
    return Layout(root=root, suite=suite, canary_link=canary_link, fail_file=fail_file, later_file=later_file)


def _assert_item_skip_session_continues(child: ChildResult, *, expect_independent_failure: bool = True) -> None:
    """Item-skip shape: no session exit 77, no capture-external SKIP line, other nodes ran."""
    assert child.returncode != 77, child.stdout
    assert not child.has_skip_line(), child.stdout_lines
    assert child.outcomes.get(_CANARY_NODE) == "skipped", child.outcomes
    assert child.outcomes.get("test_later_node_runs") == "passed", child.outcomes
    if expect_independent_failure:
        assert child.outcomes.get("test_independent_failure") == "failed", child.outcomes
        assert child.returncode == 1
    assert not child.events("popen")
    assert "INTERNALERROR" not in child.stdout + child.stderr


# --- AC2: explicit standalone runtime invocation -> stdout SKIP + exit 77 ----------------------


@pytest.mark.parametrize(
    "extra_args",
    [
        pytest.param([], id="file_only"),
        pytest.param(["-k", "ac9"], id="file_with_k_selection"),
        pytest.param(["-m", "not nothing"], id="file_with_m_selection"),
    ],
)
def test_standalone_file_unavailable_exits_77_with_line_start_skip(layout: Layout, extra_args: list[str]) -> None:
    child = _run_child(layout, [str(_CANARY_FILE), *extra_args])
    assert child.returncode == 77, (child.stdout, child.stderr)
    assert child.has_skip_line(), child.stdout_lines
    assert not child.events("popen")


def test_standalone_exact_node_id_unavailable_exits_77(layout: Layout) -> None:
    child = _run_child(layout, [f"{_CANARY_FILE}::{_CANARY_NODE}"])
    assert child.returncode == 77, (child.stdout, child.stderr)
    assert child.has_skip_line(), child.stdout_lines


def test_standalone_relative_path_and_symlink_resolve_to_same_file(layout: Layout) -> None:
    relative = _run_child(layout, [_CANARY_FILE.name], cwd=_CANARY_FILE.parent)
    assert relative.returncode == 77, (relative.stdout, relative.stderr)
    assert relative.has_skip_line(), relative.stdout_lines
    via_symlink = _run_child(layout, [str(layout.canary_link)])
    assert via_symlink.returncode == 77, (via_symlink.stdout, via_symlink.stderr)
    assert via_symlink.has_skip_line(), via_symlink.stdout_lines


@pytest.mark.parametrize(
    ("mode", "env"),
    [
        pytest.param("auth_ok", {}, id="optin_unset"),
        pytest.param("auth_ok", {_ENABLE_ENV_VAR: "0"}, id="optin_zero"),
        pytest.param("auth_ok", {_ENABLE_ENV_VAR: ""}, id="optin_empty"),
        pytest.param("auth_ok", {_ENABLE_ENV_VAR: "true"}, id="optin_invalid"),
        pytest.param("which_none", {_ENABLE_ENV_VAR: "1"}, id="gh_missing"),
        pytest.param("auth_nonzero", {_ENABLE_ENV_VAR: "1"}, id="gh_auth_nonzero"),
        pytest.param("auth_timeout", {_ENABLE_ENV_VAR: "1"}, id="gh_auth_timeout"),
        pytest.param("auth_oserror", {_ENABLE_ENV_VAR: "1"}, id="gh_auth_oserror"),
    ],
)
def test_unavailable_standalone_is_77_and_never_reaches_producer(
    layout: Layout, mode: str, env: dict[str, str]
) -> None:
    child = _run_child(layout, [str(_CANARY_FILE)], env_extra={"CANARY_DOUBLE_MODE": mode, **env})
    assert child.returncode == 77, (child.stdout, child.stderr)
    assert child.has_skip_line(), child.stdout_lines
    assert not child.events("popen"), "unavailable canary must never launch the producer"


@pytest.mark.parametrize("quiet", [True, False], ids=["q", "no_q"])
@pytest.mark.parametrize("verbosity", [[], ["-v"], ["-vv"]], ids=["default", "v", "vv"])
def test_standalone_skip_line_is_line_start_for_every_verbosity(
    layout: Layout, verbosity: list[str], quiet: bool
) -> None:
    # pytest's progress output (file name / node-id) precedes the SKIP line on the
    # same terminal line unless the SKIP line begins with its own newline.
    child = _run_child(layout, [*verbosity, str(_CANARY_FILE)], quiet=quiet)
    assert child.returncode == 77, (child.stdout, child.stderr)
    assert any(line.startswith("SKIP:") for line in child.stdout.splitlines()), child.stdout
    assert not child.events("popen")


def test_gh_auth_timeout_skip_happens_after_auth_check_only(layout: Layout) -> None:
    child = _run_child(
        layout,
        [str(_CANARY_FILE)],
        env_extra={"CANARY_DOUBLE_MODE": "auth_timeout", _ENABLE_ENV_VAR: "1"},
    )
    assert child.returncode == 77
    assert len(child.events("gh_auth_status")) == 1
    assert [e["event"] for e in child.doubles_log] == ["which_gh", "gh_auth_status"]


# --- AC2 tail: only the pre-launch availability check is converted to SKIP -----------------------


def test_unexpected_gh_auth_exception_is_not_converted_to_skip(layout: Layout) -> None:
    # Only (OSError, subprocess.TimeoutExpired) mean "unavailable"; anything else is a real failure.
    child = _run_child(
        layout,
        [str(_CANARY_FILE)],
        env_extra={"CANARY_DOUBLE_MODE": "auth_runtime_error", _ENABLE_ENV_VAR: "1"},
    )
    assert child.returncode == 1, (child.stdout, child.stderr)
    assert not child.has_skip_line(), child.stdout_lines
    assert child.outcomes.get(_CANARY_NODE) == "failed", child.outcomes
    assert len(child.events("gh_auth_status")) == 1
    assert not child.events("popen")


@pytest.mark.parametrize("mode", ["popen_runtime_error", "popen_timeout"])
def test_failure_after_producer_launch_is_not_converted_to_skip(layout: Layout, mode: str) -> None:
    child = _run_child(
        layout,
        [str(_CANARY_FILE)],
        env_extra={"CANARY_DOUBLE_MODE": mode, _ENABLE_ENV_VAR: "1"},
    )
    assert child.returncode == 1, (child.stdout, child.stderr)
    assert child.returncode != 77
    assert not child.has_skip_line(), child.stdout_lines
    assert child.outcomes.get(_CANARY_NODE) == "failed", child.outcomes
    assert len(child.events("popen")) == 1


# --- AC3: opt-in boundary --------------------------------------------------------------------


def test_opt_in_one_reaches_producer_launch_after_auth_check(layout: Layout) -> None:
    child = _run_child(
        layout,
        [str(_CANARY_FILE)],
        env_extra={"CANARY_DOUBLE_MODE": "auth_ok", _ENABLE_ENV_VAR: "1"},
    )
    assert [e["event"] for e in child.doubles_log] == ["which_gh", "gh_auth_status", "popen"]
    popen_argv = child.events("popen")[0]["argv"]
    assert any(item.endswith("run_root_review_pipeline.py") for item in popen_argv), popen_argv
    assert "produce" in popen_argv
    # The fake producer returns non-JSON: the live path itself still FAILs closed (not SKIP, not 77).
    assert child.returncode == 1, (child.stdout, child.stderr)
    assert not child.has_skip_line()


@pytest.mark.parametrize(
    "env",
    [{}, {_ENABLE_ENV_VAR: "0"}, {_ENABLE_ENV_VAR: ""}, {_ENABLE_ENV_VAR: "yes"}],
    ids=["unset", "zero", "empty", "invalid"],
)
@pytest.mark.parametrize("shape", ["standalone", "directory"])
def test_non_opt_in_never_reaches_producer_boundary(layout: Layout, env: dict[str, str], shape: str) -> None:
    target = str(_CANARY_FILE) if shape == "standalone" else str(layout.suite)
    child = _run_child(layout, [target], env_extra={"CANARY_DOUBLE_MODE": "auth_ok", **env})
    assert not child.events("popen")
    assert not child.events("gh_auth_status")
    assert not child.events("which_gh")


def test_collect_only_never_runs_the_body_even_when_opted_in(layout: Layout) -> None:
    child = _run_child(
        layout,
        ["--collect-only", str(_CANARY_FILE)],
        env_extra={"CANARY_DOUBLE_MODE": "auth_ok", _ENABLE_ENV_VAR: "1"},
    )
    assert child.returncode == 0, (child.stdout, child.stderr)
    assert child.doubles_log == []
    assert not child.has_skip_line()
    assert _CANARY_NODE in child.stdout


# --- AC1: broad collection -> item skip, session continues -----------------------------------


def test_directory_target_item_skips_and_independent_failure_stays_failed(layout: Layout) -> None:
    child = _run_child(layout, [str(layout.suite)])
    _assert_item_skip_session_continues(child)


def test_directory_target_without_independent_failure_exits_zero(layout: Layout) -> None:
    layout.fail_file.unlink()
    child = _run_child(layout, [str(layout.suite)])
    assert child.returncode == 0, (child.stdout, child.stderr)
    _assert_item_skip_session_continues(child, expect_independent_failure=False)


def test_mixed_file_targets_item_skip_and_later_nodes_run(layout: Layout) -> None:
    child = _run_child(layout, [str(layout.fail_file), str(_CANARY_FILE), str(layout.later_file)])
    _assert_item_skip_session_continues(child)


def test_canary_plus_other_node_id_is_mixed_not_standalone(layout: Layout) -> None:
    child = _run_child(layout, [f"{_CANARY_FILE}::{_CANARY_NODE}", f"{layout.later_file}::test_later_node_runs"])
    assert child.returncode == 0, (child.stdout, child.stderr)
    assert child.returncode != 77
    assert not child.has_skip_line()
    assert child.outcomes.get(_CANARY_NODE) == "skipped"
    assert child.outcomes.get("test_later_node_runs") == "passed"


def test_directory_narrowed_by_k_to_single_canary_is_not_standalone(layout: Layout) -> None:
    child = _run_child(layout, [str(layout.suite), "-k", "ac9"])
    assert child.returncode == 0, (child.stdout, child.stderr)
    assert not child.has_skip_line(), child.stdout_lines
    assert child.outcomes == {_CANARY_NODE: "skipped"}
    assert not child.events("popen")


def test_no_target_arguments_item_skip(layout: Layout) -> None:
    child = _run_child(layout, [], cwd=layout.suite)
    _assert_item_skip_session_continues(child)


def test_testpaths_resolved_canary_is_item_skip_not_standalone(layout: Layout) -> None:
    # ini ``testpaths`` fills config.args when no target is given; that is NOT an explicit
    # standalone request, even though the resolved target is exactly the canary file.
    (layout.root / "pytest.ini").write_text(f"[pytest]\ntestpaths = suite/{_CANARY_FILE.name}\n", encoding="utf-8")
    child = _run_child(layout, ["-rs"], cwd=layout.root)
    assert child.returncode == 0, (child.stdout, child.stderr)
    assert child.outcomes == {_CANARY_NODE: "skipped"}, child.outcomes
    assert not child.has_skip_line(), child.stdout_lines
    assert "INTERNALERROR" not in child.stdout + child.stderr
    assert not child.events("popen")


# --- AC4: real xdist -------------------------------------------------------------------------


def test_real_xdist_two_workers_item_skip_without_crash(layout: Layout) -> None:
    child = _run_child(layout, ["-n", "2", "-rA", str(layout.suite)], xdist=True, doubles=False)
    _assert_item_skip_session_continues(child)
    assert "SKIPPED" in child.stdout and "1 skipped" in child.stdout
    assert "crashed" not in (child.stdout + child.stderr).lower()


def test_real_xdist_standalone_file_does_not_exit_77_or_crash(layout: Layout) -> None:
    child = _run_child(layout, ["-n", "2", "-rA", str(_CANARY_FILE)], xdist=True, doubles=False)
    assert child.returncode == 0, (child.stdout, child.stderr)
    assert child.outcomes.get(_CANARY_NODE) == "skipped", child.outcomes
    assert not child.has_skip_line()
    assert "INTERNALERROR" not in child.stdout + child.stderr


# --- Environment isolation ---------------------------------------------------------------------


def test_parent_leftover_opt_in_and_worker_variables_do_not_leak_into_child(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    leftovers = {
        _ENABLE_ENV_VAR: "1",
        "PYTEST_XDIST_WORKER": "gw0",
        "PYTEST_XDIST_WORKER_COUNT": "2",
        "PYTEST_ADDOPTS": "-k nothing_matches_this",
        "PYTEST_PLUGINS": "module_that_does_not_exist_xyz",
        "PYTEST_CURRENT_TEST": "parent::leftover (call)",
    }
    for key, value in leftovers.items():
        monkeypatch.setenv(key, value)

    # Control: a naive child that inherits the parent environment is affected by the leftovers,
    # which proves the isolation assertion below is meaningful.
    inherited = _run_child(
        layout,
        [str(_CANARY_FILE)],
        use_clean_env=False,
        parent_env={
            **os.environ,
            "PYTHONPATH": str(layout.root / "plugins"),
            "CANARY_DOUBLE_LOG": str(layout.root / "doubles.jsonl"),
        },
    )
    assert inherited.returncode != 77

    isolated = _run_child(layout, [str(_CANARY_FILE)], env_extra={"CANARY_DOUBLE_MODE": "auth_ok"})
    assert isolated.returncode == 77, (isolated.stdout, isolated.stderr)
    assert isolated.has_skip_line(), isolated.stdout_lines
    assert not isolated.events("popen")
