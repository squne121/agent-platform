#!/usr/bin/env python3
"""Stateful fake `gh` for the Stage 2-5 create_issue_txn tests (fixture only, never live GitHub).

State lives in $FAKE_GH_STATE (JSON); every invocation is appended to $FAKE_GH_LOG (one JSON argv per
line). Supported surface is exactly what the label-free create path uses: `issue list` (dedupe /
race poll), `issue create`, `issue comment`, `api --method GET` (existing-body read on a dedupe hit),
`api graphql` (node id readback; a read-only query). Anything else exits 97
and is recorded under state["unexpected"], so an unapproved operation fails closed and is visible.

Fault injection (state keys): list_mode ok|fail|non_json|lag|dup_after_create, lag_calls N,
create_mode ok|fail|applied_then_fail|applied_then_hang|empty_url|garbage_url.
"""

import json
import os
import sys
import time
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
    save()
    sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def flag(name: str) -> str:
    return argv[argv.index(name) + 1]


def unexpected() -> None:
    sys.stderr.write(f"fake gh: unexpected call: {argv}\n")
    state.setdefault("unexpected", []).append(argv)
    save()
    sys.exit(97)


repo = state["repo"]

if "--repo" in argv and flag("--repo") != repo:
    sys.stderr.write(f"fake gh: --repo mismatch: {flag('--repo')}\n")
    state.setdefault("unexpected", []).append(argv)
    save()
    sys.exit(97)
if argv[:2] == ["api", "graphql"] and any("mutation" in a for a in argv):
    unexpected()

if argv[:2] == ["issue", "list"]:
    state["list_calls"] = state.get("list_calls", 0) + 1
    mode = state.get("list_mode", "ok")
    if mode == "fail":
        save()
        sys.stderr.write("list boom\n")
        sys.exit(1)
    if mode == "non_json":
        save()
        sys.stdout.write("<html>not json</html>")
        sys.exit(0)
    wanted = flag("--search").split('"', 1)[1].rsplit('"', 1)[0]
    items = [
        {"number": i["number"], "title": i["title"], "url": i["url"]}
        for i in state["issues"]
        if i["state"] == "OPEN" and i["title"] == wanted
    ]
    if mode == "lag" and state["list_calls"] <= state.get("lag_calls", 0):
        items = []
    if mode == "dup_after_create" and state.get("created"):
        items = items + [
            {"number": 900, "title": wanted, "url": f"https://github.com/{repo}/issues/900"}
        ]
    out(items)

if argv[:2] == ["issue", "create"]:
    state["create_calls"] = state.get("create_calls", 0) + 1
    mode = state.get("create_mode", "ok")
    if mode == "fail":
        save()
        sys.stderr.write("create boom\n")
        sys.exit(1)
    body = (
        Path(flag("--body-file")).read_text(encoding="utf-8")
        if "--body-file" in argv
        else flag("--body")
    )
    number = state["next_number"]
    url = f"https://github.com/{repo}/issues/{number}"
    state["next_number"] = number + 1
    state["issues"].append(
        {"number": number, "title": flag("--title"), "body": body, "url": url, "state": "OPEN"}
    )
    state["created"] = True
    if mode == "applied_then_fail":
        save()
        sys.stderr.write("boom after apply\n")
        sys.exit(1)
    if mode == "applied_then_hang":
        save()
        time.sleep(30)
        sys.exit(1)
    if mode == "empty_url":
        save()
        sys.exit(0)
    if mode == "garbage_url":
        save()
        sys.stdout.write("created something\n")
        sys.exit(0)
    save()
    sys.stdout.write(url + "\n")
    sys.exit(0)

if argv[:2] == ["issue", "comment"]:
    state.setdefault("comments", []).append(argv)
    save()
    sys.exit(0)

if argv[:1] == ["api"] and "--method" in argv and flag("--method") == "GET":
    path = next((a for a in argv if a.startswith("/repos/")), "")
    hit = [i for i in state["issues"] if path.endswith(f"/issues/{i['number']}")]
    if not hit:
        unexpected()
    out({"number": hit[0]["number"], "title": hit[0]["title"], "body": hit[0]["body"]})

if argv[:2] == ["api", "graphql"]:
    if state.get("graphql_broken"):
        save()
        sys.stderr.write("graphql boom\n")
        sys.exit(1)
    number = int(next(a.split("=", 1)[1] for a in argv if a.startswith("number=")))
    hit = [i for i in state["issues"] if i["number"] == number]
    if not hit:
        out({"data": {"repository": {"issue": None}}})
    out(
        {"data": {"repository": {"issue": {"id": f"I_node_{number}", "databaseId": 7000 + number}}}}
    )

unexpected()
