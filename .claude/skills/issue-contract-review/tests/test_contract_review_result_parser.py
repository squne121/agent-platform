"""
tests/test_contract_review_result_parser.py

Unit tests for contract_review_result_parser.py
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_PARSER_PATH = _SCRIPTS_DIR / "contract_review_result_parser.py"

spec = importlib.util.spec_from_file_location("contract_review_result_parser", _PARSER_PATH)
assert spec is not None and spec.loader is not None
_parser_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_parser_mod)  # type: ignore[union-attr]

parse_contract_review_results = _parser_mod.parse_contract_review_results
find_latest_go = _parser_mod.find_latest_go
find_latest_result = _parser_mod.find_latest_result
_extract_yaml_blocks = _parser_mod._extract_yaml_blocks
_parse_simple_yaml_block = _parser_mod._parse_simple_yaml_block
_is_valid_contract_review_result = _parser_mod._is_valid_contract_review_result
is_fingerprint_ready_go = _parser_mod.is_fingerprint_ready_go
SimpleYamlParseError = _parser_mod.SimpleYamlParseError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ISSUE_NUMBER = 817
_REPO = "example-owner/example-repo"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"


def _make_go_comment(
    comment_id: int = 1001,
    created_at: str = "2026-06-13T08:00:00Z",
    issue_url: str = _ISSUE_URL,
) -> dict:
    return {
        "id": comment_id,
        "html_url": f"{_ISSUE_URL}#issuecomment-{comment_id}",
        "created_at": created_at,
        "updated_at": created_at,
        "body": f"""Some preamble.

```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "{created_at}"
  generated_by: issue-contract-review
  issue_url: {issue_url}
```

Some postamble.
""",
    }


def _make_blocked_comment(
    comment_id: int = 1002,
    created_at: str = "2026-06-13T09:00:00Z",
    issue_url: str = _ISSUE_URL,
) -> dict:
    return {
        "id": comment_id,
        "html_url": f"{_ISSUE_URL}#issuecomment-{comment_id}",
        "created_at": created_at,
        "updated_at": created_at,
        "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "{created_at}"
  generated_by: issue-contract-review
  issue_url: {issue_url}
```
""",
    }


# ---------------------------------------------------------------------------
# YAML block extraction
# ---------------------------------------------------------------------------


class TestYamlBlockExtraction:
    """Tests for fenced yaml block extraction."""

    def test_extracts_yaml_block(self):
        body = "Preamble\n```yaml\nkey: value\n```\nPostamble"
        blocks = _extract_yaml_blocks(body)
        assert len(blocks) == 1
        assert "key: value" in blocks[0]

    def test_extracts_yml_block(self):
        body = "```yml\nkey: val\n```"
        blocks = _extract_yaml_blocks(body)
        assert len(blocks) == 1

    def test_no_blocks(self):
        body = "No yaml here"
        blocks = _extract_yaml_blocks(body)
        assert blocks == []

    def test_multiple_blocks(self):
        body = "```yaml\na: 1\n```\n```yaml\nb: 2\n```"
        blocks = _extract_yaml_blocks(body)
        assert len(blocks) == 2


# ---------------------------------------------------------------------------
# Simple YAML parser
# ---------------------------------------------------------------------------


