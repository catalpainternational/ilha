"""Tests for static-site inference in DeployServiceConfig.

Covers the case where a Caddy/Nginx-only project (no Django footprint, no
Postgres) gets sensible deploy defaults without an explicit ``deploy:`` block,
and the case where an explicit block still wins.
"""

from pathlib import Path

import pytest
import yaml

from ilha.commands.push.deploy_service_config import (
    DeployServiceConfig,
    load_deploy_service_config,
)


def _write_compose(root: Path, body: str) -> None:
    (root / ".ilha").mkdir(parents=True, exist_ok=True)
    (root / ".ilha" / "docker-compose.overlay.yml").write_text(body)


def _write_config(root: Path, deploy_block: dict) -> None:
    (root / ".ilha").mkdir(parents=True, exist_ok=True)
    (root / ".ilha" / "config.yml").write_text(yaml.safe_dump({"deploy": deploy_block}))


def test_django_default_unchanged_for_django_project(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("")
    _write_compose(
        tmp_path,
        (
            "services:\n"
            "  web:\n    image: django-app\n"
            "  db:\n    image: postgres:16\n"
        ),
    )

    cfg = load_deploy_service_config(tmp_path)

    assert cfg.web_service == "web"
    assert cfg.web_port == 8000
    assert cfg.health_path == "/health-check/"
    assert cfg.db_service == "db"
    assert "db" in cfg.dependency_services


def test_static_site_defaults_inferred(tmp_path: Path) -> None:
    """No Django, no Postgres, only a Caddy service → static defaults."""
    _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:2-alpine\n    ports: ['8080:80']\n",
    )

    cfg = load_deploy_service_config(tmp_path)

    assert cfg.web_service == "caddy"
    assert cfg.web_port == 80
    assert cfg.health_path == "/"
    assert cfg.dependency_services == []
    assert cfg.db_service == ""
    assert cfg.worker_services == []


def test_explicit_deploy_block_overrides_static_inference(tmp_path: Path) -> None:
    _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:2-alpine\n    ports: ['8080:80']\n",
    )
    _write_config(
        tmp_path,
        {
            "web_service": "edge",
            "web_port": 8081,
            "health_path": "healthz",  # missing leading slash, should be normalized
        },
    )

    cfg = load_deploy_service_config(tmp_path)

    assert cfg.web_service == "edge"
    assert cfg.web_port == 8081
    assert cfg.health_path == "/healthz"
    # Static base still applies for keys not overridden:
    assert cfg.dependency_services == []
    assert cfg.db_service == ""


def test_partial_deploy_block_merges_with_static_base(tmp_path: Path) -> None:
    """Only ``web_port`` overridden — other keys keep their static defaults."""
    _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:2-alpine\n",
    )
    _write_config(tmp_path, {"web_port": 8888})

    cfg = load_deploy_service_config(tmp_path)

    assert cfg.web_service == "caddy"
    assert cfg.web_port == 8888
    assert cfg.health_path == "/"
    assert cfg.db_service == ""


def test_empty_dependency_services_list_honored(tmp_path: Path) -> None:
    """Explicit ``dependency_services: []`` should stick."""
    (tmp_path / "manage.py").write_text("")
    _write_compose(
        tmp_path,
        "services:\n  web:\n    image: django-app\n  db:\n    image: postgres:16\n",
    )
    _write_config(tmp_path, {"dependency_services": []})

    cfg = load_deploy_service_config(tmp_path)

    assert cfg.dependency_services == []


def test_db_service_can_be_explicitly_emptied(tmp_path: Path) -> None:
    """A Django project that doesn't ship a Postgres dep can opt out."""
    (tmp_path / "manage.py").write_text("")
    _write_compose(tmp_path, "services:\n  web:\n    image: django-app\n")
    _write_config(tmp_path, {"db_service": ""})

    cfg = load_deploy_service_config(tmp_path)

    assert cfg.db_service == ""


def test_invalid_web_port_falls_back_to_base_default(tmp_path: Path) -> None:
    _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:2-alpine\n")
    _write_config(tmp_path, {"web_port": "not-a-port"})

    cfg = load_deploy_service_config(tmp_path)

    # Static base port wins because the override was invalid.
    assert cfg.web_port == 80


def test_migrate_command_inferred_for_django(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("")
    _write_compose(tmp_path, "services:\n  web:\n    image: django-app\n  db:\n    image: postgres:16\n")
    cfg = load_deploy_service_config(tmp_path)
    assert cfg.resolved_migrate_command(tmp_path) == "python manage.py migrate --noinput"


def test_migrate_command_empty_disables(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("")
    _write_compose(tmp_path, "services:\n  web:\n    image: django-app\n  db:\n    image: postgres:16\n")
    _write_config(tmp_path, {"migrate_command": ""})
    cfg = load_deploy_service_config(tmp_path)
    assert cfg.resolved_migrate_command(tmp_path) == ""
