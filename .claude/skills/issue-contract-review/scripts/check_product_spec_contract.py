#!/usr/bin/env python3
"""
Product Spec Contract Checker

Issue contract を product spec 観点から検証する。以下の 6 ルールで判定:

  PS001: docs/product/** 更新時に spec evidence / spec delta context があるか
  PS002: tasks.md が direct implementation source になっていないか
  PS003: .specify/ が canonical source になっていないか
  PS004: product spec 更新に diff_rationale / requirement trace / affected_sections があるか
  PS005: generated task が requirement_id / source_task_id を保持しているか
  PS006: generated task dependency が materialize されているか

出力: JSON with product_spec_check schema
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

_CREATE_ISSUE_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent / "create-issue" / "scripts"
)
if str(_CREATE_ISSUE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS))

_IMPL_REVIEW_LOOP_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent / "impl-review-loop" / "scripts"
)
if str(_IMPL_REVIEW_LOOP_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_IMPL_REVIEW_LOOP_SCRIPTS))

from path_classification import extract_allowed_paths as pc_extract_allowed_paths  # noqa: E402
from path_classification import has_code_or_runtime_scope  # noqa: E402
from evaluate_product_spec_gate import evaluate_product_spec_payload  # noqa: E402
from mrc_contract_parser import parse_machine_readable_contract  # noqa: E402


SPEC_CHANGE_KINDS = {
    "spec-delta",
    "spec_update",
    "spec-update",
    "product-spec",
    "product_spec",
    "product-spec-delta",
    "product_spec_delta",
    "product-spec-update",
    "product_spec_update",
}
PLACEHOLDER_RE = re.compile(
    r"^(?:<!--.*?-->\s*|TODO\b|TBD\b|N/?A\b|NONE\b|<required:[^>]+>|<todo>|<tbd>)*$",
    re.IGNORECASE | re.DOTALL,
)
ANY_H2_RE = re.compile(r"^[ ]{0,3}##[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)$")


class _DuplicateKeyError(Exception):
    def __init__(self, key: object):
        self.key = key
        super().__init__(f"duplicate mapping key: {key!r}")


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys."""


def _strict_construct_mapping(loader: _StrictSafeLoader, node, deep: bool = False):
    loader.flatten_mapping(node)
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKeyError(key)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _strict_construct_mapping,
)


@dataclass(frozen=True)
class SectionParseResult:
    present: bool
    ok: bool
    data: Optional[dict]
    reason: str = ""
    duplicate_key: Optional[str] = None


def _match_fence(line: str) -> Optional[Tuple[str, int, str]]:
    match = _FENCE_RE.match(line)
    if not match:
        return None
    fence = match.group("fence")
    return fence[0], len(fence), match.group("info").strip()