class TestSimpleYamlParser:
    """Tests for _parse_simple_yaml_block."""

    def test_flat_key_value(self):
        block = "status: go\ngenerated_by: issue-contract-review\n"
        result = _parse_simple_yaml_block(block)
        assert result["status"] == "go"
        assert result["generated_by"] == "issue-contract-review"

    def test_quoted_values(self):
        block = 'generated_at: "2026-06-13T08:00:00Z"\nissue_url: "https://example.com"\n'
        result = _parse_simple_yaml_block(block)
        assert result["generated_at"] == "2026-06-13T08:00:00Z"
        assert result["issue_url"] == "https://example.com"

    def test_nested_key(self):
        block = "CONTRACT_REVIEW_RESULT_V1:\n  status: go\n  generated_by: issue-contract-review\n"
        result = _parse_simple_yaml_block(block)
        assert "CONTRACT_REVIEW_RESULT_V1" in result
        inner = result["CONTRACT_REVIEW_RESULT_V1"]
        assert inner["status"] == "go"
        assert inner["generated_by"] == "issue-contract-review"

    @pytest.mark.parametrize("transport", ["flow", "nested"])
    def test_fallback_rejects_noncanonical_fingerprint_mapping(self, monkeypatch, transport):
        """GIVEN a flow/nested fingerprint WHEN fallback parses THEN it fails closed."""
        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        fingerprint = {
            "issue_number": _ISSUE_NUMBER,
            "contract_source_kind": "issue_comment",
            "contract_source_id": "1001",
            "contract_body_sha256": "sha256:" + "a" * 64,
            "allowed_paths_normalized_sha256": "b" * 64,
            "base_ref": "main",
            "base_sha_at_snapshot": "c" * 40,
        }
        block = (
            "CONTRACT_REVIEW_RESULT_V1:\n"
            "  status: go\n"
            "  generated_at: 2026-08-01T00:00:00Z\n"
            "  generated_by: issue-contract-review\n"
            f"  issue_url: {_ISSUE_URL}\n"
            f"  body_sha256: {fingerprint['contract_body_sha256']}\n"
        )
        if transport == "flow":
            block += f"  expected_contract_fingerprint: {json.dumps(fingerprint)}\n"
        else:
            block += "  expected_contract_fingerprint:\n" + "\n".join(
                f"    {key}: {value}" for key, value in fingerprint.items()
            ) + "\n"

        with pytest.raises(SimpleYamlParseError, match="noncanonical_contract_fingerprint_transport"):
            _parse_simple_yaml_block(block)

    def test_fallback_preserves_issue_1153_fingerprint_shape(self, monkeypatch):
        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        body_sha256 = "sha256:2875f1a5982e327face1b5fdcd3b7d76a89cdd6459b21a1147d7a18d5b31323f"
        fingerprint = {
            "issue_number": 1153,
            "contract_source_kind": "issue_comment",
            "contract_source_id": "5150877720",
            "contract_body_sha256": body_sha256,
            "allowed_paths_normalized_sha256": "31d2385bc36b2d6da2cb66ad745502a3dfe777663191217c79be0ec9dd8bcc6d",
            "base_ref": "main",
            "base_sha_at_snapshot": "487f80f2a03de2303288d13f220733ba74567869",
        }
        block = (
            "CONTRACT_REVIEW_RESULT_V1:\n"
            "  status: go\n"
            "  generated_at: 2026-08-01T09:42:58Z\n"
            "  generated_by: issue-contract-review\n"
            "  issue_url: https://github.com/example-owner/example-repo/issues/1153\n"
            f"  body_sha256: {body_sha256}\n"
            "  expected_contract_fingerprint: '"
            + json.dumps(fingerprint)
            + "'\n"
        )

        result = _parse_simple_yaml_block(block)
        inner = result["CONTRACT_REVIEW_RESULT_V1"]
        assert inner["expected_contract_fingerprint"] == fingerprint
        assert is_fingerprint_ready_go(inner, 5150877720, 1153) is True

    def test_fallback_keeps_json_array_and_existing_scalar_behavior(self, monkeypatch):
        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        result = _parse_simple_yaml_block(
            "root:\n"
            "  values: [1,true,null,\"ok\"]\n"
            "  plain: value { remains plain\n"
            "  boolean: true\n"
            "  null_value: null\n"
            "  integer: 42\n"
            "  quoted: \"quoted\"\n"
        )

        inner = result["root"]
        assert inner["values"] == [1, True, None, "ok"]
        assert inner["plain"] == "value { remains plain"
        assert inner["boolean"] == "true"
        assert inner["null_value"] == "null"
        assert inner["integer"] == "42"
        assert inner["quoted"] == "quoted"

    @pytest.mark.parametrize(
        "value",
        [
            '{"issue_number":NaN}',
            '{"issue_number":Infinity}',
            '{"issue_number":-Infinity}',
            '{"root":{"duplicate":1,"duplicate":2}}',
            '{"duplicate":1,"duplicate":2}',
        ],
    )
    def test_fallback_rejects_nonstandard_or_ambiguous_json(self, monkeypatch, value):
        """GIVEN non-RFC JSON WHEN fallback parses THEN it fails closed."""
        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        with pytest.raises(SimpleYamlParseError):
            _parse_simple_yaml_block(f"root:\n  candidate: {value}\n")

    def test_fallback_bounds_flow_collection_size_and_depth(self, monkeypatch):
        """GIVEN hostile flow JSON WHEN fallback parses THEN it rejects before recursion."""
        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        with pytest.raises(SimpleYamlParseError, match="too_large"):
            _parse_simple_yaml_block(f"root:\n  candidate: [{'1' * 16_384}]\n")
        with pytest.raises(SimpleYamlParseError, match="too_deep"):
            _parse_simple_yaml_block(f"root:\n  candidate: {'[' * 33}{']' * 33}\n")

    def test_fallback_does_not_expand_arbitrary_indentation_mappings(self, monkeypatch):
        """GIVEN unrecognized deep YAML WHEN fallback parses THEN it remains scalar-only."""
        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        result = _parse_simple_yaml_block("root:\n  child:\n    deep: ignored\n")
        assert result == {"root": {"child": None}}

    @pytest.mark.parametrize("value", ["{bad}", "[1,]"])
    def test_fallback_rejects_malformed_json_flow_collections(self, monkeypatch, value):
        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        with pytest.raises(SimpleYamlParseError, match="invalid_json_flow_collection"):
            _parse_simple_yaml_block(f"root:\n  candidate: {value}\n")

    @pytest.mark.parametrize("value", ["{'key': 'value'}", "(1, 2)", "{1, 2}"])
    def test_fallback_does_not_accept_python_literals_as_json(self, monkeypatch, value):
        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        if value.startswith("{"):
            with pytest.raises(SimpleYamlParseError):
                _parse_simple_yaml_block(f"root:\n  candidate: {value}\n")
        else:
            result = _parse_simple_yaml_block(f"root:\n  candidate: {value}\n")
            assert not isinstance(result["root"]["candidate"], (dict, list, tuple, set))

    def test_fingerprint_string_remains_not_ready(self):
        inner = {"expected_contract_fingerprint": "{\"issue_number\": 817}"}
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for _is_valid_contract_review_result."""

    def _make_block(
        self,
        status: str = "go",
        generated_by: str = "issue-contract-review",
        issue_url: str = _ISSUE_URL,
        generated_at: str = "2026-06-13T08:00:00Z",
    ) -> dict:
        return {
            "CONTRACT_REVIEW_RESULT_V1": {
                "status": status,
                "generated_by": generated_by,
                "issue_url": issue_url,
                "generated_at": generated_at,
            }
        }

    def test_valid_go_block(self):
        block = self._make_block(status="go")
        assert _is_valid_contract_review_result(block, expected_issue_url=_ISSUE_URL) is True

    def test_valid_blocked_block(self):
        block = self._make_block(status="blocked")
        assert _is_valid_contract_review_result(block, expected_issue_url=_ISSUE_URL) is True

    def test_invalid_status_human_judgment(self):
        """status: human_judgment is NOT a valid CONTRACT_REVIEW_RESULT_V1 status."""
        block = self._make_block(status="human_judgment")
        assert _is_valid_contract_review_result(block) is False

    def test_invalid_status_empty(self):
        block = self._make_block(status="")
        assert _is_valid_contract_review_result(block) is False

    def test_wrong_generated_by(self):
        block = self._make_block(generated_by="some-other-tool")
        assert _is_valid_contract_review_result(block) is False

    def test_issue_url_mismatch(self):
        block = self._make_block(issue_url="https://github.com/other/repo/issues/1")
        assert _is_valid_contract_review_result(block, expected_issue_url=_ISSUE_URL) is False

    def test_issue_url_match_no_expected(self):
        """No expected_issue_url → accept any."""
        block = self._make_block()
        assert _is_valid_contract_review_result(block, expected_issue_url=None) is True

    def test_missing_generated_at(self):
        block = {
            "CONTRACT_REVIEW_RESULT_V1": {
                "status": "go",
                "generated_by": "issue-contract-review",
                "issue_url": _ISSUE_URL,
                "generated_at": None,
            }
        }
        assert _is_valid_contract_review_result(block) is False

    def test_no_root_marker(self):
        block = {"OTHER_MARKER": {"status": "go"}}
        assert _is_valid_contract_review_result(block) is False


# ---------------------------------------------------------------------------
# parse_contract_review_results
# ---------------------------------------------------------------------------


class TestParseContractReviewResults:
    """Tests for the main parsing function."""

    def test_quoted_json_fingerprint_is_decoded_to_a_ready_mapping(self):
        """GIVEN producer-quoted fingerprint JSON WHEN parsed THEN it is source-bound."""
        fingerprint = dict(_VALID_FINGERPRINT)
        comment = _make_go_comment(comment_id=1001)
        comment["body"] = comment["body"].replace(
            f"  issue_url: {_ISSUE_URL}",
            "\n".join(
                [
                    f"  issue_url: {_ISSUE_URL}",
                    f"  body_sha256: {fingerprint['contract_body_sha256']}",
                    "  expected_contract_fingerprint: '"
                    + json.dumps(fingerprint)
                    + "'",
                ]
            ),
        )

        results = parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL)

        assert results[0]["inner"]["expected_contract_fingerprint"] == fingerprint
        assert results[0]["is_fingerprint_ready"] is True

    def test_malformed_quoted_json_fingerprint_is_rejected(self):
        """GIVEN malformed producer JSON WHEN parsed THEN no authority is accepted."""
        comment = _make_go_comment(comment_id=1001)
        comment["body"] = comment["body"].replace(
            f"  issue_url: {_ISSUE_URL}",
            f"  issue_url: {_ISSUE_URL}\n  expected_contract_fingerprint: '{{bad}}'",
        )

        assert parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL) == []

    @pytest.mark.parametrize("transport", ["flow", "nested"])
    def test_noncanonical_fingerprint_mapping_is_rejected_with_pyyaml(self, transport):
        """GIVEN a flow/nested mapping WHEN PyYAML parses THEN it cannot become authority."""
        fingerprint = dict(_VALID_FINGERPRINT)
        comment = _make_go_comment(comment_id=1001)
        if transport == "flow":
            fingerprint_yaml = f"  expected_contract_fingerprint: {json.dumps(fingerprint)}"
        else:
            fingerprint_yaml = "  expected_contract_fingerprint:\n" + "\n".join(
                f"    {key}: {value}" for key, value in fingerprint.items()
            )
        comment["body"] = comment["body"].replace(
            f"  issue_url: {_ISSUE_URL}",
            "\n".join(
                [
                    f"  issue_url: {_ISSUE_URL}",
                    f"  body_sha256: {fingerprint['contract_body_sha256']}",
                    fingerprint_yaml,
                ]
            ),
        )

        assert parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL) == []

    @pytest.mark.parametrize(
        "transport",
        [
            lambda fingerprint: json.dumps(fingerprint),
            lambda fingerprint: json.dumps(json.dumps(fingerprint)),
            lambda _fingerprint: "plain-scalar",
            lambda _fingerprint: "'{bad}'",
            lambda fingerprint: "'" + json.dumps(json.dumps(fingerprint)) + "'",
            lambda _fingerprint: "'[]'",
            lambda _fingerprint: "'NaN'",
            lambda _fingerprint: "'{\"issue_number\":1,\"issue_number\":2}'",
            lambda _fingerprint: "'" + "[" * 33 + "]" * 33 + "'",
            lambda _fingerprint: "'{\"payload\":\"" + "x" * 16_384 + "\"}'",
        ],
    )
    def test_noncanonical_or_invalid_fingerprint_is_rejected_with_parser_parity(
        self, monkeypatch, transport
    ):
        """GIVEN hostile fingerprint transport WHEN either parser runs THEN it fails closed."""
        fingerprint = dict(_VALID_FINGERPRINT)
        comment = _make_go_comment(comment_id=1001)
        comment["body"] = comment["body"].replace(
            f"  issue_url: {_ISSUE_URL}",
            "\n".join(
                [
                    f"  issue_url: {_ISSUE_URL}",
                    f"  body_sha256: {fingerprint['contract_body_sha256']}",
                    "  expected_contract_fingerprint: " + transport(fingerprint),
                ]
            ),
        )

        assert parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL) == []

        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        assert parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL) == []

    def test_nested_json_string_fingerprint_is_rejected_in_fallback(self, monkeypatch):
        """GIVEN a double-encoded fingerprint WHEN fallback parses THEN it is rejected."""
        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        fingerprint = dict(_VALID_FINGERPRINT)
        comment = _make_go_comment(comment_id=1001)
        comment["body"] = comment["body"].replace(
            f"  issue_url: {_ISSUE_URL}",
            "\n".join(
                [
                    f"  issue_url: {_ISSUE_URL}",
                    f"  body_sha256: {fingerprint['contract_body_sha256']}",
                    "  expected_contract_fingerprint: " + json.dumps(json.dumps(fingerprint)),
                ]
            ),
        )
        monkeypatch.setattr(builtins, "__import__", reject_yaml)

        results = parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL)

        assert results == []

    def test_nested_json_string_fingerprint_is_rejected_with_pyyaml(self):
        """GIVEN a double-encoded fingerprint WHEN PyYAML parses THEN it is rejected."""
        fingerprint = dict(_VALID_FINGERPRINT)
        comment = _make_go_comment(comment_id=1001)
        comment["body"] = comment["body"].replace(
            f"  issue_url: {_ISSUE_URL}",
            "\n".join(
                [
                    f"  issue_url: {_ISSUE_URL}",
                    f"  body_sha256: {fingerprint['contract_body_sha256']}",
                    "  expected_contract_fingerprint: " + json.dumps(json.dumps(fingerprint)),
                ]
            ),
        )

        results = parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL)

        assert results == []

    def test_parses_go_comment(self):
        comments = [_make_go_comment()]
        results = parse_contract_review_results(comments, expected_issue_url=_ISSUE_URL)
        assert len(results) == 1
        assert results[0]["status"] == "go"
        assert results[0]["html_url"] == comments[0]["html_url"]

    def test_parses_blocked_comment(self):
        comments = [_make_blocked_comment()]
        results = parse_contract_review_results(comments, expected_issue_url=_ISSUE_URL)
        assert len(results) == 1
        assert results[0]["status"] == "blocked"

    def test_skips_comment_without_marker(self):
        comments = [{"id": 1, "html_url": "url", "created_at": "2026-01-01", "body": "No marker"}]
        results = parse_contract_review_results(comments)
        assert len(results) == 0

    def test_skips_comment_wrong_issue_url(self):
        comment = _make_go_comment(issue_url="https://github.com/wrong/repo/issues/999")
        results = parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL)
        assert len(results) == 0

    def test_multiple_comments_parsed(self):
        comments = [_make_go_comment(comment_id=1001), _make_blocked_comment(comment_id=1002)]
        results = parse_contract_review_results(comments, expected_issue_url=_ISSUE_URL)
        assert len(results) == 2

    def test_only_fenced_yaml_blocks_parsed(self):
        """Inline mentions of CONTRACT_REVIEW_RESULT_V1 are not parsed."""
        comment = {
            "id": 1,
            "html_url": "url",
            "created_at": "2026-01-01",
            "body": "See `CONTRACT_REVIEW_RESULT_V1` for details. No fenced block.",
        }
        results = parse_contract_review_results([comment])
        assert len(results) == 0

    def test_review_comment_in_example_code_not_parsed(self):
        """Example code blocks in review comments should not be parsed as valid results."""
        comment = {
            "id": 2,
            "html_url": "url",
            "created_at": "2026-01-01",
            "body": (
                "Here's an example:\n"
                "```yaml\n"
                "# This is an example, not a real result\n"
                "CONTRACT_REVIEW_RESULT_V1:\n"
                "  status: go\n"
                "  generated_by: some-fake-tool\n"
                "  issue_url: https://example.com/1\n"
                "  generated_at: 2026-01-01T00:00:00Z\n"
                "```\n"
            ),
        }
        results = parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL)
        # Wrong generated_by → not valid
        assert len(results) == 0

    def test_fallback_skips_hostile_block_and_keeps_later_trusted_result(self, monkeypatch):
        """GIVEN a hostile earlier comment WHEN fallback parses THEN later authority survives."""
        def yaml_value_error(_block):
            raise ValueError("simulated PyYAML integer conversion failure")

        monkeypatch.setitem(sys.modules, "yaml", types.SimpleNamespace(safe_load=yaml_value_error))
        malicious = _make_go_comment(comment_id=1001)
        malicious.update(
            author="mallory",
            author_association="NONE",
            author_id=1,
            author_type="User",
        )
        malicious["body"] = malicious["body"].replace(
            "  issue_url: " + _ISSUE_URL,
            "  issue_url: " + _ISSUE_URL + "\n  bomb: [" + "1" * 5_000 + "]",
        )
        trusted = _make_go_comment(
            comment_id=1002, created_at="2026-06-13T09:00:00Z"
        )
        trusted.update(
            author="squne121",
            author_association="OWNER",
            author_id=63350259,
            author_type="User",
        )

        results = parse_contract_review_results(
            [malicious, trusted], expected_issue_url=_ISSUE_URL
        )

        assert [result["comment_id"] for result in results] == [1002]
        assert results[0]["is_trusted_author"] is True

    def test_sibling_decoy_fingerprint_key_is_not_authoritative(self, monkeypatch):
        """PR #2083 review fix (P1-1): a same-named key on an unrelated sibling
        top-level mapping must never be mistaken for
        CONTRACT_REVIEW_RESULT_V1.expected_contract_fingerprint. The
        canonical scalar must be bound to the exact node path, not merely
        present anywhere in the raw YAML block."""
        fingerprint = dict(_VALID_FINGERPRINT)
        decoy_payload = json.dumps(fingerprint)
        body = (
            "```yaml\n"
            "DECOY:\n"
            f"  expected_contract_fingerprint: '{decoy_payload}'\n"
            "CONTRACT_REVIEW_RESULT_V1:\n"
            "  status: go\n"
            '  generated_at: "2026-06-13T08:00:00Z"\n'
            "  generated_by: issue-contract-review\n"
            f"  issue_url: {_ISSUE_URL}\n"
            f"  body_sha256: {fingerprint['contract_body_sha256']}\n"
            "  expected_contract_fingerprint: " + json.dumps(fingerprint) + "\n"
            "```\n"
        )
        comment = {
            "id": 1001,
            "html_url": f"{_ISSUE_URL}#issuecomment-1001",
            "created_at": "2026-06-13T08:00:00Z",
            "body": body,
        }

        assert parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL) == []

        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        assert parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL) == []

    def test_block_scalar_decoy_text_is_not_authoritative(self, monkeypatch):
        """PR #2083 review fix (P1-1): a coincidental 2-space canonical-looking
        line inside an unrelated block scalar's literal text must not be
        mistaken for CONTRACT_REVIEW_RESULT_V1.expected_contract_fingerprint."""
        fingerprint = dict(_VALID_FINGERPRINT)
        decoy_payload = json.dumps(fingerprint)
        body = (
            "```yaml\n"
            "notes: |\n"
            f"  expected_contract_fingerprint: '{decoy_payload}'\n"
            "CONTRACT_REVIEW_RESULT_V1:\n"
            "  status: go\n"
            '  generated_at: "2026-06-13T08:00:00Z"\n'
            "  generated_by: issue-contract-review\n"
            f"  issue_url: {_ISSUE_URL}\n"
            f"  body_sha256: {fingerprint['contract_body_sha256']}\n"
            "  expected_contract_fingerprint: " + json.dumps(fingerprint) + "\n"
            "```\n"
        )
        comment = {
            "id": 1001,
            "html_url": f"{_ISSUE_URL}#issuecomment-1001",
            "created_at": "2026-06-13T08:00:00Z",
            "body": body,
        }

        assert parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL) == []

        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        assert parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL) == []

    def test_duplicate_fingerprint_key_is_not_authoritative(self, monkeypatch):
        """PR #2083 review fix (P1-1): a duplicated
        expected_contract_fingerprint key under CONTRACT_REVIEW_RESULT_V1
        makes the authoritative value ambiguous and must fail closed for
        both the PyYAML compose() path and the fallback state machine."""
        fingerprint = dict(_VALID_FINGERPRINT)
        canonical_scalar = "  expected_contract_fingerprint: '" + json.dumps(fingerprint) + "'\n"
        comment = _make_go_comment(comment_id=1001)
        comment["body"] = comment["body"].replace(
            f"  issue_url: {_ISSUE_URL}",
            "\n".join(
                [
                    f"  issue_url: {_ISSUE_URL}",
                    f"  body_sha256: {fingerprint['contract_body_sha256']}",
                ]
            ),
        )
        comment["body"] = comment["body"].replace("```\n", canonical_scalar + canonical_scalar + "```\n", 1)

        assert parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL) == []

        original_import = builtins.__import__

        def reject_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced fallback")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_yaml)
        assert parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL) == []

    def test_human_judgment_status_not_valid(self):
        """
        Comments with status: human_judgment are NOT valid CONTRACT_REVIEW_RESULT_V1.
        This guards against accidentally treating human_judgment as a valid result.
        """
        comment = {
            "id": 99,
            "html_url": "url",
            "created_at": "2026-01-01",
            "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: human_judgment
  generated_at: "2026-01-01T00:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_ISSUE_URL}
```
""",
        }
        results = parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL)
        assert len(results) == 0, "human_judgment status must not be a valid result"


