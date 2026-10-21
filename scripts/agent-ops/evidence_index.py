"""Issue #2052: SHA-bound, phase-scoped reuse cache for Issue/comment
snapshot parse/projection results consumed by
``.claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py``.

This is deliberately NOT a generic command cache. It caches exactly one
thing: the already-fetched, already-observed content of a single GitHub
resource (an Issue body or a single comment) together with a caller-supplied
"projection" of that content (e.g. a parsed/derived structure), keyed by a
SHA-bound ``EvidenceKey``:

    (repository, resource_kind, resource_id, observed_content_sha256, config_sha256)

Reuse is bounded to a single declared *phase* of a single
``run_refinement_preflight.py`` process invocation (``begin_phase()``). A
phase transition, an explicit ``invalidate()`` call (issued by the caller
immediately after any mutation to the target resource), or
``force_refresh=True`` on ``get_or_fetch()`` (explicit refresh / any
freshness-sensitive decision) always causes a fresh read via the caller's
own ``fetch_fn`` -- an ``EvidenceIndex`` snapshot is never treated as proof
of the current live GitHub state.

Out of scope (see Issue #2052 Out of Scope / Stop Conditions -- do not
extend this module to cover any of these without a new Issue):

- generic command-result caching / "do not run twice" suppression for
  tests, build, lint, mutable-worktree reads, retries after transient
  failure, GitHub mutation, review/CI/current-head checks, or any command
  whose input set cannot be fully bound to an ``EvidenceKey``.
- cross-session / cross-process persistent ledgers (owned by #2077, which
  closed such a ledger as ``not_planned`` -- this module never persists to
  disk and never survives past the end of a single process).
- ``#88``'s ``LOOP_STATE.vc_adjudication`` / current-head VC binding, or
  ``#1909``'s bounded SubAgent context bundle -- this module does not read,
  write, or otherwise participate in either.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

__all__ = [
    "RESOURCE_KIND_ISSUE_BODY",
    "RESOURCE_KIND_COMMENT",
    "EVIDENCE_INDEX_SCHEMA_VERSION",
    "EvidenceIndexError",
    "EvidenceKey",
    "FetchOutcome",
    "EvidenceIndex",
]

EVIDENCE_INDEX_SCHEMA_VERSION = "evidence_index/v1"

RESOURCE_KIND_ISSUE_BODY = "issue_body"
RESOURCE_KIND_COMMENT = "comment"

_VALID_RESOURCE_KINDS = frozenset({RESOURCE_KIND_ISSUE_BODY, RESOURCE_KIND_COMMENT})


class EvidenceIndexError(RuntimeError):
    """Raised for programmer-error misuse of this module's contract (e.g. an
    unsupported ``resource_kind``). Never raised for ordinary cache-miss /
    fallback situations -- those always fall through to ``fetch_fn``."""


def sha256_text(text: str) -> str:
    """SHA-256 hex digest of ``text`` encoded as UTF-8. Shared helper so the
    ``observed_content_sha256`` in ``EvidenceKey`` and the
    ``emitted_utf8_bytes`` byte count in ``EvidenceIndex`` are always
    computed from the exact same canonical encoding."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    """Deterministic JSON rendering used both to compute
    ``observed_content_sha256`` for non-string snapshots (e.g. the raw
    ``dict``/``list`` shapes ``_fetch_issue``/``_fetch_issue_comments``/
    ``_fetch_single_comment`` return) and to compute ``config_sha256`` for
    the cache's own config fingerprint."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _content_text(raw_snapshot: Any) -> str:
    """The canonical textual representation of a fetched raw snapshot used
    for both hashing (``observed_content_sha256``) and byte-count
    (``emitted_utf8_bytes``) purposes. Strings are used verbatim (this is
    the common case for a single Issue/comment body); anything else is
    rendered via ``_canonical_json`` so the hash is still a pure function of
    content."""
    if isinstance(raw_snapshot, str):
        return raw_snapshot
    return _canonical_json(raw_snapshot)


