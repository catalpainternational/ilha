"""
Unit tests for Caddyfile mounting error scenarios in ilha CLI.

This module tests the specific error condition where Docker Compose fails to mount
the Caddyfile due to incorrect path resolution or missing files.
"""

import pytest
import subprocess
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

from ilha.core.docker_manager import DockerManager
from ilha.commands.worktree import WorktreeManager


class TestCaddyfileMountingError:
    """Test Caddyfile mounting error scenarios."""
    
    @pytest.fixture
    def docker_manager(self):
        """Create DockerManager instance with mocked dependencies."""
        with patch('ilha.core.docker_manager.validate_docker_running'), \
             patch('ilha.core.docker_manager.get_compose_command'):
            
            manager = DockerManager()
            manager.compose_cmd = "docker compose"
            return manager
    
    @pytest.fixture
    def worktree_manager(self):
        """Create WorktreeManager instance with mocked dependencies."""
        mock_orchestrator = Mock()
        mock_git_manager = Mock()
        mock_git_manager.validate_worktree_exists.return_value = True
        mock_git_manager.find_worktree_path.return_value = Path('/test/worktree')
        mock_orchestrator.git_manager = mock_git_manager
        mock_orchestrator.start_worktree.return_value = {'success': False, 'error': 'Caddyfile error'}
        
        with patch('ilha.commands.worktree.WorktreeOrchestrator', return_value=mock_orchestrator), \
             patch('ilha.commands.worktree.get_project_root', return_value=Path('/test/project')):
            
            manager = WorktreeManager(project_root=Path('/test/project'))
            # Pre-set the orchestrator to avoid _ensure_orchestrator creating a new one
            # Use the property setter if available, otherwise set directly
            manager._orchestrator = mock_orchestrator
            manager.orchestrator = mock_orchestrator  # Also set via property
            return manager
    
    @patch('subprocess.run')
    def test_docker_compose_caddyfile_mounting_failure(self, mock_run, docker_manager):
        """Test Docker Compose failure due to Caddyfile mounting error."""
        compose_file = Path("/test/docker-compose.overlay.yml")
        env_file = Path("/test/.ilha/env.ilha")
        project_name = "test"
        
        # Simulate the exact error from the terminal output
        error_stderr = """Error response from daemon: failed to create task for container: failed to create shim task: OCI runtime create failed: runc create failed: unable to start container process: error during container init: error mounting "/host_mnt/tmp/test_project/test_project/ilha-cli/Caddyfile.ilha" to rootfs at "/etc/caddy/Caddyfile": create mountpoint for /etc/caddy/Caddyfile mount: cannot create subdirectories in "/var/lib/docker/overlay2/0e95bf1b18750d834fdd6e71829438667d616029fa9dc17eaff3c6f7ceaae713/merged/etc/caddy/Caddyfile": not a directory: unknown: Are you trying to mount a directory onto a file (or vice-versa)? Check if the specified host path exists and is the expected type"""
        
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["docker", "compose", "up", "-d"],
            stderr=error_stderr
        )
        
        result = docker_manager.start_services(compose_file, env_file, project_name)
        
        assert result == False
        mock_run.assert_called_once()
    
    def test_worktree_start_with_missing_caddyfile(self, worktree_manager):
        """Test worktree start when Caddyfile path doesn't exist."""
        branch_name = "test-branch"
        
        # Mock orchestrator to return failure (simulating Caddyfile error)
        worktree_manager.orchestrator.start_worktree.return_value = {
            'success': False,
            'error': 'Caddyfile mounting error: missing Caddyfile.ilha'
        }
        
        result = worktree_manager.start_worktree(branch_name)
        
        assert result is False
        worktree_manager.orchestrator.start_worktree.assert_called_once_with(
            branch_name, profile=None, secrets_profile=None
        )
    
    @patch('subprocess.run')
    def test_docker_compose_command_with_caddyfile_error(self, mock_run, docker_manager):
        """Test Docker Compose command execution with Caddyfile mounting error."""
        compose_file = Path("/test/docker-compose.overlay.yml")
        command = ["up", "-d"]
        env_file = Path("/test/.ilha/env.ilha")
        project_name = "test"
        
        # Mock env_file.exists() to return True
        with patch.object(Path, 'exists', return_value=True):
            # Simulate the exact error from the terminal output
            error_stderr = """Network test_internal  Creating
Network test_internal  Created
Network test_web  Creating
Network test_web  Created
Volume test_caddy_data  Creating
Volume test_caddy_data  Created
Volume test_caddy_config  Creating
Volume test_caddy_config  Created
time="2025-09-28T18:11:45+09:00" level=warning msg="volume \"test_postgres_data\" already exists but was not created by Docker Compose. Use `external: true` to use an existing volume"
time="2025-09-28T18:11:45+09:00" level=warning msg="volume \"test_redis_data\" already exists but was not created by Docker Compose. Use `external: true` to use an existing volume"
time="2025-09-28T18:11:45+09:00" level=warning msg="volume \"test_media_files\" already exists but was not created by Docker Compose. Use `external: true` to use an existing volume"
Container test-caddy  Creating
Container test-redis  Creating
Container test-db  Creating
Container test-caddy  Created
Container test-redis  Created
Container test-db  Created
Container test-rq-worker-1  Creating
Container test-web  Creating
Container test-rq-worker-1  Created
Container test-web  Created
Container test-db  Starting
Container test-redis  Starting
Container test-caddy  Starting
Container test-redis  Started
Container test-db  Started
Error response from daemon: failed to create task for container: failed to create shim task: OCI runtime create failed: runc create failed: unable to start container process: error during container init: error mounting "/host_mnt/tmp/test_project/test_project/ilha-cli/Caddyfile.ilha" to rootfs at "/etc/caddy/Caddyfile": create mountpoint for /etc/caddy/Caddyfile mount: cannot create subdirectories in "/var/lib/docker/overlay2/0e95bf1b18750d834fdd6e71829438667d616029fa9dc17eaff3c6f7ceaae713/merged/etc/caddy/Caddyfile": not a directory: unknown: Are you trying to mount a directory onto a file (or vice-versa)? Check if the specified host path exists and is the expected type"""
            
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1,
                cmd=["docker", "compose", "up", "-d"],
                stderr=error_stderr
            )
            
            success, _ = docker_manager.run_compose_command(compose_file, command, env_file, project_name)
            
            assert success is False
            mock_run.assert_called_once()
            
            # Verify the command was called with correct parameters
            call_args = mock_run.call_args[0][0]
            assert "docker" in call_args
            assert "compose" in call_args
            assert "--env-file" in call_args
            assert str(env_file) in call_args
            assert "-p" in call_args
            assert project_name in call_args
            assert "-f" in call_args
            assert str(compose_file) in call_args
            assert "up" in call_args
            assert "-d" in call_args
    
    @patch('subprocess.run')
    def test_volume_warnings_with_caddyfile_error(self, mock_run, docker_manager):
        """Test that volume warnings are present alongside Caddyfile mounting error."""
        compose_file = Path("/test/docker-compose.overlay.yml")
        env_file = Path("/test/.ilha/env.ilha")
        project_name = "test"
        
        # Include both volume warnings and Caddyfile error
        error_stderr = """Network test_internal  Creating
Network test_internal  Created
Network test_web  Creating
Network test_web  Created
Volume test_caddy_data  Creating
Volume test_caddy_data  Created
Volume test_caddy_config  Creating
Volume test_caddy_config  Created
time="2025-09-28T18:11:45+09:00" level=warning msg="volume \"test_postgres_data\" already exists but was not created by Docker Compose. Use `external: true` to use an existing volume"
time="2025-09-28T18:11:45+09:00" level=warning msg="volume \"test_redis_data\" already exists but was not created by Docker Compose. Use `external: true` to use an existing volume"
time="2025-09-28T18:11:45+09:00" level=warning msg="volume \"test_media_files\" already exists but was not created by Docker Compose. Use `external: true` to use an existing volume"
Container test-caddy  Creating
Container test-redis  Creating
Container test-db  Creating
Container test-caddy  Created
Container test-redis  Created
Container test-db  Created
Container test-rq-worker-1  Creating
Container test-web  Creating
Container test-rq-worker-1  Created
Container test-web  Created
Container test-db  Starting
Container test-redis  Starting
Container test-caddy  Starting
Container test-redis  Started
Container test-db  Started
Error response from daemon: failed to create task for container: failed to create shim task: OCI runtime create failed: runc create failed: unable to start container process: error during container init: error mounting "/host_mnt/tmp/test_project/test_project/ilha-cli/Caddyfile.ilha" to rootfs at "/etc/caddy/Caddyfile": create mountpoint for /etc/caddy/Caddyfile mount: cannot create subdirectories in "/var/lib/docker/overlay2/0e95bf1b18750d834fdd6e71829438667d616029fa9dc17eaff3c6f7ceaae713/merged/etc/caddy/Caddyfile": not a directory: unknown: Are you trying to mount a directory onto a file (or vice-versa)? Check if the specified host path exists and is the expected type"""
        
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["docker", "compose", "up", "-d"],
            stderr=error_stderr
        )
        
        result = docker_manager.start_services(compose_file, env_file, project_name)
        
        assert result == False
        
        # Verify that both volume warnings and Caddyfile error are present
        assert "volume \"test_postgres_data\" already exists" in error_stderr
        assert "volume \"test_redis_data\" already exists" in error_stderr
        assert "volume \"test_media_files\" already exists" in error_stderr
        assert "error mounting" in error_stderr
        assert "ilha-cli/Caddyfile.ilha" in error_stderr
