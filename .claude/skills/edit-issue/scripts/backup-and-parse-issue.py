#!/usr/bin/env python3
"""
backup-and-parse-issue.py

Issue 本文をバックアップし、metadata を JSON ファイルに保存する。
バックアップはリポジトリルート配下の `tmp/` に保存する（システム /tmp/ は使わない）。

Usage:
    uv run python3 backup-and-parse-issue.py <issue_number>

Output (stdout):
    {"metadata_file": "tmp/issue_<N>_backup_<ts>.json"}

Files written:
    tmp/issue_<N>_backup_<ts>.md   — Issue body（全文）
    tmp/issue_<N>_backup_<ts>.json — metadata {"issue_number": N, "repo": "owner/repo",
                                      "backup_file": "tmp/...", "title": "..."}
                                      ※ body フィールドは含まない（argv/shell サイズ制限回避）
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# allowlist: issue_number は純粋な数字のみ許可
_ISSUE_NUMBER_RE = re.compile(r'^[0-9]+$')  # ASCII only: \d also matches non-ASCII digits


def validate_issue_number(value: str) -> int:
    if not _ISSUE_NUMBER_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"issue_number must match ^[0-9]+$, got: {value!r}"
        )
    return int(value)


def get_repo() -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, check=True
    )
    url = result.stdout.strip()
    # https://github.com/owner/repo.git  or  git@github.com:owner/repo.git
    url = re.sub(r'.*github\.com[:/]', '', url)
    url = re.sub(r'\.git$', '', url)
    return url


def require_trusted_repo(repo: str) -> None:
    """Fail closed (before any `gh` call) unless the origin remote is the trusted repo."""
    guards = Path(__file__).resolve().parents[4] / "scripts" / "agent-guards"
    sys.path.insert(0, str(guards))
    try:
        from repo_identity import is_trusted_repo

        trusted = is_trusted_repo(repo)
    except Exception as exc:  # unusable identity config/module is a rejection, never a skip
        print(f"[ERROR] repo identity unavailable: {exc}", file=sys.stderr)
        sys.exit(1)
    if not trusted:
        print(f"[ERROR] origin repo is not the trusted repository: {repo!r}", file=sys.stderr)
        sys.exit(1)


def get_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True
    )
    return Path(result.stdout.strip())


def fetch_title_and_body(issue_number: int, repo: str) -> tuple[str, str]:
    """Fetch both title and body in a single gh call to avoid extra round-trips."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number),
         "--repo", repo, "--json", "title,body"],
        capture_output=True, text=True, check=True
    )
    data = json.loads(result.stdout)
    return data["title"], data["body"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backup GitHub Issue body (and title) and return as JSON"
    )
    parser.add_argument(
        "issue_number",
        type=validate_issue_number,
        help="Issue number (digits only)"
    )
    args = parser.parse_args()

    issue_number: int = args.issue_number
    ts = int(time.time())
    repo_root = get_repo_root()
    backup_dir = repo_root / "tmp"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backup_dir / f"issue_{issue_number}_backup_{ts}.md"

    repo = get_repo()
    require_trusted_repo(repo)
    title, body = fetch_title_and_body(issue_number, repo)

    backup_file.write_text(body, encoding="utf-8")
    if not backup_file.stat().st_size:
        print("[ERROR] Backup file is empty", file=sys.stderr)
        sys.exit(1)

    # metadata ファイル（body は含まない — argv/shell サイズ制限回避）
    metadata_file = backup_dir / f"issue_{issue_number}_backup_{ts}.json"
    metadata = {
        "issue_number": issue_number,
        "repo": repo,
        "backup_file": str(backup_file),
        "title": title,
    }
    metadata_file.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    # stdout には metadata_file パスのみ出力
    print(json.dumps({"metadata_file": str(metadata_file)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
