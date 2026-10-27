#!/usr/bin/env python3
"""Stateful fake `gh` for the Stage 2-4 real-subprocess tests.

State lives in $FAKE_GH_STATE (JSON), every invocation is appended to $FAKE_GH_LOG (one JSON argv per
line). Anything outside the small supported surface exits 97 and is logged as unexpected, so a test
fails closed instead of silently no-op'ing. Write fault injection is the state key `patch_mode` /
`post_mode`: ok | fail | noop (rc 0, nothing applied) | applied_then_fail (applied, rc 1).
"""

import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
state_path = Path(os.environ["FAKE_GH_STATE"])
log_path = Path(os.environ["FAKE_GH_LOG"])
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")
state = json.loads(state_path.read_text(encoding="utf-8"))


def save() -> None:
    state_path.write_text(json.dumps(state), encoding="utf-8")


def out(obj) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def unexpected() -> None:
    sys.stderr.write(f"fake gh: unexpected call: {argv}\n")
    state.setdefault("unexpected", []).append(argv)
    save()
    sys.exit(97)


def write(mode: str, apply) -> None:
    save()  # persist the call counters even when the write itself fails
    if mode == "fail":
        sys.stderr.write("boom\n")
        sys.exit(1)
    if mode == "applied_then_fail":
        apply()
        save()
        sys.stderr.write("boom after apply\n")
        sys.exit(1)
    if mode == "noop":
        out({})
    result = apply()
    save()
    out(result)


if argv[:2] == ["auth", "status"]:
    sys.stdout.write("Logged in to github.com account fake-test-user\n")
    sys.exit(0)

if argv[:2] == ["issue", "view"]:
    fields = argv[argv.index("--json") + 1].split(",") if "--json" in argv else []
    if fields == ["comments"]:
        out({"comments": state["comments"]})
    out({k: state[{"updatedAt": "updated_at"}.get(k, k)] for k in fields})

if argv[:1] == ["api"]:
    method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
    path = next((x for x in argv if x.startswith("repos/")), "")
    if method == "GET" and path.endswith(f"/issues/{state['number']}"):
        out(
            {
                "title": state["title"],
                "body": state["body"],
                "updatedAt": state["updated_at"],
                "isPullRequest": False,
            }
        )
    if method == "PATCH" and path.endswith(f"/issues/{state['number']}"):
        state["patches"] += 1
        if "--input" in argv:
            data = json.loads(Path(argv[argv.index("--input") + 1]).read_text(encoding="utf-8"))
        else:
            spec = argv[argv.index("--field") + 1].split("=@", 1)[1]
            data = {"title": state["title"], "body": Path(spec).read_text(encoding="utf-8")}

        def apply_patch():
            state["title"], state["body"] = data["title"], data["body"]
            state["updated_at"] = "2026-01-02T00:00:00Z"
            return {}

        write(state["patch_mode"], apply_patch)
    if method == "POST" and path.endswith("/comments"):
        state["posts"] += 1
        body = Path(argv[argv.index("--field") + 1].split("=@", 1)[1]).read_text(encoding="utf-8")

        def apply_post():
            n = len(state["comments"]) + 1
            url = f"https://github.com/{state['repo']}/issues/{state['number']}#issuecomment-{9000 + n}"
            state["comments"].append({"id": f"IC_{n}", "url": url, "body": body})
            return {"html_url": url, "id": 9000 + n}

        write(state["post_mode"], apply_post)
unexpected()
