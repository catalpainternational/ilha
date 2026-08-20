"""
Custom exception hierarchy for ilha.

These exceptions allow command and orchestration layers to communicate
structured failure information without duplicating logging or exit logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ilhaError(Exception):
    """Base exception carrying structured error metadata."""

    message: str
    error_code: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    exit_code: int = 1

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ilhaCommandError(ilhaError):
    """Raised when a CLI command encounters a recoverable failure."""


class PrerequisiteError(ilhaError):
    """Raised when environment or prerequisite checks fail."""


class VolumeBackupError(ilhaError):
    """Raised when a volume backup was attempted and failed.

    Distinct from "there was nothing to back up" (which is not an error): a
    deploy that requested ``--restore-volumes`` must abort on this rather than
    silently bringing the app up on an empty/fresh volume.
    """


class EnvFileError(ilhaError):
    """Raised when an env file exists but cannot be read.

    Distinct from "the file is absent" (a legitimate empty environment, ``{}``):
    an unreadable/corrupt file that silently parses to ``{}`` is the worst
    outcome — the worktree comes up with an empty environment and fails far
    downstream. Surface it at the read site instead.
    """


class ProviderAPIError(ilhaCommandError):
    """Raised when a cloud provider API call fails.

    Listing operations raise this instead of returning a partial or empty
    result: an unreadable fleet must never be mistaken for an empty one
    (a destroy pass that trusts an empty answer leaks every droplet in it).
    """

