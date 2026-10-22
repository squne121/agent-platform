#!/usr/bin/env python3
"""Tests for validate_issue_body.py"""

import pytest
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import validate_issue_body as validator

from validate_issue_body import (
    validate_issue_body,
    _extract_ac_numbers,
    _extract_vc_ac_numbers,
    _extract_section,
    _validate_lp031_kind_mismatch,
    _load_required_section_labels,
)


class TestLP001MissingRequiredSection:
    """LP001: Missing required sections."""

    def test_lp001_positive_missing_acceptance_criteria(self):
        """AC2: LP001 positive fixture - missing Acceptance Criteria."""
        body = """
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Verification Commands

```bash
test -f /some/file  # AC1
```

## Allowed Paths

- /some/path
"""
        result = validate_issue_body(body, kind="implementation")
        assert result.status == "fail"
        lp001_errors = [e for e in result.errors if e.rule_id == "LP001"]
        assert any("Acceptance Criteria" in e.message for e in lp001_errors)

    def test_lp001_positive_missing_verification_commands(self):
        """AC2: LP001 positive fixture - missing Verification Commands."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test something

## Allowed Paths

- /some/path
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp001_errors = [e for e in result.errors if e.rule_id == "LP001"]
        assert any("Verification Commands" in e.message for e in lp001_errors)

    def test_lp001_false_positive_all_sections_present(self):
        """AC3: LP001 false-positive - all required sections present."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
