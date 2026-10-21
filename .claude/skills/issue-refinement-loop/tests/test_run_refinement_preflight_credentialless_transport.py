"""Read-transport selection regression coverage for
`run_refinement_preflight.py` (Issue #2241 AC8 / #2257, updated by #2872).

History: Issue #2241 routed an isolated Claude-GPT session (HOME != the OS
account home) to the credentialless REST transport
(`scripts/agent-guards/github_credentialless_read.py`) because the launcher
of that era never forwarded GitHub auth. Issue #2299 changed the launcher
contract: GitHub auth (an ambient `GH_CONFIG_DIR`, `GH_TOKEN`/`GITHUB_TOKEN`/
`GH_HOST`/`GH_REPO`) is shared natively, so an isolated HOME says nothing
about GitHub auth. Issue #2872 therefore makes the production selector a
constant: native `gh`, always, for every read of one invocation.

This file keeps HOME isolation, GitHub auth availability and transport
selection as INDEPENDENT axes:

* the selector tests (isolated + token env / isolated + stored
  `GH_CONFIG_DIR` / isolated + authless / normal profile) pin that the
  transport never depends on either axis;
* the call-path tests (single transport threaded through every read,
  mid-read failure, multi-page comments + anchor) drive the real
  `run_preflight()` / `_fetch_*` functions with a recording transport;
* the remaining credentialless tests exercise the GET-only transport
  adapter through an EXPLICIT `transport=` argument -- it is no longer
  selected implicitly by any profile.

No test here reads, copies or prints a credential: token values are
sentinels and are asserted absent from every result.

Module loading follows the `test_operator_selected_scope_reframe.py`
convention (`importlib.util.spec_from_file_location` with a unique module
name, so this test never collides with another test file's own load of the
same source file in the same pytest session).
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[4]

_AGENT_GUARDS_DIR = REPO_ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))


def _load_preflight_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / "run_refinement_preflight.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load_preflight_module("run_refinement_preflight_2241_ac8_credentialless_transport")
gcr = preflight._credentialless_read

REPO = "squne121/agent-platform"
ISSUE_NUMBER = 2241

_GH_MARKER_SCRIPT = """#!/bin/sh
# Test-only marker executable (Issue #2241 AC8): if this is ever invoked,
# it proves the isolated-profile fetch path fell back to the `gh` CLI,
# which the AC8 fix must never do. It always exits non-zero so a caller
# that DID invoke it (a regression) sees a hard failure, not a silently
# swallowed one.
echo -n "gh_marker_invoked" >> "$GH_MARKER_INVOCATION_FILE"
exit 7
"""


class _FakeCredentiallessResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_gh_marker_executable(tmp_path: Path, monkeypatch) -> Path:
    """Puts a marker `gh` executable at the front of PATH and returns the
    path to the invocation marker file (absent unless `gh` actually runs)."""
    marker_bin_dir = tmp_path / "marker-bin"
    marker_bin_dir.mkdir()
    gh_marker_path = marker_bin_dir / "gh"
    gh_marker_path.write_text(_GH_MARKER_SCRIPT, encoding="utf-8")
    gh_marker_path.chmod(gh_marker_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    invocation_marker_file = tmp_path / "gh_marker_invoked.marker"
    monkeypatch.setenv("GH_MARKER_INVOCATION_FILE", str(invocation_marker_file))
    monkeypatch.setenv("PATH", f"{marker_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return invocation_marker_file


def _force_isolated_profile(tmp_path: Path, monkeypatch) -> None:
    """Forces `_is_isolated_claude_gpt_profile()` to True the same way
    `scripts/claude-gpt/launch.sh` does in production: point `HOME` at a
    fresh sandbox directory distinct from the real OS account home
    (`pwd.getpwuid(os.getuid()).pw_dir`), never at the real OS account home
    itself."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    assert str(isolated_home) != real_home
    monkeypatch.setenv("HOME", str(isolated_home))
    assert preflight._is_isolated_claude_gpt_profile() is True


def _patch_credentialless_opener(monkeypatch, responses_by_url: dict[str, tuple[bytes, dict[str, str]]]):
    def _fake_open(request, timeout=None):
        url = request.full_url
        assert url in responses_by_url, f"unexpected credentialless GET: {url!r}"
        body, headers = responses_by_url[url]
        return _FakeCredentiallessResponse(body, headers)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)


def test_explicit_credentialless_transport_never_invokes_gh_marker_executable(tmp_path, monkeypatch):
    """GIVEN the credentialless GET-only transport passed EXPLICITLY (it is no
    longer selected implicitly by any profile since Issue #2872) and a marker
    `gh` executable installed at the front of PATH
    WHEN `_fetch_issue()` and `_fetch_issue_comments()` are called
    THEN both succeed via that transport and the marker `gh` executable is
    never invoked (Issue #2241 AC8 contract surface, retained)."""
    invocation_marker_file = _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    issue_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"
    comments_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments?per_page=100"
    issue_body = (
        b'{"number": 2241, "title": "credentialless transport wiring", '
        b'"body": "issue body text", "labels": [{"name": "bug"}], '
        b'"html_url": "https://github.com/squne121/agent-platform/issues/2241", '
        b'"updated_at": "2026-08-17T00:00:00Z"}'
    )
    comments_body = b'[{"id": 1, "body": "comment one"}]'
    _patch_credentialless_opener(
        monkeypatch,
        {
            issue_url: (issue_body, {}),
            comments_url: (comments_body, {}),
        },
    )

    issue, issue_err = preflight._fetch_issue(REPO, ISSUE_NUMBER, transport=transport)
    comments, comments_err = preflight._fetch_issue_comments(REPO, ISSUE_NUMBER, transport=transport)

    assert issue_err == ""
    assert comments_err == ""
    assert issue is not None
    assert comments is not None
    assert not invocation_marker_file.exists(), (
        "gh marker executable was invoked -- the explicit credentialless transport fell back to the gh CLI"
    )


def test_credentialless_transport_fetch_issue_converts_credentialless_data_to_gh_cli_shape(tmp_path, monkeypatch):
    """GIVEN an explicit credentialless transport and a raw GitHub REST issue body
    WHEN `_fetch_issue()` routes through the credentialless transport
    THEN the returned dict matches the `gh issue view --json
    number,title,body,labels,url,updatedAt` field-name shape existing
    consumers of `_fetch_issue()` already depend on (Issue #2241 AC8)."""
    _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    issue_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"
    issue_body = (
        b'{"number": 2241, "title": "credentialless transport wiring", '
        b'"body": "issue body text", "labels": [{"name": "bug"}, {"name": "P1"}], '
        b'"html_url": "https://github.com/squne121/agent-platform/issues/2241", '
        b'"updated_at": "2026-08-17T00:00:00Z"}'
    )
    _patch_credentialless_opener(monkeypatch, {issue_url: (issue_body, {})})

    issue, err = preflight._fetch_issue(REPO, ISSUE_NUMBER, transport=transport)

    assert err == ""
    assert issue == {
        "number": 2241,
        "title": "credentialless transport wiring",
        "body": "issue body text",
        "labels": [{"name": "bug"}, {"name": "P1"}],
        "url": "https://github.com/squne121/agent-platform/issues/2241",
        "updatedAt": "2026-08-17T00:00:00Z",
    }


