"""VPC port-binding helpers shared by slim deploys and worker bundles.

The deploy path closes every host port publish for security; a configured
``worker_pool``'s ephemeral droplets reach central's db/redis through
private-IP bindings re-published from these helpers. Lives in utils so both
``core`` (worker bundle compose) and ``commands.push`` (slim deploy) can use
it without inverting the layering.
"""

from __future__ import annotations

from typing import List

import yaml

from .logging import log_info

def read_vpc_bind_config(worktree_path) -> tuple:
    """Return ``(auto_bind_ports, bind_to_private_ip)`` from ``.ilha/config.yml``.

    Single reader for the ``vpc:`` block so slim deploys and worker bundles
    agree on the opt-in semantics. Missing/broken config reads as disabled.
    """
    config_path = worktree_path / ".ilha" / "config.yml"
    if not config_path.exists():
        return False, True
    try:
        with open(config_path) as handle:
            vpc = (yaml.safe_load(handle) or {}).get("vpc", {}) or {}
        return bool(vpc.get("auto_bind_ports", False)), bool(vpc.get("bind_to_private_ip", True))
    except Exception:
        return False, True


def detect_service_expose_ports(compose_data: dict, service_name: str) -> List[int]:
    """Container ports from a service's ``expose`` list (never ``ports``).

    ``expose`` is how a compose file declares "this service serves here"
    without publishing on the host — exactly the set VPC bindings should
    republish on the private interface.
    """
    service = (compose_data.get("services") or {}).get(service_name) or {}
    ports: List[int] = []
    for port in service.get("expose") or []:
        try:
            ports.append(int(port))
        except (ValueError, TypeError):
            continue
    return sorted(set(ports))


def apply_vpc_port_bindings(compose_data: dict, services: List[str], private_ip: str) -> List[str]:
    """Bind each service's exposed ports to ``private_ip`` in place.

    Returns the service names that were bound. A service with an existing
    non-empty ``ports`` mapping is the user's own choice and is left alone;
    an empty ``ports: []`` (the deploy default that closes public publishes)
    is replaced. Ephemeral worker droplets reach central's db/redis through
    exactly these bindings — without them the fleet spins up and idles.
    """
    bound: List[str] = []
    compose_services = compose_data.get("services") or {}
    for service_name in services:
        service = compose_services.get(service_name)
        if not isinstance(service, dict):
            continue
        if service.get("ports"):
            continue
        exposed = detect_service_expose_ports(compose_data, service_name)
        if not exposed:
            continue
        service["ports"] = [f"{private_ip}:{port}:{port}" for port in exposed]
        bound.append(service_name)
        log_info(
            f"VPC port bindings for '{service_name}': "
            + ", ".join(service["ports"])
        )
    return bound
