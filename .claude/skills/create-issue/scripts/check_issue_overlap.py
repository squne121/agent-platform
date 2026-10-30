#!/usr/bin/env python3
"""
check_issue_overlap.py

Issue 起票前の duplicate / overlap preflight helper。

title keyword search だけに依存せず、title / goal_ref / Allowed Paths / labels /
parent issue refs / dependency refs を使って既存 OPEN Issue / 兄弟 child との
overlap を機械判定し、`ISSUE_OVERLAP_PREFLIGHT_RESULT_V1` を返す。

## 設計境界（advisory / scope）

- 本 helper は **preflight advisory + structured evidence producer** であり、
  `create_issue_txn.py` の mutation hard gate ではない（hard gate 配線は #948
  follow-up と #946 の責務）。
- verdict の closed enum（`duplicate` / `overlap_requires_comment` /
  `safe_new_issue` / `ambiguous_requires_human`）は AC2 の正本。`policy_class`
  は workflow.md（#384/#386）の Scope Collision Classification（C0/C1/C2a/C2b/C3）
  への **mapping** であり、policy の再定義ではない。
- path normalization は #387（scope_collision_check の evidence producer, OPEN）
  と互換となる contract を目指す。#387 マージ後に共有正規化関数へ寄せる
  （follow-up）。現時点では本 module 内の `normalize_path` が正本。
- GitHub source（search / read-back / child plan）の失敗・partial・saturation は
  **fail-closed**（`ambiguous_requires_human`）に倒す。`safe_new_issue` は
  「source 成功かつ overlap 0」のときだけ返す。
- child overlap（`classify_child_overlap`）は **fixture-only な sibling path
  overlap checker** であり、#946 の `CHILD_MATERIALIZATION_PLAN_V2` を完全に
  consume する gate ではない。lookup 不完全 / ambiguous child は fail-closed。

policy_mapping:
- exact_duplicate -> duplicate
- C0  -> safe_new_issue
- C1  -> overlap_requires_comment
- C2a -> overlap_requires_comment
- C2b -> ambiguous_requires_human
- C3  -> ambiguous_requires_human
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# Issue #2783: shared no-path marker predicate (`scripts/agent-ops/`). Pure
# policy library only -- not a grammar library import.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENT_OPS_DIR = _REPO_ROOT / "scripts" / "agent-ops"
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))
from allowed_paths_policy import is_no_path_marker  # noqa: E402


SCHEMA_VERSION = "issue_overlap_preflight/v1"


# ============================================================
# decision enum（closed set, AC2 の正本）
# ============================================================

DUPLICATE = "duplicate"
OVERLAP_REQUIRES_COMMENT = "overlap_requires_comment"
SAFE_NEW_ISSUE = "safe_new_issue"
AMBIGUOUS_REQUIRES_HUMAN = "ambiguous_requires_human"

VERDICTS: frozenset = frozenset({DUPLICATE, OVERLAP_REQUIRES_COMMENT, SAFE_NEW_ISSUE, AMBIGUOUS_REQUIRES_HUMAN})

# ------------------------------------------------------------
# reason_code enum
# ------------------------------------------------------------

REASON_CODES: frozenset = frozenset(
    {
        "exact_title_duplicate",
        "goal_ref_duplicate",
        "allowed_paths_overlap",
        "parent_child_collision",
        "dependency_ambiguous",
        "source_failed",
        "readback_partial",
        "malformed_candidate_contract",
        "no_overlap",
        "successor_dependency_ordering",
    }
)

# ------------------------------------------------------------
# policy_class enum（workflow.md #384/#386 への mapping）
# ------------------------------------------------------------

POLICY_CLASSES: frozenset = frozenset({"C0", "C1", "C2a", "C2b", "C3", "unknown"})

POLICY_TO_DECISION = {
    "exact_duplicate": DUPLICATE,
    "C0": SAFE_NEW_ISSUE,
    "C1": OVERLAP_REQUIRES_COMMENT,
    "C2a": OVERLAP_REQUIRES_COMMENT,
    "C2b": AMBIGUOUS_REQUIRES_HUMAN,
    "C3": AMBIGUOUS_REQUIRES_HUMAN,
}

# ------------------------------------------------------------
# matched_field enum
# ------------------------------------------------------------

MATCHED_FIELDS: frozenset = frozenset({"title", "goal_ref", "allowed_paths", "labels", "parent_refs"})

# ------------------------------------------------------------
# source_status 値
# ------------------------------------------------------------

SOURCE_OK = "ok"
SOURCE_FAILED = "failed"
SOURCE_PARTIAL = "partial"
SOURCE_SATURATED = "saturated"
SOURCE_ABSENT = "absent"

# title 類似のしきい値（Jaccard）
_TITLE_DUP_THRESHOLD = 0.8  # ほぼ同一タイトル
_TITLE_RELATED_THRESHOLD = 0.5  # 関連していると見なす下限

# ============================================================
# Allowed Paths の正規化と overlap 判定（#387 互換 contract を志向）
# ============================================================

_BULLET_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")  # - + * および 1. 1) 番号付き
_CODE_RE = re.compile(r"^`([^`]+)`\s*(?:[（(].*)?$")
_PAREN_ANNOTATION_RE = re.compile(r"\s*[（(][^）)]*[）)]\s*$")
_MULTI_SLASH_RE = re.compile(r"/{2,}")


class PathScopeKind(str, Enum):
    """Allowed Paths の raw entry が表す範囲。

    末尾 slash / glob のように明示された記法だけを directory scope として
    扱う。拡張子の有無から directory を推測しないため、README や Makefile
    のような拡張子なしの exact file を broad path に誤分類しない。
    """

    EXACT = "EXACT"
    DIRECTORY = "DIRECTORY"
    RECURSIVE_GLOB = "RECURSIVE_GLOB"
    UNKNOWN = "UNKNOWN"


_KNOWN_EXTENSIONLESS_FILES = frozenset({"README", "LICENSE", "Dockerfile", "Makefile"})


def _clean_path_entry(entry: str) -> str:
    """正規化前に path entry の Markdown 装飾だけを外す。"""
    s = entry.strip()
    s = _BULLET_RE.sub("", s).strip()
    m = _CODE_RE.match(s)
    if m:
        s = m.group(1).strip()
    else:
        s = _PAREN_ANNOTATION_RE.sub("", s).strip()
    s = s.strip("`").strip()
    s = _PAREN_ANNOTATION_RE.sub("", s).strip()
    if s.startswith("./"):
        s = s[2:]
    return _MULTI_SLASH_RE.sub("/", s)


def classify_path_scope_kind(entry: str) -> PathScopeKind:
    """raw Allowed Paths entry から、推測ではなく記法で path kind を判定する。"""
    raw = _clean_path_entry(entry)
    if not raw:
        return PathScopeKind.UNKNOWN
    if raw.endswith("/**"):
        return PathScopeKind.RECURSIVE_GLOB
    # ``/*`` は一階層だけに一致し、``/**`` のような再帰的な directory
    # scope ではない。contract の closed enum に SINGLE_LEVEL_GLOB はないため、
    # UNKNOWN として保持し paths_conflict() で一階層 semantics を適用する。
    if raw.endswith("/*"):
        return PathScopeKind.UNKNOWN
    if raw.endswith("/"):
        return PathScopeKind.DIRECTORY
    basename = raw.rsplit("/", 1)[-1]
    if basename in _KNOWN_EXTENSIONLESS_FILES or "." in basename:
        return PathScopeKind.EXACT
    return PathScopeKind.UNKNOWN


def normalize_path(entry: str) -> str:
    """Allowed Paths の 1 エントリを bare path に正規化する。

    対応:
    - bullet marker（- + *）と番号付きリスト（1. / 1)）の除去
    - backtick 包み（`path`）と末尾の全角/半角括弧注釈の除去
    - 先頭の ``./``、連続スラッシュの圧縮、末尾スラッシュの除去
    - 末尾 glob ``/**`` / ``/*`` は basedir に畳む（prefix 比較で吸収するため）
    """
    s = _clean_path_entry(entry)
    # 末尾 glob を basedir に畳む（tests/x/** -> tests/x, tests/x/* -> tests/x）
    while s.endswith("/**") or s.endswith("/*"):
        s = s.rsplit("/", 1)[0]
    s = s.rstrip("/")
    return s


def normalize_paths(paths: Iterable[str]) -> Tuple[str, ...]:
    out: List[str] = []
    seen = set()
    for p in paths:
        n = normalize_path(p)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return tuple(out)


def _segments(path: str) -> List[str]:
    return [seg for seg in path.split("/") if seg]


def paths_conflict(a: str, b: str) -> bool:
    """2 つの正規化済みパスが同一スコープを指すか判定する。

    完全一致、または一方が他方の **segment 単位の** ディレクトリ接頭辞である
    場合に True を返す（例: ``tests/create-issue`` と
    ``tests/create-issue/test_x.py``）。
    単なる文字列接頭辞（``tests/create`` と ``tests/create-issue``）は
    segment 境界が異なるため overlap としない。
    """
    raw_a = _clean_path_entry(a)
    raw_b = _clean_path_entry(b)
    a = normalize_path(raw_a)
    b = normalize_path(raw_b)
    if not a or not b:
        return False

    def single_level_matches(pattern: str, other: str) -> bool:
        base = normalize_path(pattern)
        if not base or other == base or not other.startswith(f"{base}/"):
            return False
        return len(_segments(other)) == len(_segments(base)) + 1

    # Git-style ``/*`` does not cross a slash.  Keep this before the generic
    # prefix rule so ``tests/*`` and ``tests/unit/deep/test_x.py`` are not
    # treated as overlapping scopes.
    a_single = raw_a.endswith("/*") and not raw_a.endswith("/**")
    b_single = raw_b.endswith("/*") and not raw_b.endswith("/**")
    if a_single and b_single:
        return a == b
    if a_single:
        return single_level_matches(raw_a, b)
    if b_single:
        return single_level_matches(raw_b, a)
    if a == b:
        return True
    sa, sb = _segments(a), _segments(b)
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return longer[: len(shorter)] == shorter


def allowed_paths_overlap(a_paths: Iterable[str], b_paths: Iterable[str]) -> Tuple[str, ...]:
    """両 Allowed Paths 集合の overlap を表す正規化パスを返す（昇順・重複なし）。"""
    a_entries = tuple(a_paths)
    b_entries = tuple(b_paths)
    hits = set()
    for a_raw in a_entries:
        for b_raw in b_entries:
            if paths_conflict(a_raw, b_raw):
                a = normalize_path(a_raw)
                b = normalize_path(b_raw)
                hits.add(a if len(_segments(a)) >= len(_segments(b)) else b)
    return tuple(sorted(hits))


def same_path_set(a_paths: Iterable[str], b_paths: Iterable[str]) -> bool:
    a_norm = set(normalize_paths(a_paths))
    b_norm = set(normalize_paths(b_paths))
    return bool(a_norm) and a_norm == b_norm


# ============================================================
# title / goal_ref 類似
# ============================================================

_TOKEN_RE = re.compile(r"[0-9A-Za-z]+|[぀-ヿ一-鿿]")
_TITLE_PREFIX_RE = re.compile(r"^\s*(実装|implement|impl|docs?|fix|chore|test)\s*[:：]\s*", re.IGNORECASE)


def _title_tokens(title: str) -> Tuple[str, ...]:
    """title を正規化 token の **決定的（sorted）** tuple にする。

    frozenset の反復順は非決定的なため、CLI query 生成にも使える sorted tuple
    を返す（"deterministic" helper の契約を満たす）。
    """
    body = _TITLE_PREFIX_RE.sub("", title or "")
    toks = {t.lower() for t in _TOKEN_RE.findall(body)}
    return tuple(sorted(toks))


def title_similarity(a: str, b: str) -> float:
    ta, tb = set(_title_tokens(a)), set(_title_tokens(b))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _norm_goal(goal: str) -> str:
    return re.sub(r"\s+", " ", (goal or "").strip().lower())


# ============================================================
# source_status / 入力モデル
# ============================================================


@dataclass(frozen=True)
class SourceStatus:
    issue_search: str = SOURCE_OK
    issue_readback: str = SOURCE_OK
    child_plan: str = SOURCE_ABSENT

    @staticmethod
    def ok() -> "SourceStatus":
        return SourceStatus(SOURCE_OK, SOURCE_OK, SOURCE_ABSENT)

    def is_degraded(self) -> bool:
        return (
            self.issue_search in {SOURCE_FAILED, SOURCE_SATURATED}
            or self.issue_readback in {SOURCE_FAILED, SOURCE_PARTIAL}
            or self.child_plan == SOURCE_FAILED
        )

    def degraded_reason(self) -> str:
        if self.issue_readback in {SOURCE_FAILED, SOURCE_PARTIAL}:
            return "readback_partial"
        return "source_failed"

    def to_dict(self) -> dict:
        return {
            "issue_search": self.issue_search,
            "issue_readback": self.issue_readback,
            "child_plan": self.child_plan,
        }


@dataclass(frozen=True)
class IssueScope:
    title: str
    allowed_paths: Tuple[str, ...] = ()
    number: Optional[int] = None
    url: str = ""
    goal: str = ""  # goal_ref
    labels: Tuple[str, ...] = ()
    parent_refs: Tuple[str, ...] = ()
    depends_on: Tuple[str, ...] = ()  # `Depends on #N` / native dependency
    body: Optional[str] = None
    state: str = "OPEN"
    search_hit: bool = False  # full-text search 由来 → read-back 必須

    def effective_allowed_paths(self) -> Tuple[str, ...]:
        if self.allowed_paths:
            return normalize_paths(self.allowed_paths)
        if self.body:
            return normalize_paths(extract_allowed_paths(self.body))
        return ()

    @staticmethod
    def from_dict(d: dict) -> "IssueScope":
        return IssueScope(
            title=str(d.get("title", "")),
            allowed_paths=tuple(d.get("allowed_paths", []) or []),
            number=d.get("number"),
            url=str(d.get("url", "") or ""),
            goal=str(d.get("goal", d.get("goal_ref", "")) or ""),
            labels=tuple(d.get("labels", []) or []),
            parent_refs=tuple(str(x) for x in (d.get("parent_refs", []) or [])),
            depends_on=tuple(str(x) for x in (d.get("depends_on", []) or [])),
            body=d.get("body"),
            state=str(d.get("state", "OPEN") or "OPEN"),
            search_hit=bool(d.get("search_hit", False)),
        )


def extract_allowed_paths(body: str) -> List[str]:
    """Issue body の ``## Allowed Paths`` セクションを read-back する。"""
    if not body:
        return []
    match = re.search(
        r"^##\s+Allowed Paths\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []
    paths: List[str] = []
    for entry in extract_allowed_path_entries(body):
        path = normalize_path(entry)
        if path:
            paths.append(path)
    return paths


def extract_allowed_path_entries(body: str) -> List[str]:
    """Allowed Paths の装飾除去前 entry を返す（path kind 保存用）。"""
    if not body:
        return []
    match = re.search(
        r"^##\s+Allowed Paths\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []
    entries: List[str] = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # PR #2791 review fix_delta (Issue #2783): a bare code-fence
        # delimiter line (an opening ```` ``` ```` / ```` ```text ```` with
        # an optional info string, or a bare closing ```` ``` ````) is
        # Markdown *structure* used only for visual wrapping here, never a
        # path/marker entry on its own -- skip it outright so it never
        # becomes a spurious candidate path. Lines *inside* the fence are
        # still processed as ordinary entries below.
        if stripped.startswith("```"):
            continue
        # PR #2791 review fix_delta (Issue #2783): judge no-path marker
        # BEFORE normalize_path() with the FIXED order: strip the shared
        # bullet/numbered-list wrapper (`_BULLET_RE`, which also strips
        # numbered lists like `1.` / `1)` that `allowed_paths_policy`'s own
        # bullet-only `_strip_wrapper()` deliberately does not) -> marker
        # exact-match -> ordinary normalization. A marker line contributes
        # 0 paths / entries.
        unwrapped = _BULLET_RE.sub("", stripped).strip()
        if is_no_path_marker(unwrapped):
            continue
        if normalize_path(stripped):
            entries.append(stripped)
    return entries


def _ref_key(ref: str) -> str:
    """`#948` / `948` / URL を `948` に正規化する。"""
    s = str(ref).strip()
    m = re.search(r"(\d+)\s*$", s)
    return m.group(1) if m else s.lower()


# ============================================================
# 判定結果モデル
# ============================================================


@dataclass(frozen=True)
class DependencyRelation:
    relation: str = "none"  # predecessor | successor | none | ambiguous
    evidence: str = "none"  # native_dependency | line_anchored_depends_on | parent_work_ordering | none

    def to_dict(self) -> dict:
        return {"relation": self.relation, "evidence": self.evidence}


@dataclass(frozen=True)
class CandidateEvidence:
    issue_number: Optional[int]
    url: str
    title: str
    matched_fields: Tuple[str, ...]
    labels: Tuple[str, ...]
    parent_refs: Tuple[str, ...]
    allowed_paths: Tuple[str, ...]
    overlapping_paths: Tuple[str, ...]
    dependency_relation: DependencyRelation

    def to_dict(self) -> dict:
        return {
            "issue_number": self.issue_number,
            "url": self.url,
            "title": self.title,
            "matched_fields": list(self.matched_fields),
            "labels": list(self.labels),
            "parent_refs": list(self.parent_refs),
            "allowed_paths": list(self.allowed_paths),
            "overlapping_paths": list(self.overlapping_paths),
            "dependency_relation": self.dependency_relation.to_dict(),
        }


@dataclass(frozen=True)
class OverlapResult:
    verdict: str
    reason_code: str = "no_overlap"
    policy_class: str = "C0"
    reason: str = ""
    source_status: SourceStatus = field(default_factory=SourceStatus.ok)
    target: Optional["IssueScope"] = None
    candidates: Tuple[CandidateEvidence, ...] = ()
    comment_template: Optional[str] = None
    excluded_false_positives: Tuple[int, ...] = ()

    # --- 後方互換 derived プロパティ ---
    @property
    def decision(self) -> str:
        return self.verdict

    @property
    def matched_issues(self) -> Tuple[int, ...]:
        return tuple(c.issue_number for c in self.candidates if c.issue_number is not None)

    @property
    def overlapping_paths(self) -> Tuple[str, ...]:
        hits: set = set()
        for c in self.candidates:
            hits.update(c.overlapping_paths)
        return tuple(sorted(hits))

    def to_dict(self) -> dict:
        assert self.verdict in VERDICTS, f"invalid verdict: {self.verdict}"
        assert self.reason_code in REASON_CODES, f"invalid reason_code: {self.reason_code}"
        assert self.policy_class in POLICY_CLASSES, f"invalid policy_class: {self.policy_class}"
        tgt = self.target
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "issue_overlap",
            "decision": self.verdict,
            "reason_code": self.reason_code,
            "policy_class": self.policy_class,
            "source_status": self.source_status.to_dict(),
            "target": {
                "title": tgt.title if tgt else "",
                "goal_ref": tgt.goal if tgt else "",
                "labels": list(tgt.labels) if tgt else [],
                "parent_refs": list(tgt.parent_refs) if tgt else [],
                "allowed_paths": list(tgt.effective_allowed_paths()) if tgt else [],
            },
            "candidates": [c.to_dict() for c in self.candidates],
            "matched_issues": list(self.matched_issues),
            "overlapping_paths": list(self.overlapping_paths),
            "comment_template": self.comment_template,
            "excluded_false_positives": list(self.excluded_false_positives),
        }


@dataclass(frozen=True)
class ChildOverlapPair:
    a_index: int
    b_index: int
    a_title: str
    b_title: str
    overlapping_paths: Tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "a_index": self.a_index,
            "b_index": self.b_index,
            "a_title": self.a_title,
            "b_title": self.b_title,
            "overlapping_paths": list(self.overlapping_paths),
        }


@dataclass(frozen=True)
class ChildOverlapResult:
    verdict: str
    reason_code: str = "no_overlap"
    policy_class: str = "C0"
    reason: str = ""
    overlapping_pairs: Tuple[ChildOverlapPair, ...] = ()
    comment_template: Optional[str] = None
    child_plan_status: str = SOURCE_OK

    def to_dict(self) -> dict:
        assert self.verdict in VERDICTS, f"invalid verdict: {self.verdict}"
        assert self.reason_code in REASON_CODES
        assert self.policy_class in POLICY_CLASSES
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "child_overlap",
            "decision": self.verdict,
            "reason_code": self.reason_code,
            "policy_class": self.policy_class,
            "child_plan_status": self.child_plan_status,
            "overlapping_pairs": [p.to_dict() for p in self.overlapping_pairs],
            "comment_template": self.comment_template,
        }


# ============================================================
# 判定ロジック（pure function）
# ============================================================


def _dependency_relation(current: IssueScope, cand: IssueScope) -> DependencyRelation:
    """current と candidate の dependency 関係を判定する。"""
    cur_dep = {_ref_key(r) for r in current.depends_on}
    cand_dep = {_ref_key(r) for r in cand.depends_on}
    cand_key = _ref_key(cand.number) if cand.number is not None else None
    cur_key = _ref_key(current.number) if current.number is not None else None

    if cand_key and cand_key in cur_dep:
        return DependencyRelation("predecessor", "line_anchored_depends_on")
    if cur_key and cur_key in cand_dep:
        return DependencyRelation("successor", "line_anchored_depends_on")
    # 共有 parent による work ordering
    cur_parents = {_ref_key(r) for r in current.parent_refs}
    cand_parents = {_ref_key(r) for r in cand.parent_refs}
    if cur_parents and cur_parents & cand_parents:
        return DependencyRelation("none", "parent_work_ordering")
    return DependencyRelation("none", "none")


def _evaluate_candidate(current: IssueScope, cand: IssueScope) -> Tuple[Optional[CandidateEvidence], List[str]]:
    """1 candidate を評価し evidence を返す。matched_fields を構築する。"""
    cur_paths = current.effective_allowed_paths()
    cand_paths = cand.effective_allowed_paths()
    overlap = allowed_paths_overlap(cur_paths, cand_paths)
    tsim = title_similarity(current.title, cand.title)

    matched: List[str] = []
    if tsim >= _TITLE_DUP_THRESHOLD:
        matched.append("title")
    if current.goal and cand.goal and _norm_goal(current.goal) == _norm_goal(cand.goal):
        matched.append("goal_ref")
    if overlap:
        matched.append("allowed_paths")
    cur_labels = {lbl.lower() for lbl in current.labels}
    cand_labels = {lbl.lower() for lbl in cand.labels}
    # routing 用 generic label を除いた意味的 label の共有のみ matched 扱い
    generic = {"enhancement", "bug", "workflow", "phase/implementation", "agent/implementer", "documentation", "docs"}
    shared_labels = (cur_labels & cand_labels) - generic
    if shared_labels:
        matched.append("labels")
    dep = _dependency_relation(current, cand)
    cur_parents = {_ref_key(r) for r in current.parent_refs}
    cand_key = _ref_key(cand.number) if cand.number is not None else None
    shared_parent = bool(cur_parents & {_ref_key(r) for r in cand.parent_refs})
    parent_child = bool((cand_key and cand_key in cur_parents) or (cur_parents and shared_parent))
    if parent_child:
        matched.append("parent_refs")

    evidence = CandidateEvidence(
        issue_number=cand.number,
        url=cand.url,
        title=cand.title,
        matched_fields=tuple(matched),
        labels=tuple(cand.labels),
        parent_refs=tuple(cand.parent_refs),
        allowed_paths=cand_paths,
        overlapping_paths=overlap,
        dependency_relation=dep,
    )
    return evidence, matched


def classify_overlap(
    current: IssueScope,
    candidates: Sequence[IssueScope],
    source_status: Optional[SourceStatus] = None,
) -> OverlapResult:
    """起票候補と既存 Issue 群の overlap を closed enum + structured evidence で判定する。

    precedence: source degraded(fail-closed) > exact_duplicate > parent_child_collision
    > dependency(C2a/C2b) > allowed_paths_overlap(C1) > title/goal disjoint(C3)
    > safe_new_issue(C0)。
    """
    source_status = source_status or SourceStatus.ok()

    # --- fail-closed: GitHub source が degraded なら ambiguous へ倒す ---
    if source_status.is_degraded():
        return OverlapResult(
            verdict=AMBIGUOUS_REQUIRES_HUMAN,
            reason_code=source_status.degraded_reason(),
            policy_class="unknown",
            reason="GitHub source（search / read-back）が degraded。safe 判定不可。",
            source_status=source_status,
            target=current,
        )

    cur_paths = current.effective_allowed_paths()

    duplicate: List[CandidateEvidence] = []
    parent_collision: List[CandidateEvidence] = []
    dep_open: List[CandidateEvidence] = []  # predecessor open -> C2b
    dep_closed: List[CandidateEvidence] = []  # predecessor closed -> C2a
    overlap_partial: List[CandidateEvidence] = []
    overlap_partial_successor_dependency = False  # successor + shared parent -> C2a (#1619)
    title_goal_disjoint: List[CandidateEvidence] = []
    excluded: List[int] = []

    for cand in candidates:
        if str(cand.state).upper() != "OPEN":
            continue
        ev, matched = _evaluate_candidate(current, cand)
        has_path = "allowed_paths" in matched
        has_title = "title" in matched
        has_goal = "goal_ref" in matched
        has_parent = "parent_refs" in matched

        # --- false positive 除外（read-back） ---
        if cand.search_hit and not has_path and not has_title and not has_goal:
            if cand.number is not None:
                excluded.append(cand.number)
            continue

        # precedence（candidate 単位）:
        # title+goal 同時一致 > 明示 dependency > parent/child collision
        # > 同一 path 集合(duplicate) > 部分 overlap(C1) > title/goal disjoint(C3)
        if has_title and has_goal:
            duplicate.append(ev)
            continue
        if has_path:
            rel = ev.dependency_relation.relation
            if rel == "predecessor":
                # 明示依存がある = 意図的な直列化であり duplicate ではない
                if str(cand.state).upper() == "OPEN":
                    dep_open.append(ev)
                else:
                    dep_closed.append(ev)
                continue
            elif rel == "successor":
                # candidate が current に依存している（current を止めていない）。
                # shared parent_refs があっても無条件で parent_collision(C3) に
                # 倒さず、安全な直列化順序として overlap_partial(C2a) へ倒す（#1619）。
                overlap_partial.append(ev)
                if has_parent:
                    overlap_partial_successor_dependency = True
                continue
            if has_parent:
                parent_collision.append(ev)
                continue
            if same_path_set(cur_paths, ev.allowed_paths):
                duplicate.append(ev)
                continue
            overlap_partial.append(ev)
            continue
        if has_title or has_goal:
            title_goal_disjoint.append(ev)

    # precedence に従って decision / policy_class / reason_code を決める
    if duplicate:
        return OverlapResult(
            verdict=DUPLICATE,
            reason_code="exact_title_duplicate"
            if any("title" in c.matched_fields for c in duplicate)
            else "goal_ref_duplicate",
            policy_class="unknown",
            reason="既存 OPEN Issue と Allowed Paths 同一集合、または title+goal 同時一致",
            source_status=source_status,
            target=current,
            candidates=tuple(duplicate),
            excluded_false_positives=tuple(sorted(excluded)),
        )
    if parent_collision:
        return OverlapResult(
            verdict=AMBIGUOUS_REQUIRES_HUMAN,
            reason_code="parent_child_collision",
            policy_class="C3",
            reason="parent/child 関係で Allowed Paths が衝突",
            source_status=source_status,
            target=current,
            candidates=tuple(parent_collision),
            comment_template=_build_comment_template(parent_collision, "C3"),
            excluded_false_positives=tuple(sorted(excluded)),
        )
    if dep_open:
        return OverlapResult(
            verdict=AMBIGUOUS_REQUIRES_HUMAN,
            reason_code="dependency_ambiguous",
            policy_class="C2b",
            reason="predecessor が open のまま依存順未解決（C2b）",
            source_status=source_status,
            target=current,
            candidates=tuple(dep_open),
            comment_template=_build_comment_template(dep_open, "C2b"),
            excluded_false_positives=tuple(sorted(excluded)),
        )
    if dep_closed:
        return OverlapResult(
            verdict=OVERLAP_REQUIRES_COMMENT,
            reason_code="allowed_paths_overlap",
            policy_class="C2a",
            reason="predecessor が close 済みで直列化可能（C2a）",
            source_status=source_status,
            target=current,
            candidates=tuple(dep_closed),
            comment_template=_build_comment_template(dep_closed, "C2a"),
            excluded_false_positives=tuple(sorted(excluded)),
        )
    if overlap_partial:
        if overlap_partial_successor_dependency:
            partial_reason_code = "successor_dependency_ordering"
            partial_policy_class = "C2a"
            partial_reason = (
                "candidate が current に対して successor dependency（明示依存）を"
                "持つため、shared parent_refs があっても安全な直列化順序として扱う（C2a）"
            )
        else:
            partial_reason_code = "allowed_paths_overlap"
            partial_policy_class = "C1"
            partial_reason = "既存 OPEN Issue と Allowed Paths が部分的に重複（C1 benign overlap）"
        return OverlapResult(
            verdict=OVERLAP_REQUIRES_COMMENT,
            reason_code=partial_reason_code,
            policy_class=partial_policy_class,
            reason=partial_reason,
            source_status=source_status,
            target=current,
            candidates=tuple(overlap_partial),
            comment_template=_build_comment_template(overlap_partial, partial_policy_class),
            excluded_false_positives=tuple(sorted(excluded)),
        )
    if title_goal_disjoint:
        return OverlapResult(
            verdict=AMBIGUOUS_REQUIRES_HUMAN,
            reason_code="exact_title_duplicate"
            if any("title" in c.matched_fields for c in title_goal_disjoint)
            else "goal_ref_duplicate",
            policy_class="C3",
            reason="title / goal が既存 Issue とほぼ一致するが Allowed Paths が disjoint（C3）",
            source_status=source_status,
            target=current,
            candidates=tuple(title_goal_disjoint),
            comment_template=_build_comment_template(title_goal_disjoint, "C3"),
            excluded_false_positives=tuple(sorted(excluded)),
        )
    return OverlapResult(
        verdict=SAFE_NEW_ISSUE,
        reason_code="no_overlap",
        policy_class="C0",
        reason="overlap なし（新規起票で安全, C0）",
        source_status=source_status,
        target=current,
        excluded_false_positives=tuple(sorted(excluded)),
    )


def _build_comment_template(cands: Sequence[CandidateEvidence], policy_class: str) -> str:
    refs = ", ".join(f"#{c.issue_number}" for c in cands if c.issue_number is not None)
    paths: set = set()
    for c in cands:
        paths.update(c.overlapping_paths)
    path_list = ", ".join(sorted(paths)) or "(none)"
    return (
        f"Scope Collision ({policy_class}): related_issue={refs or '(none)'}; "
        f"overlapping_paths={path_list}. "
        "non_conflict_reason / edit_intent / dependency ordering を記録すること。"
    )


def classify_child_overlap(
    children: Sequence[IssueScope],
    *,
    lookup_complete: bool = True,
    ambiguous_child: bool = False,
    child_plan_status: str = SOURCE_OK,
) -> ChildOverlapResult:
    """delivery-rollup parent の兄弟 child 同士の Allowed Paths overlap を検査する。

    **fixture-only な sibling path overlap checker** であり、#946 の child
    materialization gate ではない。lookup 不完全 / ambiguous child /
    child_plan source 失敗は fail-closed（ambiguous_requires_human）。
    """
    if (not lookup_complete) or ambiguous_child or child_plan_status == SOURCE_FAILED:
        return ChildOverlapResult(
            verdict=AMBIGUOUS_REQUIRES_HUMAN,
            reason_code="readback_partial" if not lookup_complete else "dependency_ambiguous",
            policy_class="unknown",
            reason="child plan lookup 不完全 / ambiguous child / source 失敗 → fail-closed",
            child_plan_status=child_plan_status if child_plan_status != SOURCE_OK else SOURCE_PARTIAL,
        )

    pairs: List[ChildOverlapPair] = []
    has_identical = False
    for i in range(len(children)):
        for j in range(i + 1, len(children)):
            a, b = children[i], children[j]
            a_paths = a.effective_allowed_paths()
            b_paths = b.effective_allowed_paths()
            overlap = allowed_paths_overlap(a_paths, b_paths)
            if overlap:
                if same_path_set(a_paths, b_paths):
                    has_identical = True
                pairs.append(ChildOverlapPair(i, j, a.title, b.title, overlap))

    if not pairs:
        return ChildOverlapResult(
            verdict=SAFE_NEW_ISSUE,
            reason_code="no_overlap",
            policy_class="C0",
            reason="child 同士の Allowed Paths overlap なし",
            child_plan_status=child_plan_status,
        )
    verdict = DUPLICATE if has_identical else OVERLAP_REQUIRES_COMMENT
    return ChildOverlapResult(
        verdict=verdict,
        reason_code="allowed_paths_overlap",
        policy_class="unknown" if has_identical else "C1",
        reason="child 同士で Allowed Paths が重複",
        overlapping_pairs=tuple(pairs),
        comment_template=(
            "Sibling child overlap: "
            + "; ".join(f"[{p.a_index}x{p.b_index}] {', '.join(p.overlapping_paths)}" for p in pairs)
        ),
        child_plan_status=child_plan_status,
    )


# ============================================================
# GitHub adapter（CLI 層、非 dry-run 時のみ。fail-closed source_status を返す）
# ============================================================


def gh_search_candidates(repo: str, query_tokens: Sequence[str]) -> Tuple[List[IssueScope], SourceStatus]:
    """OPEN Issue を全ページ取得し、body を含む候補を返す。

    GitHub search の ``--limit`` は上限到達時に完全性を証明できず、
    saturation を発生させる。Issue 起票時の overlap preflight は
    ``gh api --paginate --slurp`` で REST の全ページを取得するため、
    通常の OPEN Issue 件数が従来の既定上限を超えても source を degraded に
    しない。REST response に body と labels が含まれるので、個別 read-back は
    不要である。

    ``query_tokens`` は呼び出し契約との互換性のため受け取る。候補の絞り込みは
    classifier が title / goal / Allowed Paths を合わせて決定するため、ここでは
    行わず recall を落とさない。
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/issues?state=open&per_page=100",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        pages = json.loads(proc.stdout or "[]")
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise TypeError("gh api --slurp response must be a list of pages")
        issues = [issue for page in pages for issue in page]
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError):
        return [], SourceStatus(SOURCE_FAILED, SOURCE_OK, SOURCE_ABSENT)

    candidates: List[IssueScope] = []
    for issue in issues:
        # REST /issues は Pull Request も返す。Issue preflight の対象外にする。
        if not isinstance(issue, dict) or "pull_request" in issue:
            continue
        try:
            labels = issue.get("labels", []) or []
            if not isinstance(labels, list):
                raise TypeError("issue labels must be a list")
            label_names = tuple(label.get("name", "") if isinstance(label, dict) else str(label) for label in labels)
            number = issue["number"]
            title = issue["title"]
            body = issue.get("body")
        except (KeyError, TypeError):
            return [], SourceStatus(SOURCE_OK, SOURCE_PARTIAL, SOURCE_ABSENT)
        candidates.append(
            IssueScope(
                title=str(title),
                number=int(number),
                url=str(issue.get("html_url", issue.get("url", "")) or ""),
                body=body if isinstance(body, str) else None,
                labels=label_names,
                state=str(issue.get("state", "OPEN") or "OPEN"),
                search_hit=True,
            )
        )
    return candidates, SourceStatus.ok()


# ============================================================
# CLI
# ============================================================


def _read_paths_file(path: str) -> List[str]:
    out: List[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _load_candidates(path: str) -> Tuple[List[IssueScope], SourceStatus]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    status = SourceStatus.ok()
    if isinstance(data, dict):
        ss = data.get("source_status")
        if isinstance(ss, dict):
            status = SourceStatus(
                ss.get("issue_search", SOURCE_OK),
                ss.get("issue_readback", SOURCE_OK),
                ss.get("child_plan", SOURCE_ABSENT),
            )
        data = data.get("candidates") or []
    return [IssueScope.from_dict(d) for d in data], status


def _load_children(path: str) -> Tuple[List[IssueScope], bool, bool, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    lookup_complete = True
    ambiguous = False
    child_plan_status = SOURCE_OK
    if isinstance(data, dict):
        lookup_complete = bool(data.get("lookup_complete", True))
        ambiguous = bool(data.get("ambiguous_child", False))
        child_plan_status = str(data.get("child_plan_status", SOURCE_OK))
        data = data.get("children") or []
    return (
        [IssueScope.from_dict(d) for d in data],
        lookup_complete,
        ambiguous,
        child_plan_status,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="check_issue_overlap.py",
        description="Issue 起票前の duplicate / overlap preflight",
    )
    p.add_argument("--repo", help="owner/name（非 dry-run の full-text search に使用）")
    p.add_argument("--title", required=True, help="起票予定 Issue の title")
    p.add_argument("--goal", default="", help="goal_ref / Desired Destination")
    p.add_argument("--allowed-paths-file", help="起票予定 Issue の Allowed Paths（1 行 1 パス）")
    p.add_argument("--label", action="append", default=[], help="label（複数可）")
    p.add_argument("--parent-ref", action="append", default=[], help="parent issue ref（複数可）")
    p.add_argument("--depends-on", action="append", default=[], help="Depends on ref（複数可）")
    p.add_argument("--candidates-file", help="既存 Issue 候補の JSON（offline 判定 / read-back 用）")
    p.add_argument("--children-file", help="兄弟 child spec の JSON（delivery-rollup child overlap 判定用）")
    p.add_argument("--dry-run", action="store_true", help="GitHub へアクセスしない（候補は --candidates-file のみ）")
    p.add_argument(
        "--fail-on-unsafe",
        action="store_true",
        help="decision が safe_new_issue 以外なら exit code 3 を返す（shell gating 用）",
    )
    return p


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    allowed_paths: List[str] = []
    if args.allowed_paths_file:
        allowed_paths = _read_paths_file(args.allowed_paths_file)

    # --- child overlap モード ---
    if args.children_file:
        children, lookup_complete, ambiguous, child_status = _load_children(args.children_file)
        child_result = classify_child_overlap(
            children,
            lookup_complete=lookup_complete,
            ambiguous_child=ambiguous,
            child_plan_status=child_status,
        )
        print(json.dumps(child_result.to_dict(), ensure_ascii=False, indent=2))
        if args.fail_on_unsafe and child_result.verdict != SAFE_NEW_ISSUE:
            return 3
        return 0

    current = IssueScope(
        title=args.title,
        allowed_paths=tuple(allowed_paths),
        goal=args.goal,
        labels=tuple(args.label),
        parent_refs=tuple(args.parent_ref),
        depends_on=tuple(args.depends_on),
    )

    candidates: List[IssueScope] = []
    source_status = SourceStatus.ok()
    if args.candidates_file:
        candidates, source_status = _load_candidates(args.candidates_file)
    elif not args.dry_run:
        if not args.repo:
            print(
                json.dumps({"error": "--repo is required for online search"}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 2
        candidates, source_status = gh_search_candidates(args.repo, _title_tokens(args.title))

    result = classify_overlap(current, candidates, source_status)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if args.fail_on_unsafe and result.verdict != SAFE_NEW_ISSUE:
        return 3
    return 0


def main() -> None:  # pragma: no cover - thin entrypoint
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