def test_isolated_profile_fetch_issue_comments_follows_pagination_and_returns_flat_list(tmp_path, monkeypatch):
    """GIVEN the isolated profile and a two-page paginated comments response
    WHEN `_fetch_issue_comments()` routes through the credentialless
    transport
    THEN pagination is followed to exhaustion and the result is a single
    flat list -- the same shape `_fetch_issue_comments()` has always
    returned to its callers (Issue #2241 AC8)."""
    _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    page_1_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments?per_page=100"
    page_2_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments?per_page=100&page=2"
    _patch_credentialless_opener(
        monkeypatch,
        {
            page_1_url: (b'[{"id": 1}]', {"Link": f'<{page_2_url}>; rel="next"'}),
            page_2_url: (b'[{"id": 2}]', {}),
        },
    )

    comments, err = preflight._fetch_issue_comments(REPO, ISSUE_NUMBER, transport=transport)

    assert err == ""
    assert comments == [{"id": 1}, {"id": 2}]


def test_non_isolated_profile_is_not_detected_as_isolated(monkeypatch):
    """GIVEN a normal human/dev/CI shell (HOME == the real OS account home)
    WHEN `_is_isolated_claude_gpt_profile()` is evaluated
    THEN it returns False, so this fix cannot regress the existing `gh` CLI
    path for every non-isolated caller (Issue #2241 AC8, `run_refinement_
    preflight.py`'s other consumers/other command_ids must be unaffected)."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)

    assert preflight._is_isolated_claude_gpt_profile() is False


# ---------------------------------------------------------------------------
# Issue #2257 AC1/AC2/AC3/AC6: `_fetch_single_comment` isolated-profile
# regression coverage. Issue #2197's anchor comment 5315264311 was
# misclassified as missing because `_fetch_single_comment` (unlike
# `_fetch_issue`/`_fetch_issue_comments` above) had no isolated-profile
# branch and always hit an unauthenticated `gh api` call.
# ---------------------------------------------------------------------------


def test_fetch_single_comment_credentialless_transport_never_invokes_gh_marker_executable(tmp_path, monkeypatch):
    """GIVEN an isolated Claude-GPT session profile and a marker `gh`
    executable installed at the front of PATH
    WHEN `_fetch_single_comment()` is called
    THEN it succeeds via the credentialless transport and the marker `gh`
    executable is never invoked (Issue #2257 AC1/AC6 -- this is the exact
    function Issue #2197's incident traced to)."""
    invocation_marker_file = _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    comment_id = 5315264311
    comment_url = f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}"
    comment_body = (
        b'{"id": 5315264311, "body": "anchor comment body", '
        b'"issue_url": "https://api.github.com/repos/squne121/agent-platform/issues/2197"}'
    )
    _patch_credentialless_opener(monkeypatch, {comment_url: (comment_body, {})})

    data, err = preflight._fetch_single_comment(REPO, comment_id, transport=transport)

    assert err == ""
    assert data is not None
    assert data["id"] == 5315264311
    assert not invocation_marker_file.exists(), (
        "gh marker executable was invoked -- isolated profile fell back to the gh CLI "
        "(this is exactly the Issue #2197 regression: a genuinely-existing anchor "
        "comment must never be resolved through an unauthenticated gh api call)"
    )


def test_fetch_single_comment_credentialless_transport_true_404_is_semantic_missing(tmp_path, monkeypatch):
    """GIVEN an isolated Claude-GPT session profile and a genuinely
    nonexistent comment id (a true HTTP 404)
    WHEN `_fetch_single_comment()` is called
    THEN the error is classified as `semantic_missing`, not
    `transport_failure` (Issue #2257 AC2)."""
    _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    comment_id = 99999999
    comment_url = f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}"

    def _fake_open(request, timeout=None):
        raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            comment_url, 404, "Not Found", {}, __import__("io").BytesIO(b"")
        )

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    data, err = preflight._fetch_single_comment(REPO, comment_id, transport=transport)

    assert data is None
    assert err.startswith("semantic_missing:")
    assert not preflight._is_transport_failure(err)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_fetch_single_comment_credentialless_transport_transport_failures_are_never_semantic_missing(
    tmp_path, monkeypatch, status
):
    """GIVEN an isolated Claude-GPT session profile and any non-404 HTTP
    failure resolving the anchor comment (401/403/429/5xx)
    WHEN `_fetch_single_comment()` is called
    THEN the error is classified as `transport_failure`, never
    `semantic_missing` -- a genuinely-existing anchor comment must never
    be reported as not found because of an auth/rate-limit/upstream
    failure (Issue #2257 AC3, exact regression class of the #2197
    incident: gh exit 4 in the isolated profile was being conflated with
    ANCHOR_COMMENT_NOT_FOUND)."""
    _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    comment_id = 5315264311
    comment_url = f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}"

    def _fake_open(request, timeout=None):
        raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            comment_url, status, "err", {}, __import__("io").BytesIO(b"")
        )

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    data, err = preflight._fetch_single_comment(REPO, comment_id, transport=transport)

    assert data is None
    assert err.startswith("transport_failure:")
    assert preflight._is_transport_failure(err)
    assert not err.startswith("semantic_missing:")


def test_fetch_single_comment_credentialless_transport_dns_failure_is_transport_failure(tmp_path, monkeypatch):
    """GIVEN an isolated Claude-GPT session profile and a DNS resolution
    failure resolving the anchor comment
    WHEN `_fetch_single_comment()` is called
    THEN the error is classified as `transport_failure` (Issue #2257 AC3/
    AC7: DNS fault-injection case)."""
    import socket
    import urllib.error as _urllib_error

    _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    def _fake_open(request, timeout=None):
        raise _urllib_error.URLError(socket.gaierror("Name or service not known"))

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    data, err = preflight._fetch_single_comment(REPO, 5315264311, transport=transport)

    assert data is None
    assert err.startswith("transport_failure:")


