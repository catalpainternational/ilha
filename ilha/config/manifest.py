"""The ratified project manifest: `.ilha/ilha.yml`.

ilha has to translate a user's arbitrary ``docker-compose.yml`` into its routing
+ isolation model. Historically that translation was done by silent heuristics
(name allowlists, a default port of 8000) that were never shown to the user and
re-run on every command. The manifest makes the translation **explicit and
ratified**: ``ilha setup`` proposes it (via ``manifest_from_detection``), writes
it here, and shows it; thereafter the manifest is the single authority for
"which services are web, on what port, which is the db, which volumes to clone".

Detection is demoted from silent authority to a one-time proposal the user can
edit. When the manifest is absent (a project that predates it), callers fall
back to the old heuristics — so this is additive, not a flag day.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

MANIFEST_FILENAME = "ilha.yml"
MANIFEST_VERSION = 1


class WebService(BaseModel):
    """A routable service: gets Caddy labels + host-port isolation."""

    model_config = ConfigDict(extra="forbid")

    service: str
    port: int = Field(ge=1, le=65535)
    domain: Optional[str] = None  # None → the ${SITE_DOMAIN} convention


class DbConfig(BaseModel):
    """The database service whose volume ilha clones per worktree."""

    model_config = ConfigDict(extra="forbid")

    service: str
    engine: str = "postgres"


class EnvConfig(BaseModel):
    """Which env-schema profile applies (drives ALLOWED_HOSTS/CSRF emission)."""

    model_config = ConfigDict(extra="forbid")

    profile: str = "generic"  # "django" | "generic"


class IlhaManifest(BaseModel):
    """The ratified description of how ilha runs this project."""

    # extra="forbid" turns a typo'd key into a load error instead of a silently
    # ignored setting — the manifest is hand-editable, so typo safety matters.
    model_config = ConfigDict(extra="forbid")

    version: int = MANIFEST_VERSION
    project_name: str
    web: List[WebService] = Field(default_factory=list)
    db: Optional[DbConfig] = None
    volumes: List[str] = Field(default_factory=list)
    env: EnvConfig = Field(default_factory=EnvConfig)

    def routable_ports(self) -> Dict[str, int]:
        """``{service_name: port}`` for the overlay generator (authoritative)."""
        return {w.service: w.port for w in self.web}


def manifest_path(project_root: Path) -> Path:
    from .settings import ILHA_DIR

    return Path(project_root) / ILHA_DIR / MANIFEST_FILENAME


def load_manifest(project_root: Path) -> Optional[IlhaManifest]:
    """Load and validate ``.ilha/ilha.yml``; None if absent.

    Raises pydantic ``ValidationError`` on a malformed/typo'd manifest — a
    corrupt manifest must surface, not silently fall back to heuristics.
    """
    import yaml

    path = manifest_path(project_root)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text()) or {}
    return IlhaManifest.model_validate(data)


def write_manifest(project_root: Path, manifest: IlhaManifest) -> Path:
    """Write the manifest to ``.ilha/ilha.yml`` (human-readable, key order kept)."""
    import yaml

    path = manifest_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# ilha project manifest — how ilha runs this project.\n"
        "# Edit this to change which services are web-routable (and their port),\n"
        "# which service is the database, and which volumes are cloned per worktree.\n"
        "# `ilha setup` regenerates it only with --force; otherwise your edits win.\n"
    )
    body = yaml.dump(
        manifest.model_dump(exclude_none=True),
        default_flow_style=False,
        sort_keys=False,
    )
    path.write_text(header + body)
    return path


def manifest_from_detection(
    user_compose: Dict[str, Any],
    footprint: Any,
    project_name: str,
) -> IlhaManifest:
    """Propose a manifest from the user's compose + detected footprint.

    Wraps the existing heuristics (``_is_routable``, ``_detect_service_port``,
    non-external volumes, footprint db/django) so they run exactly once — here —
    rather than on every command.
    """
    from ..utils.caddy_config import _detect_service_port
    from ..utils.overlay_generator import _is_routable, _is_external_volume

    services = user_compose.get("services") or {}
    web: List[WebService] = []
    if isinstance(services, dict):
        for name, sc in services.items():
            sc = sc if isinstance(sc, dict) else {}
            if _is_routable(name, sc):
                web.append(
                    WebService(service=name, port=_detect_service_port(sc, service_name=name))
                )

    volumes_in = user_compose.get("volumes") or {}
    volumes = (
        [v for v, cfg in volumes_in.items() if not _is_external_volume(cfg)]
        if isinstance(volumes_in, dict)
        else []
    )

    db = None
    db_service = getattr(footprint, "db_service", None)
    if db_service:
        engine = "postgres" if getattr(footprint, "has_postgres", False) else "unknown"
        db = DbConfig(service=db_service, engine=engine)

    profile = "django" if getattr(footprint, "has_django", False) else "generic"

    return IlhaManifest(
        project_name=project_name,
        web=web,
        db=db,
        volumes=volumes,
        env=EnvConfig(profile=profile),
    )


def diff_manifest_vs_compose(
    manifest: IlhaManifest, user_compose: Dict[str, Any]
) -> List[str]:
    """Return human-readable drift: manifest entries no longer in the compose."""
    services = user_compose.get("services") or {}
    names = set(services.keys()) if isinstance(services, dict) else set()
    drift: List[str] = []
    for w in manifest.web:
        if w.service not in names:
            drift.append(f"web service '{w.service}' is in the manifest but not in docker-compose.yml")
    if manifest.db and manifest.db.service not in names:
        drift.append(f"db service '{manifest.db.service}' is in the manifest but not in docker-compose.yml")
    return drift
