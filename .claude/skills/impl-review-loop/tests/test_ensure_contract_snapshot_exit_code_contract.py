"""
tests/test_ensure_contract_snapshot_exit_code_contract.py

Issue #3005: ensure_contract_snapshot.py の exit code 表と artifact 出力の文書を実挙動に一致させる。

- 単一の期待値表 (NORMAL_EXIT_TABLE / FRONT_STAGE_TABLE / ARTIFACT_CONTRACT) を、
  docstring の検証 (純粋関数 validate_*) と実 main() の検証の両方で共有する。
- docstring 検証関数は ``__doc__`` 文字列だけを受け取る純粋関数であり、
  git 履歴にも実モジュールにも依存しない。
- マーカー (EXIT20_DISAMBIGUATE_BY_STATUS など) の存在は正しさの判定に使わない。
- 否定・限定条件を含む重要な説明は、対象ケースの説明ブロックを抽出し、空白・折返しを正規化した
  文 (clause) の完全一致で検証する。部分文字列の存在だけでは説明の反転 (「区別できない」->「区別できる」)
  を検出できないため (OWNER REQUEST_CHANGES P2)。
- 既存 test_ensure_contract_snapshot.py の autouse fixture には依存しない。
  各ケースは fresh な result dict を使う (main() は result を変更するため使い回さない)。

テスト関数名の接頭辞は Issue の Verification Commands の -k selector と一致させる:
  test_docstring_exit_table_*     (AC1)
  test_docstring_cli_front_stage_* (AC2)
  test_docstring_artifact_contract_* (AC3)
  test_main_*                      (AC4)
  test_mutation_*                  (AC5)
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the real module under a unique name (avoids sys.modules collisions
# with other test files that load a same-named module).
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_ECS_PATH = _HERE.parent / "scripts" / "ensure_contract_snapshot.py"
_MODULE_NAME = "ensure_contract_snapshot_exit_code_contract_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _ECS_PATH)
assert _spec is not None and _spec.loader is not None
_ecs = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = _ecs
_spec.loader.exec_module(_ecs)

# ---------------------------------------------------------------------------
# Single expectation tables (shared by docstring validation and main() tests)
# ---------------------------------------------------------------------------

# 通常 producer result: exit code -> JSON status 名の集合
NORMAL_EXIT_TABLE: dict[int, frozenset[str]] = {
    0: frozenset({"ok"}),
    10: frozenset({"blocked_needs_refinement"}),
    20: frozenset({"human_judgment", "dry_run_would_post"}),
    40: frozenset({"runtime_error"}),
    50: frozenset({"stale_or_conflicting_snapshot"}),
    60: frozenset({"controlled_publisher_binding_failed"}),
}

# 通常 result で exit code から導く status -> exit code (main() の検証に使う)
STATUS_TO_EXIT: dict[str, int] = {status: code for code, statuses in NORMAL_EXIT_TABLE.items() for status in statuses}
UNKNOWN_STATUS = "some_status_not_in_the_table"
UNKNOWN_STATUS_EXIT = 40

# docstring の説明文に必ず含まれる語句 (exit code -> 必須 substring)
NORMAL_REQUIRED_PHRASES: dict[int, tuple[str, ...]] = {
    20: ("status", "区別"),
    40: ("未知",),
}

# CLI 前段: exit code -> 経路名の集合
FRONT_STAGE_TABLE: dict[int, frozenset[str]] = {
    30: frozenset({"runtime_error"}),
    2: frozenset({"argparse_usage_error"}),
    0: frozenset({"argparse_help"}),
}

# CLI 前段の経路名 -> 実際に main() へ渡す argv (複数)
FRONT_STAGE_ARGVS: dict[str, tuple[tuple[str, ...], ...]] = {
    "runtime_error": (("--issue-number", "0"), ("--issue", "0"), ("--issue-number", "00")),
    "argparse_usage_error": ((), ("--issue-number", "abc"), ("--issue-number",)),
    "argparse_help": (("--help",),),
}

FRONT_STAGE_REQUIRED_PHRASES: dict[int, tuple[str, ...]] = {
    30: ("--issue-number 0", "--issue 0", "runtime_error", "stdout", "artifact", "到達しない"),
    2: ("必須引数", "整数型不正", "stderr", "stdout"),
    0: ("--help", "stdout"),
}

STDOUT_EXCEPTION_PHRASES: tuple[str, ...] = ("compact JSON", "例外", "argparse", "--help", "stderr")

# artifact 契約: 3 ケース名 -> 説明に必ず含まれる語句
ARTIFACT_CONTRACT: dict[str, tuple[str, ...]] = {
    "artifact_saved": ("--artifact-dir", "artifact_path", "除いた", "一致"),
    "artifact_save_failed": ("artifact_write_error", "status", "終了コード", "変わらない", "exit 0"),
    "artifact_dir_unspecified": ("未指定", "artifact_path"),
}
ARTIFACT_WHOLE_SECTION_PHRASES: tuple[str, ...] = ("証明ではない",)

# ---------------------------------------------------------------------------
# 文 (clause) 単位の期待値表 (Issue #3005 OWNER REQUEST_CHANGES P2)
#
# 上の *_PHRASES は部分文字列の存在しか見ないため、「区別できない」を「区別できる」に、
# 「追加されず」を「追加され」に反転しても通ってしまう。そこで否定・限定条件を含む重要な
# 説明は、対象ケースの説明ブロックを構造的に抽出したうえで、空白・折返しを正規化した
# 「文 (clause)」として完全一致で検証する。clause の区切りは 。 、 と括弧のみで、
# 汎用の自然言語解析は行わない。期待文には区切り文字を含めない (アトミックな文にする)。
# ---------------------------------------------------------------------------

NORMAL_REQUIRED_CLAUSES: dict[int, tuple[str, ...]] = {
    20: (
        "2 つの status が同じ exit 20 を共有する",
        "終了コードでは区別できないため",
        "stdout JSON の status で区別する",
    ),
    40: ("表にない未知 status もここへ落ちる",),
}

FRONT_STAGE_REQUIRED_CLAUSES: dict[int, tuple[str, ...]] = {
    30: (
        "整数変換後の Issue 番号が 0 の場合のみ",
        "ensure_contract_snapshot にも artifact 保存にも到達しない",
        "status が invalid_input になることはない",
    ),
    2: ("argparse が stderr に診断を出して終了し", "stdout は通常空"),
    0: ("argparse が stdout にヘルプテキストを出して終了する",),
}

# 'stdout:' 段落 (継続行を含む) の clause
STDOUT_REQUIRED_CLAUSES: tuple[str, ...] = (
    "ただし例外として argparse の早期終了は JSON を出さない",
    "exit 2 は stdout が通常空で診断は stderr",
    "--help は stdout にヘルプテキスト",
)

# 「終了コードから status を…」で始まる段落の clause
FRONT_STAGE_FOOTER_PREFIX = "終了コードから status"
FRONT_STAGE_FOOTER_CLAUSES: tuple[str, ...] = (
    "終了コードから status を一意に復元することはできない",
    "exit 0 は ok と --help の双方",
    "exit 40 は runtime_error と未知 status の双方を含み",
    "runtime_error は exit 30 と 40 の両方に現れる",
)

ARTIFACT_REQUIRED_CLAUSES: dict[str, tuple[str, ...]] = {
    "artifact_saved": (
        "--artifact-dir 指定で保存に成功した場合に限り",
        "保存先を示す artifact_path が stdout の JSON にだけ追加される",
        "は stdout の JSON から artifact_path を除いたものと内容が一致する",
        "バイト列一致ではない",
    ),
    "artifact_save_failed": (
        "artifact_path は追加されず",
        "status も終了コードも変わらない",
        "status が ok なら保存に失敗しても exit 0 のまま",
    ),
    "artifact_dir_unspecified": (
        "--artifact-dir 未指定では保存処理自体がなく",
        "ファイルは作られず artifact_path も追加されない",
    ),
}
ARTIFACT_WHOLE_SECTION_CLAUSES: tuple[str, ...] = (
    "exit 0 は今回の artifact 保存成功の証明ではない",
    "保存成功は stdout JSON の artifact_path と errors で判定する",
)

_MARKERS = (
    "EXIT20_DISAMBIGUATE_BY_STATUS",
    "EXIT30_ONLY_ISSUE_NUMBER_ZERO",
    "ARTIFACT_OMITS_ARTIFACT_PATH",
)

# ---------------------------------------------------------------------------
# Docstring validators: pure functions of the docstring text only
# ---------------------------------------------------------------------------

_EXIT_ENTRY_RE = re.compile(r"^  (\d+) ([a-z_]+(?:, [a-z_]+)*) — (.*)$")
_ARTIFACT_ENTRY_RE = re.compile(r"^  (artifact_[a-z_]+): (.*)$")


_CLAUSE_SEPARATORS = re.compile(r"[。、()（）]")


def _squash(text: str) -> str:
    """空白 (改行・折返しの空白を含む) をすべて除去する。折返し位置の違いを無効化する。"""
    return re.sub(r"\s+", "", text)


def _clauses(text: str) -> set[str]:
    """説明ブロックを 。 、 括弧で区切った clause の集合にする (空白は除去済み)。

    ``name()`` の空括弧は区切りにせず取り除く。clause の完全一致で検証するため、
    「区別できない」が「区別できる」「区別できないわけではない」になると不一致になる。
    """
    pieces = _CLAUSE_SEPARATORS.split(text.replace("()", ""))
    return {squashed for piece in pieces if (squashed := _squash(piece))}


def _missing_clauses(block: str, required: tuple[str, ...], label: str) -> list[str]:
    present = _clauses(block)
    return [f"{label}: 期待する文 {clause!r} が説明に無い" for clause in required if _squash(clause) not in present]


def _paragraph(doc: str, start_prefix: str) -> str | None:
    """start_prefix で始まる行から、次の空行までの段落を改行で連結して返す。"""
    lines = doc.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith(start_prefix):
            block = [line]
            for follow in lines[idx + 1 :]:
                if not follow.strip():
                    break
                block.append(follow)
            return "\n".join(block)
    return None


def _section_lines(doc: str, header_prefix: str) -> list[str] | None:
    """header_prefix で始まる行の次から、最初の空行までを返す。header が無ければ None。"""
    lines = doc.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith(header_prefix):
            body: list[str] = []
            for follow in lines[idx + 1 :]:
                if not follow.strip():
                    break
                body.append(follow)
            return body
    return None


def _validate_exit_section(
    doc: str,
    header_prefix: str,
    table: dict[int, frozenset[str]],
    phrases: dict[int, tuple[str, ...]],
    clauses: dict[int, tuple[str, ...]],
    label: str,
) -> list[str]:
    mismatches: list[str] = []
    section = _section_lines(doc, header_prefix)
    if section is None:
        return [f"{label}: セクション見出し {header_prefix!r} が見つからない"]

    parsed: dict[int, tuple[frozenset[str], str]] = {}
    for line_idx, line in enumerate(section):
        match = _EXIT_ENTRY_RE.match(line)
        if not match:
            continue
        code = int(match.group(1))
        statuses = frozenset(match.group(2).split(", "))
        # 継続行を連結して説明全文にする
        description = match.group(3)
        for cont in section[line_idx + 1 :]:
            if _EXIT_ENTRY_RE.match(cont) or not re.match(r"^ {4,}\S", cont):
                break
            description += " " + cont.strip()
        if code in parsed:
            mismatches.append(f"{label}: exit {code} の行が重複している")
            continue
        parsed[code] = (statuses, description)

    for code, expected_statuses in table.items():
        if code not in parsed:
            mismatches.append(f"{label}: exit {code} の行が無い")
            continue
        got_statuses, description = parsed[code]
        if got_statuses != expected_statuses:
            mismatches.append(
                f"{label}: exit {code} の status 名が {sorted(got_statuses)} (期待 {sorted(expected_statuses)})"
            )
        for phrase in phrases.get(code, ()):
            if phrase not in description:
                mismatches.append(f"{label}: exit {code} の説明に {phrase!r} が無い")
        mismatches.extend(_missing_clauses(description, clauses.get(code, ()), f"{label}: exit {code}"))
    for code in parsed:
        if code not in table:
            mismatches.append(f"{label}: 期待値表に無い exit {code} の行がある")
    return mismatches


def validate_exit_table(doc: str) -> list[str]:
    """通常 producer result の status -> exit code 表 (AC1) を検証し、不一致の説明を返す。"""
    return _validate_exit_section(
        doc,
        "Exit codes (normal producer result)",
        NORMAL_EXIT_TABLE,
        NORMAL_REQUIRED_PHRASES,
        NORMAL_REQUIRED_CLAUSES,
        "normal",
    )


def validate_cli_front_stage(doc: str) -> list[str]:
    """CLI 前段の表と stdout 例外記載 (AC2) を検証し、不一致の説明を返す。"""
    mismatches = _validate_exit_section(
        doc,
        "Exit codes (CLI front stage)",
        FRONT_STAGE_TABLE,
        FRONT_STAGE_REQUIRED_PHRASES,
        FRONT_STAGE_REQUIRED_CLAUSES,
        "front_stage",
    )
    stdout_text: str | None = None
    lines = doc.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("stdout:"):
            stdout_text = line
            for follow in lines[idx + 1 :]:
                if not re.match(r"^ {2,}\S", follow):
                    break
                stdout_text += " " + follow.strip()
            break
    if stdout_text is None:
        mismatches.append("stdout: 'stdout:' 行が無い")
    else:
        for phrase in STDOUT_EXCEPTION_PHRASES:
            if phrase not in stdout_text:
                mismatches.append(f"stdout: 説明に {phrase!r} が無い")
        mismatches.extend(_missing_clauses(stdout_text, STDOUT_REQUIRED_CLAUSES, "stdout"))
    footer = _paragraph(doc, FRONT_STAGE_FOOTER_PREFIX)
    if footer is None:
        mismatches.append(f"front_stage: {FRONT_STAGE_FOOTER_PREFIX!r} で始まる段落が無い")
    else:
        mismatches.extend(_missing_clauses(footer, FRONT_STAGE_FOOTER_CLAUSES, "front_stage footer"))
    return mismatches


def validate_artifact_contract(doc: str) -> list[str]:
    """artifact 契約 3 ケース (AC3) を検証し、不一致の説明を返す。"""
    section = _section_lines(doc, "Artifact contract")
    if section is None:
        return ["artifact: セクション見出し 'Artifact contract' が見つからない"]

    mismatches: list[str] = []
    parsed: dict[str, str] = {}
    for line_idx, line in enumerate(section):
        match = _ARTIFACT_ENTRY_RE.match(line)
        if not match:
            continue
        description = match.group(2)
        for cont in section[line_idx + 1 :]:
            if _ARTIFACT_ENTRY_RE.match(cont) or not re.match(r"^ {4,}\S", cont):
                break
            description += " " + cont.strip()
        if match.group(1) in parsed:
            mismatches.append(f"artifact: {match.group(1)} が重複している")
            continue
        parsed[match.group(1)] = description

    for case, phrases in ARTIFACT_CONTRACT.items():
        if case not in parsed:
            mismatches.append(f"artifact: ケース {case} が無い")
            continue
        for phrase in phrases:
            if phrase not in parsed[case]:
                mismatches.append(f"artifact: {case} の説明に {phrase!r} が無い")
        mismatches.extend(_missing_clauses(parsed[case], ARTIFACT_REQUIRED_CLAUSES[case], f"artifact: {case}"))
    for case in parsed:
        if case not in ARTIFACT_CONTRACT:
            mismatches.append(f"artifact: 期待値表に無いケース {case} がある")

    whole = " ".join(line.strip() for line in section)
    for phrase in ARTIFACT_WHOLE_SECTION_PHRASES:
        if phrase not in whole:
            mismatches.append(f"artifact: セクション全体に {phrase!r} が無い")
    # ケース説明の継続行 (インデント 4 以上) を除いた、セクション末尾の総括文 (インデント 2 の非ケース行)
    summary = "\n".join(line for line in section if re.match(r"^  \S", line) and not _ARTIFACT_ENTRY_RE.match(line))
    mismatches.extend(_missing_clauses(summary, ARTIFACT_WHOLE_SECTION_CLAUSES, "artifact: セクション末尾の総括"))
    return mismatches


def validate_docstring(doc: str | None) -> list[str]:
    """3 検証をまとめて実行する。不一致が 0 件なら docstring は期待値表と一致する。"""
    if not doc:
        return ["docstring が空"]
    return validate_exit_table(doc) + validate_cli_front_stage(doc) + validate_artifact_contract(doc)


# ---------------------------------------------------------------------------
# OLD docstring (origin/main bde4bce1 時点のモジュール docstring 全文)
# ---------------------------------------------------------------------------

OLD_DOCSTRING = """
ensure_contract_snapshot.py

