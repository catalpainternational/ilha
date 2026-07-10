"""Tests for the compose overlay generator."""

import yaml

from ilha.utils.overlay_generator import (
    detect_conflicting_services,
    generate_overlay,
)


def _gen(user_compose: dict, *, domain=None, ip=None):
    return generate_overlay(user_compose, domain=domain, ip=ip)


def test_manifest_routes_nonstandard_service_name():
    """The headline manifest win: a service the heuristics can't see (name not in
    the allowlist, custom port) still routes when the manifest declares it."""
    user = {"services": {"server": {"image": "myco/server"}}}

    # Heuristics alone: 'server' is invisible → nothing routable.
    heuristic = generate_overlay(user)
    assert "labels" not in heuristic["services"]["server"]
    assert "networks" not in heuristic  # no routable service

    # Manifest authoritative: 'server' routes on its declared port 3000.
    manifest = generate_overlay(user, routable={"server": 3000})
    svc = manifest["services"]["server"]
    assert svc["labels"]["caddy.proxy.reverse_proxy"].endswith("-server:3000")
    assert svc["expose"] == ["3000"]
    assert manifest["networks"] == {"ilha_caddy_proxy": {"external": True}}


def test_manifest_overrides_heuristic_routability():
    """A manifest that omits a conventionally-named service makes it non-routable."""
    user = {"services": {"app": {"expose": ["8000"]}, "web": {"expose": ["9000"]}}}
    # Only 'app' is in the manifest; 'web' (a heuristic match) is intentionally out.
    overlay = generate_overlay(user, routable={"app": 8000})
    assert "labels" in overlay["services"]["app"]
    assert "labels" not in overlay["services"]["web"]


def test_django_postgres_canonical():
    """A typical Django+Postgres project: web is routable, db is not."""
    user = {
        "services": {
            "web": {"image": "django-app", "ports": ["8000:8000"]},
            "db": {"image": "postgres:16"},
        },
        "volumes": {"postgres_data": None},
    }
    overlay = _gen(user)
    services = overlay["services"]

    assert services["web"]["container_name"] == "${COMPOSE_PROJECT_NAME}-web"
    assert services["web"]["ports"] == []
    assert services["web"]["expose"] == ["8000"]
    assert services["web"]["labels"]["caddy.proxy"] == "${SITE_DOMAIN:-${COMPOSE_PROJECT_NAME}.localhost}"
    assert services["web"]["labels"]["caddy.proxy.reverse_proxy"] == "${COMPOSE_PROJECT_NAME}-web:8000"
    assert services["web"]["networks"] == {"default": None, "ilha_caddy_proxy": None}
    assert services["web"]["env_file"] == [
        "${PROJECT_ROOT}/.env",
        "${PROJECT_ROOT}/.ilha/env.ilha",
    ]

    # db: container_name + env_file only; no ports/labels/networks override
    assert services["db"]["container_name"] == "${COMPOSE_PROJECT_NAME}-db"
    assert "ports" not in services["db"]
    assert "labels" not in services["db"]
    assert "networks" not in services["db"]

    # Top-level external network only emitted when at least one routable service
    assert overlay["networks"]["ilha_caddy_proxy"] == {"external": True}
    assert overlay["volumes"]["postgres_data"]["name"] == "${COMPOSE_PROJECT_NAME}_postgres_data"


def test_routable_ports_dump_as_compose_reset_tag():
    """Routable services reset inherited host ports in Docker Compose merges."""
    user = {"services": {"web": {"image": "django-app", "ports": ["8000:8000"]}}}

    dumped = yaml.dump(_gen(user), default_flow_style=False, sort_keys=False)

    assert "ports: !reset []" in dumped
    assert yaml.safe_load(dumped)["services"]["web"]["ports"] == []


def test_named_volumes_are_scoped_to_worktree_project():
    """Top-level named volumes are renamed to the worktree compose project."""
    user = {
        "services": {"web": {"image": "django-app"}},
        "volumes": {
            "postgres_data": {"name": "business_intelligence_postgres_data"},
            "redis_data": None,
            "shared_cache": {"external": True},
        },
    }

    overlay = _gen(user)

    assert overlay["volumes"]["postgres_data"]["name"] == "${COMPOSE_PROJECT_NAME}_postgres_data"
    assert overlay["volumes"]["redis_data"]["name"] == "${COMPOSE_PROJECT_NAME}_redis_data"
    assert "shared_cache" not in overlay["volumes"]


