"""Issue #2996: ensure_contract_snapshot envelope -> step4-adjudicate handoff.

Producer (`ensure_contract_snapshot.py`) and consumer
(`adjudicate_vc_result.py step4-adjudicate`) are both started as real CLI
subprocesses. The only fake is the GitHub boundary: a deterministic `gh` shim on
PATH that keeps Issue / comment state in one JSON file shared by the producer, its
child scripts and the consumer (stateful POST / PATCH / GET). The trusted go
comments used by the `ok` source cases are produced by the real producer CLI
(`--mode auto --post`), never hand-built. No real network and no Claude session is
used.

Before the fix the consumer cannot read the saved envelope
(`unsupported_schema:CONTRACT_SNAPSHOT_ENSURE_RESULT_V1`) and does not know
`--producer-exit-code`; those cases fail as assertion failures (exit code of the
consumer is asserted), not as collection errors.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = ROOT / ".claude" / "skills" / "impl-review-loop"
PRODUCER = SKILL_DIR / "scripts" / "ensure_contract_snapshot.py"
CONSUMER = SKILL_DIR / "scripts" / "adjudicate_vc_result.py"

REPO = "squne121/agent-platform"
ISSUE = 2996
PR_NUMBER = 3010
HEAD_A = "a" * 40
HEAD_B = "b" * 40
GENERATED_AT = "2026-10-10T00:00:00Z"
ENVELOPE_SCHEMA = "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1"

# Trivially passing contract: one VC, `test -f README.md`, run by the producer in a
# working directory that contains README.md.
ISSUE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: "step 4 snapshot handoff regression fixture"
change_kind: docs
dedupe_key: "fixture:step4-snapshot-handoff"
```

## Parent Issue

なし。

## Parent Goal Ref

- Goal: fixture
- Desired Destination: fixture

## Current Validated Scope

- fixture

## Remaining Parent Gaps

なし。

## Problem / Reproduction

fixture

## Impact

fixture

## Outcome

fixture の README が存在する。

## Runtime Verification Applicability

- decision: not_applicable
- reason: fixture のため静的検証のみで完結する

## In Scope

- fixture

## Out of Scope

- fixture

## Acceptance Criteria

- [ ] AC1: README.md が存在する。

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ test -f README.md
```

## Allowed Paths

- `README.md`

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合

## Required Skills

なし

## Required Design References

- `docs/dev/github-ops.md`

## Scope Delta（任意）

N/A

## Delivery Rule

`1 Issue = 1 PR`。
"""

# --- deterministic fake `gh` ---------------------------------------------------

FAKE_GH_SOURCE = r"""
import datetime
import json
import os
import re
import sys

STATE_PATH = os.environ["FAKE_GH_STATE"]
LOG_PATH = os.environ.get("FAKE_GH_LOG")
TRUSTED_USER = {"login": "squne121", "id": 63350259, "type": "User"}


def load():
    with open(STATE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def save(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, STATE_PATH)


def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.exit(code)


def option(args, name):
    if name in args:
        idx = args.index(name)
        if idx + 1 < len(args):
            return args[idx + 1]
    return None


def comment_view(state, comment):
    return {
        "id": comment["id"],
        "html_url": comment["html_url"],
        "issue_url": "https://api.github.com/repos/%s/issues/%s" % (state["repo"], state["issue_number"]),
        "created_at": comment["created_at"],
        "updated_at": comment["updated_at"],
        "body": comment["body"],
        "user": comment["user"],
        "author_association": comment["author_association"],
    }


def main():
    args = sys.argv[1:]
    if LOG_PATH:
        with open(LOG_PATH, "a", encoding="utf-8") as log:
            log.write(json.dumps(args) + "\n")
    state = load()
    repo = state["repo"]
    number = state["issue_number"]

    if args[:2] == ["issue", "view"]:
        if int(args[2]) != number:
            fail("issue not found")
        jq = option(args, "--jq")
        fields = (option(args, "--json") or "").split(",")
        if jq == ".body":
            sys.stdout.write(state["body"] + "\n")
            return
        if jq == ".state":
            sys.stdout.write("OPEN\n")
            return
        view = {}
        if "body" in fields:
            view["body"] = state["body"]
        if "updatedAt" in fields:
            view["updatedAt"] = state["updated_at"]
        if "title" in fields:
            view["title"] = "fixture"
        if "labels" in fields:
            view["labels"] = []
        if "state" in fields:
            view["state"] = "OPEN"
        sys.stdout.write(json.dumps(view))
        return

    if args and args[0] == "api":
        rest = args[1:]
        method = (option(rest, "--method") or "GET").upper()
        endpoint = next((a for a in rest if a.startswith("repos/") or a == "graphql"), None)
        if endpoint == "graphql":
            query = option(rest, "-f") or ""
            if "defaultBranchRef" in query:
                sys.stdout.write(json.dumps({"data": {"repository": {"defaultBranchRef": {
                    "name": state["base_ref"], "target": {"oid": state["base_sha"]}}}}}))
                return
            fail("graphql query not supported by fake gh")
        if endpoint is None:
            fail("unsupported api call")
        base = "repos/%s" % re.escape(repo)
        if re.fullmatch(base + r"/issues/%d/dependencies/blocked_by" % number, endpoint):
            sys.stdout.write("[]")
            return
        if re.fullmatch(base + r"/issues/%d/comments(\?.*)?" % number, endpoint):
            if method == "GET" and state.get("comments_list_fails"):
                fail("HTTP 500: injected comments listing failure", 1)
            if method == "POST":
                payload = json.loads(sys.stdin.read())
                state["clock"] += 1
                cid = state["next_comment_id"]
                state["next_comment_id"] += 1
                stamp = (datetime.datetime(2026, 10, 10, 0, 0, 0, tzinfo=datetime.timezone.utc)
                         + datetime.timedelta(seconds=state["clock"])).strftime("%Y-%m-%dT%H:%M:%SZ")
                comment = {
                    "id": cid,
                    "html_url": "https://github.com/%s/issues/%d#issuecomment-%d" % (repo, number, cid),
                    "created_at": stamp,
                    "updated_at": stamp,
                    "body": payload["body"],
                    "user": dict(TRUSTED_USER),
                    "author_association": "OWNER",
                }
                state["comments"].append(comment)
                save(state)
                if option(rest, "--jq") == ".html_url":
                    sys.stdout.write(comment["html_url"] + "\n")
                else:
                    sys.stdout.write(json.dumps(comment_view(state, comment)))
                return
            for comment in state["comments"]:
                sys.stdout.write(json.dumps({
                    "id": comment["id"],
                    "html_url": comment["html_url"],
                    "created_at": comment["created_at"],
                    "updated_at": comment["updated_at"],
                    "body": comment["body"],
                    "author": comment["user"]["login"],
                    "author_id": comment["user"]["id"],
                    "author_type": comment["user"]["type"],
                    "author_association": comment["author_association"],
                }) + "\n")
            return
        m = re.fullmatch(base + r"/issues/comments/(\d+)", endpoint)
        if m:
            if state.get("comment_get_fails"):
                fail("HTTP 500: injected comment readback failure", 1)
            cid = int(m.group(1))
            comment = next((c for c in state["comments"] if c["id"] == cid), None)
            if comment is None:
                fail("HTTP 404: Not Found", 1)
            if method == "PATCH":
                payload = json.loads(sys.stdin.read())
                state["clock"] += 1
                new_body = payload["body"]
                if state.get("comment_patch_strips_fingerprint"):
                    # Injected lossy PATCH: GitHub accepts the update but the stored comment lacks the
                    # source-bound fingerprint (a trusted go that is NOT fingerprint-ready). GET still
                    # works, so the producer's readback sees a body hash mismatch.
                    new_body = re.sub(r"(?m)^  expected_contract_fingerprint: .*\n", "", new_body)
                comment["body"] = new_body
                save(state)
                if "--silent" not in rest:
                    sys.stdout.write(json.dumps(comment_view(state, comment)))
                return
            sys.stdout.write(json.dumps(comment_view(state, comment)))
            return
        if re.fullmatch(base + r"/commits/.+", endpoint):
            sys.stdout.write(state["base_sha"] + "\n")
            return
        fail("unsupported endpoint %s" % endpoint)

    fail("unsupported gh invocation: %s" % args)


main()
"""


