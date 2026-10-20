"""Issue #2241: credentialless public GitHub REST read for Claude-GPT
isolated sessions.

Covers AC2 (host credentials never exposed), AC3 (unauthenticated public
Issue read succeeds), AC4 (cross-repository / non-GET rejection / redirect
rejection), and PR #2247 human review P1-1/P1-2/P1-4 hardening.
"""

from __future__ import annotations

import inspect
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agent-guards"
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import github_credentialless_read as gcr

SLUG = gcr.repo_identity.load_trusted_repo_slug()

# Non-GET HTTP method literals that must never appear anywhere in this
# module's source. Kept out of any docstring/comment in the module itself
# (including this test file's assertions on it) so a plain substring scan
# is a meaningful negative check, not one satisfied by a comment.
_NON_READ_HTTP_METHODS = ("POST", "PATCH", "DELETE", "PUT")


def test_sanitized_env_excludes_host_github_credentials(monkeypatch, tmp_path):
    """GIVEN host GitHub credential environment variables are set
    (GH_TOKEN / GITHUB_TOKEN / GH_CONFIG_DIR)
    WHEN `sanitized_env()` is called
    THEN none of their real values appear in the returned mapping
    (Issue #2241 AC2)."""
    monkeypatch.setenv("GH_TOKEN", "super-secret-host-token")
    monkeypatch.setenv("GITHUB_TOKEN", "another-secret-host-token")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh-config"))
    monkeypatch.setenv("UNRELATED_MARKER", "keep-me")

    env = gcr.sanitized_env()

    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GH_CONFIG_DIR" not in env
    assert "super-secret-host-token" not in env.values()
    assert "another-secret-host-token" not in env.values()
    assert env.get("UNRELATED_MARKER") == "keep-me"


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_opener_open(
    monkeypatch,
    captured: dict[str, object],
    body: bytes = b'{"ok": true}',
    headers: dict[str, str] | None = None,
):
    def _fake_open(request, timeout=None):
        captured["request"] = request
        captured["method"] = request.get_method()
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["headers"] = dict(request.header_items())
        return _FakeResponse(body, headers)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)


def test_credentialless_get_never_sets_authorization_header(monkeypatch):
    """GIVEN GH_TOKEN is set in the process environment
    WHEN `_credentialless_get` builds its request
    THEN the outgoing request never carries an Authorization header, and
    the request is opened via the redirect-rejecting opener with no
    credential material passed anywhere (Issue #2241 AC2)."""
    monkeypatch.setenv("GH_TOKEN", "super-secret-host-token")

    captured: dict[str, object] = {}
    _patch_opener_open(monkeypatch, captured)

    result = gcr._credentialless_get(f"https://api.github.com/repos/{SLUG}/issues/1")

    assert result == {"ok": True}
    assert captured["method"] == "GET"
    header_names_lower = {k.lower() for k in captured["headers"]}
    assert "authorization" not in header_names_lower
    for value in captured["headers"].values():
        assert "super-secret-host-token" not in str(value)


# ---------------------------------------------------------------------------
# PR #2247 review P1-4.2: behavioral (not source-literal) request invariants
# ---------------------------------------------------------------------------


def test_read_public_issue_sends_exact_get_request_with_no_body_or_auth(monkeypatch):
    """GIVEN a trusted repo/issue_number
    WHEN `read_public_issue` is called
    THEN the actual `urllib.request.Request` object handed to the opener is
    a GET with no body, no Authorization header, and the exact expected
    URL -- verified on the live `request` object, not by scanning source
    text for method literals (Issue #2241 AC4, PR #2247 review P1-4.2)."""
    captured: dict[str, object] = {}
    _patch_opener_open(monkeypatch, captured, body=b'{"number": 1, "title": "t"}')

    gcr.read_public_issue(1)

    request = captured["request"]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url == f"https://api.github.com/repos/{SLUG}/issues/1"
    header_names_lower = {k.lower() for k in captured["headers"]}
    assert "authorization" not in header_names_lower


