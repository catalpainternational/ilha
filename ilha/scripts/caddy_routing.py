"""Caddy route generation from container labels.

Single source of truth for translating ``caddy.proxy.*`` labels into
Caddy JSON routes. Both ``caddy-docker-monitor.py`` (long-running daemon
on the droplet) and ``caddy-dynamic-config.py`` (one-shot for local
worktree start-up) import from here so the schema and matcher syntax
can't drift apart.

Label schema:
    caddy.proxy=<domain>                       — required; the public host
    caddy.proxy.reverse_proxy=<container>:<port> — upstream target
    caddy.proxy.path=<comma-separated paths>   — optional; restrict route to these paths
    caddy.proxy.except=<single path>           — optional; legacy "everything except this"
    caddy.proxy.health_check=<path>            — optional; Caddy active health check
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple


def parse_path_label(value: Optional[str]) -> List[str]:
    """Split the comma-separated ``caddy.proxy.path`` value into a real list.

    Empty / missing / ``"/"`` (catch-all sentinel) all return ``[]`` so the
    caller treats the route as unconstrained on path.
    """
    if not value:
        return []
    cleaned = value.strip()
    if not cleaned or cleaned == "/":
        return []
    return [p.strip() for p in cleaned.split(",") if p.strip()]


def _default_target(container: Dict[str, Any]) -> str:
    name = container.get("Names") or container.get("name") or ""
    if isinstance(name, list):
        name = name[0] if name else ""
    return f"{name}:8000"


def _health_check_block(labels: Dict[str, str], domain: str) -> Optional[Dict[str, Any]]:
    """Return Caddy ``health_checks`` config for the upstream, or None."""
    path = labels.get("caddy.proxy.health_check")
    if not path:
        return None
    return {
        "active": {
            "path": path,
            "headers": {"Host": [domain]},
            "timeout": "30s",
            "interval": "10s",
        }
    }


def _reverse_proxy_handle(target: str, health_check: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    handle: Dict[str, Any] = {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": target}],
    }
    if health_check is not None:
        handle["health_checks"] = health_check
    return handle


def _container_target(container: Dict[str, Any], labels: Dict[str, str]) -> str:
    return labels.get("caddy.proxy.reverse_proxy") or _default_target(container)


def _container_priority(labels: Dict[str, str]) -> Tuple[int, int]:
    """Sort key: path-specific routes before catch-alls. Longer paths first."""
    paths = parse_path_label(labels.get("caddy.proxy.path"))
    if paths:
        return (0, -sum(len(p) for p in paths))
    if labels.get("caddy.proxy.except"):
        return (1, 0)
    return (2, 0)


def _build_match(host: str, paths: List[str]) -> Dict[str, Any]:
    match: Dict[str, Any] = {"host": [host]}
    if paths:
        match["path"] = paths
    return match


def _route_for_single_container(
    domain: str, container: Dict[str, Any], labels: Dict[str, str]
) -> Dict[str, Any]:
    """One container per domain → a single top-level route. Path label still
    honored even with one container (so a project can scope a single backend
    to a subset of the domain)."""
    target = _container_target(container, labels)
    paths = parse_path_label(labels.get("caddy.proxy.path"))
    return {
        "match": [_build_match(domain, paths)],
        "handle": [_reverse_proxy_handle(target, _health_check_block(labels, domain))],
    }


def _route_for_multi_container(
    domain: str, container_label_pairs: List[Tuple[Dict[str, Any], Dict[str, str]]]
) -> Dict[str, Any]:
    """Multiple containers share a domain → wrap per-service sub-routes in a
    ``subroute`` handler. Sub-routes match on path; the catch-all (no path
    constraint) sits last so first-match-wins gives specific paths first."""
    sorted_pairs = sorted(
        container_label_pairs, key=lambda pair: _container_priority(pair[1])
    )

    sub_routes: List[Dict[str, Any]] = []
    for container, labels in sorted_pairs:
        target = _container_target(container, labels)
        paths = parse_path_label(labels.get("caddy.proxy.path"))
        sub_match: List[Dict[str, Any]] = []
        if paths:
            sub_match.append({"path": paths})
        sub_routes.append({
            "match": sub_match,
            "handle": [_reverse_proxy_handle(target, _health_check_block(labels, domain))],
        })

    return {
        "match": [{"host": [domain]}],
        "handle": [{"handler": "subroute", "routes": sub_routes}],
    }


def build_routes_from_containers(
    containers: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Translate Caddy-labeled containers into Caddy HTTP routes.

    Containers without a ``caddy.proxy`` label are ignored. Containers that
    share a domain are merged into a ``subroute`` so path matchers don't
    collide. Returns the routes in the order they should appear in
    ``apps.http.servers.*.routes`` — path-specific cases ordered before
    catch-alls so first-match-wins serves the right backend.
    """
    grouped: Dict[str, List[Tuple[Dict[str, Any], Dict[str, str]]]] = {}
    domain_order: List[str] = []
    for container in containers:
        labels = container.get("Labels") or {}
        domain = labels.get("caddy.proxy")
        if not domain:
            continue
        if domain not in grouped:
            grouped[domain] = []
            domain_order.append(domain)
        grouped[domain].append((container, labels))

    routes: List[Dict[str, Any]] = []
    for domain in domain_order:
        pairs = grouped[domain]
        if len(pairs) == 1:
            container, labels = pairs[0]
            routes.append(_route_for_single_container(domain, container, labels))
        else:
            routes.append(_route_for_multi_container(domain, pairs))
    return routes
