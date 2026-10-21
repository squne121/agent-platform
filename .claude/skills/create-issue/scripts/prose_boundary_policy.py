"""
prose_boundary_policy.py

Markdown ブロック種別分類の SSOT（Single Source of Truth）。
block_kind enum と classify API を提供する。

block_kind 一覧（AC1 固定契約・変更禁止）:
  human_prose           人間が書いた自然文 prose
  canonical_heading     ## Outcome 等の標準見出し
  bilingual_heading     日英混在の見出し
  machine_contract      Machine-Readable Contract ブロック（YAML fence 等）
  yaml_machine_line     機械可読 YAML 行が支配的なブロック
  vc_command            Verification Commands 内のコマンドブロック
  shell_command         シェルコマンドブロック（$ / # prefix）
  code_fence            ``` / ~~~ フェンスで囲まれたコードブロック
  url_or_identifier     URL・識別子のみの行
  table                 GFM パイプテーブル（Safety Claim Matrix 等）

Out of scope:
  body source / route 分類（child-2 以降で扱う）はこのモジュールの block_kind に含めない。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# block_kind 定数（AC1 固定契約）
# ---------------------------------------------------------------------------

BLOCK_KIND_HUMAN_PROSE = "human_prose"
BLOCK_KIND_CANONICAL_HEADING = "canonical_heading"
BLOCK_KIND_BILINGUAL_HEADING = "bilingual_heading"
BLOCK_KIND_MACHINE_CONTRACT = "machine_contract"
BLOCK_KIND_YAML_MACHINE_LINE = "yaml_machine_line"
BLOCK_KIND_VC_COMMAND = "vc_command"
BLOCK_KIND_SHELL_COMMAND = "shell_command"
BLOCK_KIND_CODE_FENCE = "code_fence"
BLOCK_KIND_URL_OR_IDENTIFIER = "url_or_identifier"
BLOCK_KIND_TABLE = "table"

# すべての block_kind の集合（型検証・テスト用）
ALL_BLOCK_KINDS: frozenset[str] = frozenset({
    BLOCK_KIND_HUMAN_PROSE,
    BLOCK_KIND_CANONICAL_HEADING,
    BLOCK_KIND_BILINGUAL_HEADING,
    BLOCK_KIND_MACHINE_CONTRACT,
    BLOCK_KIND_YAML_MACHINE_LINE,
    BLOCK_KIND_VC_COMMAND,
    BLOCK_KIND_SHELL_COMMAND,
    BLOCK_KIND_CODE_FENCE,
    BLOCK_KIND_URL_OR_IDENTIFIER,
    BLOCK_KIND_TABLE,
})

# ---------------------------------------------------------------------------
# heading_policy inventory（#654 追加データ）
#
# 各 entry は以下のフィールドを持つ:
#   canonical_en   : 英語見出し名（## の後のテキスト）
#   canonical_ja   : 標準的な日本語訳（空文字 = 翻訳なし）
#   accepted_forms : 許容される見出し形式のリスト（normalize 後マッチ）
#   prose_guard_kind  : validate_japanese_content が使う block_kind
#   contract_checker_kind : check_issue_contract が section 抽出に使う key
#
# この inventory を追加しても classify_block() 公開 API と既存 block_kind
# 定数の意味は変更しない（AC1 固定契約）。
# ---------------------------------------------------------------------------


def _normalize_heading_text(text: str) -> str:
    """
    GFM ATX heading のテキスト部分を正規化する。

    - 先頭・末尾の空白を除去
    - 末尾の closing `#` を除去（GFM 仕様: 末尾 # はオプション）
    - 括弧内テキストを正規化（半角・全角括弧を統一して比較用に保持）
    - 正規化後のテキストを返す
    """
    t = text.strip()
    # 末尾 closing # を除去（GFM: heading text の末尾スペース + # は無視）
    t = re.sub(r'\s+#+\s*$', '', t).strip()
    return t


# heading_policy inventory
# key = canonical_en（正規化後の英語見出し名）
HEADING_POLICY: dict[str, dict] = {
    "Summary": {
        "canonical_en": "Summary",
        "canonical_ja": "要約",
        "accepted_forms": [
            "Summary",
            "要約 (Summary)",
            "要約（Summary）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Summary",
    },
    "Checks": {
        "canonical_en": "Checks",
        "canonical_ja": "確認事項",
        "accepted_forms": [
            "Checks",
            "確認事項 (Checks)",
            "確認事項（Checks）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Checks",
    },
    "Schema Change Applicability": {
        "canonical_en": "Schema Change Applicability",
        "canonical_ja": "スキーマ変更適用性",
        "accepted_forms": [
            "Schema Change Applicability",
            "スキーマ変更適用性 (Schema Change Applicability)",
            "スキーマ変更適用性（Schema Change Applicability）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Schema Change Applicability",
    },
    "Schema Consumer Inventory": {
        "canonical_en": "Schema Consumer Inventory",
        "canonical_ja": "スキーマ利用箇所一覧",
        "accepted_forms": [
            "Schema Consumer Inventory",
            "スキーマ利用箇所一覧 (Schema Consumer Inventory)",
            "スキーマ利用箇所一覧（Schema Consumer Inventory）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Schema Consumer Inventory",
    },
    "Safety Claim Matrix": {
        "canonical_en": "Safety Claim Matrix",
        "canonical_ja": "安全性主張マトリクス",
        "accepted_forms": [
            "Safety Claim Matrix",
            "安全性主張マトリクス (Safety Claim Matrix)",
            "安全性主張マトリクス（Safety Claim Matrix）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Safety Claim Matrix",
    },
    "Notes": {
        "canonical_en": "Notes",
        "canonical_ja": "補足",
        "accepted_forms": [
            "Notes",
            "補足 (Notes)",
            "補足（Notes）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Notes",
    },
    "Machine-Readable Contract": {
        "canonical_en": "Machine-Readable Contract",
        "canonical_ja": "機械可読コントラクト",
        "accepted_forms": [
            "Machine-Readable Contract",
            "機械可読コントラクト (Machine-Readable Contract)",
            "機械可読コントラクト（Machine-Readable Contract）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Machine-Readable Contract",
    },
    "Parent Issue": {
        "canonical_en": "Parent Issue",
        "canonical_ja": "親 Issue",
        "accepted_forms": [
            "Parent Issue",
            "親 Issue (Parent Issue)",
            "親 Issue（Parent Issue）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Parent Issue",
    },
    "Parent Goal Ref": {
        "canonical_en": "Parent Goal Ref",
        "canonical_ja": "親ゴール参照",
        "accepted_forms": [
            "Parent Goal Ref",
            "親ゴール参照 (Parent Goal Ref)",
            "親ゴール参照（Parent Goal Ref）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Parent Goal Ref",
    },
    "Current Validated Scope": {
        "canonical_en": "Current Validated Scope",
        "canonical_ja": "現在の検証済みスコープ",
        "accepted_forms": [
            "Current Validated Scope",
            "現在の検証済みスコープ (Current Validated Scope)",
            "現在の検証済みスコープ（Current Validated Scope）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Current Validated Scope",
    },
    "Remaining Parent Gaps": {
        "canonical_en": "Remaining Parent Gaps",
        "canonical_ja": "残存ギャップ",
        "accepted_forms": [
            "Remaining Parent Gaps",
            "残存ギャップ (Remaining Parent Gaps)",
            "残存ギャップ（Remaining Parent Gaps）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Remaining Parent Gaps",
    },
    "Outcome": {
        "canonical_en": "Outcome",
        "canonical_ja": "成果物",
        "accepted_forms": [
            "Outcome",
            "成果物 (Outcome)",
            "成果物（Outcome）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Outcome",
    },
    "Background": {
        "canonical_en": "Background",
        "canonical_ja": "背景",
        "accepted_forms": [
            "Background",
            "背景 (Background)",
            "背景（Background）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Background",
    },
    "In Scope": {
        "canonical_en": "In Scope",
        "canonical_ja": "スコープ内",
        "accepted_forms": [
            "In Scope",
            "スコープ内 (In Scope)",
            "スコープ内（In Scope）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "In Scope",
    },
    "Out of Scope": {
        "canonical_en": "Out of Scope",
        "canonical_ja": "スコープ外",
        "accepted_forms": [
            "Out of Scope",
            "スコープ外 (Out of Scope)",
            "スコープ外（Out of Scope）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Out of Scope",
    },
    "Acceptance Criteria": {
        "canonical_en": "Acceptance Criteria",
        "canonical_ja": "受け入れ条件",
        "accepted_forms": [
            "Acceptance Criteria",
            "受け入れ条件 (Acceptance Criteria)",
            "受け入れ条件（Acceptance Criteria）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Acceptance Criteria",
    },
    "Verification Commands": {
        "canonical_en": "Verification Commands",
        "canonical_ja": "検証コマンド",
        "accepted_forms": [
            "Verification Commands",
            "検証コマンド (Verification Commands)",
            "検証コマンド（Verification Commands）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Verification Commands",
    },
    "Allowed Paths": {
        "canonical_en": "Allowed Paths",
        "canonical_ja": "許可パス",
        "accepted_forms": [
            "Allowed Paths",
            "許可パス (Allowed Paths)",
            "許可パス（Allowed Paths）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Allowed Paths",
    },
    "Stop Conditions": {
        "canonical_en": "Stop Conditions",
        "canonical_ja": "停止条件",
        "accepted_forms": [
            "Stop Conditions",
            "停止条件 (Stop Conditions)",
            "停止条件（Stop Conditions）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Stop Conditions",
    },
    "Required Skills": {
        "canonical_en": "Required Skills",
        "canonical_ja": "必要スキル",
        "accepted_forms": [
            "Required Skills",
            "必要スキル (Required Skills)",
            "必要スキル（Required Skills）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Required Skills",
    },
    # Scope Delta は implementation template の任意セクション（required: false）。
    # GitHub template の label は "Scope Delta（任意）" だが、
    # Issue 本文中の heading テキストは "Scope Delta（任意）" または "Scope Delta" として現れる。
    # 両形式を accepted_forms に含め prose ratio 判定から除外する。
    "Scope Delta": {
        "canonical_en": "Scope Delta",
        "canonical_ja": "スコープ差分",
        "accepted_forms": [
            "Scope Delta",
            "Scope Delta（任意）",
            "スコープ差分 (Scope Delta)",
            "スコープ差分（Scope Delta）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Scope Delta",
    },
    "Runtime Verification Applicability": {
        "canonical_en": "Runtime Verification Applicability",
        "canonical_ja": "実行時検証適用性",
        "accepted_forms": [
            "Runtime Verification Applicability",
            "実行時検証適用性 (Runtime Verification Applicability)",
            "実行時検証適用性（Runtime Verification Applicability）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Runtime Verification Applicability",
    },
    "Required Design References": {
        "canonical_en": "Required Design References",
        "canonical_ja": "必要設計リファレンス",
        "accepted_forms": [
            "Required Design References",
            "必要設計リファレンス (Required Design References)",
            "必要設計リファレンス（Required Design References）",
        ],
        "prose_guard_kind": BLOCK_KIND_CANONICAL_HEADING,
        "contract_checker_kind": "Required Design References",
    },
}

# canonical_en のセット（高速ルックアップ用）
_CANONICAL_HEADING_KEYS: frozenset[str] = frozenset(HEADING_POLICY.keys())


def lookup_heading_policy(heading_text: str) -> dict | None:
    """
    heading text（## より後のテキスト）から heading_policy entry を引く。

    B2 fix (#654): accepted_forms の exact normalized match のみで accept する。
    任意 prefix + (CanonicalEnglish) の括弧内キー単独一致では accept しない。
    これにより「適当な日本語 (Outcome)」「成果物ではない（Outcome）」等が
    誤って Outcome として HIT する問題を修正する。

    手順:
    1. _normalize_heading_text() で正規化
    2. canonical_en（= HEADING_POLICY key）との直接一致を確認
    3. accepted_forms（正規化後）との exact match を確認

    Returns:
        HEADING_POLICY entry dict、または None（inventory に存在しない場合）
    """
    normalized = _normalize_heading_text(heading_text)

    # 直接一致（canonical_en = HEADING_POLICY key）
    if normalized in HEADING_POLICY:
        return HEADING_POLICY[normalized]

    # accepted_forms との exact normalized match のみ（B2 fix: bilingual key bypass 除去）
    for entry in HEADING_POLICY.values():
        if any(_normalize_heading_text(form) == normalized for form in entry["accepted_forms"]):
            return entry

    return None


# ---------------------------------------------------------------------------
# level-2 canonical section extraction（#2782: single authority for section
# boundary determination, shared by baseline_vc_preflight.py's
# extract_verification_commands_section() and contract_readiness_check.py's
# _extract_section_by_canonical_name()）
# ---------------------------------------------------------------------------


def _fenced_line_indices(body: str) -> frozenset[int]:
    """
    body 内の fenced code block（``` / ~~~）が占有する 0-based 行インデックス集合を返す。

    iter_markdown_blocks() の GFM 準拠 fence 判定（indent 0-3、backtick/tilde
    種別・長さ一致の closing、未閉 fence は EOF まで code とみなす）をそのまま
    再利用する。section 抽出の見出し境界判定が fenced code block 内の擬似
    `##` 行を見出しとして誤認しないようにするための補助 index。
    """
    indices: set[int] = set()
    line_index = 0
    for block_text, block_kind in iter_markdown_blocks(body):
        line_count = len(block_text.splitlines(keepends=True))
        if block_kind == BLOCK_KIND_CODE_FENCE:
            indices.update(range(line_index, line_index + line_count))
        line_index += line_count
    return frozenset(indices)


def extract_level2_section_with_bounds(
    body: str, canonical_en: str
) -> tuple[str, int, int] | None:
    """
    body から canonical_en に対応する level-2（``##``）見出しセクションを、
    本文テキストと 0-based の [start_index, end_index) 行範囲つきで抽出する。

    section boundary algorithm の単一 authority（#2782）。
    `baseline_vc_preflight.py::extract_verification_commands_section()` と
    `contract_readiness_check.py::_extract_section_by_canonical_name()` は
    いずれもこの関数（または `extract_level2_section()`）へ委譲し、
    見出し境界判定ロジックの独立コピーを持たない。

    判定規則:
      - fenced code block 内の行は見出し候補として扱わない
        （``_fenced_line_indices`` で fence 占有行を事前に除外）
      - 見出し行の解析・GFM closing-hash 除去・indent 許容は
        ``parse_atx_heading_line()`` に委譲する
      - 見出しテキストの canonical 名解決（日英表記ゆれ・括弧付き表記含む）は
        ``lookup_heading_policy()`` に委譲する
      - target セクションの終端は、target セクション内で最初に現れる
        「fenced code block 外の level-2（``##``）見出し」（canonical 名が
        一致するか否かを問わない）とする。level-3 以降の nested 見出し
        （例: ``### Runtime checks``）はセクションを終端させない

    Returns:
        None                        -- canonical_en に一致する見出しが存在しない
        (text, start_index, end_index) -- 見出しが存在する場合。
          text は見出し行を含まない本文（末尾の次見出し直前まで、raw）。
          start_index は本文の開始行（見出し行の次の行）、
          end_index は本文終了直後の行（次の level-2 見出し行、または EOF）。
          text が空/空白のみでも tuple は返す（emptiness 判定は
          ``extract_level2_section()`` の呼び出し側が行う）。
    """
    lines = body.splitlines(keepends=True)
    fenced = _fenced_line_indices(body)

    for start_index, line in enumerate(lines):
        if start_index in fenced:
            continue
        heading = parse_atx_heading_line(line.rstrip("\r\n"))
        if heading is None or heading["level"] != 2:
            continue
        policy = lookup_heading_policy(heading["text"])
        if not policy or policy.get("canonical_en") != canonical_en:
            continue

        end_index = len(lines)
        for index in range(start_index + 1, len(lines)):
            if index in fenced:
                continue
            boundary = parse_atx_heading_line(lines[index].rstrip("\r\n"))
            if boundary is not None and boundary["level"] == 2:
                end_index = index
                break
        return "".join(lines[start_index + 1:end_index]), start_index + 1, end_index

    return None


def extract_level2_section(body: str, canonical_en: str) -> str | None:
    """
    body から canonical_en に対応する level-2（``##``）見出しセクションの
    本文テキストのみを抽出する（`extract_level2_section_with_bounds()` の
    薄いラッパー。行範囲が不要な consumer 向け）。

    Return contract（#2782 AC4 — 呼び出し側は必ずこの 3 値を区別すること）:
      None            -- heading 不在（canonical_en に一致する ``##`` 見出しが
                          body に存在しない）
      ""              -- heading 存在するが本文が空、または空白のみ
      non-empty str   -- heading 存在し、本文に内容がある（raw text。
                          先頭に見出し直後の改行を含みうる）

    presence のみを判定する consumer は必ず ``is not None`` を使うこと。
    truthiness / ``bool()`` は "" と None を区別できないため使用しない
    （#2782 P1 — PR #2780 OWNER review comment F2 の指摘）。
    """
    result = extract_level2_section_with_bounds(body, canonical_en)
    if result is None:
        return None
    text, _start_index, _end_index = result
    if not text.strip():
        return ""
    return text


# ---------------------------------------------------------------------------
# GFM ATX heading parser（B2: #654）
# ---------------------------------------------------------------------------

# GFM ATX heading: 0-3 leading spaces, #{1,6}, at least one space, heading text
# trailing "空白 + #{1,} + trailing spaces" is optional closing sequence
_ATX_HEADING_RE = re.compile(
    r'^( {0,3})(#{1,6})(?:\s+(.+?))?(?:\s+#+\s*)?$'
)


def parse_atx_heading_line(line: str) -> dict | None:
    """
    1 行を GFM ATX heading として解析し、結果を返す。

    GFM ATX heading 仕様（CommonMark §4.2）:
      - 先頭 0〜3 スペースの indent が許容される（4 スペース以上は code block）
      - marker は #{1,6}
      - marker の後に少なくとも 1 つの空白、またはそれ以降が空（空見出し）
      - 末尾の「空白 + #{1,} + trailing spaces」は closing sequence として除去
      - closing sequence は heading text の一部として扱わない

    Returns:
        dict with keys:
          'level': int (1-6)
          'text':  str  (正規化後の heading text)
        または None（ATX heading でない場合）
    """
    m = _ATX_HEADING_RE.match(line)
    if not m:
        return None
    indent = m.group(1) or ''
    # 4 spaces 以上は code block（code block ではない ATX heading ではない）
    # ここでは 0-3 spaces のみ許可
    if len(indent) >= 4:
        return None
    level = len(m.group(2))
    raw_text = m.group(3) or ''
    # closing sequence 除去（末尾の空白 + # + 空白）
    text = re.sub(r'\s+#+\s*$', '', raw_text).strip()
    return {'level': level, 'text': text}


# ---------------------------------------------------------------------------
# 内部正規表現
# ---------------------------------------------------------------------------

# Markdown 見出し（後方互換用; classify_block 内では parse_atx_heading_line を使用）
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$')

# 日本語文字（ひらがな・カタカナ・CJK）
_JAPANESE_RE = re.compile(r'[぀-ゟ゠-ヿ一-鿿]')

# 技術識別子（snake_case / kebab-case / file.ext）
_IDENTIFIER_RE = re.compile(r'\b[a-zA-Z][a-zA-Z0-9]*(?:[_.\-][a-zA-Z0-9._\-]*)+\b')

# URL
_URL_RE = re.compile(r'https?://\S+')

# インラインコード
_INLINE_CODE_RE = re.compile(r'`[^`\n]+`')

# Markdown リンク
_MD_LINK_URL_RE = re.compile(r'\[([^\]]*)\]\([^\)]+\)')

# 空白
_WHITESPACE_RE = re.compile(r'\s+')

# CLI コマンド行（$ / # prefix）
_CLI_LINE_RE = re.compile(r'^\s*[$#]\s+\S.*$', re.MULTILINE)

# Issue/PR/SHA/パス参照のみ行パターン
# 例: #123, GH-123, abc1234567890123456789012345678901234567890, path/to/file
_REF_ONLY_LINE_RE = re.compile(
    r'^(?:'
    r'#\d+'                              # #123 Issue/PR
    r'|GH-\d+'                           # GH-123
    r'|[0-9a-f]{7,40}'                   # SHA (7-40 hex)
    r'|[a-zA-Z0-9._\-]+(?:/[a-zA-Z0-9._\-]+)+'  # path/to/file
    r')\s*$'
)

# machine_yaml の value 側として許容されるパターン（自然文でないもの）
_YAML_VALUE_NON_PROSE_RE = re.compile(
    r'^('
    r'true|false|yes|no|null|~'             # boolean/null
    r'|\d[\d._\-]*'                          # number / version
    r'|[a-zA-Z][a-zA-Z0-9_.\-/]*'           # identifier / path / enum (スペースなし)
    r'|https?://\S+'                         # URL
    r'|\[[^\]]*\]'                           # YAML inline list
    r'|\{[^}]*\}'                            # YAML inline map
    r'|)'                                    # 空値
    r'$'
)

# prose 的 value（3語以上 / 句読点）
_PROSE_VALUE_RE = re.compile(
    r'[,;!?]'
    r'|(?:\S+\s+){2,}\S+'
)

# grep/rg コマンド行
_GREP_CMD_LINE_RE = re.compile(
    r'^'
    r'(?:'
    r'\|?\s*(?:grep|rg|egrep|fgrep)\s+'
    r'|\$\s+(?:grep|rg|egrep|fgrep)\s+'
    r'|(?:grep|rg|egrep|fgrep)\s+-'
    r')'
)

# code fence の開始行パターン（3個以上のバッククォート / チルダ）
_CODE_FENCE_OPEN_RE = re.compile(r'^(`{3,}|~{3,})[^\n]*$')

# GFM 準拠 fence opening 行パターン（0-3 spaces indent + 3以上の backtick or tilde）
# 0-3 spaces indent + fence chars (backtick or tilde) + optional info string
_GFM_FENCE_OPEN_LINE_RE = re.compile(
    r'^( {0,3})((`{3,})|(~{3,}))(.*?)$'
)

# GFM 準拠 fence closing 行パターン
# closing fence は 0-3 spaces + fence chars のみ（trailing non-space は無効）
_GFM_FENCE_CLOSE_LINE_RE = re.compile(
    r'^( {0,3})((`{3,})|(~{3,}))\s*$'
)

# Machine-Readable Contract ブロック（```yaml で始まりかつ contract_schema_version を含む、
# または ``` の後が yaml/YAML のもの）
# ただしここでは code_fence 内の特別な判定として扱う


# ---------------------------------------------------------------------------
# GFM パイプテーブル検出（#685）
# ---------------------------------------------------------------------------

# GFM パイプテーブルのヘッダ行: | で区切られたセルを持つ行
# 少なくとも1本の | を含む行（テーブル行の基本条件）
_TABLE_ROW_RE = re.compile(r'.*\|.*')

# GFM テーブルのデリミタ行: | :---: | --- | :--- | --- のような行
# 各セルは `:?-+:?` またはスペース only
_TABLE_DELIMITER_RE = re.compile(r'^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$')


def _count_table_cells(row: str) -> int:
    """
    GFM テーブル行のセル数を数える。
    optional leading/trailing pipe を考慮する。
    """
    # エスケープされた | を一時的に置き換える
    row = row.replace('\\|', '\x00')
    # leading/trailing pipe を除去
    row = row.strip()
    if row.startswith('|'):
        row = row[1:]
    if row.endswith('|'):
        row = row[:-1]
    # セルに分割
    cells = row.split('|')
    return len(cells)


def _is_gfm_table_block(text: str) -> bool:
    """
    テキストが GFM パイプテーブルかどうかを判定する。

    GFM テーブルの条件:
    - 1行目がヘッダ行（| で区切られた複数セルを持つ）
    - 2行目がデリミタ行（:?-+:? で区切られた）
    - ヘッダとデリミタのセル数が一致する
    - fenced code block 内の | は無視（iter_markdown_blocks で処理済み）

    Returns:
        True if text is a GFM pipe table, False otherwise
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False

    # 1行目がテーブル行かチェック
    header_line = lines[0]
    if not _TABLE_ROW_RE.match(header_line):
        return False

    # 2行目がデリミタ行かチェック
    delimiter_line = lines[1]
    if not _TABLE_DELIMITER_RE.match(delimiter_line):
        return False

    # ヘッダとデリミタのセル数チェック
    header_cells = _count_table_cells(header_line)
    delimiter_cells = _count_table_cells(delimiter_line)
    if header_cells != delimiter_cells:
        return False

    return True

# GFM block-level 要素の開始パターン（テーブル終了トリガー）
# GFM 仕様では、テーブルは以下の block-level 要素の開始で終了する:
#   - 空行
#   - blockquote（>）
#   - ATX heading（# ）
#   - fenced code block（``` 、~~~）
#   - list item（- 、* 、+ 、1. ）
#   - HTML block
#   - 4-space indented code
_BLOCK_LEVEL_STARTERS = re.compile(
    r'^(?:'
    r'>{1}'              # blockquote
    r'|#{1,6}\s'        # ATX heading
    r'|```|~~~'          # fenced code
    r'|[-*+]\s'          # unordered list
    r'|\d+[.)]\s'        # ordered list
    r'|<[a-zA-Z]'        # HTML block
    r'|    '             # 4-space indented code
    r')'
)


def _is_block_level_starter(line: str) -> bool:
    """
    行が GFM の block-level 要素の開始行かどうかを判定する。
    テーブルの data row 収集を終了させるトリガーとして使用する。
    """
    return bool(_BLOCK_LEVEL_STARTERS.match(line))


# ---------------------------------------------------------------------------
# GFM 準拠 block セグメンテーション API（#659 追加）
# ---------------------------------------------------------------------------

def _yield_prose_with_table_splits(prose_text: str):
    """
    prose テキストを行単位で走査して GFM テーブルブロックを検出し、
    テーブル部分は BLOCK_KIND_TABLE として、残りは BLOCK_KIND_HUMAN_PROSE として yield する。

    GFM 仕様準拠のテーブル検出条件（B1 fix: #685）:
    - 現在行 + 次行が GFM table の header/delimiter として成立する場合にのみ table 開始
    - header/delimiter のセル数一致を確認
    - data row は GFM 仕様どおりセル数不足・過剰を許容
    - 空行または block-level 要素（blockquote、ATX heading、fenced code、list 等）で table 終了
    - table slice のみ BLOCK_KIND_TABLE として yield し、残りは通常 prose 判定へ

    修正前の実装は空行単位でのみ分割していたため、以下のような入力で
    blockquote が誤って BLOCK_KIND_TABLE に含まれていた:
        | Claim | Evidence |
        | --- | --- |
        | safe | ci green |
        > This should be prose, not table.
    """
    lines = prose_text.splitlines(keepends=True)
    n = len(lines)
    i = 0
    prose_buffer: list[str] = []

    def _flush_prose(buf: list[str]):
        if buf:
            yield ''.join(buf), BLOCK_KIND_HUMAN_PROSE

    while i < n:
        line = lines[i]
        stripped_line = line.rstrip('\n').rstrip('\r')

        # 空行: prose バッファに追加してインデックスを進める
        if not stripped_line.strip():
            prose_buffer.append(line)
            i += 1
            continue

        # block-level 要素の開始: prose バッファに追加してインデックスを進める
        # （blockquote、heading 等はテーブルを終了させ、prose として扱う）
        if _is_block_level_starter(stripped_line):
            prose_buffer.append(line)
            i += 1
            continue

        # テーブル開始チェック: 現在行（header）+ 次行（delimiter）
        if i + 1 < n:
            next_line = lines[i + 1].rstrip('\n').rstrip('\r')
            if (
                _TABLE_ROW_RE.match(stripped_line)
                and _TABLE_DELIMITER_RE.match(next_line)
            ):
                header_cells = _count_table_cells(stripped_line)
                delim_cells = _count_table_cells(next_line)
                if header_cells == delim_cells and header_cells > 0:
                    # prose バッファを先に flush
                    yield from _flush_prose(prose_buffer)
                    prose_buffer = []

                    # table rows を収集
                    table_lines = [line, lines[i + 1]]
                    j = i + 2
                    while j < n:
                        data_line = lines[j]
                        data_stripped = data_line.rstrip('\n').rstrip('\r')
                        # 空行または block-level 要素で table 終了
                        if not data_stripped.strip() or _is_block_level_starter(data_stripped):
                            break
                        table_lines.append(data_line)
                        j += 1

                    yield ''.join(table_lines), BLOCK_KIND_TABLE
                    i = j
                    continue

        # 通常の prose 行
        prose_buffer.append(line)
        i += 1

    # 残った prose バッファを flush
    yield from _flush_prose(prose_buffer)



def iter_markdown_blocks(text: str):
    """
    GFM 準拠の block セグメンテーション API。

    テキストを行単位で走査し、GFM fenced code block の境界を正確に判定して
    (text, block_kind) のタプルを yield する。

    GFM 仕様準拠の fence 判定:
    - opening fence: 0-3 spaces indent + 3以上の backtick or tilde + optional info string
    - closing fence: opening と同種（backtick/tilde）かつ同長以上、0-3 spaces indent、
      trailing non-space なし
    - 未閉 fence: EOF まで code block として扱う（prose ではない）
    - 4 spaces indent: fence として認識しない
    - backtick/tilde mismatch: closing として認識しない（異なる文字種）

    Yields:
        tuple[str, str]: (block_text, block_kind)
            block_kind は BLOCK_KIND_CODE_FENCE または BLOCK_KIND_HUMAN_PROSE
            （分類の精緻化は classify_block() が行う）
    """
    lines = text.splitlines(keepends=True)
    n = len(lines)
    i = 0
    prose_lines: list[str] = []

    while i < n:
        line = lines[i]
        # rstrip newlines for pattern matching, but keep original line for output
        stripped = line.rstrip('\n').rstrip('\r')

        # GFM fence opening line を検出
        m = _GFM_FENCE_OPEN_LINE_RE.match(stripped)
        if m:
            fence_chars = m.group(2)  # e.g. "```" or "~~~~"
            fence_char = fence_chars[0]  # '`' or '~'
            fence_len = len(fence_chars)
            info = m.group(5)  # info string（optional）

            # GFM spec: backtick fence の info string は backtick を含んではならない
            if fence_char == "`" and "`" in info:
                prose_lines.append(line)
                i += 1
                continue

            # flush any accumulated prose (detect and split table blocks)
            if prose_lines:
                yield from _yield_prose_with_table_splits(''.join(prose_lines))
                prose_lines = []

            # collect the code fence block
            fence_line_buf: list[str] = [line]
            i += 1

            while i < n:
                inner_line = lines[i]
                inner_stripped = inner_line.rstrip('\n').rstrip('\r')

                # closing fence: same char type, length >= opening length,
                # 0-3 spaces indent, no trailing non-space
                cm = _GFM_FENCE_CLOSE_LINE_RE.match(inner_stripped)
                if cm:
                    close_chars = cm.group(2)
                    close_char = close_chars[0]
                    close_len = len(close_chars)
                    if close_char == fence_char and close_len >= fence_len:
                        # valid closing fence
                        fence_line_buf.append(inner_line)
                        i += 1
                        break
                # not a valid closing fence — part of code block content
                fence_line_buf.append(inner_line)
                i += 1

            # yield code fence (closed or unclosed / EOF)
            yield ''.join(fence_line_buf), BLOCK_KIND_CODE_FENCE
        else:
            # regular (non-fence) line — accumulate into prose buffer
            prose_lines.append(line)
            i += 1

    # flush remaining prose (detect and split table blocks)
    if prose_lines:
        yield from _yield_prose_with_table_splits(''.join(prose_lines))


def split_blocks(text: str) -> list[tuple[str, str]]:
    """
    iter_markdown_blocks のリスト版。

    Returns:
        list of (block_text, block_kind) tuples
    """
    return list(iter_markdown_blocks(text))


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------


def _is_yaml_machine_line(line: str) -> bool:
    """
    1 行が machine-readable な key: value 形式かどうかを判定する。
    自然文スタイルの value（語数が多い・句読点がある）は False を返す。
    """
    m = re.match(r'^\s*[a-zA-Z_][a-zA-Z0-9_.]*\s*:\s*(.*)', line)
    if not m:
        return False
    value = m.group(1).strip()
    if _PROSE_VALUE_RE.search(value):
        return False
    return bool(_YAML_VALUE_NON_PROSE_RE.match(value))


def _is_grep_pattern_block(text: str) -> bool:
    """
    ブロックが grep/rg コマンド行またはパターン行であるかを判定する。
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    cmd_lines = sum(1 for line in lines if _GREP_CMD_LINE_RE.match(line))
    return cmd_lines >= max(1, len(lines) * 0.5)


def _clean_for_effective_char_count(text: str) -> str:
    """有効文字数カウント用のクリーニング（識別子・URL・インラインコード除去）"""
    text = _INLINE_CODE_RE.sub('', text)
    text = _URL_RE.sub('', text)
    text = _MD_LINK_URL_RE.sub(lambda m: m.group(1), text)
    text = _CLI_LINE_RE.sub('', text)
    text = _IDENTIFIER_RE.sub('', text)
    return text


def _count_effective_chars(text: str) -> int:
    """空白除き有効文字数"""
    cleaned = _WHITESPACE_RE.sub('', text)
    return len(cleaned)


def _is_machine_contract_fence(fence_text: str) -> bool:
    """
    code_fence ブロックが YAML Machine-Readable Contract かどうかを判定する。
    条件: fence prefix が yaml / yml であり、かつ contract_schema_version を行頭の key として含む。

    注意: 以前の実装は `'contract_schema_version' in content or (...)` という形で
    or のため、fence の言語指定を無視して任意のフェンス内に
    `contract_schema_version` 文字列があれば machine_contract に分類していた。
    この実装では yaml/yml prefix を必須条件とすることで過剰一致を排除する。
    """
    first_line = fence_text.splitlines()[0] if fence_text.splitlines() else ''
    if not _CODE_FENCE_OPEN_RE.match(first_line.strip()):
        return False
    content = fence_text
    return (
        first_line.strip().lstrip('`~').strip().lower() in {'yaml', 'yml'}
        and bool(re.search(r'(?m)^\s*contract_schema_version\s*:', content))
    )


def _is_url_or_identifier_line(line: str) -> bool:
    """行が URL・識別子・Issue/PR/SHA/パス参照のみで構成されているかを判定する"""
    stripped = line.strip()
    if not stripped:
        return False
    # URL のみ
    if _URL_RE.fullmatch(stripped):
        return True
    # #123 / GH-123 / SHA / パス参照
    if _REF_ONLY_LINE_RE.match(stripped):
        return True
    return False


# ---------------------------------------------------------------------------
# メイン分類 API
# ---------------------------------------------------------------------------


def classify_block(block: str) -> str:
    """
    Markdown ブロックの block_kind を返す。

    **注意: この関数は構文分類のみを行う。prose 除外可否（guard 判定）には使わないこと。**

    英語 ATX 見出し（例: ``## Outcome Risks``）は構文上 ``BLOCK_KIND_CANONICAL_HEADING``
    として分類されるが、これは prose ratio 判定から除外してよいという意味ではない。
    prose 除外可否（guard 判定）は heading_policy（``lookup_heading_policy`` /
    ``_is_heading_block``）に登録された見出しのみ行われる。

    **「classify_block(...) == BLOCK_KIND_CANONICAL_HEADING だから prose 除外してよい」
    という判定は誤り。** prose 除外が必要な consumer は必ず
    ``validate_japanese_content._is_heading_block()``（または同等の
    ``lookup_heading_policy()`` 参照）を経由すること。

    Args:
        block: 分類対象の Markdown ブロック文字列（前後空白はあってもよい）

    Returns:
        block_kind 文字列（ALL_BLOCK_KINDS のいずれか）
    """
    stripped = block.strip()
    if not stripped:
        return BLOCK_KIND_HUMAN_PROSE

    lines = stripped.splitlines()

    # -----------------------------------------------------------------------
    # code_fence チェック（3個以上のバッククォート / チルダ）
    # -----------------------------------------------------------------------
    first_line = lines[0].strip()
    if _CODE_FENCE_OPEN_RE.match(first_line):
        # machine_contract かどうか（```yaml + contract_schema_version）
        if _is_machine_contract_fence(stripped):
            return BLOCK_KIND_MACHINE_CONTRACT
        return BLOCK_KIND_CODE_FENCE

    # -----------------------------------------------------------------------
    # GFM パイプテーブルチェック（#685）
    # -----------------------------------------------------------------------
    if _is_gfm_table_block(stripped):
        return BLOCK_KIND_TABLE

    # -----------------------------------------------------------------------
    # 見出しチェック（classify_block 公開 API; AC1 維持）
    # -----------------------------------------------------------------------
    # NOTE: classify_block() の heading 分類ロジック（ATX 形式 → canonical/bilingual）は
    # AC1 により変更しない。heading_policy の SSOT 参照による非 canonical 見出しの
    # prose 残留判定は _is_heading_block()（validate_japanese_content.py）で行う（B1_B4）。
    # classify_block() での heading_policy 参照は行わない（旧動作維持）。
    if len(lines) == 1 or (len(lines) >= 1 and _HEADING_RE.match(lines[0])):
        # 見出し行のみ、または先頭行が見出し
        heading_line = lines[0]
        hm = _HEADING_RE.match(heading_line)
        if hm:
            heading_text = hm.group(2)
            has_japanese = bool(_JAPANESE_RE.search(heading_text))
            # 英語 ASCII のみ → canonical_heading
            # 日本語含む → bilingual_heading
            if has_japanese:
                return BLOCK_KIND_BILINGUAL_HEADING
            else:
                return BLOCK_KIND_CANONICAL_HEADING

    # -----------------------------------------------------------------------
    # shell_command / vc_command チェック
    # -----------------------------------------------------------------------
    shell_line_re = re.compile(r'^\s*[$#]\s+\S', re.MULTILINE)
    shell_lines = len(shell_line_re.findall(stripped))
    non_empty_lines = sum(1 for line in lines if line.strip())

    if non_empty_lines > 0 and shell_lines >= non_empty_lines * 0.5:
        # VC コマンド（uv run pytest / pnpm / rg などの検証コマンド）
        vc_cmd_re = re.compile(
            r'^\s*[$#]\s+(?:uv\s+run|pnpm|rg|pytest|python3?|bash|sh)\s',
            re.MULTILINE
        )
        vc_lines = len(vc_cmd_re.findall(stripped))
        if vc_lines >= max(1, shell_lines * 0.5):
            return BLOCK_KIND_VC_COMMAND
        return BLOCK_KIND_SHELL_COMMAND

    # -----------------------------------------------------------------------
    # grep_pattern → shell_command として扱う
    # -----------------------------------------------------------------------
    if _is_grep_pattern_block(stripped):
        return BLOCK_KIND_SHELL_COMMAND

    # -----------------------------------------------------------------------
    # yaml_machine_line チェック（machine-readable YAML block）
    # -----------------------------------------------------------------------
    if lines:
        machine_yaml_count = sum(1 for line in lines if _is_yaml_machine_line(line))
        if non_empty_lines > 0 and machine_yaml_count >= max(1, non_empty_lines * 0.6):
            return BLOCK_KIND_YAML_MACHINE_LINE

    # -----------------------------------------------------------------------
    # url_or_identifier チェック
    # -----------------------------------------------------------------------
    # すべての非空行が URL / 識別子 / 参照のみの場合
    non_empty = [line.strip() for line in lines if line.strip()]
    if non_empty and all(_is_url_or_identifier_line(line) for line in non_empty):
        return BLOCK_KIND_URL_OR_IDENTIFIER

    # 有効文字数が極端に少ない場合
    cleaned = _clean_for_effective_char_count(stripped)
    effective = _count_effective_chars(cleaned)
    if effective < 5:
        return BLOCK_KIND_URL_OR_IDENTIFIER

    # -----------------------------------------------------------------------
    # デフォルト: human_prose
    # -----------------------------------------------------------------------
    return BLOCK_KIND_HUMAN_PROSE


def classify_block_legacy(block: str) -> str:
    """
    validate_japanese_content.py の既存 consumer 向けの legacy 分類名を返す。

    legacy → block_kind マッピング:
      prose                 ← human_prose / canonical_heading / bilingual_heading
      code_fence            ← code_fence / machine_contract
      machine_yaml          ← yaml_machine_line
      shell_command         ← shell_command / vc_command
      grep_pattern          ← (shell_command に統合;
                後方互換用に shell_command → grep_pattern 変換は _classify_block で実施)
      url_or_identifier_only ← url_or_identifier

    Returns:
        'prose' | 'code_fence' | 'machine_yaml' | 'shell_command' |
        'grep_pattern' | 'url_or_identifier_only'
    """
    kind = classify_block(block)

    if kind in (BLOCK_KIND_HUMAN_PROSE, BLOCK_KIND_CANONICAL_HEADING, BLOCK_KIND_BILINGUAL_HEADING):
        return 'prose'
    elif kind in (BLOCK_KIND_CODE_FENCE, BLOCK_KIND_MACHINE_CONTRACT):
        return 'code_fence'
    elif kind == BLOCK_KIND_YAML_MACHINE_LINE:
        return 'machine_yaml'
    elif kind in (BLOCK_KIND_SHELL_COMMAND, BLOCK_KIND_VC_COMMAND):
        # legacy の grep_pattern は grep/rg コマンド行が支配的なブロック
        # _is_grep_pattern_block を再チェックして区別する
        if _is_grep_pattern_block(block.strip()):
            return 'grep_pattern'
        return 'shell_command'
    elif kind == BLOCK_KIND_URL_OR_IDENTIFIER:
        return 'url_or_identifier_only'
    elif kind == BLOCK_KIND_TABLE:
        return 'table'
    else:
        return 'prose'