def test_list_issue_comments_sends_exact_get_requests_across_pagination(monkeypatch):
    """GIVEN a paginated comments response (Link: rel="next")
    WHEN `list_issue_comments` follows pagination
    THEN every request issued -- including the followed page -- is GET,
    body-less, unauthenticated, and targets the expected URL
    (Issue #2241 AC4, PR #2247 review P1-1/P1-4.2)."""
    calls: list[dict[str, object]] = []
    page_1_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100"
    page_2_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100&page=2"

    def _fake_open(request, timeout=None):
        calls.append(
            {
                "method": request.get_method(),
                "data": request.data,
                "url": request.full_url,
                "headers": dict(request.header_items()),
            }
        )
        if request.full_url == page_1_url:
            return _FakeResponse(b'[{"id": 1}]', headers={"Link": f'<{page_2_url}>; rel="next"'})
        assert request.full_url == page_2_url
        return _FakeResponse(b'[{"id": 2}]', headers={})

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    comments = gcr.list_issue_comments(1)

    assert comments == [{"id": 1}, {"id": 2}]
    assert len(calls) == 2
    for call in calls:
        assert call["method"] == "GET"
        assert call["data"] is None
        header_names_lower = {k.lower() for k in call["headers"]}
        assert "authorization" not in header_names_lower
    assert calls[0]["url"] == page_1_url
    assert calls[1]["url"] == page_2_url


def test_credentialless_read_rejects_cross_repository_and_mutation_methods():
    """GIVEN a repository other than the trusted slug
    WHEN `read_public_issue` is called with that repository
    THEN it is rejected before any network call, and separately, the
    module's source contains no non-GET HTTP method literal anywhere --
    there is no code path capable of constructing a mutating request
    (Issue #2241 AC4)."""
    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr.read_public_issue(1, repo="attacker-org/other-repo")

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr.read_public_issue(1, repo=SLUG + "-other")

    source = inspect.getsource(gcr)
    for method in _NON_READ_HTTP_METHODS:
        assert method not in source, f"module source must never reference HTTP method {method}"


# ---------------------------------------------------------------------------
# PR #2247 review P1-2: redirect rejection (HTTP redirect based
# cross-repository/cross-host/scheme-downgrade escape)
# ---------------------------------------------------------------------------


def _make_request(url: str) -> gcr.urllib.request.Request:
    return gcr.urllib.request.Request(url, method="GET")


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_to_different_repository_is_rejected(code):
    """GIVEN a trusted-repo Issue read
    WHEN the response redirects (any 30x) to a different repository's
    Issue URL (Issue-transfer scenario)
    THEN the redirect is rejected before it is ever followed
    (PR #2247 review P1-2, negative case: trusted repo -> other repo)."""
    handler = gcr._RedirectRejectingHandler()
    req = _make_request(f"https://api.github.com/repos/{SLUG}/issues/1")
    newurl = "https://api.github.com/repos/attacker-org/other-repo/issues/1"

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        handler.redirect_request(req, None, code, "redirected", {}, newurl)


def test_redirect_to_different_host_is_rejected():
    """GIVEN a trusted `api.github.com` request
    WHEN the response redirects to a different host
    THEN the redirect is rejected (PR #2247 review P1-2, negative case:
    api.github.com -> different host)."""
    handler = gcr._RedirectRejectingHandler()
    req = _make_request(f"https://api.github.com/repos/{SLUG}/issues/1")
    newurl = f"https://evil.example.com/repos/{SLUG}/issues/1"

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        handler.redirect_request(req, None, 301, "redirected", {}, newurl)


def test_redirect_https_to_http_downgrade_is_rejected():
    """GIVEN an HTTPS request
    WHEN the response redirects to the same host over plain HTTP
    THEN the redirect is rejected (PR #2247 review P1-2, negative case:
    HTTPS -> HTTP downgrade)."""
    handler = gcr._RedirectRejectingHandler()
    req = _make_request(f"https://api.github.com/repos/{SLUG}/issues/1")
    newurl = f"http://api.github.com/repos/{SLUG}/issues/1"

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        handler.redirect_request(req, None, 302, "redirected", {}, newurl)


