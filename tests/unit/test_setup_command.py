"""Focused regression tests for the refactored setup command."""

from pathlib import Path
from unittest.mock import Mock, patch

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
    with patch("ilha.utils.file_utils.prompt_compose_file_choice", side_effect=lambda files: files[0]), patch(
        "ilha.utils.file_utils.prompt_user_input", return_value="1"
    ):
        yield


def write_compose(root: Path, data: dict):
    compose = root / "docker-compose.yml"
    with compose.open("w") as handle:
        yaml.safe_dump(data, handle)
    return compose


def test_setup_creates_minimal_scaffold_when_missing_compose(setup_manager, mock_prompts):
    assert not (setup_manager.project_root / "docker-compose.yml").exists()

    ok = setup_manager.setup_project(non_interactive=True)

    assert ok is True
    assert (setup_manager.project_root / "docker-compose.yml").exists()
    assert (setup_manager.ilha_dir / "docker-compose.worktree.yml").exists()
    assert (setup_manager.ilha_dir / "config.yml").exists()


def test_setup_transforms_existing_compose_file(setup_manager, mock_prompts):
    compose_data = {
        "version": "3.9",
        "services": {
            "web": {"image": "nginx:alpine", "ports": ["8000:80"]},
            "db": {"image": "postgres:14", "volumes": ["pgdata:/var/lib/postgresql/data"]},
        },
        "volumes": {"pgdata": {}},
    }
    write_compose(setup_manager.project_root, compose_data)

    ok = setup_manager.setup_project(non_interactive=True)

    assert ok is True
    worktree_compose = yaml.safe_load((setup_manager.ilha_dir / "docker-compose.worktree.yml").read_text())
    assert "web" in worktree_compose["services"]
    assert "expose" in worktree_compose["services"]["web"]
    config = yaml.safe_load((setup_manager.ilha_dir / "config.yml").read_text())
    assert "services" in config and "web" in config["services"]


def test_setup_handles_invalid_compose_gracefully(setup_manager, mock_prompts):
    compose = setup_manager.project_root / "docker-compose.yml"
    compose.write_text("invalid: [")

    ok = setup_manager.setup_project(non_interactive=True)

    assert ok is False


def test_setup_status_reports_completion(setup_manager, mock_prompts):
    setup_manager.setup_project(non_interactive=True)

    status = setup_manager.get_setup_status()

    assert status["is_complete"] is True
    assert status["ilha_dir_exists"] is True
    assert status["config_file_exists"] is True
    assert status["compose_file_exists"] is True


def test_detect_docker_compose_prefers_yml(setup_manager, mock_prompts):
    write_compose(setup_manager.project_root, {"version": "3", "services": {}})
    alt = setup_manager.project_root / "docker-compose.yaml"
    alt.write_text("version: '3.8'")

    detected = setup_manager.detect_docker_compose()

    assert detected.name == "docker-compose.yml"

