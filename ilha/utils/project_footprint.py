"""Project footprint detection.

Heuristics for deciding whether a project/worktree contains a Django app, a
Postgres-backed database service, and/or a static (Caddy/Nginx) app service.
The signals are intentionally cheap and file-based so they can run inside
preflight without spinning up containers.

Used to scope Django/Postgres-specific deploy gates (e.g. SITE_DOMAIN
bare-host enforcement, DJANGO_SECRET_KEY / POSTGRES_PASSWORD insecure-default
checks) and to infer sensible deploy defaults for Caddy-only static sites.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ProjectArchetype(str, enum.Enum):
    """High-level shape of a project's compose stack.

    Used to pick prod-correct defaults for routing, smoke checks, and frontend
    build strategy. Inferred from the compose file; overridable via
    ``deploy.archetype`` in ``.ilha/config.yml``.
    """

    SOLO_DJANGO = "solo_django"
    DJANGO_SPA = "django_spa"
    STATIC_ONLY = "static_only"
    UNKNOWN = "unknown"


# Image-name fragments that strongly imply a Postgres-flavored service. Plain
# `db` as a service name is intentionally NOT enough on its own — many stacks
# use `db` for MySQL/MariaDB.
_POSTGRES_IMAGE_TOKENS = ("postgres", "postgis", "pgvector", "timescale")

# Image-name prefixes that imply a static-file web server. Order matters: more
# specific tokens first.
_STATIC_IMAGE_PREFIXES = ("caddy", "nginx", "httpd")

_DJANGO_FILE_HINTS = ("manage.py",)
_PYTHON_DEP_HINTS = ("pyproject.toml", "requirements.txt", "Pipfile")

# Patterns that imply a service runs as a background job worker. Any match
# (against the service's name OR its `command` string) is enough to classify
# the service as a worker. Workers are excluded from `dependency_services`
# even if they look infra-like, because the deploy pipeline starts them
# separately after the web service is healthy.
_WORKER_NAME_PATTERN = re.compile(r"(?:^|[-_])(?:rq[-_]?worker|celery|dramatiq|sidekiq|huey|worker)(?:$|[-_])", re.IGNORECASE)
_WORKER_COMMAND_PATTERN = re.compile(r"\b(?:rq\s+worker|celery\s+(?:-A\s+\S+\s+)?worker|dramatiq|sidekiq|huey_consumer)\b", re.IGNORECASE)

# Frontend service detection. Service is "Vite/Node-style" when its image is a
# JavaScript runtime OR its command invokes a JS package manager / build tool.
_FRONTEND_IMAGE_PREFIXES = ("node", "node:", "oven/bun", "denoland/deno")
_FRONTEND_COMMAND_PATTERN = re.compile(
    r"\b(?:npm|pnpm|yarn|bun|vite|next|nuxt|remix|astro)\b", re.IGNORECASE
)

# Backend (Python web) service detection. Match either Python image families,
# Python web-server commands (Django/ASGI), or build-context-driven services
# whose Dockerfile FROM is a Python image.
_PYTHON_IMAGE_PREFIXES = ("python", "python:")
_PYTHON_COMMAND_PATTERN = re.compile(
    r"\b(?:daphne|gunicorn|uvicorn|hypercorn|granian|manage\.py|python\s+-m)\b",
    re.IGNORECASE,
)
# ASGI (websocket-capable) servers — ``/ws/*`` and similar Upgrade-driven
# paths must route to the backend, not the SPA catch-all.
_ASGI_SERVER_PATTERN = re.compile(
    r"\b(?:daphne|uvicorn|hypercorn|granian)\b", re.IGNORECASE,
)
_DOCKERFILE_PYTHON_FROM = re.compile(r"^\s*FROM\s+(?:[\w./-]+/)?python", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class ProjectFootprint:
    """Footprint flags inferred from a worktree's filesystem and compose data.

    ``primary_static_service`` / ``primary_static_port`` are populated only
    when the worktree's app service is a static file server (Caddy/Nginx) and
    no Django/Postgres footprint is present.
    """

    has_django: bool
    has_postgres: bool
    has_static_service: bool
    primary_static_service: Optional[str] = None
    primary_static_port: Optional[int] = None
    service_names: frozenset = frozenset()
    # Inferred from the project's compose file. Populated whenever a compose
    # file is found; empty otherwise. The deploy layer uses these to build a
    # ``DeployServiceConfig`` without falling back to historical guesses
    # (e.g. assuming every project ships a ``db``/``redis``/``rq-worker-1``).
    db_service: Optional[str] = None
    web_dependency_services: Tuple[str, ...] = ()
    worker_service_names: frozenset = frozenset()
    # High-level project shape + the service names backing it. Inferred for
    # routing/smoke decisions; ``unknown`` means we couldn't decide with
    # confidence (caller should keep historical behavior).
    archetype: ProjectArchetype = ProjectArchetype.UNKNOWN
    backend_service: Optional[str] = None
    frontend_service: Optional[str] = None
    # True when the backend service runs an ASGI server (Daphne / Uvicorn /
    # Hypercorn / Granian). Drives default ``/ws/*`` routing so websockets
    # don't get swallowed by the SPA catch-all.
    backend_is_asgi: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_django": self.has_django,
            "has_postgres": self.has_postgres,
            "has_static_service": self.has_static_service,
            "primary_static_service": self.primary_static_service,
            "primary_static_port": self.primary_static_port,
            "service_names": sorted(self.service_names),
            "db_service": self.db_service,
            "web_dependency_services": list(self.web_dependency_services),
            "worker_service_names": sorted(self.worker_service_names),
            "archetype": self.archetype.value,
            "backend_service": self.backend_service,
            "frontend_service": self.frontend_service,
            "backend_is_asgi": self.backend_is_asgi,
        }


def _service_image(service: Any) -> str:
    if not isinstance(service, dict):
        return ""
    image = service.get("image")
    return str(image).lower() if image else ""


def _service_env(service: Any) -> Dict[str, str]:
    """Return service environment as a flat ``Dict[str, str]`` regardless of yaml shape."""
    if not isinstance(service, dict):
        return {}
    env = service.get("environment")
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items() if k is not None}
    if isinstance(env, list):
        out: Dict[str, str] = {}
        for entry in env:
            if isinstance(entry, str) and "=" in entry:
                key, value = entry.split("=", 1)
                out[key.strip()] = value.strip()
        return out
    return {}


def _looks_like_postgres(service_name: str, service: Any) -> bool:
    image = _service_image(service)
    if any(token in image for token in _POSTGRES_IMAGE_TOKENS):
        return True
    env = _service_env(service)
    has_postgres_env = any(key.startswith("POSTGRES_") for key in env)
    if service_name == "db" and has_postgres_env:
        return True
    return has_postgres_env and not image
    # If image is unknown and POSTGRES_* env vars are present, treat as Postgres.


def _looks_like_static(service: Any) -> bool:
    image = _service_image(service)
    if not image:
        return False
    base = image.split(":", 1)[0].split("/", 1)[-1]
    return any(base.startswith(prefix) for prefix in _STATIC_IMAGE_PREFIXES)


def _detect_static_port(service: Any) -> Optional[int]:
    """Return the first usable container-side port for a static service."""
    if not isinstance(service, dict):
        return None
    expose = service.get("expose")
    if isinstance(expose, list):
        for entry in expose:
            try:
                return int(str(entry).split("/", 1)[0])
            except (TypeError, ValueError):
                continue
    ports = service.get("ports")
    if isinstance(ports, list):
        for entry in ports:
            if isinstance(entry, str):
                container_part = entry.split(":")[-1].split("/", 1)[0]
                try:
                    return int(container_part)
                except ValueError:
                    continue
            if isinstance(entry, int):
                return entry
    image = _service_image(service)
    if image.startswith("caddy"):
        return 80
    if image.startswith(("nginx", "httpd")):
        return 80
    return None


def _load_compose(compose_path: Path) -> Optional[Dict[str, Any]]:
    if not compose_path.exists():
        return None
    try:
        import yaml  # local import keeps yaml optional at module load
        return yaml.safe_load(compose_path.read_text()) or {}
    except Exception:
        return None


def _has_django_marker(worktree_path: Path) -> bool:
    if any((worktree_path / hint).exists() for hint in _DJANGO_FILE_HINTS):
        return True
    for dep_file in _PYTHON_DEP_HINTS:
        candidate = worktree_path / dep_file
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(errors="ignore").lower()
        except OSError:
            continue
        if "django" in text:
            return True
    return False


def _service_command(service: Any) -> str:
    """Return a service's ``command`` flattened to a single lowercase string."""
    if not isinstance(service, dict):
        return ""
    cmd = service.get("command")
    if isinstance(cmd, str):
        return cmd.lower()
    if isinstance(cmd, list):
        return " ".join(str(part) for part in cmd).lower()
    return ""


