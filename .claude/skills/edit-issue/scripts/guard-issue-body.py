#!/usr/bin/env python3
"""
guard-issue-body.py

Issue 本文ファイルに対して以下のガードを適用し、YAML/JSON で結果を出力する。

Guards:
  1. Template Guard         — 必須セクションが存在するか（ISSUE_TEMPLATE 動的取得）
  2. Outcome Quality Guard  — Outcome が成果物形式・完了条件を含むか
 3. Diff Threshold         — 削減率が 50% 以下か（--orig-file 指定時）
 4. AC-VC Alignment        — `## Acceptance Criteria` セクション内の AC 番号集合と
                              `## Verification Commands` セクション内 bash fence 内の
                              # AC<N> 番号集合（grouped marker・inline suffix 含む）が
                              一致するか（issue_kind 別 skip: VC セクションが不要な
                              種別ではスキップ）。AC 側の重複定義は fail。
  5. Issue Body Validation   — create-issue の validate_issue_body.py を実行し、
                              LP ルール違反を編集前に検出する。

Usage:
    python3 guard-issue-body.py <body_file> [--orig-file <original_file>]
                                [--format yaml|json] [--issue-kind implementation|research|parent]

Exit codes:
    0 — all guards pass
    2 — at least one guard failed
    1 — unexpected error
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# allowlist: ファイルパスは安全な文字のみ許可
_PATH_RE = re.compile(r'^[A-Za-z0-9._/\-]+$')

# Outcome 不適合パターン（動作状態のみ・成果物形式欠落）
_OUTCOME_NG_RE = re.compile(
    r'(決定される|整理される|完了する|検討する|改善する)\s*$',
    re.MULTILINE
)

# issue_kind の安全な文字のみ許可（パストラバーサル対策）
_ISSUE_KIND_RE = re.compile(r'^[A-Za-z0-9_-]+$')

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CREATE_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(_CREATE_ISSUE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS))

import prose_boundary_policy as _pbp  # noqa: E402

_VALIDATE_ISSUE_BODY_SCRIPT = (
    _REPO_ROOT
    / ".claude"
    / "skills"
    / "create-issue"
    / "scripts"
    / "validate_issue_body.py"
)


def guard_issue_body_validation(body_file: Path, issue_kind: str | None = None) -> dict:
    """Run validate_issue_body.py and return a guard result map."""
    if not _VALIDATE_ISSUE_BODY_SCRIPT.exists():
        return {
            "name": "issue_body_validation",
            "passed": False,
            "status": "blocked",
            "error": "validator_script_missing",
            "details": {
                "path": str(_VALIDATE_ISSUE_BODY_SCRIPT),
            },
        }

    cmd = [
        sys.executable,
        str(_VALIDATE_ISSUE_BODY_SCRIPT),
        "--body-file",
        str(body_file),
    ]
    if issue_kind:
        cmd.extend(["--kind", issue_kind])

    cp = subprocess.run(cmd, capture_output=True, text=True)
    if cp.returncode not in (0, 1):
        return {
            "name": "issue_body_validation",
            "passed": False,
            "status": "blocked",
            "error": "validator_runtime_error",
            "returncode": cp.returncode,
            "stderr": cp.stderr.strip()[:1200],
        }

    try:
        result = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        return {
            "name": "issue_body_validation",
            "passed": False,
            "status": "blocked",
            "error": "validator_output_parse_error",
            "details": str(exc),
        }

    status = result.get("status", "blocked")
    return {
        "name": "issue_body_validation",
        "passed": status == "pass",
        "status": status,
        "schema": result.get("schema"),
        "body_sha256": result.get("body_sha256"),
        "errors": result.get("errors", []),
    }


def validate_path(value: str) -> Path:
    if not _PATH_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"Path must match ^[A-Za-z0-9._/-]+$, got: {value!r}"
        )
    p = Path(value)
    if not p.exists():
        raise argparse.ArgumentTypeError(f"File not found: {value!r}")
    return p


def validate_issue_kind(issue_kind: str) -> None:
    """
    issue_kind がパストラバーサルに利用できない安全な文字列であることを検証する。

    Raises:
        ValueError: 不正な文字が含まれる場合
    """
    if not _ISSUE_KIND_RE.fullmatch(issue_kind):
        raise ValueError(
            f"Invalid issue_kind: {issue_kind!r}. "
            "Must match ^[A-Za-z0-9_-]+$ (no path traversal characters allowed)"
        )


def load_required_labels(template_dir: Path, issue_kind: str) -> list:
    """
    .github/ISSUE_TEMPLATE/{issue_kind}.yml をパースし、
    validations.required: true の要素の attributes.label を返す。
    type: markdown 要素は除外する。

    スコープ: 本リポジトリの implementation / research / parent テンプレートの
    validations.required: true の attributes.label に限定した照合を行う。
    GitHub Forms schema 全体（checkboxes の option 単位 required 等）への
    一般対応ではない。

    Args:
        template_dir: ISSUE_TEMPLATE ディレクトリのパス
        issue_kind: 種別（implementation / research / parent 等）

    Returns:
        必須ラベルのリスト（`## <label>` 形式で本文内の見出しと照合する）

    Raises:
        ValueError: issue_kind に不正な文字が含まれる場合、または
                    テンプレートの構造が不正な場合（dict でない、body が配列でない等）
        FileNotFoundError: テンプレートファイルが存在しない場合
    """
    # パストラバーサル対策: issue_kind の文字列を検証
    validate_issue_kind(issue_kind)

    template_path = template_dir / f"{issue_kind}.yml"

    # パス解決後にテンプレートディレクトリ外を参照していないか確認
    if template_path.resolve().parent != template_dir.resolve():
        raise ValueError(
            f"Resolved template path escapes template_dir: {template_path.resolve()}"
        )

    if not template_path.exists():
        raise FileNotFoundError(
            f"ISSUE_TEMPLATE not found for kind '{issue_kind}': {template_path}"
        )

    with template_path.open(encoding="utf-8") as f:
        template = yaml.safe_load(f)

    # テンプレート構造の検証
    if not isinstance(template, dict):
        raise ValueError(
            f"Template file '{issue_kind}.yml' must be a YAML mapping (dict), "
            f"got {type(template).__name__!r}"
        )

    body_items = template.get("body", [])
    if not isinstance(body_items, list):
        raise ValueError(
            f"'body' in '{issue_kind}.yml' must be a list, "
            f"got {type(body_items).__name__!r}"
        )

    required_labels = []

    for item in body_items:
        # item type: markdown 要素は除外
        if item.get("type") == "markdown":
            continue

        validations = item.get("validations", {})
        if not validations.get("required"):
            continue

        attrs = item.get("attributes", {})
        label = attrs.get("label")

        if label is None:
            raise ValueError(
                f"Required item in '{issue_kind}.yml' is missing 'attributes.label': {item!r}"
            )
        if not isinstance(label, str):
            raise ValueError(
                f"'attributes.label' must be a string, got {type(label).__name__!r}: {label!r}"
            )

        required_labels.append(label)

    return required_labels


def _extract_mrc_section(body: str) -> str:
    """
    本文から `## Machine-Readable Contract` セクションのテキストを返す。
    次の `## ` 見出しまでを切り出す。

    Returns:
        str: MRC セクションのテキスト（セクションが存在しない場合は空文字列）
    """
    lines = body.splitlines()
    in_section = False
    section_lines = []
    for line in lines:
        if re.match(r'^##[ \t]+Machine-Readable Contract[ \t]*$', line):
            in_section = True
            continue
        if in_section:
            if re.match(r'^##[ \t]+', line):
                break
            section_lines.append(line)
    return "\n".join(section_lines)


def extract_issue_kind_from_body(body: str):
    """
    本文の `## Machine-Readable Contract` セクション配下にある fenced yaml ブロックから
    issue_kind フィールドを抽出する。

    抽出条件（すべてを満たす場合のみ issue_kind を返す）:
    - ブロックが `## Machine-Readable Contract` セクション配下にある
    - yaml.safe_load() が dict を返す
    - `contract_schema_version` が "v1" である
    - `issue_kind` が str である

    Returns:
        str | None: issue_kind 文字列、または見つからなかった場合 None
    """
    mrc_section = _extract_mrc_section(body)
    if not mrc_section:
        return None

    # MRC セクション内の fenced yaml ブロックを検索
    for match in re.finditer(r'```yaml\s*(.*?)```', mrc_section, re.DOTALL):
        block_text = match.group(1)
        try:
            data = yaml.safe_load(block_text)
            if not isinstance(data, dict):
                continue
            if data.get("contract_schema_version") != "v1":
                continue
            issue_kind = data.get("issue_kind")
            if isinstance(issue_kind, str) and issue_kind.strip():
                return issue_kind.strip()
        except yaml.YAMLError:
            continue
    return None


def resolve_template_dir() -> Path:
    """
    リポジトリルートを起点に .github/ISSUE_TEMPLATE ディレクトリを返す。
    スクリプトが .claude/skills/... にあることを考慮して複数候補を探す。
    """
    # スクリプト自体のパスを起点に探す
    script_path = Path(__file__).resolve()

    # git リポジトリルートを探す（.git ディレクトリを上向きに検索）
    current = script_path.parent
    for _ in range(10):
        if (current / ".git").exists():
            candidate = current / ".github" / "ISSUE_TEMPLATE"
            if candidate.is_dir():
                return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent

    raise FileNotFoundError(
        "Could not locate .github/ISSUE_TEMPLATE directory from script path"
    )


def _strip_fenced_code_blocks(text: str) -> str:
    """共有 Markdown block parser に委譲し、code fence のみ除外する。"""
    return ''.join(
        block_text
        for block_text, block_kind in _pbp.iter_markdown_blocks(text)
        if block_kind != _pbp.BLOCK_KIND_CODE_FENCE
    )


def guard_template(body: str, issue_kind: str, template_dir=None) -> dict:
    """
    ISSUE_TEMPLATE の validations.required: true ラベルを動的に取得して
    本文に `## <label>` 形式で存在するか確認する。

    コードブロック・引用内の偽陽性を防ぐため、fenced code block を除去した
    本文に対して行頭 Markdown 見出しとして正規表現でマッチする。

    Args:
        body: Issue 本文
        issue_kind: 種別（implementation / research / parent 等）
        template_dir: ISSUE_TEMPLATE ディレクトリ（省略時は自動検出）
    """
    if template_dir is None:
        template_dir = resolve_template_dir()

    try:
        required_labels = load_required_labels(template_dir, issue_kind)
    except FileNotFoundError as e:
        return {
            "name": "template_guard",
            "passed": False,
            "error": str(e),
            "missing_sections": [],
        }
    except ValueError as e:
        return {
            "name": "template_guard",
            "passed": False,
            "error": str(e),
            "missing_sections": [],
        }

    # fenced code block を除去した本文で行頭見出しを確認
    stripped_body = _strip_fenced_code_blocks(body)
    missing = []
    for label in required_labels:
        pattern = re.compile(
            rf'^##[ \t]+{re.escape(label)}[ \t]*$',
            re.MULTILINE
        )
        if not pattern.search(stripped_body):
            missing.append(f"## {label}")

    return {
        "name": "template_guard",
        "passed": len(missing) == 0,
        "missing_sections": missing,
    }


def extract_outcome_block(text: str) -> str:
    lines = text.splitlines()
    in_block = False
    block_lines = []
    for line in lines:
        if line.strip() == "## Outcome":
            in_block = True
            continue
        if in_block:
            if line.startswith("## "):
                break
            block_lines.append(line)
    return "\n".join(block_lines)


def guard_outcome_quality(body: str) -> dict:
    outcome_block = extract_outcome_block(body)
    ng_match = _OUTCOME_NG_RE.search(outcome_block)
    return {
        "name": "outcome_quality_guard",
        "passed": ng_match is None,
        "detail": f"NG pattern found: {ng_match.group(0)!r}" if ng_match else None,
    }


def guard_diff_threshold(orig_text: str, new_text: str) -> dict:
    orig_lines = len(orig_text.splitlines())
    new_lines = len(new_text.splitlines())
    diff_lines = orig_lines - new_lines
    threshold = orig_lines // 2
    passed = diff_lines <= threshold
    return {
        "name": "diff_threshold",
        "passed": passed,
        "orig_lines": orig_lines,
        "new_lines": new_lines,
        "diff_lines": diff_lines,
        "threshold": threshold,
    }


def _extract_vc_section(body: str) -> str:
    """
    本文から `## Verification Commands` セクションのテキストを返す。
    次の `## ` 見出しまでを切り出す。

    Returns:
        str: VC セクションのテキスト（セクションが存在しない場合は空文字列）
    """
    lines = body.splitlines()
    in_section = False
    section_lines = []
    for line in lines:
        if re.match(r'^##[ \t]+Verification Commands[ \t]*$', line):
            in_section = True
            continue
        if in_section:
            if re.match(r'^##[ \t]+', line):
                break
            section_lines.append(line)
    return "\n".join(section_lines)


def _extract_ac_section(body: str) -> str:
    """
    本文から `## Acceptance Criteria` セクションのテキストを返す。
    次の `## ` 見出しまで、または本文末尾までを切り出す。

    Returns:
        str: AC セクションのテキスト（セクションが存在しない場合は空文字列）
    """
    lines = body.splitlines()
    in_section = False
    section_lines = []
    for line in lines:
        if re.match(r'^##[ \t]+Acceptance Criteria[ \t]*$', line):
            in_section = True
            continue
        if in_section:
            if re.match(r'^##[ \t]+', line):
                break
            section_lines.append(line)
    return "\n".join(section_lines)


def guard_ac_vc_alignment(body: str, issue_kind: str, template_dir=None) -> dict:
    """
    `## Acceptance Criteria` セクション内の AC 番号集合と `## Verification Commands`
    セクション内 bash fence 内の # AC<N> 番号集合が一致するか確認する。
    issue_kind が VC セクションを必須に持たない種別（parent 等）では skipped: true を返す。

    「VC セクションを必須に持つ種別か」は ISSUE_TEMPLATE の required label に
    'Verification Commands' が含まれるかで動的に判定する（ハードコードしない）。

    抽出・比較の実処理は `_evaluate_ac_vc_alignment()` に一本化している
    （main() の issue_kind 未確定フォールバック分岐と共通利用するため）。
    """
    if template_dir is None:
        try:
            template_dir = resolve_template_dir()
        except FileNotFoundError:
            # テンプレートディレクトリが見つからない場合はスキップしない（安全側）
            template_dir = None

    # VC セクションが required かどうかを ISSUE_TEMPLATE から判定
    has_vc_required = False
    if template_dir is not None:
        try:
            required_labels = load_required_labels(template_dir, issue_kind)
            has_vc_required = "Verification Commands" in required_labels
        except (FileNotFoundError, ValueError):
            # テンプレートが見つからない場合はスキップしない（安全側）
            has_vc_required = True

    if not has_vc_required:
        return {
            "name": "ac_vc_alignment",
            "passed": True,
            "skipped": True,
            "reason": f"issue_kind '{issue_kind}' does not require Verification Commands section",
        }

    result = _evaluate_ac_vc_alignment(body)
    return {
        "name": "ac_vc_alignment",
        "passed": result["passed"],
        "skipped": False,
        "ac_count": result["ac_count"],
        "vc_ac_count": result["vc_ac_count"],
        "duplicate_ac_ids": result["duplicate_ac_ids"],
        "missing_in_vc": result["missing_in_vc"],
        "extra_in_vc": result["extra_in_vc"],
    }


_BASH_FENCE_RE = re.compile(r'```\s*bash\s*\n(.*?)\n```', re.DOTALL | re.IGNORECASE)

# inline # ACN suffix パターン（コマンド末尾の "  # AC1" や "  # AC1: comment" を抽出）
_INLINE_AC_RE = re.compile(r'\s+#\s*(AC\d+)\b.*$')


def _strip_inline_ac_and_extract(command_line: str) -> tuple:
    """
    コマンド行から inline # ACN suffix を除去し、(cleaned_command, ac_label) を返す。

    例: "grep -F 'foo' file  # AC1" → ("grep -F 'foo' file", "AC1")
    inline suffix がない場合: ("grep -F 'foo' file", None)
    """
    m = _INLINE_AC_RE.search(command_line)
    if m:
        ac_label = m.group(1)
        clean = command_line[:m.start()]
        return clean.strip(), ac_label
    return command_line.strip(), None


# AC 番号参照抽出用（grouped marker "# AC1, AC2" にも対応する findall パターン）
_AC_REF_RE = re.compile(r'AC(\d+)')


def _extract_vc_ac_occurrences(vc_section: str) -> list:
    """
    VC セクションの fenced bash block 内からのみ AC<N> 参照を抽出する
    （fence 外の `# AC1` 風コメントは VC として数えない）。

    以下の両方から AC 番号を抽出する（grouped marker・inline suffix 対応）:
    - コメント行（`#` で始まる行）: `# AC1` / `# AC1, AC2` / `# AC1: 説明` 等から
      全ての AC 番号を抽出する
    - コマンド行の inline suffix（`$ some_command  # AC1` のように行末に
      `#` 以降で AC 参照を付与するパターン）: `#` 以降の部分から全ての AC 番号を抽出する

    Returns:
        list[str]: 検出した AC 番号文字列のリスト（重複を許容する。VC 側は
                   同一 AC を複数コマンドから束ねる正当なパターンがあるため）
    """
    occurrences = []
    for match in _BASH_FENCE_RE.finditer(vc_section):
        block_text = match.group(1)
        for line in block_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('#'):
                occurrences.extend(_AC_REF_RE.findall(stripped))
                continue
            # inline suffix: コマンド行中の "  # ..." 以降から AC 参照を抽出する
            inline_match = re.search(r'\s+#(.*)$', stripped)
            if inline_match:
                occurrences.extend(_AC_REF_RE.findall(inline_match.group(1)))
    return occurrences


def _evaluate_ac_vc_alignment(body: str) -> dict:
    """
    AC-VC alignment の抽出・比較ロジック本体（共通関数）。

    `guard_ac_vc_alignment()`（issue_kind 解決済みパス）と main() 内の
    issue_kind 未確定時のフォールバック分岐の両方から呼ばれる（P2: 重複実装の統合）。

    - AC 番号は `## Acceptance Criteria` セクション境界内の `- [x] AC<N>` 形式からのみ
      抽出する（セクション外のチェックボックスは数えない）
    - VC 番号は `## Verification Commands` セクション内の bash fence 内からのみ抽出する
      （grouped marker・inline suffix 含む。fence 外のコメントは数えない）
    - AC 側の重複定義（同じ AC 番号が複数回定義されている異常系）は fail とする
      （B2: set() 化により意図せず許容してしまう副作用を修正）
    - VC 側の重複参照（1 つの AC を複数コマンドから束ねる正当なパターン）は許容する
    - AC/VC 双方が空の場合は set() == set() で自然に pass する
      （B3: ac_count == 0 の fail-open 特例は削除した）

    Returns:
        dict: {
            "passed": bool,
            "ac_count": int,
            "vc_ac_count": int,
            "duplicate_ac_ids": list[str],
            "missing_in_vc": list[str],
            "extra_in_vc": list[str],
        }
    """
    ac_section = _extract_ac_section(body)
    ac_occurrences = re.findall(r'^- \[.\] AC(\d+)\b', ac_section, re.MULTILINE)

    vc_section = _extract_vc_section(body)
    vc_occurrences = _extract_vc_ac_occurrences(vc_section)

    ac_counts = Counter(ac_occurrences)
    duplicate_ac_ids = sorted(
        (ac_id for ac_id, count in ac_counts.items() if count > 1),
        key=int,
    )

    ac_refs = set(ac_counts)
    vc_refs = set(vc_occurrences)

    missing_in_vc = sorted((ac_refs - vc_refs), key=int)
    extra_in_vc = sorted((vc_refs - ac_refs), key=int)

    passed = not duplicate_ac_ids and ac_refs == vc_refs

    return {
        "passed": passed,
        "ac_count": len(ac_occurrences),
        "vc_ac_count": len(vc_occurrences),
        "duplicate_ac_ids": duplicate_ac_ids,
        "missing_in_vc": missing_in_vc,
        "extra_in_vc": extra_in_vc,
    }


def _extract_bash_commands_from_vc_section(vc_section: str) -> list:
    """
    VC セクションの fenced bash block からコマンド行を抽出する。

    - ``` bash または ```bash で始まるブロックを対象とする
    - 空行・コメント行（# で始まる行）を除外する
    - 複数のフェンスブロックがある場合はすべて処理する
    - 直前の # AC<N> または # AC<N>: comment から ac_label を抽出する

    Returns:
        list[dict]: 各要素は {
            "ac_label": str | None,  # 直前の # AC<N> コメントから抽出。なければ None
            "line_number": int,      # vc_section 内の行番号（1-indexed）
            "command": str,          # コマンド文字列
        }
    """
    result = []
    _lines = vc_section.splitlines()
    # vc_section 全体での行番号マッピングを作成
    for match in _BASH_FENCE_RE.finditer(vc_section):
        block_text = match.group(1)
        # match.start() からブロック開始行を計算（1-indexed）
        block_start_offset = vc_section[:match.start()].count('\n') + 1  # fence 開始行
        # fence 行 + \n の分 +1 してブロック本体開始行
        content_start_line = block_start_offset + 1

        block_lines = block_text.splitlines()
        current_ac_label = None
        for i, line in enumerate(block_lines):
            stripped = line.strip()
            line_number = content_start_line + i
            if not stripped:
                continue
            if stripped.startswith('#'):
                # # AC<N> または # AC<N>: ... 形式のコメントを ac_label として記録
                ac_match = re.match(r'^#\s*(AC\d+)', stripped)
                if ac_match:
                    current_ac_label = ac_match.group(1)
                continue
            # inline # ACN suffix の処理（直前行 ac_label がない場合のフォールバック）
            cleaned_command, inline_ac_label = _strip_inline_ac_and_extract(stripped)
            effective_ac_label = current_ac_label if current_ac_label is not None else inline_ac_label
            result.append({
                "ac_label": effective_ac_label,
                "line_number": line_number,
                "command": cleaned_command,
            })
    return result


# 既知の shell operator の exact set
_EXACT_OPERATORS = frozenset({"&&", "||", "|", ";", "&", "<<", "<", ">", ">>", "<<<"})

# punctuation run に含まれる shell operator 文字
_SHELL_OP_CHARS = frozenset("><|&;")


def _is_shell_operator_token(token: str) -> bool:
    """
    shlex punctuation run token が shell operator を含むか判定する。

    shlex.shlex(punctuation_chars=True) は ();<>|& の連続を単一 punctuation run token
    として返す。例: `2>&1` → tokens: ['2', '>&', '1'] の `>&` が該当する。

    - exact match: 既知の operator セットに含まれる場合
    - punctuation run: 全文字が ();<>|& かつ、shell operator 文字を含む場合
    """
    # exact match: 既知の operator
    if token in _EXACT_OPERATORS:
        return True
    # punctuation run: 全文字が ();<>|& かつ、shell operator 文字を含む
    # 例: >&, &>, |&, 2>&1 分割後の >&
    if token and all(c in "();<>|&" for c in token) and any(c in _SHELL_OP_CHARS for c in token):
        return True
    return False


def _detect_compound_operator(command: str):
    """
    コマンドが compound shell syntax を含むか検出し、最初の違反 operator を返す。

    shlex.shlex で正確に tokenize し、shell operator を検出する。
    - quoted operator（例: grep -E "foo|bar" file）は誤検出しない
    - parse error は fail-closed で compound と見なす（operator="_parse_error" を返す）
    - punctuation run token（例: >&, &>, |&）も検出する

    Returns:
        str | None: 最初に検出した違反 operator。違反なしの場合は None。
                    parse error 時は "_parse_error" を返す。
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        tokens = list(lexer)
    except ValueError:
        # parse 失敗 = 複雑なコマンド = fail-closed で compound と見なす
        return "_parse_error"

    for t in tokens:
        if _is_shell_operator_token(t):
            return t
    return None


