#!/usr/bin/env python3
"""
issue_contract_hygiene_autofix.py

Deterministic autofix for trivial format blockers in Issue contract bodies.

Supported repairs:
  C4: Add $ prefix to command lines in fenced bash blocks within Verification Commands section.
      NOTE: C4 fence parser targets the contract canonical format only (exact ` ```bash` with no
      leading spaces, no tilde). GFM variants with indentation or tildes are out of scope —
      issue-contract-review enforces canonical formatting, so non-canonical fences should not
      appear in a contract body that has passed contract review.
  C9: Insert ## Runtime Verification Applicability section with decision: not_applicable
      when section is missing and all Allowed Paths are known-non-runtime (whitelist).

Exit codes:
  0: Repairs applied (body changed)
  1: No repairs needed (body unchanged, including sha256 no_change)
  2: Non-trivial blockers detected, autofixable judgment not possible, or unsafe to autofix
     (e.g. runtime paths detected, unknown paths, missing Allowed Paths section,
      or blocking issues other than C4/C9 found)
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Issue #2783: shared no-path marker predicate (`scripts/agent-ops/`). Pure
# policy library only -- not a grammar library import.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENT_OPS_DIR = _REPO_ROOT / "scripts" / "agent-ops"
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))
from allowed_paths_policy import is_no_path_marker  # noqa: E402


# Whitelist of path prefixes that are known non-runtime (workflow/docs/scripts only).
# Paths NOT matching any prefix here → exit 2 (fail-closed).
# NOTE: .github/workflows/** is intentionally excluded from the non-runtime whitelist
# (CI permission risk — human judgment required).
NON_RUNTIME_PATH_PREFIXES_WHITELIST = (
    ".claude/agents/",
    ".claude/skills/",
    "docs/",
    "scripts/",
)

# Paths that indicate product runtime files (C9 autofix not safe)
RUNTIME_PATH_PREFIXES = (
    "src/",
    "assets/",
    "LICENSES/",
    "public/",
    "dist/",
    "tests/",  # product tests (not .claude/skills/*/tests/)
)

# Path to check_issue_contract.py (relative to this script's location)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CHECK_ISSUE_CONTRACT_SCRIPT = os.path.normpath(
    os.path.join(_SCRIPT_DIR, "../../review-issue/scripts/check_issue_contract.py")
)

CHECK_ISSUE_CONTRACT_TIMEOUT_SEC = 120

# Issue #1285: VC baseline-fail pytest command shape canonicalization.
# Cross-skill script location (issue-contract-review owns the compiler; this
# script only loads and invokes it — no logic duplication).
VC_BASELINE_SHAPE_COMPILER_SCRIPT = os.path.normpath(
    os.path.join(_SCRIPT_DIR, "../../issue-contract-review/scripts/vc_baseline_shape_compiler.py")
)


def _load_vc_baseline_shape_compiler():
    """Load vc_baseline_shape_compiler.py as a module (cross-skill import by path)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "vc_baseline_shape_compiler", VC_BASELINE_SHAPE_COMPILER_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec for {VC_BASELINE_SHAPE_COMPILER_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Sentinel result codes for repair_vc_baseline_shape() (Issue #1305 review
# Blocker 3): VC shape repair is a completion condition of Issue #1285, not
# an optional best-effort repair, so compiler load failure must fail closed
# (exit 2), not fail open.
class VcShapeResult:
    OK = "ok"                                    # repaired, or safely no-op
    NO_VC_SECTION = "no_vc_section"               # no VC section at all -> safe no-op
    LOAD_FAILED = "compiler_load_failed"          # compiler module load failed -> fail-closed
    UNSAFE_MIXED = "unsafe_mixed_changed_and_warnings"  # partial-rewrite would leave broken lines -> fail-closed


def _body_has_verification_commands_section(lines: list[str]) -> bool:
    """Return True iff the body has a literal ## Verification Commands heading.

    Self-contained (no compiler import needed) so this check works even when
    the compiler module itself cannot be loaded.
    """
    for line in lines:
        if line.rstrip() == "## Verification Commands":
            return True
    return False


def repair_vc_baseline_shape(
    lines: list[str], repo_root: str
) -> tuple[list[str], bool, str, Optional[str]]:
    """Apply VC baseline-fail pytest command shape canonicalization.

    Delegates the actual detection/rewrite logic to
    vc_baseline_shape_compiler.compile_body() (Issue #1285) and applies any
    "changed" rewrites in place. Returns (new_lines, repaired, status, reason)
    where status is one of the VcShapeResult constants above.

    Fail-closed contract (Issue #1305 review Blocker 3):
      - No ``## Verification Commands`` section at all -> safe no-op
        (VcShapeResult.NO_VC_SECTION); there is nothing to canonicalize.
      - Compiler module cannot be imported/loaded -> fail-closed
        (VcShapeResult.LOAD_FAILED). The caller (main()) must exit 2, not
        silently skip this repair.
      - compile_body() reports status == "changed" together with any
        not_autofixable warnings (a body where some pytest VC lines are
        rewritable and others are not) -> fail-closed
        (VcShapeResult.UNSAFE_MIXED). Applying only the safe subset would
        report success while leaving a still-broken VC shape behind, so
        autofix only applies when the entire body is safely rewritable.
    """
    if not _body_has_verification_commands_section(lines):
        return lines, False, VcShapeResult.NO_VC_SECTION, None

    try:
        compiler = _load_vc_baseline_shape_compiler()
        if not callable(getattr(compiler, "compile_body", None)):
            raise ImportError("vc_baseline_shape_compiler has no callable compile_body")
    except Exception as e:
        return lines, False, VcShapeResult.LOAD_FAILED, str(e)

    body = "".join(lines)
    from pathlib import Path as _Path

    try:
        result = compiler.compile_body(body, _Path(repo_root))
        if not isinstance(result, dict):
            raise TypeError(f"compile_body returned {type(result).__name__}, expected dict")
    except Exception as e:
        # a compiler that loads but cannot produce a verdict must not look like "no repair needed"
        return lines, False, VcShapeResult.LOAD_FAILED, f"compile_body failed: {e}"

    if result.get("status") == "changed" and result.get("warnings"):
        return lines, False, VcShapeResult.UNSAFE_MIXED, "; ".join(result["warnings"])

    if result.get("status") != "changed" or not result.get("rewrites"):
        return lines, False, VcShapeResult.OK, None

    new_lines = lines[:]
    repaired = False
    for rw in result["rewrites"]:
        idx = rw["line_number"] - 1
        if idx < 0 or idx >= len(new_lines):
            continue
        original_line = new_lines[idx]
        stripped = original_line.rstrip("\n")
        leading_ws = len(stripped) - len(stripped.lstrip())
        indent = stripped[:leading_ws]
        new_lines[idx] = f"{indent}$ {rw['suggested_command']}\n"
        repaired = True
    return new_lines, repaired, VcShapeResult.OK, None


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_section_lines(lines: list[str], heading: str) -> tuple[int, int]:
    """Return (start_line_index, end_line_index_exclusive) for a ## heading section."""
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == heading:
            start = i
            break
    if start is None:
        return (-1, -1)
    for i in range(start + 1, len(lines)):
        if re.match(r"^## ", lines[i]):
            return (start, i)
    return (start, len(lines))


def is_runtime_path(path: str) -> bool:
    """Return True if path indicates a product runtime file."""
    path = path.strip().lstrip("- ").strip("`")
    # If it starts with .claude/skills/*/tests/ it's workflow test (non-runtime)
    if re.match(r"\.claude/skills/[^/]+/tests/", path):
        return False
    for prefix in RUNTIME_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def is_known_non_runtime_path(path: str) -> bool:
    """
    Whitelist-based check: return True only if path matches a known non-runtime prefix.
    Unknown/unclassified paths return False (fail-closed).
    NOTE: .github/** is intentionally not in the whitelist (CI permission risk).
    """
    path = path.strip().lstrip("- ").strip("`")
    # .claude/skills/*/tests/ is explicitly non-runtime
    if re.match(r"\.claude/skills/[^/]+/tests/", path):
        return True
    for prefix in NON_RUNTIME_PATH_PREFIXES_WHITELIST:
        if path.startswith(prefix):
            return True
    return False


def parse_allowed_paths(lines: list[str]) -> Optional[list[str]]:
    """Extract allowed paths from ## Allowed Paths section. Returns None if section missing."""
    start, end = extract_section_lines(lines, "## Allowed Paths")
    if start == -1:
        return None
    paths = []
    for i in range(start + 1, end):
        line = lines[i].strip()
        if line.startswith("- "):
            # Issue #2783: judge no-path marker BEFORE stripping the
            # trailing backtick wrapper -- a marker line contributes 0
            # paths (missing-section sentinel `None` stays reserved for
            # "no ## Allowed Paths section at all", handled above).
            if is_no_path_marker(line):
                continue
            path = line[2:].strip().strip("`")
            paths.append(path)
    return paths


def all_paths_non_runtime(paths: list[str]) -> tuple[bool, Optional[str]]:
    """
    Return (all_non_runtime, reason_if_not).
    Uses whitelist approach: any path not matching NON_RUNTIME_PATH_PREFIXES_WHITELIST
    is treated as unknown/unsafe → returns (False, reason).
    """
    for p in paths:
        if is_runtime_path(p):
            return False, f"runtime path detected: {p!r}"
        if not is_known_non_runtime_path(p):
            return False, f"unknown/unclassified path (not in non-runtime whitelist): {p!r}"
    return True, None


def has_runtime_verification_section(lines: list[str]) -> bool:
    """Return True if ## Runtime Verification Applicability section exists."""
    for line in lines:
        if line.rstrip() == "## Runtime Verification Applicability":
            return True
    return False


def find_delivery_rule_line(lines: list[str]) -> int:
    """Return the line index of ## Delivery Rule, or -1 if not found."""
    for i, line in enumerate(lines):
        if line.rstrip() == "## Delivery Rule":
            return i
    return -1


# Sentinel to distinguish "no repair needed" from "unsafe to repair"
class C9Result:
    OK = "ok"             # repair applied
    NO_CHANGE = "no_change"  # already has RVA section — no repair needed
    NOT_AUTOFIXABLE = "not_autofixable"  # runtime/unknown paths or missing Allowed Paths


def repair_c9(lines: list[str]) -> tuple[list[str], str, Optional[str]]:
    """
    C9 repair: Insert ## Runtime Verification Applicability section with
    decision: not_applicable when:
    - Section is missing
    - All Allowed Paths are known-non-runtime (whitelist)

    Returns (new_lines, result_code, reason).
      result_code: C9Result.OK | C9Result.NO_CHANGE | C9Result.NOT_AUTOFIXABLE
      reason: human-readable explanation when NOT_AUTOFIXABLE
    """
    if has_runtime_verification_section(lines):
        return lines, C9Result.NO_CHANGE, None

    allowed_paths = parse_allowed_paths(lines)
    if allowed_paths is None:
        return lines, C9Result.NOT_AUTOFIXABLE, "## Allowed Paths section is missing"

    if allowed_paths == []:
        return lines, C9Result.NOT_AUTOFIXABLE, "## Allowed Paths section is empty"

    ok, reason = all_paths_non_runtime(allowed_paths)
    if not ok:
        return lines, C9Result.NOT_AUTOFIXABLE, reason

    rva_block = [
        "## Runtime Verification Applicability\n",
        "本 Issue の変更対象はワークフロー文書・スクリプトのみです。ゲームの実行時動作には影響しません。\n",
        "\n",
        "```yaml\n",
        "decision: not_applicable\n",
        'reason: "Workflow documentation and script changes only. No runtime game behavior changed."\n',
        "```\n",
        "\n",
    ]

    delivery_rule_idx = find_delivery_rule_line(lines)
    if delivery_rule_idx != -1:
        new_lines = lines[:delivery_rule_idx] + rva_block + lines[delivery_rule_idx:]
    else:
        # Append at the end (before last blank line if present)
        new_lines = lines + ["\n"] + rva_block

    return new_lines, C9Result.OK, None


def check_non_c4_c9_blockers(body: str) -> tuple[bool, list[str]]:
    """
    Run check_issue_contract.py --json on the body and return
    (has_other_blockers, other_blocker_codes).

    Returns (True, [codes...]) if there are blocking issues other than C4/C9.
    Returns (False, []) if only C4/C9 blockers (or no blockers).
    Returns (True, ["check_error"]) if check_issue_contract.py is missing, cannot be invoked,
    times out, or emits no/invalid JSON (fail-closed; Stage 2-3 intentional diff).

    C4 and C9 are the only codes this script can autofix — other blockers must
    be resolved by a human before running this autofix.
    """
    if not os.path.isfile(CHECK_ISSUE_CONTRACT_SCRIPT):
        print(
            f"[WARN] check_issue_contract.py not found at {CHECK_ISSUE_CONTRACT_SCRIPT}; "
            "failing closed (treating as non-C4/C9 blocker)",
            file=sys.stderr,
        )
        return True, ["check_error"]

    # Write body to a temp file so check_issue_contract.py can read it
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", encoding="utf-8", delete=False
        ) as tf:
            tf.write(body)
            tmp_path = tf.name
    except OSError as e:
        print(f"[WARN] Cannot write temp file for contract check: {e}", file=sys.stderr)
        return True, ["check_error"]

    try:
        # Caller contract with check_issue_contract.py --json:
        #   - stdout carries the contract-check JSON ONLY (the single final JSON object).
        #   - stderr carries diagnostics (warnings, deprecation notices, progress).
        #   - This caller MUST NOT merge stderr into stdout (i.e. do not use
        #     stderr=subprocess.STDOUT). capture_output=True keeps stdout/stderr
        #     separated; we parse JSON from stdout only, so diagnostics emitted on
        #     stderr never affect JSON parsing.
        proc = subprocess.run(
            [sys.executable, CHECK_ISSUE_CONTRACT_SCRIPT, "--file", tmp_path, "--json"],
            capture_output=True,
            text=True,
            timeout=CHECK_ISSUE_CONTRACT_TIMEOUT_SEC,
        )
        # parse JSON from stdout (exit 0 = all pass, exit 1 = has failures)
        if proc.stdout.strip():
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                # stdout is not valid JSON — the caller contract (stdout = JSON only)
                # was violated (e.g. diagnostics leaked into stdout). Fail CLOSED:
                # keep the non-C4/C9 blocker check active and treat this as a blocker
                # so autofix does not silently proceed on a broken contract check.
                print(
                    "[WARN] check_issue_contract.py --json stdout was not valid JSON; "
                    "failing closed (treating as non-C4/C9 blocker)",
                    file=sys.stderr,
                )
                return True, ["check_error"]
            blocking = data.get("blocking_issues", [])
            # blocking_issues is a list of strings (human-readable messages).
            # This script can only autofix C4 ($ prefix) and C9 (RVA section).
            # Filter out messages that belong to checks this script handles or cannot
            # meaningfully gate on (structural/section-count checks):
            #
            # C4-related:
            #   - "C4" in message (check code reference)
            #   - "VC に実行可能コマンドが見当たらない" (C4 check fires when $ is missing
            #     — this is exactly what this script fixes, so don't gate on it)
            # C9-related:
            #   - "C9" in message
            #   - "Runtime Verification" in message (RVA section messages)
            #   - "レガシー Issue" (legacy C9 warning)
            # C1-related (required section absence — structural, not content):
            #   - "必須セクション" (required section missing)
            # C2-related (Stop Conditions count — content issue, not autofix target):
            #   - "Stop Conditions の項目数" (count gate)
            #   - "## Stop Conditions セクションが存在しない"
            #   - "## Acceptance Criteria セクションが存在しないか空"
            #   - "## Outcome セクションが存在しないか空"
            # These structural/section messages are filtered because:
            # (a) this autofix is called *after* issue-contract-review approves the body,
            #     so C1/C2 should already be passing in production use, and
            # (b) these messages contain no information useful for deciding whether
            #     C4/C9 are safe to apply.
            FILTER_PATTERNS = (
                "C4",
                "C9",
                "VC に実行可能コマンドが見当たらない",
                "Runtime Verification",
                "レガシー Issue",
                "必須セクション",
                "Stop Conditions の項目数",
                "## Stop Conditions セクションが存在しない",
                "## Acceptance Criteria セクションが存在しないか空",
                "## Outcome セクションが存在しないか空",
            )
            other_blockers = []
            for msg in blocking:
                if not any(pat in msg for pat in FILTER_PATTERNS):
                    other_blockers.append(msg)
            return len(other_blockers) > 0, other_blockers
        # empty stdout: the checker produced no verdict (crash / killed) -> fail closed
        print(
            "[WARN] check_issue_contract.py --json produced no stdout; failing closed "
            f"(exit code {proc.returncode})",
            file=sys.stderr,
        )
        return True, ["check_error"]
    except Exception as e:
        print(
            f"[WARN] check_issue_contract.py invocation failed ({type(e).__name__}): {e}; "
            "failing closed",
            file=sys.stderr,
        )
        return True, ["check_error"]
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return False, []  # pragma: no cover - every branch above returns


# Patterns for C4 repair (fenced bash block command line detection)
# A line is a "command line" (needing $ prefix) if:
#   - Not empty
#   - Not a comment line (starts with #)
#   - Not a continuation line (previous non-empty line ends with \)
#   - Not already prefixed with $
#   - Not a shell variable expression at line start (e.g. VAR=..., $VAR)
#   - Not prose (markdown text outside the bash block)
#   - Not a heredoc content line (inside EOF block)
#
# NOTE: C4 fence detection uses exact ` ```bash` matching (no leading spaces, backtick only).
# This is intentional: issue-contract-review enforces canonical ` ```bash` format, so
# non-canonical GFM fences (indented, tilde, info-string variants) are out of scope here.


def is_shell_variable_expression(line: str) -> bool:
    """Return True if line starts with a shell variable assignment or expression."""
    stripped = line.strip()
    # Variable assignment: VAR=value or VAR ="value"
    if re.match(r'^[A-Z_][A-Z0-9_]*=', stripped):
        return True
    # Shell variable expansion at start: $VAR, ${VAR}
    if re.match(r'^\$[A-Z_({]', stripped):
        return True
    return False


def repair_c4_in_vc_block(lines: list[str]) -> tuple[list[str], bool]:
    """
    C4 repair: Add $ prefix to command lines in fenced bash blocks within
    the ## Verification Commands section.

    Returns (new_lines, repaired).
    """
    vc_start, vc_end = extract_section_lines(lines, "## Verification Commands")
    if vc_start == -1:
        return lines, False

    new_lines = lines[:]
    repaired = False

    i = vc_start + 1
    while i < vc_end:
        line = new_lines[i]
        # Detect start of fenced bash block (canonical format only — see module docstring)
        if re.match(r'^```bash\s*$', line):
            _block_start = i
            i += 1
            in_heredoc = False
            heredoc_delimiter: Optional[str] = None
            prev_line_continuation = False

            while i < vc_end:
                bline = new_lines[i]
                # End of fenced block
                if re.match(r'^```\s*$', bline):
                    break

                stripped = bline.rstrip('\n')

                # Track heredoc state
                if in_heredoc:
                    # Check for heredoc end
                    if heredoc_delimiter and stripped.strip() == heredoc_delimiter:
                        in_heredoc = False
                        heredoc_delimiter = None
                    # Lines inside heredoc are not command lines
                    prev_line_continuation = False
                    i += 1
                    continue

                # Check for heredoc start
                heredoc_match = re.search(r"<<['\"]?(\w+)['\"]?", stripped)
                if heredoc_match:
                    # This line starts a heredoc; next lines until delimiter are heredoc content
                    pass  # We'll handle the flag after determining if we prefix this line

                # Empty line
                if not stripped.strip():
                    prev_line_continuation = False
                    i += 1
                    continue

                # Comment line
                if stripped.strip().startswith('#'):
                    prev_line_continuation = False
                    i += 1
                    continue

                # Continuation line (previous ended with \)
                if prev_line_continuation:
                    # This is a continuation line, not a command start
                    prev_line_continuation = stripped.endswith('\\')
                    i += 1
                    continue

                # Already has $ prefix
                if stripped.strip().startswith('$'):
                    prev_line_continuation = stripped.rstrip().endswith('\\')
                    if heredoc_match:
                        in_heredoc = True
                        heredoc_delimiter = heredoc_match.group(1)
                    i += 1
                    continue

                # Shell variable expression (VAR=... at line start)
                if is_shell_variable_expression(stripped.strip()):
                    prev_line_continuation = stripped.rstrip().endswith('\\')
                    if heredoc_match:
                        in_heredoc = True
                        heredoc_delimiter = heredoc_match.group(1)
                    i += 1
                    continue

                # This is a command line — add $ prefix
                # Preserve leading whitespace
                leading = len(stripped) - len(stripped.lstrip())
                indent = stripped[:leading]
                command_part = stripped[leading:]
                new_lines[i] = indent + '$ ' + command_part + '\n'
                repaired = True

                prev_line_continuation = stripped.rstrip().endswith('\\')
                if heredoc_match:
                    in_heredoc = True
                    heredoc_delimiter = heredoc_match.group(1)
                i += 1
            # After the closing ```
            i += 1
            continue
        i += 1

    return new_lines, repaired


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic autofix for trivial format blockers in Issue contract bodies"
    )
    parser.add_argument("--body-file", help="Input body file path (default: stdin)")
    parser.add_argument("--out-file", help="Output file path (default: stdout)")
    args = parser.parse_args()

    # Read input
    if args.body_file:
        try:
            with open(args.body_file, "r", encoding="utf-8") as f:
                original_body = f.read()
        except OSError as e:
            print(f"[ERROR] Cannot read body file: {e}", file=sys.stderr)
            return 2
    else:
        original_body = sys.stdin.read()

    original_sha256 = sha256_of(original_body)
    lines = original_body.splitlines(keepends=True)

    # AC4: Check for non-C4/C9 blockers first — if found, exit 2
    # (This script can only autofix C4 and C9; other blockers need human attention)
    has_other, other_codes = check_non_c4_c9_blockers(original_body)
    if has_other:
        print(
            f"[ERROR] Non-C4/C9 blocking issues detected; cannot autofix: {other_codes}",
            file=sys.stderr,
        )
        return 2

    # Apply C4 repair
    lines, c4_repaired = repair_c4_in_vc_block(lines)

    # Apply VC baseline-fail pytest command shape repair (Issue #1285).
    # Ordered before C9 (Issue #1305 review Blocker 4) so that a VC-shape
    # load failure or unsafe-mixed body fails closed before C9 does any
    # further (possibly redundant) work, and so C9's own not_autofixable
    # fail-closed path can be exercised together with VC shape repair in the
    # same run.
    repo_root_for_vc_shape = os.path.normpath(os.path.join(_SCRIPT_DIR, "../../../.."))
    lines, vc_shape_repaired, vc_shape_status, vc_shape_reason = repair_vc_baseline_shape(
        lines, repo_root_for_vc_shape
    )
    if vc_shape_status == VcShapeResult.LOAD_FAILED:
        print(
            "[ERROR] vc_baseline_shape_compiler could not be loaded; failing closed "
            f"(reason_code=vc_shape_compiler_load_failed): {vc_shape_reason}",
            file=sys.stderr,
        )
        return 2
    if vc_shape_status == VcShapeResult.UNSAFE_MIXED:
        print(
            "[ERROR] VC baseline shape has both rewritable and not_autofixable pytest "
            "command lines; failing closed rather than applying a partial rewrite "
            f"(reason_code=vc_shape_mixed_changed_and_warnings): {vc_shape_reason}",
            file=sys.stderr,
        )
        return 2

    # Apply C9 repair
    lines, c9_result, c9_reason = repair_c9(lines)
    c9_repaired = (c9_result == C9Result.OK)

    # If C9 is not autofixable (runtime/unknown paths or missing Allowed Paths) → exit 2
    if c9_result == C9Result.NOT_AUTOFIXABLE:
        print(
            f"[ERROR] C9 autofix not safe: {c9_reason}",
            file=sys.stderr,
        )
        return 2

    new_body = "".join(lines)
    new_sha256 = sha256_of(new_body)

    # sha256 guard: if body unchanged, return exit 1
    if original_sha256 == new_sha256:
        print("status: no_change", file=sys.stderr)
        return 1

    # Write output using temp file + os.replace() for atomic write
    if args.out_file:
        try:
            out_dir = os.path.dirname(os.path.abspath(args.out_file))
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=out_dir, delete=False, suffix=".tmp"
            ) as tf:
                tf.write(new_body)
                tmp_out = tf.name
            os.replace(tmp_out, args.out_file)
        except OSError as e:
            print(f"[ERROR] Cannot write output file: {e}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(new_body)

    print(
        f"status: repaired  c4={c4_repaired}  c9={c9_repaired}  vc_shape={vc_shape_repaired}  "
        f"original_sha256={original_sha256[:16]}...  new_sha256={new_sha256[:16]}...",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