# --- helpers -------------------------------------------------------------------


def _sha256_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


BODY_SHA256 = _sha256_of(ISSUE_BODY)


class World:
    """One isolated GitHub + filesystem universe shared by producer and consumer."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        (root / "fake_gh.py").write_text(FAKE_GH_SOURCE, encoding="utf-8")
        shim = bin_dir / "gh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{root / "fake_gh.py"}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        self.state_path = root / "state.json"
        self.log_path = root / "gh.log"
        self.work_dir = root / "work"
        self.work_dir.mkdir(exist_ok=True)
        (self.work_dir / "README.md").write_text("fixture\n", encoding="utf-8")
        self.loop_state = root / "loop_state.json"
        self._counter = 0
        self.write_state(
            {
                "repo": REPO,
                "issue_number": ISSUE,
                "body": ISSUE_BODY,
                "updated_at": "2026-10-09T00:00:00Z",
                "comments": [],
                "next_comment_id": 6000000001,
                "clock": 0,
                "base_ref": "main",
                "base_sha": "d" * 40,
                "comment_get_fails": False,
                "comment_patch_strips_fingerprint": False,
                "comments_list_fails": False,
            }
        )
        self.env = dict(os.environ)
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self.env["FAKE_GH_STATE"] = str(self.state_path)
        self.env["FAKE_GH_LOG"] = str(self.log_path)

    # -- fake GitHub state -------------------------------------------------

    def read_state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def write_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def comment_list_calls(self) -> int:
        if not self.log_path.exists():
            return 0
        count = 0
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            argv = json.loads(line)
            if argv[:2] == ["api", "--paginate"] and "/comments?per_page=100" in " ".join(argv):
                count += 1
        return count

    def add_trusted_blocked_comment(self) -> int:
        """A trusted `status: blocked` result newer than every existing comment."""
        state = self.read_state()
        state["clock"] += 1
        cid = state["next_comment_id"]
        state["next_comment_id"] += 1
        stamp = f"2026-10-10T01:00:{state['clock']:02d}Z"
        body = (
            "## Contract Review Result\n\n```yaml\nCONTRACT_REVIEW_RESULT_V1:\n"
            "  status: blocked\n"
            f'  generated_at: "{stamp}"\n'
            "  generated_by: issue-contract-review\n"
            f"  issue_url: https://github.com/{REPO}/issues/{ISSUE}\n"
            f'  body_sha256: "{BODY_SHA256}"\n```\n'
        )
        state["comments"].append(
            {
                "id": cid,
                "html_url": f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-{cid}",
                "created_at": stamp,
                "updated_at": stamp,
                "body": body,
                "user": {"login": "squne121", "id": 63350259, "type": "User"},
                "author_association": "OWNER",
            }
        )
        self.write_state(state)
        return cid

    # -- producer ------------------------------------------------------------

    def run_producer(self, *args: str) -> tuple[int, dict[str, Any], Path]:
        """Run the real `ensure_contract_snapshot.py`; return (exit code, stdout envelope, artifact path)."""
        self._counter += 1
        artifact_dir = self.root / f"snap-{self._counter}"
        completed = subprocess.run(
            [
                sys.executable,
                str(PRODUCER),
                "--issue-number",
                str(ISSUE),
                "--repo",
                REPO,
                *args,
                "--artifact-dir",
                str(artifact_dir),
            ],
            cwd=self.work_dir,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        assert completed.stdout.strip(), f"producer produced no stdout: {completed.stderr}"
        envelope = json.loads(completed.stdout.strip().splitlines()[-1])
        artifact = artifact_dir / f"contract-snapshot-{ISSUE}.json"
        assert artifact.exists(), f"producer saved no artifact: {completed.stderr}"
        saved = json.loads(artifact.read_text(encoding="utf-8"))
        assert saved["status"] == envelope["status"]
        return completed.returncode, envelope, artifact

    # -- consumer ------------------------------------------------------------

    def write_json(self, name: str, value: Any) -> Path:
        path = self.root / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return path

    def run_consumer(
        self,
        snapshot: Path,
        *,
        producer_exit_code: str | None = None,
        repo: str | None = None,
        head: str = HEAD_A,
        expected_head: str | None = None,
        body_sha256: str = BODY_SHA256,
        command_hashes: list[str] | None = None,
        expected_issue_number: int | None = ISSUE,
        reuse_stored: bool = False,
        extra: tuple[str, ...] = (),
        verdict_hash: str | None = None,
    ) -> tuple[int, dict[str, Any], str]:
        """Run the real `adjudicate_vc_result.py step4-adjudicate`; return (exit code, JSON payload, stderr)."""
        hashes = command_hashes if command_hashes is not None else [COMMAND_HASH]
        tag = f"{head[:4]}-{self._counter}"
        self._counter += 1
        hashes_file = self.write_json(f"hashes-{tag}.json", hashes)
        argv = [
            sys.executable,
            str(CONSUMER),
            "step4-adjudicate",
            "--loop-state-file",
            str(self.loop_state),
            "--expected-head-sha",
            expected_head or head,
            "--expected-contract-body-sha256",
            body_sha256,
            "--expected-command-hashes-file",
            str(hashes_file),
        ]
        if reuse_stored:
            argv.append("--reuse-stored")
        else:
            verdict = self.write_json(
                f"verdict-{tag}.json", _test_verdict(head, body_sha256, verdict_hash or COMMAND_HASH)
            )
            diff = self.write_json(
                f"diff-{tag}.json", {"head_sha": head, "pr_number": PR_NUMBER, "changed_paths": ["README.md"]}
            )
            allowed = self.write_json(f"allowed-{tag}.json", ["README.md"])
            argv += [
                "--test-verdict-file",
                str(verdict),
                "--contract-snapshot-file",
                str(snapshot),
                "--diff-summary-file",
                str(diff),
                "--allowed-paths-file",
                str(allowed),
                "--expected-pr-number",
                str(PR_NUMBER),
            ]
        if expected_issue_number is not None:
            argv += ["--expected-issue-number", str(expected_issue_number)]
        if producer_exit_code is not None:
            argv += ["--producer-exit-code", producer_exit_code]
        if repo is not None:
            argv += ["--repo", repo]
        argv += list(extra)
        completed = subprocess.run(
            argv, cwd=self.root, env=self.env, capture_output=True, text=True, check=False, timeout=120
        )
        payload: dict[str, Any] = {}
        if completed.stdout.strip():
            try:
                payload = json.loads(completed.stdout.strip().splitlines()[-1])
            except json.JSONDecodeError:
                payload = {"unparsed_stdout": completed.stdout}
        return completed.returncode, payload, completed.stderr

    def stored_keys(self) -> set[str]:
        if not self.loop_state.exists():
            return set()
        return set(json.loads(self.loop_state.read_text(encoding="utf-8")).get("vc_adjudication", {}))


# command hash of `test -f README.md` as derived by baseline_vc_preflight (sha256 of the raw command)
COMMAND_HASH = _sha256_of("test -f README.md")


def _test_verdict(head: str, body_sha256: str, command_hash: str) -> dict[str, Any]:
    return {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "issue_number": ISSUE,
        "pr_number": PR_NUMBER,
        "head_sha": head,
        "reviewed_head_sha": head,
        "diff_head_sha": head,
        "contract_body_sha256": body_sha256,
        "result": "PASS",
        "generated_at": GENERATED_AT,
        "runtime_ac_results": [
            {
                "ac": "AC1",
                "command": "test -f README.md",
                "command_hash": command_hash,
                "exit_code": 0,
                "status": "pass",
                "fallback_detected": False,
                "artifact_present": "not_required",
                "human_review_required": False,
                "stop_condition_triggered": False,
                "notes": "",
            }
        ],
    }


def _copy_world(src: World, dst_root: Path) -> World:
    """A fresh world whose fake GitHub state is a copy of `src`'s current state."""
    dst = World(dst_root)
    dst.write_state(copy.deepcopy(src.read_state()))
    return dst


# --- module-scoped real producer artifacts ---------------------------------------