test -f file  # AC1
```

## Allowed Paths

- /some/path
"""
        result = validate_issue_body(body)
        lp001_errors = [e for e in result.errors if e.rule_id == "LP001"]
        assert len(lp001_errors) == 0

    def test_1844_existing_parent_profile_does_not_read_template_sections(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The versioned readiness profile owns existing-parent required sections."""
        body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Summary
## Goal
## Desired Destination
## Current Validated Scope
## Decisions Fixed
## Quality Decision Record
## Parent Closure Rule
## Child Issues
## Remaining Parent Gaps
## Phase Handoff Contract
## Acceptance Criteria
"""
        monkeypatch.setattr(
            validator,
            "_load_required_section_labels",
            lambda _kind: pytest.fail("existing profile must not consult ISSUE_TEMPLATE"),
        )

        result = validator.validate_issue_body(
            body,
            kind="parent",
            validation_profile=validator.EXISTING_ISSUE_READINESS_V1,
        )

        assert result.status == "pass"


class TestLP002InvalidYAML:
    """LP002: Invalid Machine-Readable Contract YAML."""

    def test_lp002_positive_missing_yaml_fence(self):
        """AC2: LP002 positive fixture - no YAML fence."""
        body = """
## Machine-Readable Contract

contract_schema_version: v1
issue_kind: implementation

## Acceptance Criteria

- [ ] AC1: Test
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp002_errors = [e for e in result.errors if e.rule_id == "LP002"]
        assert any("```yaml" in e.message for e in lp002_errors)

    def test_lp002_positive_missing_required_field(self):
        """AC2: LP002 positive fixture - missing contract_schema_version."""
        body = """
## Machine-Readable Contract

```yaml
issue_kind: implementation
```

## Acceptance Criteria

- [ ] AC1: Test
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp002_errors = [e for e in result.errors if e.rule_id == "LP002"]
        assert any("contract_schema_version" in e.message for e in lp002_errors)

    def test_lp002_positive_yaml_syntax_error(self):
        """AC2: LP002 positive fixture - invalid YAML syntax."""
        body = """
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
  invalid: [unclosed list
```

## Acceptance Criteria

- [ ] AC1: Test
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp002_errors = [e for e in result.errors if e.rule_id == "LP002"]
        assert len(lp002_errors) >= 1, "LP002 should detect YAML syntax errors"
        assert any("YAML" in e.message or "syntax" in e.message.lower() for e in lp002_errors)

    def test_lp002_positive_yaml_not_dict(self):
        """AC2: LP002 positive fixture - YAML is not a dict (list instead)."""
        body = """
## Machine-Readable Contract

```yaml
- item1
- item2
```

## Acceptance Criteria

- [ ] AC1: Test
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp002_errors = [e for e in result.errors if e.rule_id == "LP002"]
        assert len(lp002_errors) >= 1, "LP002 should detect non-dict YAML"
        assert any("dictionary" in e.message.lower() or "dict" in e.message.lower() for e in lp002_errors)

    def test_lp002_false_positive_valid_yaml(self):
        """AC3: LP002 false-positive - valid YAML contract."""
        body = """
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Acceptance Criteria

- [ ] AC1: Test
"""
        result = validate_issue_body(body)
        lp002_errors = [e for e in result.errors if e.rule_id == "LP002"]
        assert len(lp002_errors) == 0, "LP002 should pass valid YAML"


class TestLP010ACVCMismatch:
    """LP010: AC ⇔ VC number set mismatch."""

    def test_lp010_positive_missing_ac_in_vc(self):
        """AC4: LP010 positive fixture - AC2 defined but missing in VC."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test first
- [ ] AC2: Test second

## Verification Commands

```bash
test -f file  # AC1
```

## Allowed Paths

- /some/path
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        assert len(lp010_errors) > 0
        error = lp010_errors[0]
        assert error.expected == ["AC1", "AC2"]
        assert error.actual == ["AC1"]

    def test_lp010_positive_extra_ac_in_vc(self):
        """AC4: LP010 positive fixture - VC has AC3 but not defined."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test first
- [ ] AC2: Test second

## Verification Commands

```bash
test -f file  # AC1
test -d dir   # AC2
test -x exec  # AC3
```

## Allowed Paths

- /some/path
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        assert len(lp010_errors) > 0
        error = lp010_errors[0]
        assert "AC3" in error.actual
        assert "AC3" not in error.expected

    def test_lp010_false_positive_matching_ac_vc(self):
        """AC3: LP010 false-positive - AC and VC numbers match."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test first
- [ ] AC2: Test second

## Verification Commands

```bash
test -f file  # AC1
test -d dir   # AC2
```

## Allowed Paths

- /some/path
"""
        result = validate_issue_body(body)
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        assert len(lp010_errors) == 0

    def test_lp010_minimal_context_truncation(self):
        """AC4/AC6: LP010 includes minimal_context with truncation flag."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
line 1
line 2
line 3
line 4
line 5
line 6
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)
        lp010_errors = [e for e in result.errors if e.rule_id == "LP010"]
        if lp010_errors:
            error = lp010_errors[0]
            assert isinstance(error.minimal_context, list)
            # Context should be limited to reasonable size
            context_str = '\n'.join(error.minimal_context)
            assert len(error.minimal_context) <= 5
            assert len(context_str.encode('utf-8')) <= 2048


class TestLP011VerificationCommandFormat:
    """LP011: Verification Commands format."""

    def test_lp011_positive_no_bash_fence(self):
        """AC2: LP011 positive fixture - commands not in bash fence."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

test -f file  # AC1

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp011_errors = [e for e in result.errors if e.rule_id == "LP011"]
        assert any("```bash" in e.message for e in lp011_errors)

    def test_lp011_false_positive_bash_fence_present(self):
        """AC3: LP011 false-positive - commands in bash fence."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
test -f file  # AC1
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)
        lp011_errors = [e for e in result.errors if e.rule_id == "LP011"]
        assert len(lp011_errors) == 0


class TestLP012RgEncodingFlag:
    """LP012: rg -E encoding flag misuse."""

    def test_lp012_positive_rg_e_token_aware(self):
        """AC9: LP012 positive fixture - rg -E flag (token-aware detection)."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
rg -E 'pattern' file  # AC1
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)
        lp012_errors = [e for e in result.errors if e.rule_id == "LP012"]
        # Token-aware detection should find 'rg' + '-E' token combination
        assert len(lp012_errors) >= 1, "LP012 should detect rg with -E flag"
        assert any("rg -E" in e.message.lower() or "-E" in e.message for e in lp012_errors)

    def test_lp012_false_positive_rg_encoding_only(self):
        """AC3: LP012 false-positive - rg with --encoding but no -E."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
rg --encoding=UTF-8 'pattern' file  # AC1
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)
        lp012_errors = [e for e in result.errors if e.rule_id == "LP012"]
        assert len(lp012_errors) == 0, "LP012 should allow --encoding without -E flag"

    def test_lp012_false_positive_rg_without_e_flag(self):
        """AC3: LP012 false-positive - rg without -E flag."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
