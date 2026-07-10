"""Declarative environment-variable schema for ilha worktrees.

Single source of truth for *which* env vars a worktree needs, whether they are
required (conditioned on the project's footprint), what their default is, and
which values are forbidden. Two consumers:

- **Generation** — ``generate_default_app_env`` builds the app-owned ``.env``
  when a project has no source ``.env`` to copy, replacing the old hardcoded
  f-string that wrote ``POSTGRES_*`` blank with a "set these by hand" comment.
- **Validation** — ``validate_env`` returns structured issues for a merged
  environment. This is the pure seam a future ``ilha doctor`` / preflight can
  call; nothing here reaches out to the CLI or the filesystem.

The schema is footprint-driven (duck-typed on ``.has_django`` / ``.has_postgres``)
so it stays framework-agnostic: a non-Django/non-Postgres project simply has
fewer required keys, rather than being forced into Django/Postgres defaults.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol

from .settings import INSECURE_DEFAULT_SECRET_KEY, generate_secret_key


class _Footprint(Protocol):
    """The subset of ProjectFootprint the schema reads (duck-typed)."""

    has_django: bool
    has_postgres: bool


def _generate_db_password() -> str:
    """A strong, URL-safe DB password.

    URL-safe so it survives being embedded in ``DATABASE_URL`` without
    percent-encoding. Both the app and the Postgres container read this from
    the same env files, so a generated value keeps them in agreement — no
    manual "make these match" step.
    """
    return secrets.token_urlsafe(24)


@dataclass(frozen=True)
class EnvVarSpec:
    """One environment variable's contract."""

    name: str
    # "app" = belongs in the user's .env (secrets, DB creds, DEBUG).
    # "ilha" = plumbing ilha owns and generates into env.ilha.
    owner: str
    # Given a footprint, is this var required for the worktree to function?
    required_when: Callable[[_Footprint], bool] = lambda f: False
    # Static default used at generation time when no generator is set.
    default: Optional[str] = None
    # Callable producing a fresh value at generation time (wins over default).
    generator: Optional[Callable[[], str]] = None
    # Values that must never be used (e.g. the public insecure SECRET_KEY).
    forbid_values: frozenset = field(default_factory=frozenset)
    # Emit into a generated .env even when not required (unconditional defaults).
    emit_always: bool = False
    description: str = ""


# App-owned env contract. ilha-owned plumbing (COMPOSE_PROJECT_NAME,
# PROJECT_ROOT, SITE_DOMAIN, ALLOWED_HOSTS, …) is generated contextually by the
# env.ilha generator and is intentionally not modelled here.
ENV_SCHEMA: List[EnvVarSpec] = [
    EnvVarSpec(
        name="POSTGRES_USER",
        owner="app",
        required_when=lambda f: f.has_postgres,
        default="postgres",
        description="PostgreSQL role; shared by the app and the db container.",
    ),
    EnvVarSpec(
        name="POSTGRES_PASSWORD",
        owner="app",
        required_when=lambda f: f.has_postgres,
        generator=_generate_db_password,
        description="PostgreSQL password; generated so app and db agree.",
    ),
    EnvVarSpec(
        name="POSTGRES_DB",
        owner="app",
        required_when=lambda f: f.has_postgres,
        default="postgres",
        description="PostgreSQL database name.",
    ),
    EnvVarSpec(
        name="DJANGO_SECRET_KEY",
        owner="app",
        required_when=lambda f: f.has_django,
        generator=generate_secret_key,
        forbid_values=frozenset({INSECURE_DEFAULT_SECRET_KEY, ""}),
        description="Django secret key; unique per environment, never the public default.",
    ),
    EnvVarSpec(
        name="DEBUG",
        owner="app",
        default="True",
        emit_always=True,
        description="Django debug flag for local worktrees.",
    ),
]


@dataclass(frozen=True)
class EnvIssue:
    """A single problem found while validating a merged environment."""

    key: str
    severity: str  # "error" | "warning"
    message: str


def _resolve_value(spec: EnvVarSpec) -> Optional[str]:
    if spec.generator is not None:
        return spec.generator()
    return spec.default


def generate_default_app_env(footprint: _Footprint) -> Dict[str, str]:
    """Build the app-owned env vars for a greenfield worktree with no source .env.

    Includes every spec that is required for this footprint (using its
    generator or default) plus any ``emit_always`` spec. Postgres creds are
    generated rather than left blank so the app and db container start in
    agreement.
    """
    out: Dict[str, str] = {}
    for spec in ENV_SCHEMA:
        if spec.owner != "app":
            continue
        if not (spec.required_when(footprint) or spec.emit_always):
            continue
        value = _resolve_value(spec)
        if value is not None:
            out[spec.name] = value
    return out


def validate_env(merged: Dict[str, str], footprint: _Footprint) -> List[EnvIssue]:
    """Return structured issues for a *merged* environment (pure; no I/O).

    Errors: a required var is missing/blank, or a var holds a forbidden value
    (e.g. the public insecure SECRET_KEY). This is the seam a preflight/doctor
    gate consumes.
    """
    issues: List[EnvIssue] = []
    for spec in ENV_SCHEMA:
        value = merged.get(spec.name)
        required = spec.required_when(footprint)

        if required and (value is None or value == ""):
            issues.append(
                EnvIssue(
                    key=spec.name,
                    severity="error",
                    message=f"{spec.name} is required for this project but is missing or blank.",
                )
            )
            continue

        if value is not None and value in spec.forbid_values:
            issues.append(
                EnvIssue(
                    key=spec.name,
                    severity="error",
                    message=f"{spec.name} is set to a forbidden value ({value!r}); "
                    "use a unique, generated value.",
                )
            )
    return issues


def required_keys(footprint: _Footprint) -> List[str]:
    """Names of the vars required for this footprint (for messaging/tests)."""
    return [s.name for s in ENV_SCHEMA if s.required_when(footprint)]
