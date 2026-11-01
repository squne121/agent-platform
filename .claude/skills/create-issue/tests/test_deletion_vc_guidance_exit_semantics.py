#!/usr/bin/env python3
"""create-issue の削除確認 VC guidance が exit 0 成功の absence 形であることを検証する (#2943).

検証内容:
1. SKILL.md / references/body-authoring.md の「削除確認パターン」節に、
   `rg --files-without-match --fixed-strings -- "<literal>" <file>` (1 command = 1 file) が
   存在し、`-c` / `--count` / `-q` / `--quiet` / `!` 否定検索の例が残っていないこと。
2. その canonical 置換形を repo の実 `baseline_vc_preflight.py` に real subprocess として
   渡し、baseline fixture では expected_fail、current-head fixture では
   expected_pass_resolved_on_current_head に分類されること（mock / fake classifier は使わない）。
   fixture の literal は `-` で始まる値を使い、`--` separator が option parse error を防ぐことも固定する。

uv / pytest / rg / git が利用できない場合は SKIP (PASS 扱い) にせず FAIL (assertion failure) とする。
exit 77 のためだけの wrapper / adapter は追加しない（Issue #2943 契約）。
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_MD = REPO_ROOT / ".claude" / "skills" / "create-issue" / "SKILL.md"
BODY_AUTHORING_MD = REPO_ROOT / ".claude" / "skills" / "create-issue" / "references" / "body-authoring.md"
PREFLIGHT_SCRIPT = REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"

SECTION_TITLE = "削除確認パターン"
SEARCH_COMMANDS = {"rg", "grep", "egrep", "fgrep"}
FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^#{1,6}\s")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


# ---------------------------------------------------------------------------
# section / command extraction helpers
# ---------------------------------------------------------------------------


def _skill_section_lines() -> list[str]:
    """SKILL.md の「削除確認パターン」箇条 (1 行) を返す。"""
    lines = SKILL_MD.read_text(encoding="utf-8").splitlines()
    matched = [ln for ln in lines if ln.lstrip().startswith("- **" + SECTION_TITLE + "**")]
    assert len(matched) == 1, f"SKILL.md の削除確認パターン箇条が 1 件ではない: {len(matched)}"
    return matched


def _body_authoring_section_lines() -> list[str]:
    """body-authoring.md の「### 削除確認パターン」節を次の見出しまで返す。"""
    lines = BODY_AUTHORING_MD.read_text(encoding="utf-8").splitlines()
    start = None
    for idx, ln in enumerate(lines):
        if ln.strip() == "### " + SECTION_TITLE:
            start = idx
            break
    assert start is not None, "body-authoring.md に「### 削除確認パターン」節がない"
    in_fence = False
    section: list[str] = []
    for ln in lines[start + 1 :]:
        if FENCE_RE.match(ln):
            in_fence = not in_fence
            section.append(ln)
            continue
        if not in_fence and HEADING_RE.match(ln):
            break
        section.append(ln)
    return section


def _fenced_command_lines(section: list[str]) -> list[str]:
    commands: list[str] = []
    in_fence = False
    for ln in section:
        if FENCE_RE.match(ln):
            in_fence = not in_fence
            continue
        if in_fence:
            stripped = ln.strip()
            if stripped and not stripped.startswith("#"):
                commands.append(stripped)
    return commands


def _inline_commands(section: list[str]) -> list[str]:
    commands: list[str] = []
    in_fence = False
    for ln in section:
        if FENCE_RE.match(ln):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        commands.extend(m.group(1).strip() for m in INLINE_CODE_RE.finditer(ln))
    return commands