def test_fetch_single_comment_non_isolated_profile_uses_gh_cli(monkeypatch):
    """GIVEN a normal (non-isolated) profile
    WHEN `_fetch_single_comment()` is called
    THEN it still routes through `_run_gh` (unchanged `gh api` behavior --
    Issue #2257 AC8: the normal-profile gh path must not regress)."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)
    assert preflight._is_isolated_claude_gpt_profile() is False

    calls: list[list[str]] = []

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        calls.append(argv)
        return {"id": 1, "issue_url": "x"}, ""

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)

    data, err = preflight._fetch_single_comment(REPO, 1)

    assert err == ""
    assert data == {"id": 1, "issue_url": "x"}
    assert len(calls) == 1
    assert calls[0] == ["gh", "api", f"repos/{REPO}/issues/comments/1"]


def test_fetch_single_comment_non_isolated_profile_gh_exit_4_is_transport_failure(monkeypatch):
    """GIVEN a normal (non-isolated) profile where `gh api` fails with
    exit code 4 (gh CLI's documented "authentication required" exit code)
    WHEN `_fetch_single_comment()` is called
    THEN the error is classified as `transport_failure`, not
    `semantic_missing` (Issue #2257 AC3: the gh-CLI-path mirror of the
    isolated-profile 401 case above)."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        return None, "gh_exit_4: authentication required"

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)

    data, err = preflight._fetch_single_comment(REPO, 1)

    assert data is None
    assert err.startswith("transport_failure:")


def test_fetch_single_comment_non_isolated_profile_gh_404_is_semantic_missing(monkeypatch):
    """GIVEN a normal (non-isolated) profile where `gh api` fails with a
    genuine HTTP 404
    WHEN `_fetch_single_comment()` is called
    THEN the error is classified as `semantic_missing` (Issue #2257 AC2)."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)

    monkeypatch.setattr(
        preflight,
        "_run_gh",
        lambda argv, timeout=preflight.GH_API_TIMEOUT: (None, "gh_exit_1: HTTP 404: Not Found (https://api.github.com/...)"),
    )

    data, err = preflight._fetch_single_comment(REPO, 1)

    assert data is None
    assert err.startswith("semantic_missing:")


# ---------------------------------------------------------------------------
# Issue #2257 AC5/AC6 (retained as an explicit-transport, opt-in live test by
# Issue #2872): exact incident replay against the real GitHub REST API (no
# opener mocking) -- Issue #2197 anchor comment 5315264311, under a fresh
# isolated HOME, empty GH_CONFIG_DIR, token unset. Only DNS/egress-
# unavailable/upstream-5xx/rate-limit failures are an environment SKIP
# (never silently upgraded to PASS); any auth-dependency result is a hard
# `pytest.fail` -- that IS the #2197 regression reproducing.
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Issue #2872: transport selection is independent of HOME isolation and of
# GitHub auth availability. Native `gh` is selected for every read of one
# production invocation; no auth-state probing, no mid-read authority split.
# ---------------------------------------------------------------------------

TOKEN_SENTINEL = "SENTINEL_2872_token_value_must_never_appear_anywhere"
HOSTS_YML_SENTINEL = "SENTINEL_2872_stored_config_content_must_never_be_read"
ANCHOR_COMMENT_ID = 5942301496
ANCHOR_URL = f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{ANCHOR_COMMENT_ID}"
_ISSUE_PAYLOAD = {
    "number": ISSUE_NUMBER,
    "title": "t",
    "body": "## Outcome\n\nx\n",
    "labels": [],
    "url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
    "updatedAt": "2026-10-02T00:00:00Z",
}
_ANCHOR_PAYLOAD = {
    "id": ANCHOR_COMMENT_ID,
    "body": "anchor body",
    "html_url": ANCHOR_URL,
    "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}",
    "author_association": "OWNER",
    "updated_at": "2026-10-02T00:00:00Z",
}


def _forbid_credentialless_authority(monkeypatch) -> list[str]:
    """Fails the test if anything routes to the anonymous authority: neither
    the adapter class is constructed nor the raw opener is invoked. Returns a
    list that records any (unexpected) use."""
    used: list[str] = []

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            used.append("_CredentiallessPreflightTransport constructed")
            raise AssertionError("credentialless transport must not be selected")

    def _forbidden_open(request, timeout=None):
        used.append(f"credentialless GET {getattr(request, 'full_url', request)!r}")
        raise AssertionError("anonymous credentialless GET must not be issued")

    monkeypatch.setattr(preflight, "_CredentiallessPreflightTransport", _Forbidden)
    if gcr is not None:
        monkeypatch.setattr(gcr._opener, "open", _forbidden_open)
    return used


class _RecordingTransport:
    """`GitHubReadTransport`-shaped recorder standing in for the single read
    authority. Each method records its call so a test can prove every read of
    one invocation used THIS instance."""

    SOURCE_LABEL = "recording_native_gh"

    def __init__(self, *, comments=None, comments_error="", anchor=None, anchor_error="", issue_error=""):
        self.calls: list[tuple[str, tuple]] = []
        self._comments = [] if comments is None else comments
        self._comments_error = comments_error
        self._anchor = anchor
        self._anchor_error = anchor_error
        self._issue_error = issue_error

    def read_issue(self, repo, issue_number):
        self.calls.append(("read_issue", (repo, issue_number)))
        if self._issue_error:
            return None, self._issue_error
        return dict(_ISSUE_PAYLOAD), ""

    def list_issue_comments(self, repo, issue_number):
        self.calls.append(("list_issue_comments", (repo, issue_number)))
        if self._comments_error:
            return None, self._comments_error
        return list(self._comments), ""

    def read_issue_comment(self, repo, comment_id):
        self.calls.append(("read_issue_comment", (repo, comment_id)))
        if self._anchor_error:
            return None, self._anchor_error
        return (dict(self._anchor) if self._anchor is not None else None), ""


def _run_live_preflight(monkeypatch, tmp_path, transport, anchor_urls=None):
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(preflight, "_select_read_transport", lambda: transport)
    return preflight.run_preflight(
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        anchor_comment_urls=list(anchor_urls or []),
        fixture_path=None,
    )


def _assert_no_secret_in(*values) -> None:
    rendered = "".join(repr(v) for v in values)
    assert TOKEN_SENTINEL not in rendered
    assert HOSTS_YML_SENTINEL not in rendered


def test_isolated_with_token_env_selects_native_gh_transport(tmp_path, monkeypatch):
    """GIVEN an isolated HOME (launcher-equivalent) and GitHub auth shared via
    token env (`GH_TOKEN`/`GITHUB_TOKEN`)
    WHEN the production read transport is selected and an issue is read
    THEN it is the native `gh` transport (HOME difference alone never selects
    the anonymous credentialless transport), the read is a `gh issue view`,
    no anonymous GET is issued and the token value is never surfaced (Issue
    #2872 AC1/AC5(a)/AC6)."""
    _force_isolated_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("GH_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    used = _forbid_credentialless_authority(monkeypatch)
    argvs: list[list[str]] = []

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        argvs.append(list(argv))
        return dict(_ISSUE_PAYLOAD), ""

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)

    transport = preflight._select_read_transport()
    issue, err = preflight._fetch_issue(REPO, ISSUE_NUMBER)

    assert preflight._is_isolated_claude_gpt_profile() is True
    assert isinstance(transport, preflight._GhCliPreflightTransport)
    assert transport.SOURCE_LABEL == "gh_cli"
    assert err == ""
    assert issue == _ISSUE_PAYLOAD
    assert argvs == [
        ["gh", "issue", "view", str(ISSUE_NUMBER), "--repo", REPO, "--json", "number,title,body,labels,url,updatedAt"]
    ]
    assert used == []
    _assert_no_secret_in(issue, err, argvs)


def test_isolated_with_stored_gh_config_dir_selects_native_gh_transport(tmp_path, monkeypatch):
    """GIVEN an isolated HOME and a stored `gh auth login` configuration
    reached only via `GH_CONFIG_DIR` (NO token env)
    WHEN the production read transport is selected and an issue is read
    THEN it is the native `gh` transport -- a separate positive case from the
    token-env one -- and the selector/transport never open or read anything
    inside that config directory (auth resolution is left to `gh`; Issue #2872
    AC1/AC5(b)/AC8)."""
    _force_isolated_profile(tmp_path, monkeypatch)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config_dir = tmp_path / "stored-gh-config"
    config_dir.mkdir()
    (config_dir / "hosts.yml").write_text(f"github.com:\n  {HOSTS_YML_SENTINEL}\n", encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))
    used = _forbid_credentialless_authority(monkeypatch)
    opened: list[str] = []
    real_open = open

    def _spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    argvs: list[list[str]] = []

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        argvs.append(list(argv))
        return dict(_ISSUE_PAYLOAD), ""

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)
    monkeypatch.setattr("builtins.open", _spy_open)
    try:
        transport = preflight._select_read_transport()
        issue, err = preflight._fetch_issue(REPO, ISSUE_NUMBER)
    finally:
        monkeypatch.setattr("builtins.open", real_open)

    assert isinstance(transport, preflight._GhCliPreflightTransport)
    assert err == ""
    assert issue == _ISSUE_PAYLOAD
    assert [a[:3] for a in argvs] == [["gh", "issue", "view"]]
    assert used == []
    assert not any(str(config_dir) in path for path in opened), opened
    _assert_no_secret_in(issue, err, argvs)


