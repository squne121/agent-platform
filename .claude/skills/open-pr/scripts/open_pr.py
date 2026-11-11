#!/usr/bin/env python3
"""Minimal interactive PR caller (I-4-4D11, #108).

Wires the D7 single reference evaluator (`validate_pr_body.py --evaluate-reference-policy`) to a
`gh pr create --draft` call for the trusted repository only. It reuses an existing OPEN PR for the
same repo/head/base and never edits one. Every GitHub read that fails is a stop, never "no PR".

Authority: the caller's own interactive `gh` session. This script holds no credential, registers no
controlled `pr_*` route and does not merge, update a branch, edit a body or comment.
Exit codes: 0 = dry_run / created / reused, 1 = stopped (nothing or an unknown outcome), 2 = usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
EVALUATOR = HERE / "validate_pr_body.py"
BASE = "main"
READ_TIMEOUT = 60
CREATE_TIMEOUT = 120
LIST_LIMIT = 100
RESULT_KEYS = frozenset(
    {
        "decision",
        "level",
        "reason_code",
        "repo",
        "issue_number",
        "pr_number",
        "pr_body_sha256",
        "effective_kind",
        "body_verdict",
        "body_reason",
    }
)
PR_JSON_FIELDS = "number,url,state,isDraft,headRefName,headRefOid,baseRefName,isCrossRepository,headRepositoryOwner,body"
_DECISION_LINE = "Reference-Decision:"
_DECISION_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9._-]+)/issues/[0-9]+#issuecomment-([0-9]+)"
)
_ORIGIN_URL = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?"
)
_PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([0-9]+)")


class Stop(Exception):
    """A fail-closed stop. `outcome` says whether a GitHub write may have happened."""

    def __init__(self, code: str, detail: str = "", **extra: object) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.extra = code, detail, extra


def _run(argv: list[str], timeout: int = READ_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, check=False, timeout=timeout, stdin=subprocess.DEVNULL
    )


def gh_json(code: str, *args: str) -> object:
    """A read-only `gh` call whose failure, timeout or non-JSON output is a stop."""
    try:
        proc = _run(["gh", *args])
    except (OSError, subprocess.SubprocessError) as exc:
        raise Stop(code, f"gh unavailable: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise Stop(code, f"gh exit {proc.returncode}: {proc.stderr.strip()[:200]}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise Stop(code, "gh output is not JSON") from exc


def git(*args: str) -> str:
    try:
        proc = _run(["git", *args])
    except (OSError, subprocess.SubprocessError) as exc:
        raise Stop("E_GIT_UNAVAILABLE", type(exc).__name__) from exc
    if proc.returncode != 0:
        raise Stop("E_GIT_READ_FAILED", f"git {args[0]} exit {proc.returncode}")
    return proc.stdout.strip()


def trusted_repo() -> str:
    guards = ROOT / "scripts" / "agent-guards"
    if str(guards) not in sys.path:
        sys.path.insert(0, str(guards))
    try:
        import repo_identity

        return repo_identity.load_trusted_repo_slug()
    except Exception as exc:
        raise Stop("E_REPO_IDENTITY_UNAVAILABLE", type(exc).__name__) from exc


def verify_git_state(repo: str, requested_branch: str | None) -> tuple[str, str]:
    """Read-only check that origin, branch, HEAD and the pushed ref agree. Returns (branch, head_sha)."""
    match = _ORIGIN_URL.fullmatch(git("config", "--get", "remote.origin.url"))
    if match is None or match.group(1) != repo:
        raise Stop("E_ORIGIN_MISMATCH", "origin is not the trusted repository")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch in {"HEAD", BASE} or (requested_branch is not None and requested_branch != branch):
        raise Stop("E_BRANCH_UNEXPECTED", f"current branch {branch!r}")
    if git("status", "--porcelain"):
        raise Stop("E_WORKTREE_DIRTY", "uncommitted changes")
    head = git("rev-parse", "HEAD")
    remote = git("ls-remote", "origin", f"refs/heads/{branch}").split()
    if len(remote) != 2 or remote[0] != head:
        raise Stop("E_HEAD_NOT_PUSHED", "origin branch is missing or differs from the local HEAD")
    base_remote = git("ls-remote", "origin", f"refs/heads/{BASE}").split()
    if len(base_remote) != 2:
        raise Stop("E_BASE_UNKNOWN", f"origin has no {BASE}")
    return branch, head


def fetch_issue(repo: str, number: int) -> tuple[str, str]:
    data = gh_json(
        "E_LINKED_ISSUE_STATE_UNKNOWN",
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "number,state,body",
    )
    if (
        not isinstance(data, dict)
        or data.get("number") != number
        or data.get("state") not in {"OPEN", "CLOSED"}
        or not isinstance(data.get("body"), str)
    ):
        raise Stop("E_LINKED_ISSUE_STATE_UNKNOWN", "unexpected issue payload")
    return data["state"], data["body"]


def fetch_decision_comment(repo: str, body: str) -> dict[str, object] | None:
    """Acquire the A1 comment fact only. Its validity is judged by the evaluator alone."""
    values = [
        ln[len(_DECISION_LINE) :].strip()
        for ln in re.split(r"\r\n|\n|\r", body)
        if ln.startswith(_DECISION_LINE)
    ]
    match = _DECISION_URL.fullmatch(values[0]) if len(values) == 1 else None
    if match is None or f"{match.group(1)}/{match.group(2)}" != repo:
        return None
    try:
        data = gh_json(
            "E_DECISION_COMMENT_UNREADABLE", "api", f"repos/{repo}/issues/comments/{match.group(3)}"
        )
    except Stop:
        return None
    if not isinstance(data, dict):
        return None
    comment = {
        key: data.get(src)
        for key, src in (
            ("url", "html_url"),
            ("id", "id"),
            ("issue_url", "issue_url"),
            ("author_association", "author_association"),
            ("body", "body"),
        )
    }
    if type(comment["id"]) is not int or not all(
        isinstance(comment[k], str) for k in ("url", "issue_url", "author_association", "body")
    ):
        return None
    return comment


def evaluate(body: str, issue: int, issue_body: str, facts: dict[str, object]) -> dict[str, object]:
    """Run the D7 evaluator CLI on exact bytes. Any transport/shape problem is `facts_invalid`."""
    raw = body.encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    unavailable = {k: None for k in RESULT_KEYS} | {
        "decision": "fail_closed",
        "reason_code": "facts_invalid",
        "pr_body_sha256": sha,
        "effective_kind": "none",
        "body_verdict": "block",
        "body_reason": "not_evaluated",
    }
    with tempfile.TemporaryDirectory(prefix="open-pr-") as tmp:
        files = {name: Path(tmp) / name for name in ("body.md", "facts.json", "issue.md")}
        files["body.md"].write_bytes(raw)
        files["facts.json"].write_text(json.dumps(facts), encoding="utf-8")
        files["issue.md"].write_text(issue_body, encoding="utf-8")
        try:
            proc = _run(
                [
                    sys.executable,
                    str(EVALUATOR),
                    "--evaluate-reference-policy",
                    "--body-file",
                    str(files["body.md"]),
                    "--linked-issue",
                    str(issue),
                    "--linked-issue-body-file",
                    str(files["issue.md"]),
                    "--reference-facts-file",
                    str(files["facts.json"]),
                ]
            )
            result = json.loads(proc.stdout) if proc.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return unavailable
    if (
        not isinstance(result, dict)
        or set(result) != RESULT_KEYS
        or result["pr_body_sha256"] != sha
    ):
        return unavailable
    return result


def reference_body(
    body: str, issue: int, issue_body: str, facts: dict[str, object]
) -> tuple[str, dict[str, object]]:
    """Append the one reference the evaluator requires (only when missing/repairable), then re-evaluate."""
    result = evaluate(body, issue, issue_body, facts)
    if (
        result["decision"] in {"closing_required", "nonclosing_required"}
        and result["body_reason"] != "closing_for_other"
        and (
            result["body_verdict"] == "repair"
            or result["body_reason"] == "reference_missing"
            or result["effective_kind"] == "notes-only"
        )
    ):
        kind = "Closes" if result["decision"] == "closing_required" else "Refs"
        body = body + ("\n" if body.endswith("\n") else "\n\n") + f"{kind} #{issue}\n"
        result = evaluate(body, issue, issue_body, facts)
    return body, result


def require_valid(result: dict[str, object]) -> None:
    """`exit 0` of the evaluator is not permission; only a decided lane with a `valid` body is."""
    if (
        result["decision"] not in {"closing_required", "nonclosing_required"}
        or result["body_verdict"] != "valid"
    ):
        raise Stop(
            "E_REFERENCE_POLICY_BLOCKED",
            f"decision={result['decision']} reason_code={result['reason_code']} "
            f"body_verdict={result['body_verdict']} body_reason={result['body_reason']}",
            reference=result,
        )


def find_open_pr(repo: str, branch: str, owner: str) -> dict[str, object] | None:
    """`None` = verified absent. A failed, malformed, foreign or ambiguous lookup is a stop."""
    rows = gh_json(
        "E_PR_LOOKUP_FAILED",
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "open",
        "--limit",
        str(LIST_LIMIT),
        "--json",
        PR_JSON_FIELDS,
    )
    if (
        not isinstance(rows, list)
        or len(rows) >= LIST_LIMIT
        or not all(isinstance(r, dict) for r in rows)
    ):
        raise Stop("E_PR_LOOKUP_AMBIGUOUS", "malformed or possibly truncated PR list")
    same_head = [r for r in rows if r.get("headRefName") == branch]
    if not same_head:
        return None
    owner_of = lambda r: (r.get("headRepositoryOwner") or {}).get("login")
    if len(same_head) > 1:
        raise Stop("E_PR_LOOKUP_AMBIGUOUS", f"{len(same_head)} open PRs share the head")
    pr = same_head[0]
    if pr.get("isCrossRepository") is not False or owner_of(pr) != owner:
        raise Stop("E_PR_FOREIGN_HEAD", "an open PR has this head name from another repository")
    if pr.get("baseRefName") != BASE:
        raise Stop(
            "E_PR_BASE_MISMATCH", f"an open PR for this head targets {pr.get('baseRefName')!r}"
        )
    return pr


def read_back(repo: str, number: int, branch: str, head: str, owner: str) -> dict[str, object]:
    pr = gh_json(
        "E_PR_READBACK_FAILED", "pr", "view", str(number), "--repo", repo, "--json", PR_JSON_FIELDS
    )
    if not isinstance(pr, dict):
        raise Stop("E_PR_READBACK_FAILED", "unexpected PR payload")
    problems = [
        name
        for name, ok in (
            ("number", pr.get("number") == number),
            ("state", pr.get("state") == "OPEN"),
            ("head", pr.get("headRefName") == branch),
            ("head_sha", pr.get("headRefOid") == head),
            ("base", pr.get("baseRefName") == BASE),
            (
                "same_repository",
                pr.get("isCrossRepository") is False
                and (pr.get("headRepositoryOwner") or {}).get("login") == owner,
            ),
            ("body", isinstance(pr.get("body"), str)),
            (
                "url",
                isinstance(pr.get("url"), str)
                and (_PR_URL.fullmatch(pr["url"]) or None) is not None
                and pr["url"].startswith(f"https://github.com/{repo}/pull/"),
            ),
        )
        if not ok
    ]
    if problems:
        raise Stop("E_PR_READBACK_MISMATCH", ",".join(problems))
    return pr


def guard_body_file(body: str) -> None:
    """github-ops Pre-edit guard: no empty/1-byte body, no HEREDOC escape residue."""
    if len(body.encode("utf-8")) <= 1 or re.search(r'\\(?:"|\$)', body):
        raise Stop("E_BODY_GUARD", "empty body or HEREDOC escape residue")


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.publish != "yes" and not args.dry_run:
        raise Stop("E_APPROVAL_MISSING", "publish: yes is required")
    title = args.pr_title.strip()
    if not title or "\n" in title:
        raise Stop("E_TITLE_INVALID", "a single non-empty line is required")
    try:
        body = Path(args.pr_body_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Stop("E_BODY_UNREADABLE", type(exc).__name__) from exc
    repo = trusted_repo()
    owner = repo.split("/")[0]
    branch, head = verify_git_state(repo, args.branch)
    state, issue_body = fetch_issue(repo, args.linked_issue)
    existing = find_open_pr(repo, branch, owner)

    def facts(text: str, number: int | None) -> dict[str, object]:
        return {
            "repo": repo,
            "issue_state": state,
            "pr_number": number,
            "decision_comment": fetch_decision_comment(repo, text),
        }

    if existing is not None:
        number = existing["number"]
        pr = read_back(repo, number, branch, head, owner)
        live = pr["body"]
        result = evaluate(live, args.linked_issue, issue_body, facts(live, number))
        require_valid(
            result
        )  # a PR for another Issue or with a broken reference is reported, never edited
        return {
            "status": "reused",
            "url": pr["url"],
            "pr_number": number,
            "head_sha": head,
            "body_matches_requested": live == body
            or live == reference_body(body, args.linked_issue, issue_body, facts(body, None))[0],
            "reference": result,
            "writes": 0,
        }

    final_body, result = reference_body(body, args.linked_issue, issue_body, facts(body, None))
    require_valid(result)
    guard_body_file(final_body)
    summary = {
        "repo": repo,
        "head": branch,
        "head_sha": head,
        "base": BASE,
        "draft": True,
        "reference": result,
        "final_body_sha256": hashlib.sha256(final_body.encode("utf-8")).hexdigest(),
    }
    if args.dry_run:
        return {"status": "dry_run", "writes": 0, **summary}

    with tempfile.TemporaryDirectory(prefix="open-pr-body-") as tmp:
        body_file = Path(tmp) / "pr-body.md"
        body_file.write_text(final_body, encoding="utf-8")
        on_disk = body_file.read_bytes()
        if hashlib.sha256(on_disk).hexdigest() != result["pr_body_sha256"]:
            raise Stop("E_BODY_HASH_MISMATCH", "validated body differs from the file to publish")
        try:
            proc = _run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repo,
                    "--head",
                    branch,
                    "--base",
                    BASE,
                    "--title",
                    title,
                    "--body-file",
                    str(body_file),
                    "--draft",
                ],
                timeout=CREATE_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise Stop(
                "E_CREATE_OUTCOME_UNKNOWN",
                f"{type(exc).__name__}: not retried",
                **_probe(repo, branch, owner),
            ) from exc
    urls = [m for m in _PR_URL.finditer(proc.stdout)]
    if proc.returncode != 0 or not urls or urls[-1].group(1) != repo:
        code = "E_CREATE_FAILED" if proc.returncode != 0 else "E_CREATE_OUTCOME_UNKNOWN"
        raise Stop(
            code,
            f"gh exit {proc.returncode}: {proc.stderr.strip()[:200]}; not retried",
            **_probe(repo, branch, owner),
        )
    number = int(urls[-1].group(2))
    pr = read_back(repo, number, branch, head, owner)
    live = pr["body"]
    after = evaluate(live, args.linked_issue, issue_body, facts(live, number))
    require_valid(after)
    if (
        pr.get("isDraft") is not True
        or hashlib.sha256(live.encode("utf-8")).hexdigest()
        != hashlib.sha256(final_body.encode("utf-8")).hexdigest()
    ):
        raise Stop(
            "E_PR_READBACK_MISMATCH", "draft flag or body differs after create", url=pr["url"]
        )
    return {
        "status": "created",
        "url": pr["url"],
        "pr_number": number,
        "writes": 1,
        **summary,
        "reference": after,
    }


def _probe(repo: str, branch: str, owner: str) -> dict[str, object]:
    """After an uncertain create, one read-only look. It reports; it never retries a create."""
    try:
        found = find_open_pr(repo, branch, owner)
    except Stop as exc:
        return {"probe": exc.code}
    return {
        "probe": "absent" if found is None else "found",
        "probe_url": None if found is None else found.get("url"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create (or reuse) a Draft PR for the trusted repository"
    )
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--linked-issue", required=True, type=int)
    parser.add_argument("--pr-body-file", required=True)
    parser.add_argument("--publish", default="no", choices=("yes", "no"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--branch", default=None)
    args = parser.parse_args(argv)
    if args.linked_issue <= 0:
        parser.error("--linked-issue must be positive")
    try:
        out = run(args)
        code = 0
    except Stop as stop:
        out = {"status": "stopped", "code": stop.code, "detail": stop.detail, **stop.extra}
        print(f"[ERROR] {stop.code}: {stop.detail}", file=sys.stderr)
        code = 1
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