impl-review-loop の missing_contract_go 分岐で呼ばれる orchestration script。
Issue に有効な CONTRACT_REVIEW_RESULT_V1 status: go コメントが存在するか確認し、
存在しない場合は issue-contract-review を自動実行して go コメントを取得・投稿する。

Exit codes:
  0   ok — contract snapshot が確認または materialize できた
  10  blocked_needs_refinement — contract blocked / readiness blocked
  20  human_judgment — 分類不能 / ambiguous / env error
  30  invalid_input — argument エラー
  40  runtime_error — subprocess / network エラー
  50  stale_or_conflicting_snapshot — atomicity 検証で body_sha256 or updatedAt mismatch
  60  controlled_publisher_binding_failed — #1475: 投稿直後の comment ID readback binding 不一致・欠落

stdout: CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 compact JSON のみ
stderr: diagnostic messages のみ

Modes:
  check-only  — 既存 go コメントを確認するのみ。mutation なし (default)
  auto        — go コメントがなければ run_contract_review_once.py を実行
  dry-run     — auto と同じ判定だが GitHub 投稿はしない

--post: GitHub mutation を有効化 (auto mode 以外では無視)

idempotency marker:
  <!-- agent-platform:contract-snapshot issue=<N> body_sha256=sha256:<...> schema=CONTRACT_REVIEW_RESULT_V1 -->

