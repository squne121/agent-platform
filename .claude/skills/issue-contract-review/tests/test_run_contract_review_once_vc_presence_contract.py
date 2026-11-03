"""
test_run_contract_review_once_vc_presence_contract.py

Issue #2782 AC5:

`run_contract_review_once.py::_resolve_delivery_rollup_applicability()` の
delivery-rollup applicability 判定が、absent / present-empty /
present-nonempty / closing-hash-present の4ケースで既存の意図（VC section
が実質的に存在する場合は Final-Gate 適用除外の対象外にする）を維持し、
presence 判定に truthiness ではなく
`extract_verification_commands_section(body) is not None` を使うことを固定
する回帰テスト（PR #2780 OWNER review comment F2 の指摘: 旧 regex は空
section でも truthy な `"\\n"` を返す一方、shared GFM-aware extractor は
空 section で `""`（falsy）を返すため、`if extract_verification_commands_section(body):`
のような truthiness 判定は空 VC section を持つ delivery-rollup parent を
誤って「VC section なし」= 適用除外 True と判定してしまう）。

Runtime Verification Applicability: not_applicable
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location(
    "run_contract_review_once", _SCRIPTS_DIR / "run_contract_review_once.py"
)
assert _spec is not None and _spec.loader is not None
_rcr_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rcr_mod)  # type: ignore[union-attr]

_resolve_delivery_rollup_applicability = _rcr_mod._resolve_delivery_rollup_applicability
_is_delivery_rollup_parent_without_vc_section = (
    _rcr_mod._is_delivery_rollup_parent_without_vc_section
)


_MRC_PARENT_DELIVERY_ROLLUP = textwrap.dedent(
    """\
    ## Machine-Readable Contract

    ```yaml
    contract_schema_version: v1
    issue_kind: parent
    parent_mode: delivery-rollup
    ```

    ## Outcome

    fixture delivery-rollup parent issue.
    """
)


def _body_absent() -> str:
    """absent: `## Verification Commands` 見出し自体が存在しない。"""
    return _MRC_PARENT_DELIVERY_ROLLUP + textwrap.dedent(
        """\

        ## Stop Conditions

        - N/A
        """
    )


def _body_present_empty() -> str:
    """present-empty: 見出しは存在するが本文が空（次見出しまで空行のみ）。"""
    return _MRC_PARENT_DELIVERY_ROLLUP + textwrap.dedent(
        """\

        ## Verification Commands


        ## Stop Conditions

        - N/A
        """
    )


def _body_present_nonempty() -> str:
    """present-nonempty: 見出しが存在し本文に content がある。"""
    return _MRC_PARENT_DELIVERY_ROLLUP + textwrap.dedent(
        """\

        ## Verification Commands

        ```bash
        $ pytest tests/test_child.py -q
        ```

        ## Stop Conditions

        - N/A
        """
    )


def _body_closing_hash_present() -> str:
    """closing-hash-present: GFM closing-hash 見出し（`## Verification Commands ##`）
    かつ本文に content がある。"""
    return _MRC_PARENT_DELIVERY_ROLLUP + textwrap.dedent(
        """\

        ## Verification Commands ##

        ```bash
        $ pytest tests/test_child.py -q
        ```

        ## Stop Conditions

        - N/A
        """
    )


# ---------------------------------------------------------------------------
# _resolve_delivery_rollup_applicability(): 4 ケース
# ---------------------------------------------------------------------------


def test_absent_vc_section_makes_delivery_rollup_parent_applicable():
    """absent: VC section が存在しない delivery-rollup parent は
    Final-Gate 適用除外の対象（applicable=True）。"""
    result = _resolve_delivery_rollup_applicability(_body_absent())
    assert result.applicable is True
    assert result.reason_code == "delivery_rollup_parent_without_verification_commands"


def test_present_empty_vc_section_makes_delivery_rollup_parent_not_applicable():
    """present-empty: VC 見出しは存在する（本文は空）ため、heading の
    presence（`is not None`）を正しく検出し、適用除外の対象外
    （applicable=False, reason_code="vc_section_present"）とする。

    旧 truthiness 判定（`if extract_verification_commands_section(body):`）
    ではこのケースで `""` が falsy と評価され、誤って
    applicable=True（reason_code="delivery_rollup_parent_without_verification_commands"）
    になっていた。
    """
    result = _resolve_delivery_rollup_applicability(_body_present_empty())
    assert result.applicable is False
    assert result.reason_code == "vc_section_present"


def test_present_nonempty_vc_section_makes_delivery_rollup_parent_not_applicable():
    """present-nonempty: VC section に content がある場合は従来通り
    適用除外の対象外。"""
    result = _resolve_delivery_rollup_applicability(_body_present_nonempty())
    assert result.applicable is False
    assert result.reason_code == "vc_section_present"


def test_closing_hash_present_vc_section_makes_delivery_rollup_parent_not_applicable():
    """closing-hash-present: GFM closing-hash 見出しでも正しく presence を
    検出し、適用除外の対象外とする。"""
    result = _resolve_delivery_rollup_applicability(_body_closing_hash_present())
    assert result.applicable is False
    assert result.reason_code == "vc_section_present"


# ---------------------------------------------------------------------------
# 後方互換 bool predicate: _is_delivery_rollup_parent_without_vc_section()
# ---------------------------------------------------------------------------


def test_bool_predicate_matches_applicability_for_all_four_cases():
    """既存 call site 向けの bool predicate が
    _resolve_delivery_rollup_applicability().applicable と一致することを
    4 ケース全てで固定する。"""
    assert _is_delivery_rollup_parent_without_vc_section(_body_absent()) is True
    assert _is_delivery_rollup_parent_without_vc_section(_body_present_empty()) is False
    assert _is_delivery_rollup_parent_without_vc_section(_body_present_nonempty()) is False
    assert _is_delivery_rollup_parent_without_vc_section(_body_closing_hash_present()) is False
