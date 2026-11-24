#!/usr/bin/env python3
"""Read-only producer of the Step 5 ``main_drift`` facts (I-4-4D17, Issue #120).

Two actions, both without any GitHub mutation and without changing the target checkout's
source, worktree, index, branch or refs:

``capture-evidence``  observe the live PR HEAD / base tip once, at evidence-collection time
                      (immediately before the independent VC run) and store it write-once.
``build``             combine that record with fresh live observations and the checkout's real Git
                      state into the exact ``live_mergeability`` mapping that the existing
                      ``build_step5_live_mergeability()`` accepts (and ``step5-terminal-gate``
                      reads through ``--live-mergeability-file``).

The routing, the main-drift policy and the merge-tree oracle are reused unchanged; this module
adds no judgement of its own.  Anything missing, inconsistent or not retrievable is refused with a
structured error and exit 1; a current value never stands in for a historical one.

Fact provenance (see Issue #120):
  current_base_sha                 live ref tip of the PR's baseRefName (== PR baseRefOid)
  evidence_base_sha                base tip recorded by ``capture-evidence`` (NOT the GO base)
  allowed_paths_snapshot_base_sha  ``base_sha_at_snapshot`` of an eligible trusted GO
  allowed_paths                    live linked Issue body ``## Allowed Paths`` (canonical grammar)
  latest_main_net_diff             ``git diff --name-only`` evidence_base..current_base in the checkout
  expected_old_sha                 PR HEAD recorded by ``capture-evidence`` (the old ref)
  observed_old_sha                 live PR head branch ref re-read in this cycle
  semantic_ambiguity               real ``git merge-tree`` oracle (current_base vs expected_old)

Not produced (remain manual): drift_rebind_attempts / max_drift_rebind_attempts, reviewer_verdict.
``git fetch`` is never run here; object availability is checked and a shortage is refused.
Merge-tree may write tree objects to the object database, so this is not "zero writes".
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[3]
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_COMMIT_SPEC = re.compile(r"^[0-9a-f]{40}\^\{commit\}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FIELDS = re.compile(r"^[A-Za-z,]+$")
_REF_PREFIX = "git/ref/heads/"
_ENDPOINT = re.compile(
    r"^repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(git/ref/heads/(?:[A-Za-z0-9_.~/-]|%[0-9A-F]{2})+|issues/[0-9]+/comments\?per_page=100)$"
)
_REF_BAD_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)
_RECORD_VERSION = 1
_COMMENTS_JQ = (
    ".[] | {id, html_url, created_at, updated_at, body, "
    "author: .user.login, author_id: .user.id, "
    "author_type: .user.type, author_association}"
)
_PR_FIELDS = (
    "number,state,isDraft,headRefOid,headRefName,baseRefName,baseRefOid,mergeable,mergeStateStatus"
)

Runner = Callable[..., subprocess.CompletedProcess]


class FactsError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --- read-only subprocess seam --------------------------------------------------------------


def _valid_branch(name: str) -> bool:
    """Subset of `git check-ref-format` rules (plus no leading '-'), so a branch cannot forge a path."""
    if not name or name == "@" or name.startswith(("-", "/")) or name.endswith(("/", ".")):
        return False
    if "//" in name or ".." in name or "@{" in name or _REF_BAD_CHARS.search(name):
        return False
    return all(c and not c.startswith(".") and not c.endswith(".lock") for c in name.split("/"))


def _encode_branch(name: str) -> str:
    if not _valid_branch(name):
        raise FactsError("invalid_branch_name", name[:80])
    try:
        return urllib.parse.quote(name, safe="/", errors="strict")
    except UnicodeEncodeError as exc:
        raise FactsError("invalid_branch_name", "not encodable as UTF-8") from exc


def _endpoint_allowed(tok: str) -> bool:
    if _ENDPOINT.fullmatch(tok) is None:
        return False
    if _REF_PREFIX in tok:
        enc = tok.split(_REF_PREFIX, 1)[1]
        try:
            dec = urllib.parse.unquote(enc, errors="strict")
        except UnicodeDecodeError:
            return False
        return _valid_branch(dec) and urllib.parse.quote(dec, safe="/") == enc
    return True


def _argv_allowed(argv: Sequence[str]) -> bool:
    """Exact read-only argv shapes only; any other token (attached flags included) is refused."""
    a = list(argv)
    if len(a) == 8 and a[0] == "gh" and a[1] in ("pr", "issue") and a[2] == "view":
        return (
            a[3].isdigit()
            and a[4] == "--repo"
            and _REPO.fullmatch(a[5]) is not None
            and a[6] == "--json"
            and _FIELDS.fullmatch(a[7]) is not None
        )
    if len(a) >= 3 and a[0] == "gh" and a[1] == "api":
        rest, i, endpoint = a[2:], 0, None
        while i < len(rest):
            tok = rest[i]
            if tok == "--paginate":
                i += 1
            elif (tok == "--jq" and i + 1 < len(rest)) or (
                tok == "--method" and rest[i + 1 : i + 2] == ["GET"]
            ):
                i += 2
            elif endpoint is None and _endpoint_allowed(tok):
                endpoint, i = tok, i + 1
            else:
                return False
        return endpoint is not None
    if a and a[0] == "git":
        sub = a[1:]
        if sub in (
            ["rev-parse", "--show-toplevel"],
            ["rev-parse", "HEAD"],
            ["remote", "get-url", "origin"],
        ):
            return True
        if len(sub) == 3 and sub[:2] == ["cat-file", "-e"]:
            return _COMMIT_SPEC.fullmatch(sub[2]) is not None
        if len(sub) == 4 and sub[:2] == ["merge-base", "--is-ancestor"]:
            return _SHA40.fullmatch(sub[2]) is not None and _SHA40.fullmatch(sub[3]) is not None
        if len(sub) == 4 and sub[:2] == ["merge-tree", "--write-tree"]:
            return _SHA40.fullmatch(sub[2]) is not None and _SHA40.fullmatch(sub[3]) is not None
        if len(sub) == 9 and sub[:7] == [
            "diff", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv",
            "--ignore-submodules=none", "-z",
        ]:  # fmt: skip
            return _SHA40.fullmatch(sub[7]) is not None and _SHA40.fullmatch(sub[8]) is not None
    return False


def _default_runner(argv: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV}
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        timeout=60,
    )


class _Guarded:
    """Wraps a runner so that every argv is checked against the read-only allowlist and logged."""

    def __init__(self, runner: Runner, cwd: Path | None) -> None:
        self._runner = runner
        self._cwd = cwd
        self.log: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], *, cwd: Path | None = None, input: str | None = None
    ) -> subprocess.CompletedProcess:
        if not _argv_allowed(argv):
            raise FactsError("read_only_violation", " ".join(argv))
        self.log.append(list(argv))
        try:
            return self._runner(argv, cwd=cwd if cwd is not None else self._cwd)
        except UnicodeDecodeError as exc:
            raise FactsError("command_output_decode_failed", " ".join(argv)) from exc
        except subprocess.TimeoutExpired as exc:
            raise FactsError("command_timeout", " ".join(argv)) from exc
        except OSError as exc:
            raise FactsError("command_unavailable", f"{argv[0]}: {exc}") from exc


def _ok(proc: subprocess.CompletedProcess, code: str, what: str) -> str:
    if proc.returncode != 0:
        raise FactsError(
            code, f"{what}: exit {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
        )
    return proc.stdout or ""


def _json(text: str, code: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FactsError(code, "not valid JSON") from exc


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA40.fullmatch(value):
        raise FactsError("invalid_sha", f"{name} is not a 40-hex lowercase sha")
    return value


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FactsError("module_unavailable", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- live observations ----------------------------------------------------------------------


def _ref_sha(run: _Guarded, repo: str, branch: str) -> str:
    out = _json(
        _ok(
            run(["gh", "api", f"repos/{repo}/git/ref/heads/{_encode_branch(branch)}"]),
            "github_fetch_failed",
            branch,
        ),
        "github_response_invalid",
    )
    obj = out.get("object") if isinstance(out, dict) else None
    return _sha(obj.get("sha") if isinstance(obj, dict) else None, f"ref:{branch}")


def _observe(run: _Guarded, repo: str, pr: int, issue: int, ens: Any) -> dict[str, Any]:
    data = _json(
        _ok(
            run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", _PR_FIELDS]),
            "github_fetch_failed",
            "pr view",
        ),
        "github_response_invalid",
    )
    if not isinstance(data, dict):
        raise FactsError("github_response_invalid", "pr view is not an object")
    for key in ("headRefName", "baseRefName", "mergeable", "mergeStateStatus", "state"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise FactsError("github_response_incomplete", f"pr.{key}")
    if not isinstance(data.get("isDraft"), bool):
        raise FactsError("github_response_incomplete", "pr.isDraft")
    if data.get("number") != pr:
        raise FactsError("pr_number_mismatch", f"{data.get('number')} != {pr}")
    if data["state"] != "OPEN":
        raise FactsError("pr_not_open", data["state"])
    head = _sha(data.get("headRefOid"), "pr.headRefOid")
    base_oid = _sha(data.get("baseRefOid"), "pr.baseRefOid")
    base_tip = _ref_sha(run, repo, data["baseRefName"])
    if base_tip != base_oid:
        raise FactsError("base_ref_oid_mismatch", f"ref tip {base_tip} != baseRefOid {base_oid}")
    head_ref = _ref_sha(run, repo, data["headRefName"])
    if head_ref != head:
        raise FactsError("head_ref_mismatch", f"head ref {head_ref} != headRefOid {head}")
    body_data = _json(
        _ok(
            run(["gh", "issue", "view", str(issue), "--repo", repo, "--json", "body,updatedAt"]),
            "github_fetch_failed",
            "issue view",
        ),
        "github_response_invalid",
    )
    body = body_data.get("body") if isinstance(body_data, dict) else None
    if not isinstance(body, str) or not body:
        raise FactsError("github_response_incomplete", "issue.body")
    return {
        "observed_at": _now(),
        "pr": {k: data[k] for k in ("number", "state", "isDraft", "headRefName", "baseRefName")},
        "head_sha": head,
        "base_sha": base_tip,
        "observed_old_sha": head_ref,
        "mergeable": data["mergeable"],
        "merge_state_status": data["mergeStateStatus"],
        "issue_body": body,
        "issue_body_sha256": ens.sha256_of(body),
        "issue_updated_at": body_data.get("updatedAt"),
    }


def _stable(first: dict[str, Any], second: dict[str, Any]) -> None:
    for key in ("head_sha", "base_sha", "observed_old_sha", "issue_body_sha256"):
        if first[key] != second[key]:
            raise FactsError("live_state_changed_during_collection", key)
    if first["pr"]["baseRefName"] != second["pr"]["baseRefName"]:
        raise FactsError("live_state_changed_during_collection", "baseRefName")


# --- checkout -------------------------------------------------------------------------------


def _verify_checkout(run: _Guarded, checkout: Path, repo: str) -> dict[str, str]:
    real = checkout.resolve()
    top = _ok(
        run(["git", "rev-parse", "--show-toplevel"], cwd=real), "checkout_invalid", "toplevel"
    ).strip()
    if Path(top).resolve() != real:
        raise FactsError("checkout_not_toplevel", f"{real} != {top}")
    url = _ok(
        run(["git", "remote", "get-url", "origin"], cwd=real), "checkout_invalid", "origin"
    ).strip()
    if not re.fullmatch(
        r"(https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
        + re.escape(repo)
        + r"(\.git)?/?",
        url,
    ):
        raise FactsError("checkout_repo_mismatch", f"origin does not point at {repo}")
    head = _ok(run(["git", "rev-parse", "HEAD"], cwd=real), "checkout_invalid", "HEAD").strip()
    return {"path": str(real), "head": head}


def _git_admin_dirs(checkout: Path) -> list[Path]:
    """Git dir and common Git dir of the checkout, read from the filesystem (no subprocess)."""
    root = checkout.resolve()
    dot = root / ".git"
    dirs: list[Path] = []
    try:
        if dot.is_file():
            first = dot.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
            if first and first[0].startswith("gitdir:"):
                dirs.append((root / first[0][len("gitdir:") :].strip()).resolve())
        elif dot.is_dir():
            dirs.append(dot.resolve())
        for gd in list(dirs):
            common = gd / "commondir"
            if common.is_file():
                text = common.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    dirs.append((gd / text).resolve())
    except OSError as exc:
        raise FactsError("checkout_invalid", f"git dir unreadable: {exc}") from exc
    return dirs


def _outside(path: Path, checkout: Path) -> None:
    real = path.resolve()
    for root in [checkout.resolve(), *_git_admin_dirs(checkout)]:
        if real == root or root in real.parents:
            raise FactsError("artifact_inside_checkout", str(real))


def _has_commit(run: _Guarded, checkout: Path, sha: str) -> bool:
    return run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=checkout).returncode == 0


# --- evidence record ------------------------------------------------------------------------


def _record_digest(rec: dict[str, Any]) -> str:
    body = {k: v for k, v in rec.items() if k != "record_sha256"}
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    )


def _record_name(repo: str, pr: int, head: str) -> str:
    return f"step5-evidence-{repo.replace('/', '_')}-pr{pr}-{head[:12]}.json"


def capture_evidence(
    run: _Guarded, ens: Any, repo: str, pr: int, issue: int, checkout: Path, out_dir: Path
) -> dict[str, Any]:
    _verify_checkout(run, checkout, repo)
    _outside(out_dir, checkout)
    first = _observe(run, repo, pr, issue, ens)
    second = _observe(run, repo, pr, issue, ens)
    _stable(first, second)
    rec: dict[str, Any] = {
        "record_version": _RECORD_VERSION,
        "repo": repo,
        "pr_number": pr,
        "issue_number": issue,
        "head_sha": first["head_sha"],
        "head_ref_name": first["pr"]["headRefName"],
        "base_ref_name": first["pr"]["baseRefName"],
        "base_sha": first["base_sha"],
        "captured_at": first["observed_at"],
    }
    rec["record_sha256"] = _record_digest(rec)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / _record_name(repo, pr, rec["head_sha"])
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise FactsError("evidence_record_exists", str(path)) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=True) + "\n")
    return {"record_path": str(path), "record": rec}


def _load_record(path: Path, repo: str, pr: int, issue: int) -> dict[str, Any]:
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise FactsError("evidence_record_missing_or_invalid", str(path)) from exc
    if not isinstance(rec, dict) or rec.get("record_version") != _RECORD_VERSION:
        raise FactsError("evidence_record_missing_or_invalid", "record_version")
    for key in ("repo", "head_ref_name", "base_ref_name", "captured_at", "record_sha256"):
        if not isinstance(rec.get(key), str) or not rec[key]:
            raise FactsError("evidence_record_missing_or_invalid", key)
    try:
        captured_at = datetime.fromisoformat(rec["captured_at"].replace("Z", "+00:00"))
        if captured_at.utcoffset() is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        raise FactsError("evidence_record_missing_or_invalid", "captured_at") from exc
    _sha(rec.get("head_sha"), "record.head_sha")
    _sha(rec.get("base_sha"), "record.base_sha")
    if rec.get("record_sha256") != _record_digest(rec):
        raise FactsError("evidence_record_digest_mismatch", str(path))
    if rec["repo"] != repo or rec.get("pr_number") != pr or rec.get("issue_number") != issue:
        raise FactsError("evidence_record_target_mismatch", f"{rec['repo']}#{rec.get('pr_number')}")
    return rec


# --- trusted GO -----------------------------------------------------------------------------


def _eligible_go(
    run: _Guarded, ens: Any, repo: str, issue: int, obs: dict[str, Any]
) -> dict[str, Any]:
    parser = ens._import_parser_module()
    proc = run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/issues/{issue}/comments?per_page=100",
            "--jq",
            _COMMENTS_JQ,
        ]
    )
    text = _ok(proc, "github_fetch_failed", "issue comments")
    comments: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        item = _json(line, "comments_fetch_incomplete")
        if not isinstance(item, dict):
            raise FactsError("comments_fetch_incomplete", "non-object comment")
        comments.append(item)
    results = parser.parse_contract_review_results(
        comments, expected_issue_url=f"https://github.com/{repo}/issues/{issue}"
    )
    adoptable = [
        r for r in results if r.get("status") != "go" or r.get("is_fingerprint_ready") is True
    ]
    latest = parser.find_latest_result(adoptable, trusted_only=True)
    go = parser.find_latest_go(results, trusted_only=True, fingerprint_ready_only=True)
    if go is None:
        raise FactsError("go_missing_or_untrusted", "no trusted fingerprint-ready GO")
    if latest is None or latest.get("comment_id") != go.get("comment_id"):
        raise FactsError("go_superseded", "a later trusted result is not this GO")
    if not ens.is_go_current(go, obs["issue_body_sha256"]):
        raise FactsError("go_body_mismatch", "GO is not current for the live Issue body")
    fp = go["inner"].get("expected_contract_fingerprint")
    if not isinstance(fp, dict):
        raise FactsError("go_fingerprint_missing", "")
    allowed = ens._canonicalize_allowed_paths_strict(_allowed(ens, obs["issue_body"]))
    try:
        recomputed = ens.compute_expected_contract_fingerprint(
            issue_number=issue,
            contract_source_id=fp.get("contract_source_id"),
            contract_body_sha256=obs["issue_body_sha256"],
            allowed_paths=allowed,
            base_ref=fp.get("base_ref"),
            base_sha_at_snapshot=fp.get("base_sha_at_snapshot"),
        )
    except (ValueError, TypeError) as exc:
        raise FactsError("go_fingerprint_invalid", str(exc)) from exc
    if recomputed != fp:
        raise FactsError("go_fingerprint_mismatch", "body / allowed_paths hash differs from live")
    if fp.get("base_ref") != obs["pr"]["baseRefName"]:
        raise FactsError(
            "go_base_ref_mismatch", f"{fp.get('base_ref')} != {obs['pr']['baseRefName']}"
        )
    return {
        "comment_id": go.get("comment_id"),
        "html_url": go.get("html_url"),
        "created_at": go.get("created_at"),
        "fingerprint": dict(fp),
        "base_ref": fp["base_ref"],
        "base_sha_at_snapshot": _sha(fp.get("base_sha_at_snapshot"), "go.base_sha_at_snapshot"),
    }


def _allowed(ens: Any, body: str) -> list[str]:
    raw = ens.extract_allowed_paths_from_body(body)
    if not raw:
        raise FactsError("allowed_paths_missing_or_empty", "")
    try:
        ens._canonicalize_allowed_paths_strict(raw)  # must be acceptable to the review gate
    except ValueError as exc:
        raise FactsError("allowed_paths_invalid", str(exc)) from exc
    # The fact keeps the Issue's own entries: the router matches "exact file" or "trailing-slash
    # prefix", which the gate's canonical glob form ("dir/**") would not satisfy.
    return list(raw)


# --- build ----------------------------------------------------------------------------------


def build(
    run: _Guarded,
    ens: Any,
    repo: str,
    pr: int,
    issue: int,
    checkout: Path,
    evidence_file: Path,
    output: Path,
) -> dict[str, Any]:
    co = _verify_checkout(run, checkout, repo)
    _outside(output, checkout)
    if output.resolve() == evidence_file.resolve() or (
        output.exists() and evidence_file.exists() and os.path.samefile(output, evidence_file)
    ):
        raise FactsError("output_aliases_evidence", str(output))
    rec = _load_record(evidence_file, repo, pr, issue)
    first = _observe(run, repo, pr, issue, ens)
    if first["pr"]["baseRefName"] != rec["base_ref_name"]:
        raise FactsError(
            "base_ref_name_changed", f"{rec['base_ref_name']} -> {first['pr']['baseRefName']}"
        )
    if first["pr"]["headRefName"] != rec["head_ref_name"]:
        raise FactsError(
            "head_ref_name_changed", f"{rec['head_ref_name']} -> {first['pr']['headRefName']}"
        )
    allowed = _allowed(ens, first["issue_body"])
    go = _eligible_go(run, ens, repo, issue, first)

    evidence_base, current_base = rec["base_sha"], first["base_sha"]
    if evidence_base == current_base and go["base_sha_at_snapshot"] != current_base:
        raise FactsError("stale_go_snapshot_base", "GO snapshot base differs from current base")
    expected_old, observed_old = rec["head_sha"], first["observed_old_sha"]
    missing = [
        s
        for s in dict.fromkeys((evidence_base, current_base, expected_old, observed_old))
        if not _has_commit(run, checkout, s)
    ]
    if missing:
        raise FactsError("git_object_missing", ",".join(missing))
    if evidence_base != current_base:
        anc = run(["git", "merge-base", "--is-ancestor", evidence_base, current_base], cwd=checkout)
        if anc.returncode == 1:
            raise FactsError("evidence_base_not_ancestor", f"{evidence_base}..{current_base}")
        if anc.returncode != 0:
            raise FactsError("git_error", f"merge-base: exit {anc.returncode}")
        diff = _ok(
            run(
                [
                    "git",
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--ignore-submodules=none",
                    "-z",
                    evidence_base,
                    current_base,
                ],
                cwd=checkout,
            ),
            "git_error",
            "diff",
        )
        net_diff = [p for p in diff.split("\0") if p]
    else:
        net_diff = []

    oracle = _load(
        _ROOT / "scripts" / "agent-ops" / "pr_head_replay_publish_exec.py", "_d17_oracle"
    )
    semantic = bool(
        oracle.compute_semantic_ambiguity(current_base, expected_old, cwd=checkout, runner=run)
    )

    second = _observe(run, repo, pr, issue, ens)
    _stable(first, second)
    final_go = _eligible_go(run, ens, repo, issue, second)
    if final_go != go:
        raise FactsError("go_changed_during_collection", "eligible GO changed between samples")

    route = _load(_HERE / "route_loop_verdict_v2.py", "_d17_route_loop_verdict_v2")
    facts = {
        "current_base_sha": current_base,
        "evidence_base_sha": evidence_base,
        "allowed_paths_snapshot_base_sha": go["base_sha_at_snapshot"],
        "allowed_paths": allowed,
        "latest_main_net_diff": net_diff,
        "expected_old_sha": expected_old,
        "observed_old_sha": observed_old,
        "semantic_ambiguity": semantic,
    }
    try:
        live = route.build_step5_live_mergeability(
            {
                "head_sha": second["head_sha"],
                "mergeable": second["mergeable"],
                "merge_state_status": second["merge_state_status"],
            },
            facts,
        )
    except ValueError as exc:
        raise FactsError("consumer_rejected_facts", str(exc)) from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(output.parent), prefix=".live_mergeability-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(live, sort_keys=True, ensure_ascii=True) + "\n")
    os.replace(tmp, output)
    return {
        "output": str(output),
        "live_mergeability": live,
        "provenance": {
            "checkout": co,
            "evidence_record": {
                "path": str(evidence_file),
                "digest": rec["record_sha256"],
                "captured_at": rec["captured_at"],
            },
            "go": go,
            "pr": {
                **second["pr"],
                "mergeable": second["mergeable"],
                "mergeStateStatus": second["merge_state_status"],
            },
            "samples": {"first": first["observed_at"], "second": second["observed_at"]},
            "issue": {
                "body_sha256": first["issue_body_sha256"],
                "updated_at": first["issue_updated_at"],
            },
            "semantic_ambiguity_oracle": {
                "candidate": expected_old,
                "base": current_base,
                "result": semantic,
            },
            "fetch_performed": False,
            "manual_inputs_remaining": [
                "drift_rebind_attempts",
                "max_drift_rebind_attempts",
                "reviewer_verdict",
            ],
            "commands": run.log,
        },
    }


# --- CLI ------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Read-only Step 5 main_drift facts producer (Issue #120)."
    )
    sub = p.add_subparsers(dest="action", required=True)
    for name in ("capture-evidence", "build"):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True)
        s.add_argument("--pr-number", type=int, required=True)
        s.add_argument("--issue-number", type=int, required=True)
        s.add_argument("--checkout", required=True)
    sub.choices["capture-evidence"].add_argument("--evidence-dir", required=True)
    sub.choices["build"].add_argument("--evidence-file", required=True)
    sub.choices["build"].add_argument("--output", required=True)
    return p


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    args = _parser().parse_args(argv)
    checkout = Path(args.checkout)
    run = _Guarded(runner or _default_runner, None)
    try:
        ens = _load(_HERE / "ensure_contract_snapshot.py", "_d17_ensure_contract_snapshot")
        if args.action == "capture-evidence":
            result = capture_evidence(
                run,
                ens,
                args.repo,
                args.pr_number,
                args.issue_number,
                checkout,
                Path(args.evidence_dir),
            )
        else:
            result = build(
                run,
                ens,
                args.repo,
                args.pr_number,
                args.issue_number,
                checkout,
                Path(args.evidence_file),
                Path(args.output),
            )
    except FactsError as exc:
        sys.stdout.write(
            json.dumps({"status": "error", "code": exc.code, "detail": exc.detail}) + "\n"
        )
        return 1
    sys.stdout.write(
        json.dumps({"status": "ok", **result}, sort_keys=True, ensure_ascii=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
