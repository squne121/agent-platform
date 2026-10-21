#!/usr/bin/env python3
"""Shared VC grammar helpers for AC markers and preflight-scope parsing.

LOOP_PROTOCOL canonical lint rules (not GFM).  Command lines inside
```bash fences must start with $  (dollar-space).  Inline
backticks, compound shell operators, and unlabeled fences are rejected.

Also provides:
  - baseline-expect annotation parser (Issue #889)
  - vc-role annotation parser (Issue #889)
  - parse_verification_commands_section() — unified VC section parser (Issue #993)
  - vc-regex-intent annotation parser (Issue #589 / moved here in #2788)
  - detect_compound_command() — shlex-based quote-aware compound shell
    operator detector (moved here in #2788 AC9; this is the SAME primitive
    `baseline_vc_preflight.py`'s normal execution already used for its own
    compound-command classification, now also the authority
    `parse_verification_commands_section()` below delegates to instead of
    an independent, non-quote-aware regex).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Optional

# Valid preflight-scope marker values recognized by both validator and preflight runtime.
VALID_PRE_FLIGHT_SCOPE_VALUES = ("pr_review_only", "runtime_only")

# Valid baseline-expect annotation values (Issue #889).
# "pass"     - VC expected to exit 0 at baseline (promotion/refactor issue)
# "fail"     - VC expected to exit non-0 at baseline (new implementation)
# "deferred" - VC baseline run is deferred (equiv. to pr_review_only scope)
VALID_BASELINE_EXPECT_VALUES = ("pass", "fail", "deferred")

# Marker comment pattern (single-line comment prefix only).
_AC_MARKER_PATTERN = re.compile(r"^\s*#\s*AC(\d+)\b(.*)$")
_PRE_FLIGHT_SCOPE_PATTERN = re.compile(r"^\s*#\s*preflight-scope:\s*(.*?)\s*$")
_BASELINE_EXPECT_PATTERN = re.compile(r"^\s*#\s*baseline-expect:\s*(.*?)\s*$")
_VC_ROLE_PATTERN = re.compile(r"^\s*#\s*vc-role:\s*(.*?)\s*$")

# Grouped AC marker: "# AC1, AC2" or "# AC2, AC3, AC4" (comma-separated, no suffix)
_GROUPED_AC_MARKER_PATTERN = re.compile(
    r"^\s*#\s*(AC\d+(?:\s*,\s*AC\d+)+)\s*$"
)


def parse_ac_marker_line(line: str) -> tuple[str | None, bool]:
    """Parse standalone AC marker comment line.

    Args:
        line: A single source line.

    Returns:
        tuple[str|None, bool]: (marker_label, is_valid)

        marker_label = 'AC1', 'AC2', ... when line is an AC marker comment.
        is_valid = True only for bare '# AC1' / '# AC1   ' style forms.

    Notes:
        Any suffix after the AC number (": text", "：text", "- text", "— text")
        is treated as invalid, so `_extract_vc_ac_numbers` in strict mode will not
        treat it as a match.
    """

    match = _AC_MARKER_PATTERN.match(line)
    if not match:
        return None, False

    label = f"AC{match.group(1)}"
    suffix = match.group(2).strip()
    return (label, not bool(suffix))


def parse_preflight_scope_marker_line(line: str) -> tuple[str | None, bool]:
    """Parse standalone preflight-scope marker line.

    Returns:
        tuple[str|None, bool]: (scope_value, is_known_value)

        scope_value is extracted raw value (without surrounding whitespace) when the
        line is a preflight-scope marker, or None otherwise.
        is_known_value is True when scope_value is one of
        VALID_PRE_FLIGHT_SCOPE_VALUES.

    Empty value and whitespace-only values are treated as markers but not known.
    """

    match = _PRE_FLIGHT_SCOPE_PATTERN.match(line)
    if not match:
        return None, False

    value = match.group(1).strip()
    return value, value in VALID_PRE_FLIGHT_SCOPE_VALUES


def parse_baseline_expect_annotation(line: str) -> tuple[Optional[str], bool]:
    """Parse standalone baseline-expect annotation line (Issue #889).

    Format: ``# baseline-expect: pass|fail|deferred``

    Args:
        line: A single source line.

    Returns:
        tuple[str|None, bool]: (value, is_known_value)

        value is the extracted annotation value when the line matches, or None.
        is_known_value is True when value is one of VALID_BASELINE_EXPECT_VALUES.

    Semantics:
        baseline-expect is an "execution result classification annotation"
        (not a safety policy bypass annotation).  It tells the preflight
        runtime what the author *expects* the VC to return at baseline:

        - ``pass``    : VC is expected to exit 0 at baseline (promotion/refactor)
        - ``fail``    : VC is expected to exit non-0 at baseline (new implementation)
        - ``deferred``: VC baseline run is deferred (like pr_review_only scope)

    Important: baseline-expect does NOT override static blockers.
    unsafe_command / compound / trivially-pass / broad search path detection
    takes precedence over any baseline-expect annotation.
    """
    match = _BASELINE_EXPECT_PATTERN.match(line)
    if not match:
        return None, False
    value = match.group(1).strip()
    return value, value in VALID_BASELINE_EXPECT_VALUES


def parse_vc_role_annotation(line: str) -> tuple[Optional[str], bool]:
    """Parse standalone vc-role annotation line (Issue #889).

    Format: ``# vc-role: <role>``

    Currently advisory (informational only).  The parser returns the raw value
    for downstream use.

    Returns:
        tuple[str|None, bool]: (value, True if value is non-empty)
    """
    match = _VC_ROLE_PATTERN.match(line)
    if not match:
        return None, False
    value = match.group(1).strip()
    return value if value else None, bool(value)


# Compound-shell operator characters (Issue #2788 fix_delta P1-A).  A shlex
# token is treated as a compound-shell OPERATOR token when it is composed
# ENTIRELY of characters from this set -- this generalizes the previous
# exact-match-against-a-finite-operator-set approach (which only recognized
# `{"&&", "||", "|", ";", "&", "<<", "<", ">", ">>", "<<<"}` verbatim) so
# that ANY punctuation-only run built from these characters is caught,
# including Bash-legal combinations the finite set missed:
# `>&` (`2>&1`), `&>` (`&>out`), `|&` (`|& tee x`), `>|` (`>| out`),
# `<>` (`<> file`).
#
# A token that MIXES an operator character with a word character (e.g. the
# quoted-regex-alternation token `foo|bar` shlex produces for
# `rg -n "foo|bar" PATH` -- Issue #589 / #2788 AC9 regression contract) is
# NEVER all-operator-chars, so it is correctly left non-compound.
_COMPOUND_OPERATOR_CHARS = frozenset(";&|<>")


def detect_compound_command(command: str) -> bool:
    """Detect whether ``command`` contains compound shell syntax.

    Moved here from ``baseline_vc_preflight.py`` (Issue #2788 AC9) so it is
    the SINGLE quote-aware compound-shell detection primitive shared by
    both normal VC execution classification AND
    ``parse_verification_commands_section()``'s static ``compound_shell``
    check below -- no independent regex-based compound detector exists
    anywhere else in this grammar.

    Uses ``shlex.shlex`` to tokenize precisely and detect shell operators:
    - ``cmd&&cmd`` (no whitespace) is detected.
    - A ``|`` inside a quoted string (e.g. ``rg -n "foo|bar" PATH``) is NOT
      a false positive (quote-aware; Issue #589 / #2788 AC9).
    - Redirects (``>``, ``<``, ``>>``, etc.) are treated as compound
      (fail-closed), including the less-common Bash redirect/pipe forms
      ``2>&1``, ``&>out``, ``|& tee x``, ``>| out``, and ``<> file``
      (Issue #2788 fix_delta P1-A -- these were previously false negatives
      because ``shlex``'s ``punctuation_chars=True`` tokenizer merges a
      contiguous run of punctuation characters into ONE token, e.g. ``>&``,
      which did not exact-match any entry in the old finite operator set).
    - A tokenization failure (malformed shell quoting) is treated as
      compound (fail-closed) -- preserving existing fail-closed semantics
      for malformed shell tokenization.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        tokens = list(lexer)
    except ValueError:
        # parse failure = ambiguous/complex command = fail-closed as compound
        return True

    return any(
        token and all(ch in _COMPOUND_OPERATOR_CHARS for ch in token)
        for token in tokens
    )


def extract_vc_regex_intent_annotation(lines: list, target_line_idx: int) -> Optional[str]:
    """Extract ``# vc-regex-intent: <value>`` annotation from the contiguous
    annotation/comment block immediately preceding a VC command line
    (Issue #589; moved here from ``baseline_vc_preflight.py`` in Issue #2788
    AC1/AC5/AC10 so both normal execution -- via the shared adapter -- and
    the shared parser's own ``VcCommandEntry.vc_regex_intent`` field source
    this SAME extraction logic).

    AC3 (Issue #589): backslash-pipe (``\\|``) regex-bearing commands
    (``rg`` / ``egrep`` etc.) are exempted from ``regex_literal_pipe_suspected``
    when a ``# vc-regex-intent: literal-pipe-ok`` annotation immediately
    precedes the command line.

    Format: ``# vc-regex-intent: literal-pipe-ok reason="..."``

    Scope rules (same as ``extract_baseline_expect_annotation`` /
    ``extract_vc_role_annotation``):
    - Only the contiguous block of comment/annotation lines directly before
      ``target_line_idx`` is considered (0-based index within ``lines``).
    - An empty line or a ``$ command`` line terminates the block.
    - ``# preflight-scope:`` and ``# AC<N>`` markers are transparent
      (allowed in the same block).

    Returns:
        The annotation value (e.g. ``"literal-pipe-ok"``) or ``None``.
    """
    found_annotation = None
    for offset in range(1, target_line_idx + 1):
        line_idx = target_line_idx - offset
        if line_idx < 0:
            break
        line = lines[line_idx].strip()

        # Empty line: stop scanning (annotation scope ended)
        if not line:
            break

        # $ command line: stop scanning (another command intervened)
        if re.match(r"^\$\s+", line) or re.match(r"^\$\s*$", line):
            break

        # vc-regex-intent annotation line: record it and continue scanning the block
        match = re.match(r"^#\s*vc-regex-intent:\s*(\S+)", line)
        if match:
            found_annotation = match.group(1)
            continue

        # preflight-scope marker: transparent (allowed in the same block)
        marker, _ = parse_preflight_scope_marker_line(line)
        if marker is not None:
            continue

        # AC marker line (# AC1 etc): transparent (allowed in the same block)
        ac_label, is_valid = parse_ac_marker_line(line)
        if ac_label is not None and is_valid:
            continue

        # Any other line (regular comment or non-comment non-command): stop scanning
        break

    return found_annotation


def extract_baseline_expect_annotation(
    lines: list,
    target_line_idx: int,
) -> tuple:
    """Extract ``# baseline-expect:`` annotation from the contiguous comment block
    immediately preceding a VC command line (Issue #889).

    Scope rules (consistent with existing vc-regex-intent annotation scoping):
    - Only the contiguous block of comment/annotation lines directly before
      target_line_idx is considered (0-based index within ``lines``).
    - An empty line or a ``$ command`` line terminates the block.
    - ``# preflight-scope:``, ``# AC<N>``, and ``# vc-role:`` markers are
      transparent (allowed in the same block).

    Args:
        lines: All lines of the bash block (list, 0-indexed).
        target_line_idx: 0-based index of the command line (``$ <cmd>``).

    Returns:
        tuple[value, line_number, raw_line]:
          value       - annotation value string or None
          line_number - 1-based line number within ``lines`` or None
          raw_line    - raw annotation line text or None
    """
    found_value: Optional[str] = None
    found_line_no: Optional[int] = None
    found_raw: Optional[str] = None

    for offset in range(1, target_line_idx + 1):
        line_idx = target_line_idx - offset
        if line_idx < 0:
            break
        line = lines[line_idx].strip()

        # Empty line: stop scanning
        if not line:
            break

        # $ command line: stop scanning (another command intervened)
        if re.match(r"^\$\s+", line) or re.match(r"^\$\s*$", line):
            break

        # baseline-expect annotation: record it and continue scanning
        # BLOCKER 2 fix: check is_known_value; invalid values are treated as None
        # so that typos (e.g. "pas") do not silently degrade to a missing annotation.
        value, is_known_value = parse_baseline_expect_annotation(line)
        if value is not None:
            if is_known_value:
                found_value = value
            else:
                # Invalid annotation value: store as sentinel "__invalid__" so
                # baseline_vc_preflight can emit human_judgment / invalid_baseline_expect_annotation.
                # Using None here would silently treat as "no annotation".
                found_value = f"__invalid__:{value}"
            found_line_no = line_idx + 1  # 1-based
            found_raw = line
            continue

        # preflight-scope marker: transparent
        scope, _ = parse_preflight_scope_marker_line(line)
        if scope is not None:
            continue

        # AC marker: transparent
        ac_label, is_valid = parse_ac_marker_line(line)
        if ac_label is not None and is_valid:
            continue

        # vc-role annotation: transparent
        role, _ = parse_vc_role_annotation(line)
        if role is not None:
            continue

        # Any other line: stop scanning
        break

    return found_value, found_line_no, found_raw


def extract_vc_role_annotation(
    lines: list,
    target_line_idx: int,
) -> Optional[str]:
    """Extract ``# vc-role:`` annotation from the contiguous comment block
    preceding a VC command line (Issue #889).

    Uses the same scope rules as ``extract_baseline_expect_annotation``.

    Returns:
        role value string or None.
    """
    for offset in range(1, target_line_idx + 1):
        line_idx = target_line_idx - offset
        if line_idx < 0:
            break
        line = lines[line_idx].strip()

        if not line:
            break
        if re.match(r"^\$\s+", line) or re.match(r"^\$\s*$", line):
            break

        role, _ = parse_vc_role_annotation(line)
        if role is not None:
            return role

        # Transparent markers
        scope, _ = parse_preflight_scope_marker_line(line)
        if scope is not None:
            continue

        ac_label, is_valid = parse_ac_marker_line(line)
        if ac_label is not None and is_valid:
            continue

        v, _ = parse_baseline_expect_annotation(line)
        if v is not None:
            continue

        # Any other line: stop
        break

    return None


# =============================================================================
# Unified VC section parser (Issue #993)
# =============================================================================


@dataclass
class VcParseError:
    """A single parse error found during VC section parsing.

    Attributes:
        kind: Error kind token (e.g. "colon_marker", "unlabeled_fence",
              "non_dollar_command", "inline_backtick").
        line_number: 1-based line number within the Verification Commands
                     section content (not the whole Issue body).
        raw_line: The raw offending line (stripped of leading/trailing whitespace).
        fix_hint: Human-readable suggestion for how to fix this error.
        rule_id: Optional LP rule ID (e.g. "LP016") for downstream routing.
    """
    kind: str
    line_number: int
    raw_line: str
    fix_hint: str
    rule_id: Optional[str] = None


@dataclass
class VcCommandEntry:
    """A single canonical VC command extracted from a bash fence.

    Attributes:
        ac_refs: Set of AC labels this command is associated with
                 (e.g. {"AC1"} for a `# AC1` marker, {"AC2", "AC3"} for grouped).
                 Empty set means the command is not explicitly labelled.
        command: The raw command string without the leading `$ `.
        line_number: 1-based line number within the VC section content.
        preflight_scope: Value of `# preflight-scope:` annotation if present,
                         otherwise None.
        baseline_expect: Value of `# baseline-expect:` annotation if present,
                         otherwise None.
        vc_role: Value of `# vc-role:` annotation if present, otherwise None.
        vc_regex_intent: Value of `# vc-regex-intent:` annotation if present
                         (e.g. "literal-pipe-ok"), otherwise None (Issue #589
                         / #2788 AC5/AC10 -- losslessly carried so downstream
                         normal execution can exempt a backslash-pipe
                         regex-bearing command from
                         `regex_literal_pipe_suspected`).
        annotation_source_line: 1-based line number within this command's
                         enclosing bash block (first line after the opening
                         ```bash fence is line 1) of the `# baseline-expect:`
                         annotation, or None. Unlike section-relative
                         `line_number`, this is block-relative, matching
                         `block_line_number` and the legacy normal result's
                         `annotation_source.line` (Issue #889 AC11 / #2788 AC5).
        annotation_source_raw: Raw text of that same annotation line, or
                         None.
        block_line_number: 1-based line number of this command WITHIN its
                         own enclosing ```bash fence (i.e. relative to the
                         first line after the ```bash opening fence line,
                         NOT relative to the whole VC section) -- Issue
                         #2788 fix_delta P2. This is an ADDITIVE provenance
                         field carried purely so legacy consumers that
                         expect ``parse_commands_from_block()``'s
                         block-relative line-number semantics (e.g. the
                         `results[].line` field in
                         ``baseline_vc_preflight.py``'s JSON output) can
                         recover that EXACT SAME coordinate system through
                         the shared adapter, without changing
                         `line_number`'s own (section-relative) semantics,
                         which other existing consumers already depend on.
    """
    ac_refs: set  # set[str]
    command: str
    line_number: int
    preflight_scope: Optional[str] = None
    baseline_expect: Optional[str] = None
    vc_role: Optional[str] = None
    vc_regex_intent: Optional[str] = None
    annotation_source_line: Optional[int] = None
    annotation_source_raw: Optional[str] = None
    block_line_number: Optional[int] = None


@dataclass
class VcParseResult:
    """Result of parsing a Verification Commands section.

    Canonical format:
        ## Verification Commands
        ```bash
        # ACN            (bare marker — no suffix)
        $ command
        ```

    Also supported:
        # AC2, AC3       (grouped marker — #814 compatibility)
        command # AC1    (inline suffix on command line)

    Non-canonical inputs generate entries in ``errors``.

    Attributes:
        commands: List of canonical VcCommandEntry items extracted from bash fences.
        errors: List of VcParseError items for non-canonical inputs.
        ac_refs: Set of all AC labels referenced in commands (union of all
                 VcCommandEntry.ac_refs values). Does NOT include refs that
                 only appear in parse errors.
        has_bash_fence: True if at least one ```bash fence was present.
        has_unlabeled_fence: True if at least one unlabeled ``` fence was present.
        static_errors: Subset of ``errors`` that block static validation
                       (non-canonical inputs; excludes warnings).
    """
    commands: list = field(default_factory=list)   # list[VcCommandEntry]
    errors: list = field(default_factory=list)     # list[VcParseError]
    canonical_ac_refs: set = field(default_factory=set)  # set[str] — $ command refs
    compat_ac_refs: set = field(default_factory=set)     # set[str] — non-$ backward compat refs

    @property
    def ac_refs(self) -> set:
        """Union of canonical and compat AC refs (backward compat)."""
        return self.canonical_ac_refs | self.compat_ac_refs
    has_bash_fence: bool = False
    has_unlabeled_fence: bool = False

    @property
    def static_errors(self) -> list:
        """Return errors that constitute static validation blockers."""
        # All errors are static blockers in the current design.
        return list(self.errors)


def parse_verification_commands_section(vc_section: str) -> "VcParseResult":
    """Parse the content of a '## Verification Commands' section.

    Canonical format (Issue #993):
    - Commands must reside inside bash fenced blocks (triple-backtick bash).
    - AC markers must be bare '# ACN' lines (no suffix).
    - Grouped markers '# AC2, AC3' are supported (#814 compatibility).
    - Inline suffix 'command # ACN' on command lines is supported.
    - Commands must start with ``$ `` (dollar sign + space).

    Non-canonical inputs that generate VcParseError entries:
    - Unlabeled fences (```  without language specifier).
    - AC marker lines with a suffix: ``# AC1:``, ``# AC1 text``, etc.
      → kind="colon_marker" (for colon/fullwidth-colon variants)
        or kind="suffixed_marker" (for other suffixes),
        rule_id="LP016".
    - Command lines without a leading ``$`` inside bash fences
      (non-empty, non-comment lines that are not annotations).
      → kind="non_dollar_command", no rule_id.
    - Inline backtick commands outside bash fences.
      → kind="inline_backtick", no rule_id.

    Args:
        vc_section: The raw text content of the Verification Commands section
                    (i.e., everything after ``## Verification Commands`` up to
                    the next ``##`` heading, as returned by extract_section()).

    Returns:
        VcParseResult with populated commands, errors, ac_refs, has_bash_fence,
        has_unlabeled_fence.
    """
    result = VcParseResult()

    if not vc_section:
        return result

    lines = vc_section.splitlines()
    _n = len(lines)

    # -----------------------------------------------------------------------
    # Pass 1: Detect unlabeled fences (``` without language specifier).
    # We need to find fences outside of bash blocks; a simple scan works
    # because nesting is not supported in Markdown.
    # -----------------------------------------------------------------------
    in_fence = False
    _fence_lang = ""
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if not in_fence:
                # Opening fence
                lang_part = stripped[3:].strip().lower()
                in_fence = True
                _fence_lang = lang_part
                if not lang_part:
                    result.has_unlabeled_fence = True
                    result.errors.append(VcParseError(
                        kind="unlabeled_fence",
                        line_number=0,  # will be fixed in pass 2
                        raw_line=stripped,
                        fix_hint=(
                            "Use ```bash (not ```) for Verification Commands fences. "
                            "Commands in unlabeled fences are not recognized as canonical VC."
                        ),
                    ))
            else:
                # Closing fence
                in_fence = False
                _fence_lang = ""

    # -----------------------------------------------------------------------
    # Pass 2: Extract commands and markers from bash fences,
    # detect colon/suffix AC markers and non-$ commands.
    # -----------------------------------------------------------------------
    in_bash = False
    current_ac_refs: set = set()  # AC refs accumulated for the next command
    bash_block_lines: list = []   # lines inside current bash block (for annotation lookup)
    _bash_block_line_offset = 0    # line number (1-based) of first line inside bash block

    # Track line numbers for unlabeled fence errors (fix up pass-1 results)
    unlabeled_fence_error_idx = 0

    # Inline backtick detection outside fences
    # We track whether we're inside any fence to avoid false positives.
    in_any_fence = False

    for line_no_0, raw_line in enumerate(lines):
        line_no = line_no_0 + 1  # 1-based
        stripped = raw_line.strip()

        # ── Fence boundary detection ──────────────────────────────────────
        if stripped.startswith("```"):
            if not in_any_fence:
                lang_part = stripped[3:].strip().lower()
                in_any_fence = True
                if lang_part == "bash":
                    in_bash = True
                    result.has_bash_fence = True
                    bash_block_lines = []
                    _bash_block_line_offset = line_no + 1
                    current_ac_refs = set()
                else:
                    # Unlabeled or other language fence — fix up line_number
                    if (unlabeled_fence_error_idx < len(result.errors) and
                            result.errors[unlabeled_fence_error_idx].kind == "unlabeled_fence"):
                        result.errors[unlabeled_fence_error_idx].line_number = line_no
                        unlabeled_fence_error_idx += 1
            else:
                # Closing fence
                in_any_fence = False
                if in_bash:
                    in_bash = False
                    current_ac_refs = set()
                    bash_block_lines = []
            continue

        if not in_any_fence:
            # Outside fences: detect inline backtick VC patterns
            # An inline backtick is flagged only if it looks like a command
            # (i.e., contains `$ ...` or starts with `-` and has backtick).
            # We check for "- `..." list-style VC and inline `$ cmd` patterns.
            if re.search(r'`[^`]+`', stripped):
                # Check if this looks like a VC command pattern
                if re.match(r'^\s*-\s+`', raw_line) or re.search(r'`\$\s+\S', raw_line):
                    result.errors.append(VcParseError(
                        kind="inline_backtick",
                        line_number=line_no,
                        raw_line=stripped,
                        fix_hint=(
                            "Inline backtick commands are not canonical VC format. "
                            "Place commands in a ```bash fenced block with $ prefix."
                        ),
                    ))
            continue

        if not in_bash:
            # Inside a non-bash fence — skip content
            continue

        # ── Inside a bash fence ───────────────────────────────────────────
        bash_block_lines.append(raw_line)
        block_idx = len(bash_block_lines) - 1  # 0-based index within block

        # Grouped AC marker: "# AC2, AC3, AC4"
        grouped_m = _GROUPED_AC_MARKER_PATTERN.match(stripped)
        if grouped_m:
            for ac_tok in re.findall(r"AC\d+", grouped_m.group(1)):
                current_ac_refs.add(ac_tok)
            continue

        # Single AC marker (bare or with suffix)
        ac_label, is_valid = parse_ac_marker_line(stripped)
        if ac_label is not None:
            if is_valid:
                current_ac_refs.add(ac_label)
            else:
                # Detect suffix kind
                suffix_m = re.match(r"^\s*#\s*AC\d+\s*([：:])", stripped)
                if suffix_m:
                    error_kind = "colon_marker"
                    fix_hint = (
                        f"Remove the colon/suffix from the AC marker. "
                        f"Use bare '# {ac_label}' (no colon, no description)."
                    )
                else:
                    error_kind = "suffixed_marker"
                    fix_hint = (
                        f"AC marker must be bare '# {ac_label}' without any suffix. "
                        f"Found: {stripped!r}"
                    )
                result.errors.append(VcParseError(
                    kind=error_kind,
                    line_number=line_no,
                    raw_line=stripped,
                    fix_hint=fix_hint,
                    rule_id="LP016",
                ))
            continue

        # Skip known annotation lines (not commands)
        scope_val, _ = parse_preflight_scope_marker_line(stripped)
        if scope_val is not None:
            continue

        be_val, _ = parse_baseline_expect_annotation(stripped)
        if be_val is not None:
            continue

        role_val, _ = parse_vc_role_annotation(stripped)
        if role_val is not None:
            continue

        # Skip other comment lines and empty lines
        if stripped.startswith("#") or not stripped:
            continue

        # Skip vc-regex-intent annotations
        if re.match(r"^\s*#\s*vc-regex-intent:\s*\S+", stripped):
            continue

        # ── Command line ──────────────────────────────────────────────────
        dollar_m = re.match(r"^\s*\$\s+(.+)$", stripped) or re.match(r"^\$\s*$", stripped)
        if dollar_m:
            cmd_str = dollar_m.group(1).strip() if dollar_m.lastindex else ""

            # Detect inline suffix "command # ACN"
            inline_ac_refs: set = set()
            if cmd_str:
                suffix_m2 = re.search(r"\s+#\s*(.+)\s*$", cmd_str)
                if suffix_m2:
                    suffix_label, suffix_valid = parse_ac_marker_line(f"# {suffix_m2.group(1)}")
                    if suffix_label is not None and suffix_valid:
                        inline_ac_refs.add(suffix_label)
                        cmd_str = re.sub(r"\s+#\s*AC\d+\s*$", "", cmd_str).strip()

            # Resolve AC refs: inline suffix overrides current_ac_refs if present
            if inline_ac_refs and current_ac_refs:
                result.errors.append(VcParseError(
                    kind="preceding_marker_with_inline_suffix",
                    line_number=line_no,
                    raw_line=stripped,
                    fix_hint=(
                        "Command has both a preceding '# ACN' marker and an inline '# ACN' suffix. "
                        "Use one or the other, not both."
                    ),
                ))
            if inline_ac_refs:
                resolved_ac = inline_ac_refs
            else:
                resolved_ac = set(current_ac_refs)

            # Extract annotations from the block (using 0-based index within block)
            preflight_scope: Optional[str] = None
            baseline_expect_val: Optional[str] = None
            vc_role_val: Optional[str] = None
            vc_regex_intent_val: Optional[str] = None
            annotation_source_line_val: Optional[int] = None
            annotation_source_raw_val: Optional[str] = None

            if block_idx > 0:
                ps_marker, ps_known = parse_preflight_scope_marker_line(
                    bash_block_lines[block_idx - 1].strip()
                )
                if ps_marker is not None:
                    preflight_scope = ps_marker

                be_v, be_line, be_raw = extract_baseline_expect_annotation(
                    [ln.strip() for ln in bash_block_lines], block_idx
                )
                baseline_expect_val = be_v
                annotation_source_line_val = be_line
                annotation_source_raw_val = be_raw

                vc_role_val = extract_vc_role_annotation(
                    [ln.strip() for ln in bash_block_lines], block_idx
                )

                # Issue #2788 AC5/AC10: thread `# vc-regex-intent:` provenance
                # into the shared entry so normal execution (via the shared
                # adapter) no longer needs its OWN independent extraction of
                # this annotation.
                vc_regex_intent_val = extract_vc_regex_intent_annotation(
                    [ln.strip() for ln in bash_block_lines], block_idx
                )

            entry = VcCommandEntry(
                ac_refs=resolved_ac,
                command=cmd_str,
                line_number=line_no,
                preflight_scope=preflight_scope,
                baseline_expect=baseline_expect_val,
                vc_role=vc_role_val,
                vc_regex_intent=vc_regex_intent_val,
                annotation_source_line=annotation_source_line_val,
                annotation_source_raw=annotation_source_raw_val,
                # Issue #2788 fix_delta P2: block_idx is the 0-based index of
                # THIS command line within `bash_block_lines` (the content
                # of the enclosing ```bash fence, fence markers excluded) --
                # the SAME coordinate system `extract_fenced_bash_blocks()` +
                # `parse_commands_from_block()`'s `i` (1-based) legacy
                # block-relative `line_number` used.
                block_line_number=block_idx + 1,
            )
            result.commands.append(entry)
            result.canonical_ac_refs.update(resolved_ac)

            # Detect compound shell operators (Issue #2788 AC9: delegate to
            # the shlex-based quote-aware detect_compound_command() instead
            # of an independent, non-quote-aware regex -- this preserves
            # `rg -n "foo|bar" PATH` (a quoted regex alternation) as NOT
            # compound_shell, while still detecting unquoted `|`, `&&`,
            # `;`, and redirects).
            if cmd_str and detect_compound_command(cmd_str):
                result.errors.append(VcParseError(
                    kind="compound_shell",
                    line_number=line_no,
                    raw_line=stripped,
                    fix_hint=(
                        "Compound shell operators (&&, ||, ;, >, |, etc.) are not allowed in VC commands. "
                        f"Split into separate $ lines. Found: {stripped!r}"
                    ),
                ))

            # Reset current_ac_refs after a command consumes it
            # (each command "claims" the accumulated markers)
            current_ac_refs = set()
        else:
            # Non-$ command line inside bash fence
            # This is a non-canonical command format.
            # For backward compatibility, still detect inline suffix AC refs (#AC1 format)
            # so LP010 can match them.  The error is still recorded to trigger LP016/C4.
            non_dollar_suffix_m = re.search(r"\s+#\s*(.+)\s*$", stripped)
            if non_dollar_suffix_m:
                suffix_label2, suffix_valid2 = parse_ac_marker_line(
                    f"# {non_dollar_suffix_m.group(1)}"
                )
                if suffix_label2 is not None and suffix_valid2:
                    result.compat_ac_refs.add(suffix_label2)
            elif current_ac_refs:
                # Backward compat: if valid AC markers were set before this non-$ command,
                # still add them to ac_refs so C5 does not co-fire with C4.
                # (old parser did this; omitting it causes autofix tools to refuse C4-only repairs)
                result.compat_ac_refs.update(current_ac_refs)

            result.errors.append(VcParseError(
                kind="non_dollar_command",
                line_number=line_no,
                raw_line=stripped,
                fix_hint=(
                    "Commands in Verification Commands bash fences must start with '$ '. "
                    f"Found non-$ line: {stripped!r}"
                ),
            ))



    return result
