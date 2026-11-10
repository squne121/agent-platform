"""D9: real publisher CLI in a minimal tree, fake executor only; no live writes."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = "66b39024eca3b67cea1d9fcd4f9f014fd8cff2e1"
SCRIPTS = ".claude/skills/issue-refinement-loop/scripts"
PUBLISHER = f"{SCRIPTS}/publish_termination_report.py"
POLICY = ".claude/skills/issue-refinement-loop/references/termination-policy.md"
SKILL = ".claude/skills/issue-refinement-loop/SKILL.md"
MANIFEST = "docs/dev/i4-4d9-handoff-claim-consistency-manifest.json"
D8 = "docs/dev/i4-4d8-hookless-reviewer-manifest.json"
MARKER = "<!-- LOOP_HANDOFF_RESULT_V1 -->"
HEX = "ab" * 32


def approved() -> dict:
    return {
        "status": "refinement_approved",
        "routing_action": "none",
        "contract_review": {"status": "go", "gate_result": "fresh_go"},
        "metadata": {"title_prefix_ready": True},
        "auto_fixes": {"result": "auto_fixed", "required": [], "skipped": []},
        "blockers": [],
    }


def repair(kind: str = "metadata_hygiene") -> dict:
    return {
        "kind": kind,
        "executor": "implementation-worker",
        "result": "applied",
        "evidence": {
            "before": "missing",
            "after": "repaired",
            "comment_url": "https://github.com/o/r/issues/1#issuecomment-2",
        },
    }


def body(value: dict) -> str:
    return (
        f"## 終了報告\n\n{MARKER}\n```yaml\n"
        + yaml.safe_dump({"LOOP_HANDOFF_RESULT_V1": value}, sort_keys=False)
        + "```\n"
    )


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Only the production publisher, bridge, policy and repo identity dependency.
    files = [
        PUBLISHER,
        f"{SCRIPTS}/isolation_issue_comment_bridge.py",
        "scripts/agent-guards/controlled_skill_mutation_policy.py",
        "scripts/agent-guards/repo_identity.py",
        "config/repository_identity.v1.json",
    ]
    for rel in files:
        destination = tmp_path / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, destination)
    executor = tmp_path / "scripts/agent-guards/controlled_skill_mutation_exec.py"
    executor.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        'payload = json.loads(Path(args[args.index("--input-file") + 1]).read_text())\n'
        'Path("executor.json").write_text(json.dumps({"argv": args, "payload": payload}))\n'
        'if "--dry-run" not in args: Path("mutation.txt").write_text("fake publish only")\n'
        'print(json.dumps({"status_detail": "dry_run_ok" if "--dry-run" in args else "created"}))\n',
        encoding="utf-8",
    )
    # No inherited real token/session routing, no installed gh accessible to children.
    env = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith(("GH_", "GITHUB_", "LOOP_", "CONTROLLED_", "CLAUDE_", "PUBLISH_")))
    }
    env.update(
        PATH="",
        PUBLISH_ARTIFACT_DIR=str(tmp_path / "diagnostics"),
    )

    env["CLAUDE_CODE_SESSION_ID"] = "test-local-only"

    def run(text: str, *args: str, stdin: bool = False):
        path = tmp_path / "body.md"
        path.write_text(text, encoding="utf-8")
        cmd = [
            sys.executable,
            str(tmp_path / PUBLISHER),
            "--issue-number",
            "1",
            "--repo",
            "squne121/agent-platform",
            *args,
        ]
        if not stdin:
            cmd += ["--body-file", str(path)]
        proc = subprocess.run(
            cmd,
            input=text if stdin else None,
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        return proc, tmp_path

    return run


def assert_rejected(cli, text: str, *args: str) -> None:
    proc, root = cli(text, *args)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "reason_code='handoff_" in proc.stderr
    assert not (root / "executor.json").exists()
    assert not (root / "mutation.txt").exists()
    assert not (root / "artifacts").exists()  # rejected even before request materialization
    assert "task_context refinement_approved signal" not in proc.stderr
    diagnostics = list((root / "diagnostics").glob("*.json"))
    assert len(diagnostics) == 1
    assert set(json.loads(diagnostics[0].read_text())) == {
        "timestamp",
        "issue_number",
        "reason_code",
    }


def test_ac1_issue101_contradiction_before_executor(cli):
    claim = approved()
    claim["metadata"]["title_prefix_ready"] = False
    fix = repair()
    fix["result"] = "skipped"
    claim["auto_fixes"]["required"] = [fix]
    assert_rejected(
        cli, body(claim), "--termination-reason", "approved", "--approved-body-sha256", HEX
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("routing_action", "ask_human"),
        ("routing_action", "blocked"),
        ("status", "impl_ready"),
        ("status", "unknown"),
        ("blockers", [{"kind": "unresolved"}]),
        ("blockers", None),
        ("metadata", {}),
        ("metadata", {"title_prefix_ready": "true"}),
        ("metadata", {"title_prefix_ready": False}),
        ("auto_fixes", {}),
        ("auto_fixes", {"required": [], "skipped": ["unresolved"]}),
        ("auto_fixes", {"required": None, "skipped": []}),
    ],
)
def test_ac1_claim_invariants(cli, field, value):
    claim = approved()
    claim[field] = value
    assert_rejected(cli, body(claim))


@pytest.mark.parametrize(
    "gate", ["missing_go", "stale_go", "invalidated_by_request_changes", "blocked", None]
)
def test_ac1_not_fresh(cli, gate):
    claim = approved()
    claim["contract_review"]["gate_result"] = gate
    assert_rejected(cli, body(claim))


@pytest.mark.parametrize("status", ["blocked", "human_judgment", None])
def test_ac1_review_not_go(cli, status):
    claim = approved()
    claim["contract_review"]["status"] = status
    assert_rejected(cli, body(claim))


@pytest.mark.parametrize(
    "defect", ["skipped", "failed", "missing", "before", "after", "comment_url", "empty", "null"]
)
def test_ac1_required_fix_evidence(cli, defect):
    claim = approved()
    fix = repair()
    if defect in ("skipped", "failed"):
        fix["result"] = defect
    elif defect == "missing":
        del fix["evidence"]
    elif defect in ("before", "after", "comment_url"):
        del fix["evidence"][defect]
    else:
        fix["evidence"]["after"] = {"empty": "  ", "null": None}[defect]
    claim["auto_fixes"]["required"] = [fix]
    assert_rejected(cli, body(claim))


@pytest.mark.parametrize(
    "status,route",
    [
        ("human_judgment_required", "none"),
        ("blocked", "none"),
        ("human_judgment_required", "blocked"),
        ("blocked", "ask_human"),
    ],
)
def test_ac1_nonapproval_route_mismatch(cli, status, route):
    claim = approved()
    claim.update(status=status, routing_action=route)
    assert_rejected(cli, body(claim))


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate_marker",
        "duplicate_status",
        "duplicate_nested",
        "duplicate_root",
        "alias",
        "merge",
        "unclosed",
        "missing_html",
        "missing_yaml",
        "malformed_yaml",
        "scalar",
        "multi_document",
        "wrong_root",
        "malformed_html",
        "unterminated_html",
        "second_yaml",
        "second_quoted_yaml",
    ],
)
def test_ac1_ambiguous_or_malformed_marker(cli, defect):
    text = body(approved())
    mutations = {
        "duplicate_marker": text + text,
        "duplicate_status": text.replace(
            "  status: refinement_approved", "  status: blocked\n  status: refinement_approved"
        ),
        "duplicate_nested": text.replace(
            "    title_prefix_ready: true",
            "    title_prefix_ready: false\n    title_prefix_ready: true",
        ),
        "duplicate_root": text.replace("```\n", "LOOP_HANDOFF_RESULT_V1: {}\n```\n"),
        "alias": text.replace("  blockers: []", "  blockers: &empty []\n  hidden: *empty"),
        "merge": text.replace(
            "  status: refinement_approved",
            "  <<: {status: blocked}\n  status: refinement_approved",
        ),
        "unclosed": text[:-4],
        "missing_html": text.replace(MARKER, ""),
        "missing_yaml": MARKER,
        "malformed_yaml": text.replace("status: refinement_approved", "status: ["),
        "scalar": f"{MARKER}\n```yaml\nLOOP_HANDOFF_RESULT_V1: approved\n```\n",
        "multi_document": text.replace("```\n", "---\nother: value\n```\n"),
        "wrong_root": text.replace("```\n", "other: value\n```\n"),
        "malformed_html": text.replace(MARKER, "<!-- LOOP_HANDOFF_RESULT_V1 extra -->"),
        "unterminated_html": "<!-- LOOP_HANDOFF_RESULT_V1 >",
        "second_yaml": text + "```yaml\nLOOP_HANDOFF_RESULT_V1: {}\n```\n",
        "second_quoted_yaml": text + '```yaml\n"LOOP_HANDOFF_RESULT_V1": {}\n```\n',
    }
    assert_rejected(cli, mutations[defect], "--dry-run")


@pytest.mark.parametrize(
    "kind",
    [
        "plain",
        "mention",
        "approved",
        "human",
        "blocked",
        "metadata_hygiene",
        "template_hygiene",
        "labels_failed",
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_ac2_legitimate_cli_paths(cli, kind, dry_run):
    claim = approved()
    if kind in ("metadata_hygiene", "template_hygiene"):
        claim["metadata"]["title_prefix_ready"] = False
        claim["auto_fixes"]["required"] = [repair(kind)]
    elif kind == "human":
        claim.update(status="human_judgment_required", routing_action="ask_human")
        claim["metadata"]["title_prefix_ready"] = False
        fix = repair()
        fix["result"] = "skipped"
        claim["auto_fixes"].update(required=[fix], skipped=[copy.deepcopy(fix)])
    elif kind == "blocked":
        claim.update(status="blocked", routing_action="blocked", blockers=[{"kind": "unresolved"}])
        claim["contract_review"].update(status="blocked", gate_result="stale_go")
    elif kind == "labels_failed":
        claim["label_sync_observation"] = {"kind": "presentation_sync", "result": "failed"}
    text = {
        "plain": "## 終了\nFinal Gate: not applicable\n",
        "mention": "LOOP_HANDOFF_RESULT_V1 is audit telemetry only.\n",
    }.get(kind, body(claim))
    proc, root = cli(text, *(["--dry-run"] if dry_run else []), stdin=kind == "plain")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads((root / "executor.json").read_text())
    assert receipt["payload"]["comment_body"].startswith(text)
    assert receipt["argv"][receipt["argv"].index("--command-id") + 1] == "issue_comment.publish"
    assert ("--dry-run" in receipt["argv"]) is dry_run
    assert (root / "mutation.txt").exists() is not dry_run
    assert "task_context refinement_approved signal" not in proc.stderr


@pytest.mark.parametrize(
    "sha,reason", [(HEX, "ADAPTER_UNAVAILABLE"), ("sha256:" + HEX, "INVALID_APPROVED_BODY_SHA256")]
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_ac2_sha_and_absent_adapter_compatibility(cli, sha, reason, dry_run):
    proc, root = cli(
        body(approved()),
        "--termination-reason",
        "approved",
        "--approved-body-sha256",
        sha,
        *(["--dry-run"] if dry_run else []),
    )
    assert proc.returncode == 0, proc.stderr
    assert (root / "executor.json").exists()
    assert not (root / "scripts/task-context").exists()
    assert "disposition='applied'" not in proc.stderr
    if dry_run:
        assert "signal disposition" not in proc.stderr
        assert not (root / "mutation.txt").exists()
        assert not (root / "diagnostics").exists()
    else:
        assert reason in proc.stderr
        assert (root / "mutation.txt").exists()
        diagnostic = json.loads(next((root / "diagnostics").glob("*.json")).read_text())
        assert diagnostic["reason_code"] == "task_context_signal_not_applied"
        assert diagnostic["signal_disposition"] == (
            "deferred" if sha == HEX else "rejected_evidence"
        )


def test_ac3_provenance_history_and_unconnected_boundaries():
    manifest = json.loads((ROOT / MANIFEST).read_text())
    d8 = json.loads((ROOT / D8).read_text())
    assert manifest["base_commit"] == BASE
    # Frozen pre-D9 pins, not git show: every existing CI lane also runs this
    # closure from a shallow checkout where the base object may be absent.
    assert manifest["base_file_sha256"] == {
        PUBLISHER: "eaac6340b59e03f50c2fdde5619274dfb816cb4c24ef51ecfa29ac46c7c2996f",
        ".claude/skills/issue-refinement-loop/tests/test_publish_termination_report.py": "be0d8e687a5bf02cb8171f0e3a02d57eaabfed2f0dfad2281575346bd1f48907",
        SKILL: "00488a3e6bbc2c3ef57f6b0f86126f222ea1fb9b63ea49e095bff334c98e29f5",
        POLICY: "7fda1b338da575fa694e3a782a9bfae053da06d633cb43ae357304c80ed0ca2f",
        D8: "0ff7eb8cf320770c5fb4c8da35675722c9dc9af8c0fd803f03422a02627d1ae2",
        "tests/stage_i4/test_i4_4d8_hookless_reviewer.py": "3b5827ba691161ebccbb06037837d4f597539445ad1cf4671c4f6ddc8107615f",
    }
    source_tests = ".claude/skills/issue-refinement-loop/tests/test_publish_termination_report.py"
    original_tests = (
        (ROOT / source_tests).read_bytes().split(b"\n\nclass TestHandoffClaimConsistency:")[0]
    )
    assert hashlib.sha256(original_tests).hexdigest() == manifest["base_file_sha256"][source_tests]
    historical_defects = json.dumps(
        d8["handoff_audit"]["defects"], sort_keys=True, separators=(",", ":")
    )
    assert hashlib.sha256(historical_defects.encode()).hexdigest() == (
        "3918720ec85aa675bf59da4c419e2330f91f22e4c5471a33b4a53e8a289ae365"
    )
    assert d8["handoff_audit"]["evidence"]["termination_report"].endswith("6766041114")
    assert d8["handoff_audit"]["current_d9"]["claim_check"] == "publisher_pre_executor"
    assert manifest["policy_reference"] == POLICY
    assert all(value is False for value in manifest["evidence_boundary"].values())
    assert not (ROOT / "scripts/task-context").exists()
    for path in (POLICY, SKILL):
        text = (ROOT / path).read_text()
        assert "既存6条件" in text and "controlled executor" in text
        assert "human_judgment_required / ask_human" in text


def test_ac3_ci_and_manifest_are_count_bound():
    manifest = json.loads((ROOT / MANIFEST).read_text())
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"][
        "i4-4d9-handoff-claim-consistency"
    ]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert f"run_stage1b1_tests.py {MANIFEST}" in commands
    assert f"0 deferred with reasons, {manifest['totals']['ran']} ran, 0 skipped" in commands
    assert "(skipped|xfailed|xpassed|deselected)" in commands
    assert "check_import_closure.py" in commands
    assert manifest["expected_collect"] == manifest["expected_ran"]
    assert manifest["deferred_tests"] == []
    assert all(
        manifest["totals"][key] == 0
        for key in ("deferred", "skipped", "xfailed", "xpassed", "deselected")
    )
    assert manifest["totals"]["ran"] == sum(manifest["expected_ran"].values())


@pytest.mark.parametrize("quoted", [False, True])
def test_ac1_fix_delta1_second_flow_claim_before_executor(cli, quoted):
    name = '"LOOP_HANDOFF_RESULT_V1"' if quoted else "LOOP_HANDOFF_RESULT_V1"
    second = (
        "{" + name + ": {status: refinement_approved, routing_action: none, "
        "metadata: {title_prefix_ready: false}, "
        "auto_fixes: {required: [{result: skipped}], skipped: []}}}"
    )
    assert_rejected(cli, body(approved()) + "\n```yaml\n" + second + "\n```\n")


@pytest.mark.parametrize("result", ["blocked", "human_judgment_required"])
def test_ac1_fix_delta1_aggregate_fix_result_before_executor(cli, result):
    claim = approved()
    claim["auto_fixes"]["result"] = result
    assert_rejected(cli, body(claim))


@pytest.mark.parametrize("title_ready", [False, True])
@pytest.mark.parametrize("missing", [("kind",), ("executor",), ("kind", "executor")])
def test_ac1_fix_delta1_required_entry_fields_before_executor(cli, title_ready, missing):
    claim = approved()
    claim["metadata"]["title_prefix_ready"] = title_ready
    fix = repair()
    for key in missing:
        del fix[key]
    claim["auto_fixes"]["required"] = [fix]
    assert_rejected(cli, body(claim))


@pytest.mark.parametrize("kind", ["metadata_hygiene", "known_marker_fix"])
@pytest.mark.parametrize("literal", ["LOOP_HANDOFF_RESULT_V1: broken old marker", MARKER])
def test_ac2_fix_delta1_literal_evidence_is_not_a_second_claim(cli, kind, literal):
    claim = approved()
    claim["metadata"]["title_prefix_ready"] = kind != "metadata_hygiene"
    claim["auto_fixes"]["required"] = [repair(kind)]
    text = body(claim).replace("before: missing", "before: |\n          " + literal)
    assert "before: |" in text
    proc, root = cli(text)
    assert proc.returncode == 0, proc.stderr
    sent = json.loads((root / "executor.json").read_text())["payload"]["comment_body"]
    assert sent.startswith(text)
    assert (root / "mutation.txt").is_file()


def test_ac2_fix_delta1_flow_claim_and_unrelated_flow_yaml_are_legal(cli):
    claim = yaml.safe_dump(
        {"LOOP_HANDOFF_RESULT_V1": approved()}, default_flow_style=True, width=1000
    )
    text = MARKER + "\n```yaml\n" + claim + "```\n\n```yaml\n{note: unrelated}\n```\n"
    proc, root = cli(text, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert (root / "executor.json").is_file()
    assert not (root / "mutation.txt").exists()


@pytest.mark.parametrize("framing", ["yml", "YAML", "", "raw"])
@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("layout", ["flow", "multiline_flow", "block", "sequence", "wrapped"])
def test_ac1_fix_delta2_claim_framing_before_executor(cli, framing, quoted, layout):
    name = '"LOOP_HANDOFF_RESULT_V1"' if quoted else "LOOP_HANDOFF_RESULT_V1"
    value = "{status: refinement_approved, metadata: {title_prefix_ready: false}, auto_fixes: {required: [{result: skipped}]}}"
    second = {
        "flow": "{" + name + ": " + value + "}",
        "multiline_flow": "{\n  " + name + ": " + value + "\n}",
        "block": name + ": " + value,
        "sequence": "- {" + name + ": " + value + "}",
        "wrapped": "audit: {" + name + ": " + value + "}",
    }[layout]
    extra = second if framing == "raw" else "```" + framing + "\n" + second + "\n```"
    assert_rejected(cli, body(approved()) + "\n" + extra + "\n")


@pytest.mark.parametrize("framing", ["yml", "YAML", "", "raw"])
@pytest.mark.parametrize(
    "content",
    [
        "{note: unrelated}",
        '{before: "LOOP_HANDOFF_RESULT_V1: broken old marker"}',
        "before: |\n  LOOP_HANDOFF_RESULT_V1 is an audit marker, not authority",
    ],
)
def test_ac2_fix_delta2_nonclaim_framing_still_publishes(cli, framing, content):
    extra = content if framing == "raw" else "```" + framing + "\n" + content + "\n```"
    text = body(approved()) + "\n" + extra + "\n"
    proc, root = cli(text)
    assert proc.returncode == 0, proc.stderr
    assert (root / "mutation.txt").is_file()
    assert json.loads((root / "executor.json").read_text())["payload"]["comment_body"].startswith(
        text
    )


@pytest.mark.parametrize("field", ["before", "after"])
@pytest.mark.parametrize(
    "literal",
    [
        "{LOOP_HANDOFF_RESULT_V1: {metadata: {title_prefix_ready: false}}}",
        '{"LOOP_HANDOFF_RESULT_V1": {auto_fixes: {required: [{result: skipped}]}}}',
    ],
)
def test_ac2_fix_delta2_scalar_evidence_still_publishes(cli, field, literal):
    claim = approved()
    claim["auto_fixes"]["required"] = [repair()]
    original = "missing" if field == "before" else "repaired"
    text = body(claim).replace(field + ": " + original, field + ": |\n          " + literal)
    assert field + ": |" in text
    proc, root = cli(text)
    assert proc.returncode == 0, proc.stderr
    assert (root / "mutation.txt").is_file()
    assert json.loads((root / "executor.json").read_text())["payload"]["comment_body"].startswith(
        text
    )


@pytest.fixture
def pub():
    """Load the production module for the two tests moved from the source lane."""
    spec = importlib.util.spec_from_file_location("_publisher_d9_moved_tests", ROOT / PUBLISHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestHandoffClaimConsistency:
    """D9 direct-call cases moved intact so Stage2-2 retains its 18 source tests."""

    def test_malformed_marker_never_reaches_transport_or_signal(self, tmp_path, monkeypatch, pub):
        monkeypatch.setattr(pub, "ARTIFACT_DIR", tmp_path)
        with (
            patch.object(pub, "_post_github_comment") as post,
            patch.object(pub, "_emit_refinement_approved_signal") as signal,
        ):
            result = pub.publish(
                issue_number=42,
                body="<!-- LOOP_HANDOFF_RESULT_V1 -->",
                repo="squne121/agent-platform",
                termination_reason="approved",
                dry_run=True,
            )
        assert result == 1
        post.assert_not_called()
        signal.assert_not_called()
        artifact = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert artifact["reason_code"] == "handoff_marker_malformed"

    def test_marker_free_markdown_is_not_a_new_approval_gate(self, pub):
        with patch.object(pub, "_post_github_comment", return_value=0) as post:
            result = pub.publish(issue_number=42, body="## 終了\n", repo="squne121/agent-platform")
        assert result == 0
        post.assert_called_once()