def _is_compound_command(command: str) -> bool:
    """
    コマンドが compound shell syntax を含むか検出する。
    """
    return _detect_compound_operator(command) is not None


# Implementation issue ready tuple constants
_IMPLEMENTATION_TITLE_PREFIXES = ("実装:", "implement:")
_IMPLEMENTATION_REQUIRED_LABEL = "phase/implementation"


def check_ready_tuple(
    issue_kind,
    title: str,
    label_names: list,
) -> list:
    """Check implementation issue ready tuple (title prefix + phase/implementation label).

    Args:
        issue_kind: The issue kind (e.g. 'implementation'). Only validates when 'implementation'.
        title: The issue title string.
        label_names: List of label name strings on the issue.

    Returns:
        list[str]: List of error message strings. Empty list means PASS.
    """
    if issue_kind != "implementation":
        return []

    errors: list = []

    if not title.startswith(_IMPLEMENTATION_TITLE_PREFIXES):
        errors.append(
            f"implementation issue title must start with '実装:' or 'implement:'. Got: {title!r}"
        )

    if _IMPLEMENTATION_REQUIRED_LABEL not in set(label_names):
        errors.append(
            f"implementation issue must have label '{_IMPLEMENTATION_REQUIRED_LABEL}'"
        )

    return errors