def _looks_like_worker(name: str, service: Any) -> bool:
    """A service is a worker when its name OR command matches a known runner."""
    if _WORKER_NAME_PATTERN.search(name):
        return True
    return bool(_WORKER_COMMAND_PATTERN.search(_service_command(service)))


def _parse_depends_on(service: Any) -> List[str]:
    """Return the names declared in a service's ``depends_on`` (any form)."""
    if not isinstance(service, dict):
        return []
    deps = service.get("depends_on")
    if isinstance(deps, list):
        return [str(d) for d in deps if isinstance(d, str)]
    if isinstance(deps, dict):
        return [str(k) for k in deps.keys() if isinstance(k, str)]
    return []


def _classify_services(
    services: Dict[str, Any], web_service: str
) -> Tuple[Optional[str], Tuple[str, ...], frozenset]:
    """Identify db/dependency/worker services purely from compose data.

    Returns ``(db_service, web_dependency_services, worker_service_names)``.

    - Workers are services whose name or command matches a known runner.
    - The db service is the first service classified as Postgres.
    - Dependency services come from the web service's explicit ``depends_on``
      when present, filtered to known services and excluding workers and the
      web service itself. If no ``depends_on`` is declared, fall back to
      every image-only (non-built, non-worker, non-web) service — these are
      the infra tier that the web service typically needs.
    """
    if not services:
        return None, (), frozenset()

    workers = {name for name, svc in services.items() if _looks_like_worker(name, svc)}

    db_service: Optional[str] = None
    for name, svc in services.items():
        if name in workers:
            continue
        if _looks_like_postgres(name, svc):
            db_service = name
            break

    web_svc = services.get(web_service)
    declared_deps = _parse_depends_on(web_svc) if web_svc is not None else []
    if declared_deps:
        deps = tuple(
            d for d in declared_deps
            if d in services and d != web_service and d not in workers
        )
    else:
        deps = tuple(
            name for name, svc in services.items()
            if name != web_service
            and name not in workers
            and isinstance(svc, dict)
            and svc.get("image")
            and "build" not in svc
        )

    return db_service, deps, frozenset(workers)


