#!/usr/bin/env python3
"""
PR Review Deterministic Gates Checker (G1-G5)

Implements 5 deterministic gates to catch reviewer miss-types:
  G1: CI gate coverage (ci_test_selection/v1 artifact required)
  G2: Evidence binding structure validation
  G3: Implementation oracle verification (Python AST + grep)
  G4: Head SHA consistency check
  G5: Fixture guard path coverage trace
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import ast
import re
from dataclasses import dataclass
from enum import Enum
try:
    import yaml
except ImportError:
    yaml = None


class GateStatus(Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class Verdict(Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


@dataclass
class Finding:
    """Evidence reference structure per finding (per-finding scope)."""
    head_sha: str
    source_kind: str  # e.g., "ci_artifact", "github_api", "local_git"
    # {code_ref?, pr_head_sha?, local_head_sha?, ci_run_ref{url,workflow,job,step,command}?}
    evidence_refs: Dict[str, Any]


@dataclass
class GateResult:
    """Result of a single gate."""
    gate_id: str  # g1, g2, g3, g4, g5
    gate_name: str
    status: str  # pass | fail | not_applicable
    minimal_context: Optional[str] = None  # Only for fail gates
    findings: Optional[List[Finding]] = None


@dataclass
class PRReviewGateResult:
    """PR_REVIEW_GATE_RESULT_V1 schema."""
    schema_version: str = "PR_REVIEW_GATE_RESULT_V1"
    generated_at: str = None
    generated_by: str = "check_pr_review_gates.py"
    verdict: str = "APPROVE"  # APPROVE | REQUEST_CHANGES
    gates: List[GateResult] = None

    def __post_init__(self):
        if self.generated_at is None:
            self.generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if self.gates is None:
            self.gates = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "generated_by": self.generated_by,
            "verdict": self.verdict,
            "gates": [
                {
                    "gate_id": g.gate_id,
                    "gate_name": g.gate_name,
                    "status": g.status,
                    **({"minimal_context": g.minimal_context} if g.minimal_context and g.status == "fail" else {}),
                    **(
                        {
                            "findings": [
                                {
                                    "head_sha": f.head_sha,
                                    "source_kind": f.source_kind,
                                    "evidence_refs": f.evidence_refs,
                                }
                                for f in g.findings
                            ]
                        }
                        if g.findings
                        else {}
                    )
                }
                for g in self.gates
            ]
        }


class CheckPRReviewGates:
    """Main checker class implementing G1-G6."""

    def __init__(self, strict: bool = False):
        self.result = PRReviewGateResult()
        self.strict = strict

    def g1_ci_test_selection(self, artifact_path: Optional[str] = None) -> GateResult:
        """
        G1: CI gate coverage

        Checks CI artifact (schema_version: ci_test_selection/v1) for uncovered test files.
        Requires: HEAD SHA + uncovered_changed_test_files field non-empty → fail

        In strict mode: missing or unreadable artifact → fail
        """
        gate = GateResult(
            gate_id="g1",
            gate_name="ci_test_selection",
            status=GateStatus.NOT_APPLICABLE.value
        )

        if not artifact_path or not Path(artifact_path).exists():
            if self.strict:
                gate.status = GateStatus.FAIL.value
                gate.minimal_context = "G1: required --ci-artifact missing or unreadable"
            else:
                gate.status = GateStatus.NOT_APPLICABLE.value
            return gate

        try:
            with open(artifact_path) as f:
                artifact = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            if self.strict:
                gate.status = GateStatus.FAIL.value
                gate.minimal_context = "G1: required --ci-artifact missing or unreadable"
            else:
                gate.status = GateStatus.NOT_APPLICABLE.value
            return gate

        # Verify schema version
        if artifact.get("schema_version") != "ci_test_selection/v1":
            gate.status = GateStatus.NOT_APPLICABLE.value
            return gate

        head_sha = artifact.get("pr_head_sha") or artifact.get("head_sha")
        uncovered = artifact.get("uncovered_changed_test_files", [])

        if uncovered:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = f"Uncovered changed test files: {', '.join(uncovered)}"
            gate.findings = [Finding(
                head_sha=head_sha or "unknown",
                source_kind="ci_artifact",
                evidence_refs={
                    "ci_run_ref": {
                        "url": artifact.get("ci_run_url", "N/A"),
                        "workflow": artifact.get("workflow", "N/A"),
                        "job": artifact.get("job", "N/A"),
                        "command": artifact.get("collected_test_files", [])
                    }
                }
            )]
        else:
            gate.status = GateStatus.PASS.value

        return gate

    def g2_evidence_binding(self, pr_body: str = "", pr_head_sha: str = "", local_head_sha: str = "") -> GateResult:
        """
        G2: Evidence binding structure validation

        Extracts fenced code blocks (YAML/JSON) and validates findings structure:
        - Each finding must have: head_sha, source_kind, evidence_refs
        - evidence_refs must have at least one supporting ref (not self_report_only)
        - Supporting refs: code_ref, pr_head_sha, local_head_sha, ci_run_ref

        Fails if:
        - No findings block found (strict: fail / lenient: fail if "findings" keyword present)
        - All findings blocks invalid (missing required keys or self_report_only)
        """
        gate = GateResult(
            gate_id="g2",
            gate_name="evidence_binding",
            status=GateStatus.PASS.value
        )

        if not pr_body:
            if self.strict:
                gate.status = GateStatus.FAIL.value
                gate.minimal_context = "G2: required --pr-body missing or empty in strict review mode"
            else:
                gate.status = GateStatus.NOT_APPLICABLE.value
            return gate

        # Extract fenced code blocks
        findings_blocks = self._extract_findings_blocks(pr_body)

        if not findings_blocks:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = "G2: no structured findings block found in PR body"
            return gate

        # Validate findings blocks
        valid_findings = []
        for block in findings_blocks:
            if self._validate_findings_structure(block):
                valid_findings.extend(block)

        if not valid_findings:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = (
                "G2: all findings lack proper evidence_refs (only self_report or missing required keys)"
            )
            return gate

        gate.status = GateStatus.PASS.value
        return gate

    @staticmethod
    def _coerce_scalar(value: str) -> Any:
        value = value.strip()
        if value in {"true", "false"}:
            return value == "true"
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                return []
            return [item.strip().strip("'\"") for item in inner.split(",")]
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            return value[1:-1]
        return value

    def _parse_findings_yaml_block(self, text: str) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        in_evidence_refs = False
        in_ci_run_ref = False

        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped == "findings:":
                continue
            if stripped.startswith("- "):
                if current:
                    findings.append(current)
                current = {}
                in_evidence_refs = False
                in_ci_run_ref = False
                stripped = stripped[2:].strip()
                if stripped and ":" in stripped:
                    key, value = stripped.split(":", 1)
                    current[key.strip()] = self._coerce_scalar(value)
                continue
            if current is None or ":" not in stripped:
                continue

            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip()

            if key == "evidence_refs" and not value:
                current.setdefault("evidence_refs", {})
                in_evidence_refs = True
                in_ci_run_ref = False
                continue
            if in_evidence_refs and key == "ci_run_ref" and not value:
                current.setdefault("evidence_refs", {})["ci_run_ref"] = {}
                in_ci_run_ref = True
                continue
            if in_evidence_refs and in_ci_run_ref and key in {"url", "workflow", "job", "step", "command"}:
                current["evidence_refs"]["ci_run_ref"][key] = self._coerce_scalar(value)
                continue
            if in_evidence_refs:
                current.setdefault("evidence_refs", {})[key] = self._coerce_scalar(value)
                in_ci_run_ref = False
                continue

            current[key] = self._coerce_scalar(value)

        if current:
            findings.append(current)
        return findings

    def _extract_findings_blocks(self, pr_body: str) -> List[List[Dict[str, Any]]]:
        """Extract and parse fenced code blocks containing findings."""
        blocks = []

        # Match fenced code blocks with optional language specifiers
        pattern = r'```(?:\w+)?\n([\s\S]*?)\n```'
        matches = re.findall(pattern, pr_body)

        for match in matches:
            try:
                if yaml:
                    parsed = yaml.safe_load(match)
                else:
                    # Fallback to JSON
                    parsed = json.loads(match)

                if isinstance(parsed, dict) and "findings" in parsed:
                    if isinstance(parsed["findings"], list):
                        blocks.append(parsed["findings"])
                        continue
            except (yaml.YAMLError if yaml else Exception, json.JSONDecodeError):
                pass

            parsed_findings = self._parse_findings_yaml_block(match)
            if parsed_findings:
                blocks.append(parsed_findings)

        return blocks

    def _validate_findings_structure(self, findings: List[Dict[str, Any]]) -> bool:
        """Validate each finding has required keys and valid evidence_refs."""
        if not findings:
            return False

        valid_count = 0
        for finding in findings:
            if not isinstance(finding, dict):
                continue

            # Check required keys
            if not all(k in finding for k in ["head_sha", "source_kind", "evidence_refs"]):
                continue

            # Validate evidence_refs
            evidence_refs = finding.get("evidence_refs", {})
            if not isinstance(evidence_refs, dict):
                continue

            # Check for self_report_only violation
            if evidence_refs.get("self_report_only") is True:
                if not any(k in evidence_refs for k in ["code_ref", "pr_head_sha", "local_head_sha", "ci_run_ref"]):
                    continue

            # Check for at least one supporting ref
            if any(k in evidence_refs for k in ["code_ref", "pr_head_sha", "local_head_sha", "ci_run_ref"]):
                valid_count += 1

        return valid_count > 0

    def g3_implementation_oracle(
        self,
        issue_body: str = "",
        target_files: Optional[List[str]] = None,
        method: str = "ast"
    ) -> GateResult:
        """
        G3: Implementation oracle verification

        Parses implementation_oracles: YAML block from issue body.
        For kind: ast or python_ast_call → uses AST-based verification with alias resolution.
        Falls back to grep (strict: kind: grep only) or returns fail.

        Handles import aliases:
          - import subprocess as sp → sp.run(...) matches subprocess.run
          - from subprocess import run → run(...) matches subprocess.run
        """
        gate = GateResult(
            gate_id="g3",
            gate_name="implementation_oracle",
            status=GateStatus.NOT_APPLICABLE.value
        )

        if not issue_body or "implementation_oracles:" not in issue_body:
            if self.strict and target_files:
                gate.status = GateStatus.FAIL.value
                gate.minimal_context = "G3: implementation_oracles block required but --target-files provided"
            return gate

        oracles = self._parse_implementation_oracles(issue_body)
        if not oracles:
            if self.strict and target_files:
                gate.status = GateStatus.FAIL.value
                gate.minimal_context = "G3: implementation_oracles parsing failed but --target-files provided"
            return gate

        gate.status = GateStatus.PASS.value
        findings: List[Finding] = []
        failed_oracles = []

        for oracle in oracles:
            oracle_id = oracle.get("id", "unknown")
            oracle_kind = oracle.get("kind", "ast")
            files = oracle.get("files", target_files or [])

            if not files:
                continue

            result = None
            if oracle_kind in ["ast", "python_ast_call"]:
                result = self._verify_ast_call(oracle, files)
            elif oracle_kind == "grep":
                result = self._verify_grep(oracle, files)
            else:
                result = {"passed": False, "error": f"unknown oracle kind: {oracle_kind}"}

            if not result.get("passed"):
                failed_oracles.append((oracle_id, result.get("error", "unknown error")))
                findings.append(Finding(
                    head_sha=result.get("head_sha", "unknown"),
                    source_kind=oracle_kind,
                    evidence_refs={"oracle_id": oracle_id, "error": result.get("error", "unknown")}
                ))

        if failed_oracles:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = (
                "Oracle verification failed: "
                f"{', '.join([f'{id}: {err}' for id, err in failed_oracles])}"
            )
            gate.findings = findings

        return gate

    def _parse_implementation_oracles(self, issue_body: str) -> List[Dict[str, Any]]:
        """Parse implementation_oracles: YAML block from issue body."""
        oracles: List[Dict[str, Any]] = []

        # Find the implementation_oracles: line and extract the block
        lines = issue_body.split("\n")
        start_idx = None
        for i, line in enumerate(lines):
            if "implementation_oracles:" in line:
                start_idx = i
                break

        if start_idx is None:
            return oracles

        # Collect lines following implementation_oracles:
        block_lines = []
        for i in range(start_idx + 1, len(lines)):
            line = lines[i]
            if line.strip() == "":
                continue
            if line[0] not in (" ", "\t", "-"):
                break
            block_lines.append(line)

        if not block_lines:
            return oracles

        block_text = "\n".join(block_lines)
        if yaml:
            try:
                parsed = yaml.safe_load(block_text)
                if isinstance(parsed, list):
                    return parsed
                if isinstance(parsed, dict):
                    if "oracles" in parsed:
                        return parsed["oracles"] if isinstance(parsed["oracles"], list) else []
                    return [parsed] if any(k in parsed for k in ["id", "kind", "must_call"]) else []
            except yaml.YAMLError:
                pass

        current: Optional[Dict[str, Any]] = None
        in_must_call = False
        for raw_line in block_lines:
            stripped = raw_line.strip()
            if stripped.startswith("- "):
                if current:
                    oracles.append(current)
                current = {}
                in_must_call = False
                stripped = stripped[2:].strip()
                if stripped and ":" in stripped:
                    key, value = stripped.split(":", 1)
                    current[key.strip()] = self._coerce_scalar(value)
                continue
            if current is None or ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key == "must_call" and not value:
                current["must_call"] = {}
                in_must_call = True
                continue
            if in_must_call and key in {"module", "function"}:
                current.setdefault("must_call", {})[key] = self._coerce_scalar(value)
                continue
            current[key] = self._coerce_scalar(value)
            in_must_call = False

        if current:
            oracles.append(current)

        return oracles

    def _validate_non_comment_literal_match(self, filepath: str, literal: str) -> bool:
        try:
            with open(filepath) as handle:
                for raw_line in handle:
                    stripped = raw_line.lstrip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if literal in raw_line:
                        return True
        except (FileNotFoundError, OSError):
            return False
        return False

    def _verify_ast_call(self, oracle: Dict[str, Any], files: List[str]) -> Dict[str, Any]:
        """Verify must_call using Python AST analysis with alias resolution."""
        must_call = oracle.get("must_call", {})
        module = must_call.get("module")
        function = must_call.get("function")

        if not module or not function:
            return {"passed": False, "error": "must_call missing module or function"}

        found_calls = []
        for filepath in files:
            if not Path(filepath).exists():
                continue
            try:
                with open(filepath) as f:
                    tree = ast.parse(f.read())
                calls = self._find_ast_calls_with_aliases(tree, module, function)
                found_calls.extend(calls)
            except (SyntaxError, UnicodeDecodeError):
                continue

        if found_calls:
            return {"passed": True, "head_sha": "ast-verified"}

        # Try grep fallback only if oracle explicitly specifies kind: grep
        if oracle.get("kind") == "grep":
            return self._verify_grep(oracle, files)

        return {
            "passed": False,
            "error": f"AST call not found: {module}.{function} in {files}",
            "head_sha": "unknown"
        }

    def _find_ast_calls_with_aliases(self, tree: ast.AST, module: str, function: str) -> List[str]:
        """Find calls to module.function with import alias resolution."""
        # Build alias tables
        alias_table = {}  # maps alias to actual module
        from_table = {}   # maps imported name to (module, name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # import M [as A] → alias_table[A or M] = M
                    key = alias.asname if alias.asname else alias.name
                    alias_table[key] = alias.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    # from M import F [as G] → from_table[G or F] = (M, F)
                    key = alias.asname if alias.asname else alias.name
                    from_table[key] = (mod, alias.name)

        # Find calls
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Match Name.Attr(...) pattern
                if isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        caller_name = node.func.value.id
                        func_name = node.func.attr
                        # Check direct match
                        if caller_name == module and func_name == function:
                            calls.append(f"{module}.{function}")
                        # Check alias match: import subprocess as sp; sp.run(...)
                        elif alias_table.get(caller_name) == module and func_name == function:
                            calls.append(f"{module}.{function}")
                # Match Name(...) pattern
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id
                    # Check from_table match: from subprocess import run; run(...)
                    if func_name in from_table:
                        mod, name = from_table[func_name]
                        if mod == module and name == function:
                            calls.append(f"{module}.{function}")

        return calls

    def _verify_grep(self, oracle: Dict[str, Any], files: List[str]) -> Dict[str, Any]:
        """Fallback verification using grep with stricter matching (exclude comments)."""
        must_call = oracle.get("must_call", {})
        function = must_call.get("function", oracle.get("pattern", ""))

        if not function:
            return {"passed": False, "error": "No pattern or function specified"}

        for filepath in files:
            if not Path(filepath).exists():
                continue
            if self._validate_non_comment_literal_match(filepath, function):
                return {"passed": True, "head_sha": "grep-verified"}

        return {
            "passed": False,
            "error": f"Grep pattern not found: {function} in {files}",
            "head_sha": "unknown"
        }

    def g4_head_sha_consistency(self, pr_head_sha: str = "", local_head_sha: str = "") -> GateResult:
        """
        G4: Head SHA consistency

        Compares gh pr view headRefOid with local git rev-parse.
        Mismatch → fail (indicates push incomplete or stale review).

        In strict mode: both SHAs required
        """
        gate = GateResult(
            gate_id="g4",
            gate_name="head_sha_consistency",
            status=GateStatus.PASS.value
        )

        if not pr_head_sha or not local_head_sha:
            if self.strict:
                gate.status = GateStatus.FAIL.value
                gate.minimal_context = (
                    "G4: required --pr-head-sha and --local-head-sha must both be provided in strict review mode"
                )
            else:
                gate.status = GateStatus.NOT_APPLICABLE.value
            return gate

        if pr_head_sha != local_head_sha:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = (
                f"Head SHA mismatch: PR={pr_head_sha[:8]},"
                f" local={local_head_sha[:8]}. Commit may not be pushed."
            )
            gate.findings = [Finding(
                head_sha=pr_head_sha,
                source_kind="github_api_vs_local_git",
                evidence_refs={
                    "pr_head_sha": pr_head_sha,
                    "local_head_sha": local_head_sha,
                    "note": "mismatch indicates incomplete push"
                }
            )]
        else:
            gate.status = GateStatus.PASS.value

        return gate

    def g5_fixture_guard_path_coverage(
        self,
        trace_log: Optional[str] = None,
        coverage_file: Optional[str] = None
    ) -> GateResult:
        """
        G5: Fixture guard path coverage with structured trace validation

        Validates fixture_path_coverage/v1 schema with:
        - schema_version: "fixture_path_coverage/v1"
        - tests: array with test/expected_guard_path/observed_guard_path/status

        Fails if:
        - Parse fails
        - schema_version mismatch
        - tests array empty
        - any test with status != pass or path mismatch
        """
        gate = GateResult(
            gate_id="g5",
            gate_name="fixture_guard_path_coverage",
            status=GateStatus.NOT_APPLICABLE.value
        )

        trace_content = None

        # Try trace_log first
        if trace_log:
            trace_content = trace_log
        # Then try coverage_file
        elif coverage_file and Path(coverage_file).exists():
            try:
                with open(coverage_file) as f:
                    trace_content = f.read()
            except (FileNotFoundError, IOError):
                pass

        if not trace_content:
            if self.strict and (trace_log or coverage_file):
                gate.status = GateStatus.FAIL.value
                gate.minimal_context = "G5: trace parse failed"
            else:
                gate.status = GateStatus.NOT_APPLICABLE.value
            return gate

        # Parse trace content
        try:
            if yaml:
                parsed = yaml.safe_load(trace_content)
            else:
                parsed = json.loads(trace_content)
        except (yaml.YAMLError if yaml else Exception, json.JSONDecodeError):
            parsed = self._parse_fixture_trace_text(trace_content)
            if parsed is None:
                gate.status = GateStatus.FAIL.value
                gate.minimal_context = "G5: trace parse failed"
                return gate

        if not isinstance(parsed, dict):
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = "G5: trace parse failed"
            return gate

        # Validate schema_version
        if parsed.get("schema_version") != "fixture_path_coverage/v1":
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = "G5: trace parse failed"
            return gate

        # Validate tests array
        tests = parsed.get("tests", [])
        if not isinstance(tests, list) or not tests:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = "G5: empty tests array"
            return gate

        # Validate each test
        failed_tests = []
        for test in tests:
            if not isinstance(test, dict):
                failed_tests.append("invalid test entry")
                continue

            expected = test.get("expected_guard_path")
            observed = test.get("observed_guard_path")
            status = test.get("status")

            if expected != observed:
                failed_tests.append(f"{test.get('test', 'unknown')}: path mismatch")
            if status != "pass":
                failed_tests.append(f"{test.get('test', 'unknown')}: status={status}")

        if failed_tests:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = f"G5: {', '.join(failed_tests[:3])}"
            return gate

        gate.status = GateStatus.PASS.value
        return gate

    def _parse_fixture_trace_text(self, text: str) -> Optional[Dict[str, Any]]:
        parsed: Dict[str, Any] = {}
        tests: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None

        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped.startswith("schema_version:"):
                parsed["schema_version"] = self._coerce_scalar(stripped.split(":", 1)[1])
                continue
            if stripped == "tests:":
                continue
            if stripped.startswith("- "):
                if current:
                    tests.append(current)
                current = {}
                stripped = stripped[2:].strip()
                if stripped and ":" in stripped:
                    key, value = stripped.split(":", 1)
                    current[key.strip()] = self._coerce_scalar(value)
                continue
            if current is not None and ":" in stripped:
                key, value = stripped.split(":", 1)
                current[key.strip()] = self._coerce_scalar(value)

        if current:
            tests.append(current)
        if "schema_version" not in parsed or not tests:
            return None
        parsed["tests"] = tests
        return parsed

    def g6_allowed_paths_gate_producer_role_presence(self, pr_body: str = "") -> GateResult:
        """
        G6: allowed_paths_gate / producer_role block presence (Issue #1776)

        Detects PR review bodies that omit the allowed_paths_gate block or
        the producer_role field entirely. Both omissions are treated as
        indeterminate (merge-blocking), matching the existing
        `producer_role != review_subagent` routing already documented in
        impl-review-loop/SKILL.md's Allowed Paths Gate Routing table.

        Not applicable when no PR review body is supplied at all (no data
        to evaluate -- distinct from a body that omits the fields).
        """
        gate = GateResult(
            gate_id="g6",
            gate_name="allowed_paths_gate_producer_role_presence",
            status=GateStatus.NOT_APPLICABLE.value
        )

        if not pr_body or not pr_body.strip():
            return gate

        missing: List[str] = []
        if not re.search(r"allowed_paths_gate\s*:", pr_body):
            missing.append("allowed_paths_gate block missing")
        if not re.search(r'["\']?producer_role["\']?\s*[:=]', pr_body):
            missing.append("producer_role field missing")

        if missing:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = (
                "G6: indeterminate (merge-blocking) -- " + "; ".join(missing)
            )
            return gate

        gate.status = GateStatus.PASS.value
        return gate

    def run_gate(self, gate_id: str, **kwargs) -> GateResult:
        """Run a specific gate by ID."""
        if gate_id == "g1":
            return self.g1_ci_test_selection(kwargs.get("artifact_path"))
        elif gate_id == "g2":
            return self.g2_evidence_binding(
                kwargs.get("pr_body", ""),
                kwargs.get("pr_head_sha", ""),
                kwargs.get("local_head_sha", "")
            )
        elif gate_id == "g3":
            return self.g3_implementation_oracle(
                kwargs.get("issue_body", ""),
                kwargs.get("target_files"),
                kwargs.get("method", "ast")
            )
        elif gate_id == "g4":
            return self.g4_head_sha_consistency(
                kwargs.get("pr_head_sha", ""),
                kwargs.get("local_head_sha", "")
            )
        elif gate_id == "g5":
            return self.g5_fixture_guard_path_coverage(
                kwargs.get("trace_log"),
                kwargs.get("coverage_file")
            )
        elif gate_id == "g6":
            return self.g6_allowed_paths_gate_producer_role_presence(
                kwargs.get("pr_body", "")
            )
        raise ValueError(f"Unknown gate: {gate_id}")

    def finalize_verdict(self):
        """Determine final verdict based on gate results."""
        has_fail = any(g.status == GateStatus.FAIL.value for g in self.result.gates)
        self.result.verdict = Verdict.REQUEST_CHANGES.value if has_fail else Verdict.APPROVE.value


def validate_against_schema(output_dict: Dict[str, Any], schema_path: Path) -> List[str]:
    """Validate output dict against JSON Schema (hand-rolled lightweight validator)."""
    errors = []

    # I-4-4D6 fail-closed: a missing / unreadable / malformed schema must never fall back to a
    # weaker built-in schema (the source fell back silently, so APPROVE could exit 0 unvalidated).
    try:
        with open(schema_path) as f:
            schema_doc = yaml.safe_load(f) if yaml else json.load(f)
    except Exception as exc:
        return [f"Schema unavailable (fail-closed): {schema_path}: {type(exc).__name__}"]

    if not isinstance(schema_doc, dict) or not isinstance(schema_doc.get("schema"), dict):
        return [f"Schema malformed (fail-closed): no top-level 'schema' mapping in {schema_path}"]

    schema = schema_doc["schema"]
    if not schema.get("required"):
        return [f"Schema malformed (fail-closed): empty 'required' in {schema_path}"]

    # Required top-level keys
    required_keys = schema.get("required", [])
    for key in required_keys:
        if key not in output_dict:
            errors.append(f"Missing required top-level key: {key}")

    # Validate verdict enum
    if "verdict" in output_dict:
        if output_dict["verdict"] not in ["APPROVE", "REQUEST_CHANGES"]:
            errors.append(f"Invalid verdict value: {output_dict['verdict']}")

    # Validate gates
    gates = output_dict.get("gates", [])
    if not isinstance(gates, list):
        errors.append("gates must be array")
        return errors

    if len(gates) < 5:
        errors.append(f"gates must have at least 5 items, got {len(gates)}")

    for gate in gates:
        if not isinstance(gate, dict):
            errors.append("Each gate must be object")
            continue

        gate_id = gate.get("gate_id")
        gate_status = gate.get("status")
        minimal_context = gate.get("minimal_context")

        # Validate required gate fields
        if gate_id not in ["g1", "g2", "g3", "g4", "g5", "g6"]:
            errors.append(f"Invalid gate_id: {gate_id}")
        if gate_status not in ["pass", "fail", "not_applicable"]:
            errors.append(f"Invalid gate status: {gate_status}")

        # Validate minimal_context rules
        if gate_status == "fail":
            if not minimal_context or not isinstance(minimal_context, str):
                errors.append(f"Gate {gate_id}: fail status requires non-empty minimal_context")
        else:
            if minimal_context is not None:
                errors.append(f"Gate {gate_id}: {gate_status} status must not have minimal_context")

        # Validate findings
        findings = gate.get("findings")
        if findings is not None:
            if not isinstance(findings, list):
                errors.append(f"Gate {gate_id}: findings must be array")
                continue
            for finding in findings:
                if not isinstance(finding, dict):
                    errors.append(f"Gate {gate_id}: each finding must be object")
                    continue
                for req_key in ["head_sha", "source_kind", "evidence_refs"]:
                    if req_key not in finding:
                        errors.append(f"Gate {gate_id}: finding missing {req_key}")
                # Validate evidence_refs
                evidence_refs = finding.get("evidence_refs", {})
                if isinstance(evidence_refs, dict):
                    if evidence_refs.get("self_report_only") is True:
                        has_support = any(k in evidence_refs for k in [(
                            "code_ref"
                        ), "pr_head_sha", "local_head_sha", "ci_run_ref"])
                        if not has_support:
                            errors.append(f"Gate {gate_id}: finding has self_report_only without supporting refs")

    return errors


def main():
    parser = argparse.ArgumentParser(
        description="PR Review Deterministic Gates Checker (G1-G6)"
    )
    parser.add_argument(
        "--rule", "-r",
        help="Gate to run: g1|g2|g3|g4|g5|g6 or all (default: all)",
        default="all"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Enable strict mode for --rule all (default ON for all, OFF for single rules)"
    )
    parser.add_argument(
        "--ci-artifact",
        help="Path to CI artifact (ci_test_selection/v1 JSON)"
    )
    parser.add_argument(
        "--pr-body",
        help="PR body text"
    )
    parser.add_argument(
        "--pr-head-sha",
        help="PR head SHA from GitHub"
    )
    parser.add_argument(
        "--local-head-sha",
        help="Local HEAD SHA from git"
    )
    parser.add_argument(
        "--issue-body",
        help="Issue body text for oracle verification"
    )
    parser.add_argument(
        "--target-files",
        nargs="+",
        help="Target files for oracle verification"
    )
    parser.add_argument(
        "--trace-guards",
        help="Trace log output from --trace-guards flag"
    )
    parser.add_argument(
        "--coverage-file",
        help="Coverage marker file with fixture_path_coverage/v1"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file (default: stdout)",
        default=None
    )
    parser.add_argument(
        "--format", "-f",
        choices=["json", "yaml"],
        default="json",
        help="Output format"
    )

    args = parser.parse_args()

    # Determine strict mode: ON for --rule all, OFF for single rules (unless explicitly set)
    is_all_rule = args.rule.lower() == "all"
    strict_mode = args.strict if args.strict else is_all_rule

    checker = CheckPRReviewGates(strict=strict_mode)
    gates_to_run = args.rule.split(",") if args.rule != "all" else ["g1", "g2", "g3", "g4", "g5", "g6"]

    for gate in gates_to_run:
        gate = gate.strip().lower()
        kwargs = {
            "artifact_path": args.ci_artifact,
            "pr_body": args.pr_body or "",
            "pr_head_sha": args.pr_head_sha or "",
            "local_head_sha": args.local_head_sha or "",
            "issue_body": args.issue_body or "",
            "target_files": args.target_files,
            "trace_log": args.trace_guards,
            "coverage_file": args.coverage_file,
        }
        result = checker.run_gate(gate, **kwargs)
        checker.result.gates.append(result)

    checker.finalize_verdict()

    # Serialize output
    output_dict = checker.result.to_dict()

    if args.format == "yaml":
        if yaml:
            output = yaml.dump(output_dict, default_flow_style=False)
        else:
            output = json.dumps(output_dict, indent=2)
    else:
        output = json.dumps(output_dict, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Result written to {args.output}")
    else:
        print(output)

    # Validate output against schema
    schema_path = Path(__file__).parent.parent / "references" / "pr-review-gate-result-schema.yml"
    validation_errors = validate_against_schema(output_dict, schema_path)
    if validation_errors:
        print("Schema validation errors:", file=sys.stderr)
        for err in validation_errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(2)

    # Exit code based on verdict
    sys.exit(0 if checker.result.verdict == Verdict.APPROVE.value else 1)


if __name__ == "__main__":
    main()
