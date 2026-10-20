"""Stage 0: repository identity single source + accept/reject (positive and negative)."""

from __future__ import annotations

import ast
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARDS = ROOT / "scripts" / "agent-guards"
sys.path.insert(0, str(GUARDS))

import github_credentialless_read as gcr
import repo_identity

CANONICAL = "squne121/agent-platform"
LEGACY = "squne121/" + "loop-protocol"
THIRD = "example/other-repo"


def _write(tmp_path, payload):
    path = tmp_path / "identity.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _cfg(slug):
    return {"schema": "REPOSITORY_IDENTITY_V1", "trusted_repo_slug": slug}


def test_canonical_slug_is_loaded_from_config():
    assert repo_identity.load_trusted_repo_slug() == CANONICAL


def test_accepts_canonical_and_rejects_legacy_and_others():
    assert repo_identity.is_trusted_repo(CANONICAL) is True
    for bad in (LEGACY, THIRD, CANONICAL.upper(), CANONICAL + "/", " " + CANONICAL, "", None, 1):
        assert repo_identity.is_trusted_repo(bad) is False, bad


def test_gcr_accepts_canonical_and_rejects_legacy():
    assert gcr._validate_repo(CANONICAL) == CANONICAL
    assert gcr._validate_repo(None) == CANONICAL
    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr._validate_repo(LEGACY)


def test_third_slug_config_flips_accept_and_reject(tmp_path, monkeypatch):
    path = _write(tmp_path, _cfg(THIRD))
    assert repo_identity.is_trusted_repo(THIRD, path) is True
    assert repo_identity.is_trusted_repo(CANONICAL, path) is False
    monkeypatch.setattr(repo_identity, "DEFAULT_CONFIG_PATH", path)
    assert gcr._validate_repo(None) == THIRD
    assert gcr._validate_repo(THIRD) == THIRD
    for rejected in (CANONICAL, LEGACY):
        with pytest.raises(gcr.CrossRepositoryReadRejected):
            gcr._validate_repo(rejected)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        "{}",
        {"schema": "REPOSITORY_IDENTITY_V1"},
        {"schema": "WRONG", "trusted_repo_slug": CANONICAL},
        _cfg(""),
        _cfg("no-slash"),
        _cfg("a/b/c"),
        _cfg("has space/x"),
        _cfg(None),
        _cfg(5),
        {**_cfg(CANONICAL), "extra": 1},
    ],
)
def test_invalid_config_fails_closed(tmp_path, payload):
    path = _write(tmp_path, payload)
    with pytest.raises(repo_identity.RepositoryIdentityError):
        repo_identity.load_trusted_repo_slug(path)
    with pytest.raises(repo_identity.RepositoryIdentityError):
        repo_identity.is_trusted_repo(CANONICAL, path)


def test_missing_config_fails_closed_through_gcr(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_identity, "DEFAULT_CONFIG_PATH", tmp_path / "absent.json")
    with pytest.raises(repo_identity.RepositoryIdentityError):
        gcr._validate_repo(CANONICAL)
    with pytest.raises(repo_identity.RepositoryIdentityError):
        gcr.read_public_issue(1)


def test_env_cannot_override_identity(monkeypatch):
    for key in ("TRUSTED_REPO_SLUG", "GITHUB_REPOSITORY", "REPO_SLUG"):
        monkeypatch.setenv(key, THIRD)
    assert repo_identity.load_trusted_repo_slug() == CANONICAL


def test_layout_shift_fails_closed(tmp_path):
    """Moving the guards one level deeper breaks the config lookup instead of guessing."""
    shifted = tmp_path / "scripts" / "agent-guards" / "nested"
    shifted.mkdir(parents=True)
    shutil.copy(GUARDS / "repo_identity.py", shifted / "repo_identity.py")
    import importlib.util

    spec = importlib.util.spec_from_file_location("shifted_identity", shifted / "repo_identity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(mod.RepositoryIdentityError):
        mod.load_trusted_repo_slug()


def test_slug_defined_in_exactly_one_place():
    """AST check: no production module hard-codes a slug-shaped literal or a TRUSTED_REPO_SLUG."""
    slug_shape = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    offenders = []
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in (CANONICAL, LEGACY):
                offenders.append(f"{path.name}:{node.lineno} literal")
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id == "TRUSTED_REPO_SLUG":
                        offenders.append(f"{path.name}:{node.lineno} TRUSTED_REPO_SLUG")
    assert offenders == []
    config = json.loads((ROOT / "config" / "repository_identity.v1.json").read_text())
    assert slug_shape.match(config["trusted_repo_slug"])