def _sha256_prefixed(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _repo_identity():
    """Load scripts/agent-guards/repo_identity.py by absolute path (no sys.path pollution)."""
    import importlib.util

    path = Path(__file__).resolve().parents[4] / "scripts" / "agent-guards" / "repo_identity.py"
    spec = importlib.util.spec_from_file_location("repo_identity_for_product_spec_check", path)
    if spec is None or spec.loader is None:
        raise ImportError("repo_identity_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_gh_api(issue_number: int, repo: str) -> Optional[Dict[str, Any]]:
    """GitHub API から Issue 情報を取得."""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "title,body,labels",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


def _extract_sections(body: str, section_name: str) -> List[str]:
    sections: List[str] = []
    lines = body.splitlines()
    active_fence: Optional[Tuple[str, int]] = None
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        fence = _match_fence(line)
        if fence:
            fence_char, fence_len, _ = fence
            if active_fence is None:
                active_fence = (fence_char, fence_len)
            elif active_fence[0] == fence_char and fence_len >= active_fence[1]:
                active_fence = None
            idx += 1
            continue
        match = ANY_H2_RE.match(line)
        if active_fence is None and match and match.group("text").strip() == section_name:
            idx += 1
            buf: List[str] = []
            inner_fence = active_fence
            while idx < len(lines):
                current = lines[idx]
                current_fence = _match_fence(current)
                if current_fence:
                    fence_char, fence_len, _ = current_fence
                    if inner_fence is None:
                        inner_fence = (fence_char, fence_len)
                    elif inner_fence[0] == fence_char and fence_len >= inner_fence[1]:
                        inner_fence = None
                    buf.append(current)
                    idx += 1
                    continue
                if inner_fence is None and ANY_H2_RE.match(current):
                    break
                buf.append(current)
                idx += 1
            sections.append("\n".join(buf).strip())
            continue
        idx += 1
    return sections


def _extract_yaml_fences(section_text: str) -> List[str]:
    fences: List[str] = []
    lines = section_text.splitlines()
    capture: Optional[List[str]] = None
    active_fence: Optional[Tuple[str, int]] = None
    for line in lines:
        fence = _match_fence(line)
        if fence:
            fence_char, fence_len, info = fence
            if active_fence is None:
                active_fence = (fence_char, fence_len)
                if info.lower() in {"yaml", "yml"}:
                    capture = []
                else:
                    capture = None
            elif active_fence[0] == fence_char and fence_len >= active_fence[1]:
                if capture is not None:
                    fences.append("\n".join(capture))
                active_fence = None
                capture = None
            elif capture is not None:
                capture.append(line)
            continue
        if capture is not None:
            capture.append(line)
    return fences


def _parse_yaml_section(body: str, section_name: str) -> SectionParseResult:
    sections = _extract_sections(body, section_name)
    if not sections:
        return SectionParseResult(present=False, ok=False, data=None, reason="section_missing")
    if len(sections) > 1:
        return SectionParseResult(present=True, ok=False, data=None, reason="section_multiple")

    fences = _extract_yaml_fences(sections[0])
    if not fences:
        return SectionParseResult(present=True, ok=False, data=None, reason="yaml_fence_missing")
    if len(fences) > 1:
        return SectionParseResult(present=True, ok=False, data=None, reason="yaml_fence_multiple")

    try:
        data = yaml.load(fences[0], Loader=_StrictSafeLoader)
    except _DuplicateKeyError as exc:
        return SectionParseResult(
            present=True,
            ok=False,
            data=None,
            reason="duplicate_key",
            duplicate_key=str(exc.key),
        )
    except yaml.YAMLError:
        return SectionParseResult(present=True, ok=False, data=None, reason="yaml_syntax_error")

    if not isinstance(data, dict):
        return SectionParseResult(present=True, ok=False, data=None, reason="root_not_mapping")

    return SectionParseResult(present=True, ok=True, data=data)


def _is_placeholder_text(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return True
    return bool(PLACEHOLDER_RE.fullmatch(normalized))


def _is_meaningful_scalar(value: Any) -> bool:
    return isinstance(value, str) and not _is_placeholder_text(value)


def _normalize_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    normalized: List[str] = []
    for item in value:
        if isinstance(item, str) and not _is_placeholder_text(item):
            normalized.append(item.strip())
    return normalized


def _change_kind(mrc: SectionParseResult) -> str:
    if not mrc.ok or not mrc.data:
        return ""
    value = mrc.data.get("change_kind")
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _product_spec_parse_error(parse_result: SectionParseResult) -> Optional[str]:
    if not parse_result.present:
        return None
    if parse_result.ok:
        return None
    if parse_result.reason == "duplicate_key" and parse_result.duplicate_key:
        return f"Product Spec Context duplicate key: {parse_result.duplicate_key}"
    return f"Product Spec Context parse error: {parse_result.reason}"


def _has_spec_reference(context: Dict[str, Any]) -> bool:
    ref_fields = (
        "product_spec_id",
        "spec_ssot",
        "changed_requirement_id",
        "requirement_id",
        "source_task_id",
    )
    for field_name in ref_fields:
        if _is_meaningful_scalar(context.get(field_name)):
            return True
    list_fields = ("changed_requirement_ids", "requirement_ids")
    return any(_normalize_string_list(context.get(field_name)) for field_name in list_fields)


def _has_required_diff_evidence(context: Dict[str, Any]) -> Tuple[bool, List[str]]:
    missing: List[str] = []
    if not _is_meaningful_scalar(context.get("diff_rationale")):
        missing.append("diff_rationale")

    requirement_fields = (
        "changed_requirement_id",
        "requirement_id",
    )
    has_requirement_ref = any(
        _is_meaningful_scalar(context.get(field_name))
        for field_name in requirement_fields
    ) or any(
        _normalize_string_list(context.get(field_name))
        for field_name in ("changed_requirement_ids", "requirement_ids")
    )
    if not has_requirement_ref:
        missing.append("changed_requirement_id/requirement_id")

    if not _normalize_string_list(context.get("affected_sections")):
        missing.append("affected_sections")
    return not missing, missing


def extract_depends_on(issue_body: str) -> List[int]:
    """line-anchored `Depends on #N` を抽出（制御セクション）."""
    depends_on = []
    match = re.search(
        r"^##\s+Depends On\s*$(.+?)(?=^##|\Z)", issue_body, re.MULTILINE | re.DOTALL
    )
    if match:
        section = match.group(1)
        for line in section.split("\n"):
            line = line.strip()
            found = re.match(r"^-?\s*Depends on #(\d+)", line)
            if found:
                depends_on.append(int(found.group(1)))
    return depends_on


def check_trigger_conditions(
    issue_body: str,
    allowed_paths: List[str],
    product_spec_context: SectionParseResult,
) -> Dict[str, bool]:
    """trigger conditions を判定."""
    return {
        "docs_product_allowed_paths": any(
            path.lstrip("./").startswith("docs/product/") for path in allowed_paths
        ),
        "tasks_md_mentioned": bool(
            re.search(r"\btasks\.md\b|tasks\.md", issue_body, re.IGNORECASE)
        ),
        "specify_artifact_mentioned": bool(
            re.search(r"\.specify|specify/", issue_body, re.IGNORECASE)
        ),
        "generated_task_mentioned": bool(
            re.search(
                r"generated[_-]?task|task_materialization|from[_-]?tasks\.md|generated from",
                issue_body,
                re.IGNORECASE,
            )
        ),
        "product_spec_context_present": product_spec_context.present,
    }


def check_ps001(
    allowed_paths: List[str],
    triggers: Dict[str, bool],
    mrc: SectionParseResult,
    product_spec_context: SectionParseResult,
) -> Tuple[str, List[Dict[str, str]]]:
    """
    PS001: docs/product/** 更新が spec evidence / spec delta context を含んでいるか.
    """
    if not triggers["docs_product_allowed_paths"]:
        return "pass", []

    if _change_kind(mrc) in SPEC_CHANGE_KINDS:
        return "pass", [
            {
                "rule_id": "PS001",
                "source": "machine_readable_contract",
                "excerpt": "Issue 自体が product spec delta/update Issue として明示",
            }
        ]

    parse_error = _product_spec_parse_error(product_spec_context)
    if parse_error:
        return "fail", [
            {
                "rule_id": "PS001",
                "source": "product_spec_context",
                "excerpt": parse_error,
            }
        ]

    context = product_spec_context.data or {}
    if _has_spec_reference(context):
        return "pass", [
            {
                "rule_id": "PS001",
                "source": "product_spec_context",
                "excerpt": "Product Spec Context に検証済み spec reference がある",
            }
        ]

    scope_kind = "mixed_scope" if has_code_or_runtime_scope(allowed_paths) else "docs_only"
    return "fail", [
        {
            "rule_id": "PS001",
            "source": "allowed_paths",
            "excerpt": f"{scope_kind}: docs/product/** 更新ありだが spec evidence / spec delta context が不足",
        }
    ]


def check_ps002(issue_body: str, triggers: Dict[str, bool]) -> Tuple[str, List[Dict[str, str]]]:
    """PS002: tasks.md が direct implementation source になっていないか."""
    if not triggers["tasks_md_mentioned"]:
        return "pass", []

    allow = [
        r"tasks\.md.*staging",
        r"staging.*tasks\.md",
        r"tasks\.md.*artifact",
        r"artifact.*tasks\.md",
        r"convert.*tasks\.md.*issue|materialize.*tasks\.md",
    ]
    if any(re.search(pattern, issue_body, re.IGNORECASE) for pattern in allow):
        return "pass", [
            {
                "rule_id": "PS002",
                "source": "issue_body",
                "excerpt": "tasks.md を staging artifact として参照",
            }
        ]

    prohibit = [
        r"tasks\.md.*(?:direct|implementation|source of truth|ssot|canonical)",
        r"(?:use|implement).*tasks\.md.*(?:directly|as source)",
    ]
    for pattern in prohibit:
        found = re.search(pattern, issue_body, re.IGNORECASE | re.DOTALL)
        if found:
            return "fail", [
                {
                    "rule_id": "PS002",
                    "source": "issue_body",
                    "excerpt": found.group(0)[:120],
                }
            ]

    return "pass", []


def check_ps003(issue_body: str, triggers: Dict[str, bool]) -> Tuple[str, List[Dict[str, str]]]:
    """PS003: .specify/ が canonical source になっていないか."""
    if not triggers["specify_artifact_mentioned"]:
        return "pass", []

    prohibit = [
        r"\.specify.*?\bcanonical\b(?!.*not)(?!.*derived)",
        r"\.specify.*?\bssot\b(?!.*docs)(?!.*wins)",
        r"\.specify.*?(?:override.*?docs|takes priority.*docs|comes.*?before.*docs)",
    ]
    for pattern in prohibit:
        found = re.search(pattern, issue_body, re.IGNORECASE | re.DOTALL)
        if found:
            return "fail", [
                {
                    "rule_id": "PS003",
                    "source": "issue_body",
                    "excerpt": found.group(0)[:120],
                }
            ]

    allow = [
        r"\.specify.*workbench|workbench.*\.specify",
        r"\.specify.*derived|derived.*\.specify",
        r"\.specify.*artifact",
        r"docs.*wins|docs.*priority|docs.*ssot.*wins|docs.*SSOT",
        r"\.specify.*not canonical",
    ]
    if any(re.search(pattern, issue_body, re.IGNORECASE) for pattern in allow):
        return "pass", [
            {
                "rule_id": "PS003",
                "source": "issue_body",
                "excerpt": ".specify/ を derived workbench として明記",
            }
        ]

    return "pass", []


def check_ps004(
    triggers: Dict[str, bool],
    mrc: SectionParseResult,
    product_spec_context: SectionParseResult,
) -> Tuple[str, List[Dict[str, str]]]:
    """PS004: product spec 更新に diff evidence があるか."""
    if not triggers["docs_product_allowed_paths"]:
        return "pass", []

    mrc_data = mrc.data or {}
    if _change_kind(mrc) in SPEC_CHANGE_KINDS and _is_meaningful_scalar(
        mrc_data.get("diff_rationale")
    ):
        return "pass", [
            {
                "rule_id": "PS004",
                "source": "machine_readable_contract",
                "excerpt": "spec delta issue with non-placeholder diff_rationale",
            }
        ]

    parse_error = _product_spec_parse_error(product_spec_context)
    if parse_error:
        return "fail", [
            {
                "rule_id": "PS004",
                "source": "product_spec_context",
                "excerpt": parse_error,
            }
        ]

    context = product_spec_context.data or {}
    ok, missing = _has_required_diff_evidence(context)
    if ok:
        return "pass", [
            {
                "rule_id": "PS004",
                "source": "product_spec_context",
                "excerpt": "diff evidence fields are present and non-placeholder",
            }
        ]

    return "fail", [
        {
            "rule_id": "PS004",
            "source": "product_spec_context",
            "excerpt": f"missing diff evidence: {', '.join(missing)}",
        }
    ]


def check_ps005(
    triggers: Dict[str, bool],
    mrc: SectionParseResult,
    product_spec_context: SectionParseResult,
) -> Tuple[str, List[Dict[str, str]]]:
    """PS005: generated task が requirement_id / source_task_id を保持しているか."""
    if not triggers["generated_task_mentioned"]:
        return "pass", []

    mrc_data = mrc.data or {}
    context = product_spec_context.data or {}
    has_requirement_id = any(
        _is_meaningful_scalar(source.get("requirement_id"))
        for source in (mrc_data, context)
    ) or bool(_normalize_string_list(context.get("requirement_ids")))
    has_source_task_id = any(
        _is_meaningful_scalar(source.get("source_task_id"))
        for source in (mrc_data, context)
    )

    if has_requirement_id and has_source_task_id:
        return "pass", [
            {
                "rule_id": "PS005",
                "source": "issue_body",
                "excerpt": "requirement_id と source_task_id 両方存在",
            }
        ]

    missing = []
    if not has_requirement_id:
        missing.append("requirement_id")
    if not has_source_task_id:
        missing.append("source_task_id")
    return "fail", [
        {
            "rule_id": "PS005",
            "source": "issue_body",
            "excerpt": f"generated task tracing: {', '.join(missing)} 欠落",
        }
    ]


def check_ps006(issue_body: str, triggers: Dict[str, bool]) -> Tuple[str, List[Dict[str, str]]]:
    """PS006: generated task dependency が materialize されているか."""
    if not triggers["generated_task_mentioned"]:
        return "pass", []

    has_dependency_mention = bool(
        re.search(
            r"depend|depend on|prerequisite|requires|prior.*task|blocked.*by|blocks",
            issue_body,
            re.IGNORECASE,
        )
    )
    if not has_dependency_mention:
        return "pass", []

    depends_on = extract_depends_on(issue_body)
    if depends_on:
        return "pass", [
            {
                "rule_id": "PS006",
                "source": "dependencies",
                "excerpt": "Depends on #N 宣言あり（line-anchored）",
            }
        ]

    return "human_judgment", [
        {
            "rule_id": "PS006",
            "source": "issue_body",
            "excerpt": "generated task が dependencies を言及するもそれが materialize されていない（確認必要）",
        }
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Product Spec Contract Checker")
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--body-file", type=str, help="Read issue body from file (for testing)")
    args = parser.parse_args()

    source_type = "body_file" if args.body_file else "github_api"
    if args.body_file:
        issue_body = Path(args.body_file).read_text(encoding="utf-8")
    else:
        # Bind the target repository to the single identity source BEFORE any external probe.
        try:
            repo_trusted = _repo_identity().is_trusted_repo(args.repo)
        except Exception:  # config missing / invalid -> fail closed (RepositoryIdentityError)
            repo_trusted = False
        if not repo_trusted:
            print(
                json.dumps(
                    {
                        "schema": "product_spec_check/v1",
                        "applicability": "applicable",
                        "decision": "human_judgment",
                        "blocked_reasons": [],
                        "body_sha256": _sha256_prefixed(""),
                        "source_provenance": {"source_type": source_type},
                        "error": "repository_not_trusted",
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        issue_data = run_gh_api(args.issue_number, args.repo)
        if not issue_data:
            print(
                json.dumps(
                    {
                        "schema": "product_spec_check/v1",
                        "applicability": "applicable",
                        "decision": "human_judgment",
                        "blocked_reasons": [],
                        "body_sha256": _sha256_prefixed(""),
                        "source_provenance": {"source_type": source_type},
                        "error": "Failed to fetch issue",
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        issue_body = issue_data.get("body", "")

    body_sha256 = _sha256_prefixed(issue_body)
    allowed_paths = pc_extract_allowed_paths(issue_body)
    mrc_parsed = parse_machine_readable_contract(issue_body)
    mrc = SectionParseResult(
        present=bool(_extract_sections(issue_body, "Machine-Readable Contract")),
        ok=mrc_parsed.ok,
        data=mrc_parsed.data,
        reason=mrc_parsed.reason,
        duplicate_key=mrc_parsed.duplicate_key,
    )
    product_spec_context = _parse_yaml_section(issue_body, "Product Spec Context")
    triggers = check_trigger_conditions(issue_body, allowed_paths, product_spec_context)
    triggers["machine_readable_contract_present"] = mrc.present
    applicability = "applicable" if any(triggers.values()) else "not_applicable"

    checks = {
        "docs_product_requires_spec_evidence": check_ps001(
            allowed_paths, triggers, mrc, product_spec_context
        ),
        "tasks_md_not_direct_source": check_ps002(issue_body, triggers),
        "specify_not_canonical": check_ps003(issue_body, triggers),
        "diff_first_rationale_present": check_ps004(
            triggers, mrc, product_spec_context
        ),
        "generated_task_trace_present": check_ps005(
            triggers, mrc, product_spec_context
        ),
        "task_dependencies_materialized": check_ps006(issue_body, triggers),
    }

    conditions = {}
    blocked_reasons: List[Dict[str, str]] = []
    has_fail = False
    has_human_judgment = False

    parse_errors: List[Dict[str, str]] = []
    if mrc.present and not mrc.ok:
        parse_errors.append(
            {
                "rule_id": "PS001",
                "source": "machine_readable_contract",
                "excerpt": f"Machine-Readable Contract parse error: {mrc.reason}",
            }
        )
    psc_parse_error = _product_spec_parse_error(product_spec_context)
    if psc_parse_error:
        parse_errors.append(
            {
                "rule_id": "PS001",
                "source": "product_spec_context",
                "excerpt": psc_parse_error,
            }
        )
    if parse_errors:
        conditions["named_yaml_sections_valid"] = {
            "status": "fail",
            "evidence": parse_errors,
        }
        has_fail = True
        blocked_reasons.extend(parse_errors)

    for check_name, (status, evidence) in checks.items():
        conditions[check_name] = {"status": status, "evidence": evidence}
        if status == "fail":
            has_fail = True
            blocked_reasons.extend(evidence)
        elif status == "human_judgment":
            has_human_judgment = True
            blocked_reasons.extend(evidence)

    if applicability == "not_applicable":
        decision = "pass"
        blocked_reasons = []
    elif has_fail:
        decision = "fail"
    elif has_human_judgment:
        decision = "human_judgment"
    else:
        decision = "pass"

    result = {
        "schema": "product_spec_check/v1",
        "applicability": applicability,
        "decision": decision,
        "triggers": triggers,
        "conditions": conditions,
        "blocked_reasons": blocked_reasons,
        "body_sha256": body_sha256,
        "source_provenance": {
            "source_type": source_type,
            "body_file": args.body_file if args.body_file else None,
        },
    }

    gate_result = evaluate_product_spec_payload(
        result,
        issue_url=f"https://github.com/{args.repo}/issues/{args.issue_number}",
        body_sha256=body_sha256,
        exit_code=0 if result["decision"] == "pass" else 1,
    )
    if gate_result["routing_action"] == "refresh_contract_snapshot":
        result["applicability"] = "applicable"
        result["decision"] = "human_judgment"
        result["blocked_reasons"] = result["blocked_reasons"] or [
            {
                "rule_id": "PS001",
                "source": "product_spec_check",
                "excerpt": gate_result["reason"],
            }
        ]

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["decision"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