def test_normal_profile_selects_native_gh_transport(monkeypatch):
    """GIVEN a normal profile (HOME == the real OS account home)
    WHEN the production read transport is selected
    THEN it is the same native `gh` transport used for isolated profiles --
    the selection is one constant, not a per-profile decision (Issue #2872
    AC5(d))."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)
    assert preflight._is_isolated_claude_gpt_profile() is False

    normal = preflight._select_read_transport()
    monkeypatch.setenv("HOME", "/nonexistent-isolated-home-2872")
    assert preflight._is_isolated_claude_gpt_profile() is True
    isolated = preflight._select_read_transport()

    assert isinstance(normal, preflight._GhCliPreflightTransport)
    assert normal.SOURCE_LABEL == "gh_cli"
    assert normal is isolated


def test_single_transport_threaded_through_all_read_call_sites(tmp_path, monkeypatch):
    """GIVEN one production `run_preflight()` invocation whose selector hands
    out ONE recording transport
    WHEN Issue and comments (anchor present) are read through the real
    `_fetch_*` call paths and -- in a second scenario -- the anchor
    single-comment fallback is read through the real anchor validation
    THEN every read lands on that very instance (call recording) and nothing
    is routed to the anonymous credentialless transport (Issue #2872 AC3,
    #2257 split-brain non-regression)."""
    used = _forbid_credentialless_authority(monkeypatch)

    # Scenario 1: the anchor is part of the complete comments traversal.
    transport_1 = _RecordingTransport(comments=[dict(_ANCHOR_PAYLOAD)])
    _run_live_preflight(monkeypatch, tmp_path, transport_1, anchor_urls=[ANCHOR_URL])
    methods_1 = [name for name, _ in transport_1.calls]
    assert methods_1[:2] == ["read_issue", "list_issue_comments"], methods_1
    assert "read_issue_comment" not in methods_1, methods_1

    # Scenario 2: the single-comment read (fresh-readback fallback of the
    # anchor validation, reached when no pre-fetched traversal is supplied)
    # must use the SAME explicitly-passed instance, never re-select.
    transport_2 = _RecordingTransport(anchor=dict(_ANCHOR_PAYLOAD))
    monkeypatch.setattr(preflight, "_select_read_transport", lambda: pytest.fail("re-selected"))
    valid, blockers = preflight._validate_anchor_comment_url(
        ANCHOR_URL, REPO, ISSUE_NUMBER, fixture_comments=None, transport=transport_2
    )
    assert (valid, blockers) == (True, [])
    assert [name for name, _ in transport_2.calls] == ["read_issue_comment"]
    monkeypatch.undo()

    # Scenario 3: NO selector patching, isolated HOME -- the production
    # selector itself must route every read of the invocation (Issue,
    # comments, anchor) through native `gh`, never the anonymous transport.
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)
    _force_isolated_profile(tmp_path, monkeypatch)
    used = _forbid_credentialless_authority(monkeypatch)
    argvs: list[list[str]] = []

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        argvs.append(list(argv))
        if argv[:3] == ["gh", "issue", "view"]:
            return dict(_ISSUE_PAYLOAD), ""
        return [[dict(_ANCHOR_PAYLOAD)]], ""

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)
    preflight.run_preflight(issue_number=ISSUE_NUMBER, repo=REPO, anchor_comment_urls=[ANCHOR_URL], fixture_path=None)
    assert [a[:2] for a in argvs] == [["gh", "issue"], ["gh", "api"]], argvs
    assert used == []


