"""Focused regression tests for the setup command (overlay model)."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from ilha.commands.setup import SetupManager


@pytest.fixture
def temp_project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def setup_manager(temp_project):
    return SetupManager(project_root=temp_project)


@pytest.fixture(autouse=True)
def mock_prerequisites():
    with patch("ilha.commands.setup.check_prerequisites", return_value=True):
        yield


@pytest.fixture
def mock_prompts():
    with patch(
        "ilha.utils.file_utils.prompt_compose_file_choice",
        side_effect=lambda files: files[0],
    ), patch("ilha.utils.file_utils.prompt_user_input", return_value="1"):
        yield


def write_compose(root: Path, data: dict):
    compose = root / "docker-compose.yml"
    with compose.open("w") as handle:
        yaml.safe_dump(data, handle)
    return compose


def test_setup_fails_when_missing_compose(setup_manager, mock_prompts):
    """Per the overlay charter, ilha requires the user to supply docker-compose.yml."""
    assert not (setup_manager.project_root / "docker-compose.yml").exists()

    ok = setup_manager.setup_project(non_interactive=True)

    assert ok is False
    assert not (setup_manager.project_root / "docker-compose.yml").exists()


def test_setup_writes_overlay_for_existing_compose(setup_manager, mock_prompts):
    compose_data = {
        "services": {
            "web": {"image": "nginx:alpine", "ports": ["8000:80"]},
            "db": {"image": "postgres:14", "volumes": ["pgdata:/var/lib/postgresql/data"]},
        },
        "volumes": {"pgdata": {}},
    }
    write_compose(setup_manager.project_root, compose_data)

    ok = setup_manager.setup_project(non_interactive=True)

    assert ok is True
    overlay_path = setup_manager.ilha_dir / "docker-compose.overlay.yml"
    assert overlay_path.exists()
    overlay = yaml.safe_load(overlay_path.read_text())
    assert "web" in overlay["services"]
    assert overlay["services"]["web"]["container_name"] == "${COMPOSE_PROJECT_NAME}-web"
    # Routable service: ports replaced with [], expose declared, Caddy labels set
    assert overlay["services"]["web"]["ports"] == []
    assert overlay["services"]["web"]["expose"] == ["80"]
    labels = overlay["services"]["web"]["labels"]
    assert "caddy.proxy" in labels
    assert "caddy.proxy.reverse_proxy" in labels
    # Non-routable service: container_name + env_file, no ports/labels
    assert overlay["services"]["db"]["container_name"] == "${COMPOSE_PROJECT_NAME}-db"
    assert "ports" not in overlay["services"]["db"]
    assert "labels" not in overlay["services"]["db"]
    # Top-level networks
    assert overlay["networks"]["ilha_caddy_proxy"]["external"] is True


def test_setup_does_not_modify_users_compose(setup_manager, mock_prompts):
    """Charter: ilha never edits the user's docker-compose.yml."""
    compose_data = {
        "services": {
            "web": {"image": "django-app", "ports": ["8000:8000"], "environment": {"DEBUG": "True"}},
        },
    }
    write_compose(setup_manager.project_root, compose_data)
    before = (setup_manager.project_root / "docker-compose.yml").read_text()

    ok = setup_manager.setup_project(non_interactive=True)
    assert ok is True

    after = (setup_manager.project_root / "docker-compose.yml").read_text()
    assert before == after, "docker-compose.yml must be byte-identical after setup"


def test_setup_handles_invalid_compose_gracefully(setup_manager, mock_prompts):
    compose = setup_manager.project_root / "docker-compose.yml"
    compose.write_text("invalid: [")

    ok = setup_manager.setup_project(non_interactive=True)

    assert ok is False


def test_setup_static_only_caddy_project(setup_manager, mock_prompts):
    """Static site (single caddy:2-alpine service) gets routable overlay treatment."""
    compose_data = {
        "services": {
            "caddy": {
                "image": "caddy:2-alpine",
                "ports": ["8080:80"],
                "volumes": [
                    "./public:/usr/share/caddy:ro",
                    "./Caddyfile:/etc/caddy/Caddyfile:ro",
                ],
            }
        }
    }
    write_compose(setup_manager.project_root, compose_data)

    ok = setup_manager.setup_project(non_interactive=True)

    assert ok is True
    overlay = yaml.safe_load(
        (setup_manager.ilha_dir / "docker-compose.overlay.yml").read_text()
    )
    caddy = overlay["services"]["caddy"]
    assert caddy["container_name"] == "${COMPOSE_PROJECT_NAME}-caddy"
    assert caddy["expose"] == ["80"]
    assert "caddy.proxy" in caddy["labels"]
    assert caddy["labels"]["caddy.proxy.reverse_proxy"].endswith(":80")


def test_setup_warns_when_caddy_alongside_other_services(setup_manager, mock_prompts):
    """A `caddy` service alongside other app services is flagged via warning.

    Charter: ilha doesn't edit user files. The user's `caddy` service still
    appears in the overlay (so the overall project at least parses), but
    setup emits a loud warning telling the user to remove it.
    """
    from ilha.utils.overlay_generator import detect_conflicting_services
    compose_data = {
        "services": {
            "web": {"image": "django-app", "ports": ["8000:8000"]},
            "caddy": {"image": "caddy:2-alpine", "ports": ["80:80"]},
        }
    }
    assert detect_conflicting_services(compose_data) == ["caddy"]

    write_compose(setup_manager.project_root, compose_data)

    ok = setup_manager.setup_project(non_interactive=True)
    assert ok is True

    # Both services are in the overlay; ilha didn't filter the user's file.
    overlay = yaml.safe_load(
        (setup_manager.ilha_dir / "docker-compose.overlay.yml").read_text()
    )
    assert "web" in overlay["services"]
    assert "caddy" in overlay["services"]


def test_detect_docker_compose_prefers_yml(setup_manager, mock_prompts):
    write_compose(setup_manager.project_root, {"version": "3", "services": {}})
    alt = setup_manager.project_root / "docker-compose.yaml"
    alt.write_text("version: '3.8'")

    detected = setup_manager.detect_docker_compose()

    assert detected.name == "docker-compose.yml"


def test_setup_refreshes_overlay_from_edited_manifest(setup_manager, mock_prompts):
    """A manifest edit takes effect on a non-interactive re-run.

    Regression for the integration gap where re-running setup non-interactively
    bailed out on an existing .ilha and never regenerated the overlay — so a
    hand-declared web service never routed.
    """
    from ilha.config.manifest import load_manifest, write_manifest, WebService

    # 'server' is not a conventional web-service name → nothing routable at first.
    write_compose(
        setup_manager.project_root,
        {"services": {"server": {"image": "myco/server", "expose": ["3000"]}}},
    )
    assert setup_manager.setup_project(non_interactive=True) is True
    assert load_manifest(setup_manager.project_root).web == []

    # User declares it in the manifest, then re-runs setup (non-interactive).
    m = load_manifest(setup_manager.project_root)
    m.web = [WebService(service="server", port=3000)]
    write_manifest(setup_manager.project_root, m)
    assert setup_manager.setup_project(non_interactive=True) is True

    # The overlay now routes server:3000, and the manifest edit is preserved.
    overlay = yaml.safe_load(
        (setup_manager.ilha_dir / "docker-compose.overlay.yml").read_text()
    )
    labels = overlay["services"]["server"]["labels"]
    assert labels["caddy.proxy.reverse_proxy"].endswith("-server:3000")
    assert load_manifest(setup_manager.project_root).web[0].service == "server"