def test_static_only_caddy_project():
    """Single caddy:2-alpine service IS the app and gets routable treatment."""
    user = {"services": {"caddy": {"image": "caddy:2-alpine", "ports": ["8080:80"]}}}
    overlay = _gen(user)
    caddy = overlay["services"]["caddy"]

    assert caddy["expose"] == ["80"]
    assert caddy["labels"]["caddy.proxy.reverse_proxy"] == "${COMPOSE_PROJECT_NAME}-caddy:80"
    assert overlay["networks"]["ilha_caddy_proxy"] == {"external": True}


def test_non_routable_only_project_no_top_level_networks():
    """A project with only non-routable services (workers/etc) doesn't get the
    top-level networks declaration."""
    user = {"services": {"worker": {"image": "myproject/worker"}}}
    overlay = _gen(user)

    assert "networks" not in overlay
    assert overlay["services"]["worker"]["container_name"] == "${COMPOSE_PROJECT_NAME}-worker"
    assert "labels" not in overlay["services"]["worker"]


def test_existing_caddy_labels_make_service_routable():
    """A non-standard service name with caddy.proxy labels gets routable treatment."""
    user = {
        "services": {
            "myservice": {
                "image": "custom",
                "labels": ["caddy.proxy.reverse_proxy=myservice:9000"],
            }
        }
    }
    overlay = _gen(user)
    assert overlay["services"]["myservice"]["expose"] == ["8000"]
    # Overlay sets canonical Caddy labels (mapping form so it deep-merges with
    # whatever the user has in dict-form)
    assert overlay["services"]["myservice"]["labels"]["caddy.proxy.reverse_proxy"] == (
        "${COMPOSE_PROJECT_NAME}-myservice:9000"
    ) or overlay["services"]["myservice"]["labels"]["caddy.proxy.reverse_proxy"] == (
        "${COMPOSE_PROJECT_NAME}-myservice:8000"
    )


def test_domain_override_replaces_proxy_label():
    user = {"services": {"web": {"image": "django-app"}}}
    overlay = _gen(user, domain="staging.example.com")
    assert overlay["services"]["web"]["labels"]["caddy.proxy"] == "staging.example.com"


def test_ip_override_replaces_proxy_label():
    user = {"services": {"web": {"image": "django-app"}}}
    overlay = _gen(user, ip="203.0.113.10")
    assert overlay["services"]["web"]["labels"]["caddy.proxy"] == "203.0.113.10"


def test_user_compose_is_not_mutated():
    """Charter: the overlay generator NEVER mutates the user's parsed compose."""
    user = {
        "services": {
            "web": {"image": "django-app", "ports": ["8000:8000"], "environment": {"DEBUG": "True"}},
        }
    }
    import copy
    snapshot = copy.deepcopy(user)
    _gen(user)
    assert user == snapshot


def test_multiple_routable_services():
    """Multiple WEB_SERVICE_NAMES services all become routable."""
    user = {
        "services": {
            "web": {"image": "web-app", "expose": ["3000"]},
            "api": {"image": "api-app", "expose": ["5000"]},
            "redis": {"image": "redis:7"},
        }
    }
    overlay = _gen(user)
    assert overlay["services"]["web"]["expose"] == ["3000"]
    assert overlay["services"]["api"]["expose"] == ["5000"]
    # redis (non-routable) doesn't get expose/labels
    assert "labels" not in overlay["services"]["redis"]


def test_detect_conflicting_services_caddy_alone_is_fine():
    """Static-only project (single caddy service) is not a conflict."""
    user = {"services": {"caddy": {"image": "caddy:2-alpine"}}}
    assert detect_conflicting_services(user) == []


def test_detect_conflicting_services_caddy_with_app():
    """`caddy` alongside other app services IS a conflict."""
    user = {"services": {"web": {"image": "django"}, "caddy": {"image": "caddy:2-alpine"}}}
    assert detect_conflicting_services(user) == ["caddy"]


def test_detect_conflicting_services_caddy_monitor():
    user = {"services": {"web": {"image": "django"}, "caddy-monitor": {"image": "anything"}}}
    conflicts = detect_conflicting_services(user)
    assert "caddy-monitor" in conflicts


def test_caddy_monitor_excluded_from_overlay():
    """ilha-infrastructure services in the user's compose are skipped in the overlay."""
    user = {"services": {"web": {"image": "django"}, "caddy-monitor": {"image": "anything"}}}
    overlay = _gen(user)
    assert "web" in overlay["services"]
    assert "caddy-monitor" not in overlay["services"]