def test_isolated_authless_keeps_contract_without_secret_probing(tmp_path, monkeypatch):
    """GIVEN a genuinely authless isolated environment (isolated HOME, no
    token env, no `GH_CONFIG_DIR`) and a decoy stored `hosts.yml` under the
    isolated HOME, with a marker `gh` that answers exit 4 ("authentication
    required") and records every invocation
    WHEN `run_preflight()` reads the Issue
    THEN the result is the structured `environment_failure` /
    `gh_auth_required` (current contract), exactly ONE `gh` read was made, no
    `gh auth ...` probe ran, `hosts.yml` was never opened, and no anonymous
    GET was issued (Issue #2872 AC4/AC8)."""
    _force_isolated_profile(tmp_path, monkeypatch)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    decoy_dir = Path(os.environ["HOME"]) / ".config" / "gh"
    decoy_dir.mkdir(parents=True)
    (decoy_dir / "hosts.yml").write_text(f"github.com:\n  {HOSTS_YML_SENTINEL}\n", encoding="utf-8")

    bin_dir = tmp_path / "authless-bin"
    bin_dir.mkdir()
    log_file = tmp_path / "gh-argv.log"
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\necho "$@" >> "$GH_ARGV_LOG"\n'
        'echo "To get started with GitHub CLI, please run:  gh auth login" >&2\nexit 4\n',
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("GH_ARGV_LOG", str(log_file))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    used = _forbid_credentialless_authority(monkeypatch)
    opened: list[str] = []
    real_open = open

    def _spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)
    monkeypatch.setattr("builtins.open", _spy_open)
    try:
        result, exit_code = preflight.run_preflight(
            issue_number=ISSUE_NUMBER, repo=REPO, anchor_comment_urls=[ANCHOR_URL], fixture_path=None
        )
    finally:
        monkeypatch.setattr("builtins.open", real_open)

    assert exit_code == preflight.EXIT_ENVIRONMENT_FAILURE
    assert result["status"] == "environment_failure"
    assert result["reason_code"] == "gh_auth_required"
    assert result["source"] == "gh_cli"
    assert result["operation"] == "read_issue"
    gh_calls = log_file.read_text(encoding="utf-8").splitlines()
    assert len(gh_calls) == 1 and gh_calls[0].startswith("issue view"), gh_calls
    assert not any(call.split()[:1] == ["auth"] for call in gh_calls)
    assert not any("hosts.yml" in path for path in opened), opened
    assert used == []
    _assert_no_secret_in(result)


def test_mid_read_failure_does_not_switch_transport(tmp_path, monkeypatch):
    """GIVEN a read transport whose comments listing fails mid-invocation with
    an auth/rate-limit style error (after the Issue read succeeded)
    WHEN `run_preflight()` runs
    THEN the invocation reports a structured `environment_failure` sourced
    from THAT transport, performs no retry on another authority, and never
    constructs the credentialless transport or issues an anonymous GET (Issue
    #2872 AC3/AC5(e); #2257 split-brain non-regression)."""
    used = _forbid_credentialless_authority(monkeypatch)
    transport = _RecordingTransport(comments_error="gh_exit_1: HTTP 403: API rate limit exceeded")

    result, exit_code = _run_live_preflight(monkeypatch, tmp_path, transport, anchor_urls=[ANCHOR_URL])

    assert exit_code == preflight.EXIT_ENVIRONMENT_FAILURE
    assert result["status"] == "environment_failure"
    assert result["source"] == "recording_native_gh"
    assert result["operation"] == "list_issue_comments"
    assert [name for name, _ in transport.calls] == ["read_issue", "list_issue_comments"]
    assert used == []
    monkeypatch.undo()

    # Same failure through the PRODUCTION selector under an isolated HOME: the
    # failing authority is native `gh`, reported as such, with no anonymous
    # retry (no credentialless construction, no raw GET).
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)
    _force_isolated_profile(tmp_path, monkeypatch)
    used = _forbid_credentialless_authority(monkeypatch)
    argvs: list[list[str]] = []

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        argvs.append(list(argv))
        if argv[:3] == ["gh", "issue", "view"]:
            return dict(_ISSUE_PAYLOAD), ""
        return None, "gh_exit_1: HTTP 403: API rate limit exceeded"

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)
    result, exit_code = preflight.run_preflight(
        issue_number=ISSUE_NUMBER, repo=REPO, anchor_comment_urls=[ANCHOR_URL], fixture_path=None
    )
    assert exit_code == preflight.EXIT_ENVIRONMENT_FAILURE
    assert result["source"] == "gh_cli"
    assert result["operation"] == "list_issue_comments"
    assert [a[:2] for a in argvs] == [["gh", "issue"], ["gh", "api"]], argvs
    assert used == []


def test_multipage_comments_anchor_resolution_single_transport(tmp_path, monkeypatch):
    """GIVEN the real native `gh` transport and a two-page paginated comments
    response (`gh api --paginate --slurp` shape) whose second page holds the
    anchor comment
    WHEN `run_preflight()` resolves the anchor
    THEN exactly one Issue read and one comments traversal run through the
    same `gh` transport, the anchor is resolved from the complete traversal
    (NO extra single-comment GET) and nothing goes anonymous (Issue #2872
    AC5(f))."""
    _force_isolated_profile(tmp_path, monkeypatch)
    used = _forbid_credentialless_authority(monkeypatch)
    argvs: list[list[str]] = []
    page_1 = [{"id": 111, "body": "first page comment"}]
    page_2 = [dict(_ANCHOR_PAYLOAD)]

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        argvs.append(list(argv))
        if argv[:3] == ["gh", "issue", "view"]:
            return dict(_ISSUE_PAYLOAD), ""
        if "--slurp" in argv and any("/comments" in a for a in argv):
            return [page_1, page_2], ""
        raise AssertionError(f"unexpected extra gh read: {argv}")

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)

    preflight.run_preflight(
        issue_number=ISSUE_NUMBER, repo=REPO, anchor_comment_urls=[ANCHOR_URL], fixture_path=None
    )

    assert len(argvs) == 2, argvs
    assert argvs[0][:3] == ["gh", "issue", "view"]
    assert any(f"repos/{REPO}/issues/{ISSUE_NUMBER}/comments" in a for a in argvs[1]), argvs[1]
    assert not any(f"/issues/comments/{ANCHOR_COMMENT_ID}" in " ".join(a) for a in argvs)
    assert used == []


def test_selector_is_constant_and_ignores_every_auth_signal(tmp_path, monkeypatch):
    """The selector returns one process-wide native `gh` instance regardless
    of HOME, token env, `GH_CONFIG_DIR` and `GH_HOST` (no auth-state
    inspection exists to depend on; Issue #2872 AC4/AC8)."""
    first = preflight._select_read_transport()
    _force_isolated_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("GH_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("GH_HOST", "example.invalid")
    assert preflight._select_read_transport() is first
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.delenv("GH_CONFIG_DIR")
    assert preflight._select_read_transport() is first
    assert first is preflight._NATIVE_GH_READ_TRANSPORT


def test_default_and_explicit_fetch_call_paths_land_on_one_transport_instance(tmp_path, monkeypatch):
    """Call-recording proof that the process-wide constant selector cannot
    split authority (Issue #2872 AC3): the production constant is swapped for
    ONE recording instance, then the `transport=None` default fetchers
    (`_fetch_issue` / `_fetch_issue_comments` / `_fetch_single_comment`), the
    explicit-`transport=` default-fetcher call sites added for the
    repair-apply / trusted-anchor lane, and a real `run_preflight()` all run.
    Every recorded read lands on that single instance and nothing reaches the
    anonymous credentialless authority."""
    used = _forbid_credentialless_authority(monkeypatch)
    recorder = _RecordingTransport(comments=[dict(_ANCHOR_PAYLOAD)], anchor=dict(_ANCHOR_PAYLOAD))
    monkeypatch.setattr(preflight, "_NATIVE_GH_READ_TRANSPORT", recorder)
    assert preflight._select_read_transport() is recorder

    # `transport=None` default fetchers resolve the one constant instance.
    assert preflight._fetch_issue(REPO, ISSUE_NUMBER) == (dict(_ISSUE_PAYLOAD), "")
    assert preflight._fetch_issue_comments(REPO, ISSUE_NUMBER) == ([dict(_ANCHOR_PAYLOAD)], "")
    assert preflight._fetch_single_comment(REPO, ANCHOR_COMMENT_ID) == (dict(_ANCHOR_PAYLOAD), "")
    # An explicitly passed instance is honoured as-is (explicit threading).
    explicit = _RecordingTransport()
    preflight._fetch_issue(REPO, ISSUE_NUMBER, transport=explicit)
    assert [name for name, _ in explicit.calls] == ["read_issue"]
    assert [name for name, _ in recorder.calls] == ["read_issue", "list_issue_comments", "read_issue_comment"]

    # A real production invocation: Issue, comments (anchor resolved out of
    # the complete traversal) all land on the same instance.
    recorder.calls.clear()
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)
    preflight.run_preflight(issue_number=ISSUE_NUMBER, repo=REPO, anchor_comment_urls=[ANCHOR_URL], fixture_path=None)
    assert [name for name, _ in recorder.calls][:2] == ["read_issue", "list_issue_comments"], recorder.calls
    assert used == []


