#!/usr/bin/env python3
"""mrc_contract_parser.py — shared, section-bound Machine-Readable Contract parser.

SSOT for parsing the ``## Machine-Readable Contract`` (MRC) YAML block of an Issue
body. Consumed by both ``check_issue_contract.py`` (review-issue, C12) and
``validate_issue_body.py`` (create-issue, LP002) so the two never diverge
(Issue #1135 P0: parser differential).

Hardening (Issue #1135 P0 / P1b):
  - MRC parsing is bound to the ``## Machine-Readable Contract`` *section* only.
    A YAML fence appearing elsewhere in the body (e.g. under ``## Notes``) can NOT
    supply contract fields such as ``change_kind``.
  - Exactly one ``` ```yaml ``` ``` fence is required inside the section.
  - A duplicate-key-rejecting strict ``SafeLoader`` is used, so a last-wins
    override like ``change_kind: code`` / ``change_kind: docs`` is fail-closed.
  - The YAML root must be a mapping.

Any of the following is reported as ``ok == False`` (fail-closed); callers must
preserve their pre-existing behaviour (do NOT silently treat a parse failure as a
docs-only / exempt issue):
  - MRC section missing or appearing more than once
  - 0 or >1 YAML fences inside the MRC section
  - unterminated fence / YAML syntax error
  - duplicate mapping key (any key)
  - YAML root not a mapping

Pure-stdlib + PyYAML; no dependency on either skill's private helpers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import yaml

SECTION_NAME = "Machine-Readable Contract"

# Trace / classification keys whose duplication is explicitly disallowed by #1135.
# (The strict loader rejects ALL duplicate keys; this set is exported for callers
# that want to report which sensitive key was duplicated.)
SENSITIVE_DUPLICATE_KEYS = frozenset(
    {"change_kind", "requirement_id", "source_task_id", "product_spec_id"}
)

# ATX level-2 heading line, e.g. "## Machine-Readable Contract" (GFM allows 0-3
# leading spaces and an optional trailing run of #). The section name is matched
# case-sensitively against the canonical English heading.
_H2_RE = re.compile(r"^[ ]{0,3}##[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$")
# Any ATX heading (used as a section boundary).
_ANY_HEADING_RE = re.compile(r"^[ ]{0,3}#{1,6}[ \t]+\S")
# ```yaml ... ``` fence (opening fence must be ```yaml on its own line).
_YAML_FENCE_RE = re.compile(r"```yaml[ \t]*\n(.*?)\n```", re.DOTALL)


class _DuplicateKeyError(Exception):
    """Raised by the strict loader when a mapping key is declared twice."""

    def __init__(self, key: object):
        self.key = key
        super().__init__(f"duplicate mapping key: {key!r}")


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (YAML 1.2 key uniqueness)."""


def _strict_construct_mapping(loader: _StrictSafeLoader, node, deep: bool = False):
    loader.flatten_mapping(node)
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKeyError(key)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _strict_construct_mapping,
)


@dataclass(frozen=True)
class MRCParseResult:
    """Result of parsing the Machine-Readable Contract section.

    ok:        True iff a single, strictly-valid MRC mapping was parsed.
    data:      the parsed mapping (only when ok); never None when ok is True.
    reason:    machine-readable failure reason when not ok (one of REASONS), else "".
    duplicate_key: the offending key for reason == "duplicate_key", else None.
    """

    ok: bool
    data: Optional[dict] = None
    reason: str = ""
    duplicate_key: Optional[str] = None

    def get(self, key: str, default=None):
        """Convenience: read a field from the parsed mapping (None-safe)."""
        if not self.ok or not isinstance(self.data, dict):
            return default
        return self.data.get(key, default)


# machine-readable failure reasons
REASON_MISSING = "mrc_section_missing"
REASON_MULTIPLE_SECTIONS = "mrc_section_multiple"
REASON_NO_FENCE = "mrc_yaml_fence_missing"
REASON_MULTIPLE_FENCES = "mrc_yaml_fence_multiple"
REASON_YAML_ERROR = "mrc_yaml_syntax_error"
REASON_DUPLICATE_KEY = "duplicate_key"
REASON_ROOT_NOT_MAPPING = "mrc_root_not_mapping"


def _extract_mrc_sections(body: str) -> list[str]:
    """Return the body text of every ``## Machine-Readable Contract`` section.

    A section spans from just after its heading line to just before the next ATX
    heading (any level) or end of body. Returns one entry per matching heading so
    callers can fail-closed on duplicates.
    """
    lines = body.splitlines()
    sections: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _H2_RE.match(lines[i])
        if m and m.group("text").strip() == SECTION_NAME:
            j = i + 1
            buf: list[str] = []
            while j < n and not _ANY_HEADING_RE.match(lines[j]):
                buf.append(lines[j])
                j += 1
            sections.append("\n".join(buf))
            i = j
        else:
            i += 1
    return sections


def parse_machine_readable_contract(body: str) -> MRCParseResult:
    """Parse the MRC section of ``body`` strictly and section-bound.

    See module docstring for the fail-closed contract.
    """
    sections = _extract_mrc_sections(body)
    if len(sections) == 0:
        return MRCParseResult(ok=False, reason=REASON_MISSING)
    if len(sections) > 1:
        return MRCParseResult(ok=False, reason=REASON_MULTIPLE_SECTIONS)

    fences = _YAML_FENCE_RE.findall(sections[0])
    if len(fences) == 0:
        return MRCParseResult(ok=False, reason=REASON_NO_FENCE)
    if len(fences) > 1:
        return MRCParseResult(ok=False, reason=REASON_MULTIPLE_FENCES)

    try:
        data = yaml.load(fences[0], Loader=_StrictSafeLoader)
    except _DuplicateKeyError as exc:
        return MRCParseResult(
            ok=False, reason=REASON_DUPLICATE_KEY, duplicate_key=str(exc.key)
        )
    except (yaml.YAMLError, TypeError):
        # A complex YAML mapping key can be constructed as an unhashable
        # Python object (for example, a list).  Treat it like every other
        # malformed MRC input: callers receive a JSON-safe parse result,
        # never a loader exception.
        return MRCParseResult(ok=False, reason=REASON_YAML_ERROR)

    if not isinstance(data, dict):
        return MRCParseResult(ok=False, reason=REASON_ROOT_NOT_MAPPING)

    return MRCParseResult(ok=True, data=data)
