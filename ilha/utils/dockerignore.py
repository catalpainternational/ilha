"""Helpers for refusing secret-bearing Docker build contexts."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from ilha.exceptions import ilhaCommandError

# Directories that commonly hold operator secrets or dumps. If they exist in
# the build context they must be excluded by `.dockerignore`.
DEFAULT_SECRET_CONTEXT_DIRS = (".ilha", "secrets", "backups")


def dockerignore_covers(text: str, name: str) -> bool:
    """Return True if a root-level directory *name* is excluded by dockerignore.

    This is a conservative matcher, not a full Docker glob implementation.
    Negation lines (``!...``) are ignored so a later un-ignore cannot silently
    satisfy the gate.
    """
    needle = name.strip().strip("/")
    if not needle:
        return False
    accepted = {
        needle,
        f"{needle}/",
        f"{needle}/**",
        f"**/{needle}",
        f"**/{needle}/",
        f"**/{needle}/**",
    }
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if line in accepted or line.rstrip("/") == needle:
            return True
        if line.startswith(f"{needle}/"):
            return True
    return False


def missing_secret_dir_ignores(
    context: Path,
    *,
    required_dirs: Sequence[str] = DEFAULT_SECRET_CONTEXT_DIRS,
) -> list[str]:
    """Return required directory names that exist in *context* but are not ignored."""
    dockerignore = context / ".dockerignore"
    text = dockerignore.read_text(encoding="utf-8") if dockerignore.exists() else ""
    missing: list[str] = []
    for name in required_dirs:
        if (context / name).exists() and not dockerignore_covers(text, name):
            missing.append(name)
    return missing


def assert_build_context_excludes_secrets(
    context: Path,
    *,
    required_dirs: Iterable[str] = DEFAULT_SECRET_CONTEXT_DIRS,
) -> None:
    """Refuse a Docker build whose context would copy operator secrets into the image."""
    missing = missing_secret_dir_ignores(context, required_dirs=tuple(required_dirs))
    if not missing:
        return
    listed = ", ".join(f"{name}/" for name in missing)
    raise ilhaCommandError(
        f"Build context {context} contains {listed} but .dockerignore does not exclude "
        f"{listed}. Add those paths to {context / '.dockerignore'} before `ilha image build` "
        "or `ilha image publish` so age keys, env files, and backups cannot enter the image."
    )