def test_unthreaded_read_call_sites_are_a_closed_set_resolving_the_constant_selector():
    """Structural guard: every call site of the three `_fetch_*` readers that
    does NOT pass `transport=` is a known, closed set of functions. Those
    sites resolve the `transport=None` default, which is the one
    process-wide constant instance (see
    `test_default_and_explicit_fetch_call_paths_land_on_one_transport_instance`).
    They are intentionally not threaded explicitly: pre-existing tests
    outside this module's Allowed Paths (e.g.
    `test_structural_repair_action_apply_consumer.py`,
    `test_evidence_index_preflight_integration.py`) patch the readers with
    fixed-arity doubles. A NEW un-threaded call site must be threaded or
    consciously added here -- it cannot silently pick its own authority."""
    import ast

    tree = ast.parse(Path(preflight.__file__).read_text(encoding="utf-8"))
    readers = {"_fetch_issue", "_fetch_issue_comments", "_fetch_single_comment"}
    unthreaded: list[tuple[str, str]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name in readers and "transport" not in {kw.arg for kw in node.keywords}:
                unthreaded.append((name, self.stack[-1] if self.stack else "<module>"))
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert {fn for _, fn in unthreaded} <= {
        "run_preflight",
        "_default_fetch_current",
        "_revalidate_owner_anchor_sources_before_dispatch",
        "fetch_current",
    }, unthreaded


def test_gh_exit_4_is_projected_as_gh_auth_required():
    """gh's documented authentication-required exit code maps to the closed
    `gh_auth_required` reason, never to a generic `gh_exit_error`."""
    assert preflight._project_environment_failure_reason("gh_exit_4: authentication required") == "gh_auth_required"
    assert preflight._project_environment_failure_reason("gh_exit_1: boom") == "gh_exit_error"

def test_gh_outage_stderr_projection_is_bounded_and_not_blanket():
    """Native-`gh` failure projection (Issue #2872 AC9 / PR #2876 review P2):
    only stderr-CONFIRMED external outage maps onto the outage reason codes
    (the AC9 UNAVAILABLE set); every other `gh_exit_<N>` stays the unclassified
    `gh_exit_error` and exit 4 stays `gh_auth_required`. `gh` exits 1 for HTTP
    and connection errors alike, so the exit code alone never implies outage."""
    project = preflight._project_environment_failure_reason
    assert project("gh_timeout after 30s") == "gh_timeout"
    assert project("gh_exit_4: authentication required") == "gh_auth_required"
    assert project("gh_exit_1: HTTP 403: API rate limit exceeded") == "rate_limited"
    assert project("gh_exit_1: HTTP 502: Bad Gateway") == "upstream_environment_failure"
    assert project("gh_exit_1: error connecting to api.github.com") == "transport_connectivity_failure"
    assert project("gh_exit_1: boom") == "gh_exit_error"
    assert project("gh_exit_1: HTTP 403: Resource not accessible by integration") == "gh_exit_error"
    assert project("gh_exit_2: ") == "gh_exit_error"

    classify = preflight._classify_gh_single_comment_error
    prefix = preflight._TRANSPORT_FAILURE_PREFIX
    assert classify("gh_timeout after 30s") == f"{prefix}gh_timeout"
    assert classify("gh_exit_4: authentication required") == f"{prefix}gh_auth_required"
    assert classify("gh_exit_1: HTTP 403: API rate limit exceeded") == f"{prefix}rate_limited"
    assert classify("gh_exit_1: HTTP 503: unavailable") == f"{prefix}upstream_environment_failure"
    assert classify("gh_exit_1: dial tcp: lookup api.github.com: no such host") == (
        f"{prefix}transport_connectivity_failure"
    )
    assert classify("gh_exit_1: boom") == f"{prefix}gh_exit_1"
    assert classify("gh_exit_1: HTTP 404: Not Found").startswith(preflight._SEMANTIC_MISSING_PREFIX)


# ---------------------------------------------------------------------------
# Issue #2872 AC9 / AC11 runtime verification (opt-in `github_live`; deselected
# from the default python-test run and NOT a required CI gate -- #2361).
#
# `_classify_ac9_outcome()` is the single, network-independent success /
# UNAVAILABLE / FAIL predicate. The live test below feeds it the stdout fields
# and the freshly produced `raw_issue_snapshot.json` of a real run; the
# parametrized `test_ac9_predicate_classification` pins it without a network.
# ---------------------------------------------------------------------------

_AC9_ISSUE_NUMBER = 2845
_AC9_ANCHOR_ID = "5942301496"
_AC9_ANCHOR_URL = f"https://github.com/{REPO}/issues/{_AC9_ISSUE_NUMBER}#issuecomment-{_AC9_ANCHOR_ID}"
_AC9_COMMAND_ID = "preflight.run.with_human_context"
# Confirmed external outage / unreachability only. `gh_exit_error` is NOT here
# (blanket SKIP would hide implementation defects) and neither is
# `gh_auth_required` (an auth that is usable in the parent but did not reach
# the child is exactly the regression AC9 exists to catch).
_AC9_UNAVAILABLE_REASON_CODES = frozenset(
    {"rate_limited", "upstream_environment_failure", "transport_connectivity_failure", "gh_timeout"}
)
_AC9_READ_OPERATIONS = frozenset({"read_issue", "list_issue_comments", "read_issue_comment"})
_AC9_VALID_STATUSES = frozenset({"pass", "warn", "needs_fix", "blocked", "environment_failure"})


class _Ac9Outcome(NamedTuple):
    verdict: str  # "PASS" | "FAIL" | "UNAVAILABLE"
    reason: str
    downstream: str  # non-empty only for PASS: Step 1 reach / separated later blocker


def _ac9_first(fields: dict, key: str) -> str:
    values = fields.get(key) or []
    return str(values[0]).strip() if values else ""


def _ac9_parse_stdout(stdout: str) -> dict[str, list[str]]:
    """Project the compact preflight stdout onto the AC9 fields (no raw body)."""
    fields: dict[str, list[str]] = {
        key: [ln[len(key) + 1 :].strip() for ln in stdout.splitlines() if ln.startswith(key + ":")]
        for key in ("STATUS", "REASON_CODE", "SOURCE", "OPERATION")
    }
    blockers: list[str] = []
    in_blockers = False
    for ln in stdout.splitlines():
        if ln.startswith("BLOCKERS:"):
            in_blockers = True
        elif in_blockers and ln.startswith("  - "):
            blockers.append(ln[4:].strip())
        elif in_blockers:
            in_blockers = False
    fields["BLOCKERS"] = blockers
    return fields


def _ac9_anchor_resolution_problem(snapshot) -> str:
    """Positive check on the snapshot THIS run produced: Issue #2845, comments
    retrieved, anchor `5942301496` resolved. Returns "" when all hold."""
    if not isinstance(snapshot, dict):
        return "snapshot_missing"
    issue = snapshot.get("issue")
    if (
        snapshot.get("issue_number") != _AC9_ISSUE_NUMBER
        or snapshot.get("repo") != REPO
        or not isinstance(issue, dict)
        or issue.get("number") != _AC9_ISSUE_NUMBER
    ):
        return "issue_identity_mismatch"
    comments = snapshot.get("comments")
    if not isinstance(comments, list) or not comments:
        return "comments_not_retrieved"
    anchor = snapshot.get("anchor_comment")
    if not isinstance(anchor, dict):
        return "anchor_not_resolved"
    if (
        str(anchor.get("id")) != _AC9_ANCHOR_ID
        or anchor.get("url") != _AC9_ANCHOR_URL
        or anchor.get("issue_number") != _AC9_ISSUE_NUMBER
    ):
        return "anchor_identity_mismatch"
    if not any(isinstance(c, dict) and str(c.get("id")) == _AC9_ANCHOR_ID for c in comments):
        return "anchor_not_in_retrieved_comments"
    return ""


def _classify_ac9_outcome(fields: dict, snapshot) -> _Ac9Outcome:
    """AC9 verdict from the stdout `fields` and this run's `snapshot`.

    UNAVAILABLE only for a CONFIRMED external outage; PASS only on positive
    resolution of Issue #2845 + anchor `5942301496` (a later contract blocker
    is reported as `downstream`, never an AC9 failure); everything else FAIL."""
    status = _ac9_first(fields, "STATUS")
    if status not in _AC9_VALID_STATUSES:
        return _Ac9Outcome("FAIL", "status_empty_or_malformed", "")
    source = _ac9_first(fields, "SOURCE")
    reason_code = _ac9_first(fields, "REASON_CODE")
    operation = _ac9_first(fields, "OPERATION")
    if source == "credentialless_transport":
        return _Ac9Outcome("FAIL", "credentialless_transport_selected", "")
    if status == "environment_failure":
        if reason_code in _AC9_UNAVAILABLE_REASON_CODES:
            return _Ac9Outcome("UNAVAILABLE", f"external_outage:{reason_code}", "")
        if (
            operation in _AC9_READ_OPERATIONS
            or reason_code.startswith("gh_")
            or reason_code == "transport_internal_error"
            or not reason_code
        ):
            detail = f"{operation or 'none'}:{reason_code or 'none'}"
            return _Ac9Outcome("FAIL", f"read_phase_environment_failure:{detail}", "")
    problem = _ac9_anchor_resolution_problem(snapshot)
    if problem:
        return _Ac9Outcome("FAIL", problem, "")
    if status == "environment_failure":
        downstream = f"downstream_environment_failure:{reason_code}"
    elif status == "blocked":
        blockers = ",".join(fields.get("BLOCKERS") or []) or "unspecified"
        downstream = f"downstream_blocker:{blockers}"
    else:
        downstream = f"step1_reached:{status}"
    return _Ac9Outcome("PASS", "anchor_resolved", downstream)


def _ac9_positive_snapshot() -> dict:
    return {
        "schema_version": "raw_issue_snapshot/v1",
        "issue_number": _AC9_ISSUE_NUMBER,
        "repo": REPO,
        "issue": {"number": _AC9_ISSUE_NUMBER},
        "comments": [{"id": 1}, {"id": int(_AC9_ANCHOR_ID)}],
        "anchor_comment": {
            "id": int(_AC9_ANCHOR_ID),
            "url": _AC9_ANCHOR_URL,
            "issue_number": _AC9_ISSUE_NUMBER,
        },
    }


def _ac9_snapshot(**overrides) -> dict:
    snap = _ac9_positive_snapshot()
    snap.update(overrides)
    return snap


def _ac9_fields(status="blocked", reason="", source="", operation="", blockers=()) -> dict:
    return {
        "STATUS": [status] if status else [],
        "REASON_CODE": [reason] if reason else [],
        "SOURCE": [source] if source else [],
        "OPERATION": [operation] if operation else [],
        "BLOCKERS": list(blockers),
    }


@pytest.mark.parametrize(
    ("label", "fields", "snapshot", "verdict"),
    [
        # --- FAIL: the three false positives of the previous predicate ------
        (
            "anchor_not_found_blocked",
            _ac9_fields("blocked", blockers=["BLOCKER_ANCHOR_COMMENT_NOT_FOUND"]),
            _ac9_snapshot(anchor_comment=None),
            "FAIL",
        ),
        (
            "anchor_not_found_no_snapshot",
            _ac9_fields("blocked", blockers=["BLOCKER_ANCHOR_COMMENT_NOT_FOUND"]),
            None,
            "FAIL",
        ),
        (
            "read_issue_comment_auth_failure",
            _ac9_fields("environment_failure", "gh_auth_required", "gh_cli", "read_issue_comment"),
            None,
            "FAIL",
        ),
        (
            "read_issue_comment_transport_failure",
            _ac9_fields("environment_failure", "transport_internal_error", "gh_cli", "read_issue_comment"),
            None,
            "FAIL",
        ),
        ("empty_status", _ac9_fields(""), _ac9_positive_snapshot(), "FAIL"),
        ("malformed_status", _ac9_fields("???"), _ac9_positive_snapshot(), "FAIL"),
        # --- FAIL: identity must be positively bound ------------------------
        ("other_issue_snapshot", _ac9_fields("pass"), _ac9_snapshot(issue_number=2846), "FAIL"),
        ("other_issue_payload", _ac9_fields("pass"), _ac9_snapshot(issue={"number": 1}), "FAIL"),
        (
            "other_anchor",
            _ac9_fields("pass"),
            _ac9_snapshot(anchor_comment={"id": 1, "url": "x", "issue_number": _AC9_ISSUE_NUMBER}),
            "FAIL",
        ),
        ("no_comments", _ac9_fields("pass"), _ac9_snapshot(comments=[]), "FAIL"),
        (
            "anchor_not_among_comments",
            _ac9_fields("pass"),
            _ac9_snapshot(comments=[{"id": 1}]),
            "FAIL",
        ),
        # --- FAIL: no blanket SKIP / regression to anonymous ----------------
        (
            "gh_exit_error_is_not_blanket_skip",
            _ac9_fields("environment_failure", "gh_exit_error", "gh_cli", "read_issue"),
            None,
            "FAIL",
        ),
        (
            "gh_auth_required_propagation_regression",
            _ac9_fields("environment_failure", "gh_auth_required", "gh_cli", "read_issue"),
            None,
            "FAIL",
        ),
        (
            "credentialless_source",
            _ac9_fields("blocked", source="credentialless_transport"),
            _ac9_positive_snapshot(),
            "FAIL",
        ),
        # --- UNAVAILABLE: confirmed external outage only --------------------
        (
            "gh_timeout",
            _ac9_fields("environment_failure", "gh_timeout", "gh_cli", "read_issue"),
            None,
            "UNAVAILABLE",
        ),
        (
            "gh_timeout_on_anchor_read",
            _ac9_fields("environment_failure", "gh_timeout", "gh_cli", "read_issue_comment"),
            None,
            "UNAVAILABLE",
        ),
        (
            "rate_limited",
            _ac9_fields("environment_failure", "rate_limited", "gh_cli", "list_issue_comments"),
            None,
            "UNAVAILABLE",
        ),
        (
            "upstream_outage",
            _ac9_fields("environment_failure", "upstream_environment_failure", "gh_cli", "read_issue"),
            None,
            "UNAVAILABLE",
        ),
        (
            "connectivity_failure",
            _ac9_fields("environment_failure", "transport_connectivity_failure", "gh_cli", "read_issue"),
            None,
            "UNAVAILABLE",
        ),
        # --- PASS: positive resolution; later blockers separated -----------
        ("positive_success", _ac9_fields("pass"), _ac9_positive_snapshot(), "PASS"),
        (
            "downstream_contract_blocker_after_anchor_resolved",
            _ac9_fields("blocked", blockers=["BLOCKER_SOME_2845_CONTRACT_DEFECT"]),
            _ac9_positive_snapshot(),
            "PASS",
        ),
        (
            "downstream_needs_fix_after_anchor_resolved",
            _ac9_fields("needs_fix"),
            _ac9_positive_snapshot(),
            "PASS",
        ),
    ],
)
def test_ac9_predicate_classification(label, fields, snapshot, verdict):
    """Network-independent pin of the AC9 predicate (Issue #2872 AC11): anchor
    not found / `read_issue_comment` transport-auth failure / empty `STATUS:` /
    identity mismatch are FAIL (never PASS); `gh_timeout` is UNAVAILABLE;
    `gh_exit_error` is not a blanket SKIP; a downstream contract blocker after
    positive resolution is not an AC9 failure and is reported separately."""
    outcome = _classify_ac9_outcome(fields, snapshot)
    assert outcome.verdict == verdict, (label, outcome)
    assert bool(outcome.downstream) == (verdict == "PASS"), (label, outcome)
    if label.startswith("downstream_contract_blocker"):
        assert "BLOCKER_SOME_2845_CONTRACT_DEFECT" in outcome.downstream


def test_ac9_stdout_parser_extracts_fields_and_blockers():
    stdout = (
        "STATUS: blocked\nNEXT_ACTION: human_judgment_required\nBLOCKERS:\n  - B_ONE\n  - B_TWO\n"
        "REQUIRED_SECTIONS:\n  - Outcome\n"
    )
    fields = _ac9_parse_stdout(stdout)
    assert fields["STATUS"] == ["blocked"] and fields["BLOCKERS"] == ["B_ONE", "B_TWO"]
    assert _ac9_parse_stdout("STATUS:\n")["STATUS"] == [""]
    assert _classify_ac9_outcome(_ac9_parse_stdout("STATUS:\n"), _ac9_positive_snapshot()).verdict == "FAIL"


def _ac9_write_evidence(artifacts: Path, stamp: str, lines: list[str], forbidden: list[str]) -> Path:
    """Write the sanitized AC9 evidence log. A write failure (or a log that
    would carry a forbidden value) is a hard FAIL ("evidence not captured"),
    never silently ignored and never promoted to PASS."""
    text = "\n".join(lines) + "\n"
    leaked = [value for value in forbidden if value and value in text]
    if leaked:
        pytest.fail("AC9 EVIDENCE_NOT_CAPTURED: sanitized log would carry a forbidden value")
    try:
        artifacts.mkdir(parents=True, exist_ok=True)
        path = artifacts / f"runtime-verification-AC9-{stamp}.log"
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        pytest.fail(f"AC9 EVIDENCE_NOT_CAPTURED: cannot write the evidence log ({type(exc).__name__})")
    return path


def test_ac9_evidence_write_failure_is_a_hard_failure_not_swallowed(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="EVIDENCE_NOT_CAPTURED"):
        _ac9_write_evidence(blocker / "artifacts", "20260101T000000Z", ["verdict: PASS"], [])
    with pytest.raises(pytest.fail.Exception, match="EVIDENCE_NOT_CAPTURED"):
        _ac9_write_evidence(tmp_path / "artifacts", "20260101T000000Z", ["home: /ho" "me/someone"], ["/ho" "me/someone"])
    written = _ac9_write_evidence(tmp_path / "artifacts", "20260101T000000Z", ["verdict: PASS"], [])
    assert written.read_text(encoding="utf-8") == "verdict: PASS\n"


def _ac9_detect_auth_form(parent_home: str) -> tuple[str, str]:
    """Category of the parent's GitHub auth shape, by PRESENCE only -- never
    reads a token value, `hosts.yml`, a keyring or runs `gh auth status`.
    Returns (category, config_dir): `token_env`, `stored_config_dir` (path the
    launcher would fix as `GH_CONFIG_DIR`: ambient `GH_CONFIG_DIR`, else
    `$HOME/.config/gh`), or ("none", "")."""
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return "token_env", ""
    config_dir = os.environ.get("GH_CONFIG_DIR") or os.path.join(parent_home, ".config", "gh")
    if os.path.isdir(config_dir):
        return "stored_config_dir", config_dir
    return "none", ""


