"""Resolve Docker host volume names for ``external: true`` compose volumes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, MutableMapping, Optional

from .validation import validate_volume_exists  # for resolve_compose_project_name

_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def expand_compose_vars(template: str, env: MutableMapping[str, str]) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` using *env* (iterative until stable)."""

    def _replace_one(s: str) -> str:
        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            default = match.group(2)
            if key in env:
                return str(env[key])
            if default is not None:
                return default
            return match.group(0)

        return _VAR_PATTERN.sub(repl, s)

    result = str(template)
    for _ in range(20):
        nxt = _replace_one(result)
        if nxt == result:
            return nxt
        result = nxt
    return result


@dataclass(frozen=True)
class NamedVolume:
    """A named volume declared in a project's compose ``volumes:`` block.

    ``key`` is the dictionary key in the compose (``pgdata`` in
    ``volumes: { pgdata: }``). ``docker_name`` is the actual volume name
    Docker uses on the host — derived from the compose-project-name prefix
    or from an explicit ``name:`` override on external volumes.
    """

    key: str
    docker_name: str
    is_external: bool
    declared_in: str  # path of the compose file that declared (or last redeclared) this key


def _resolve_volume_docker_name(
    key: str, spec: Dict[str, Any], compose_project_name: str
) -> str:
    """Compute the docker volume name compose would resolve at runtime.

    External volumes use ``name:`` if present, else the bare key (Docker's
    ``external`` convention treats the key as the literal volume name).
    Non-external volumes get the standard ``{project}_{key}`` prefix that
    ``docker compose up`` produces.
    """
    name_field = spec.get("name") if isinstance(spec, dict) else None
    if name_field:
        return str(name_field)
    if isinstance(spec, dict) and spec.get("external"):
        return key  # bare key, no project prefix — compose's external default
    if compose_project_name:
        return f"{compose_project_name}_{key}"
    return key


def discover_named_volumes(
    compose_paths: Iterable[Path],
    env: Optional[Dict[str, str]] = None,
) -> List[NamedVolume]:
    """Return every named volume declared across the given compose files.

    Source of truth for backup/restore: parses the project's actual
    ``volumes:`` block(s) instead of guessing from a hardcoded suffix list.
    Multiple compose files merge with last-wins semantics on duplicate keys
    (matches ``docker compose -f a -f b ...`` behavior). Skipped:

    - Anonymous volumes (no top-level entry)
    - Bind mounts (handled separately by deploy file copying)

    External volumes are returned with ``is_external=True`` so callers can
    decide whether to include them in a backup (typically: skip — the
    project owns their lifecycle).
    """
    try:
        import yaml
    except ImportError:
        return []

    env = env or {}
    seen: Dict[str, NamedVolume] = {}

    for compose_path in compose_paths:
        if not compose_path.exists():
            continue
        try:
            raw_text = compose_path.read_text(encoding="utf-8")
        except OSError:
            continue
        expanded = "\n".join(expand_compose_vars(line, env) for line in raw_text.splitlines())
        try:
            data: Dict[str, Any] = yaml.safe_load(expanded) or {}
        except yaml.YAMLError:
            continue
        volumes = data.get("volumes") or {}
        if not isinstance(volumes, dict):
            continue

        compose_project_name = env.get("COMPOSE_PROJECT_NAME", "").strip()
        for key, spec in volumes.items():
            spec_dict: Dict[str, Any] = spec if isinstance(spec, dict) else {}
            is_external = bool(spec_dict.get("external"))
            docker_name = _resolve_volume_docker_name(
                str(key), spec_dict, compose_project_name
            )
            seen[str(key)] = NamedVolume(
                key=str(key),
                docker_name=docker_name,
                is_external=is_external,
                declared_in=str(compose_path),
            )

    return list(seen.values())


