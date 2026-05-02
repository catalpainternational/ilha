"""Tests for compose external volume name resolution."""

from __future__ import annotations

from pathlib import Path

from ilha.utils.compose_external_volumes import (
    compose_service_is_buildable,
    compose_web_image_reference,
    expand_compose_vars,
    list_external_volume_host_names,
)


def test_expand_compose_vars_simple():
    assert expand_compose_vars("pre-${COMPOSE_PROJECT_NAME}-suf", {"COMPOSE_PROJECT_NAME": "demo"}) == "pre-demo-suf"


def test_expand_compose_vars_default():
    assert (
        expand_compose_vars("${MISSING:-fallback}", {"OTHER": "x"}) == "fallback"
    )


def test_list_external_volume_host_names(tmp_path: Path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
volumes:
  logs_volume:
    external: true
    name: ${COMPOSE_PROJECT_NAME}_logs_volume
  other:
    external: false
""",
        encoding="utf-8",
    )
    env = {"COMPOSE_PROJECT_NAME": "myproj-branch"}
    names = list_external_volume_host_names(compose, env)
    assert names == ["myproj-branch_logs_volume"]


def test_compose_web_image_reference_explicit(tmp_path: Path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services:
  web:
    image: myregistry/web:1.2.3
""",
        encoding="utf-8",
    )
    ref = compose_web_image_reference(compose, {}, "web")
    assert ref == "myregistry/web:1.2.3"


def test_compose_web_image_reference_default_from_project(tmp_path: Path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services:
  web:
    build: .
""",
        encoding="utf-8",
    )
    ref = compose_web_image_reference(
        compose, {"COMPOSE_PROJECT_NAME": "business-intelligence-staging"}, "web"
    )
    assert ref == "business-intelligence-staging_web:latest"


def test_compose_web_image_reference_missing_service_returns_empty(tmp_path: Path):
    """Do not infer {project}_{service}:latest when the service key is absent."""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services: {}
""",
        encoding="utf-8",
    )
    ref = compose_web_image_reference(
        compose, {"COMPOSE_PROJECT_NAME": "static-site-master"}, "caddy"
    )
    assert ref == ""


def test_compose_service_is_buildable_true_for_build_directive(tmp_path: Path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services:
  web:
    build: .
    image: my-app:dev
""",
        encoding="utf-8",
    )
    assert compose_service_is_buildable(compose, {}, "web") is True


def test_compose_service_is_buildable_false_for_pull_only_image(tmp_path: Path):
    """A pull-only public image (no `build:`) is not buildable; skip remote build phase."""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services:
  caddy:
    image: caddy:2-alpine
""",
        encoding="utf-8",
    )
    assert compose_service_is_buildable(compose, {}, "caddy") is False


def test_compose_service_is_buildable_false_for_missing_service(tmp_path: Path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    assert compose_service_is_buildable(compose, {}, "web") is False