def _defensive_copy(value: Any) -> Any:
    """Issue #2052 fix_delta C: the single deep-copy boundary used both when
    storing a fetched ``raw_snapshot``/``projection`` into the cache and
    when returning a cached entry to a caller. Without this, a caller that
    mutates the object returned by ``get_or_fetch()`` (e.g.
    ``outcome.raw_snapshot["body"] = "..."``) would silently also mutate
    this cache's OWN internal state (they were the exact same object) --
    the next ``get_or_fetch()`` call for the same key would then return the
    caller's mutated content while ``EvidenceKey.observed_content_sha256``
    still reflects the ORIGINAL, pre-mutation content it was computed from,
    breaking the content<->SHA correspondence Issue #2052 AC3 depends on.
    ``copy.deepcopy`` is used unconditionally (rather than a shallow copy
    or a bespoke immutable wrapper) because ``raw_snapshot``/``projection``
    shapes are caller-defined (``dict``/``list``/``str`` for this module's
    own Issue/comment fetchers, but an arbitrary caller-supplied
    ``project_fn`` may return anything) -- a deep copy is the only
    shape-agnostic way to guarantee independence."""
    return copy.deepcopy(value)


@dataclass(frozen=True)
class EvidenceKey:
    """SHA-bound identity of a single observed Issue/comment snapshot,
    scoped to one phase. Two ``EvidenceKey`` instances with different
    ``observed_content_sha256`` are, by construction, never treated as the
    same evidence -- this is what makes a body/comment edit between two
    reads (Issue #2052 AC3) impossible to silently paper over."""

    repository: str
    resource_kind: str
    resource_id: str
    observed_content_sha256: str
    config_sha256: str

    def __post_init__(self) -> None:
        if self.resource_kind not in _VALID_RESOURCE_KINDS:
            raise EvidenceIndexError(
                f"unsupported resource_kind: {self.resource_kind!r} "
                f"(must be one of {sorted(_VALID_RESOURCE_KINDS)})"
            )
        if not self.repository:
            raise EvidenceIndexError("EvidenceKey.repository must be non-empty")
        if not self.resource_id:
            raise EvidenceIndexError("EvidenceKey.resource_id must be non-empty")

    def as_dict(self) -> dict:
        return {
            "repository": self.repository,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "observed_content_sha256": self.observed_content_sha256,
            "config_sha256": self.config_sha256,
        }

    def lookup_key(self) -> tuple:
        """The subset of the key identity used to look up a CANDIDATE cache
        slot (excludes ``observed_content_sha256``, which is compared
        separately as the staleness check in
        ``EvidenceIndex._entry_is_compatible``)."""
        return (self.repository, self.resource_kind, self.resource_id)


@dataclass
class FetchOutcome:
    """Result of ``EvidenceIndex.get_or_fetch()``."""

    raw_snapshot: Any
    projection: Any
    err: Optional[str]
    reused: bool
    evidence_key: Optional[EvidenceKey]

    @property
    def ok(self) -> bool:
        return self.err is None


@dataclass
class _CacheEntry:
    key: EvidenceKey
    raw_snapshot: Any
    projection: Any
    phase: str