def _tokenize(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _search_commands(section: list[str]) -> list[list[str]]:
    """inline code と fenced block から rg / grep 系 command を抽出して token 化する。

    先頭が `!` の否定検索も含める（token 先頭の `!` は別 token または prefix として保持する）。
    """
    found: list[list[str]] = []
    for command in _fenced_command_lines(section) + _inline_commands(section):
        tokens = _tokenize(command)
        if not tokens:
            continue
        head = tokens[0]
        if head.startswith("!") and len(head) > 1:
            probe = head[1:]
        elif head == "!" and len(tokens) > 1:
            probe = tokens[1]
        else:
            probe = head
        if probe in SEARCH_COMMANDS:
            found.append(tokens)
    return found


def _flag_tokens(tokens: list[str]) -> list[str]:
    """`--` (option parsing の終端) より前の option token だけを返す。"""
    flags: list[str] = []
    for t in tokens[1:]:
        if t == "--":
            break
        if t.startswith("-") and t != "-":
            flags.append(t)
    return flags


def _has_count_flag(tokens: list[str]) -> bool:
    for flag in _flag_tokens(tokens):
        if flag == "--count" or flag.startswith("--count="):
            return True
        if flag in {"--count-matches"}:
            return True
        if not flag.startswith("--") and "c" in flag[1:]:
            return True
    return False


def _has_quiet_flag(tokens: list[str]) -> bool:
    for flag in _flag_tokens(tokens):
        if flag == "--quiet":
            return True
        if not flag.startswith("--") and "q" in flag[1:]:
            return True
    return False


def _is_negated(tokens: list[str]) -> bool:
    return tokens[0].startswith("!")


def _is_canonical_absence_form(tokens: list[str]) -> bool:
    return (
        tokens[0] == "rg"
        and "--files-without-match" in tokens
        and "--fixed-strings" in tokens
        and "--" in tokens
        and tokens.index("--") > max(tokens.index("--files-without-match"), tokens.index("--fixed-strings"))
    )


def _path_operands(tokens: list[str]) -> list[str]:
    """canonical 形 (boolean long flag + `--` separator) の `--` 以降から path operand を返す。"""
    after = tokens[tokens.index("--") + 1 :]
    # `--` 直後の先頭 positional は pattern (literal)、残りが path operand
    return after[1:]


# ---------------------------------------------------------------------------
# test 1: guidance 文書の静的検査
# ---------------------------------------------------------------------------


def _assert_section_is_canonical(doc_name: str, section: list[str]) -> None:
    commands = _search_commands(section)
    assert commands, f"{doc_name}: 削除確認節から rg command を 1 件も抽出できない"

    for tokens in commands:
        rendered = " ".join(tokens)
        assert not _has_count_flag(tokens), f"{doc_name}: -c/--count が残っている: {rendered}"
        assert not _has_quiet_flag(tokens), f"{doc_name}: -q/--quiet が残っている: {rendered}"
        assert not _is_negated(tokens), f"{doc_name}: ! 否定検索が例として残っている: {rendered}"

    canonical = [t for t in commands if _is_canonical_absence_form(t)]
    assert canonical, f"{doc_name}: canonical 置換形 (--files-without-match + --fixed-strings + `--` separator) がない"
    for tokens in canonical:
        operands = _path_operands(tokens)
        assert len(operands) == 1, f"{doc_name}: path operand がちょうど 1 つではない: {operands} ({' '.join(tokens)})"
        assert operands == ["<file>"], f"{doc_name}: file placeholder が <file> ではない: {operands}"


def test_guidance_deletion_section_has_canonical_single_file_absence_form() -> None:
    """GIVEN 削除確認パターン節 WHEN rg command を抽出 THEN canonical absence 形のみが残る."""
    _assert_section_is_canonical("SKILL.md", _skill_section_lines())
    _assert_section_is_canonical("references/body-authoring.md", _body_authoring_section_lines())


# ---------------------------------------------------------------------------
# test 2: 実 baseline_vc_preflight による分類観測
# ---------------------------------------------------------------------------

# `-` で始まる literal。`--` separator がなければ rg は option と解釈して exit 2 になる。
LITERAL = "--DELETION_TARGET_LITERAL_2943"
# 旧形 `rg -c` の対照用（旧形は dash-leading literal をそもそも扱えないため dash なしの部分文字列を使う）。
PLAIN_LITERAL = LITERAL.lstrip("-")
TARGET_FILE = "target.md"


def _require_tools() -> None:
    # Issue #2943 契約: tool 不在は SKIP / PASS ではなく hard FAIL (assertion failure)。
    # exit 77 のためだけの wrapper は追加しない。uv / pytest は本 test の起動経路として必須。
    missing = [tool for tool in ("uv", "rg", "git") if shutil.which(tool) is None]
    assert not missing, f"必要な tool が利用不能 (SKIP は PASS ではない): {missing}"
    assert PREFLIGHT_SCRIPT.is_file(), f"baseline_vc_preflight.py がない: {PREFLIGHT_SCRIPT}"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _canonical_command_from_fenced_block() -> str:
    commands = _fenced_command_lines(_body_authoring_section_lines())
    canonical = [c for c in commands if _is_canonical_absence_form(_tokenize(c))]
    assert len(canonical) == 1, f"fenced block の canonical 置換形が 1 件ではない: {commands}"
    return canonical[0]


def _vc_body(command: str) -> str:
    return (
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "# baseline-expect: fail\n"
        f"$ {command}\n"
        "```\n\n"
        "## Allowed Paths\n\n"
        f"- {TARGET_FILE}\n"
    )


def _run_preflight(body_file: Path, repo: Path, *extra: str) -> tuple[int, dict[str, Any], dict[str, Any]]:
    proc = subprocess.run(
        [
            sys.executable,
            str(PREFLIGHT_SCRIPT),
            "--body-file",
            str(body_file),
            "--cwd",
            str(repo),
            "--format",
            "json",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    payload = json.loads(proc.stdout)
    results = payload["results"]
    assert len(results) == 1, f"VC 結果が 1 件ではない: {payload}"
    return proc.returncode, payload, results[0]


def test_replacement_command_classified_by_real_baseline_vc_preflight(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """GIVEN canonical 置換形 WHEN 実 baseline_vc_preflight に渡す THEN baseline=fail / current-head=pass."""
    _require_tools()

    new_command = _canonical_command_from_fenced_block().replace("<literal>", LITERAL).replace("<file>", TARGET_FILE)
    old_command = f'rg -c "{PLAIN_LITERAL}" {TARGET_FILE}'
    assert "--" in _tokenize(new_command), f"canonical 形に `--` separator がない: {new_command}"
    assert LITERAL.startswith("-"), "fixture literal は dash-leading でなければならない"

    repo = tmp_path / "fixture_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / TARGET_FILE).write_text(f"before {LITERAL} after\n", encoding="utf-8")
    _git(repo, "add", TARGET_FILE)
    _git(repo, "commit", "-q", "-m", "baseline: literal present")

    new_body = tmp_path / "new_form_body.md"
    new_body.write_text(_vc_body(new_command), encoding="utf-8")
    old_body = tmp_path / "old_form_body.md"
    old_body.write_text(_vc_body(old_command), encoding="utf-8")

    observations: list[str] = []

    # (a) baseline fixture (literal あり): 置換形は exit 1 / expected_fail
    _, _, new_baseline = _run_preflight(new_body, repo)
    observations.append(
        f"baseline new_form exit_code={new_baseline['exit_code']} "
        f"classification={new_baseline['classification']} category={new_baseline['category']}"
    )
    assert new_baseline["exit_code"] == 1
    assert new_baseline["classification"] == "expected_fail"
    assert new_baseline["category"] == "expected_baseline_fail"
    # dash-leading literal が option parse error (rg exit 2) になっていないこと
    assert "unrecognized flag" not in json.dumps(new_baseline, ensure_ascii=False)

    # 対照: `--` separator のない旧 canonical 形は dash-leading literal を option と解釈し rg exit 2
    no_sep = subprocess.run(
        ["rg", "--files-without-match", "--fixed-strings", LITERAL, TARGET_FILE],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    observations.append(
        f"no-separator form (dash-leading literal) rg exit_code={no_sep.returncode} stderr={no_sep.stderr.strip()!r}"
    )
    assert no_sep.returncode == 2
    assert "unrecognized flag" in no_sep.stderr

    # 対照: 旧形 rg -c は literal あり baseline で exit 0 / unexpected pass
    _, _, old_present = _run_preflight(old_body, repo)
    observations.append(
        f"baseline old_form(rg -c) literal_present exit_code={old_present['exit_code']} "
        f"classification={old_present['classification']}"
    )
    assert old_present["exit_code"] == 0
    assert old_present["classification"] == "unexpected_pass"

    # literal を除去して commit する (current-head fixture)
    (repo / TARGET_FILE).write_text("before after\n", encoding="utf-8")
    _git(repo, "add", TARGET_FILE)
    _git(repo, "commit", "-q", "-m", "resolved: literal removed")
    head = _git(repo, "rev-parse", "HEAD")

    # (b) current-head fixture (literal なし): 置換形は exit 0 / expected_pass_resolved_on_current_head
    rc, payload, new_current = _run_preflight(
        new_body, repo, "--evidence-mode", "current-head", "--reviewed-head-sha", head
    )
    observations.append(
        f"current-head new_form exit_code={new_current['exit_code']} "
        f"classification={new_current['classification']} category={new_current['category']} "
        f"process_rc={rc}"
    )
    assert payload["evidence_mode"] == "current-head"
    assert payload["reviewed_head_sha"] == head
    assert payload["stop_condition_triggered"] is False
    assert new_current["exit_code"] == 0
    assert new_current["classification"] == "expected_pass"
    assert new_current["category"] == "expected_pass_resolved_on_current_head"
    assert rc == 0

    # 対照: 旧形 rg -c は literal 除去後に exit 1 (成否が逆転する)
    _, _, old_absent = _run_preflight(old_body, repo)
    observations.append(
        f"baseline old_form(rg -c) literal_absent exit_code={old_absent['exit_code']} "
        f"classification={old_absent['classification']}"
    )
    assert old_absent["exit_code"] == 1

    with capsys.disabled():
        print()
        for line in observations:
            print(f"[#2943 runtime observation] {line}")
