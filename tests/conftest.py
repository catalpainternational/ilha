"""
Pytest configuration and fixtures for ilha CLI tests.
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch
from typing import Generator

from ilha.core.docker_manager import DockerManager
from ilha.core.git_manager import GitManager
from ilha.core.environment_manager import EnvironmentManager


def pytest_collection_modifyitems(config, items):
    """Auto-mark tests under tests/integration/ and tests/e2e/ so the default
    suite (unit only) stays fast. Opt in with `pytest -m integration` etc."""
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if "/tests/integration/" in path:
            item.add_marker(pytest.mark.integration)
        elif "/tests/e2e/" in path:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def temp_project_dir() -> Generator[Path, None, None]:
    """Create a temporary project directory for testing."""
    temp_dir = Path(tempfile.mkdtemp(prefix="ilha_test_"))
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture(scope="function")
def test_branch() -> str:
    """Generate a test branch name."""
    return "test"


@pytest.fixture(scope="function")
def mock_docker_manager() -> Mock:
    """Mock Docker manager for testing."""
    return Mock(spec=DockerManager)


@pytest.fixture(scope="function")
def mock_git_manager() -> Mock:
    """Mock Git manager for testing."""
    return Mock(spec=GitManager)


@pytest.fixture(scope="function")
def mock_environment_manager() -> Mock:
    """Mock Environment manager for testing."""
    return Mock(spec=EnvironmentManager)


@pytest.fixture(scope="function")
def mock_subprocess():
    """Mock subprocess for testing."""
    with patch('subprocess.run') as mock:
        yield mock


@pytest.fixture(scope="function")
def mock_path_exists():
    """Mock pathlib.Path.exists for testing."""
    with patch('pathlib.Path.exists') as mock:
        mock.return_value = True
        yield mock


@pytest.fixture(scope="function")
def mock_os_chdir():
    """Mock os.chdir for testing."""
    with patch('os.chdir') as mock:
        yield mock


@pytest.fixture(scope="function")
def mock_os_getcwd():
    """Mock os.getcwd for testing."""
    with patch('os.getcwd') as mock:
        mock.return_value = "/test/project"
        yield mock


@pytest.fixture(scope="function")
def sample_worktree_config() -> dict:
    """Sample worktree configuration for testing."""
    return {
        "branch_name": "test-branch",
        "domain_name": "test-branch.localhost",
        "allowed_hosts": "localhost,127.0.0.1,test-branch.localhost,*.localhost",
        "database_url": "postgres://biuser:bipassword@test-branch-db:5432/test_project",
        "redis_url": "redis://test-branch-redis:6379/0",
        "volume_names": {
            "postgres": "test-branch_postgres_data",
            "redis": "test-branch_redis_data",
            "media": "test-branch_media_files"
        },
        "environment_variables": {
            "COMPOSE_PROJECT_NAME": "test-branch",
            "SITE_DOMAIN": "test-branch.localhost",
            "ALLOWED_HOSTS": "localhost,127.0.0.1,test-branch.localhost,*.localhost"
        }
    }


@pytest.fixture(scope="function")
def sample_volume_names() -> dict:
    """Sample volume names for testing."""
    return {
        "postgres": "test-branch_postgres_data",
        "redis": "test-branch_redis_data",
        "media": "test-branch_media_files"
    }


@pytest.fixture(scope="function")
def sample_worktree_list() -> list:
    """Sample worktree list for testing."""
    return [
        ("/path/to/worktrees/test-branch", "abc123", "test-branch"),
        ("/path/to/worktrees/feature-auth", "def456", "feature-auth"),
    ]


@pytest.fixture(scope="function")
def sample_volume_list() -> list:
    """Sample volume list for testing."""
    return [
        "test-branch_postgres_data",
        "test-branch_redis_data",
        "test-branch_media_files",
        "feature-auth_postgres_data",
        "feature-auth_redis_data",
        "feature-auth_media_files",
    ]


@pytest.fixture(scope="function")
def sample_volume_sizes() -> dict:
    """Sample volume sizes for testing."""
    return {
        "test-branch_postgres_data": "1.2GB",
        "test-branch_redis_data": "50MB",
        "test-branch_media_files": "200MB",
        "feature-auth_postgres_data": "800MB",
        "feature-auth_redis_data": "30MB",
        "feature-auth_media_files": "150MB",
    }
