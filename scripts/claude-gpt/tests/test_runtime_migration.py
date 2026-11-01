"""scripts/claude-gpt/tests/test_runtime_migration.py

agent-platform Issue #57: the shared runtime launcher is consumed as
`cd <consumer>; sh "$PLATFORM/scripts/claude-gpt/launch.sh" --claude-bin <claude> -- ...`.
These focused subprocess tests drive the real `launch.sh` through the existing harness
against a separate-process fake proxy and a fake `claude` that records argv / env / cwd.

No real proxy, Claude binary, login, inference or credential path is involved: every
HOME / XDG / config / socket value below is a sentinel under `tmp_path`.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_HARNESS_PATH = Path(__file__).resolve().parent / "_launcher_harness.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_launcher_harness_57_runtime", _HARNESS_PATH
)
assert _spec is not None and _spec.loader is not None
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

# The complete set of env entries the launcher adds for the claude child, with exact values
# (CLAUDE_GPT_CLAUDE_BIN / ANTHROPIC_BASE_URL are per-run and filled in by `_expected_env`).
EXPECTED_STATIC_ENV = {
    "ANTHROPIC_AUTH_TOKEN": "unused",
    "ANTHROPIC_MODEL": "gpt-6-sol[1m]",
    "ANTHROPIC_SMALL_FAST_MODEL": "gpt-6-luna[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "gpt-6-sol[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "gpt-6-sol[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-6-luna[1m]",
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "272000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    "LOOP_TASK_CONTEXT_RUNTIME_VARIANT": "claude_gpt",
}
# Variables a POSIX shell itself may set while running the launcher script.
_SHELL_NOISE = {"PWD", "OLDPWD", "SHLVL", "_"}


def _expected_env(server_url: str, claude_bin: Path) -> dict:
    return {
        **EXPECTED_STATIC_ENV,
        "ANTHROPIC_BASE_URL": server_url,
        "CLAUDE_GPT_CLAUDE_BIN": str(claude_bin),
    }


def _consumer_dir(tmp_path: Path) -> Path:
    """A consumer checkout that is not the platform root, with project config to protect."""
    consumer = tmp_path / "consumer-project"
    (consumer / ".claude").mkdir(parents=True)
    (consumer / ".claude" / "settings.json").write_text('{"consumer": "sentinel"}\n')
    (consumer / ".mcp.json").write_text('{"mcpServers": {}}\n')
    assert consumer.resolve() != H.REPO_ROOT.resolve()
    assert H.REPO_ROOT.resolve() not in consumer.resolve().parents
    return consumer


def _ambient_env(tmp_path: Path, server_url: str, out: Path, **extra) -> dict:
    env = H.base_env(
        tmp_path,
        ANTHROPIC_BASE_URL=server_url,
        FAKE_CLAUDE_OUT=str(out),
        XDG_DATA_HOME=str(tmp_path / "ambient-xdg-data"),
        XDG_STATE_HOME=str(tmp_path / "ambient-xdg-state"),
        GH_CONFIG_DIR=str(tmp_path / "ambient-gh-config"),
        SSH_AUTH_SOCK=str(tmp_path / "ambient-ssh-agent.sock"),
        CONSUMER_UNRELATED_SENTINEL="kept as-is",
        # keep the fake claude's own interpreter from adding LC_CTYPE (PEP 538), so the
        # recorded env shows only what the launcher changed
        PYTHONCOERCECLOCALE="0",
        **extra,
    )
    for key in ("XDG_DATA_HOME", "XDG_STATE_HOME", "GH_CONFIG_DIR"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    # Non-empty user-level config so "nothing was written" is not vacuously true.
    (Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text('{"user": "sentinel"}\n')
    (Path(env["HOME"]) / ".claude.json").write_text('{"home": "sentinel"}\n')
    (Path(env["GH_CONFIG_DIR"]) / "config.yml").write_text("sentinel: true\n")
    return env


def _launch(tmp_path, server_url, claude_args=(), *, cwd=None, **extra_env):
    """Run `launch.sh --claude-bin <fake> -- <claude_args>` from `cwd` (default: consumer)."""
    fake = H.write_fake_claude(tmp_path / "fake-claude")
    out = tmp_path / "fake-claude-out.json"
    env = _ambient_env(tmp_path, server_url, out, **extra_env)
    consumer = cwd or _consumer_dir(tmp_path)
    proc = H.run_launcher(["--claude-bin", str(fake), "--", *claude_args], env, cwd=consumer)
    observed = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return proc, observed, env, fake, consumer


def _snapshot(*roots: Path) -> dict:
    files = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files[str(path)] = path.read_bytes()
    return files


# ---------------------------------------------------------------------------
# caller cwd / ambient identity
# ---------------------------------------------------------------------------


def test_caller_cwd_is_retained_when_it_differs_from_the_platform_root(tmp_path):
    with H.FakeServer() as server:
        proc, observed, _env, _fake, consumer = _launch(tmp_path, server.url, ("-p", "x"))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert observed is not None
    assert Path(observed["cwd"]).resolve() == consumer.resolve()
    assert Path(observed["cwd"]).resolve() != H.REPO_ROOT.resolve()
    # provenance still names the platform launcher, not the consumer checkout
    assert f"launcher={H.SCRIPT_DIR / 'launch.sh'} " in proc.stderr


def test_ambient_home_xdg_config_gh_and_ssh_agent_are_preserved(tmp_path):
    with H.FakeServer() as server:
        proc, observed, env, _fake, _consumer = _launch(tmp_path, server.url)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    child = observed["env"]
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "CLAUDE_CONFIG_DIR",
        "GH_CONFIG_DIR",
        "SSH_AUTH_SOCK",
    ):
        assert child[key] == env[key], key
    # Every caller variable not owned by the launcher reaches claude unchanged.
    owned = set(_expected_env(server.url, Path("x")))
    changed = {k: (env[k], child.get(k)) for k in env if k not in owned and child.get(k) != env[k]}
    assert changed == {}
    # CLAUDE_GPT_HOME (repair helper only) is never exported as a config root for claude.
    assert child["HOME"] != env["CLAUDE_GPT_HOME"]
    assert child["CLAUDE_CONFIG_DIR"] != env["CLAUDE_GPT_HOME"]


def test_ambient_user_and_project_config_files_are_not_touched(tmp_path):
    consumer = _consumer_dir(tmp_path)
    with H.FakeServer() as server:
        env_probe = _ambient_env(tmp_path, server.url, tmp_path / "unused.json")
        roots = [
            consumer,
            Path(env_probe["HOME"]),
            Path(env_probe["CLAUDE_CONFIG_DIR"]),
            Path(env_probe["XDG_CONFIG_HOME"]),
            Path(env_probe["GH_CONFIG_DIR"]),
        ]
        before = _snapshot(*roots)
        assert len(before) >= 4, "sentinel config must exist so the comparison is meaningful"
        proc, observed, _env, _fake, _c = _launch(tmp_path, server.url, ("-p", "x"), cwd=consumer)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert observed is not None
    assert _snapshot(*roots) == before


# ---------------------------------------------------------------------------
# exact routing identifiers, no injection
# ---------------------------------------------------------------------------


def test_exact_model_env_and_routing_identifiers_are_retained(tmp_path):
    with H.FakeServer() as server:
        proc, observed, env, fake, _consumer = _launch(tmp_path, server.url)
        dry = H.run_launcher(["--dry-run", "--claude-bin", str(fake)], env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    child = observed["env"]
    expected = _expected_env(server.url, fake)
    assert {k: child.get(k) for k in expected} == expected
    # The launcher adds exactly this set and nothing else (no settings / MCP / hook carrier).
    added = set(child) - set(env) - _SHELL_NOISE
    assert added == set(expected) - set(env)
    # --dry-run reports the same contract the claude child actually receives.
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["launch_env"] == expected
    assert dry_payload["isolation"] == "none"
    assert dry_payload["generated_settings"] is False


@pytest.mark.parametrize(
    "claude_args",
    [
        (),
        ("--resume", "fake-resume-target-0001"),
        (
            "--resume",
            "fake-resume-target-0002",
            "--effort",
            "high",
            "-p",
            "two  spaced words",
            "",
            "*",
            "$HOME",
            "--",
            "trailing",
        ),
    ],
)
def test_caller_argv_is_forwarded_unchanged_without_injected_flags(tmp_path, claude_args):
    with H.FakeServer() as server:
        proc, observed, _env, _fake, _consumer = _launch(tmp_path, server.url, claude_args)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    # Exact equality: no --settings, --permission-mode, --mcp-config, hook or plugin flag
    # is prepended or appended, and quoting / empty arguments survive.
    assert observed["argv"] == list(claude_args)


@pytest.mark.parametrize("status", [0, 1, 42, 130])
def test_fake_claude_exit_status_is_propagated(tmp_path, status):
    with H.FakeServer() as server:
        proc, observed, _env, _fake, _consumer = _launch(
            tmp_path, server.url, ("-p", "x"), FAKE_CLAUDE_EXIT=str(status)
        )
    assert observed is not None, "fake claude must have run"
    assert proc.returncode == status, (proc.stdout, proc.stderr)


# ---------------------------------------------------------------------------
# failed proxy / catalog never reaches claude
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "server_kwargs",
    [
        {"models": ("gpt-6-sol",)},
        {"models": ()},
        {"models_status": 500},
        {"raw_models_body": "not json"},
    ],
)
def test_failed_catalog_never_invokes_fake_claude(tmp_path, server_kwargs):
    with H.FakeServer(**server_kwargs) as server:
        proc, observed, _env, _fake, _consumer = _launch(tmp_path, server.url, ("-p", "x"))
        assert server.alive() and server.listening()
    assert proc.returncode == 7, (proc.stdout, proc.stderr)
    assert observed is None
    assert json.loads(proc.stdout)["status"] == "failed"


@pytest.mark.parametrize(
    "url",
    [
        "closed",
        "http://example.com:18765",
        "http://127.0.0.1:18765/v1",
    ],
)
def test_unreachable_or_invalid_proxy_never_invokes_fake_claude(tmp_path, url):
    if url == "closed":
        url = f"http://127.0.0.1:{H.closed_port()}"
    proc, observed, _env, _fake, _consumer = _launch(tmp_path, url, ("-p", "x"))
    assert proc.returncode == 7, (proc.stdout, proc.stderr)
    assert observed is None
    assert json.loads(proc.stdout)["status"] == "failed"


# ---------------------------------------------------------------------------
# server ownership; catalog success is reachability only
# ---------------------------------------------------------------------------


def test_check_only_and_normal_launch_neither_own_nor_stop_the_server(tmp_path):
    with H.FakeServer() as server:
        pid = server.pid
        env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=server.url)
        check = H.run_launcher(["--check-only"], env, cwd=_consumer_dir(tmp_path))
        assert check.returncode == 0, (check.stdout, check.stderr)
        assert server.alive() and server.listening() and server.proc.pid == pid

        proc, observed, _env, _fake, _consumer = _launch(
            tmp_path, server.url, ("-p", "x"), cwd=tmp_path / "consumer-project"
        )
        assert proc.returncode == 0 and observed is not None
        assert server.alive() and server.listening() and server.proc.pid == pid

        # still serving a complete catalog afterwards
        again = H.run_launcher(["--check-only"], env)
        assert again.returncode == 0, (again.stdout, again.stderr)
        assert server.alive() and server.proc.pid == pid


class _RecordingServer:
    """Loopback server that lists the catalog but rejects every other request (404)."""

    def __init__(self):
        self.hits: list[tuple[str, str]] = []
        hits = self.hits
        catalog = json.dumps({"data": [{"id": m} for m in H.REQUIRED_MODELS]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def _reply(self):
                hits.append((self.command, self.path))
                ok = self.command == "GET" and self.path == "/v1/models"
                body = catalog if ok else b"{}"
                self.send_response(200 if ok else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _reply

            def log_message(self, fmt, *args):
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_positive_catalog_is_reachability_only_not_auth_or_entitlement(tmp_path):
    """A server that lists the models but rejects every inference request still passes:
    the check proves catalog reachability only, never authentication or entitlement."""
    with _RecordingServer() as server:
        env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=server.url)
        check = H.run_launcher(["--check-only"], env)
        proc, observed, _env, _fake, _consumer = _launch(tmp_path, server.url, ("-p", "x"))
    assert check.returncode == 0, (check.stdout, check.stderr)
    assert proc.returncode == 0 and observed is not None
    # Only catalog GETs were made; the launcher sends no message / inference request.
    assert server.hits == [("GET", "/v1/models"), ("GET", "/v1/models")]
    payload = json.loads(check.stdout)
    connected = payload["connected_server"]
    assert connected["model_catalog_ok"] is True
    for key in connected:
        assert "auth" not in key.lower() and "entitle" not in key.lower(), key
    # The only auth-shaped value is the fixed placeholder, not a credential.
    assert payload["launch_env"]["ANTHROPIC_AUTH_TOKEN"] == "unused"
    assert observed["env"]["ANTHROPIC_AUTH_TOKEN"] == "unused"