def test_redirect_chain_is_rejected_on_first_hop():
    """GIVEN a request that would otherwise redirect twice (chain)
    WHEN the first redirect is encountered
    THEN it is rejected immediately -- there is no code path in this
    module that ever follows a second hop, because the first is already a
    hard failure (PR #2247 review P1-2, negative case: redirect chain)."""
    handler = gcr._RedirectRejectingHandler()
    req = _make_request(f"https://api.github.com/repos/{SLUG}/issues/1")
    first_hop = f"https://api.github.com/repos/{SLUG}/issues/1?redirected=1"

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        handler.redirect_request(req, None, 302, "redirected", {}, first_hop)
    # A same-host, same-repo, same-scheme redirect is STILL rejected: this
    # module never follows any redirect, so a would-be chain never gets a
    # chance to reach its second hop.


def test_redirect_301_transferred_issue_is_rejected_end_to_end(monkeypatch):
    """GIVEN `read_public_issue` issues a real request through `_opener`
    WHEN the opener's HTTP layer raises the 301-transferred-issue redirect
    (simulated by making `_opener.open` itself route through
    `_RedirectRejectingHandler.redirect_request`, matching what
    `http.client`/`urllib` actually does internally on a 301 response)
    THEN `read_public_issue` propagates `CrossRepositoryReadRejected`
    instead of ever returning a foreign-repository Issue body
    (PR #2247 review P1-2, negative case: 301 transferred issue)."""

    def _fake_open(request, timeout=None):
        handler = gcr._RedirectRejectingHandler()
        newurl = "https://api.github.com/repos/attacker-org/transferred-repo/issues/1"
        handler.redirect_request(request, None, 301, "Moved Permanently", {}, newurl)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr.read_public_issue(1)


# ---------------------------------------------------------------------------
# PR #2247 review P1-4.1: HTTP status classification (fail-closed, no
# blanket OSError-to-skip conversion)
# ---------------------------------------------------------------------------


def _http_error(status: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"https://api.github.com/repos/{SLUG}/issues/1",
        status,
        "err",
        headers or {},
        io.BytesIO(b""),
    )


@pytest.mark.parametrize(
    "status,expected_cls",
    [
        (401, gcr.UnexpectedAuthenticationDependency),
        # Issue #2257 AC9: a bare 403 with no rate-limit header evidence is
        # `ForbiddenRejected`, NOT `RateLimitedRejected` -- 403 alone is not
        # sufficient evidence of rate limiting.
        (403, gcr.ForbiddenRejected),
        (429, gcr.RateLimitedRejected),
        (404, gcr.CanonicalResourceMissing),
        (500, gcr.UpstreamEnvironmentFailure),
        (503, gcr.UpstreamEnvironmentFailure),
    ],
)
def test_http_error_status_is_classified_not_hidden(monkeypatch, status, expected_cls):
    """GIVEN the opener raises `HTTPError` for a given status
    WHEN `read_public_issue` calls through `_credentialless_get`
    THEN the specific, distinguishable exception class is raised -- never a
    blanket `pytest.skip`-worthy `OSError` (PR #2247 review P1-4.1; 403
    header-aware classification per Issue #2257 AC9)."""

    def _fake_open(request, timeout=None):
        raise _http_error(status)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(expected_cls):
        gcr.read_public_issue(1)


def test_403_with_zero_remaining_header_is_primary_rate_limit(monkeypatch):
    """GIVEN the opener raises `HTTPError(403)` WITH `x-ratelimit-remaining:
    0` in the response headers
    WHEN `read_public_issue` calls through `_credentialless_get`
    THEN `RateLimitedRejected` is raised -- safe header metadata is
    sufficient evidence of a primary rate limit (Issue #2257 AC9)."""

    def _fake_open(request, timeout=None):
        raise _http_error(403, headers={"x-ratelimit-remaining": "0"})

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.RateLimitedRejected):
        gcr.read_public_issue(1)


