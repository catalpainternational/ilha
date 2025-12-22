"""
Unit tests for package import with domain override functionality.

Tests cover:
- Standalone import with domain override
- Normal import with domain override
- Verification that domain overrides are applied correctly
- Both localhost (default) and domain override scenarios
"""

import pytest
import yaml
import json
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from ilha.core.package_manager import PackageManager
from ilha.core.environment_manager import EnvironmentManager


class TestPackageImportDomain:
    """Test package import with domain override functionality."""

    @pytest.fixture
    def temp_project(self, tmp_path):
        """Create a temporary project directory."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        return project_root

    @pytest.fixture
    def package_manager(self, temp_project):
        """Create PackageManager instance."""
        with patch('ilha.core.package_manager.get_project_root', return_value=temp_project):
            return PackageManager(project_root=temp_project)

    def _create_test_package(self, package_dir: Path, branch_name: str = "test-branch") -> Path:
        """Create a test package with minimal contents.
        
        Returns:
            Path to the created package file
        """
        # Package structure: {name}.ilha-package/
        package_name = f"{branch_name}.ilha-package"
        package_content_dir = package_dir / package_name
        package_content_dir.mkdir(parents=True)
        
        # Create metadata
        metadata = {
            "branch_name": branch_name,
            "project_name": "test-project",
            "include_code": True,
            "timestamp": "2024-01-01T00:00:00Z"
        }
        metadata_file = package_content_dir / "metadata.json"
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f)
        
        # Create code archive
        code_dir = package_content_dir / "code"
        code_dir.mkdir()
        code_archive = code_dir / f"{branch_name}.tar.gz"
        with tarfile.open(code_archive, 'w:gz') as tar:
            # Add a dummy file
            dummy_file = package_content_dir / "dummy.txt"
            dummy_file.write_text("test content")
            tar.add(dummy_file, arcname="dummy.txt")
        
        # Create environment files
        env_dir = package_content_dir / "environment"
        env_dir.mkdir()
        ilha_env_dir = env_dir / ".ilha"
        ilha_env_dir.mkdir()
        
        # Create env.ilha with localhost
        env_ilha = ilha_env_dir / "env.ilha"
        env_ilha.write_text("""COMPOSE_PROJECT_NAME=test-project-test-branch