# ---------------------------------------------------------------------------
# find_latest_go / find_latest_result
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# is_fingerprint_ready_go / find_latest_go(fingerprint_ready_only=True) (#1537)
# ---------------------------------------------------------------------------

_VALID_FINGERPRINT = {
    "issue_number": _ISSUE_NUMBER,
    "contract_source_kind": "issue_comment",
    "contract_source_id": "1001",
    "contract_body_sha256": "sha256:" + "a" * 64,
    "allowed_paths_normalized_sha256": "b" * 64,
    "base_ref": "main",
    "base_sha_at_snapshot": "c" * 40,
}


class TestFingerprintReadiness:
    """Tests for is_fingerprint_ready_go (Issue #1537 AC2)."""

    def test_valid_fingerprint_is_ready(self):
        inner = {
            "body_sha256": _VALID_FINGERPRINT["contract_body_sha256"],
            "expected_contract_fingerprint": dict(_VALID_FINGERPRINT),
        }
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is True

    def test_missing_fingerprint_key_is_not_ready(self):
        inner = {"status": "go"}
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is False

    def test_fingerprint_not_a_dict_is_not_ready(self):
        inner = {"expected_contract_fingerprint": "not-a-dict"}
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is False

    def test_fingerprint_missing_required_key_is_not_ready(self):
        fp = dict(_VALID_FINGERPRINT)
        del fp["base_sha_at_snapshot"]
        inner = {"expected_contract_fingerprint": fp}
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is False

    def test_issue_number_bool_is_not_ready(self):
        fp = dict(_VALID_FINGERPRINT)
        fp["issue_number"] = True
        inner = {"expected_contract_fingerprint": fp}
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is False

    def test_issue_number_mismatch_is_not_ready(self):
        inner = {"expected_contract_fingerprint": dict(_VALID_FINGERPRINT)}
        assert is_fingerprint_ready_go(inner, 1001, 999) is False

    def test_contract_source_kind_wrong_is_not_ready(self):
        fp = dict(_VALID_FINGERPRINT)
        fp["contract_source_kind"] = "pr_comment"
        inner = {"expected_contract_fingerprint": fp}
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is False

    def test_contract_source_id_mismatch_with_actual_comment_id_is_not_ready(self):
        """The fingerprint's self-declared contract_source_id must equal the
        id of the comment it was actually parsed from -- otherwise it is a
        self-reference to a different (or nonexistent) comment."""
        inner = {"expected_contract_fingerprint": dict(_VALID_FINGERPRINT)}
        assert is_fingerprint_ready_go(inner, 9999, _ISSUE_NUMBER) is False

    def test_contract_source_id_non_digit_string_is_not_ready(self):
        fp = dict(_VALID_FINGERPRINT)
        fp["contract_source_id"] = "not-a-number"
        inner = {"expected_contract_fingerprint": fp}
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is False

    def test_malformed_contract_body_sha256_is_not_ready(self):
        fp = dict(_VALID_FINGERPRINT)
        fp["contract_body_sha256"] = "not-a-hash"
        inner = {"expected_contract_fingerprint": fp}
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is False

    def test_allowed_paths_hash_with_sha256_prefix_is_not_ready(self):
        """allowed_paths_normalized_sha256 must be a bare hex digest to match
        AllowedPathsGateEvaluator.compute_allowed_paths_hash() exactly."""
        fp = dict(_VALID_FINGERPRINT)
        fp["allowed_paths_normalized_sha256"] = "sha256:" + "b" * 64
        inner = {"expected_contract_fingerprint": fp}
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is False

    def test_empty_base_ref_is_not_ready(self):
        fp = dict(_VALID_FINGERPRINT)
        fp["base_ref"] = ""
        inner = {"expected_contract_fingerprint": fp}
        assert is_fingerprint_ready_go(inner, 1001, _ISSUE_NUMBER) is False

    def test_missing_authoritative_comment_or_issue_context_is_not_ready(self):
        inner = {
            "body_sha256": _VALID_FINGERPRINT["contract_body_sha256"],
            "expected_contract_fingerprint": dict(_VALID_FINGERPRINT),
        }
        assert is_fingerprint_ready_go(inner) is False