def test_403_with_retry_after_header_is_secondary_rate_limit(monkeypatch):
    """GIVEN the opener raises `HTTPError(403)` WITH a `Retry-After` header
    (GitHub's secondary/abuse-detection rate limit signal)
    WHEN `read_public_issue` calls through `_credentialless_get`
    THEN `RateLimitedRejected` is raised (Issue #2257 AC9)."""

    def _fake_open(request, timeout=None):
        raise _http_error(403, headers={"Retry-After": "60"})

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.RateLimitedRejected):
        gcr.read_public_issue(1)


def test_403_without_any_ratelimit_header_evidence_is_forbidden_not_ratelimited(monkeypatch):
    """GIVEN the opener raises `HTTPError(403)` with no rate-limit-related
    headers at all
    WHEN `read_public_issue` calls through `_credentialless_get`
    THEN `ForbiddenRejected` is raised, and the response body is never
    consulted to make this determination (Issue #2257 AC9)."""

    def _fake_open(request, timeout=None):
        raise _http_error(403, headers={"X-Unrelated": "1"})

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.ForbiddenRejected):
        gcr.read_public_issue(1)


def test_unmapped_http_error_status_is_not_swallowed(monkeypatch):
    """GIVEN the opener raises `HTTPError` for a status this module does
    not have a specific classification for
    WHEN `read_public_issue` is called
    THEN the original `HTTPError` propagates unmodified (fail-closed --
    never silently downgraded to SKIP) (PR #2247 review P1-4.1)."""

    def _fake_open(request, timeout=None):
        raise _http_error(418)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(urllib.error.HTTPError):
        gcr.read_public_issue(1)


# ---------------------------------------------------------------------------
# PR #2247 review P1-1: GitHubReadTransport / issue_to_gh_cli_shape
# ---------------------------------------------------------------------------


def test_issue_to_gh_cli_shape_maps_rest_fields_to_gh_cli_field_names():
    """GIVEN a raw GitHub REST `/issues/{number}` JSON body
    WHEN `issue_to_gh_cli_shape` converts it
    THEN the result matches the field names `gh issue view --json
    number,title,body,labels,url,updatedAt` produces (PR #2247 review
    P1-1), so `run_refinement_preflight.py`'s downstream `gh`-shaped
    consumers need no schema change to switch transports."""
    raw = {
        "number": 42,
        "title": "Example",
        "body": "body text",
        "labels": [{"name": "bug"}, {"name": "priority:high"}],
        "html_url": f"https://github.com/{SLUG}/issues/42",
        "updated_at": "2026-08-01T00:00:00Z",
    }

    shaped = gcr.issue_to_gh_cli_shape(raw)

    assert shaped == {
        "number": 42,
        "title": "Example",
        "body": "body text",
        "labels": [{"name": "bug"}, {"name": "priority:high"}],
        "url": f"https://github.com/{SLUG}/issues/42",
        "updatedAt": "2026-08-01T00:00:00Z",
    }


def test_credentialless_transport_implements_read_transport_protocol(monkeypatch):
    """GIVEN `CredentiallessGitHubReadTransport`
    WHEN its `read_issue`/`list_issue_comments` methods are called
    THEN they delegate to the credentialless functions above with the
    correct repo/issue_number binding and return the expected shapes
    (PR #2247 review P1-1)."""
    captured: dict[str, object] = {}
    body = json.dumps(
        {
            "number": 7,
            "title": "t",
            "labels": [],
            "html_url": f"https://github.com/{SLUG}/issues/7",
            "updated_at": "2026-08-01T00:00:00Z",
        }
    ).encode("utf-8")
    _patch_opener_open(monkeypatch, captured, body=body)

    transport = gcr.CredentiallessGitHubReadTransport()
    issue = transport.read_issue(SLUG, 7)

    assert issue["number"] == 7
    assert issue["url"] == f"https://github.com/{SLUG}/issues/7"
    assert "updatedAt" in issue

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        transport.read_issue("attacker-org/other-repo", 7)


# ---------------------------------------------------------------------------
# Issue #2257 AC1/AC3/AC7: `read_single_comment` and the fault-injection
# matrix (401/403/404/429/5xx/DNS/connection/timeout/invalid JSON/incomplete
# pagination) that `_fetch_single_comment` in `run_refinement_preflight.py`
# depends on to distinguish semantic_missing from transport_failure.
# ---------------------------------------------------------------------------


