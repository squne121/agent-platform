"""AC12: LP002 (validate_issue_body) uses the shared mrc_contract_parser.

Verifies that LP002:
  - rejects a duplicate MRC key (last-wins is no longer accepted)
  - is bound to the ## Machine-Readable Contract section (a decoy YAML elsewhere
    does not satisfy / corrupt LP002)
  - still accepts a normal single, valid MRC (compat)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validate_issue_body as vib  # noqa: E402


def _lp002_errors(body: str):
    result = vib.validate_issue_body(body)
    return [e for e in result.errors if e.rule_id == "LP002"]


VALID_MRC = (
    "## Machine-Readable Contract\n\n"
    "```yaml\n"
    "contract_schema_version: v1\n"
    "issue_kind: implementation\n"
    "change_kind: code\n"
    "```\n"
)


def test_mrc_shared_parser_rejects_duplicate_key():
    body = (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        "issue_kind: implementation\n"
        "change_kind: code\n"
        "change_kind: docs\n"
        "```\n"
    )
    errs = _lp002_errors(body)
    assert errs, "LP002 must report an error for a duplicate MRC key"
    assert any("duplicate" in e.message.lower() or "重複" in e.message for e in errs), (
        f"LP002 message should mention the duplicate key; got {[e.message for e in errs]}"
    )


def test_mrc_shared_parser_valid_contract_has_no_lp002_error():
    assert _lp002_errors(VALID_MRC) == []


def test_mrc_shared_parser_section_bound_ignores_decoy_yaml():
    """A decoy YAML fence outside the MRC section must not be treated as the MRC;
    LP002 still validates only the real (valid) MRC → no error."""
    body = (
        "## Notes\n\n"
        "```yaml\n"
        "change_kind: docs\n"
        "```\n\n"
        + VALID_MRC
    )
    assert _lp002_errors(body) == []


def test_mrc_shared_parser_reports_multiple_fences():
    body = (
        "## Machine-Readable Contract\n\n"
        "```yaml\ncontract_schema_version: v1\nissue_kind: implementation\n```\n\n"
        "```yaml\nchange_kind: docs\n```\n"
    )
    errs = _lp002_errors(body)
    assert errs, "LP002 must report an error for multiple YAML fences in the MRC section"
