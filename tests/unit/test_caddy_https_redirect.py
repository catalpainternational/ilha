"""Regression tests for Caddy HTTP to HTTPS redirects on domain deploys."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from ilha.scripts.caddy_admin_utils import build_http_to_https_redirect_route


def _load_monitor_class():
    path = Path(__file__).resolve().parents[2] / "ilha" / "scripts" / "caddy-docker-monitor.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("caddy_docker_monitor", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CaddyDockerMonitor


def test_redirect_route_matches_http_only_and_preserves_uri():
    route = build_http_to_https_redirect_route(["demo.example.com"])

    assert route["match"] == [{"host": ["demo.example.com"], "protocol": "http"}]
    assert route["handle"][0]["status_code"] == 308
    assert route["handle"][0]["headers"]["Location"] == [
        "https://{http.request.host}{http.request.uri}"
    ]


def test_monitor_inserts_redirect_before_reverse_proxy_for_domains():
    Monitor = _load_monitor_class()
    monitor = Monitor.__new__(Monitor)

    config = monitor.create_route_config(
        [
            {
                "Names": "demo-web",
                "Labels": {
                    "caddy.proxy": "demo.example.com",
                    "caddy.proxy.reverse_proxy": "demo-web:8000",
                },
            }
        ]
    )
    routes = config["apps"]["http"]["servers"]["srv0"]["routes"]

    assert routes[0]["handle"][0]["handler"] == "static_response"
    assert routes[0]["match"][0]["protocol"] == "http"
    assert routes[1]["handle"][0]["handler"] == "reverse_proxy"


def test_monitor_does_not_redirect_ip_only_routes():
    Monitor = _load_monitor_class()
    monitor = Monitor.__new__(Monitor)

    config = monitor.create_route_config(
        [
            {
                "Names": "demo-web",
                "Labels": {
                    "caddy.proxy": "203.0.113.10",
                    "caddy.proxy.reverse_proxy": "demo-web:8000",
                },
            }
        ]
    )
    routes = config["apps"]["http"]["servers"]["srv0"]["routes"]

    assert routes[0]["handle"][0]["handler"] == "reverse_proxy"
    assert config["apps"]["http"]["servers"]["srv0"]["automatic_https"] == {
        "disable_redirects": True
    }