def test_read_single_comment_sends_exact_get_request_with_no_body_or_auth(monkeypatch):
    """GIVEN a trusted repo/comment_id
    WHEN `read_single_comment` is called
    THEN the actual request is a GET with no body, no Authorization
    header, and the exact expected single-comment URL (Issue #2257 AC1)."""
    captured: dict[str, object] = {}
    _patch_opener_open(monkeypatch, captured, body=b'{"id": 5315264311, "issue_url": "x"}')

    result = gcr.read_single_comment(5315264311)

    assert result == {"id": 5315264311, "issue_url": "x"}
    request = captured["request"]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url == f"https://api.github.com/repos/{SLUG}/issues/comments/5315264311"
    header_names_lower = {k.lower() for k in captured["headers"]}
    assert "authorization" not in header_names_lower


def test_read_single_comment_rejects_cross_repository_and_invalid_comment_id():
    """GIVEN a repository other than the trusted slug, or a non-positive
    comment id
    WHEN `read_single_comment` is called
    THEN it is rejected before any network call (Issue #2257 AC1)."""
    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr.read_single_comment(1, repo="attacker-org/other-repo")

    with pytest.raises(gcr.InvalidCommentIdRejected):
        gcr.read_single_comment(0)

    with pytest.raises(gcr.InvalidCommentIdRejected):
        gcr.read_single_comment(-1)


@pytest.mark.parametrize(
    "status,expected_cls",
    [
        (401, gcr.UnexpectedAuthenticationDependency),
        (403, gcr.ForbiddenRejected),
        (404, gcr.CanonicalResourceMissing),
        (429, gcr.RateLimitedRejected),
        (500, gcr.UpstreamEnvironmentFailure),
        (503, gcr.UpstreamEnvironmentFailure),
    ],
)
def test_read_single_comment_http_error_status_is_classified(monkeypatch, status, expected_cls):
    """GIVEN the opener raises `HTTPError` for a given status while
    resolving a single comment
    WHEN `read_single_comment` is called
    THEN the specific, distinguishable exception class is raised (Issue
    #2257 AC7 fault-injection matrix: 401/403/404/429/5xx; 403 header-aware
    classification per AC9)."""

    def _fake_open(request, timeout=None):
        raise _http_error(status)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(expected_cls):
        gcr.read_single_comment(1)


def test_read_single_comment_dns_failure_is_transport_connectivity_failure(monkeypatch):
    """GIVEN the opener raises `URLError` wrapping a DNS resolution
    failure (`socket.gaierror`)
    WHEN `read_single_comment` is called
    THEN `TransportConnectivityFailure` is raised -- distinct from any
    HTTP-status-derived exception, since no HTTP response was ever
    received (Issue #2257 AC3/AC7)."""
    import socket

    def _fake_open(request, timeout=None):
        raise urllib.error.URLError(socket.gaierror("Name or service not known"))

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.TransportConnectivityFailure):
        gcr.read_single_comment(1)


def test_read_single_comment_connection_refused_is_transport_connectivity_failure(monkeypatch):
    """GIVEN the opener raises `URLError` wrapping a connection-refused
    error
    WHEN `read_single_comment` is called
    THEN `TransportConnectivityFailure` is raised (Issue #2257 AC3/AC7:
    connection fault-injection case)."""

    def _fake_open(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("Connection refused"))

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.TransportConnectivityFailure):
        gcr.read_single_comment(1)


def test_read_single_comment_timeout_is_transport_connectivity_failure(monkeypatch):
    """GIVEN the opener raises a bare `TimeoutError` (as `urlopen` does on
    a read timeout, not always wrapped in `URLError`)
    WHEN `read_single_comment` is called
    THEN `TransportConnectivityFailure` is raised (Issue #2257 AC3/AC7:
    timeout fault-injection case)."""

    def _fake_open(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.TransportConnectivityFailure):
        gcr.read_single_comment(1)