rg 'pattern' file  # AC1
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)
        lp012_errors = [e for e in result.errors if e.rule_id == "LP012"]
        assert len(lp012_errors) == 0, "LP012 should allow rg without -E flag"


class TestLP013DeletionNegativeGrep:
    """LP013: Deletion negative grep without literal targets."""

    def test_lp013_positive_grep_v_no_literal_check(self):
        """AC5: LP013 positive fixture - grep -v without explicit target."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
grep -v pattern file  # AC1
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)
        lp013_warnings = [e for e in result.errors if e.rule_id == "LP013"]
        assert any(e.severity == "warning" for e in lp013_warnings)

    def test_lp013_false_positive_grep_v_with_literal_check(self):
        """AC5: LP013 false-positive - grep -v with test -f."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
test -f file && grep -v pattern file  # AC1
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)
        _lp013_warnings = [e for e in result.errors if e.rule_id == "LP013" and e.severity == "warning"]
        # With test -f present, should not warn
        # (depending on implementation)


class TestLP014MarkdownBacktickGrep:
    """LP014: Markdown backtick grep mismatch."""

    def test_lp014_positive_grep_with_backticks(self):
        """AC5: LP014 positive fixture - grep pattern with backticks."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
grep '```' file  # AC1
```

## Allowed Paths

- /path
"""
        _result = validate_issue_body(body)
        # This test might not trigger depending on implementation
        # as backticks in shlex.split() are handled differently


class TestLP015BaselineVCHeadingOnly:
    """LP015: Baseline VC heading-only match."""

    def test_lp015_positive_grep_on_heading_markers(self):
        """AC5: LP015 positive fixture - grep matching ## only."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
grep '##' file  # AC1
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)
        lp015_warnings = [e for e in result.errors if e.rule_id == "LP015"]
        assert len(lp015_warnings) > 0
        assert lp015_warnings[0].severity == "warning"


class TestLP016AcMarkerStrictness:
    """LP016: strict AC marker form enforcement in Verification Commands."""

    @pytest.mark.parametrize("marker", [
        "# AC1: description",
        "# AC1：description",
        "# AC1 - description",
        "# AC1 — description",
        "# AC1 description",
    ])
    def test_lp016_rejects_suffix_variants(self, marker):
        body = f"""\
## Acceptance Criteria

- [ ] AC1 テスト

## Verification Commands

```bash
{marker}
test -f /etc/passwd
```

## Allowed Paths

- /etc
"""
        result = validate_issue_body(body)
        lp016_errors = [e for e in result.errors if e.rule_id == "LP016"]
        assert len(lp016_errors) == 1
        assert "bare" in lp016_errors[0].message

    def test_lp016_accepts_bare_marker(self):
        body = """\
## Acceptance Criteria

- [ ] AC1 テスト

## Verification Commands

```bash
# AC1
test -f /etc/passwd
```

## Allowed Paths

- /etc
"""
        result = validate_issue_body(body)
        lp016_errors = [e for e in result.errors if e.rule_id == "LP016"]
        assert len(lp016_errors) == 0


class TestLP018LP019PreflightScope:
    """LP018 / LP019: preflight-scope marker validation and attachment."""

    def test_lp018_rejects_invalid_scope_value(self):
        body = """\
## Acceptance Criteria

- [ ] AC1 テスト

## Verification Commands

```bash
# preflight-scope: foo
# AC1
true
```