def _looks_like_frontend(name: str, service: Any) -> bool:
    """Service is a Vite/Node-style frontend (image OR command signal)."""
    image = _service_image(service)
    if image and any(image.startswith(prefix) for prefix in _FRONTEND_IMAGE_PREFIXES):
        return True
    return bool(_FRONTEND_COMMAND_PATTERN.search(_service_command(service)))


def _looks_like_python_backend(
    name: str, service: Any, worktree_path: Optional[Path] = None
) -> bool:
    """Service is a Python web backend (Daphne/Gunicorn/Uvicorn-style)."""
    if not isinstance(service, dict):
        return False
    image = _service_image(service)
    if image and any(image.startswith(prefix) for prefix in _PYTHON_IMAGE_PREFIXES):
        return True
    if _PYTHON_COMMAND_PATTERN.search(_service_command(service)):
        return True
    # Fall back to inspecting the build context's Dockerfile for FROM python.
    if "build" in service and worktree_path is not None:
        build = service["build"]
        ctx = build if isinstance(build, str) else (
            build.get("context") if isinstance(build, dict) else None
        )
        dockerfile_name = (
            build.get("dockerfile", "Dockerfile") if isinstance(build, dict) else "Dockerfile"
        )
        if isinstance(ctx, str):
            try:
                df_path = (worktree_path / ctx / dockerfile_name).resolve()
                if df_path.is_file():
                    head = df_path.read_text(errors="ignore")[:4096]
                    if _DOCKERFILE_PYTHON_FROM.search(head):
                        return True
            except (OSError, ValueError):
                pass
    return False