def test_read_single_comment_invalid_json_is_malformed_response_body(monkeypatch):
    """GIVEN a real HTTP response body that is not valid JSON
    WHEN `read_single_comment` is called
    THEN `MalformedResponseBody` is raised -- distinct from a transport
    failure, since a real response was received (Issue #2257 AC3/AC7:
    invalid JSON fault-injection case)."""
    captured: dict[str, object] = {}
    _patch_opener_open(monkeypatch, captured, body=b"not valid json{{{")

    with pytest.raises(gcr.MalformedResponseBody):
        gcr.read_single_comment(1)


def test_list_issue_comments_incomplete_pagination_cycle_is_rejected(monkeypatch):
    """GIVEN a `Link: rel="next"` response that cycles back to an
    already-seen page URL (a malformed/incomplete pagination sequence)
    WHEN `list_issue_comments` follows pagination
    THEN it fails closed with `CredentiallessReadError` instead of looping
    forever or silently truncating the comment list (Issue #2257 AC7:
    incomplete pagination fault-injection case)."""
    page_1_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100"

    def _fake_open(request, timeout=None):
        # Always points back at itself: an incomplete/cyclic pagination
        # sequence a well-behaved server would never produce.
        return _FakeResponse(b'[{"id": 1}]', headers={"Link": f'<{page_1_url}>; rel="next"'})

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.CredentiallessReadError):
        gcr.list_issue_comments(1)


def test_list_issue_comments_non_list_page_is_rejected(monkeypatch):
    """GIVEN a comments page response body that is not a JSON list (an
    incomplete/malformed pagination page)
    WHEN `list_issue_comments` is called
    THEN it fails closed with `CredentiallessReadError` (Issue #2257 AC7:
    incomplete pagination fault-injection case)."""
    captured: dict[str, object] = {}
    _patch_opener_open(monkeypatch, captured, body=b'{"not": "a list"}')

    with pytest.raises(gcr.CredentiallessReadError):
        gcr.list_issue_comments(1)


# ---------------------------------------------------------------------------
# Issue #2257 AC7 negative controls (a)-(e): pagination hardening beyond
# host/scheme trust -- same-host-but-wrong-target, malformed Link header,
# a true multi-hop cycle, immediate non-progression, and duplicate comment
# id dedupe.
# ---------------------------------------------------------------------------


def test_list_issue_comments_rejects_same_host_link_to_different_issue(monkeypatch):
    """AC7(a): GIVEN a `Link: rel="next"` header that is same-host/same-scheme
    but points at a DIFFERENT issue number's comments endpoint
    WHEN `list_issue_comments` follows pagination
    THEN it is rejected (`CrossRepositoryReadRejected`) -- host/scheme trust
    alone is not sufficient; the endpoint identity must also match."""
    page_1_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100"
    wrong_issue_url = f"https://api.github.com/repos/{SLUG}/issues/999/comments?per_page=100&page=2"

    def _fake_open(request, timeout=None):
        if request.full_url == page_1_url:
            return _FakeResponse(
                b'[{"id": 1}]', headers={"Link": f'<{wrong_issue_url}>; rel="next"'}
            )
        raise AssertionError(f"unexpected follow-through to {request.full_url!r}")

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr.list_issue_comments(1)


def test_list_issue_comments_rejects_same_host_link_to_different_repo(monkeypatch):
    """AC7(a): GIVEN a `Link: rel="next"` header that is same-host but points
    at a different repository's comments endpoint
    WHEN `list_issue_comments` follows pagination
    THEN it is rejected (`CrossRepositoryReadRejected`)."""
    page_1_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100"
    wrong_repo_url = (
        "https://api.github.com/repos/attacker-org/other-repo/issues/1/comments?per_page=100&page=2"
    )

    def _fake_open(request, timeout=None):
        if request.full_url == page_1_url:
            return _FakeResponse(
                b'[{"id": 1}]', headers={"Link": f'<{wrong_repo_url}>; rel="next"'}
            )
        raise AssertionError(f"unexpected follow-through to {request.full_url!r}")

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr.list_issue_comments(1)