body/comment snapshot atomicity (B2):
  最初に一括取得し body_sha256, issue_updated_at, comments_digest を保存。
  投稿直前に再取得して比較。
  body_sha256 変化 OR updatedAt 変化 OR latest blocked コメント出現 → exit 50。

API error classification (403/429/422 blind retry 禁止):
  not_requested | dry_run_would_post | posted | deduped_existing |
  permission_denied | rate_limited | validation_failed_or_spam | ambiguous_no_retry

Schema key: post_status (not post_result — B4)

status: ok implies contract_snapshot_url is not None (B3).
dry-run / no-post → status: dry_run_would_post (not ok).

Comment posting: gh api REST (B5) for precise HTTP status classification.
V1 comment includes checks summary (B6).

Security scope (#1475 fix_delta P1 item 3 -- explicit, deliberately narrowed
claim rather than an unimplemented receipt schema):
  "Authoritative" means: authored by a GitHub account present in
  contract_review_result_parser.TRUSTED_CONTRACT_PUBLISHERS (an exact
  user.id + user.login + user.type + author_association match), which today
  is the repo OWNER account only. It does NOT mean "identical to the exact
  bytes this specific ensure_contract_snapshot.py process instance posted
  moments ago" for every code path:
    - The publish-time path (POST_STATUS_POSTED) DOES verify the freshly
      posted comment id, issue binding, publisher identity, and comment
      body hash via an independent direct-GET readback
      (verify_controlled_publisher_comment_id_binding).
    - The existing-snapshot-reuse path (source: existing_go, status: ok
      without a POST in this run) does NOT re-run that binding check. It is
      protected instead by the strict identity-tuple allowlist applied to
      every comment considered a candidate (only the allowlisted account
      can ever produce an authoritative go/blocked entry) plus the
      body_sha256 / vc_preflight / product_spec_check freshness checks in
      is_go_current(). It does not protect against the allowlisted account's
      own comment being edited after posting to a body that still hashes to
      a value is_go_current() would accept as current -- that residual risk
      is intentionally out of scope for this Issue and tracked as a
      follow-up (CONTRACT_SNAPSHOT_PUBLISH_RECEIPT_V1, option 1 in the
      Issue #1475 fix_delta) rather than claimed as solved here.
"""

# ---------------------------------------------------------------------------
# main() harness
# ---------------------------------------------------------------------------


def _fresh_result(status: str = "ok") -> dict:
    """ensure_contract_snapshot() の result を模した fresh dict (main() が破壊的に変更する)。"""
    return {
        "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
        "status": status,
        "issue_number": 3005,
        "errors": [],
    }


def _run_main(monkeypatch, capsys, argv, result=None):
    """ensure_contract_snapshot を fake して main() を実行する。

    戻り値: (exit_code, stdout, stderr, calls)。argparse 早期終了は SystemExit.code を exit_code にする。
    """
    calls: list[dict] = []

    def fake_ensure_contract_snapshot(**kwargs):
        calls.append(kwargs)
        return result if result is not None else _fresh_result()

    monkeypatch.setattr(_ecs, "ensure_contract_snapshot", fake_ensure_contract_snapshot)
    monkeypatch.setattr(sys, "argv", ["ensure_contract_snapshot.py", *argv])
    try:
        code = _ecs.main()
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err, calls


# ---------------------------------------------------------------------------
# AC1: docstring exit table (normal producer result)
# ---------------------------------------------------------------------------


def test_docstring_exit_table_matches_expectation_table():
    assert validate_exit_table(_ecs.__doc__) == []


def test_docstring_exit_table_exit20_lists_both_statuses():
    section = _section_lines(_ecs.__doc__, "Exit codes (normal producer result)")
    assert section is not None
    exit20 = [line for line in section if line.startswith("  20 ")]
    assert len(exit20) == 1
    match = _EXIT_ENTRY_RE.match(exit20[0])
    assert match is not None
    assert set(match.group(2).split(", ")) == {"human_judgment", "dry_run_would_post"}


def test_docstring_exit_table_keeps_published_exit_values():
    # exit code の値は変更しない (published contract)。期待値表自体の固定。
    assert set(NORMAL_EXIT_TABLE) == {0, 10, 20, 40, 50, 60}
    assert STATUS_TO_EXIT == {
        "ok": 0,
        "blocked_needs_refinement": 10,
        "human_judgment": 20,
        "dry_run_would_post": 20,
        "runtime_error": 40,
        "stale_or_conflicting_snapshot": 50,
        "controlled_publisher_binding_failed": 60,
    }


# ---------------------------------------------------------------------------
# AC2: docstring CLI front stage + stdout exception
# ---------------------------------------------------------------------------


def test_docstring_cli_front_stage_matches_expectation_table():
    assert validate_cli_front_stage(_ecs.__doc__) == []


def test_docstring_cli_front_stage_does_not_claim_invalid_input_status():
    # exit 30 の JSON status は invalid_input ではなく runtime_error である。
    section = _section_lines(_ecs.__doc__, "Exit codes (CLI front stage)")
    assert section is not None
    exit30 = [line for line in section if line.startswith("  30 ")]
    assert len(exit30) == 1
    match = _EXIT_ENTRY_RE.match(exit30[0])
    assert match is not None
    assert match.group(2) == "runtime_error"


def test_docstring_cli_front_stage_stdout_exception_is_documented():
    mismatches = [m for m in validate_cli_front_stage(_ecs.__doc__) if m.startswith("stdout:")]
    assert mismatches == []


# ---------------------------------------------------------------------------
# AC3: docstring artifact contract
# ---------------------------------------------------------------------------


def test_docstring_artifact_contract_matches_expectation_table():
    assert validate_artifact_contract(_ecs.__doc__) == []


def test_docstring_artifact_contract_has_exactly_three_cases():
    section = _section_lines(_ecs.__doc__, "Artifact contract")
    assert section is not None
    cases = [m.group(1) for line in section if (m := _ARTIFACT_ENTRY_RE.match(line))]
    assert sorted(cases) == sorted(ARTIFACT_CONTRACT)


# ---------------------------------------------------------------------------
# AC4: real main()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [*sorted(STATUS_TO_EXIT.items()), (UNKNOWN_STATUS, UNKNOWN_STATUS_EXIT)],
)
def test_main_normal_result_status_maps_to_documented_exit_code(monkeypatch, capsys, status, expected_exit):
    code, out, _err, calls = _run_main(monkeypatch, capsys, ["--issue-number", "3005"], _fresh_result(status))
    assert code == expected_exit
    assert len(calls) == 1
    assert calls[0]["issue_number"] == 3005
    payload = json.loads(out)
    assert payload["status"] == status


def test_main_result_without_status_key_is_exit_40(monkeypatch, capsys):
    result = _fresh_result()
    del result["status"]
    code, _out, _err, _calls = _run_main(monkeypatch, capsys, ["--issue-number", "3005"], result)
    assert code == 40


def test_main_exit_codes_cover_every_row_of_the_expectation_table(monkeypatch, capsys):
    for code_expected, statuses in NORMAL_EXIT_TABLE.items():
        for status in sorted(statuses):
            code, _out, _err, _calls = _run_main(monkeypatch, capsys, ["--issue-number", "3005"], _fresh_result(status))
            assert code == code_expected, status


def test_main_exit20_is_shared_and_distinguished_only_by_json_status(monkeypatch, capsys):
    seen = {}
    for status in ("human_judgment", "dry_run_would_post"):
        code, out, _err, _calls = _run_main(monkeypatch, capsys, ["--issue-number", "3005"], _fresh_result(status))
        seen[status] = (code, json.loads(out)["status"])
    assert seen["human_judgment"][0] == seen["dry_run_would_post"][0] == 20
    assert seen["human_judgment"][1] != seen["dry_run_would_post"][1]


@pytest.mark.parametrize("argv", FRONT_STAGE_ARGVS["runtime_error"], ids=lambda a: " ".join(a))
def test_main_front_stage_issue_number_zero_is_exit_30_without_producer_or_artifact(
    monkeypatch, capsys, tmp_path, argv
):
    artifact_dir = tmp_path / "artifacts"
    code, out, _err, calls = _run_main(monkeypatch, capsys, [*argv, "--artifact-dir", str(artifact_dir)])
    assert code == 30
    assert code in FRONT_STAGE_TABLE
    payload = json.loads(out)
    assert payload["status"] == "runtime_error"
    assert payload["errors"] == ["--issue-number is required"]
    assert "artifact_path" not in payload
    assert calls == []  # ensure_contract_snapshot() は呼ばれない
    assert not artifact_dir.exists()  # artifact 保存 (mkdir/write) に到達しない


@pytest.mark.parametrize("argv", FRONT_STAGE_ARGVS["argparse_usage_error"], ids=lambda a: " ".join(a) or "no-args")
def test_main_front_stage_argparse_usage_error_is_exit_2_with_stderr(monkeypatch, capsys, argv):
    code, out, err, calls = _run_main(monkeypatch, capsys, list(argv))
    assert code == 2
    assert out == ""
    assert "--issue-number" in err
    assert err.strip()
    assert calls == []


def test_main_front_stage_non_integer_issue_number_reports_invalid_int(monkeypatch, capsys):
    _code, _out, err, _calls = _run_main(monkeypatch, capsys, ["--issue-number", "abc"])
    assert "invalid int value" in err


def test_main_front_stage_help_is_exit_0_with_stdout_help(monkeypatch, capsys):
    code, out, err, calls = _run_main(monkeypatch, capsys, ["--help"])
    assert code == 0
    assert "usage:" in out
    assert "--issue-number" in out
    assert err == ""
    assert calls == []
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)  # --help は JSON ではない (stdout は compact JSON のみ、の argparse 例外)


def test_main_front_stage_table_rows_are_all_exercised(monkeypatch, capsys, tmp_path):
    expected_by_name = {name: code for code, names in FRONT_STAGE_TABLE.items() for name in names}
    assert set(expected_by_name) == set(FRONT_STAGE_ARGVS)
    for name, argv_variants in FRONT_STAGE_ARGVS.items():
        for argv in argv_variants:
            code, _out, _err, _calls = _run_main(monkeypatch, capsys, list(argv))
            assert code == expected_by_name[name], (name, argv)


def test_main_artifact_saved_matches_stdout_without_artifact_path(monkeypatch, capsys, tmp_path):
    artifact_dir = tmp_path / "nested" / "artifacts"
    code, out, _err, calls = _run_main(
        monkeypatch, capsys, ["--issue-number", "3005", "--artifact-dir", str(artifact_dir)], _fresh_result("ok")
    )
    assert code == 0
    assert calls[0]["artifact_dir"] == str(artifact_dir)
    stdout_payload = json.loads(out)
    saved_path = artifact_dir / "contract-snapshot-3005.json"
    assert stdout_payload["artifact_path"] == str(saved_path)
    saved_payload = json.loads(saved_path.read_text(encoding="utf-8"))
    assert "artifact_path" not in saved_payload
    assert saved_payload == {k: v for k, v in stdout_payload.items() if k != "artifact_path"}


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [("ok", 0), ("human_judgment", 20), ("stale_or_conflicting_snapshot", 50)],
)
def test_main_artifact_save_failure_keeps_status_and_exit_code(monkeypatch, capsys, tmp_path, status, expected_exit):
    not_a_dir = tmp_path / "regular-file"
    not_a_dir.write_text("not a directory", encoding="utf-8")
    code, out, _err, _calls = _run_main(
        monkeypatch, capsys, ["--issue-number", "3005", "--artifact-dir", str(not_a_dir)], _fresh_result(status)
    )
    payload = json.loads(out)
    assert code == expected_exit  # status ok なら保存失敗でも exit 0 (exit 0 は保存成功の証明ではない)
    assert payload["status"] == status
    assert "artifact_path" not in payload
    assert any(str(e).startswith("artifact_write_error") for e in payload["errors"])
    assert not_a_dir.read_text(encoding="utf-8") == "not a directory"


def test_main_artifact_dir_unspecified_saves_nothing(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    code, out, _err, calls = _run_main(monkeypatch, capsys, ["--issue-number", "3005"], _fresh_result("ok"))
    assert code == 0
    assert calls[0]["artifact_dir"] is None
    payload = json.loads(out)
    assert "artifact_path" not in payload
    assert payload["errors"] == []
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# AC5: mutation checks (git 履歴に依存しない)
# ---------------------------------------------------------------------------


def test_mutation_old_docstring_is_detected_as_mismatch():
    mismatches = validate_docstring(OLD_DOCSTRING)
    assert mismatches
    # 3 つの検証すべてで不一致が出る (旧 docstring は 2 表も artifact 契約も持たない)
    assert validate_exit_table(OLD_DOCSTRING)
    assert validate_cli_front_stage(OLD_DOCSTRING)
    assert validate_artifact_contract(OLD_DOCSTRING)


def test_mutation_old_docstring_with_markers_appended_is_still_detected():
    with_markers = OLD_DOCSTRING.rstrip("\n") + "\n\n" + "\n".join(_MARKERS) + "\n"
    for marker in _MARKERS:
        assert marker in with_markers
    assert validate_exit_table(with_markers)
    assert validate_cli_front_stage(with_markers)
    assert validate_artifact_contract(with_markers)
    assert validate_docstring(with_markers)


def test_mutation_real_module_docstring_has_zero_mismatches():
    assert validate_docstring(_ecs.__doc__) == []


def test_mutation_dropping_dry_run_would_post_from_exit20_is_detected():
    doc = _ecs.__doc__
    assert "20 human_judgment, dry_run_would_post — " in doc
    mutated = doc.replace("20 human_judgment, dry_run_would_post — ", "20 human_judgment — ")
    assert any("exit 20" in m for m in validate_exit_table(mutated))


def test_mutation_changing_exit30_status_to_invalid_input_is_detected():
    doc = _ecs.__doc__
    assert "30 runtime_error — " in doc
    mutated = doc.replace("30 runtime_error — ", "30 invalid_input — ")
    assert any("exit 30" in m for m in validate_cli_front_stage(mutated))


def test_mutation_removing_artifact_failure_case_is_detected():
    doc = _ecs.__doc__
    mutated = "\n".join(line for line in doc.splitlines() if not line.startswith("  artifact_save_failed:"))
    assert any("artifact_save_failed" in m for m in validate_artifact_contract(mutated))


def test_mutation_markers_alone_never_satisfy_validators():
    markers_only = "\n".join(_MARKERS)
    assert validate_docstring(markers_only)
    assert validate_docstring(None)
    assert validate_docstring("")


# ---------------------------------------------------------------------------
# AC5 (続き): 説明の反転 mutation (Issue #3005 OWNER REQUEST_CHANGES P2)
#
# 見出し・ケース名・exit/status 表・検索用マーカーをすべて残したまま、説明の否定・限定条件だけを
# 反転した docstring を、対応する validate_* が不一致として検出することを固定する。
# 上の「見出し削除」「表の status 変更」「旧 docstring」系とは別軸である。
# 変異は実モジュールの正しい docstring への置換で作り、置換元が実在し、ちょうど 1 回
# 置換されたこと (vacuous でないこと) を必ず確認する。
# ---------------------------------------------------------------------------

_HEADINGS = (
    "Exit codes (normal producer result)",
    "Exit codes (CLI front stage)",
    "Artifact contract",
)
_CASE_NAMES = ("artifact_saved:", "artifact_save_failed:", "artifact_dir_unspecified:")
_STATUS_ROWS = ("20 human_judgment, dry_run_would_post — ", "30 runtime_error — ")

# (id, 置換元, 置換後, 対応する validator, 不一致メッセージに含まれるべき対象)
_INVERSION_MUTATIONS = [
    pytest.param(
        "artifact_path は追加されず",
        "artifact_path は追加され",
        validate_artifact_contract,
        "artifact_save_failed",
        id="artifact_save_failed_adds_artifact_path",
    ),
    pytest.param(
        "ファイルは作られず artifact_path も追加されない",
        "ファイルが作られ artifact_path も追加される",
        validate_artifact_contract,
        "artifact_dir_unspecified",
        id="artifact_dir_unspecified_creates_file",
    ),
    pytest.param(
        "内容が一致する",
        "内容が一致しない",
        validate_artifact_contract,
        "artifact_saved",
        id="artifact_saved_content_does_not_match",
    ),
    pytest.param(
        "argparse の早期終了は JSON を出さない",
        "argparse の早期終了は JSON を出す",
        validate_cli_front_stage,
        "stdout",
        id="argparse_early_exit_emits_json",
    ),
    pytest.param(
        "終了コードでは区別できない",
        "終了コードでは区別できる",
        validate_exit_table,
        "exit 20",
        id="exit20_distinguishable_by_exit_code",
    ),
    pytest.param(
        "整数変換後の Issue 番号が 0 の場合のみ",
        "引数が不正な場合",
        validate_cli_front_stage,
        "exit 30",
        id="exit30_for_any_invalid_argument",
    ),
]


@pytest.mark.parametrize(("original", "inverted", "validator", "target"), _INVERSION_MUTATIONS)
def test_mutation_inverted_explanation_is_detected_with_headings_and_markers_kept(
    original, inverted, validator, target
):
    doc = _ecs.__doc__
    # vacuous 防止: 置換元は実モジュールの docstring にちょうど 1 回だけ存在する
    assert doc.count(original) == 1
    mutated = doc.replace(original, inverted)
    assert mutated != doc
    assert inverted in mutated and original not in mutated

    # 見出し・ケース名・exit/status 表・検索用マーカーは残っている (説明だけを反転した変異)
    for kept in (*_HEADINGS, *_CASE_NAMES, *_STATUS_ROWS, *_MARKERS):
        assert kept in mutated, kept

    # 変異前は不一致 0 件、変異後は対応する検証が説明の不一致を検出する
    assert validator(doc) == []
    mismatches = validator(mutated)
    assert mismatches
    assert any(target in m for m in mismatches), mismatches
    # 表・見出し・語句ではなく、説明の文の不一致として落ちている
    assert any("期待する文" in m for m in mismatches), mismatches


@pytest.mark.parametrize(("original", "inverted", "validator", "target"), _INVERSION_MUTATIONS)
def test_mutation_inverted_explanation_is_detected_by_validate_docstring(original, inverted, validator, target):
    doc = _ecs.__doc__
    assert doc.count(original) == 1
    assert validate_docstring(doc) == []
    assert validate_docstring(doc.replace(original, inverted))


@pytest.mark.parametrize(("original", "inverted", "validator", "target"), _INVERSION_MUTATIONS)
def test_mutation_deleting_explanation_clause_is_detected(original, inverted, validator, target):
    doc = _ecs.__doc__
    assert doc.count(original) == 1
    assert validator(doc.replace(original, ""))


def _all_required_clauses() -> list[str]:
    clauses: list[str] = []
    for group in (
        *NORMAL_REQUIRED_CLAUSES.values(),
        *FRONT_STAGE_REQUIRED_CLAUSES.values(),
        STDOUT_REQUIRED_CLAUSES,
        FRONT_STAGE_FOOTER_CLAUSES,
        *ARTIFACT_REQUIRED_CLAUSES.values(),
        ARTIFACT_WHOLE_SECTION_CLAUSES,
    ):
        clauses.extend(group)
    return clauses


def test_mutation_required_clauses_are_atomic_and_all_present_in_real_docstring():
    clauses = _all_required_clauses()
    assert clauses
    squashed_doc = _squash(_ecs.__doc__.replace("()", ""))
    for clause in clauses:
        # 区切り文字を含まないアトミックな文だけを期待値にする (完全一致検証の前提)
        assert not _CLAUSE_SEPARATORS.search(clause), clause
        # 期待文は実 docstring に実在する (表が空振りしていない。厳密な clause 一致は validate_* が検証する)
        assert _squash(clause) in squashed_doc, clause


def test_mutation_clause_matching_ignores_whitespace_and_fold_position():
    reference = _clauses("終了コードでは区別できないため、stdout JSON の status で区別する (EXIT20)。")
    folded = _clauses("終了コード\n      では区\n別できないため、stdout\n JSON の\nstatus で区別する\n(EXIT20)。")
    assert reference == folded
    assert _squash("終了コードでは区別できないため") in reference
    # 否定が反転した文・否定の後ろに語が足された文は完全一致しない
    assert _squash("終了コードでは区別できないため") not in _clauses("終了コードでは区別できるため、")
    assert _squash("終了コードでは区別できないため") not in _clauses("終了コードでは区別できないためではない、")


def _refold(doc: str, width: int) -> str:
    """説明の継続行を、空白位置で width 文字幅に折り返し直す (意味不変。折返し位置だけ変える)。

    対象は exit 表 / artifact 契約の継続行 (インデント 6)、stdout 段落の継続行、
    「終了コードから status」で始まる段落の行。見出し・ケース名・表の先頭行は触らない。
    """
    out: list[str] = []
    in_footer = False
    for line in doc.splitlines():
        if not line.strip():
            in_footer = False
        if line.startswith(FRONT_STAGE_FOOTER_PREFIX):
            in_footer = True
        is_continuation = line.startswith("      ") and line.strip() != ""
        is_stdout_cont = line.startswith(("  ただし例外として", "  (exit 2 は"))
        if is_continuation or is_stdout_cont or in_footer:
            indent = line[: len(line) - len(line.lstrip())]
            wrapped = textwrap.wrap(line.strip(), width=width, break_long_words=False, break_on_hyphens=False)
            out.extend(indent + piece for piece in wrapped)
        else:
            out.append(line)
    return "\n".join(out)


@pytest.mark.parametrize("step", [20, 30, 50])
def test_mutation_rewrapping_real_docstring_keeps_zero_mismatches(step):
    doc = _ecs.__doc__
    rewrapped = _refold(doc, step)
    assert rewrapped != doc  # 折返し位置が実際に変わっている
    assert validate_docstring(rewrapped) == []


@pytest.mark.parametrize("step", [20, 40])
@pytest.mark.parametrize(("original", "inverted", "validator", "target"), _INVERSION_MUTATIONS)
def test_mutation_inverted_explanation_is_detected_after_rewrapping(step, original, inverted, validator, target):
    doc = _ecs.__doc__
    assert doc.count(original) == 1
    # 反転は raw 行で行い、そのうえで折返し位置を変えても検出される
    assert validator(_refold(doc.replace(original, inverted), step))