SITE_DOMAIN=test-project-test-branch.localhost
ALLOWED_HOSTS=localhost,127.0.0.1,test-project-test-branch.localhost,*.localhost,web
DEBUG=True
""")
        
        # Create compose file with localhost labels
        compose_file = ilha_env_dir / "docker-compose.worktree.yml"
        compose_data = {
            'services': {
                'web': {
                    'image': 'nginx:alpine',
                    'labels': [
                        'caddy.proxy=${COMPOSE_PROJECT_NAME}.localhost',
                        'caddy.proxy.reverse_proxy=${COMPOSE_PROJECT_NAME}-web:8000'
                    ]
                }
            }
        }
        with open(compose_file, 'w') as f:
            yaml.dump(compose_data, f)
        
        # Create .env file
        env_file = env_dir / ".env"
        env_file.write_text("SITE_DOMAIN=test-project-test-branch.localhost\n")
        
        # Create package tar.gz
        package_file = package_dir.parent / f"{branch_name}.tar.gz"
        with tarfile.open(package_file, 'w:gz') as tar:
            tar.add(package_content_dir, arcname=package_content_dir.name)
        
        return package_file

    @patch('ilha.core.package_manager.subprocess.run')
    @patch('ilha.commands.setup.SetupManager')
    @patch('ilha.core.package_manager.WorktreeOrchestrator')
    @patch('ilha.core.package_manager.EnvironmentManager')
    def test_standalone_import_with_domain_override(
        self, mock_env_manager_class, mock_orchestrator_class, mock_setup_class, mock_subprocess, 
        package_manager, temp_project
    ):
        """Test standalone import applies domain override correctly."""
        # Setup mocks
        mock_setup = MagicMock()
        mock_setup.setup_project.return_value = True
        mock_setup_class.return_value = mock_setup
        
        mock_orchestrator = MagicMock()
        mock_orchestrator.create_worktree.return_value = {
            "success": True,
            "data": {"branch": "test-branch", "status": "created"}
        }
        mock_orchestrator_class.return_value = mock_orchestrator
        
        mock_env_manager = MagicMock()
        mock_env_manager.apply_domain_overrides.return_value = True
        mock_env_manager_class.return_value = mock_env_manager
        
        mock_subprocess.return_value = MagicMock(returncode=0)
        
        # Create test package
        package_file = self._create_test_package(temp_project / "package", "test-branch")
        
        # Use a unique target directory to avoid conflicts
        import uuid
        unique_target = temp_project / f"test-project-{uuid.uuid4().hex[:8]}"
        
        # Import with domain override
        domain = "app.example.com"
        result = package_manager.import_package(
            package_file,
            target_branch=None,
            restore_data=False,
            standalone=True,
            target_directory=unique_target,
            domain=domain,
            ip=None,
            non_interactive=True
        )
        
        assert result["success"] is True
        
        # Verify setup_project was called with domain
        mock_setup.setup_project.assert_called_once()
        call_kwargs = mock_setup.setup_project.call_args[1]
        assert call_kwargs.get("domain") == domain
        
        # Verify worktree was created
        mock_orchestrator.create_worktree.assert_called_once_with("test-branch")
        
        # Verify domain override was applied (check that apply_domain_overrides was called)
        # This is done internally in _standalone_import, so we verify the worktree path exists
        worktree_path = temp_project / "test-project" / "worktrees" / "test-branch"
        # Note: In real scenario, this would be created, but with mocks we can't verify file updates
        # The actual file updates are tested in test_domain_overrides_compose.py

    @patch('ilha.core.package_manager.subprocess.run')
    @patch('ilha.commands.setup.SetupManager')
    @patch('ilha.core.package_manager.WorktreeOrchestrator')
    @patch('ilha.core.package_manager.EnvironmentManager')
    def test_standalone_import_without_domain_keeps_localhost(
        self, mock_env_manager_class, mock_orchestrator_class, mock_setup_class, mock_subprocess,
        package_manager, temp_project
    ):
        """Test standalone import without domain override keeps localhost."""
        # Setup mocks
        mock_setup = MagicMock()
        mock_setup.setup_project.return_value = True
        mock_setup_class.return_value = mock_setup
        
        mock_orchestrator = MagicMock()
        mock_orchestrator.create_worktree.return_value = {
            "success": True,
            "data": {"branch": "test-branch", "status": "created"}
        }
        mock_orchestrator_class.return_value = mock_orchestrator
        
        mock_env_manager = MagicMock()
        mock_env_manager.apply_domain_overrides.return_value = True
        mock_env_manager_class.return_value = mock_env_manager
        
        mock_subprocess.return_value = MagicMock(returncode=0)
        
        # Create test package
        package_file = self._create_test_package(temp_project / "package", "test-branch")
        
        # Use a unique target directory to avoid conflicts
        import uuid
        unique_target = temp_project / f"test-project-{uuid.uuid4().hex[:8]}"
        
        # Import without domain override
        result = package_manager.import_package(
            package_file,
            target_branch=None,
            restore_data=False,
            standalone=True,
            target_directory=unique_target,
            domain=None,
            ip=None,
            non_interactive=True
        )
        
        assert result["success"] is True
        
        # Verify setup_project was called without domain
        mock_setup.setup_project.assert_called_once()
        call_kwargs = mock_setup.setup_project.call_args[1]
        assert call_kwargs.get("domain") is None

    def test_apply_domain_overrides_after_import(self, temp_project):
        """Test that apply_domain_overrides works correctly after package import.
        
        This simulates the scenario where:
        1. Package is imported (restores files with localhost)
        2. Domain override is applied
        3. Files are updated correctly
        """
        env_manager = EnvironmentManager(project_root=temp_project)
        worktree_path = temp_project / "worktrees" / "test-branch"
        worktree_path.mkdir(parents=True)
        ilha_dir = worktree_path / ".ilha"
        ilha_dir.mkdir(parents=True)
        
        # Simulate restored environment files (from package) with localhost
        env_file = ilha_dir / "env.ilha"
        env_file.write_text("""COMPOSE_PROJECT_NAME=test-project-test-branch