def guard_ready_tuple(issue_kind, title: str, label_names: list) -> dict:
    """Guard check for implementation issue ready tuple.

    Returns a guard result dict integrated with the existing checks array schema:
        {name: "ready_tuple", passed: bool, errors: [...]}

    This guard only applies to implementation kind issues.
    For other kinds, passed=True with empty errors list.
    """
    errors = check_ready_tuple(issue_kind, title, label_names)
    return {
        "name": "ready_tuple",
        "passed": len(errors) == 0,
        "errors": errors,
    }


def guard_vc_compound_shell_disallowed(body: str) -> dict:
    """
    ## Verification Commands セクションの fenced bash block から
    compound shell syntax を含むコマンドを検出する。

    違反 1 件以上で passed=False を返す。

    Returns:
        dict: {
            "name": "vc_compound_shell_disallowed",
            "passed": bool,
            "violations": list[dict],  # 構造化された違反情報のリスト
        }

    violations の各要素:
        {
            "ac_label": str | None,   # 直前の # AC<N> コメントから抽出。なければ None
            "line_number": int,        # Issue body 内の行番号（1-indexed）
            "command": str,            # 違反コマンド文字列
            "category": "compound_command_disallowed",
            "operator": str,           # 最初に検出した違反 operator
        }
    """
    vc_section = _extract_vc_section(body)
    # vc_section の開始行番号を body 内から計算（1-indexed）
    vc_section_start_line = 0
    for i, line in enumerate(body.splitlines(), 1):
        if re.match(r'^##[ \t]+Verification Commands[ \t]*$', line):
            vc_section_start_line = i
            break

    entries = _extract_bash_commands_from_vc_section(vc_section)

    violations = []
    for entry in entries:
        operator = _detect_compound_operator(entry["command"])
        if operator is not None:
            violations.append({
                "ac_label": entry["ac_label"],
                "line_number": vc_section_start_line + entry["line_number"],
                "command": entry["command"],
                "category": "compound_command_disallowed",
                "operator": operator,
            })

    return {
        "name": "vc_compound_shell_disallowed",
        "passed": len(violations) == 0,
        "violations": violations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Guard checks for GitHub Issue body files"
    )
    parser.add_argument(
        "body_file",
        type=validate_path,
        help="Path to new body file (safe chars only)"
    )
    parser.add_argument(
        "--orig-file",
        dest="orig_file",
        type=validate_path,
        default=None,
        help="Path to original body file for diff threshold check"
    )
    parser.add_argument(
        "--format",
        choices=["yaml", "json"],
        default="yaml",
        help="Output format (default: yaml)"
    )
    parser.add_argument(
        "--issue-kind",
        dest="issue_kind",
        choices=["implementation", "research", "parent"],
        default=None,
        help="Issue kind for template-based guard (implementation/research/parent)"
    )
    parser.add_argument(
        "--check-ready-tuple",
        dest="check_ready_tuple",
        action="store_true",
        default=False,
        help=(
            "When set, adds a ready_tuple guard check: verifies that implementation issues "
            "have correct title prefix ('実装:' or 'implement:') and 'phase/implementation' label. "
            "Requires --title and --label options when checking implementation kind."
        )
    )
    parser.add_argument(
        "--title",
        dest="title",
        type=str,
        default=None,
        help="Issue title (used with --check-ready-tuple for ready_tuple guard)"
    )
    parser.add_argument(
        "--label",
        dest="labels",
        action="append",
        default=[],
        metavar="LABEL",
        help="Issue label name (may be specified multiple times; used with --check-ready-tuple)"
    )
    parser.add_argument(
        "--readback-json",
        dest="readback_json",
        type=validate_path,
        default=None,
        help=(
            "Path to JSON file produced by 'gh issue view --json title,labels'. "
            "Path must match ^[A-Za-z0-9._/-]+$ (safe chars only). "
            "When provided with --check-ready-tuple, extracts title and label names from the file "
            "instead of requiring --title and --label arguments."
        )
    )
    args = parser.parse_args()

    # Resolve title and labels for --check-ready-tuple:
    # --readback-json takes precedence over --title / --label when both are provided.
    rt_title: str = args.title or ""
    rt_labels: list = list(args.labels)
    if args.readback_json is not None:
        # args.readback_json is already a Path (validated by validate_path type=)
        rb_path: Path = args.readback_json
        try:
            rb_raw = rb_path.read_text(encoding="utf-8")
            rb_data = json.loads(rb_raw)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"ERROR: Failed to parse --readback-json: {exc}", file=sys.stderr)
            sys.exit(1)

        # Schema validation: root must be dict
        if not isinstance(rb_data, dict):
            # Return guard result instead of hard exit so callers get structured output
            _rb_error_result = {
                "name": "ready_tuple",
                "passed": False,
                "errors": [f"malformed readback JSON: root must be a dict, got {type(rb_data).__name__!r}"],
            }
            output = {"all_passed": False, "guards": [_rb_error_result]}
            if hasattr(args, 'format') and args.format == "json":
                print(json.dumps(output, ensure_ascii=False, indent=2))
            else:
                print("all_passed: false")
                print("guards:")
                print("  - name: ready_tuple")
                print("    passed: false")
                print("    errors:")
                print(f"      - {_rb_error_result['errors'][0]!r}")
            sys.exit(2)

        # title must be str (missing treated as empty string)
        rb_title_raw = rb_data.get("title", "")
        if rb_title_raw is not None and not isinstance(rb_title_raw, str):
            _rb_error_result = {
                "name": "ready_tuple",
                "passed": False,
                "errors": [f"malformed readback JSON: 'title' must be str, got {type(rb_title_raw).__name__!r}"],
            }
            output = {"all_passed": False, "guards": [_rb_error_result]}
            if hasattr(args, 'format') and args.format == "json":
                print(json.dumps(output, ensure_ascii=False, indent=2))
            else:
                print("all_passed: false")
                print("guards:")
                print("  - name: ready_tuple")
                print("    passed: false")
                print("    errors:")
                print(f"      - {_rb_error_result['errors'][0]!r}")
            sys.exit(2)

        # labels must be list
        rb_labels_raw = rb_data.get("labels", [])
        if not isinstance(rb_labels_raw, list):
            _rb_error_result = {
                "name": "ready_tuple",
                "passed": False,
                "errors": [f"malformed readback JSON: 'labels' must be list, got {type(rb_labels_raw).__name__!r}"],
            }
            output = {"all_passed": False, "guards": [_rb_error_result]}
            if hasattr(args, 'format') and args.format == "json":
                print(json.dumps(output, ensure_ascii=False, indent=2))
            else:
                print("all_passed: false")
                print("guards:")
                print("  - name: ready_tuple")
                print("    passed: false")
                print("    errors:")
                print(f"      - {_rb_error_result['errors'][0]!r}")
            sys.exit(2)

        # each label must be dict with name: str
        for i, lbl in enumerate(rb_labels_raw):
            if not isinstance(lbl, dict):
                _rb_error_result = {
                    "name": "ready_tuple",
                    "passed": False,
                    "errors": [f"malformed readback JSON: labels[{i}] must be dict, got {type(lbl).__name__!r}"],
                }
                output = {"all_passed": False, "guards": [_rb_error_result]}
                if hasattr(args, 'format') and args.format == "json":
                    print(json.dumps(output, ensure_ascii=False, indent=2))
                else:
                    print("all_passed: false")
                    print("guards:")
                    print("  - name: ready_tuple")
                    print("    passed: false")
                    print("    errors:")
                    print(f"      - {_rb_error_result['errors'][0]!r}")
                sys.exit(2)
            if not isinstance(lbl.get("name"), str):
                _rb_error_result = {
                    "name": "ready_tuple",
                    "passed": False,
                    "errors": [
                        f"malformed readback JSON: labels[{i}].name must be str,"
                        f" got {type(lbl.get('name')).__name__!r}"
                    ],
                }
                output = {"all_passed": False, "guards": [_rb_error_result]}
                if hasattr(args, 'format') and args.format == "json":
                    print(json.dumps(output, ensure_ascii=False, indent=2))
                else:
                    print("all_passed: false")
                    print("guards:")
                    print("  - name: ready_tuple")
                    print("    passed: false")
                    print("    errors:")
                    print(f"      - {_rb_error_result['errors'][0]!r}")
                sys.exit(2)

        rt_title = rb_title_raw or ""
        rt_labels = [lbl["name"] for lbl in rb_labels_raw]

    body = args.body_file.read_text(encoding="utf-8")

    # issue_kind 解決: (1) --issue-kind 引数 → (2) 本文 MRC の issue_kind → (3) fail
    issue_kind = args.issue_kind
    if issue_kind is None:
        issue_kind = extract_issue_kind_from_body(body)

    if issue_kind is None:
        # issue_kind を解決できない場合は template_guard を fail
        results = [
            {
                "name": "template_guard",
                "passed": False,
                "error": (
                    "Cannot determine issue_kind. "
                    "Provide --issue-kind argument or include issue_kind in Machine-Readable Contract block."
                ),
                "missing_sections": [],
            }
        ]
        results.append(guard_outcome_quality(body))

        # 共通バリデータ結果を常時先頭寄りに追加（kind 未確定でも validator を実行）
        results.append(guard_issue_body_validation(args.body_file))

        if args.orig_file is not None:
            orig_body = args.orig_file.read_text(encoding="utf-8")
            results.append(guard_diff_threshold(orig_body, body))

        # ac_vc_alignment は issue_kind 不明のためスキップしない（安全側）。
        # 抽出・比較ロジックは _evaluate_ac_vc_alignment() に一本化している
        # （P2: guard_ac_vc_alignment() との重複実装を解消）。
        _alignment_result = _evaluate_ac_vc_alignment(body)
        results.append({
            "name": "ac_vc_alignment",
            "passed": _alignment_result["passed"],
            "skipped": False,
            "ac_count": _alignment_result["ac_count"],
            "vc_ac_count": _alignment_result["vc_ac_count"],
            "duplicate_ac_ids": _alignment_result["duplicate_ac_ids"],
            "missing_in_vc": _alignment_result["missing_in_vc"],
            "extra_in_vc": _alignment_result["extra_in_vc"],
        })
        results.append(guard_vc_compound_shell_disallowed(body))
        # ready_tuple guard (when --check-ready-tuple is set)
        if args.check_ready_tuple:
            results.append(guard_ready_tuple(issue_kind, rt_title, rt_labels))
    else:
        results = []
        results.append(guard_template(body, issue_kind))
        results.append(guard_outcome_quality(body))
        results.append(guard_issue_body_validation(args.body_file, issue_kind))

        if args.orig_file is not None:
            orig_body = args.orig_file.read_text(encoding="utf-8")
            results.append(guard_diff_threshold(orig_body, body))

        results.append(guard_ac_vc_alignment(body, issue_kind))
        results.append(guard_vc_compound_shell_disallowed(body))
        # ready_tuple guard (when --check-ready-tuple is set)
        if args.check_ready_tuple:
            results.append(guard_ready_tuple(issue_kind, rt_title, rt_labels))

    all_passed = all(r["passed"] for r in results)
    output = {
        "all_passed": all_passed,
        "guards": results,
    }

    if args.format == "json":
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        # Simple YAML-like output (no external dependency beyond PyYAML)
        print(f"all_passed: {str(all_passed).lower()}")
        print("guards:")
        for r in results:
            # support both 'name' key (legacy guards) and 'check' key (vc_compound_shell_disallowed)
            guard_id = r.get('name') or r.get('check', 'unknown')
            print(f"  - name: {guard_id}")
            print(f"    passed: {str(r['passed']).lower()}")
            for k, v in r.items():
                if k in ("name", "check", "passed"):
                    continue
                if v is None:
                    print(f"    {k}: null")
                elif isinstance(v, bool):
                    print(f"    {k}: {str(v).lower()}")
                elif isinstance(v, list):
                    if v:
                        print(f"    {k}:")
                        for item in v:
                            print(f"      - {item!r}")
                    else:
                        print(f"    {k}: []")
                else:
                    print(f"    {k}: {v}")

    sys.exit(0 if all_passed else 2)


if __name__ == "__main__":
    main()
