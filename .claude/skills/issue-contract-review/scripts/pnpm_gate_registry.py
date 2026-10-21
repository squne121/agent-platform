"""Single fail-closed authority for approved pnpm package-script gates."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any, Iterable


EVIDENCE_SCHEMA = "pnpm_gate_evidence/v1"
PACKAGE_MANAGER = "pnpm@11.7.0"
RUNNER_ENV_DELTA = {"CI": "true"}


@dataclass(frozen=True)
class GateDescriptor:
    gate_id: str
    request_argv: tuple[str, str]
    script_name: str
    closure: tuple[str, ...]


_SCRIPTS = {
    "typecheck": "tsc --noEmit",
    "lint": "eslint .",
    "test": (
        "vitest run --exclude 'tests/e2e/**' --exclude 'tests/component/**' "
        "--exclude '.claude/worktrees/**' --exclude '.claude/tmp/**' --exclude 'tmp/**'"
    ),
    "build": "tsc && vite build",
    "typecheck:e2e": "tsc -p tests/e2e/tsconfig.json --noEmit",
    "lint:docs": "pnpm run lint:md && pnpm run lint:prose && pnpm run validate:roadmap-refs",
    "lint:md": "markdownlint-cli2",
    "lint:prose": (
        "textlint --config .textlintrc 'docs/**/*.md' 'README.md' '.github/**/*.md' "
        "'CLAUDE.md' 'AGENTS.md' 'SECURITY.md' '.claude/**/*.md'"
    ),
    "validate:roadmap-refs": "node scripts/validate-roadmap-refs.mjs",
    # Argument-free wrapper invocations (Issue #1709 PR review P0-5 / AC5,
    # AC7). The package.json script strings themselves are bare, no-argv
    # invocations -- the Issue #1709 Verification Commands match these
    # literal strings verbatim, and this fail-closed, exact-two-token gate
    # registry never forwards extra argv to the underlying CLI. Both
    # `generate-retro-live-verification.mjs` and `check-retro-live-
    # verification.mjs` fall back to their own checked-in, fixture-derived
    # default arguments whenever they are invoked with zero CLI flags (see
    # `DEFAULT_GENERATE_ARGS` / `DEFAULT_CHECK_ARGS` in those scripts), so a
    # bare `pnpm retro-live-verification:generate` / `:verify` performs a
    # real, read-only fixture validation instead of failing with a
    # required-option usage error.
    # `retro-live-verification:post` is a mutation command (it can create/
    # update a live GitHub comment) and is deliberately NOT registered here;
    # it must only ever be invoked by the protected
    # `.github/workflows/retro-live-verification.yml` `post-canonical-
    # comment` job, never through this generic agent-facing gate registry.
    "retro-live-verification:generate": "node scripts/generate-retro-live-verification.mjs",
    "retro-live-verification:verify": "node scripts/check-retro-live-verification.mjs",
}

_GATES = (
    GateDescriptor("pnpm.typecheck.v1", ("pnpm", "typecheck"), "typecheck", ("typecheck",)),
    GateDescriptor("pnpm.lint.v1", ("pnpm", "lint"), "lint", ("lint",)),
    GateDescriptor("pnpm.test.v1", ("pnpm", "test"), "test", ("test",)),
    GateDescriptor("pnpm.build.v1", ("pnpm", "build"), "build", ("build",)),
    GateDescriptor(
        "pnpm.typecheck-e2e.v1",
        ("pnpm", "typecheck:e2e"),
        "typecheck:e2e",
        ("typecheck:e2e",),
    ),
    GateDescriptor(
        "pnpm.lint-docs.v1",
        ("pnpm", "lint:docs"),
        "lint:docs",
        ("lint:docs", "lint:md", "lint:prose", "validate:roadmap-refs"),
    ),
    GateDescriptor(
        "pnpm.retro-live-verification-generate.v1",
        ("pnpm", "retro-live-verification:generate"),
        "retro-live-verification:generate",
        ("retro-live-verification:generate",),
    ),
    GateDescriptor(
        "pnpm.retro-live-verification-verify.v1",
        ("pnpm", "retro-live-verification:verify"),
        "retro-live-verification:verify",
        ("retro-live-verification:verify",),
    ),
)


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def iter_gate_descriptors() -> Iterable[GateDescriptor]:
    return _GATES


def expected_scripts() -> dict[str, str]:
    return dict(_SCRIPTS)


def gate_for_request(argv: list[str]) -> GateDescriptor | None:
    """Accept only case-sensitive, two-token requests from the shared registry."""
    frozen = tuple(argv)
    return next((gate for gate in _GATES if gate.request_argv == frozen), None)


def looks_like_pnpm(argv: list[str]) -> bool:
    return bool(argv) and Path(argv[0]).name == "pnpm"


def _integrity_payload(gate: GateDescriptor) -> dict[str, Any]:
    closure = {name: _SCRIPTS[name] for name in gate.closure}
    return {
        "package_manager": PACKAGE_MANAGER,
        "gate_id": gate.gate_id,
        "script_name": gate.script_name,
        "closure": closure,
        "validator_digest": _digest({"validate:roadmap-refs": _SCRIPTS["validate:roadmap-refs"]}),
        "hook_policy": "no_pre_or_post_hooks",
    }


def validate_manifest(gate: GateDescriptor, cwd: str) -> tuple[dict[str, Any] | None, str | None]:
    manifest_path = Path(cwd).resolve() / "package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"manifest_integrity:manifest_unreadable:{exc}"
    if not isinstance(manifest, dict) or manifest.get("packageManager") != PACKAGE_MANAGER:
        return None, "manifest_integrity:package_manager_mismatch"
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return None, "manifest_integrity:scripts_missing"
    expected = _integrity_payload(gate)
    for name, body in expected["closure"].items():
        if scripts.get(name) != body:
            return None, f"manifest_integrity:closure_drift:{name}"
        if f"pre{name}" in scripts or f"post{name}" in scripts:
            return None, f"manifest_integrity:lifecycle_hook_present:{name}"
    observed = {
        "package_manager": manifest.get("packageManager"),
        "gate_id": gate.gate_id,
        "script_name": gate.script_name,
        "closure": {name: scripts.get(name) for name in gate.closure},
        "validator_digest": _digest({"validate:roadmap-refs": scripts.get("validate:roadmap-refs")}),
        "hook_policy": "no_pre_or_post_hooks",
    }
    if _digest(observed) != _digest(expected):
        return None, "manifest_integrity:digest_mismatch"
    return {
        "manifest_path": str(manifest_path),
        "manifest_integrity_digest": _digest(expected),
        "validator_digest": expected["validator_digest"],
    }, None


def resolve_trusted_pnpm(repo_root: str) -> str | None:
    candidate = shutil.which("pnpm")
    if not candidate:
        return None
    resolved = Path(candidate).resolve()
    root = Path(repo_root).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    try:
        if resolved.is_relative_to(root) or resolved.is_relative_to(temporary):
            return None
    except ValueError:
        return None
    try:
        mode = resolved.stat().st_mode
    except OSError:
        return None
    if (
        not stat.S_ISREG(mode)
        or not os.access(resolved, os.X_OK)
        or mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        return None
    return str(resolved)


def prepare_launch(argv: list[str], cwd: str) -> tuple[list[str] | None, dict[str, Any] | None, str | None]:
    gate = gate_for_request(argv)
    if gate is None:
        return None, None, "pnpm_gate:noncanonical_request_argv"
    manifest, error = validate_manifest(gate, cwd)
    if error:
        return None, None, error
    trusted = resolve_trusted_pnpm(cwd)
    if not trusted:
        return None, None, "pnpm_gate:trusted_runner_unavailable"
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "gate_id": gate.gate_id,
        "exact_request_argv": list(gate.request_argv),
        "launch_argv": [trusted, "run", gate.script_name],
        "runner_env_delta": dict(RUNNER_ENV_DELTA),
        **(manifest or {}),
    }
    return list(evidence["launch_argv"]), evidence, None


def evidence_for_request(argv: list[str], cwd: str) -> tuple[dict[str, Any] | None, str | None]:
    _, evidence, error = prepare_launch(argv, cwd)
    return evidence, error


def validate_evidence(
    evidence: Any, raw_argv: list[str], cwd: str
) -> tuple[GateDescriptor | None, str | None]:
    gate = gate_for_request(raw_argv)
    if gate is None:
        return None, "noncanonical_pnpm_gate"
    if not isinstance(evidence, dict) or evidence.get("schema") != EVIDENCE_SCHEMA:
        return None, "pnpm_gate_evidence_missing_or_invalid"
    _, expected, error = prepare_launch(raw_argv, cwd)
    if error or expected is None:
        return None, error or "pnpm_gate_evidence_invalid"
    for key in (
        "gate_id",
        "exact_request_argv",
        "launch_argv",
        "runner_env_delta",
        "manifest_integrity_digest",
        "validator_digest",
    ):
        if evidence.get(key) != expected.get(key):
            return None, f"pnpm_gate_evidence_mismatch:{key}"
    return gate, None
