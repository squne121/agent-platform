"""scripts/claude-gpt/tests/test_repair_proxy.py

Issue #2801 AC4/AC5: focused tests for `scripts/claude-gpt/repair_proxy.sh`,
the repository-supported one-command bounded repair/bootstrap helper.

Hermetic strategy: `repair_proxy.sh` fetches its installer script via `curl`
from `CLAUDE_GPT_REPAIR_INSTALLER_URL` (defaulting to the real upstream
`raine/claude-code-proxy` installer). Tests override that URL with a local
`file://` URL pointing at a small fixture "fake installer" shell script
(curl supports the `file://` scheme without any network access), so no real
network access or upstream dependency is exercised. The fixture installer
itself writes a fake, dependency-free `claude-code-proxy` HTTP server binary
(stdlib-only Python) whose `--version` output and `/v1/models` catalog are
controlled entirely by env vars that `repair_proxy.sh` passes through
unmodified to the installer step (unlike `launch.sh`'s `env -i` proxy child,
this installer invocation is not scrubbed).
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

# Loaded via importlib.util.spec_from_file_location (not a bare `import`) with
# a name unique to this test module, mirroring the established precedent for
# avoiding sys.modules collisions with same-named sibling helper modules under
# the shared repo-wide pytest session (see
# `scripts/claude-gpt/tests/test_proxy_model_compatibility.py`).
_HELPER_PATH = Path(__file__).resolve().parent / "_proxy_model_compat_fixture_helpers.py"
_helper_spec = importlib.util.spec_from_file_location(
    "claude_gpt_proxy_model_compat_fixture_helpers_2801_repair", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_helper_spec)
assert _helper_spec.loader is not None
_helper_spec.loader.exec_module(_helper)
write_fake_proxy = _helper.write_fake_proxy

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt/
REPAIR_PROXY_SH = SCRIPT_DIR / "repair_proxy.sh"
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"

FAKE_INSTALLER_SOURCE = r"""#!/bin/sh
set -e
: "${CLAUDE_CODE_PROXY_INSTALL_DIR:?}"
: "${CLAUDE_CODE_PROXY_VERSION:?}"
MODELS_JSON="${CLAUDE_GPT_TEST_FIXTURE_MODELS_JSON:-[]}"
mkdir -p "$CLAUDE_CODE_PROXY_INSTALL_DIR"
cat > "$CLAUDE_CODE_PROXY_INSTALL_DIR/claude-code-proxy" <<PYEOF
#!/usr/bin/env python3
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ${MODELS_JSON}
VERSION = "claude-code-proxy ${CLAUDE_CODE_PROXY_VERSION}"


