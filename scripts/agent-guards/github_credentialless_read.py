#!/usr/bin/env python3
"""Credentialless public GitHub REST read for Claude-GPT isolated sessions
(Issue #2241, hardened per PR #2247 human review).

This module was introduced for a Claude-GPT launcher that did not forward
GitHub auth into the isolated child (Issue #2232 comment 5316900237 root
cause), so the child could still read a *public* GitHub Issue (and its
comments) in this repository without any authentication.

Issue #2872 note: since the #2299 launcher contract the isolated child shares
GitHub auth natively, and `run_refinement_preflight.py`'s production read path
uses native `gh` for every profile -- an isolated HOME no longer selects this
transport. It remains a deliberately narrow GET-only primitive that a caller
must opt into explicitly (and whose anonymous 60 req/hour budget is accepted
only for such explicit use):

  - GET only. There is no function anywhere in this module that constructs
    a request with any HTTP method other than GET. This is a hard,
    read-only boundary enforced at the source level, not a runtime flag.
  - Repository-bound. The trusted slug comes from the
    single identity config (`repo_identity.load_trusted_repo_slug`); every
    read helper rejects any other repository slug before making a network
    call, and fails closed when the config is unusable.
  - Never sends an `Authorization` header. Host credentials
    (`GH_TOKEN` / `GITHUB_TOKEN` / `GH_CONFIG_DIR`) are neither read from
    nor forwarded into the request. `sanitized_env()` below documents and
    enforces this even for any future subprocess-based caller.
  - Never follows an HTTP redirect. `_RedirectRejectingHandler` makes every
    redirect (301/302/303/307/308, regardless of target host/scheme) a
    hard `CrossRepositoryReadRejected` instead of an implicit follow --
    GitHub Issue transfer between repositories is served as a redirect, and
    a follow would silently defeat the repository-bound check above
    (PR #2247 review P1-2).

Out of scope (Issue #2241 "Out of Scope" / Stop Conditions): a GET-only
authenticated read broker for higher rate limits. This module intentionally
accepts the anonymous GitHub REST rate limit (60 requests/hour per source
IP) until that limit is demonstrated to be insufficient for normal
workload -- at which point a follow-up Issue should design an authenticated
broker, not this module.

PR #2247 review P1-1 note: this module provides the credentialless
transport primitives (`read_public_issue`, `list_issue_comments`,
`CredentiallessGitHubReadTransport`). `run_refinement_preflight.py` wraps them
as `_CredentiallessPreflightTransport`, which a caller passes explicitly via
`transport=`; it is no longer chosen implicitly by an isolated-session profile
(Issue #2872).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import repo_identity

# Trust boundary: this module only ever reads from the repository named in
# the identity config (config/repository_identity.v1.json). Never derived from
# an environment variable or CLI argument; the slug is resolved at call time.

GITHUB_API_BASE = "https://api.github.com"
_TRUSTED_API_HOST = "api.github.com"

# Host GitHub credential environment keys that must never be read by this
# module, and must never be present in any environment this module builds
# for a subprocess (this module does not spawn any subprocess today, but
# `sanitized_env()` exists so a future caller cannot accidentally forward
# these into one).
_CREDENTIAL_ENV_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR")

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# GitHub's paginated `Link` response header, e.g.:
#   <https://api.github.com/...&page=2>; rel="next", <...>; rel="last"
_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class CredentiallessReadError(Exception):
    """Base class for credentialless-read rejections."""


class CrossRepositoryReadRejected(CredentiallessReadError):
    """Raised when a caller requests a repository other than
    the trusted repository slug (Issue #2241 AC4), or when the underlying HTTP
    transport attempts to redirect to a different repository/host/scheme
    (PR #2247 review P1-2)."""


class InvalidIssueNumberRejected(CredentiallessReadError):
    """Raised when the requested issue number is not a positive integer."""


class InvalidCommentIdRejected(CredentiallessReadError):
    """Raised when the requested comment id is not a positive integer
    (Issue #2257 AC1 -- `read_single_comment` companion to
    `InvalidIssueNumberRejected`)."""


class TransportConnectivityFailure(CredentiallessReadError):
    """Raised when the underlying network transport itself fails before
    any HTTP response is received: DNS resolution failure, connection
    refused/reset, no route to host, or a client-side timeout (Issue #2257
    AC3/AC7). Distinguished from the HTTP-status-derived exceptions above
    because no HTTP status code was ever received to classify."""


class MalformedResponseBody(CredentiallessReadError):
    """Raised when a response body that was received successfully (a real
    HTTP response, not a transport failure) cannot be parsed as JSON
    (Issue #2257 AC3/AC7)."""


class RateLimitedRejected(CredentiallessReadError):
    """Raised on HTTP 429, or HTTP 403 when safe response-header metadata
    (`x-ratelimit-remaining: 0`, or a `Retry-After` header indicating
    GitHub's secondary/abuse-detection rate limit) actually indicates a rate
    limit condition. Distinguished from `UnexpectedAuthenticationDependency`
    and `CanonicalResourceMissing` so callers can tell "try again later"
    apart from "this read structurally cannot succeed" (PR #2247 review
    P1-4.1). Issue #2257 AC9: a bare 403 with no such header evidence is
    `ForbiddenRejected`, not this class -- 403 alone is not a sufficient
    condition for "rate limited"."""


class ForbiddenRejected(CredentiallessReadError):
    """Raised on HTTP 403 when safe response-header metadata does not
    indicate a rate limit condition (Issue #2257 AC9). A conservative,
    non-rate-limit classification: the response body is never consulted to
    make this determination."""


class UnexpectedAuthenticationDependency(CredentiallessReadError):
    """Raised on HTTP 401: a public, unauthenticated GET should never
    require authentication. Signals a real problem (the resource is not
    actually public, or GitHub's anonymous-read contract changed), not a
    transient condition (PR #2247 review P1-4.1)."""


class CanonicalResourceMissing(CredentiallessReadError):
    """Raised on HTTP 404: the requested Issue/comment does not exist at
    the trusted, canonical URL (PR #2247 review P1-4.1)."""


class UpstreamEnvironmentFailure(CredentiallessReadError):
    """Raised on HTTP 5xx: an upstream GitHub failure, not a boundary
    violation and not a client error. Callers must not treat this as PASS
    (it is a real failure to obtain the resource) but should distinguish it
    from the fail-closed classes above (PR #2247 review P1-4.1)."""


def sanitized_env() -> dict[str, str]:
    """Return a copy of the current process environment with every known
    host GitHub credential key removed (Issue #2241 AC2).

    This module performs its network I/O in-process via `urllib.request`
    and never spawns a subprocess, so nothing here actually consumes this
    mapping today. It is provided as an explicit, independently-testable
    guarantee: even if a future change in this module (or a caller) adds a
    subprocess invocation, using `sanitized_env()` for that subprocess's
    environment keeps host credentials from leaking into it.
    """
    return {key: value for key, value in os.environ.items() if key not in _CREDENTIAL_ENV_KEYS}


def _validate_repo(repo: str | None) -> str:
    """Resolve the default (None -> identity config) and reject anything else."""
    if repo is None:
        repo = repo_identity.load_trusted_repo_slug()
    if not repo_identity.is_trusted_repo(repo) or not _REPO_SLUG_RE.match(repo):
        raise CrossRepositoryReadRejected(f"cross_repository_read_rejected:{repo!r}")
    return repo


def _validate_issue_number(issue_number: int) -> None:
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
        raise InvalidIssueNumberRejected(f"invalid_issue_number:{issue_number!r}")


def _validate_comment_id(comment_id: int) -> None:
    if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id <= 0:
        raise InvalidCommentIdRejected(f"invalid_comment_id:{comment_id!r}")


class _RedirectRejectingHandler(urllib.request.HTTPRedirectHandler):
    """Rejects every HTTP redirect outright (Issue #2241 / PR #2247 review
    P1-2).

    GitHub serves an Issue-transfer-to-another-repository as a redirect
    (e.g. HTTP 301) to the new canonical URL. Silently following it would
    let this module read (and implicitly trust) an Issue outside
    the trusted repository slug even though the caller-supplied `repo` argument was
    validated up front -- the redirect target's repository identity is
    never re-validated by a plain `urlopen()` follow. This handler makes
    ALL redirects (any status code, any target host, any target scheme --
    including an HTTPS-to-HTTP downgrade) a hard failure instead of an
    implicit follow. There is no "same repository, so allow it" carve-out:
    a trusted read must reach its answer via the URL this module
    constructed, in one hop, or not at all.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CrossRepositoryReadRejected(
            f"redirect_rejected:status={code}:from={req.full_url!r}:to={newurl!r}"
        )

    # `HTTPRedirectHandler` also defines `http_error_301`/`http_error_302`/
    # etc. as thin wrappers around `redirect_request` for the legacy
    # `http_error_*` dispatch path; overriding `redirect_request` alone is
    # sufficient because those wrappers all delegate to it.


# Built once at import time. `build_opener` replaces the default
# `HTTPRedirectHandler`-derived handler with this subclass (it detects the
# subclass relationship), so every request issued through `_opener` -- and
# only requests issued through `_opener`, never the module-level
# `urllib.request.urlopen` -- is redirect-rejecting.
_opener = urllib.request.build_opener(_RedirectRejectingHandler)


def _classify_http_error(exc: urllib.error.HTTPError) -> Exception:
    """Map an `HTTPError` status code to the specific exception class
    callers should see (PR #2247 review P1-4.1; refined by Issue #2257 AC9).

    401 -> UnexpectedAuthenticationDependency (a public GET should never
           need auth)
    429 -> RateLimitedRejected unconditionally (GitHub's unambiguous "Too
           Many Requests" status).
    403 -> RateLimitedRejected ONLY when safe response-header metadata
           actually indicates a rate limit: `x-ratelimit-remaining: 0`
           (primary rate limit) or a `Retry-After` header (secondary /
           abuse-detection rate limit). A bare 403 with neither header is
           `ForbiddenRejected` -- 403 alone is never treated as sufficient
           evidence of rate limiting (Issue #2257 AC9; the response body is
           never consulted for this decision).
    404 -> CanonicalResourceMissing
    5xx -> UpstreamEnvironmentFailure
    anything else -> the original HTTPError, unmodified
    """
    status = exc.code
    if status == 401:
        return UnexpectedAuthenticationDependency(f"unexpected_authentication_dependency:{status}")
    if status == 429:
        return RateLimitedRejected(f"rate_limited:{status}")
    if status == 403:
        headers = exc.headers
        remaining = headers.get("x-ratelimit-remaining") if headers is not None else None
        if remaining == "0":
            return RateLimitedRejected("rate_limited:403_primary_ratelimit_exhausted")
        retry_after = headers.get("Retry-After") if headers is not None else None
        if retry_after:
            return RateLimitedRejected("rate_limited:403_secondary_ratelimit")
        return ForbiddenRejected("http_403_forbidden")
    if status == 404:
        return CanonicalResourceMissing(f"canonical_resource_missing:{status}")
    if 500 <= status < 600:
        return UpstreamEnvironmentFailure(f"upstream_environment_failure:{status}")
    return exc


def _build_request(url: str) -> urllib.request.Request:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "agent-platform-credentialless-read (Issue-2241)")
    # Deliberately no Authorization header: no GH_TOKEN / GITHUB_TOKEN /
    # GH_CONFIG_DIR is ever read from the environment by this function.
    return request


def _credentialless_get(url: str, *, timeout: float = 15.0) -> dict:
    """Issue a single, unauthenticated GET request and return the parsed
    JSON body.

    This is the only function in this module that opens a network
    connection without also returning pagination metadata, and it is
    hardcoded to the GET method. No caller in this module -- and no public
    function exported by it -- can cause a non-read HTTP method to be
    issued, and no response is ever followed through a redirect (see
    `_RedirectRejectingHandler`).
    """
    data, _next_url = _credentialless_get_page(url, timeout=timeout)
    return data


# Matches `rel="next"` anywhere in a `Link` header, independent of whether
# the URL portion parses (Issue #2257 AC7(b)): used to distinguish "no next
# page" (no `rel="next"` token at all) from "a `rel="next"` token is present
# but the header is malformed" (must reject, not silently stop pagination).
_LINK_REL_NEXT_PRESENT_RE = re.compile(r'rel="next"')


def _credentialless_get_page(url: str, *, timeout: float = 15.0) -> tuple[object, str | None]:
    """Issue a single, unauthenticated GET request and return
    `(parsed_json_body, next_page_url_or_None)`.

    `next_page_url_or_None` is populated from the response's `Link:
    rel="next"` header (RFC 5988), used by `list_issue_comments` to follow
    pagination without ever falling back to an unbounded/unauthenticated
    `gh api --paginate` shell invocation.
    """
    request = _build_request(url)
    try:
        with _opener.open(request, timeout=timeout) as response:
            raw = response.read()
            link_header = response.headers.get("Link", "")
    except urllib.error.HTTPError as exc:
        raise _classify_http_error(exc) from exc
    except TimeoutError as exc:
        # `socket.timeout` is `TimeoutError` (Python 3.10+); urlopen can
        # raise this directly (not wrapped in URLError) on a read timeout
        # (Issue #2257 AC3/AC7).
        raise TransportConnectivityFailure("transport_connectivity_failure:timeout") from exc
    except urllib.error.URLError as exc:
        # DNS resolution failure, connection refused/reset, no route to
        # host, or a wrapped timeout -- no HTTP response was ever received
        # to classify via `_classify_http_error` (Issue #2257 AC3/AC7). The
        # sanitized reason is the underlying OSError subclass name only,
        # never `str(exc.reason)` (which can include hostnames/addresses).
        reason_cls = type(exc.reason).__name__ if exc.reason is not None else "unknown"
        raise TransportConnectivityFailure(f"transport_connectivity_failure:{reason_cls}") from exc
    try:
        data = json.loads(raw)
    except UnicodeDecodeError as exc:
        # Issue #2257 P0-4: a response body that decoded to bytes but is not
        # valid text in the encoding `json.loads` inferred is a malformed
        # response, not a JSON syntax error -- kept in the same closed
        # exception class (`MalformedResponseBody`) with a distinct reason
        # code so callers can still tell the two apart if needed.
        raise MalformedResponseBody("malformed_response_body:invalid_encoding") from exc
    except json.JSONDecodeError as exc:
        raise MalformedResponseBody("malformed_response_body:invalid_json") from exc
    next_match = _LINK_NEXT_RE.search(link_header or "")
    if next_match is None:
        if _LINK_REL_NEXT_PRESENT_RE.search(link_header or ""):
            # Issue #2257 AC7(b): a `rel="next"` token exists but the URL
            # portion of the Link header did not parse -- reject rather than
            # silently treating this as "no more pages" (which would
            # truncate pagination without any signal).
            raise MalformedResponseBody("malformed_response_body:malformed_link_header")
        return data, None
    next_url = next_match.group(1)
    return data, next_url


def _validate_pagination_target(url: str, *, repo: str, issue_number: int) -> None:
    """Re-validate a `Link: rel="next"` URL before it is followed
    (PR #2247 review P1-2 defense-in-depth, hardened by Issue #2257 AC7/P1-5):
    pagination is a second, server-supplied URL this module did not
    construct itself, so it gets the same host/scheme trust check as the
    redirect handler applies to redirects, PLUS a check that the URL still
    targets the exact same repository/issue/endpoint this call started with
    -- a same-host Link header pointing at a different repository, a
    different issue, or a different REST endpoint entirely (e.g. a crafted
    response trying to redirect comment pagination onto an unrelated
    resource) is rejected rather than followed unconditionally."""
    parsed_scheme, _, rest = url.partition("://")
    if parsed_scheme != "https":
        raise CrossRepositoryReadRejected(f"pagination_scheme_rejected:{url!r}")
    host_and_path = rest.split("?", 1)[0]
    host = host_and_path.split("/", 1)[0].split("@")[-1].split(":")[0]
    if host != _TRUSTED_API_HOST:
        raise CrossRepositoryReadRejected(f"pagination_host_rejected:{url!r}")
    path = "/" + host_and_path.split("/", 1)[1] if "/" in host_and_path else ""
    expected_prefix = f"/repos/{repo}/issues/{issue_number}/comments"
    if not path.startswith(expected_prefix):
        raise CrossRepositoryReadRejected(f"pagination_endpoint_rejected:{url!r}")


def read_public_issue(issue_number: int, repo: str | None = None) -> dict:
    """Read a public GitHub Issue in `repo` with no authentication.

    Raises `CrossRepositoryReadRejected` if `repo` is not
    the trusted repository slug (Issue #2241 AC4), `InvalidIssueNumberRejected` if
    `issue_number` is not a positive integer (both checks happen before any
    network call is attempted), and one of `UnexpectedAuthenticationDependency`
    / `RateLimitedRejected` / `CanonicalResourceMissing` /
    `UpstreamEnvironmentFailure` for the corresponding HTTP status
    (PR #2247 review P1-4.1).
    """
    repo = _validate_repo(repo)
    _validate_issue_number(issue_number)
    url = f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}"
    return _credentialless_get(url)


def read_single_comment(comment_id: int, repo: str | None = None) -> dict:
    """Read a single public GitHub Issue comment in `repo` with no
    authentication (Issue #2257 AC1).

    This is the credentialless counterpart to `_fetch_single_comment`'s
    previously-unconditional `gh api repos/{repo}/issues/comments/{id}`
    call in `run_refinement_preflight.py` (Issue #2257 root cause: that
    call had no isolated-profile branch, so under the pre-#2299 launcher an
    isolated Claude-GPT session -- which then had no working `gh`
    credential -- always failed there, even for a genuinely-existing anchor
    comment).

    Raises `CrossRepositoryReadRejected` if `repo` is not
    the trusted repository slug, `InvalidCommentIdRejected` if `comment_id` is not
    a positive integer (both checks happen before any network call is
    attempted), and one of `UnexpectedAuthenticationDependency` /
    `RateLimitedRejected` / `CanonicalResourceMissing` /
    `UpstreamEnvironmentFailure` / `TransportConnectivityFailure` /
    `MalformedResponseBody` for the corresponding failure. Only
    `CanonicalResourceMissing` (a true HTTP 404) means the comment does
    not exist -- every other exception here is a transport failure that
    must never be reported as "comment not found" (Issue #2257 AC2/AC3).

    Returns the same REST JSON shape `gh api
    repos/{repo}/issues/comments/{comment_id}` would return (this endpoint
    IS that REST endpoint), so a caller that already consumes that `gh
    api` shape needs no field-name translation.
    """
    repo = _validate_repo(repo)
    _validate_comment_id(comment_id)
    url = f"{GITHUB_API_BASE}/repos/{repo}/issues/comments/{comment_id}"
    return _credentialless_get(url)


def list_issue_comments(
    issue_number: int, repo: str | None = None, *, per_page: int = 100
) -> list[dict]:
    """Read every comment on a public GitHub Issue in `repo` with no
    authentication, following `Link: rel="next"` pagination until
    exhausted (PR #2247 review P1-1).

    Returns comments in the same REST JSON shape `gh api
    repos/{repo}/issues/{issue_number}/comments` would return (this
    endpoint IS that REST endpoint), so a caller that already consumes
    `gh api` comment JSON needs no field-name translation.
    """
    repo = _validate_repo(repo)
    _validate_issue_number(issue_number)
    if not isinstance(per_page, int) or not (1 <= per_page <= 100):
        raise CredentiallessReadError(f"invalid_per_page:{per_page!r}")
    comments: list[dict] = []
    seen_comment_ids: set[object] = set()
    url = f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}/comments?per_page={per_page}"
    seen_urls: set[str] = set()
    page_count = 0
    # Issue #2257 AC7: an unbounded loop bounded only by cycle detection is
    # still a denial-of-service surface if a malicious/misbehaving server
    # returns a `Link: rel="next"` chain that never repeats a URL and never
    # progresses -- cap the number of pages this function will ever follow
    # for a single call, independent of cycle detection.
    max_pages = 1000
    while url:
        if url in seen_urls:
            raise CredentiallessReadError(f"pagination_cycle_detected:{url!r}")
        seen_urls.add(url)
        page_count += 1
        if page_count > max_pages:
            raise CredentiallessReadError(f"pagination_page_limit_exceeded:{max_pages}")
        page, next_url = _credentialless_get_page(url)
        if not isinstance(page, list):
            raise CredentiallessReadError(f"unexpected_comments_page_type:{type(page).__name__}")
        if next_url is not None:
            _validate_pagination_target(next_url, repo=repo, issue_number=issue_number)
            if next_url == url:
                # Issue #2257 AC7(d): the server-advertised "next" page is
                # identical to the page just fetched -- pagination is not
                # progressing. Reject rather than looping forever (cycle
                # detection above only catches a URL seen on an EARLIER
                # iteration, not immediate non-progression on the first
                # repeat).
                raise CredentiallessReadError(f"pagination_non_progressing:{next_url!r}")
        for item in page:
            comment_id = item.get("id") if isinstance(item, dict) else None
            if comment_id is not None:
                if comment_id in seen_comment_ids:
                    # Issue #2257 AC7(e): dedupe by comment id -- a
                    # misbehaving/overlapping pagination response must not
                    # produce duplicate comments in the traversal result.
                    continue
                seen_comment_ids.add(comment_id)
            comments.append(item)
        url = next_url
    return comments


def issue_to_gh_cli_shape(raw_issue: dict) -> dict:
    """Convert a raw GitHub REST `/issues/{number}` JSON body (as returned
    by `read_public_issue`) into the field-name shape
    `gh issue view --json number,title,body,labels,url,updatedAt` produces
    (PR #2247 review P1-1) -- so a caller that already consumes the `gh`
    CLI shape (e.g. `run_refinement_preflight.py`'s `_fetch_issue`) can
    switch transports without a schema change downstream.
    """
    labels_raw = raw_issue.get("labels") or []
    labels = [
        {"name": label.get("name")}
        for label in labels_raw
        if isinstance(label, dict) and label.get("name") is not None
    ]
    return {
        "number": raw_issue.get("number"),
        "title": raw_issue.get("title"),
        "body": raw_issue.get("body") or "",
        "labels": labels,
        "url": raw_issue.get("html_url"),
        "updatedAt": raw_issue.get("updated_at"),
    }


class GitHubReadTransport(Protocol):
    """Transport-selector interface (PR #2247 review P1-1; extended by
    Issue #2257 P0-1): a caller that needs to fetch an Issue, its comments,
    and (only when a fresh single-comment readback is explicitly required,
    e.g. TOCTOU verification around a trusted-anchor contract update)
    an individual comment, without caring whether the underlying transport
    is `gh` (authenticated) or this credentialless module, can depend on
    this Protocol instead of importing either transport module directly.
    A single instance of an implementation of this Protocol must be
    selected once per invocation and threaded through every read on that
    invocation's critical path -- no call site may re-select or
    re-instantiate a transport independently (Issue #2257 single-authority
    contract)."""

    def read_issue(self, repo: str, issue_number: int) -> dict: ...

    def list_issue_comments(self, repo: str, issue_number: int) -> list[dict]: ...

    def read_issue_comment(self, repo: str, comment_id: int) -> dict: ...


class CredentiallessGitHubReadTransport:
    """`GitHubReadTransport` implementation backed entirely by this
    module's unauthenticated REST GET functions. Returns the `gh` CLI
    field-name shape for `read_issue` (via `issue_to_gh_cli_shape`) and the
    raw REST shape for `list_issue_comments`/`read_issue_comment` (already
    `gh api`-shaped) so a caller written against `gh`-shaped data needs no
    other changes."""

    def read_issue(self, repo: str, issue_number: int) -> dict:
        return issue_to_gh_cli_shape(read_public_issue(issue_number, repo=repo))

    def list_issue_comments(self, repo: str, issue_number: int) -> list[dict]:
        return list_issue_comments(issue_number, repo=repo)

    def read_issue_comment(self, repo: str, comment_id: int) -> dict:
        return read_single_comment(comment_id, repo=repo)


__all__ = [
    "GITHUB_API_BASE",
    "CanonicalResourceMissing",
    "CredentiallessGitHubReadTransport",
    "CredentiallessReadError",
    "CrossRepositoryReadRejected",
    "ForbiddenRejected",
    "GitHubReadTransport",
    "InvalidCommentIdRejected",
    "InvalidIssueNumberRejected",
    "MalformedResponseBody",
    "RateLimitedRejected",
    "TransportConnectivityFailure",
    "UnexpectedAuthenticationDependency",
    "UpstreamEnvironmentFailure",
    "issue_to_gh_cli_shape",
    "list_issue_comments",
    "read_public_issue",
    "read_single_comment",
    "sanitized_env",
]