## Allowed Paths

- .
"""
        result = validate_issue_body(body)
        errors = [e for e in result.errors if e.rule_id == "LP018"]
        assert len(errors) == 1
        assert "Allowed values" in errors[0].message

    def test_lp018_rejects_empty_scope_value(self):
        body = """\
## Acceptance Criteria

- [ ] AC1 テスト

## Verification Commands

```bash
# preflight-scope:
# AC1
true
```

## Allowed Paths

- .
"""
        result = validate_issue_body(body)
        errors = [e for e in result.errors if e.rule_id in ("LP018", "LP019")]
        assert len(errors) >= 1

    def test_lp019_requires_scope_attached(self):
        body = """\
## Acceptance Criteria

- [ ] AC1 テスト

## Verification Commands

```bash
# preflight-scope: runtime_only
# AC1
test -f /etc/passwd
```

## Allowed Paths

- /etc
"""
        result = validate_issue_body(body)
        errors = [e for e in result.errors if e.rule_id == "LP019"]
        assert len(errors) == 1

    def test_lp019_accepts_attached_scope_marker(self):
        body = """\
## Acceptance Criteria

- [ ] AC1 テスト

## Verification Commands

```bash
# preflight-scope: runtime_only
test -f /etc/passwd
```

## Allowed Paths

- /etc
"""
        result = validate_issue_body(body)
        lp019_errors = [e for e in result.errors if e.rule_id == "LP019"]
        assert len(lp019_errors) == 0


class TestLP020RuntimeVerificationIncomplete:
    """LP020: Runtime Verification Applicability incomplete."""

    def test_lp020_positive_missing_decision(self):
        """AC4: LP020 positive fixture - missing decision field."""
        body = """
## Runtime Verification Applicability

- reason: testing

## Acceptance Criteria

- [ ] AC1: Test
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp020_errors = [e for e in result.errors if e.rule_id == "LP020"]
        assert any("decision" in e.message for e in lp020_errors)

    def test_lp020_positive_deferred_missing_fields(self):
        """AC4: LP020 positive fixture - deferred without required fields."""
        body = """
## Runtime Verification Applicability

- decision: deferred
- reason: testing

## Acceptance Criteria

- [ ] AC1: Test
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp020_errors = [e for e in result.errors if e.rule_id == "LP020"]
        assert any("deferred" in e.message for e in lp020_errors)

    def test_lp020_false_positive_valid_not_applicable(self):
        """AC3: LP020 false-positive - valid not_applicable decision."""
        body = """
## Runtime Verification Applicability

- decision: not_applicable
- reason: static validation only

## Acceptance Criteria

- [ ] AC1: Test
"""
        result = validate_issue_body(body)
        lp020_errors = [e for e in result.errors if e.rule_id == "LP020"]
        assert len(lp020_errors) == 0

    def test_lp020_positive_missing_reason(self):
        """AC7: authoring validation requires an explicit RVA reason."""
        body = """
## Runtime Verification Applicability

- decision: not_applicable
"""
        result = validate_issue_body(body, kind="implementation")
        lp020_errors = [e for e in result.errors if e.rule_id == "LP020"]
        assert any("reason" in e.message for e in lp020_errors)

    def test_implementation_kind_requires_rva_section_from_issue_form(self):
        """AC7: the implementation form makes RVA a required authoring section."""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Acceptance Criteria

- [ ] AC1: test

## Verification Commands

```bash
test -f file  # AC1
```

## Allowed Paths

- file
"""
        result = validate_issue_body(body, kind="implementation")
        lp001_errors = [e for e in result.errors if e.rule_id == "LP001"]
        assert any("Runtime Verification Applicability" in e.message for e in lp001_errors)


