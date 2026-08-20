"""Deploy-side service configuration.

Captures the service names, port, and health endpoint that the slim deploy
pipeline assumes about the target project. Defaults match the historical
Django/Postgres conventions so existing deploys continue to work unchanged,
but ilha now also infers a static-site profile (Caddy/Nginx, port 80, no
db/deps) when the project's footprint clearly lacks Django and a database.
Projects can override anything via ``.ilha/config.yml``::

    deploy:
      web_service: web
      web_port: 8000
      health_path: /health-check/
      dependency_services: [db, redis]
      db_service: db
      worker_services: [rq-worker-1]

For static sites the inferred defaults look like::

    deploy:
      web_service: caddy
      web_port: 80
      health_path: /
      dependency_services: []
      db_service: ""
      worker_services: []
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...utils.logging import log_warning
from ...utils.project_footprint import (
    ProjectArchetype,
    ProjectFootprint,
    detect_project_footprint,
)


# Conservative defaults for the Django+SPA archetype: every path Django
# typically owns. Anything not matched here is routed to the frontend SPA.
# Project-specific extras (e.g. ``/xero/*``, ``/planning/*``) go in
# ``deploy.api_paths`` of ``.ilha/config.yml``.
DEFAULT_DJANGO_SPA_API_PATHS = (
    "/api/*",
    "/admin",
    "/admin/*",
    "/static/*",
    "/media/*",
)
# Extra paths included when the backend speaks ASGI (Daphne / Uvicorn /
# Hypercorn / Granian). Caddy preserves ``Upgrade`` headers on
# ``reverse_proxy``, so once the path matches the backend, websockets
# work transparently. The catch matters because without ``/ws/*`` here,
# a websocket request for ``/ws/chat`` would fall through to the SPA
# frontend and fail to upgrade.
DEFAULT_ASGI_EXTRA_PATHS = ("/ws/*",)


@dataclass(frozen=True)
class DeployServiceConfig:
    """Which compose services the deploy pipeline should target.

    ``db_service``/``dependency_services``/``worker_services`` may be empty
    for projects without a database tier (e.g. a static site served by
    Caddy). The remote deploy script honors empty values by skipping the
    corresponding setup steps.
    """

    web_service: str = "web"
    web_port: int = 8000
    health_path: str = "/health-check/"
    dependency_services: List[str] = field(default_factory=lambda: ["db", "redis"])
    db_service: str = "db"
    worker_services: List[str] = field(default_factory=lambda: ["rq-worker-1"])
    # Path the post-deploy smoke check probes through Caddy at the public
    # domain. ``None`` = fall back to detection (caddy.proxy.health_check
    # label, then README hint, then ``/``). Set explicitly to override.
    smoke_path: Optional[str] = None
    # The frontend service in a Django+SPA project. Inferred from the project
    # footprint; ``None`` for solo Django (no frontend tier).
    frontend_service: Optional[str] = None
    # For Django+SPA archetype: paths that should route to ``web_service``
    # (the rest go to ``frontend_service``). ``None`` = use archetype defaults
    # when applicable. Override via ``deploy.api_paths`` in config.yml.
    api_paths: Optional[List[str]] = None
    # Post-up migrate. ``None`` = infer Django ``manage.py migrate`` when the
    # project footprint has Django; ``""`` = skip; any other string is run
    # via ``docker compose exec -T <web_service>``.
    migrate_command: Optional[str] = None
    # Extra remote commands after migrate (each is ``compose exec`` argv joined).
    post_up: List[str] = field(default_factory=list)

    def app_service_names(self) -> List[str]:
        """Web + optional frontend + workers (services ilha starts/pins)."""
        names = [self.web_service]
        if self.frontend_service:
            names.append(self.frontend_service)
        names.extend(self.worker_services)
        return [n for n in names if n]

    def resolved_migrate_command(self, project_root: Path) -> str:
        """Return the migrate command to run after ``compose up``, or ``""``."""
        if self.migrate_command is not None:
            return str(self.migrate_command).strip()
        fp = detect_project_footprint(project_root)
        if getattr(fp, "has_django", False):
            return "python manage.py migrate --noinput"
        return ""

    @classmethod
    def static_defaults(
        cls,
        *,
        web_service: str = "caddy",
        web_port: int = 80,
        health_path: str = "/",
    ) -> "DeployServiceConfig":
        """Defaults for a static-site project (Caddy/Nginx, no db tier)."""
        return cls(
            web_service=web_service,
            web_port=web_port,
            health_path=health_path,
            dependency_services=[],
            db_service="",
            worker_services=[],
        )

    @classmethod
    def from_mapping(
        cls,
        data: Optional[Dict[str, Any]],
        *,
        base: Optional["DeployServiceConfig"] = None,
    ) -> "DeployServiceConfig":
        """Build a config from the ``deploy:`` block of config.yml.

        Unknown keys are ignored with a warning; missing keys fall back to
        ``base`` (or the historical Django defaults if base is None).
        """
        defaults = base or cls()
        if not data:
            return defaults

        known = {
            "web_service",
            "web_port",
            "health_path",
            "dependency_services",
            "db_service",
            "worker_services",
            "smoke_path",
            "frontend_service",
            "api_paths",
            "migrate_command",
            "post_up",
        }
        unknown = set(data.keys()) - known
        if unknown:
            log_warning(
                f"Ignoring unknown keys in deploy config: {sorted(unknown)}. "
                f"Known keys: {sorted(known)}."
            )

        kwargs: Dict[str, Any] = {
            "web_service": defaults.web_service,
            "web_port": defaults.web_port,
            "health_path": defaults.health_path,
            "dependency_services": list(defaults.dependency_services),
            "db_service": defaults.db_service,
            "worker_services": list(defaults.worker_services),
            "smoke_path": defaults.smoke_path,
            "frontend_service": defaults.frontend_service,
            "api_paths": (
                list(defaults.api_paths) if defaults.api_paths is not None else None
            ),
            "migrate_command": defaults.migrate_command,
            "post_up": list(defaults.post_up),
        }
        for key in known:
            if key in data and data[key] is not None:
                kwargs[key] = data[key]

        # Normalize lists: accept comma-strings too, for convenience. Also
        # tolerate explicit empty lists (no deps / no workers).
        for list_key in ("dependency_services", "worker_services", "post_up"):
            value = kwargs.get(list_key)
            if isinstance(value, str):
                kwargs[list_key] = [s.strip() for s in value.split(",") if s.strip()]
            elif value is None:
                kwargs[list_key] = []
            elif isinstance(value, list):
                kwargs[list_key] = [str(s).strip() for s in value if str(s).strip()]

        if "migrate_command" in data and data["migrate_command"] is not None:
            kwargs["migrate_command"] = str(data["migrate_command"]).strip()

        # db_service may be intentionally empty (no database tier).
        if "db_service" in kwargs and kwargs["db_service"] is None:
            kwargs["db_service"] = ""
        if "db_service" in kwargs and isinstance(kwargs["db_service"], str):
            kwargs["db_service"] = kwargs["db_service"].strip()

        # Validate web_port shape.
        try:
            kwargs["web_port"] = int(kwargs["web_port"])
        except (TypeError, ValueError):
            log_warning(
                f"Invalid deploy.web_port {kwargs['web_port']!r}; falling back to {defaults.web_port}."
            )
            kwargs["web_port"] = defaults.web_port

        # Ensure health_path starts with / so downstream URL building is safe.
        hp = str(kwargs["health_path"]).strip() or "/"
        if not hp.startswith("/"):
            hp = "/" + hp
        kwargs["health_path"] = hp

        return cls(**kwargs)


def _infer_base_config(footprint: ProjectFootprint) -> DeployServiceConfig:
    """Build a base config purely from the project's compose footprint.

    - Static-site profile fires when the project clearly lacks a Django app
      and a database tier (web_service becomes the static server, no deps).
    - Otherwise: ``db_service``, ``dependency_services``, and
      ``worker_services`` come from the compose classifier — no historical
      ``[db, redis]`` / ``rq-worker-1`` guesses. ``web_service``, ``web_port``,
      and ``health_path`` keep their conventional defaults; users override
      via ``deploy:`` in ``.ilha/config.yml``.
    """
    if (
        footprint.has_static_service
        and not footprint.has_django
        and not footprint.has_postgres
        and footprint.primary_static_service
    ):
        return DeployServiceConfig.static_defaults(
            web_service=footprint.primary_static_service,
            web_port=footprint.primary_static_port or 80,
        )
    is_spa = footprint.archetype == ProjectArchetype.DJANGO_SPA
    api_paths: Optional[List[str]] = None
    if is_spa:
        api_paths = list(DEFAULT_DJANGO_SPA_API_PATHS)
        if footprint.backend_is_asgi:
            api_paths.extend(DEFAULT_ASGI_EXTRA_PATHS)
    return DeployServiceConfig(
        web_service=footprint.backend_service or "web",
        dependency_services=list(footprint.web_dependency_services),
        db_service=footprint.db_service or "",
        worker_services=sorted(footprint.worker_service_names),
        frontend_service=footprint.frontend_service if is_spa else None,
        api_paths=api_paths,
    )


def load_deploy_service_config(
    project_root: Path,
    *,
    footprint: Optional[ProjectFootprint] = None,
) -> DeployServiceConfig:
    """Load the deploy service config from ``.ilha/config.yml`` under ``deploy:``.

    Inferred per-project defaults (Django/Postgres vs static) are picked up
    from the project footprint when an explicit ``deploy:`` block is absent
    or partial. An explicit block always wins; only missing keys fall back
    to the inferred defaults.
    """
    from ...config.settings import ILHA_DIR

    fp = footprint if footprint is not None else detect_project_footprint(project_root)
    base = _infer_base_config(fp)

    config_path = project_root / ILHA_DIR / "config.yml"
    if not config_path.exists():
        return base

    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text()) or {}
    except Exception as exc:
        log_warning(
            f"Could not parse {config_path} for deploy service config: {exc}. Using inferred defaults."
        )
        return base

    config = DeployServiceConfig.from_mapping(raw.get("deploy"), base=base)
    return _filter_against_compose(config, fp)


def _filter_against_compose(
    config: DeployServiceConfig, footprint: ProjectFootprint
) -> DeployServiceConfig:
    """Strip ``dependency_services``/``worker_services`` entries that aren't
    declared as services in the project's compose file.

    Why: defaults assume the historical Django stack (db, redis, rq-worker-1)
    but real projects may omit any of those. Asking ``docker compose up`` for a
    nonexistent service fails the whole deploy, so we filter to what's
    actually defined and warn about removed entries.
    """
    known = footprint.service_names
    if not known:
        return config

    def _filter(values: List[str], label: str) -> List[str]:
        kept = [s for s in values if s in known]
        dropped = [s for s in values if s not in known]
        if dropped:
            log_warning(
                f"deploy.{label} references services not in compose: {dropped}. "
                f"Skipping them."
            )
        return kept

    new_deps = _filter(config.dependency_services, "dependency_services")
    new_workers = _filter(config.worker_services, "worker_services")
    new_db = config.db_service if (not config.db_service or config.db_service in known) else ""
    if config.db_service and not new_db:
        log_warning(
            f"deploy.db_service '{config.db_service}' not found in compose; "
            f"treating as no-database deploy."
        )

    if (
        new_deps == config.dependency_services
        and new_workers == config.worker_services
        and new_db == config.db_service
    ):
        return config

    new_frontend = (
        config.frontend_service
        if (not config.frontend_service or config.frontend_service in known)
        else None
    )
    if config.frontend_service and not new_frontend:
        log_warning(
            f"deploy.frontend_service '{config.frontend_service}' not in compose; "
            f"falling back to backend-only routing."
        )

    return replace(
        config,
        dependency_services=new_deps,
        db_service=new_db,
        worker_services=new_workers,
        frontend_service=new_frontend,
    )
