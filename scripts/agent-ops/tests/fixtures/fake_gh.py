#!/usr/bin/env python3
"""Deterministic fake `gh` binary for Issue #2299 `runtime_smoke_test.sh
--scenario issue_create` (AC2/AC5), Issue #2273 P0-3/P1-4 producer/consumer
integration tests, and future create-issue fake-provider harnesses.

Understands the small, fixed subset of `gh` invocations that the
`create-issue` skill's dedupe step and `create_issue_txn.run_transaction()`
issue when labels/parent/dependencies are all empty: `gh issue list`
(dedupe search, any additional flags such as `--search`/`--state`/`--limit`/
`--json` are accepted and ignored beyond their presence), `gh issue create`,
and `gh api graphql` (node/database id readback); plus `gh auth status`,
`gh repo view`, and `gh issue comment`/`gh issue view` needed by the
`workflow_capability_preflight.py` <-> `root_entry_router.py`
producer/consumer integration tests (Issue #2273 P1-4). Any other invocation
is rejected with a non-zero exit so a scenario relying on unsupported `gh`
subcommands fails closed instead of silently no-op'ing.

`gh auth status` / `gh repo view` success is controlled via the
`FAKE_GH_AUTH_OK` / `FAKE_GH_REPO_READ_OK` environment variables (default:
both "1"/available) so tests can force a blocked capability preflight
without needing real GitHub credentials. When `FAKE_GH_AUTH_OK` is unset or
"1", `gh auth status` prints `Logged in to github.com account fake-test-user`
(matching real `gh` output) so ambient-auth preflight checks that parse this
line also pass.

Issue #2306 (AC2) additionally teaches this fixture: `--body`/`--body-file`
handling on `issue create`, `repo`/`state` persistence, `gh issue view
<number> --repo R --json fields` (used by `live_issue_create_canary.sh`
identity verification), `gh issue list --search Q --repo R --json fields`
title/repo-filtered fallback search, real `gh issue close` state mutation
(instead of the previous unconditional no-op), and a normalized `calls[]`
trace (operation / repo / primary subcommand only -- not exact argv, not
strict ordering, not exact-count assertions).

Fault-injection knobs (env vars, deterministic-test-only, never read by
production code):
  FAKE_GH_CLOSE_SHOULD_FAIL=1  -- `issue close` exits 1 without mutating state.
  FAKE_GH_CLOSE_NOOP=1         -- `issue close` exits 0 but leaves state
                                   unmutated (simulates a state-readback
                                   mismatch after an apparently successful
                                   close).

Issue #2306 follow-up (P1-d): `gh auth status` is now also understood (exits
0 and prints the ambient-auth success line unless `FAKE_GH_AUTH_OK=0`) so
`scripts/claude-gpt/tests/test_live_issue_create_canary.py`'s real-subprocess
`main()`/EXIT-trap tests (which run the unmodified production
`live_issue_create_canary.sh` -- not just its sourced helper functions --
against this fake `gh`) can pass the production script's `gh auth status
--hostname github.com` ambient-auth preflight check without touching real
GitHub auth, while Issue #2273's blocked-capability-preflight tests can still
force a failure via `FAKE_GH_AUTH_OK=0`.

State is persisted across invocations (each `gh` call is a fresh subprocess)
via a JSON file at $FAKE_GH_STATE.

Issue #2278 (`runtime_smoke_test.sh --scenario issue_to_impl`) additionally
teaches this fixture: `FAKE_GH_SEED_ISSUES_PATH` (a JSON file with the same
`{"next_number": int, "issues": {...}}` shape as the persisted state file,
consulted only when no `$FAKE_GH_STATE` file exists yet) so a fixture Issue
can be pre-seeded before any live `gh issue view` call, and a `labels` field
on `issue view`/`issue list` payloads (empty list when absent) so readers
that request `--json ...,labels,...` observe the same field shape real `gh`
returns.

Originally salvaged from the Issue #2259 bridge test fixture
(`.claude/worktrees/issue-2259-isolated-issue-create-bridge/scripts/
claude-gpt/tests/fixtures/fake_gh.py`, never merged to main) and adapted for
the native (non-bridge) live scenario harness (Issue #2299), then extended
for Issue #2306.

Issue #2330 additionally teaches this fixture the exact `gh` CLI shapes the
production consumer `.claude/skills/issue-refinement-loop/scripts/
root_entry_router.py` actually issues (a compatibility gap PR #2325's
pr-reviewer flagged as non-blocking, later confirmed authoritative by an
OWNER adversarial review):

  - `gh repo view <owner>/<repo> --json nameWithOwner --jq .nameWithOwner`
    -- a POSITIONAL repo argument directly after `view` (not `--repo
    <repo>`), with a bare-string (`--jq`-filtered, not JSON-quoted) stdout
    of the repo identity. The prior `--repo <repo>` flag form is still
    accepted for backward compatibility.
  - `gh api repos/<owner>/<repo>/git/refs/heads/<base_ref> --jq
    .object.sha` -- `<base_ref>` is the full suffix after
    `/git/refs/heads/` (which may itself contain `/`, e.g. `release/next`,
    not just `main`), and the fixture prints a deterministic 40-hex fake
    SHA as a bare string (no JSON quoting), derived from the repo path +
    ref so different refs/repos produce stable-but-distinct fake SHAs.
    Only this exact `--jq .object.sha` argv shape is understood -- this is
    not a generic REST router or jq evaluator (Out of Scope, Issue #2330).

PR #2377 OWNER REQUEST_CHANGES fix_delta (P1-1): both shapes above are now
EXACT-argv contracts, not containment/substring checks -- an argv with an
extra trailing token (e.g. a stray `--method DELETE`), an empty `base_ref`,
a missing `owner/repo` structure, an unexpected path segment before the
`/git/refs/heads/` marker, or a wrong/extra `--jq` selector on the `repo
view` shape now fails closed (non-zero exit) instead of being silently
accepted as a near-miss success.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys

# Issue #2330 fix_delta (P1-1): exact-argv contract for the
# `gh api repos/<owner>/<repo>/git/refs/heads/<base_ref> --jq .object.sha`
# endpoint shape -- requires a non-empty `owner/repo` pair and a non-empty
# `base_ref` suffix (which may itself contain further `/`).
_REF_SHA_ENDPOINT_RE = re.compile(r"^(repos/[^/]+/[^/]+)/git/refs/heads/(.+)$")


def _load(state_path: str) -> dict:
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            return json.load(fh)
    # Issue #2278 AC2/AC3: `--scenario issue_to_impl` seeds the fake `gh`
    # state with a fixture Issue (issue-2230-equivalent/issue.json) BEFORE
    # any `gh issue create` call happens, so a live `gh issue view` against a
    # pre-existing fixture Issue number succeeds on the very first
    # invocation. Only consulted when no state file exists yet (first call of
    # a fresh $FAKE_GH_STATE); once state has been persisted once, this seed
    # path is never re-read (matches the existing single-source-of-truth
    # persisted-state contract above).
    seed_path = os.environ.get("FAKE_GH_SEED_ISSUES_PATH")
    if seed_path and os.path.exists(seed_path):
        with open(seed_path, encoding="utf-8") as fh:
            seed = json.load(fh)
        issues = seed.get("issues", {}) if isinstance(seed, dict) else {}
        next_number = seed.get("next_number", 9001) if isinstance(seed, dict) else 9001
        return {"next_number": next_number, "issues": issues, "calls": []}
    return {"next_number": 9001, "issues": {}, "calls": []}


def _save(state_path: str, state: dict) -> None:
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def _record_call(state: dict, operation: str, repo, subcommand, number=None) -> None:
    """Append a normalized call trace entry.

    Only operation / repo / the primary (non-flag) subcommand tokens are
    recorded -- callers must not assert exact argv, exact ordering across
    unrelated operations, or exact call counts beyond what a given test
    explicitly sets up (Issue #2306 In Scope wording).

    Issue #2278 (`runtime_smoke_test.sh --scenario issue_to_impl`) PR #2325
    fix_delta (P0-2): `number` is additionally recorded (when the caller
    passes one) so a consumer can confirm CAUSALLY that a `gh issue view
    <specific number>` call happened -- not merely that SOME `issue view`
    call happened at some point -- without requiring exact-argv/exact-
    ordering assertions on the rest of the trace.
    """
    calls = state.setdefault("calls", [])
    entry = {"operation": operation, "repo": repo, "subcommand": subcommand}
    if number is not None:
        entry["number"] = number
    calls.append(entry)


def _extract_flag(args, flag):
    for i, value in enumerate(args):
        if value == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def _resolve_body(args):
    body = _extract_flag(args, "--body")
    if body is not None:
        return body
    body_file = _extract_flag(args, "--body-file")
    if body_file is not None:
        if body_file == "-":
            return sys.stdin.read()
        with open(body_file, encoding="utf-8") as fh:
            return fh.read()
    return None


def main() -> int:
    state_path = os.environ.get("FAKE_GH_STATE")
    if not state_path:
        print("FAKE_GH_STATE not set", file=sys.stderr)
        return 1
    args = sys.argv[1:]
    state = _load(state_path)

    if args[:2] == ["auth", "status"]:
        if os.environ.get("FAKE_GH_AUTH_OK", "1") != "1":
            return 1
        # Always report authenticated when not forced to fail; matches real
        # `gh auth status` output so ambient-auth preflight checks that parse
        # this line also pass. See module docstring (Issue #2306 follow-up
        # P1-d, Issue #2273 P1-4).
        print("Logged in to github.com account fake-test-user")
        return 0

    if args[:2] == ["repo", "view"]:
        if os.environ.get("FAKE_GH_REPO_READ_OK", "1") != "1":
            return 1
        # Issue #2330 fix_delta (P1-1, PR #2377 OWNER REQUEST_CHANGES): the
        # bare/`--jq`-filtered output the production consumer
        # (`root_entry_router.py`) depends on is only emitted for this EXACT
        # positional-repo argv shape -- `["repo", "view", <owner>/<repo>,
        # "--json", "nameWithOwner", "--jq", ".nameWithOwner"]` (7 tokens,
        # nothing more, nothing less). A previous substring/containment-only
        # check (`"--jq" in args and ".nameWithOwner" in args`) let near-miss
        # argv (wrong `--jq` selector, extra trailing tokens) through as a
        # false success; any near-miss now fails closed instead.
        exact_positional_identity_read = (
            len(args) == 7
            and not args[2].startswith("-")
            and args[3:] == ["--json", "nameWithOwner", "--jq", ".nameWithOwner"]
        )
        if exact_positional_identity_read:
            # Real `gh --jq` output is raw/unquoted text, not JSON.
            print(args[2])
            return 0
        if "--jq" in args:
            # A near-miss of the exact positional+jq shape above (wrong
            # selector, extra tokens, `--repo` flag combined with `--jq`,
            # etc.) -- fail closed rather than silently falling back to the
            # legacy JSON-quoted output below.
            return 1

        # Legacy `--repo <repo>` flag form (Issue #2273/#2306-era callers),
        # JSON-quoted output, no `--jq` involved. Still accepted for
        # backward compatibility with existing callers.
        repo = None
        for i, value in enumerate(args):
            if value == "--repo" and i + 1 < len(args):
                repo = args[i + 1]
        if "--json" in args and "nameWithOwner" in args:
            print(json.dumps(repo or "fake/repo"))
        return 0

    if args[:2] == ["issue", "comment"]:
        number = args[2] if len(args) > 2 else None
        body = None
        for i, value in enumerate(args):
            if value == "--body" and i + 1 < len(args):
                body = args[i + 1]
        if number is not None:
            record = state.setdefault("comments", {}).setdefault(str(number), [])
            record.append(body or "")
            _save(state_path, state)
        return 0

    if args[:2] == ["issue", "view"] and "comments" in args:
        number = args[2] if len(args) > 2 else None
        comments = state.get("comments", {}).get(str(number), [])
        print(json.dumps(comments))
        return 0

    if args[:2] == ["issue", "list"]:
        repo = _extract_flag(args, "--repo")
        search = _extract_flag(args, "--search")
        _record_call(state, "issue_list", repo, ["issue", "list"])
        _save(state_path, state)
        out = []
        for number, info in state["issues"].items():
            if repo is not None and info.get("repo") != repo:
                continue
            if search and search not in info.get("title", ""):
                continue
            out.append(
                {
                    "number": int(number),
                    "title": info["title"],
                    "url": info["url"],
                    "body": info.get("body", ""),
                    "state": info.get("state", "open"),
                    "labels": info.get("labels", []),
                }
            )
        print(json.dumps(out))
        return 0

    if args[:2] == ["issue", "create"]:
        title = _extract_flag(args, "--title")
        repo = _extract_flag(args, "--repo")
        body = _resolve_body(args) or ""
        number = state["next_number"]
        state["next_number"] += 1
        url = f"https://github.com/{repo}/issues/{number}"
        state["issues"][str(number)] = {
            "title": title,
            "url": url,
            "repo": repo,
            "body": body,
            "state": "open",
        }
        _record_call(state, "issue_create", repo, ["issue", "create"])
        _save(state_path, state)
        print(url)
        return 0

    if args[:2] == ["issue", "view"]:
        number = args[2] if len(args) > 2 and not args[2].startswith("-") else None
        repo = _extract_flag(args, "--repo")
        # Issue #2278 PR #2325 fix_delta (P0-2): record the SPECIFIC issue
        # number in the call trace (not just "some issue_view call
        # happened"), so a consumer can confirm a `gh issue view <N>` call
        # against a specific fixture Issue genuinely occurred.
        _record_call(state, "issue_view", repo, ["issue", "view"], number=number)
        _save(state_path, state)
        info = state["issues"].get(str(number)) if number else None
        if info is None or (repo is not None and info.get("repo") != repo):
            print(f"no issue found for number {number}", file=sys.stderr)
            return 1
        payload = {
            "number": int(number),
            "title": info.get("title"),
            "url": info.get("url"),
            "body": info.get("body", ""),
            "state": info.get("state", "open"),
            # Issue #2278 AC3: additionally surface `labels` (when present in
            # seeded/created state) so a live `gh issue view ... --json
            # title,body,labels,comments` reader (the `implement-issue` /
            # `issue-contract-review` skill shape) observes the same field
            # set real `gh` would return, instead of a silently-absent key.
            "labels": info.get("labels", []),
        }
        json_fields_raw = _extract_flag(args, "--json")
        requested_fields = set(json_fields_raw.split(",")) if json_fields_raw else set()
        if not json_fields_raw or "comments" in requested_fields:
            # Issue #2278 PR #2325 fix_delta (P1-2): a COMBINED `--json
            # title,body,labels,comments` flag is a single argv token, so
            # the "comments" in args" exact-token check above (kept for
            # backward compatibility with its existing callers) never fires
            # for it -- the generic payload previously silently omitted
            # `comments` in that case. Real `gh issue view --json ...`
            # always includes exactly the fields the caller asked for.
            payload["comments"] = state.get("comments", {}).get(str(number), [])
        print(json.dumps(payload))
        return 0

    if args[:2] == ["issue", "close"]:
        number = args[2] if len(args) > 2 and not args[2].startswith("-") else None
        repo = _extract_flag(args, "--repo")
        _record_call(state, "issue_close", repo, ["issue", "close"])
        if os.environ.get("FAKE_GH_CLOSE_SHOULD_FAIL") == "1":
            _save(state_path, state)
            print("fake gh: forced issue close failure", file=sys.stderr)
            return 1
        info = state["issues"].get(str(number)) if number else None
        if info is None or (repo is not None and info.get("repo") != repo):
            _save(state_path, state)
            print(f"no issue found for number {number}", file=sys.stderr)
            return 1
        if os.environ.get("FAKE_GH_CLOSE_NOOP") != "1":
            info["state"] = "closed"
        _save(state_path, state)
        return 0

    if args[:2] == ["api", "--hostname"]:
        # Issue #2340 AC2/AC6: `workflow_capability_preflight.py`'s
        # `controlled_github_read` probe issues exactly
        # `gh api --hostname <host> repos/<repo> --jq {name}` (sanitized-env,
        # trusted-host-pinned, same shape the controlled executor's own read
        # helpers use). Independent toggle from `FAKE_GH_REPO_READ_OK` (the
        # plain `gh repo view` root-shell check) so a fixture can express the
        # exact root-read-passes/controlled-read-fails asymmetry AC2 exists to
        # catch. Defaults to "1" (succeeds) so callers that don't care about
        # this distinction are unaffected.
        if os.environ.get("FAKE_GH_CONTROLLED_READ_OK", "1") != "1":
            print("fake gh: forced controlled-read probe failure", file=sys.stderr)
            return 1
        print(json.dumps({"name": "repo"}))
        return 0

    if len(args) == 4 and args[0] == "api" and args[2:] == ["--jq", ".object.sha"]:
        # Issue #2330 fix_delta (P1-1, PR #2377 OWNER REQUEST_CHANGES): the
        # production consumer (root_entry_router.py) issues `gh api
        # repos/<owner>/<repo>/git/refs/heads/<base_ref> --jq .object.sha`.
        # `<base_ref>` is NOT limited to a single path segment (i.e. not
        # just `main`) -- it is everything after the `/git/refs/heads/`
        # marker, which may itself contain `/` (e.g. `release/next`). This
        # is now an EXACT argv contract: `len(args) == 4` (no extra trailing
        # tokens such as a stray `--method DELETE`) and `args[1]` must
        # `re.fullmatch` `repos/<owner>/<repo>/git/refs/heads/<non-empty
        # base_ref>` -- a prior containment-only check (`startswith("repos/")`
        # + `"/git/refs/heads/" in args[1]`) let near-miss endpoints (empty
        # base_ref, missing `owner/repo` structure, an unexpected segment
        # before the marker) through as a false success. Only this exact
        # `--jq .object.sha` argv shape is understood; this is not a
        # generic REST router or jq evaluator (Out of Scope). Prints a
        # deterministic 40-hex fake SHA (bare string, no JSON quoting)
        # derived from the repo path + ref so different refs/repos produce
        # stable-but-distinct fake SHAs.
        match = _REF_SHA_ENDPOINT_RE.fullmatch(args[1])
        if match:
            ref_path, base_ref = match.groups()
            digest = hashlib.sha1(f"{ref_path}:{base_ref}".encode()).hexdigest()
            print(digest)
            return 0

    if args[:2] == ["api", "graphql"]:
        number = None
        for i, value in enumerate(args):
            if value == "-F" and args[i + 1].startswith("number="):
                number = args[i + 1].split("=", 1)[1]
        node_id = f"NODEID_{number}"
        payload = {
            "data": {"repository": {"issue": {"id": node_id, "databaseId": int(number or 0)}}}
        }
        print(json.dumps(payload))
        return 0

    print(f"unsupported fake gh args: {args}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
