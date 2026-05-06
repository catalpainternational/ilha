"""
Overlay generator for ilha.

Produces a compose overlay (`.ilha/docker-compose.overlay.yml`) that, when
combined with the user's own ``docker-compose.yml`` via
``docker compose -f docker-compose.yml -f .ilha/docker-compose.overlay.yml``,
adds the small set of deltas ilha owns: per-worktree container names, the
ilha Caddy proxy network, Caddy labels for routable services, host-port
isolation, and env-file wiring.

Read-only on the user's compose. The user's file is never mutated and is
not even copied — compose deep-merges base + overlay at runtime.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .caddy_config import (
    WEB_SERVICE_NAMES,
    _detect_service_port,
    _has_caddy_labels,
    _is_static_web_service,
)
from .compose_yaml import compose_reset
from .deploy_hosts import normalize_deploy_host


# Services whose presence in the user's root compose conflicts with ilha's
# global Caddy proxy (which runs as the `ilha-proxy` project). Setup emits
# a warning when any of these is detected — overlays cannot remove services,
# so the user has to remove them from their own compose.
CONFLICTING_SERVICE_NAMES = {"caddy", "caddy-monitor", "ilha_caddy_proxy"}


def detect_conflicting_services(user_compose: Dict[str, Any]) -> list[str]:
    """Return service names in the user's compose that conflict with ilha's proxy.

    Static-only projects whose only service is named ``caddy`` are not
    flagged: a single ``caddy`` service IS the app, and ilha simply puts
    Caddy labels on it. The conflict is when ``caddy`` exists alongside
    other app services (in which case it's almost certainly a leftover
    from a previous setup or a copy-pasted dev proxy).
    """
    services = user_compose.get("services") or {}
    if not isinstance(services, dict):
        return []
    names = list(services.keys())
    conflicts: list[str] = []
    has_caddy = "caddy" in names
    has_other_app_services = any(n != "caddy" and n not in CONFLICTING_SERVICE_NAMES for n in names)
    if has_caddy and has_other_app_services:
        conflicts.append("caddy")
    for n in ("caddy-monitor", "ilha_caddy_proxy"):
        if n in names:
            conflicts.append(n)
    return conflicts


def _is_routable(service_name: str, service_config: Dict[str, Any]) -> bool:
    """Decide whether a service should receive Caddy labels and host-port isolation.

    Routable services are those whose traffic should flow through the global
    Caddy proxy on ``ilha_caddy_proxy``. Heuristic mirrors the one used by
    ``ensure_caddy_labels_and_network`` in caddy_config.py.
    """
    if not isinstance(service_config, dict):
        return False
    return (
        service_name in WEB_SERVICE_NAMES
        or _is_static_web_service(service_config)
        or _has_caddy_labels(service_config)
    )


def _resolve_proxy_domain(domain: Optional[str], ip: Optional[str]) -> str:
    """Pick the Caddy ``caddy.proxy`` label value.

    Priority: explicit ``domain`` (production), then ``ip`` (HTTP-only),
    then a runtime variable reference that resolves at ``docker compose``
    time from env.ilha's ``SITE_DOMAIN`` (set per-worktree to the bare
    ``<project>-<branch>.localhost`` host).
    """
    if domain:
        return normalize_deploy_host(domain)
    if ip:
        return normalize_deploy_host(ip)
    return "${SITE_DOMAIN:-${COMPOSE_PROJECT_NAME}.localhost}"


def _is_external_volume(volume_config: Any) -> bool:
    """Return whether a top-level compose volume is managed outside ilha."""
    if not isinstance(volume_config, dict):
        return False
    external = volume_config.get("external")
    if isinstance(external, dict):
        return bool(external.get("name") or external.get("external"))
    return bool(external)


def generate_overlay(
    user_compose: Dict[str, Any],
    *,
    domain: Optional[str] = None,
    ip: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the compose overlay dict for the user's project.

    The user's compose dict is treated as read-only — this function never
    mutates it. The returned dict is what ilha writes to
    ``.ilha/docker-compose.overlay.yml``.

    Django-specific deltas live in env.ilha (gated by ``has_django`` in
    ``generate_env_compose_content``); the overlay itself is framework-neutral.
    """
    services_in: Dict[str, Any] = user_compose.get("services") or {}
    proxy_domain = _resolve_proxy_domain(domain, ip)

    services_out: Dict[str, Any] = {}
    any_routable = False

    for service_name, service_config in services_in.items():
        if service_name in CONFLICTING_SERVICE_NAMES and service_name != "caddy":
            # caddy-monitor / ilha_caddy_proxy never receive an overlay; user
            # is warned at setup time and expected to remove them.
            continue

        sc = service_config if isinstance(service_config, dict) else {}
        ovr: Dict[str, Any] = {
            "container_name": f"${{COMPOSE_PROJECT_NAME}}-{service_name}",
            "env_file": [
                "${PROJECT_ROOT}/.env",
                "${PROJECT_ROOT}/.ilha/env.ilha",
            ],
        }

        if _is_routable(service_name, sc):
            any_routable = True
            port = _detect_service_port(sc, service_name=service_name)
            # Compose appends list values during multi-file merges. Use its
            # explicit reset tag so base host-port mappings do not leak into
            # worktree environments.
            ovr["ports"] = compose_reset()
            ovr["expose"] = [str(port)]
            ovr["labels"] = {
                "caddy.proxy": proxy_domain,
                "caddy.proxy.reverse_proxy": (
                    f"${{COMPOSE_PROJECT_NAME}}-{service_name}:{port}"
                ),
            }
            # Mapping form so compose deep-merges with whatever the user has.
            ovr["networks"] = {"default": None, "ilha_caddy_proxy": None}

        services_out[service_name] = ovr

    overlay: Dict[str, Any] = {"services": services_out}

    volumes_in = user_compose.get("volumes") or {}
    if isinstance(volumes_in, dict):
        volumes_out = {
            volume_name: {"name": f"${{COMPOSE_PROJECT_NAME}}_{volume_name}"}
            for volume_name, volume_config in volumes_in.items()
            if not _is_external_volume(volume_config)
        }
        if volumes_out:
            overlay["volumes"] = volumes_out

    if any_routable:
        # Top-level networks declaration only required when at least one
        # service references the external ilha_caddy_proxy network.
        overlay["networks"] = {"ilha_caddy_proxy": {"external": True}}

    return overlay
