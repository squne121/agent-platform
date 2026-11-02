"""scripts/claude-gpt/tests/_proxy_model_compat_fixture_helpers.py

Issue #2801: shared fake `claude-code-proxy` binary fixture builder used by
`test_proxy_model_compatibility.py` and `test_repair_proxy.py`.

`launch.sh` invokes the proxy binary via `env -i` with an explicit allowlist
(Issue #2204 AC4), so env vars set by the *test process* do not reach the
spawned fake proxy child. This helper therefore bakes the desired `--version`
output and `/v1/models` catalog directly into the generated fake proxy
script's source text (as literal constants), mirroring the pattern already
established in `scripts/claude-gpt/test_launch_transport_policy.py` (Issue
#2204), rather than relying on env passthrough.

Not a pytest test module itself (leading underscore -- pytest does not
collect it).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

FAKE_PROXY_SOURCE_TEMPLATE = r"""#!/usr/bin/env python3
import json
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = {models_literal}
VERSION = {version_literal}


def _serve(port: int) -> int:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
            if self.path == "/v1/models":
                body = json.dumps({{"data": [{{"id": m}} for m in MODELS]}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):  # noqa: A002 - silence test server logs
            return

    httpd = HTTPServer(("127.0.0.1", port), Handler)

    def _on_term(signum, frame):
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)
    httpd.serve_forever()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return 1
    if args[0] == "--version":
        print(VERSION)
        return 0
    if args[0] == "codex" and len(args) >= 3 and args[1] == "auth" and args[2] == "status":
        # preflight.sh's ChatGPT subscription auth check (Issue #2158 P0-2)
        # only inspects stdout for an "Account:" line; it is independent of
        # the /v1/models catalog compatibility this fixture exists to
        # control, so a fixed authenticated response keeps that check a
        # no-op for these catalog-compatibility-focused tests.
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


if __name__ == "__main__":
    sys.exit(main())
"""


def build_fake_proxy_source(*, models: list[str], version: str) -> str:
    return FAKE_PROXY_SOURCE_TEMPLATE.format(
        models_literal=json.dumps(models),
        version_literal=json.dumps(version),
    )


def write_fake_proxy(path: Path, *, models: list[str], version: str) -> Path:
    path.write_text(build_fake_proxy_source(models=models, version=version), encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path