@pytest.fixture(scope="module")
def produced(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run the real producer CLI for every envelope shape the consumer must handle."""
    base = tmp_path_factory.mktemp("handoff-produced")

    # A: no comments yet. check-only -> human_judgment (exit 20, same code as dry_run_would_post).
    world_a = World(base / "a")
    rc_human, env_human, art_human = world_a.run_producer("--mode", "check-only")

    # (1) dry-run candidate: nothing is posted.
    rc_dry, env_dry, art_dry = world_a.run_producer("--mode", "dry-run", "--evidence-mode", "baseline")
    assert world_a.read_state()["comments"] == []

    # (3) real `--post`: the trusted go comment is generated by the producer CLI itself.
    rc_post, env_post, art_post = world_a.run_producer("--mode", "auto", "--post")

    # (2) existing_go: baseline reuse of that posted go (nested result is null).
    rc_existing, env_existing, art_existing = world_a.run_producer("--mode", "check-only")
    posted_state = copy.deepcopy(world_a.read_state())

    # B: outer failure whose nested result is still a go (injected GET readback failure after POST).
    world_b = World(base / "b")
    state_b = world_b.read_state()
    state_b["comment_get_fails"] = True
    world_b.write_state(state_b)
    rc_outer, env_outer, art_outer = world_b.run_producer("--mode", "auto", "--post")

    # C: a newer trusted blocked result -> blocked_needs_refinement (exit 10).
    world_c = _copy_world(world_a, base / "c")
    blocked_id = world_c.add_trusted_blocked_comment()
    rc_blocked, env_blocked, art_blocked = world_c.run_producer("--mode", "check-only")

    return {
        "base": base,
        "posted_state": posted_state,
        "blocked_state": copy.deepcopy(world_c.read_state()),
        "blocked_comment_id": blocked_id,
        "human": (rc_human, env_human, art_human),
        "dry": (rc_dry, env_dry, art_dry),
        "post": (rc_post, env_post, art_post),
        "existing": (rc_existing, env_existing, art_existing),
        "outer": (rc_outer, env_outer, art_outer),
        "blocked": (rc_blocked, env_blocked, art_blocked),
    }


def _variant_body(body: str, tag: str) -> str:
    return body.rstrip("\n") + f"\n\n<!-- fixture variant {tag} -->\n"


@pytest.fixture(scope="module")
def provisional(tmp_path_factory: pytest.TempPathFactory, produced: dict[str, Any]) -> dict[str, Any]:
    """AC2/AC4 (g): a trusted, fingerprint-UNREADY go that is newer than a trusted fingerprint-ready go.

    Everything is produced by the real producer CLI (`--post`); no authoritative GO comment is
    hand-built. Sequence: go A (fingerprint-ready, body0) exists -> the Issue body is edited and
    the producer is run with `--post` while the fake GitHub drops the fingerprint from the PATCH of
    the second phase (GET still works), which leaves a trusted but fingerprint-unready go B (body1)
    behind -> the body is restored to body0. The producer
    then adopts A as `existing_go` even though B is the newest trusted result.
    """
    base = tmp_path_factory.mktemp("handoff-provisional")
    w = World(base / "w")
    w.write_state(copy.deepcopy(produced["posted_state"]))
    state = w.read_state()
    go_a_id = max(c["id"] for c in state["comments"])
    state["body"] = _variant_body(ISSUE_BODY, "provisional")
    state["comment_patch_strips_fingerprint"] = True
    w.write_state(state)
    rc_fail, env_fail, _ = w.run_producer("--mode", "auto", "--post")
    assert (rc_fail, env_fail["status"]) == (60, "controlled_publisher_binding_failed")
    state = w.read_state()
    state["body"] = ISSUE_BODY
    state["comment_patch_strips_fingerprint"] = False
    w.write_state(state)
    provisional_id = max(c["id"] for c in state["comments"])
    assert provisional_id > go_a_id
    rc, envelope, artifact = w.run_producer("--mode", "check-only")
    saved = copy.deepcopy(w.read_state())
    return {
        "state": saved,
        "go_a_id": go_a_id,
        "provisional_id": provisional_id,
        "producer": (rc, envelope, artifact),
    }


@pytest.fixture
def provisional_world(tmp_path: Path, provisional: dict[str, Any]) -> World:
    w = World(tmp_path / "provisional-world")
    w.write_state(copy.deepcopy(provisional["state"]))
    return w


@pytest.fixture
def world(tmp_path: Path, produced: dict[str, Any]) -> World:
    """A fresh consumer-side world holding the producer-generated trusted go comment."""
    w = World(tmp_path / "world")
    w.write_state(copy.deepcopy(produced["posted_state"]))
    return w


def _artifact_copy(w: World, artifact: Path, name: str, mutate: Any = None) -> Path:
    value = json.loads(artifact.read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(value)
    return w.write_json(name, value)


# --- (a) the producer facts the rest of the file relies on ----------------------------


def test_real_producer_exit_codes_and_envelopes(produced: dict[str, Any]) -> None:
    rc_dry, env_dry, _ = produced["dry"]
    assert (rc_dry, env_dry["status"], env_dry["source"]) == (20, "dry_run_would_post", "materialized_go")
    assert env_dry["contract_snapshot_url"] is None
    assert env_dry["contract_review_once_result"]["status"] == "go"

    rc_post, env_post, _ = produced["post"]
    assert (rc_post, env_post["status"], env_post["source"]) == (0, "ok", "materialized_go")
    assert env_post["contract_snapshot_url"].endswith("#issuecomment-6000000001")

    rc_existing, env_existing, _ = produced["existing"]
    assert (rc_existing, env_existing["status"], env_existing["source"]) == (0, "ok", "existing_go")
    assert env_existing["contract_snapshot_url"] == env_post["contract_snapshot_url"]
    assert env_existing["contract_review_once_result"] is None

    rc_human, env_human, _ = produced["human"]
    assert (rc_human, env_human["status"]) == (20, "human_judgment")

    rc_outer, env_outer, _ = produced["outer"]
    assert (rc_outer, env_outer["status"]) == (60, "controlled_publisher_binding_failed")
    assert env_outer["contract_review_once_result"]["status"] == "go"

    rc_blocked, env_blocked, _ = produced["blocked"]
    assert (rc_blocked, env_blocked["status"]) == (10, "blocked_needs_refinement")


# --- allowed source combinations (1) (2) (3) -----------------------------------------


def _assert_opened(rc: int, payload: dict[str, Any], stderr: str, w: World) -> None:
    assert rc == 0, f"expected invoke, got rc={rc} payload={payload} stderr={stderr}"
    assert payload["invoke_pr_reviewer"] is True
    assert payload["reason_code"] is None
    assert payload["seq"] == 1
    assert payload["binding_key"] in w.stored_keys()
    assert "unsupported_schema" not in json.dumps(payload)


def test_dry_run_candidate_exit_20_reaches_reviewer_without_repo(world: World, produced: dict[str, Any]) -> None:
    rc_producer, _, artifact = produced["dry"]
    rc, payload, stderr = world.run_consumer(artifact, producer_exit_code=str(rc_producer))
    _assert_opened(rc, payload, stderr, world)
    # dry_run_would_post does not use --repo and never consults GitHub comments
    assert world.comment_list_calls() == 0


def test_existing_go_nested_null_resolved_through_trusted_comment(world: World, produced: dict[str, Any]) -> None:
    rc_producer, envelope, artifact = produced["existing"]
    assert envelope["contract_review_once_result"] is None
    before = world.comment_list_calls()
    rc, payload, stderr = world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
    _assert_opened(rc, payload, stderr, world)
    assert world.comment_list_calls() > before, "the trusted comment was not re-verified through the shared parser"


def test_materialized_go_post_resolved_through_trusted_comment(world: World, produced: dict[str, Any]) -> None:
    rc_producer, envelope, artifact = produced["post"]
    assert envelope["contract_review_once_result"] is not None  # present, but not the authority
    before = world.comment_list_calls()
    rc, payload, stderr = world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
    _assert_opened(rc, payload, stderr, world)
    assert world.comment_list_calls() > before


def test_ok_authority_is_the_comment_not_the_nested_result(world: World, produced: dict[str, Any]) -> None:
    """A nested result that is non-null (or tampered) never substitutes for the comment."""
    rc_producer, _, artifact = produced["post"]
    state = world.read_state()
    state["comments"] = []  # the comment the URL points at no longer exists
    world.write_state(state)
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert "contract_snapshot_no_trusted_result" in payload["adjudication"]["errors"]


# --- trusted comment selection --------------------------------------------------------


def test_newer_trusted_blocked_than_url_go_closes_gate(tmp_path: Path, produced: dict[str, Any]) -> None:
    w = World(tmp_path / "blocked")
    w.write_state(copy.deepcopy(produced["blocked_state"]))
    for key in ("post", "existing"):
        rc_producer, _, artifact = produced[key]
        rc, payload, _ = w.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
        assert rc == 1, key
        assert payload["invoke_pr_reviewer"] is False
        assert "contract_snapshot_latest_trusted_result_blocked" in payload["adjudication"]["errors"]


def test_url_pointing_to_a_different_comment_is_rejected(world: World, produced: dict[str, Any]) -> None:
    rc_producer, _, artifact = produced["post"]

    def retarget(value: dict[str, Any]) -> None:
        value["contract_snapshot_url"] = value["contract_snapshot_url"].replace("6000000001", "6000000099")

    snapshot = _artifact_copy(world, artifact, "retargeted.json", retarget)
    rc, payload, _ = world.run_consumer(snapshot, producer_exit_code=str(rc_producer), repo=REPO)
    assert rc == 1
    assert "contract_snapshot_comment_not_latest_fingerprint_ready_go" in payload["adjudication"]["errors"]


def test_comment_fetch_failure_is_fail_closed(world: World, produced: dict[str, Any]) -> None:
    rc_producer, _, artifact = produced["post"]
    state = world.read_state()
    state["comments_list_fails"] = True
    world.write_state(state)
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert "contract_snapshot_comments_fetch_failed:gh_other_error" in payload["adjudication"]["errors"]


def test_repo_argument_not_the_envelope_decides_the_repo_binding(world: World, produced: dict[str, Any]) -> None:
    rc_producer, _, artifact = produced["post"]
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo="other-owner/other-repo")
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert "envelope_repo_mismatch" in payload["adjudication"]["errors"]


# --- producer status / exit code fail-closed matrix -------------------------------------


@pytest.mark.parametrize("claimed_exit_code", ["0", "20", "60", "40"])
def test_outer_failure_with_inner_go_never_opens_gate(
    world: World, produced: dict[str, Any], claimed_exit_code: str
) -> None:
    _, envelope, artifact = produced["outer"]
    assert envelope["contract_review_once_result"]["status"] == "go"
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code=claimed_exit_code, repo=REPO)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert (
        "producer_status_not_handoff_eligible:controlled_publisher_binding_failed" in payload["adjudication"]["errors"]
    )
    assert world.stored_keys() == set()


def test_human_judgment_exit_20_is_always_rejected(world: World, produced: dict[str, Any]) -> None:
    rc_producer, _, artifact = produced["human"]
    assert rc_producer == 20
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code="20", repo=REPO)
    assert rc == 1
    assert "producer_status_not_handoff_eligible:human_judgment" in payload["adjudication"]["errors"]


def test_blocked_needs_refinement_is_always_rejected(world: World, produced: dict[str, Any]) -> None:
    rc_producer, _, artifact = produced["blocked"]
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
    assert rc == 1
    assert "producer_status_not_handoff_eligible:blocked_needs_refinement" in payload["adjudication"]["errors"]


@pytest.mark.parametrize("status", ["runtime_error", "stale_or_conflicting_snapshot", "surprise"])
def test_other_statuses_are_rejected(world: World, produced: dict[str, Any], status: str) -> None:
    _, _, artifact = produced["dry"]
    snapshot = _artifact_copy(world, artifact, "status.json", lambda v: v.update(status=status))
    for code in ("0", "20", "40", "50"):
        rc, payload, _ = world.run_consumer(snapshot, producer_exit_code=code)
        assert rc == 1
        assert f"producer_status_not_handoff_eligible:{status}" in payload["adjudication"]["errors"]


@pytest.mark.parametrize(
    ("which", "claimed", "expected_error"),
    [
        ("dry", "0", "producer_exit_code_mismatch:status=dry_run_would_post:exit_code=0"),
        ("post", "20", "producer_exit_code_mismatch:status=ok:exit_code=20"),
        ("existing", "10", "producer_exit_code_mismatch:status=ok:exit_code=10"),
        ("dry", None, "producer_exit_code_missing"),
        ("dry", "twenty", "producer_exit_code_not_integer"),
        ("dry", "20.0", "producer_exit_code_not_integer"),
        ("dry", "-1", "producer_exit_code_not_integer"),
        ("dry", "", "producer_exit_code_not_integer"),
        ("dry", " 20", "producer_exit_code_not_integer"),
    ],
)
def test_exit_code_missing_malformed_or_mismatched_is_rejected(
    world: World, produced: dict[str, Any], which: str, claimed: str | None, expected_error: str
) -> None:
    _, _, artifact = produced[which]
    rc, payload, stderr = world.run_consumer(artifact, producer_exit_code=claimed, repo=REPO)
    assert rc == 1, f"payload={payload} stderr={stderr}"
    assert payload["invoke_pr_reviewer"] is False
    assert expected_error in payload["adjudication"]["errors"]


# --- body digest / malformed envelope --------------------------------------------------


def test_body_digest_mismatch_is_rejected_for_every_source(world: World, produced: dict[str, Any]) -> None:
    other = "sha256:" + "9" * 64
    for key in ("dry", "post", "existing"):
        rc_producer, _, artifact = produced[key]
        rc, payload, _ = world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO, body_sha256=other)
        assert rc == 1, key
        assert payload["invoke_pr_reviewer"] is False
        assert "snapshot_body_sha256_mismatch" in payload["adjudication"]["errors"], key


@pytest.mark.parametrize(
    ("which", "mutate", "expected_error"),
    [
        ("dry", lambda v: v.update(contract_review_once_result=None), "envelope_nested_result_missing_or_wrong_schema"),
        (
            "dry",
            lambda v: v["contract_review_once_result"].update(status="blocked"),
            "envelope_nested_result_not_go:blocked",
        ),
        (
            "dry",
            lambda v: v["contract_review_once_result"].update(body_sha256="sha256:" + "8" * 64),
            "envelope_nested_body_sha256_binding_mismatch",
        ),
        (
            "dry",
            lambda v: v["contract_review_once_result"].pop("vc_preflight_classifications"),
            "envelope_nested_vc_preflight_classifications_missing",
        ),
        ("dry", lambda v: v.update(source="existing_go"), "envelope_source_not_allowed:dry_run_would_post:existing_go"),
        (
            "dry",
            lambda v: v.update(
                contract_snapshot_url="https://github.com/squne121/agent-platform/issues/2996#issuecomment-1"
            ),
            "envelope_dry_run_contract_snapshot_url_not_null",
        ),
        ("dry", lambda v: v.update(issue_number=2997), "envelope_issue_number_mismatch"),
        ("post", lambda v: v.update(source="human_judgment"), "envelope_source_not_allowed:ok:human_judgment"),
        ("post", lambda v: v.update(contract_snapshot_url=None), "contract_snapshot_url_missing_or_malformed"),
        ("post", lambda v: v.update(contract_snapshot_url="not-a-url"), "contract_snapshot_url_missing_or_malformed"),
        (
            "post",
            lambda v: v.update(
                contract_snapshot_url="https://github.com/squne121/agent-platform/issues/1#issuecomment-6000000001"
            ),
            "contract_snapshot_url_issue_mismatch",
        ),
        (
            "post",
            lambda v: v.update(
                contract_snapshot_url="https://github.com/other/repo/issues/2996#issuecomment-6000000001"
            ),
            "contract_snapshot_url_repo_mismatch",
        ),
        ("post", lambda v: v.update(repo="other/repo"), "envelope_repo_mismatch"),
        ("existing", lambda v: v.update(issue_number=1), "envelope_issue_number_mismatch"),
    ],
)
def test_malformed_or_inconsistent_envelope_never_opens_gate(
    world: World, produced: dict[str, Any], which: str, mutate: Any, expected_error: str
) -> None:
    rc_producer, _, artifact = produced[which]
    snapshot = _artifact_copy(world, artifact, "mutated.json", mutate)
    rc, payload, stderr = world.run_consumer(snapshot, producer_exit_code=str(rc_producer), repo=REPO)
    assert rc == 1, f"payload={payload} stderr={stderr}"
    assert payload["invoke_pr_reviewer"] is False
    assert expected_error in payload["adjudication"]["errors"]


def test_ok_source_requires_repo_and_expected_issue_number(world: World, produced: dict[str, Any]) -> None:
    for key in ("post", "existing"):
        rc_producer, _, artifact = produced[key]
        rc, payload, _ = world.run_consumer(artifact, producer_exit_code=str(rc_producer))
        assert rc == 1
        assert "repo_required_for_ok_snapshot" in payload["adjudication"]["errors"]
        rc, payload, _ = world.run_consumer(
            artifact, producer_exit_code=str(rc_producer), repo=REPO, expected_issue_number=None
        )
        assert rc == 1
        assert "expected_issue_number_required_for_ok_snapshot" in payload["adjudication"]["errors"]
        rc, payload, _ = world.run_consumer(
            artifact, producer_exit_code=str(rc_producer), repo=REPO, expected_issue_number=ISSUE + 1
        )
        assert rc == 1
        assert "envelope_issue_number_mismatch" in payload["adjudication"]["errors"]


def test_non_json_snapshot_file_is_an_input_error(world: World) -> None:
    broken = world.write_json("broken.json", "{not json")
    rc, payload, _ = world.run_consumer(broken, producer_exit_code="20")
    assert rc == 1
    assert any(e.startswith("input_json_error") for e in payload["adjudication"]["errors"])


# --- other snapshot shapes / argument combinations ----------------------------------------


def _legacy_snapshot(produced: dict[str, Any]) -> dict[str, Any]:
    _, env_dry, _ = produced["dry"]
    nested = env_dry["contract_review_once_result"]
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": nested["body_sha256"],
        "checks": {"vc_preflight": {"classifications": nested["vc_preflight_classifications"]}},
    }


def test_existing_canonical_snapshot_still_works_without_new_arguments(world: World, produced: dict[str, Any]) -> None:
    snapshot = world.write_json("legacy.json", _legacy_snapshot(produced))
    rc, payload, stderr = world.run_consumer(snapshot)
    _assert_opened(rc, payload, stderr, world)


def test_producer_exit_code_with_non_envelope_snapshot_is_rejected(world: World, produced: dict[str, Any]) -> None:
    snapshot = world.write_json("legacy.json", _legacy_snapshot(produced))
    rc, payload, _ = world.run_consumer(snapshot, producer_exit_code="20")
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert "producer_exit_code_requires_envelope_snapshot" in payload["adjudication"]["errors"]


def test_envelope_without_producer_exit_code_is_rejected(world: World, produced: dict[str, Any]) -> None:
    _, _, artifact = produced["dry"]
    rc, payload, _ = world.run_consumer(artifact)
    assert rc == 1
    assert "producer_exit_code_missing" in payload["adjudication"]["errors"]


def test_new_arguments_are_refused_with_reuse_stored_and_other_subcommands(
    world: World, produced: dict[str, Any]
) -> None:
    _, _, artifact = produced["dry"]
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code="20")
    _assert_opened(rc, payload, "", world)
    before = world.loop_state.read_text(encoding="utf-8")
    for extra in (("--producer-exit-code", "20"), ("--repo", REPO)):
        rc, payload, stderr = world.run_consumer(artifact, reuse_stored=True, extra=extra)
        assert rc == 2, f"rc={rc} stderr={stderr}"
        assert payload == {}
        assert "cannot be combined" in stderr
    assert world.loop_state.read_text(encoding="utf-8") == before  # nothing was written or invalidated
    completed = subprocess.run(
        [sys.executable, str(CONSUMER), "step4-gate", "--producer-exit-code", "20"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "only accepted by step4-adjudicate" in completed.stderr


def test_plain_reuse_stored_is_unchanged(world: World, produced: dict[str, Any]) -> None:
    _, _, artifact = produced["dry"]
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code="20")
    _assert_opened(rc, payload, "", world)
    rc, payload, stderr = world.run_consumer(artifact, reuse_stored=True)
    assert rc == 0, f"payload={payload} stderr={stderr}"
    assert payload["invoke_pr_reviewer"] is True
    assert payload["reused"] is True
    assert payload["seq"] == 2


# --- existing bindings are not loosened ---------------------------------------------------


def test_head_and_command_bindings_still_close_the_gate(world: World, produced: dict[str, Any]) -> None:
    rc_producer, _, artifact = produced["dry"]
    code = str(rc_producer)

    # the report / diff describe HEAD_A but the live PR head being bound is HEAD_B
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code=code, head=HEAD_A, expected_head=HEAD_B)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    # the persist self-check (evaluate_step4_vc_gate: head_mismatch) refuses to store it
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    assert world.stored_keys() == set()

    # declared command hashes that are not the baseline Verification Command
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code=code, command_hashes=["sha256:" + "7" * 64])
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False

    # a current-head report that describes a different command than the baseline
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code=code, verdict_hash="sha256:" + "6" * 64)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert world.stored_keys() == set()


# --- (e) stateful invalidation sequence -----------------------------------------------------


def test_failed_handoff_invalidates_only_the_same_binding(world: World, produced: dict[str, Any]) -> None:
    rc_producer, _, artifact = produced["dry"]
    code = str(rc_producer)

    # 1. PASS stored for binding A (HEAD_A) and for an unrelated binding B (HEAD_B)
    rc, payload_a, stderr = world.run_consumer(artifact, producer_exit_code=code, head=HEAD_A)
    assert rc == 0, f"payload={payload_a} stderr={stderr}"
    rc, payload_b, stderr = world.run_consumer(artifact, producer_exit_code=code, head=HEAD_B)
    assert rc == 0, f"payload={payload_b} stderr={stderr}"
    key_a, key_b = payload_a["binding_key"], payload_b["binding_key"]
    assert key_a != key_b
    assert {key_a, key_b} <= world.stored_keys()

    # 2. ordinary --reuse-stored still re-opens binding A while nothing was re-verified
    rc, payload, stderr = world.run_consumer(artifact, reuse_stored=True, head=HEAD_A)
    assert rc == 0, f"payload={payload} stderr={stderr}"

    # 3. a new canonical adjudication for binding A whose handoff fails (wrong producer exit code)
    rc, payload, _ = world.run_consumer(artifact, producer_exit_code="0", head=HEAD_A)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert "producer_exit_code_mismatch:status=dry_run_would_post:exit_code=0" in payload["adjudication"]["errors"]
    assert key_a not in world.stored_keys()
    assert key_b in world.stored_keys()

    # 4. the stale PASS of binding A can no longer be reused ...
    rc, payload, _ = world.run_consumer(artifact, reuse_stored=True, head=HEAD_A)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert payload["reason_code"] == "adjudication_missing_or_malformed"

    # 5. ... while the unrelated binding B is untouched
    rc, payload, stderr = world.run_consumer(artifact, reuse_stored=True, head=HEAD_B)
    assert rc == 0, f"payload={payload} stderr={stderr}"
    assert payload["invoke_pr_reviewer"] is True


def test_every_handoff_failure_class_invalidates_a_stored_pass(world: World, produced: dict[str, Any]) -> None:
    rc_producer, _, dry_artifact = produced["dry"]
    _, _, post_artifact = produced["post"]
    rc, payload, stderr = world.run_consumer(dry_artifact, producer_exit_code=str(rc_producer))
    assert rc == 0, f"payload={payload} stderr={stderr}"
    key = payload["binding_key"]
    assert key in world.stored_keys()

    # failure: malformed snapshot file for the SAME binding still goes through persist
    broken = world.write_json("broken.json", "{not json")
    rc, payload, _ = world.run_consumer(broken, producer_exit_code="20")
    assert rc == 1
    assert key not in world.stored_keys()

    # failure: a trusted-comment re-verification failure (ok envelope, comment gone)
    rc, payload, _ = world.run_consumer(dry_artifact, producer_exit_code=str(rc_producer))
    assert rc == 0 and key in world.stored_keys()
    state = world.read_state()
    state["comments"] = []
    world.write_state(state)
    rc, payload, _ = world.run_consumer(post_artifact, producer_exit_code="0", repo=REPO)
    assert rc == 1
    assert key not in world.stored_keys()


# --- (f) type-invalid envelopes: structured rejection + persist/invalidation path ------------


def _store_two_passes(world: World, artifact: Path, code: str) -> tuple[str, str]:
    rc, payload_a, stderr = world.run_consumer(artifact, producer_exit_code=code, head=HEAD_A)
    assert rc == 0, f"payload={payload_a} stderr={stderr}"
    rc, payload_b, stderr = world.run_consumer(artifact, producer_exit_code=code, head=HEAD_B)
    assert rc == 0, f"payload={payload_b} stderr={stderr}"
    key_a, key_b = payload_a["binding_key"], payload_b["binding_key"]
    assert key_a != key_b and {key_a, key_b} <= world.stored_keys()
    return key_a, key_b


_TYPE_INVALID_CASES = [
    # (id, producer artifact, mutate, producer exit code override, expected structured error)
    ("status_list", "dry", lambda v: v.update(status=[]), None, "envelope_status_not_string:list"),
    ("status_dict", "dry", lambda v: v.update(status={}), None, "envelope_status_not_string:dict"),
    ("status_int", "dry", lambda v: v.update(status=20), None, "envelope_status_not_string:int"),
    ("status_null", "dry", lambda v: v.update(status=None), None, "envelope_status_not_string:NoneType"),
    ("source_list_dry", "dry", lambda v: v.update(source=[]), None, "envelope_source_not_string:list"),
    ("source_dict_dry", "dry", lambda v: v.update(source={}), None, "envelope_source_not_string:dict"),
    ("source_list_ok", "post", lambda v: v.update(source=["materialized_go"]), "0", "envelope_source_not_string:list"),
    ("source_dict_ok", "existing", lambda v: v.update(source={"a": 1}), "0", "envelope_source_not_string:dict"),
    ("huge_exit_code", "dry", None, "9" * 5000, "producer_exit_code_not_integer"),
    ("long_exit_code", "dry", None, "0" * 40 + "20", "producer_exit_code_not_integer"),
    ("nested_result_list", "dry", lambda v: v.update(contract_review_once_result=[]), None,
     "envelope_nested_result_missing_or_wrong_schema"),
    ("nested_result_string", "dry", lambda v: v.update(contract_review_once_result="go"), None,
     "envelope_nested_result_missing_or_wrong_schema"),
    ("nested_status_list", "dry", lambda v: v["contract_review_once_result"].update(status=["go"]), None,
     "envelope_nested_result_not_go:['go']"),
    ("nested_body_sha_dict", "dry", lambda v: v["contract_review_once_result"].update(body_sha256={}), None,
     "envelope_nested_body_sha256_missing"),
    ("nested_classifications_dict", "dry",
     lambda v: v["contract_review_once_result"].update(vc_preflight_classifications={}), None,
     "envelope_nested_vc_preflight_classifications_missing"),
]


@pytest.mark.parametrize(
    ("which", "mutate", "code_override", "expected_error"),
    [pytest.param(w, m, c, e, id=i) for i, w, m, c, e in _TYPE_INVALID_CASES],
)
def test_type_invalid_envelope_is_a_structured_rejection_that_invalidates_only_its_binding(
    world: World,
    produced: dict[str, Any],
    which: str,
    mutate: Any,
    code_override: str | None,
    expected_error: str,
) -> None:
    rc_producer, _, artifact = produced["dry"]
    good_code = str(rc_producer)
    key_a, key_b = _store_two_passes(world, artifact, good_code)

    source_artifact = produced[which][2]
    bad = _artifact_copy(world, source_artifact, "type-invalid.json", mutate)
    code = code_override if code_override is not None else str(produced[which][0])
    # A new canonical adjudication for binding A with a type-invalid envelope.
    rc, payload, stderr = world.run_consumer(bad, producer_exit_code=code, repo=REPO, head=HEAD_A)
    assert "Traceback" not in stderr, stderr
    assert rc == 1, f"expected structured rejection, got rc={rc} payload={payload} stderr={stderr}"
    assert payload["invoke_pr_reviewer"] is False
    assert expected_error in payload["adjudication"]["errors"], payload["adjudication"]["errors"]
    # state mutation: the stale PASS of binding A was invalidated by the persist path, B is kept
    assert key_a not in world.stored_keys()
    assert key_b in world.stored_keys()

    # the stale PASS of binding A can no longer be reused ...
    rc, payload, _ = world.run_consumer(artifact, reuse_stored=True, head=HEAD_A)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    # ... the unrelated binding B still can ...
    rc, payload, stderr = world.run_consumer(artifact, reuse_stored=True, head=HEAD_B)
    assert rc == 0, f"payload={payload} stderr={stderr}"
    assert payload["invoke_pr_reviewer"] is True
    # ... and a well-formed envelope still re-opens binding A (backward compatible)
    rc, payload, stderr = world.run_consumer(artifact, producer_exit_code=good_code, head=HEAD_A)
    assert rc == 0, f"payload={payload} stderr={stderr}"
    assert key_a in world.stored_keys()


def test_non_object_envelope_values_are_structured_rejections(world: World, produced: dict[str, Any]) -> None:
    rc_producer, _, artifact = produced["dry"]
    key_a, _ = _store_two_passes(world, artifact, str(rc_producer))
    for name, value in (("list.json", "[]"), ("string.json", '"x"'), ("number.json", "7")):
        bad = world.write_json(name, value)
        rc, payload, stderr = world.run_consumer(bad, producer_exit_code="20", head=HEAD_A)
        assert "Traceback" not in stderr, stderr
        assert rc == 1, f"{name}: payload={payload} stderr={stderr}"
        assert payload["invoke_pr_reviewer"] is False
        assert key_a not in world.stored_keys()
        rc, _, _ = world.run_consumer(artifact, producer_exit_code="20", head=HEAD_A)
        assert rc == 0


_UNREADABLE_SNAPSHOT_CASES = [
    # (id, raw bytes, expected structured error prefix)
    ("non_utf8", b"\xff\xfe\x80 not utf-8 \xc3\x28", "input_read_error:"),
    ("deeply_nested", b"[" * 200000 + b"]" * 200000, "input_json_error:"),
    ("oversized_int_literal", b'{"status": ' + b"9" * 5000 + b"}", "input_json_error:"),
]


@pytest.mark.parametrize(
    ("raw", "expected_prefix"),
    [pytest.param(r, e, id=i) for i, r, e in _UNREADABLE_SNAPSHOT_CASES],
)
def test_unreadable_snapshot_file_is_a_structured_rejection_that_invalidates_only_its_binding(
    world: World, produced: dict[str, Any], raw: bytes, expected_prefix: str
) -> None:
    """Non-UTF-8 / over-nested / over-long-integer snapshot files must not escape the persist path."""
    rc_producer, _, artifact = produced["dry"]
    good_code = str(rc_producer)
    key_a, key_b = _store_two_passes(world, artifact, good_code)

    bad = world.root / "unreadable.json"
    bad.write_bytes(raw)
    rc, payload, stderr = world.run_consumer(bad, producer_exit_code=good_code, head=HEAD_A)
    assert "Traceback" not in stderr, stderr
    assert rc == 1, f"expected structured rejection, got rc={rc} payload={payload} stderr={stderr}"
    assert payload["invoke_pr_reviewer"] is False
    errors = payload["adjudication"]["errors"]
    assert any(e.startswith(expected_prefix) for e in errors), errors
    # state mutation: the stale PASS of binding A was invalidated by the persist path, B is kept
    assert key_a not in world.stored_keys()
    assert key_b in world.stored_keys()

    # the stale PASS of binding A can no longer be reused ...
    rc, payload, _ = world.run_consumer(artifact, reuse_stored=True, head=HEAD_A)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    # ... the unrelated binding B still can ...
    rc, payload, stderr = world.run_consumer(artifact, reuse_stored=True, head=HEAD_B)
    assert rc == 0, f"payload={payload} stderr={stderr}"
    assert payload["invoke_pr_reviewer"] is True
    # ... and a well-formed envelope still re-opens binding A (backward compatible)
    rc, payload, stderr = world.run_consumer(artifact, producer_exit_code=good_code, head=HEAD_A)
    assert rc == 0, f"payload={payload} stderr={stderr}"
    assert key_a in world.stored_keys()


# --- (g) provisional GO: producer `existing_go` is consumed with the same precedence --------


def test_producer_returns_existing_go_a_although_a_newer_provisional_go_exists(
    provisional: dict[str, Any],
) -> None:
    rc, envelope, _ = provisional["producer"]
    assert (rc, envelope["status"], envelope["source"]) == (0, "ok", "existing_go")
    assert envelope["contract_snapshot_url"].endswith(f"#issuecomment-{provisional['go_a_id']}")
    assert provisional["provisional_id"] > provisional["go_a_id"]
    provisional_comment = next(c for c in provisional["state"]["comments"] if c["id"] == provisional["provisional_id"])
    assert "status: go" in provisional_comment["body"]
    assert "expected_contract_fingerprint" not in provisional_comment["body"]


def test_consumer_accepts_the_producer_existing_go_despite_a_newer_provisional_go(
    provisional_world: World, provisional: dict[str, Any]
) -> None:
    rc_producer, _, artifact = provisional["producer"]
    rc, payload, stderr = provisional_world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
    _assert_opened(rc, payload, stderr, provisional_world)


def test_provisional_go_does_not_open_the_gate_by_itself(
    provisional_world: World, provisional: dict[str, Any]
) -> None:
    """The URL of the provisional (fingerprint-unready) go is never accepted as the authority."""
    rc_producer, _, artifact = provisional["producer"]

    def to_provisional(value: dict[str, Any]) -> None:
        value["contract_snapshot_url"] = value["contract_snapshot_url"].replace(
            str(provisional["go_a_id"]), str(provisional["provisional_id"])
        )

    snapshot = _artifact_copy(provisional_world, artifact, "to-provisional.json", to_provisional)
    rc, payload, _ = provisional_world.run_consumer(snapshot, producer_exit_code=str(rc_producer), repo=REPO)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert "contract_snapshot_comment_not_latest_fingerprint_ready_go" in payload["adjudication"]["errors"]


def test_newer_trusted_blocked_after_provisional_go_closes_gate(
    provisional_world: World, provisional: dict[str, Any]
) -> None:
    rc_producer, _, artifact = provisional["producer"]
    provisional_world.add_trusted_blocked_comment()
    rc, payload, _ = provisional_world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert "contract_snapshot_latest_trusted_result_blocked" in payload["adjudication"]["errors"]


def test_newer_fingerprint_ready_go_than_the_url_go_closes_gate(
    provisional_world: World, provisional: dict[str, Any]
) -> None:
    """A newer trusted AND fingerprint-ready go (produced by the real `--post`) makes the old URL stale."""
    rc_producer, _, artifact = provisional["producer"]
    state = provisional_world.read_state()
    state["body"] = _variant_body(ISSUE_BODY, "newer-ready-go")
    provisional_world.write_state(state)
    rc_new, env_new, _ = provisional_world.run_producer("--mode", "auto", "--post")
    assert (rc_new, env_new["status"], env_new["source"]) == (0, "ok", "materialized_go")
    newer_id = int(env_new["contract_snapshot_url"].rsplit("-", 1)[1])
    assert newer_id > provisional["provisional_id"]
    state = provisional_world.read_state()
    state["body"] = ISSUE_BODY
    provisional_world.write_state(state)
    rc, payload, _ = provisional_world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert "contract_snapshot_comment_not_latest_fingerprint_ready_go" in payload["adjudication"]["errors"]


def test_provisional_scenario_keeps_every_other_binding(provisional_world: World, provisional: dict[str, Any]) -> None:
    rc_producer, _, artifact = provisional["producer"]
    code = str(rc_producer)
    rc, payload, _ = provisional_world.run_consumer(
        artifact, producer_exit_code=code, repo=REPO, body_sha256="sha256:" + "9" * 64
    )
    assert rc == 1 and "snapshot_body_sha256_mismatch" in payload["adjudication"]["errors"]
    rc, payload, _ = provisional_world.run_consumer(artifact, producer_exit_code=code, repo="other-owner/other-repo")
    assert rc == 1 and "envelope_repo_mismatch" in payload["adjudication"]["errors"]
    rc, payload, _ = provisional_world.run_consumer(
        artifact, producer_exit_code=code, repo=REPO, expected_issue_number=ISSUE + 1
    )
    assert rc == 1 and "envelope_issue_number_mismatch" in payload["adjudication"]["errors"]
    state = provisional_world.read_state()
    state["comments_list_fails"] = True
    provisional_world.write_state(state)
    rc, payload, _ = provisional_world.run_consumer(artifact, producer_exit_code=code, repo=REPO)
    assert rc == 1
    assert "contract_snapshot_comments_fetch_failed:gh_other_error" in payload["adjudication"]["errors"]
    assert payload["invoke_pr_reviewer"] is False


# --- CLI wiring facts ------------------------------------------------------------------------


def test_consumer_declares_the_new_optional_arguments() -> None:
    completed = subprocess.run([sys.executable, str(CONSUMER), "--help"], capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    assert "--producer-exit-code" in completed.stdout
    assert "--repo" in completed.stdout


# --- Issue #3012: a trusted fingerprint-unready go newer than a trusted blocked ----------------
#
# G = fingerprint-ready trusted go, B = trusted blocked, P = schema-valid trusted go that is NOT
# fingerprint-ready.  G, P and the stored envelope come from the real producer CLI (the `provisional`
# fixture builds `[G, P]`); only B's posting time is hand-set so that it sits strictly between G and P.


def _nr_parser() -> Any:
    import importlib.util

    path = ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "contract_review_result_parser.py"
    spec = importlib.util.spec_from_file_location("contract_review_result_parser_for_nonready_go_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _nr_add_comment_at(
    w: World,
    created_at: str,
    body: str,
    *,
    user: dict[str, Any] | None = None,
    association: str = "OWNER",
    comment_id: int | None = None,
) -> int:
    state = w.read_state()
    if comment_id is None:
        cid = state["next_comment_id"]
        state["next_comment_id"] += 1
    else:
        cid = comment_id
        assert all(c["id"] != cid for c in state["comments"])
    state["comments"].append(
        {
            "id": cid,
            "html_url": f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-{cid}",
            "created_at": created_at,
            "updated_at": created_at,
            "body": body,
            "user": user or {"login": "squne121", "id": 63350259, "type": "User"},
            "author_association": association,
        }
    )
    w.write_state(state)
    return cid


def _nr_blocked_body() -> str:
    return (
        "## Contract Review Result\n\n```yaml\nCONTRACT_REVIEW_RESULT_V1:\n"
        "  status: blocked\n"
        '  generated_at: "2026-10-10T00:30:00Z"\n'
        "  generated_by: issue-contract-review\n"
        f"  issue_url: https://github.com/{REPO}/issues/{ISSUE}\n"
        f'  body_sha256: "{BODY_SHA256}"\n```\n'
    )


def _nr_stamp(w: World, comment_id: int) -> str:
    return next(c["created_at"] for c in w.read_state()["comments"] if c["id"] == comment_id)


def _nr_add_blocked_between(w: World, earlier_id: int, later_id: int) -> int:
    """A trusted blocked comment whose created_at lies strictly between two existing comments."""
    import datetime

    lo, hi = _nr_stamp(w, earlier_id), _nr_stamp(w, later_id)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    lo_dt, hi_dt = datetime.datetime.strptime(lo, fmt), datetime.datetime.strptime(hi, fmt)
    mid = (lo_dt + (hi_dt - lo_dt) / 2).strftime(fmt)
    assert lo < mid < hi, (lo, mid, hi)
    return _nr_add_comment_at(w, mid, _nr_blocked_body())


def _nr_assert_shape(w: World, *, go_id: int | None, blocked_id: int | None, nonready_id: int | None) -> None:
    """After the real parser G/B/P are trusted + schema-valid, P is not fingerprint-ready, G is."""
    parser = _nr_parser()
    comments = [
        {
            "id": c["id"],
            "html_url": c["html_url"],
            "created_at": c["created_at"],
            "updated_at": c["updated_at"],
            "body": c["body"],
            "author": c["user"]["login"],
            "author_id": c["user"]["id"],
            "author_type": c["user"]["type"],
            "author_association": c["author_association"],
        }
        for c in w.read_state()["comments"]
    ]
    by_id = {
        r["comment_id"]: r
        for r in parser.parse_contract_review_results(
            comments, expected_issue_url=f"https://github.com/{REPO}/issues/{ISSUE}"
        )
    }
    for cid in (go_id, blocked_id, nonready_id):
        if cid is not None:
            assert by_id[cid]["is_trusted_author"] is True
    if go_id is not None:
        assert (by_id[go_id]["status"], by_id[go_id]["is_fingerprint_ready"]) == ("go", True)
    if blocked_id is not None:
        assert by_id[blocked_id]["status"] == "blocked"
    if nonready_id is not None:
        assert (by_id[nonready_id]["status"], by_id[nonready_id]["is_fingerprint_ready"]) == ("go", False)


def _nr_assert_gate_closed_by_blocked(rc: int, payload: dict[str, Any]) -> None:
    assert rc == 1, payload
    assert payload["invoke_pr_reviewer"] is False
    assert "contract_snapshot_latest_trusted_result_blocked" in payload["adjudication"]["errors"]


def test_nonready_go_after_blocked_closes_gate_and_invalidates_stored_pass(
    provisional_world: World, provisional: dict[str, Any]
) -> None:
    rc_producer, envelope, artifact = provisional["producer"]
    code = str(rc_producer)
    assert envelope["contract_snapshot_url"].endswith(f"#issuecomment-{provisional['go_a_id']}")

    # precondition: with [G, P] the stored envelope points at G and the gate opens (PASS is stored)
    rc, payload, stderr = provisional_world.run_consumer(artifact, producer_exit_code=code, repo=REPO)
    _assert_opened(rc, payload, stderr, provisional_world)
    key = payload["binding_key"]

    blocked_id = _nr_add_blocked_between(provisional_world, provisional["go_a_id"], provisional["provisional_id"])
    _nr_assert_shape(
        provisional_world,
        go_id=provisional["go_a_id"],
        blocked_id=blocked_id,
        nonready_id=provisional["provisional_id"],
    )
    ordered_ids = (provisional["go_a_id"], blocked_id, provisional["provisional_id"])
    stamps = [_nr_stamp(provisional_world, i) for i in ordered_ids]
    assert stamps == sorted(stamps) and len(set(stamps)) == 3  # genuinely [G, B, P]

    rc, payload, _ = provisional_world.run_consumer(artifact, producer_exit_code=code, repo=REPO)
    _nr_assert_gate_closed_by_blocked(rc, payload)
    # the stale PASS of the same binding was invalidated, and --reuse-stored cannot re-open the gate
    assert key not in provisional_world.stored_keys()
    rc, payload, _ = provisional_world.run_consumer(artifact, reuse_stored=True)
    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False


def test_nonready_go_after_blocked_input_order_and_same_created_at_do_not_change_result(
    provisional_world: World, provisional: dict[str, Any]
) -> None:
    rc_producer, _, artifact = provisional["producer"]
    code = str(rc_producer)
    # B shares P's created_at but has the SMALLER numeric comment id: by the raw tie-break P would be
    # latest, yet P is not an adoptable result so B must still close the gate
    blocked_id = _nr_add_comment_at(
        provisional_world,
        _nr_stamp(provisional_world, provisional["provisional_id"]),
        _nr_blocked_body(),
        comment_id=provisional["go_a_id"] - 1,
    )
    assert blocked_id < provisional["provisional_id"]
    _nr_assert_shape(
        provisional_world,
        go_id=provisional["go_a_id"],
        blocked_id=blocked_id,
        nonready_id=provisional["provisional_id"],
    )
    rc, payload, _ = provisional_world.run_consumer(artifact, producer_exit_code=code, repo=REPO)
    _nr_assert_gate_closed_by_blocked(rc, payload)

    # the same comments in reversed input order give the same verdict
    state = provisional_world.read_state()
    state["comments"].reverse()
    provisional_world.write_state(state)
    rc, payload, _ = provisional_world.run_consumer(artifact, producer_exit_code=code, repo=REPO)
    _nr_assert_gate_closed_by_blocked(rc, payload)


def test_nonready_go_after_blocked_without_ready_go_behaves_like_blocked_alone(
    provisional_world: World, provisional: dict[str, Any]
) -> None:
    rc_producer, _, artifact = provisional["producer"]
    state = provisional_world.read_state()
    state["comments"] = [c for c in state["comments"] if c["id"] != provisional["go_a_id"]]
    provisional_world.write_state(state)  # [P] only
    blocked_id = _nr_add_comment_at(provisional_world, "2026-10-10T00:00:02Z", _nr_blocked_body())
    _nr_assert_shape(provisional_world, go_id=None, blocked_id=blocked_id, nonready_id=provisional["provisional_id"])
    p_id = provisional["provisional_id"]
    assert _nr_stamp(provisional_world, blocked_id) < _nr_stamp(provisional_world, p_id)  # [B, P]

    rc, payload, _ = provisional_world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
    _nr_assert_gate_closed_by_blocked(rc, payload)


def test_nonready_go_after_blocked_later_ready_go_than_blocked_still_opens_gate(
    provisional_world: World, provisional: dict[str, Any]
) -> None:
    """[G, B, P, G2]: a ready go posted after the blocked one is authoritative again.

    G2 is G re-posted under a new comment id (fingerprint source id rewritten accordingly)."""
    rc_producer, _, artifact = provisional["producer"]
    _nr_add_blocked_between(provisional_world, provisional["go_a_id"], provisional["provisional_id"])
    state = provisional_world.read_state()
    go_a = next(c for c in state["comments"] if c["id"] == provisional["go_a_id"])
    g2_id = state["next_comment_id"]
    g2_body = go_a["body"].replace(str(provisional["go_a_id"]), str(g2_id))
    assert g2_body != go_a["body"]
    g2 = _nr_add_comment_at(provisional_world, "2026-10-10T00:00:30Z", g2_body)
    assert g2 == g2_id
    _nr_assert_shape(provisional_world, go_id=g2_id, blocked_id=None, nonready_id=provisional["provisional_id"])

    def to_g2(value: dict[str, Any]) -> None:
        value["contract_snapshot_url"] = value["contract_snapshot_url"].replace(
            str(provisional["go_a_id"]), str(g2_id)
        )

    snapshot = _artifact_copy(provisional_world, artifact, "to-g2.json", to_g2)
    rc, payload, stderr = provisional_world.run_consumer(snapshot, producer_exit_code=str(rc_producer), repo=REPO)
    _assert_opened(rc, payload, stderr, provisional_world)


def test_nonready_go_after_blocked_untrusted_blocked_and_plain_comments_do_not_close_gate(
    provisional_world: World, provisional: dict[str, Any]
) -> None:
    rc_producer, _, artifact = provisional["producer"]
    untrusted = {"login": "mallory", "id": 4242, "type": "User"}
    mid_blocked = _nr_add_blocked_between(provisional_world, provisional["go_a_id"], provisional["provisional_id"])
    # turn the in-between blocked into an UNTRUSTED one, and add a plain comment between as well
    state = provisional_world.read_state()
    for comment in state["comments"]:
        if comment["id"] == mid_blocked:
            comment["user"] = untrusted
            comment["author_association"] = "NONE"
    provisional_world.write_state(state)
    _nr_add_comment_at(provisional_world, _nr_stamp(provisional_world, mid_blocked), "ordinary discussion")

    rc, payload, stderr = provisional_world.run_consumer(artifact, producer_exit_code=str(rc_producer), repo=REPO)
    _assert_opened(rc, payload, stderr, provisional_world)