def test_list_issue_comments_rejects_same_host_link_to_different_endpoint(monkeypatch):
    """AC7(a): GIVEN a `Link: rel="next"` header that is same-host/same-repo
    but points at a DIFFERENT REST endpoint entirely (not the comments
    listing)
    WHEN `list_issue_comments` follows pagination
    THEN it is rejected (`CrossRepositoryReadRejected`)."""
    page_1_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100"
    wrong_endpoint_url = f"https://api.github.com/repos/{SLUG}/issues/1?page=2"

    def _fake_open(request, timeout=None):
        if request.full_url == page_1_url:
            return _FakeResponse(
                b'[{"id": 1}]', headers={"Link": f'<{wrong_endpoint_url}>; rel="next"'}
            )
        raise AssertionError(f"unexpected follow-through to {request.full_url!r}")

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr.list_issue_comments(1)


def test_list_issue_comments_rejects_malformed_link_header(monkeypatch):
    """AC7(b): GIVEN a `Link` header that contains a `rel="next"` token but
    whose URL portion does not parse
    WHEN `list_issue_comments` follows pagination
    THEN it is rejected (`MalformedResponseBody`) rather than silently
    treated as "no more pages" (which would truncate the traversal without
    any signal)."""
    page_1_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100"

    def _fake_open(request, timeout=None):
        assert request.full_url == page_1_url
        # `rel="next"` present, but no well-formed `<url>` token precedes it.
        return _FakeResponse(b'[{"id": 1}]', headers={"Link": 'rel="next", garbage'})

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.MalformedResponseBody):
        gcr.list_issue_comments(1)


def test_list_issue_comments_detects_true_multi_hop_cycle(monkeypatch):
    """AC7(c): GIVEN a `Link: rel="next"` chain that returns to page 1 only
    after visiting page 2 (a true multi-hop cycle, not an immediate
    non-progression)
    WHEN `list_issue_comments` follows pagination
    THEN it fails closed with `CredentiallessReadError`."""
    page_1_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100"
    page_2_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100&page=2"

    def _fake_open(request, timeout=None):
        if request.full_url == page_1_url:
            return _FakeResponse(b'[{"id": 1}]', headers={"Link": f'<{page_2_url}>; rel="next"'})
        if request.full_url == page_2_url:
            return _FakeResponse(b'[{"id": 2}]', headers={"Link": f'<{page_1_url}>; rel="next"'})
        raise AssertionError(f"unexpected url {request.full_url!r}")

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.CredentiallessReadError):
        gcr.list_issue_comments(1)


def test_list_issue_comments_rejects_immediately_non_progressing_page(monkeypatch):
    """AC7(d): GIVEN a `Link: rel="next"` header on page 1 that points at
    page 1 itself (not merely a later-seen URL, but the URL just fetched)
    WHEN `list_issue_comments` follows pagination
    THEN it fails closed with `CredentiallessReadError` on the very first
    non-progression, without requiring a second full loop iteration."""
    page_1_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100"

    def _fake_open(request, timeout=None):
        assert request.full_url == page_1_url
        return _FakeResponse(b'[{"id": 1}]', headers={"Link": f'<{page_1_url}>; rel="next"'})

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.CredentiallessReadError):
        gcr.list_issue_comments(1)


def test_list_issue_comments_dedupes_duplicate_comment_ids_across_pages(monkeypatch):
    """AC7(e): GIVEN two pages whose comment lists overlap on a shared
    comment id
    WHEN `list_issue_comments` follows pagination
    THEN the duplicate id appears only once in the returned traversal."""
    page_1_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100"
    page_2_url = f"https://api.github.com/repos/{SLUG}/issues/1/comments?per_page=100&page=2"

    def _fake_open(request, timeout=None):
        if request.full_url == page_1_url:
            return _FakeResponse(
                b'[{"id": 1}, {"id": 2}]',
                headers={"Link": f'<{page_2_url}>; rel="next"'},
            )
        assert request.full_url == page_2_url
        # comment id 2 overlaps with the last item of page 1.
        return _FakeResponse(b'[{"id": 2}, {"id": 3}]', headers={})

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    comments = gcr.list_issue_comments(1)

    assert [c["id"] for c in comments] == [1, 2, 3]
