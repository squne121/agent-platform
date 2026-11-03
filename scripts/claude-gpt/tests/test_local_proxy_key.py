"""Issue #64: CLIProxyAPI 等のローカル client key 対応の offline 回帰試験（mock / dummy key）。

実 SIWC / 実 GPT の証跡ではない。key はテスト用ダミーのみ。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "claude_gpt_launcher_harness_64_key", Path(__file__).resolve().parent / "_launcher_harness.py"
)
assert _spec is not None and _spec.loader is not None
_H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_H)
FakeServer = _H.FakeServer
base_env = _H.base_env
run_launcher = _H.run_launcher
run_launcher_with_fake_claude = _H.run_launcher_with_fake_claude
write_fake_claude = _H.write_fake_claude

DUMMY_KEY = "dummy-local-key-0123456789"


def _check_only(tmp_path, server, **env):
    e = base_env(tmp_path, ANTHROPIC_BASE_URL=server.url, **env)
    return run_launcher(["--check-only"], e)


def test_anonymous_models_probe_is_401_exit_7_without_key(tmp_path):
    with FakeServer(require_key=DUMMY_KEY) as srv:
        proc = _check_only(tmp_path, srv)
    assert proc.returncode == 7
    assert "401" in proc.stdout + proc.stderr
    assert "CLAUDE_GPT_PROXY_API_KEY" in proc.stdout + proc.stderr


def test_valid_key_check_only_succeeds_and_never_prints_key(tmp_path):
    with FakeServer(require_key=DUMMY_KEY) as srv:
        proc = _check_only(tmp_path, srv, CLAUDE_GPT_PROXY_API_KEY=DUMMY_KEY)
    assert proc.returncode == 0, proc.stderr
    assert DUMMY_KEY not in proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["connected_server"]["models_http_status"] == 200


def test_wrong_key_fails_with_401_and_does_not_leak(tmp_path):
    with FakeServer(require_key=DUMMY_KEY) as srv:
        proc = _check_only(tmp_path, srv, CLAUDE_GPT_PROXY_API_KEY="wrong-key-abcdef")
    assert proc.returncode == 7
    assert "wrong-key-abcdef" not in proc.stdout + proc.stderr


def test_dry_run_redacts_key(tmp_path):
    env = base_env(tmp_path, CLAUDE_GPT_PROXY_API_KEY=DUMMY_KEY)
    proc = run_launcher(["--dry-run"], env)
    assert proc.returncode == 0
    assert DUMMY_KEY not in proc.stdout + proc.stderr
    assert "<redacted" in proc.stdout


def test_key_is_exported_to_claude_as_auth_token(tmp_path):
    with FakeServer(require_key=DUMMY_KEY) as srv:
        proc, observed, _ = run_launcher_with_fake_claude(
            tmp_path, srv.url, ("-p", "x"), CLAUDE_GPT_PROXY_API_KEY=DUMMY_KEY
        )
    assert proc.returncode == 0, proc.stderr
    assert observed["env"]["ANTHROPIC_AUTH_TOKEN"] == DUMMY_KEY
    assert "CLAUDE_GPT_PROXY_API_KEY" not in observed["env"]
    assert DUMMY_KEY not in proc.stdout + proc.stderr


def test_legacy_default_keeps_placeholder_token(tmp_path):
    with FakeServer() as srv:
        proc, observed, _ = run_launcher_with_fake_claude(tmp_path, srv.url)
    assert proc.returncode == 0
    assert observed["env"]["ANTHROPIC_AUTH_TOKEN"] == "unused"
    assert observed["env"]["ANTHROPIC_MODEL"] == "gpt-6-sol[1m]"


def test_malformed_key_is_rejected_before_any_probe(tmp_path):
    fake = write_fake_claude(tmp_path / "fc")
    env = base_env(tmp_path, CLAUDE_GPT_PROXY_API_KEY='bad"key\nx')
    proc = run_launcher(["--claude-bin", str(fake), "--check-only"], env)
    assert proc.returncode == 2
    assert "invalid_proxy_api_key_format" in proc.stderr
    assert 'bad"key' not in proc.stdout + proc.stderr


def test_non_loopback_host_rejected_even_with_key(tmp_path):
    env = base_env(
        tmp_path, ANTHROPIC_BASE_URL="http://example.com:8317", CLAUDE_GPT_PROXY_API_KEY=DUMMY_KEY
    )
    proc = run_launcher(["--check-only"], env)
    assert proc.returncode == 7
    assert DUMMY_KEY not in proc.stdout + proc.stderr


def test_model_override_flows_to_catalog_and_claude_env(tmp_path):
    with FakeServer(models=("gpt-5.6-luna",), require_key=DUMMY_KEY) as srv:
        proc, observed, _ = run_launcher_with_fake_claude(
            tmp_path,
            srv.url,
            CLAUDE_GPT_PROXY_API_KEY=DUMMY_KEY,
            CLAUDE_GPT_MODEL_MAIN="gpt-5.6-luna",
            CLAUDE_GPT_MODEL_SMALL_FAST="gpt-5.6-luna",
            CLAUDE_GPT_MODEL_OPUS="gpt-5.6-luna",
            CLAUDE_GPT_MODEL_SONNET="gpt-5.6-luna",
            CLAUDE_GPT_MODEL_HAIKU="gpt-5.6-luna",
        )
    assert proc.returncode == 0, proc.stderr
    assert observed["env"]["ANTHROPIC_MODEL"] == "gpt-5.6-luna"
    assert observed["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "gpt-5.6-luna"