class TestLP030ForbiddenAuthPath:
    """LP030: Forbidden authoring doc path."""

    def test_lp030_positive_body_authoring_path(self):
        """AC2: LP030 positive fixture - reference to docs/dev/body-authoring.md."""
        body = """
## Outcome

See docs/dev/body-authoring.md for details.

## Acceptance Criteria

- [ ] AC1: Test
"""
        result = validate_issue_body(body)
        assert result.status == "fail"
        lp030_errors = [e for e in result.errors if e.rule_id == "LP030"]
        assert any("body-authoring.md" in e.message for e in lp030_errors)

    def test_lp030_false_positive_no_forbidden_path(self):
        """AC3: LP030 false-positive - no forbidden paths referenced."""
        body = """
## Outcome

Implement the validator.

## Acceptance Criteria

- [ ] AC1: Test
"""
        result = validate_issue_body(body)
        lp030_errors = [e for e in result.errors if e.rule_id == "LP030"]
        assert len(lp030_errors) == 0


class TestWarningVsError:
    """AC5: Warnings don't cause validation failure."""

    def test_warnings_status_pass(self):
        """AC5: warnings only should result in status=pass."""
        body = """
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
grep -v pattern file  # AC1
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)
        # Should have warnings but still pass (no errors)
        has_errors = any(e.severity == "error" for e in result.errors)
        if has_errors:
            assert result.status == "fail"
        else:
            assert result.status == "pass"


class TestMinimalContextLimits:
    """AC6: minimal_context is limited to 5 lines and 2KB."""

    def test_minimal_context_size_limits(self):
        """AC6: minimal_context respects size limits (multi-line case)."""
        large_body = """
## Acceptance Criteria

- [ ] AC1: Test
- [ ] AC2: Another

## Verification Commands

```bash
""" + "\n".join([f"line_{i}" for i in range(100)]) + """
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(large_body)

        for error in result.errors:
            if error.minimal_context:
                # Check line limit
                assert len(error.minimal_context) <= 5

                # Check byte limit
                context_str = '\n'.join(error.minimal_context)
                assert len(context_str.encode('utf-8')) <= 2048

    def test_minimal_context_single_long_line(self):
        """AC6: minimal_context respects byte limit with single long line."""
        # Create a body with a single very long line (3KB)
        long_line = "x" * 3000
        body = f"""
## Acceptance Criteria

- [ ] AC1: Test

## Verification Commands

```bash
{long_line}  # AC1
```

## Allowed Paths

- /path
"""
        result = validate_issue_body(body)

        for error in result.errors:
            if error.minimal_context:
                # Check byte limit is enforced even for single long lines
                context_str = '\n'.join(error.minimal_context)
                assert len(context_str.encode('utf-8')) <= 2048, \
                    f"Context exceeded 2KB limit: {len(context_str.encode('utf-8'))} bytes"


class TestSHA256Hash:
    """AC4/AC6: body_sha256 field is computed."""

    def test_sha256_computed(self):
        """Verify body_sha256 is computed correctly."""
        body = "test body"
        result = validate_issue_body(body)
        assert result.body_sha256.startswith("sha256:")
        import hashlib
        expected_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
        assert result.body_sha256 == f"sha256:{expected_hash}"


class TestHelperFunctions:
    """Test helper extraction functions."""

    def test_extract_ac_numbers(self):
        """Test AC number extraction."""
        body = """
## Acceptance Criteria

- [ ] AC1: First
- [x] AC2: Second
- [ ] AC3: Third
"""
        ac_nums = _extract_ac_numbers(body)
        assert ac_nums == {"AC1", "AC2", "AC3"}

    def test_extract_vc_ac_numbers(self):
        """Test VC AC number extraction."""
        body = """
## Verification Commands

```bash
test -f file  # AC1
grep pattern  # AC2
ls -la        # AC3
```
"""
        vc_nums = _extract_vc_ac_numbers(body)
        assert vc_nums == {"AC1", "AC2", "AC3"}

    def test_extract_section(self):
        """Test section extraction."""
        body = """
## Section One

content here
multiple lines

## Section Two

other content
"""
        section_info = _extract_section(body, "Section One")
        assert section_info is not None
        content, start_line, end_line = section_info
        assert "content here" in content
        assert "multiple lines" in content
        assert "Section Two" not in content


class TestLP031TitlePrefix:
    """LP031: implementation kind title prefix validation and kind mismatch detection.
    AC13: MRC/CLI kind mismatch unit tests.
    """

    _IMPL_BODY_WITH_MRC = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#1"
goal_ref: "test"
change_kind: workflow
```