class TestFindLatestGoFingerprintReadyOnly:
    """Tests for find_latest_go(fingerprint_ready_only=True) (#1537 AC2/AC3)."""

    def test_excludes_go_without_fingerprint(self):
        results = [
            {
                "status": "go",
                "created_at": "2026-01-01",
                "comment_id": 1,
                "html_url": "url1",
                "is_fingerprint_ready": False,
            },
        ]
        assert find_latest_go(results, fingerprint_ready_only=True) is None

    def test_includes_go_with_fingerprint(self):
        results = [
            {
                "status": "go",
                "created_at": "2026-01-01",
                "comment_id": 1,
                "html_url": "url1",
                "is_fingerprint_ready": True,
            },
        ]
        r = find_latest_go(results, fingerprint_ready_only=True)
        assert r is not None
        assert r["html_url"] == "url1"

    def test_default_does_not_require_fingerprint(self):
        """Backward compatibility: fingerprint_ready_only defaults to False."""
        results = [
            {
                "status": "go",
                "created_at": "2026-01-01",
                "comment_id": 1,
                "html_url": "url1",
                "is_fingerprint_ready": False,
            },
        ]
        assert find_latest_go(results) is not None


class TestFindLatest:
    """Tests for find_latest_go and find_latest_result."""

    def test_find_latest_go_returns_go(self):
        results = [
            {"status": "go", "created_at": "2026-01-01", "comment_id": 1, "html_url": "url1"},
        ]
        r = find_latest_go(results)
        assert r is not None
        assert r["status"] == "go"

    def test_find_latest_go_no_go_returns_none(self):
        results = [
            {"status": "blocked", "created_at": "2026-01-01", "comment_id": 1, "html_url": "url1"},
        ]
        r = find_latest_go(results)
        assert r is None

    def test_find_latest_go_picks_newest(self):
        results = [
            {"status": "go", "created_at": "2026-01-01T00:00:00Z", "comment_id": 1, "html_url": "url1"},
            {"status": "go", "created_at": "2026-01-02T00:00:00Z", "comment_id": 2, "html_url": "url2"},
        ]
        r = find_latest_go(results)
        assert r["html_url"] == "url2"

    def test_find_latest_result_returns_newest(self):
        results = [
            {"status": "go", "created_at": "2026-01-01T00:00:00Z", "comment_id": 1, "html_url": "url1"},
            {"status": "blocked", "created_at": "2026-01-02T00:00:00Z", "comment_id": 2, "html_url": "url2"},
        ]
        r = find_latest_result(results)
        assert r["status"] == "blocked"
        assert r["html_url"] == "url2"

    def test_find_latest_result_empty(self):
        r = find_latest_result([])
        assert r is None

    def test_find_latest_go_empty(self):
        r = find_latest_go([])
        assert r is None
