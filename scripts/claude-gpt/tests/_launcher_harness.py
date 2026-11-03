"""scripts/claude-gpt/tests/_launcher_harness.py

Issue #2925: 実 `scripts/claude-gpt/launch.sh` を subprocess で駆動する focused test の共有
harness（pytest は先頭 underscore のため collect しない）。

- `start_fake_server`: 実 TCP port に bind する別 process の fake `claude-code-proxy`
  （`/v1/models` と `/healthz` を返す）。launcher が **所有しない** server が launcher 終了後も
  生き続けることを、実 PID で検証するために separate process にしている。
- `write_fake_claude`: 起動時の env と argv を JSON file へ書き出して終了する fake `claude`。
  launcher が `exec` した後の子 process が実際に受け取った env を観測する。

launcher の判定ロジックは一切再実装しない。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Self

SCRIPT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPT_DIR.parent.parent
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"

REQUIRED_MODELS = ("gpt-6-sol", "gpt-6-luna")

_FAKE_SERVER_SOURCE = textwrap.dedent(
    """
    import json
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    config = json.loads(sys.argv[1])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/v1/models" and config.get("require_key") is not None:
                if self.headers.get("Authorization") != "Bearer " + config["require_key"]:
                    body = b'{"error":"unauthorized"}'
                    self.send_response(401)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            if self.path == "/v1/models":
                status = config["models_status"]
                if config.get("raw_models_body") is not None:
                    body = config["raw_models_body"].encode()
                else:
                    body = json.dumps({"data": [{"id": m} for m in config["models"]]}).encode()
            elif self.path == "/healthz":
                status = 200
                body = b'{"ok":true}'
            else:
                status = 404
                body = b"{}"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    print(httpd.server_address[1], flush=True)
    httpd.serve_forever()
    """
)

_FAKE_CLAUDE_SOURCE = """#!{python}
import json
import os
import sys

with open(os.environ["FAKE_CLAUDE_OUT"], "w", encoding="utf-8") as fh:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}}, fh)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""


class FakeServer:
    """Separate-process fake claude-code-proxy bound to a real loopback TCP port."""

    def __init__(
        self,
        models=REQUIRED_MODELS,
        models_status: int = 200,
        raw_models_body: str | None = None,
        require_key: str | None = None,
    ):
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _FAKE_SERVER_SOURCE,
                json.dumps(
                    {
                        "models": list(models),
                        "models_status": models_status,
                        "raw_models_body": raw_models_body,
                        "require_key": require_key,
                    }
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        self.port = int(self.proc.stdout.readline().strip())
        self.pid = self.proc.pid

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def listening(self) -> bool:
        with socket.socket() as sock:
            sock.settimeout(1.0)
            return sock.connect_ex(("127.0.0.1", self.port)) == 0

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.proc.stdout is not None:
            self.proc.stdout.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_fake_claude(path: Path) -> Path:
    path.write_text(_FAKE_CLAUDE_SOURCE.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def base_env(tmp_path: Path, **overrides) -> dict:
    """Hermetic parent env: sentinel HOME/XDG/config roots so isolation is observable."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "ambient-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "ambient-xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "ambient-xdg-cache"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "ambient-claude-config"),
        "CLAUDE_GPT_HOME": str(tmp_path / "claude-gpt-home"),
    }
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    env.update({k: v for k, v in overrides.items() if v is not None})
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
    return env


def run_launcher(
    args, env: dict, *, timeout: float = 60.0, cwd=None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(LAUNCH_SH), *args],
        check=False,
        env=env,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_launcher_with_fake_claude(tmp_path: Path, server_url: str, extra_args=(), **env_overrides):
    """Run `launch.sh -- <extra_args>` against `server_url` with a fake claude.

    Returns (completed_process, observed) where `observed` is the JSON the fake claude
    wrote (None when the launcher never reached the exec)."""
    fake = write_fake_claude(tmp_path / "fake-claude")
    out = tmp_path / "fake-claude-out.json"
    env = base_env(
        tmp_path,
        ANTHROPIC_BASE_URL=server_url,
        FAKE_CLAUDE_OUT=str(out),
        **env_overrides,
    )
    proc = run_launcher(["--claude-bin", str(fake), "--", *extra_args], env)
    observed = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return proc, observed, env