def _serve(port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": m} for m in MODELS]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            return

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main():
    args = sys.argv[1:]
    if not args:
        return 1
    if args[0] == "--version":
        print(VERSION)
        return 0
    if args[0] == "codex" and len(args) >= 3 and args[1] == "auth" and args[2] == "status":
        # preflight.sh's ChatGPT subscription auth check (Issue #2158 P0-2) is
        # independent of the /v1/models catalog compatibility this fixture
        # controls -- a fixed authenticated response keeps it a no-op for
        # tests that exercise the installed binary via launch.sh (fix_delta
        # F7 end-to-end test).
        print("Account: fake-test-account")
        return 0
    if args[0] == "serve":
        port = None
        i = 1
        while i < len(args):
            if args[i] == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
            else:
                i += 1
        if port is None:
            return 1
        return _serve(port)
    return 1


sys.exit(main())
PYEOF
chmod +x "$CLAUDE_CODE_PROXY_INSTALL_DIR/claude-code-proxy"
"""

# --- fix_delta F6 fixtures ------------------------------------------------------
#
# F6a: captures the actual release-asset download URL a real upstream
# installer would construct, by embedding $CLAUDE_CODE_PROXY_VERSION verbatim
# as the GitHub Releases tag name (mirroring the real upstream installer's own
# behavior -- see Issue #2801 fix_delta body point 1). This lets tests assert
# on the *shape* repair_proxy.sh actually causes to be requested, not merely
# on the auxiliary `--version` string reported back by the installed binary.
FAKE_INSTALLER_URL_CAPTURE_SOURCE = r"""#!/bin/sh
set -e
: "${CLAUDE_CODE_PROXY_INSTALL_DIR:?}"
: "${CLAUDE_CODE_PROXY_VERSION:?}"
: "${CLAUDE_GPT_TEST_CAPTURED_URL_FILE:?}"
MODELS_JSON="${CLAUDE_GPT_TEST_FIXTURE_MODELS_JSON:-[]}"
ASSET_URL="https://github.com/raine/claude-code-proxy/releases/download/${CLAUDE_CODE_PROXY_VERSION}"
ASSET_URL="${ASSET_URL}/claude-code-proxy-linux-amd64.tar.gz"
printf '%s' "$ASSET_URL" > "$CLAUDE_GPT_TEST_CAPTURED_URL_FILE"
mkdir -p "$CLAUDE_CODE_PROXY_INSTALL_DIR"
cat > "$CLAUDE_CODE_PROXY_INSTALL_DIR/claude-code-proxy" <<PYEOF
#!/usr/bin/env python3
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ${MODELS_JSON}
VERSION = "claude-code-proxy ${CLAUDE_CODE_PROXY_VERSION}"


def _serve(port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": m} for m in MODELS]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            return

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main():
    args = sys.argv[1:]
    if not args:
        return 1
    if args[0] == "--version":
        print(VERSION)
        return 0
    if args[0] == "serve":
        port = None
        i = 1
        while i < len(args):
            if args[i] == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
            else:
                i += 1
        if port is None:
            return 1
        return _serve(port)
    return 1


sys.exit(main())
PYEOF
chmod +x "$CLAUDE_CODE_PROXY_INSTALL_DIR/claude-code-proxy"
"""

# F6b: a hermetic fixture installer based on an actual bash-only construct
# (`[[ ... ]]` extended test syntax used as a standalone runtime guard, not
# just inside an `if`) that this repo's own `/bin/sh` (dash, a real POSIX sh,
# not bash-in-disguise) cannot execute -- it aborts with "not found" (exit
# 127) BEFORE reaching the install step, whereas bash evaluates it correctly
# and continues. This is the same class of construct the real upstream
# `raine/claude-code-proxy` installer's `#!/usr/bin/env bash` script uses
# internally (Issue #2801 fix_delta body point 2), reproduced hermetically
# here (no network / no dependency on the real upstream script's exact
# text -- only on the same *kind* of bash-only syntax to pin the sh-vs-bash
# execution boundary itself).
FAKE_INSTALLER_BASH_ONLY_SOURCE = r"""#!/usr/bin/env bash
set -e
: "${CLAUDE_CODE_PROXY_INSTALL_DIR:?}"
: "${CLAUDE_CODE_PROXY_VERSION:?}"
MODELS_JSON="${CLAUDE_GPT_TEST_FIXTURE_MODELS_JSON:-[]}"
# Bash-only conditional expression syntax used as a standalone runtime guard
# (mirrors upstream's own use of `[[ ... ]]` / `&>/dev/null` style constructs).
# Under dash this line itself fails to execute ("[[: not found", exit 127)
# and -- because of the preceding `set -e` -- aborts the whole script here,
# never reaching the install step below.
[[ -n "$CLAUDE_CODE_PROXY_VERSION" ]]
mkdir -p "$CLAUDE_CODE_PROXY_INSTALL_DIR"
cat > "$CLAUDE_CODE_PROXY_INSTALL_DIR/claude-code-proxy" <<PYEOF
#!/usr/bin/env python3
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ${MODELS_JSON}
VERSION = "claude-code-proxy ${CLAUDE_CODE_PROXY_VERSION}"


def _serve(port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": m} for m in MODELS]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            return

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main():
    args = sys.argv[1:]
    if not args:
        return 1
    if args[0] == "--version":
        print(VERSION)
        return 0
    if args[0] == "serve":
        port = None
        i = 1
        while i < len(args):
            if args[i] == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
            else:
                i += 1
        if port is None:
            return 1
        return _serve(port)
    return 1


sys.exit(main())
PYEOF
chmod +x "$CLAUDE_CODE_PROXY_INSTALL_DIR/claude-code-proxy"
"""


def _required_models() -> list[str]:
    result = subprocess.run(
        ["sh", "-c", ". ./lib.sh; claude_gpt_required_model_set"],
        check=False,
        cwd=str(SCRIPT_DIR),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    models = [line for line in result.stdout.splitlines() if line]
    assert models
    return models


def _write_fake_installer(tmp_path: Path, *, source: str = FAKE_INSTALLER_SOURCE) -> Path:
    installer_path = tmp_path / "fake-install.sh"
    installer_path.write_text(source, encoding="utf-8")
    mode = installer_path.stat().st_mode
    installer_path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return installer_path


def _run_repair(
    tmp_path: Path,
    *,
    fixture_models: list[str],
    claude_gpt_home: Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float = 30.0,
    installer_source: str = FAKE_INSTALLER_SOURCE,
) -> subprocess.CompletedProcess:
    installer_path = _write_fake_installer(tmp_path, source=installer_source)
    env = dict(os.environ)
    env["CLAUDE_GPT_REPAIR_INSTALLER_URL"] = f"file://{installer_path}"
    env["CLAUDE_GPT_TEST_FIXTURE_MODELS_JSON"] = json.dumps(fixture_models)
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home or (tmp_path / "claude-gpt-home"))
    env.pop("CLAUDE_CODE_PROXY_INSTALL_DIR", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(REPAIR_PROXY_SH)],
        check=False,
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# --- AC4 ----------------------------------------------------------------------


def test_repair_installs_isolated_compatible_proxy(tmp_path):
    """GIVEN the (fixture) upstream installer and no pre-existing proxy
    WHEN repair_proxy.sh runs with default install dir / version pin
    THEN it installs a compatible proxy binary into `$CLAUDE_GPT_HOME/bin`
    (isolated Claude-GPT-owned directory), reports success, and uses the
    `CLAUDE_CODE_PROXY_VERSION` pin (`CLAUDE_GPT_MIN_KNOWN_COMPATIBLE_PROXY_VERSION`
    by default) -- without touching anything outside `$CLAUDE_GPT_HOME`
    (global PATH install / Native Claude / running session are not disturbed).
    """
    required = _required_models()
    claude_gpt_home = tmp_path / "claude-gpt-home"
    decoy_native_home = tmp_path / "decoy-native-home"
    decoy_native_home.mkdir()
    (decoy_native_home / "sentinel.txt").write_text("untouched", encoding="utf-8")

    result = _run_repair(tmp_path, fixture_models=required, claude_gpt_home=claude_gpt_home)
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert payload["schema"] == "CLAUDE_GPT_REPAIR_PROXY_RESULT_V1"
    assert payload["status"] == "ok"

    expected_installed_path = claude_gpt_home / "bin" / "claude-code-proxy"
    assert payload["installed_path"] == str(expected_installed_path)
    assert expected_installed_path.exists()
    assert os.access(expected_installed_path, os.X_OK)
    assert "0.1.42" in payload["installed_version"]  # default version pin
    assert set(payload["required_models"]) == set(required)

    # Isolation: nothing was written outside $CLAUDE_GPT_HOME.
    assert (decoy_native_home / "sentinel.txt").read_text(encoding="utf-8") == "untouched"
    assert list(decoy_native_home.iterdir()) == [decoy_native_home / "sentinel.txt"]


def test_repair_respects_explicit_version_pin_override(tmp_path):
    """GIVEN an explicit CLAUDE_CODE_PROXY_VERSION override
    WHEN repair_proxy.sh runs
    THEN the fixture installer receives and reports that exact version pin
    (not the repository default), matching the documented
    `CLAUDE_CODE_PROXY_VERSION` env contract.
    """
    required = _required_models()
    result = _run_repair(
        tmp_path,
        fixture_models=required,
        extra_env={"CLAUDE_CODE_PROXY_VERSION": "9.9.9-pinned-test"},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "9.9.9-pinned-test" in payload["installed_version"]


# --- AC5 ------------------------------------------------------------------------


def test_repair_reverifies_catalog_after_install(tmp_path):
    """GIVEN the fixture installer succeeds (exit 0, binary present) and the
    installed binary's live catalog has every required model
    WHEN repair_proxy.sh runs
    THEN it reports success only after re-verifying the live catalog itself
    (AC5) -- not merely because the installer process exited 0.
    """
    required = _required_models()
    result = _run_repair(tmp_path, fixture_models=required)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_repair_fails_when_install_exits_zero_but_catalog_still_incompatible(tmp_path):
    """GIVEN the fixture installer succeeds (exit 0, binary present) but the
    installed binary's live catalog is STILL missing a required model
    WHEN repair_proxy.sh runs
    THEN it does NOT report success -- an installer exit 0 alone is not
    sufficient; required models must actually be present afterwards (AC5).
    """
    required = _required_models()
    incompatible_catalog = required[:-1]
    dropped = required[-1]

    result = _run_repair(tmp_path, fixture_models=incompatible_catalog)
    assert result.returncode == 2, result.stdout

    payload = json.loads(result.stderr)
    assert payload["schema"] == "CLAUDE_GPT_REPAIR_PROXY_RESULT_V1"
    assert payload["status"] == "failed"
    assert payload["reason"] == "catalog_still_incompatible_after_install"
    assert dropped in payload["missing_models"]


# --- fix_delta F6: OWNER P1 recurrence prevention -------------------------------


def test_repair_default_version_pin_downloads_v_prefixed_release_tag(tmp_path):
    """GIVEN no explicit CLAUDE_CODE_PROXY_VERSION override
    WHEN repair_proxy.sh runs and the (fixture) upstream installer constructs
    its release-asset download URL by embedding $CLAUDE_CODE_PROXY_VERSION
    verbatim as the GitHub Releases tag name (mirroring the real upstream
    installer's own behavior)
    THEN the constructed URL uses the official `v<version>` tag form
    (`v0.1.42`), not the bare `0.1.42` diagnostic-display value -- fix_delta
    F1 regression guard for the "default repair path fails at artifact
    download" bug (OWNER P1).
    """
    required = _required_models()
    captured_url_file = tmp_path / "captured-url.txt"
    result = _run_repair(
        tmp_path,
        fixture_models=required,
        installer_source=FAKE_INSTALLER_URL_CAPTURE_SOURCE,
        extra_env={"CLAUDE_GPT_TEST_CAPTURED_URL_FILE": str(captured_url_file)},
    )
    assert result.returncode == 0, result.stderr
    captured_url = captured_url_file.read_text(encoding="utf-8")
    assert captured_url == (
        "https://github.com/raine/claude-code-proxy/releases/download/"
        "v0.1.42/claude-code-proxy-linux-amd64.tar.gz"
    )


def test_repair_explicit_version_override_not_forced_v_prefixed(tmp_path):
    """GIVEN an explicit CLAUDE_CODE_PROXY_VERSION override that does NOT
    start with `v`
    WHEN repair_proxy.sh runs
    THEN the constructed release-asset download URL uses the operator's exact
    value verbatim -- the `v` prefix fallback normalization (F1) must only
    apply to the repository's own default, never silently rewrite an
    explicit operator override (override intent preserved).
    """
    required = _required_models()
    captured_url_file = tmp_path / "captured-url.txt"
    result = _run_repair(
        tmp_path,
        fixture_models=required,
        installer_source=FAKE_INSTALLER_URL_CAPTURE_SOURCE,
        extra_env={
            "CLAUDE_CODE_PROXY_VERSION": "2.0.0-custom",
            "CLAUDE_GPT_TEST_CAPTURED_URL_FILE": str(captured_url_file),
        },
    )
    assert result.returncode == 0, result.stderr
    captured_url = captured_url_file.read_text(encoding="utf-8")
    assert captured_url == (
        "https://github.com/raine/claude-code-proxy/releases/download/"
        "2.0.0-custom/claude-code-proxy-linux-amd64.tar.gz"
    )
    assert "v2.0.0-custom" not in captured_url


def test_repair_installer_bash_only_construct_succeeds_via_repair_proxy_sh(tmp_path):
    """GIVEN a hermetic fixture installer using a real bash-only construct
    (`[[ ... ]]` extended test syntax, the same class of syntax the real
    upstream installer's `#!/usr/bin/env bash` script relies on)
    WHEN repair_proxy.sh runs (F2: now executes the installer via `bash`, not
    `sh`)
    THEN it succeeds end-to-end (install + live-catalog re-verify PASS) --
    proving repair_proxy.sh's own `bash` invocation is what makes this class
    of installer script actually work.
    """
    required = _required_models()
    result = _run_repair(
        tmp_path, fixture_models=required, installer_source=FAKE_INSTALLER_BASH_ONLY_SOURCE
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_installer_bash_only_construct_fails_under_sh_execution_boundary(tmp_path):
    """GIVEN the SAME hermetic fixture installer as the previous test
    WHEN it is executed directly with `sh` (this repo's `/bin/sh`, a real
    POSIX dash, not bash-in-disguise) instead of through repair_proxy.sh
    THEN it fails (non-zero exit, "not found" for `[[`) BEFORE ever reaching
    the install step -- directly pinning the sh-vs-bash execution boundary
    itself (not just asserting repair_proxy.sh's behavior), so this test
    would fail loudly if repair_proxy.sh's `bash` invocation (F2) were ever
    reverted back to `sh`.
    """
    installer_path = _write_fake_installer(tmp_path, source=FAKE_INSTALLER_BASH_ONLY_SOURCE)
    install_dir = tmp_path / "sh-boundary-install-dir"
    env = dict(os.environ)
    env["CLAUDE_CODE_PROXY_INSTALL_DIR"] = str(install_dir)
    env["CLAUDE_CODE_PROXY_VERSION"] = "v0.1.42"
    env["CLAUDE_GPT_TEST_FIXTURE_MODELS_JSON"] = "[]"

    result = subprocess.run(
        ["sh", str(installer_path)],
        check=False,
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, result.stdout
    assert "not found" in result.stderr
    assert not (install_dir / "claude-code-proxy").exists()


# --- fix_delta F7: end-to-end repair -> normal launcher re-selection path -------


def test_repair_is_binary_scoped_and_does_not_redirect_launcher_auxiliary_evidence(tmp_path):
    """GIVEN PATH exposes an old/incompatible `claude-code-proxy` binary
    WHEN repair_proxy.sh (via the fixture installer) installs a compatible
    proxy into `$CLAUDE_GPT_HOME/bin`
    THEN (Issue #2925 AC3) the result states that only a BINARY was repaired
    and that the running server must be restarted by its owner, and
    `claude_gpt_resolve_proxy_bin()` (the default launcher's auxiliary
    evidence) still reports the PATH binary: the launcher never starts a
    proxy, so the repaired managed binary is not something it runs or
    reports as the connected server.
    """
    required = _required_models()
    claude_gpt_home = tmp_path / "claude-gpt-home"

    path_bin_dir = tmp_path / "path-bin"
    path_bin_dir.mkdir()
    old_path_proxy = write_fake_proxy(
        path_bin_dir / "claude-code-proxy",
        models=required[:-1],  # incompatible: missing one required model
        version="claude-code-proxy 0.1.30",
    )

    # --- step 1: repair installs a compatible proxy into $CLAUDE_GPT_HOME/bin ---
    repair_result = _run_repair(tmp_path, fixture_models=required, claude_gpt_home=claude_gpt_home)
    assert repair_result.returncode == 0, repair_result.stderr
    repair_payload = json.loads(repair_result.stdout)
    assert repair_payload["status"] == "ok"
    assert repair_payload["repaired_scope"] == "binary_only"
    assert repair_payload["server_restart_required"] is True
    managed_bin = claude_gpt_home / "bin" / "claude-code-proxy"
    assert repair_payload["installed_path"] == str(managed_bin)
    assert managed_bin.exists()
    assert "NOT restarted" in repair_result.stderr

    resolve_env = dict(os.environ)
    resolve_env["CLAUDE_GPT_HOME"] = str(claude_gpt_home)
    resolve_env["PATH"] = f"{path_bin_dir}{os.pathsep}{resolve_env['PATH']}"
    resolve_env.pop("CLAUDE_GPT_PROXY_BIN", None)

    # --- step 2: explicit-override-free resolution still reports the PATH
    #     binary (auxiliary evidence), never the managed repair target. ---
    resolve_result = subprocess.run(
        ["sh", "-c", ". ./lib.sh; claude_gpt_resolve_proxy_bin"],
        check=False,
        cwd=str(SCRIPT_DIR),
        env=resolve_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert resolve_result.returncode == 0, resolve_result.stderr
    resolved_bin = resolve_result.stdout.strip()
    assert resolved_bin == str(old_path_proxy)
    assert resolved_bin != str(managed_bin)


# --- misc: dry-run / curl-unavailable guardrails (scripts/CLAUDE.md 破壊的処理不変条件) --


def test_dry_run_performs_no_filesystem_mutation(tmp_path):
    """GIVEN --dry-run
    WHEN repair_proxy.sh runs
    THEN it reports the planned install_dir/version_pin/installer_url without
    creating any directory or file (scripts/CLAUDE.md dry-run invariant).
    """
    claude_gpt_home = tmp_path / "claude-gpt-home"
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home)
    result = subprocess.run(
        [str(REPAIR_PROXY_SH), "--dry-run"],
        check=False,
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "dry_run"
    assert payload["install_dir"] == str(claude_gpt_home / "bin")
    assert not claude_gpt_home.exists()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