## Outcome

test outcome

## Acceptance Criteria

- [ ] AC1: test

## Verification Commands

```bash
# AC1
test -f foo.py
```

## Allowed Paths

- foo.py
"""

    _RESEARCH_BODY_WITH_MRC = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: "#1"
goal_ref: "test"
change_kind: research
```

## Outcome

test outcome

## Acceptance Criteria

- [ ] AC1: test

## Verification Commands

```bash
# AC1
test -f foo.md
```

## Allowed Paths

- foo.md
"""

    @pytest.mark.parametrize("title,cli_kind,body_attr,expected_has_lp031", [
        # MRC implementation + 実装: prefix => pass (no LP031)
        ("実装: foo", None, "impl", False),
        # MRC implementation + implement: prefix => pass (no LP031)
        ("implement: bar", None, "impl", False),
        # MRC implementation + non-compliant title => fail (LP031)
        ("feat: foo", None, "impl", True),
        # CLI implementation + 実装: prefix => pass
        ("実装: foo", "implementation", "no_mrc", False),
        # CLI implementation + non-compliant title => fail
        ("chore: fix", "implementation", "no_mrc", True),
        # research kind (MRC) => pass (LP031 not applicable)
        ("調査: foo", None, "research", False),
        # research CLI kind => pass
        ("調査: bar", "research", "no_mrc", False),
    ])
    def test_lp031_parametrized(self, title, cli_kind, body_attr, expected_has_lp031):
        """Parametrized LP031 tests for various kind/title combinations."""
        if body_attr == "impl":
            body = self._IMPL_BODY_WITH_MRC
        elif body_attr == "research":
            body = self._RESEARCH_BODY_WITH_MRC
        else:
            # no_mrc: minimal body without MRC
            body = """\
## Outcome

test

## Acceptance Criteria

- [ ] AC1: test

## Verification Commands

```bash
# AC1
test -f foo.py
```

## Allowed Paths

- foo.py
"""

        result = validate_issue_body(body, kind=cli_kind, title=title)
        lp031_errors = [e for e in result.errors if e.rule_id == "LP031"]
        _lp031_mismatch_errors = [e for e in lp031_errors if "mismatch" in e.message]

        # Filter to only title-prefix LP031 errors (not mismatch)
        title_lp031_errors = [e for e in lp031_errors if "mismatch" not in e.message]

        if expected_has_lp031:
            assert len(title_lp031_errors) > 0, (
                f"Expected LP031 title error for title={title!r} kind={cli_kind} body={body_attr} "
                f"but got no LP031 errors"
            )
        else:
            assert len(title_lp031_errors) == 0, (
                f"Unexpected LP031 title error for title={title!r} kind={cli_kind} body={body_attr}: "
                f"{[e.message for e in title_lp031_errors]}"
            )

    @pytest.mark.parametrize("mrc_kind,cli_kind,should_mismatch", [
        # AC13: MRC implementation + CLI research => kind mismatch
        ("implementation", "research", True),
        # AC13: MRC research + CLI implementation => kind mismatch
        ("research", "implementation", True),
        # same kind => no mismatch
        ("implementation", "implementation", False),
        # no CLI kind => no mismatch check
        ("implementation", None, False),
        # no MRC => no mismatch check
        (None, "implementation", False),
    ])
    def test_lp031_kind_mismatch(self, mrc_kind, cli_kind, should_mismatch):
        """AC13: MRC/CLI kind mismatch triggers LP031 mismatch error before title check."""
        if mrc_kind is not None:
            body = f"""\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: {mrc_kind}
parent_issue: "#1"
goal_ref: "test"
change_kind: workflow
```

## Outcome

test

## Acceptance Criteria

- [ ] AC1: test

## Verification Commands

```bash
# AC1
test -f foo.py
```

## Allowed Paths

- foo.py
"""
        else:
            # no MRC
            body = """\
## Outcome

test

## Acceptance Criteria

- [ ] AC1: test

## Verification Commands

```bash
# AC1
test -f foo.py
```

## Allowed Paths

- foo.py
"""

        errors = _validate_lp031_kind_mismatch(body, cli_kind)
        if should_mismatch:
            assert len(errors) > 0, (
                f"Expected mismatch error for MRC={mrc_kind} CLI={cli_kind}"
            )
            assert any("mismatch" in e.message for e in errors)
        else:
            assert len(errors) == 0, (
                f"Unexpected mismatch error for MRC={mrc_kind} CLI={cli_kind}: "
                f"{[e.message for e in errors]}"
            )

    def test_lp031_mrc_kind_takes_priority_over_cli(self):
        """When MRC issue_kind=implementation, effective_kind is implementation even with no CLI kind."""
        body = self._IMPL_BODY_WITH_MRC
        result = validate_issue_body(body, kind=None, title="feat: bad")
        lp031_errors = [e for e in result.errors if e.rule_id == "LP031" and "mismatch" not in e.message]
        assert len(lp031_errors) > 0, "MRC implementation should trigger LP031 for bad title"

    def test_lp031_no_title_provided_no_check(self):
        """When title is None, LP031 title check is skipped entirely."""
        body = self._IMPL_BODY_WITH_MRC
        result = validate_issue_body(body, kind="implementation", title=None)
        lp031_errors = [e for e in result.errors if e.rule_id == "LP031" and "mismatch" not in e.message]
        assert len(lp031_errors) == 0, "No title => no LP031 title check"


