"""Repository identity: single source of truth for the trusted repository slug.

The slug is read from `config/repository_identity.v1.json` and nowhere else.
It is never taken from an environment variable or CLI argument. Any problem
with the config (missing, malformed, wrong schema, invalid slug, extra keys)
raises `RepositoryIdentityError`, so callers fail closed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA = "REPOSITORY_IDENTITY_V1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "repository_identity.v1.json"

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RepositoryIdentityError(RuntimeError):
    """The identity config is missing or invalid (fail-closed)."""


def load_trusted_repo_slug(config_path: Path | None = None) -> str:
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepositoryIdentityError(f"identity_config_unreadable:{path.name}") from exc
    if not isinstance(data, dict) or set(data) != {"schema", "trusted_repo_slug"}:
        raise RepositoryIdentityError("identity_config_shape_invalid")
    if data["schema"] != SCHEMA:
        raise RepositoryIdentityError("identity_config_schema_mismatch")
    slug = data["trusted_repo_slug"]
    if (
        not isinstance(slug, str)
        or not _SLUG_RE.fullmatch(slug)
        or {".", ".."} & set(slug.split("/"))
    ):
        raise RepositoryIdentityError("identity_config_slug_invalid")
    return slug


def is_trusted_repo(repo: object, config_path: Path | None = None) -> bool:
    """Exact-match accept. Raises RepositoryIdentityError when config is unusable."""
    slug = load_trusted_repo_slug(config_path)
    return isinstance(repo, str) and repo == slug