def _classify_archetype(
    services: Dict[str, Any],
    worktree_path: Path,
    worker_names: frozenset,
    static_service: Optional[str],
) -> Tuple[ProjectArchetype, Optional[str], Optional[str]]:
    """Pick an archetype and the service names backing it.

    Workers and infra (image-only DBs/caches) are excluded from the
    backend/frontend choices. Returns ``(archetype, backend, frontend)``.
    """
    backend: Optional[str] = None
    frontend: Optional[str] = None
    for name, svc in services.items():
        if name in worker_names:
            continue
        if frontend is None and _looks_like_frontend(name, svc):
            frontend = name
            continue
        if backend is None and _looks_like_python_backend(name, svc, worktree_path):
            backend = name

    if backend and frontend:
        return ProjectArchetype.DJANGO_SPA, backend, frontend
    if backend:
        return ProjectArchetype.SOLO_DJANGO, backend, None
    if frontend and not backend:
        # Frontend without a Python backend: lean on the static-server detector
        # to decide whether this is a static-only deploy.
        if static_service:
            return ProjectArchetype.STATIC_ONLY, None, frontend
        return ProjectArchetype.UNKNOWN, None, frontend
    if static_service:
        return ProjectArchetype.STATIC_ONLY, None, None
    return ProjectArchetype.UNKNOWN, None, None


def _candidate_compose_files(worktree_path: Path) -> List[Path]:
    """Compose files we may inspect for footprint detection.

    The user's compose is authoritative for service-shape questions
    (Postgres? static caddy?). The ilha overlay is consulted as a fallback
    so tests that drop only the overlay still detect a footprint.
    """
    return [
        worktree_path / "docker-compose.yml",
        worktree_path / "compose.yml",
        worktree_path / ".ilha" / "docker-compose.overlay.yml",
    ]


def detect_project_footprint(worktree_path: Path) -> ProjectFootprint:
    """Return footprint flags for a worktree (or project root)."""
    has_django = _has_django_marker(worktree_path)

    has_postgres = False
    static_service: Optional[str] = None
    static_port: Optional[int] = None
    other_app_service = False

    services: Dict[str, Any] = {}
    for compose_path in _candidate_compose_files(worktree_path):
        data = _load_compose(compose_path)
        if not data:
            continue
        candidate_services = data.get("services") or {}
        if isinstance(candidate_services, dict) and candidate_services:
            services = candidate_services
            break

    for name, svc in services.items():
        if _looks_like_postgres(name, svc):
            has_postgres = True
            continue
        if _looks_like_static(svc):
            if static_service is None:
                static_service = name
                static_port = _detect_static_port(svc)
            continue
        # Anything else (web app, worker, redis, etc.) counts as "another app
        # service exists", which means the static service is supporting infra,
        # not the primary app.
        other_app_service = True

    has_static_service = static_service is not None
    if other_app_service or has_django:
        # Static service is not the primary app; don't surface it as such.
        static_service = None
        static_port = None

    db_service, web_deps, worker_names = _classify_services(services, web_service="web")
    archetype, backend, frontend = _classify_archetype(
        services, worktree_path, worker_names, static_service
    )
    backend_is_asgi = bool(
        backend
        and isinstance(services.get(backend), dict)
        and _ASGI_SERVER_PATTERN.search(_service_command(services[backend]))
    )

    return ProjectFootprint(
        has_django=has_django,
        has_postgres=has_postgres,
        has_static_service=has_static_service,
        primary_static_service=static_service,
        primary_static_port=static_port,
        service_names=frozenset(str(name) for name in services.keys()),
        db_service=db_service,
        web_dependency_services=web_deps,
        worker_service_names=worker_names,
        archetype=archetype,
        backend_service=backend,
        frontend_service=frontend,
        backend_is_asgi=backend_is_asgi,
    )