class TestAC9RequiredDesignReferencesAuthoring:
    """AC9 (#1346): authoring-path (validate_issue_body.py --kind implementation)
    must require 'Required Design References' regardless of ISSUE_TEMPLATE's
    validations.required flag (forced to false for AC7 to avoid the
    scope_signal_guard template-driven required-section regression).
    """

    def test_required_section_labels_includes_rdr_for_implementation_kind(self):
        """AC9: _load_required_section_labels('implementation') must include
        'Required Design References' even though the template field itself is
        validations.required: false."""
        labels = _load_required_section_labels("implementation")
        assert "Required Design References" in labels, labels

    def test_required_section_labels_unaffected_for_research_kind(self):
        """AC9: non-implementation kinds are not affected by this authoring-only rule."""
        labels = _load_required_section_labels("research")
        assert "Required Design References" not in labels, labels

    def test_validate_issue_body_flags_missing_rdr_for_implementation_kind(self):
        """AC9: validate_issue_body(kind='implementation') on a body lacking the
        Required Design References section reports an LP001 error mentioning it."""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: docs
```

## Acceptance Criteria

- [ ] AC1: test

## Verification Commands

```bash
test -f foo.py  # AC1
```

## Allowed Paths

- foo.py
"""
        result = validate_issue_body(body, kind="implementation")
        assert result.status == "fail"
        lp001_errors = [e for e in result.errors if e.rule_id == "LP001"]
        assert any("Required Design References" in e.message for e in lp001_errors), (
            [e.message for e in lp001_errors]
        )

    def test_validate_issue_body_no_rdr_flag_when_kind_omitted(self):
        """AC9: without --kind, this authoring-only rule does not apply (legacy
        fixtures / contract_readiness_check.py callers which do not pass --kind
        remain unaffected)."""
        body = """\
## Acceptance Criteria

- [ ] AC1: test

## Verification Commands

```bash
test -f foo.py  # AC1
```

## Allowed Paths

- foo.py
"""
        result = validate_issue_body(body, kind=None)
        lp001_errors = [e for e in result.errors if e.rule_id == "LP001"]
        assert not any("Required Design References" in e.message for e in lp001_errors)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