class EvidenceIndex:
    """Phase-scoped, SHA-bound, in-process-only reuse cache.

    An ``EvidenceIndex`` instance lives for (at most) the lifetime of a
    single ``run_refinement_preflight.py`` process invocation. It is never
    serialized to disk and never shared across processes -- there is no
    "cached artifact" file format introduced by this module for its OWN
    storage (Issue #2052 In Scope bullet 3: reuse existing
    ``raw_issue_snapshot.json`` / ``planner_input.json`` /
    ``refinement_preflight_result_v1.json`` artifact shapes, do not invent a
    new bundle/receipt/ledger format). The "cached artifact
    missing/corrupt/stale/incompatible" fallback required by AC5 is
    satisfied at the level of THIS in-memory cache entry: a missing entry
    (never fetched yet in this phase), a structurally invalid entry (raised
    as ``EvidenceIndexError`` and caught defensively), a stale entry (a
    fresh fetch happened and its SHA no longer matches -- always beats the
    cache because ``get_or_fetch`` fetches fresh whenever no compatible
    cached entry exists) or an incompatible entry (``config_sha256``
    mismatch, e.g. after the caller changes ``config``) all fall through to
    ``fetch_fn`` identically -- there is exactly one fallback code path, not
    a family of special cases.
    """

    def __init__(self, config: Optional[dict] = None):
        self._config: dict = dict(config or {})
        self._config_sha256 = sha256_text(_canonical_json(self._config))
        self._phase: Optional[str] = None
        self._entries: dict[tuple, _CacheEntry] = {}

        # AC7 metrics -- observed only, never fabricated. Reset to zero on
        # every genuine phase TRANSITION by `begin_phase()` (fix_delta D) --
        # these are phase-LOCAL counters, not lifetime-of-instance totals.
        #
        # Precise measurement boundaries (fix_delta D docs the actual
        # semantics so the field NAME and what it measures do not drift
        # apart again):
        #   fetch_count: incremented exactly once per actual `fetch_fn()`
        #     invocation (a cache MISS that reached the caller's own
        #     read/fetch), regardless of whether that fetch succeeded or
        #     failed. Only counts calls made THROUGH `get_or_fetch()` --
        #     any read a consumer performs without going through this
        #     cache (e.g. the paginated `_fetch_issue_comments()` listing
        #     call in `run_refinement_preflight.py`, which this Issue's
        #     Allowed Paths deliberately do not route through this cache
        #     because it is called at most once per phase) is NOT counted
        #     here.
        #   emitted_utf8_bytes: the UTF-8 byte length of the canonical
        #     textual representation (`_content_text()`) of a successfully
        #     fetched `raw_snapshot`, added exactly once per successful
        #     cache-MISS fetch (never on a cache hit, and never for a
        #     failed fetch). This is a proxy for "how much snapshot content
        #     was newly read from GitHub and would otherwise need
        #     re-reading downstream" -- it is NOT a measurement of actual
        #     stdout/artifact emission size (a cache HIT still returns the
        #     full content to its caller; this counter does not grow for
        #     that reuse, by design, since AC6 defines "reduction" as this
        #     counter -- or `fetch_count` -- going down when the SAME
        #     content is referenced twice).
        #   snapshot_reuse_count: incremented exactly once per cache HIT
        #     (a `get_or_fetch()` call that returned an already-cached,
        #     phase-/config-compatible entry instead of calling
        #     `fetch_fn()`).
        #   duplicate_projection_count: incremented exactly once per cache
        #     HIT where the caller ALSO supplied a non-``None`` ``project_fn``
        #     -- i.e. a projection computation that was actually suppressed
        #     by the reuse. A cache hit for a call that never passes
        #     ``project_fn`` never ran a projection to begin with, so it
        #     must not increment this counter (fix_delta D).
        self._fetch_count = 0
        self._emitted_utf8_bytes = 0
        self._snapshot_reuse_count = 0
        self._duplicate_projection_count = 0

    # -- identity -----------------------------------------------------

    @property
    def config_sha256(self) -> str:
        return self._config_sha256

    @property
    def phase(self) -> Optional[str]:
        return self._phase

    # -- phase / freshness boundaries ----------------------------------

    def begin_phase(self, phase: str) -> None:
        """Declare the start of ``phase``. If ``phase`` differs from the
        currently active phase, ALL cached entries are discarded (Issue
        #2052 AC2: a phase transition always forces a fresh fetch for the
        next reference to any resource -- a snapshot cached in a previous
        phase must never leak into a new one). Calling ``begin_phase()``
        again with the SAME phase name is a no-op (re-entering the same
        phase does not itself invalidate anything).

        Issue #2052 fix_delta D: a phase transition ALSO resets the AC7
        metrics counters to phase-LOCAL values (zero). Without this, the
        counters are instance-wide cumulative totals, so
        ``context_budget_report.record_from_evidence_index()`` -- which
        records ``metrics_snapshot()`` as-is for whichever phase is active
        at call time -- would double count: e.g. phase A does one fetch
        (``fetch_count`` == 1, recorded for phase A), then phase B does one
        MORE fetch (``fetch_count`` == 2, recorded for phase B as if phase
        B alone had performed 2 fetches), and ``totals()`` would then sum
        1 + 2 == 3 for only 2 real fetches. Re-entering the SAME phase name
        does not reset counters (matches the pre-existing "no-op" cache
        semantics for entries -- ongoing accumulation within one phase is
        intentional, only a genuine transition to a DIFFERENT phase must
        start that new phase's own count from zero)."""
        if phase != self._phase:
            self._entries.clear()
            self._phase = phase
            self._fetch_count = 0
            self._emitted_utf8_bytes = 0
            self._snapshot_reuse_count = 0
            self._duplicate_projection_count = 0

    def invalidate(self, repository: str, resource_kind: str, resource_id: "str | int") -> None:
        """Explicitly drop any cached entry for this resource. Callers MUST
        invoke this immediately after any mutation to the resource (Issue
        edit, comment create/edit/delete) or whenever a freshness-sensitive
        decision requires bypassing reuse even within the same phase (Issue
        #2052 AC2/AC3). A no-op if no entry is cached."""
        self._entries.pop((repository, resource_kind, str(resource_id)), None)

    def clear(self) -> None:
        """Drop every cached entry without changing the declared phase."""
        self._entries.clear()

    # -- core operation --------------------------------------------------

    def get_or_fetch(
        self,
        *,
        repository: str,
        resource_kind: str,
        resource_id: "str | int",
        fetch_fn: Callable[[], "tuple[Any, str]"],
        project_fn: Optional[Callable[[Any], Any]] = None,
        force_refresh: bool = False,
    ) -> FetchOutcome:
        """Return the parse/projection result for ``resource_id``, reusing a
        same-phase cached entry when one is compatible and
        ``force_refresh`` is False.

        ``fetch_fn`` MUST return a ``(raw_snapshot, err)`` pair using the
        same convention as ``run_refinement_preflight.py``'s
        ``_fetch_issue`` / ``_fetch_issue_comments`` / ``_fetch_single_comment``:
        ``err`` truthy means the fetch failed and ``raw_snapshot`` is
        ``None`` (or otherwise unusable). ``project_fn``, if given, is
        applied to ``raw_snapshot`` to compute the cached projection;
        omitted, the projection IS the raw snapshot.

        Fallback semantics (Issue #2052 AC5): whenever no cached entry can
        be reused (missing / phase-incompatible / config-incompatible /
        ``force_refresh``), this calls ``fetch_fn()`` -- the caller's normal
        read path. If THAT fallback read itself fails (``err`` truthy), the
        failure is returned as-is (``FetchOutcome.err`` set, nothing is
        cached) -- it is never coerced into a successful result.
        """
        if resource_kind not in _VALID_RESOURCE_KINDS:
            raise EvidenceIndexError(
                f"unsupported resource_kind: {resource_kind!r} "
                f"(must be one of {sorted(_VALID_RESOURCE_KINDS)})"
            )
        resource_id_s = str(resource_id)
        lookup_key = (repository, resource_kind, resource_id_s)

        if force_refresh:
            # Issue #2052 fix_delta B: `force_refresh=True` is a caller's
            # explicit declaration that ANY previously-cached entry for this
            # resource must never be treated as current again -- drop it
            # BEFORE attempting the fresh fetch below, not after. Without
            # this, a fresh fetch that itself fails (transient failure)
            # would leave the OLD successful entry in place, and the very
            # next ordinary (non-`force_refresh`) call would silently reuse
            # that stale entry as if it were still valid (`reused=True,
            # err=None`) instead of being forced to fetch again.
            self._entries.pop(lookup_key, None)
        elif self._phase is not None:
            entry = self._entries.get(lookup_key)
            if entry is not None and self._entry_is_compatible(entry):
                self._snapshot_reuse_count += 1
                # Issue #2052 fix_delta D: `duplicate_projection_count` must
                # only count a suppressed re-projection -- i.e. a cache hit
                # where `project_fn` was actually supplied (and would
                # otherwise have re-run). A hit with no `project_fn` never
                # ran a projection in the first place, so there is nothing
                # to suppress.
                if project_fn is not None:
                    self._duplicate_projection_count += 1
                # Issue #2052 fix_delta C: return an independent deep copy,
                # never the exact object stored internally -- a caller that
                # mutates the returned `raw_snapshot`/`projection` (e.g.
                # `outcome.raw_snapshot["body"] = ...`) must never also
                # mutate this cache's OWN internal state (which would
                # silently desynchronize the cached content from
                # `entry.key.observed_content_sha256`, and would leak into
                # every subsequent cache hit for this same entry).
                return FetchOutcome(
                    raw_snapshot=_defensive_copy(entry.raw_snapshot),
                    projection=_defensive_copy(entry.projection),
                    err=None,
                    reused=True,
                    evidence_key=entry.key,
                )

        raw_snapshot, err = fetch_fn()
        self._fetch_count += 1
        if err:
            # AC5: the fallback read itself failed -- never treated as
            # success, and nothing is cached for this lookup_key.
            return FetchOutcome(
                raw_snapshot=raw_snapshot,
                projection=None,
                err=err,
                reused=False,
                evidence_key=None,
            )

        content_text = _content_text(raw_snapshot)
        self._emitted_utf8_bytes += len(content_text.encode("utf-8"))
        observed_sha = sha256_text(content_text)
        key = EvidenceKey(
            repository=repository,
            resource_kind=resource_kind,
            resource_id=resource_id_s,
            observed_content_sha256=observed_sha,
            config_sha256=self._config_sha256,
        )
        projection = project_fn(raw_snapshot) if project_fn is not None else raw_snapshot

        if self._phase is not None:
            # Issue #2052 fix_delta C: store an independent deep copy of
            # both `raw_snapshot` and `projection` -- the caller receiving
            # THIS call's return value below still gets the original,
            # freshly-fetched objects (mutating those has no effect on what
            # is stored here, since it is already a separate copy).
            self._entries[lookup_key] = _CacheEntry(
                key=key,
                raw_snapshot=_defensive_copy(raw_snapshot),
                projection=_defensive_copy(projection),
                phase=self._phase,
            )

        return FetchOutcome(
            raw_snapshot=raw_snapshot,
            projection=projection,
            err=None,
            reused=False,
            evidence_key=key,
        )

    def _entry_is_compatible(self, entry: _CacheEntry) -> bool:
        """Issue #2052 AC5: a structurally invalid or otherwise-incompatible
        cached entry (e.g. a fault-injected object that does not have the
        expected ``_CacheEntry`` shape) must be treated as "not compatible"
        -- falling through to a normal fresh fetch in ``get_or_fetch`` --
        never raise and crash the caller. Any failure to even inspect the
        candidate ``entry`` therefore counts as incompatible."""
        try:
            return entry.phase == self._phase and entry.key.config_sha256 == self._config_sha256
        except AttributeError:
            return False

    # -- metrics (Issue #2052 AC7: observed only) -------------------------

    def metrics_snapshot(self) -> dict:
        """Observed-only counters accumulated since the CURRENT phase began
        (fix_delta D: ``begin_phase()`` resets these to zero on every
        genuine phase transition -- they are phase-local, not
        lifetime-of-instance totals). See the field-by-field measurement
        boundaries documented next to their initialization in
        ``__init__``. Never includes token / model-turn figures -- those
        are not observable at this layer and ``context_budget_report.py``
        must not fabricate them either."""
        return {
            "fetch_count": self._fetch_count,
            "emitted_utf8_bytes": self._emitted_utf8_bytes,
            "snapshot_reuse_count": self._snapshot_reuse_count,
            "duplicate_projection_count": self._duplicate_projection_count,
        }