SITE_DOMAIN=test-project-test-branch.localhost
ALLOWED_HOSTS=localhost,127.0.0.1,test-project-test-branch.localhost,*.localhost,web
DEBUG=True
""")
        
        compose_file = ilha_dir / "docker-compose.worktree.yml"
        compose_data = {
            'services': {
                'web': {
                    'image': 'nginx:alpine',
                    'labels': [
                        'caddy.proxy=${COMPOSE_PROJECT_NAME}.localhost',
                        'caddy.proxy.reverse_proxy=${COMPOSE_PROJECT_NAME}-web:8000'
                    ]
                }
            }
        }
        with open(compose_file, 'w') as f:
            yaml.dump(compose_data, f)
        
        # Apply domain override (simulating what happens after import)
        domain = "hub.logboom.io"
        result = env_manager.apply_domain_overrides(worktree_path, domain)
        
        assert result is True
        
        # Verify compose file was updated
        with open(compose_file) as f:
            updated_compose = yaml.safe_load(f)
        
        web_labels = updated_compose['services']['web']['labels']
        assert 'caddy.proxy=hub.logboom.io' in web_labels
        assert 'caddy.proxy=${COMPOSE_PROJECT_NAME}.localhost' not in web_labels
        
        # Verify env.ilha was updated
        env_content = env_file.read_text()
        assert f'SITE_DOMAIN=https://{domain}' in env_content
        assert domain in env_content
        assert 'ALLOWED_HOSTS=' in env_content
        # Check ALLOWED_HOSTS includes domain
        lines = env_content.split('\n')
        allowed_hosts_line = [l for l in lines if l.startswith('ALLOWED_HOSTS=')][0]
        assert domain in allowed_hosts_line

    def test_localhost_vs_domain_comparison(self, temp_project):
        """Test comparison between localhost (default) and domain override scenarios."""
        env_manager = EnvironmentManager(project_root=temp_project)
        
        # Scenario 1: Localhost (default)
        localhost_worktree = temp_project / "worktrees" / "localhost-branch"
        localhost_worktree.mkdir(parents=True)
        localhost_ilha = localhost_worktree / ".ilha"
        localhost_ilha.mkdir(parents=True)
        
        localhost_env = localhost_ilha / "env.ilha"
        localhost_env.write_text("""COMPOSE_PROJECT_NAME=test-project-localhost-branch
SITE_DOMAIN=test-project-localhost-branch.localhost
ALLOWED_HOSTS=localhost,127.0.0.1,test-project-localhost-branch.localhost,*.localhost,web
""")
        
        localhost_compose = localhost_ilha / "docker-compose.worktree.yml"
        localhost_compose_data = {
            'services': {
                'web': {
                    'labels': ['caddy.proxy=${COMPOSE_PROJECT_NAME}.localhost']
                }
            }
        }
        with open(localhost_compose, 'w') as f:
            yaml.dump(localhost_compose_data, f)
        
        # Scenario 2: Domain override
        domain_worktree = temp_project / "worktrees" / "domain-branch"
        domain_worktree.mkdir(parents=True)
        domain_ilha = domain_worktree / ".ilha"
        domain_ilha.mkdir(parents=True)
        
        # Start with localhost (simulating package restore)
        domain_env = domain_ilha / "env.ilha"
        domain_env.write_text("""COMPOSE_PROJECT_NAME=test-project-domain-branch
SITE_DOMAIN=test-project-domain-branch.localhost
ALLOWED_HOSTS=localhost,127.0.0.1,test-project-domain-branch.localhost,*.localhost,web
""")
        
        domain_compose = domain_ilha / "docker-compose.worktree.yml"
        domain_compose_data = {
            'services': {
                'web': {
                    'labels': ['caddy.proxy=${COMPOSE_PROJECT_NAME}.localhost']
                }
            }
        }
        with open(domain_compose, 'w') as f:
            yaml.dump(domain_compose_data, f)
        
        # Apply domain override
        domain = "app.example.com"
        env_manager.apply_domain_overrides(domain_worktree, domain)
        
        # Compare results
        # Localhost should keep localhost
        localhost_env_content = localhost_env.read_text()
        assert 'test-project-localhost-branch.localhost' in localhost_env_content
        
        with open(localhost_compose) as f:
            localhost_compose_content = yaml.safe_load(f)
        assert '${COMPOSE_PROJECT_NAME}.localhost' in str(localhost_compose_content['services']['web']['labels'])
        
        # Domain should be updated
        domain_env_content = domain_env.read_text()
        assert f'https://{domain}' in domain_env_content
        assert 'test-project-domain-branch.localhost' not in domain_env_content
        
        with open(domain_compose) as f:
            domain_compose_content = yaml.safe_load(f)
        assert domain in str(domain_compose_content['services']['web']['labels'])
        assert '${COMPOSE_PROJECT_NAME}.localhost' not in str(domain_compose_content['services']['web']['labels'])

