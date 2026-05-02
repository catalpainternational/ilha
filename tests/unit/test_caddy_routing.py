"""Tests for the shared Caddy route generator.

Both ``caddy-docker-monitor.py`` (long-running daemon) and
``caddy-dynamic-config.py`` (one-shot) consume this module. The tests here
cover the matrix of label combinations they each used to handle independently.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[2] / "ilha" / "scripts" / "caddy_routing.py"
    spec = importlib.util.spec_from_file_location("caddy_routing", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def routing():
    return _load_module()


def _container(name: str, **labels) -> dict:
    return {"Names": name, "Labels": labels}


# ── parse_path_label ──────────────────────────────────────────────────────


def test_parse_path_label_empty_returns_empty_list(routing):
    assert routing.parse_path_label("") == []
    assert routing.parse_path_label(None) == []


def test_parse_path_label_root_sentinel_returns_empty_list(routing):
    """``/`` is the legacy "catch-all" sentinel — treat as no path constraint."""
    assert routing.parse_path_label("/") == []


def test_parse_path_label_splits_on_commas_and_strips(routing):
    """Regression: previously the comma-string was wrapped in a single-element
    list, producing a literal-string match Caddy never satisfied."""
    assert routing.parse_path_label("/api/*,/admin,/admin/*") == [
        "/api/*",
        "/admin",
        "/admin/*",
    ]
    assert routing.parse_path_label("/api/*, /admin , /admin/*") == [
        "/api/*",
        "/admin",
        "/admin/*",
    ]


def test_parse_path_label_single_path_no_commas(routing):
    assert routing.parse_path_label("/api/*") == ["/api/*"]


# ── single-container per domain ───────────────────────────────────────────


def test_single_container_no_path_emits_flat_route(routing):
    routes = routing.build_routes_from_containers(
        [_container("svc", **{"caddy.proxy": "example.com"})]
    )
    assert len(routes) == 1
    assert routes[0]["match"] == [{"host": ["example.com"]}]
    assert routes[0]["handle"][0]["handler"] == "reverse_proxy"
    assert routes[0]["handle"][0]["upstreams"] == [{"dial": "svc:8000"}]


def test_single_container_with_path_emits_path_match(routing):
    """Even with one container, a path label restricts the route."""
    routes = routing.build_routes_from_containers([
        _container(
            "api",
            **{
                "caddy.proxy": "example.com",
                "caddy.proxy.reverse_proxy": "api:9000",
                "caddy.proxy.path": "/v1/*,/v2/*",
            },
        )
    ])
    assert routes[0]["match"] == [{"host": ["example.com"], "path": ["/v1/*", "/v2/*"]}]
    assert routes[0]["handle"][0]["upstreams"] == [{"dial": "api:9000"}]


def test_explicit_reverse_proxy_target_overrides_default(routing):
    routes = routing.build_routes_from_containers([
        _container(
            "alias",
            **{"caddy.proxy": "example.com", "caddy.proxy.reverse_proxy": "elsewhere:1234"},
        )
    ])
    assert routes[0]["handle"][0]["upstreams"] == [{"dial": "elsewhere:1234"}]


def test_health_check_label_emits_active_health_check(routing):
    routes = routing.build_routes_from_containers([
        _container(
            "svc",
            **{"caddy.proxy": "example.com", "caddy.proxy.health_check": "/healthz"},
        )
    ])
    hc = routes[0]["handle"][0]["health_checks"]
    assert hc["active"]["path"] == "/healthz"
    assert hc["active"]["headers"]["Host"] == ["example.com"]


# ── multiple containers, same domain (the django_spa shape) ───────────────


def test_two_services_same_domain_uses_subroute_and_orders_paths_first(routing):
    """API paths must precede the catch-all so Caddy's first-match-wins
    delivers backend for /api/* and frontend for everything else."""
    routes = routing.build_routes_from_containers([
        # Frontend catch-all (no path) declared first to verify ordering.
        _container(
            "frontend",
            **{"caddy.proxy": "pivot.io", "caddy.proxy.reverse_proxy": "frontend:5173"},
        ),
        _container(
            "web",
            **{
                "caddy.proxy": "pivot.io",
                "caddy.proxy.reverse_proxy": "web:8000",
                "caddy.proxy.path": "/api/*,/admin/*",
            },
        ),
    ])
    assert len(routes) == 1
    route = routes[0]
    assert route["match"] == [{"host": ["pivot.io"]}]
    sub = route["handle"][0]
    assert sub["handler"] == "subroute"
    sub_routes = sub["routes"]
    # Path-specific subroute must be FIRST.
    assert sub_routes[0]["match"] == [{"path": ["/api/*", "/admin/*"]}]
    assert sub_routes[0]["handle"][0]["upstreams"] == [{"dial": "web:8000"}]
    # Catch-all subroute (no match) must be LAST.
    assert sub_routes[-1]["match"] == []
    assert sub_routes[-1]["handle"][0]["upstreams"] == [{"dial": "frontend:5173"}]


def test_two_services_both_with_paths_kept_in_priority_order(routing):
    """Both have path constraints; longer total path-string sorts first
    (longest-prefix-wins heuristic for sibling specific routes)."""
    routes = routing.build_routes_from_containers([
        _container(
            "short",
            **{
                "caddy.proxy": "ex.com",
                "caddy.proxy.path": "/a/*",
                "caddy.proxy.reverse_proxy": "short:1",
            },
        ),
        _container(
            "long",
            **{
                "caddy.proxy": "ex.com",
                "caddy.proxy.path": "/abc/foo/*,/abc/bar/*",
                "caddy.proxy.reverse_proxy": "long:2",
            },
        ),
    ])
    sub = routes[0]["handle"][0]
    sub_routes = sub["routes"]
    assert sub_routes[0]["handle"][0]["upstreams"] == [{"dial": "long:2"}]
    assert sub_routes[1]["handle"][0]["upstreams"] == [{"dial": "short:1"}]


def test_multiple_domains_emit_separate_top_level_routes(routing):
    routes = routing.build_routes_from_containers([
        _container("a", **{"caddy.proxy": "a.example.com"}),
        _container("b", **{"caddy.proxy": "b.example.com"}),
    ])
    hosts = sorted(r["match"][0]["host"][0] for r in routes)
    assert hosts == ["a.example.com", "b.example.com"]


def test_containers_without_caddy_proxy_label_ignored(routing):
    routes = routing.build_routes_from_containers([
        _container("db", image="postgres:16"),
        _container("web", **{"caddy.proxy": "ex.com"}),
    ])
    assert len(routes) == 1
    assert routes[0]["match"][0]["host"] == ["ex.com"]


def test_single_container_root_path_treated_as_catch_all(routing):
    """Sentinel ``/`` must not become a literal path matcher."""
    routes = routing.build_routes_from_containers([
        _container(
            "svc",
            **{"caddy.proxy": "ex.com", "caddy.proxy.path": "/"},
        )
    ])
    assert routes[0]["match"] == [{"host": ["ex.com"]}]  # no "path" key
