"""Tests for preflight warnings that catch project-side traps before deploy.

These warnings exist so users don't sit through a full deploy + smoke before
hitting a fixable misconfiguration (e.g. Django's static admin 404 trap).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ilha.commands.push import deploy_preflight as preflight_module
from ilha.commands.push.deploy_preflight import DeployPreflight
from ilha.utils.project_footprint import ProjectArchetype, ProjectFootprint


def _footprint(has_django: bool, backend: str = "web") -> ProjectFootprint:
    return ProjectFootprint(
        has_django=has_django,
        has_postgres=True,
        has_static_service=False,
        archetype=ProjectArchetype.SOLO_DJANGO if has_django else ProjectArchetype.UNKNOWN,
        backend_service=backend if has_django else None,
    )


def _write_compose(tmp_path: Path, web_command: str, service_name: str = "web") -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        f"  {service_name}:\n"
        "    build: .\n"
        f"    command: {web_command}\n"
    )


@pytest.fixture
def captured_warnings(monkeypatch):
    """Capture ``log_warning`` calls (the rich-console helper bypasses
    Python's logging, so caplog won't see them)."""
    bucket: list[str] = []
    monkeypatch.setattr(preflight_module, "log_warning", bucket.append)
    return bucket


def test_warns_when_django_command_missing_collectstatic(tmp_path: Path, captured_warnings):
    _write_compose(tmp_path, 'sh -c "python manage.py migrate && daphne config.asgi:application"')
    pre = DeployPreflight(project_root=tmp_path)
    pre._warn_if_django_missing_collectstatic(tmp_path, _footprint(has_django=True))
    joined = " ".join(captured_warnings)
    assert "collectstatic" in joined
    assert "/static/admin" in joined


def test_no_warning_when_collectstatic_present(tmp_path: Path, captured_warnings):
    _write_compose(
        tmp_path,
        'sh -c "python manage.py migrate && python manage.py collectstatic --noinput && daphne config.asgi:application"',
    )
    pre = DeployPreflight(project_root=tmp_path)
    pre._warn_if_django_missing_collectstatic(tmp_path, _footprint(has_django=True))
    assert not any("collectstatic" in m for m in captured_warnings)


def test_no_warning_when_not_django(tmp_path: Path, captured_warnings):
    _write_compose(tmp_path, "caddy run")
    pre = DeployPreflight(project_root=tmp_path)
    pre._warn_if_django_missing_collectstatic(tmp_path, _footprint(has_django=False))
    assert captured_warnings == []


def test_silent_when_no_command_in_compose(tmp_path: Path, captured_warnings):
    """Image-defined entrypoints can't be inspected; staying quiet beats
    false-positive nag."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    image: my-prebuilt:latest\n"
    )
    pre = DeployPreflight(project_root=tmp_path)
    pre._warn_if_django_missing_collectstatic(tmp_path, _footprint(has_django=True))
    assert captured_warnings == []


def test_warns_for_custom_backend_service_name(tmp_path: Path, captured_warnings):
    """Backend service inferred as ``api`` (not ``web``) — warning still fires."""
    _write_compose(
        tmp_path,
        'sh -c "daphne config.asgi:application"',
        service_name="api",
    )
    pre = DeployPreflight(project_root=tmp_path)
    pre._warn_if_django_missing_collectstatic(
        tmp_path, _footprint(has_django=True, backend="api")
    )
    assert any("'api'" in m and "collectstatic" in m for m in captured_warnings)