def resolve_compose_project_name(
    project_root: Path,
    branch_name: str,
    *,
    sanitized_project_name: str,
) -> str:
    """Pick the ``COMPOSE_PROJECT_NAME`` to use for ``branch_name``.

    For non-main branches, returns ilha's per-worktree convention
    ``<project>-<branch>``. For ``main`` / ``master`` branches, probes the
    Docker host for volumes declared in the project's compose under the
    *bare* project namespace; if any exist, the project pre-existed ilha
    (was used via ``docker compose up`` from the project root), and ilha
    must respect that naming so backups discover the real data.

    The probe is cheap (one ``docker volume inspect`` per declared volume)
    and only runs for main/master, so worktree deploys see no overhead.
    """
    convention = f"{sanitized_project_name}-{branch_name}"
    if branch_name not in {"main", "master"}:
        return convention

    compose_files = [
        project_root / "docker-compose.yml",
        project_root / ".ilha" / "docker-compose.overlay.yml",
    ]
    bare_env = {"COMPOSE_PROJECT_NAME": sanitized_project_name}
    discovered = discover_named_volumes(compose_files, env=bare_env)
    bare_volumes = [v.docker_name for v in discovered if not v.is_external]

    if any(validate_volume_exists(name) for name in bare_volumes):
        return sanitized_project_name
    return convention


def list_external_volume_host_names(
    compose_path: Path,
    env: Dict[str, str],
) -> List[str]:
    """Return Docker volume names required for top-level ``external: true`` volumes.

    Expands ``${COMPOSE_PROJECT_NAME}`` (and similar) using *env* before reading YAML.
    """
    try:
        import yaml
    except ImportError:
        return []

    raw_text = compose_path.read_text(encoding="utf-8")
    expanded_lines: List[str] = []
    for line in raw_text.splitlines():
        expanded_lines.append(expand_compose_vars(line, env))
    data: Dict[str, Any] = yaml.safe_load("\n".join(expanded_lines)) or {}
    volumes = data.get("volumes") or {}
    out: List[str] = []
    for key, spec in volumes.items():
        if not isinstance(spec, dict):
            continue
        if not spec.get("external"):
            continue
        name_field = spec.get("name")
        if name_field:
            name = str(name_field)
        else:
            project = env.get("COMPOSE_PROJECT_NAME", "").strip()
            name = f"{project}_{key}" if project else str(key)
        if name and name not in out:
            out.append(name)
    return out


def _load_compose_service(
    compose_path: Path, env: Dict[str, str], service: str
) -> Dict[str, Any]:
    """Return the (possibly empty) service mapping for *service* in *compose_path*."""
    try:
        import yaml
    except ImportError:
        return {}
    try:
        raw_text = compose_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    expanded = "\n".join(expand_compose_vars(line, env) for line in raw_text.splitlines())
    data: Dict[str, Any] = yaml.safe_load(expanded) or {}
    services = data.get("services") or {}
    svc = services.get(service)
    return svc if isinstance(svc, dict) else {}


def compose_web_image_reference(
    compose_path: Path,
    env: Dict[str, str],
    web_service: str,
) -> str:
    """Resolve the image reference used for *web_service* (for ``docker image inspect``).

    When the compose file only declares ``build:`` and no ``image:``, Compose defaults
    to ``{COMPOSE_PROJECT_NAME}_{service}`` with tag ``latest`` on the Docker host.
    Returns ``""`` when the service is missing from the compose file.
    """
    try:
        import yaml  # noqa: F401 — surfaced via _load_compose_service
    except ImportError:
        return ""

    try:
        raw_text = compose_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    import yaml as _yaml
    expanded = "\n".join(expand_compose_vars(line, env) for line in raw_text.splitlines())
    data: Dict[str, Any] = _yaml.safe_load(expanded) or {}
    services = data.get("services") or {}
    if web_service not in services:
        return ""
    svc = services[web_service]
    if not isinstance(svc, dict):
        return ""
    img = svc.get("image")
    if img:
        return str(img).strip()
    project = env.get("COMPOSE_PROJECT_NAME", "").strip()
    if project:
        return f"{project}_{web_service}:latest"
    return f"{web_service}:latest"


def compose_service_is_buildable(
    compose_path: Path,
    env: Dict[str, str],
    service: str,
) -> bool:
    """Return True if *service* has a ``build:`` directive in *compose_path*.

    Pull-only services (e.g. ``image: caddy:2-alpine`` with no ``build:``) are
    NOT buildable; ``docker compose up`` will pull them, and ``docker compose
    build`` would emit ``No services to build``. Callers use this to skip a
    no-op remote build phase.
    """
    svc = _load_compose_service(compose_path, env, service)
    return bool(svc.get("build"))
