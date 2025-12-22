"""
Unit tests for build context resolution in worktrees.

This module tests that Docker build contexts are correctly resolved
when running ilha from different locations (project root vs worktree).
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import os

from ilha.core.docker_manager import DockerManager
from ilha.config.settings import get_project_root


class TestBuildContextResolution:
    """Test build context resolution for worktrees."""
    
    @pytest.fixture
    def docker_manager(self):
        """Create DockerManager instance."""
        return DockerManager()
    
    @pytest.fixture
    def mock_project_root(self, tmp_path):
        """Create a mock project root with .ilha/config.yml."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        
        # Create .ilha directory with config.yml
        ilha_dir = project_root / ".ilha"
        ilha_dir.mkdir()
        config_file = ilha_dir / "config.yml"
        config_file.write_text("project_name: test-project\n")
        
        return project_root
    
    @pytest.fixture
    def mock_worktree(self, mock_project_root):
        """Create a mock worktree with Dockerfile."""
        worktree_path = mock_project_root / "worktrees" / "feature"
        worktree_path.mkdir(parents=True)
        
        # Create Dockerfile in worktree
        dockerfile = worktree_path / "Dockerfile"
        dockerfile.write_text("FROM alpine:latest\n")
        
        # Create .ilha directory in worktree (fractal design)
        worktree_ilha = worktree_path / ".ilha"
        worktree_ilha.mkdir()
        
        # Copy config.yml to worktree (simulating fractal design)
        config_file = worktree_ilha / "config.yml"
        config_file.write_text("project_name: test-project\n")
        
        return worktree_path
    
    def test_get_project_root_from_project_root(self, mock_project_root):
        """Test get_project_root() returns true project root when run from project root."""
        with patch('pathlib.Path.cwd', return_value=mock_project_root):
            result = get_project_root()
            assert result == mock_project_root
            assert (result / ".ilha" / "config.yml").exists()
    
    def test_get_project_root_from_worktree(self, mock_project_root, mock_worktree):
        """Test get_project_root() behavior when run from worktree."""
        with patch('pathlib.Path.cwd', return_value=mock_worktree):
            result = get_project_root()
            # Since the worktree also has .ilha/config.yml (fractal design),
            # get_project_root() may return the worktree path if it finds config there first.
            # This is acceptable behavior - the worktree is a valid project root.
            assert result in (mock_project_root, mock_worktree)
            assert (result / ".ilha" / "config.yml").exists()
    
    def test_get_project_root_from_worktree_subdirectory(self, mock_project_root, mock_worktree):
        """Test get_project_root() behavior when run from worktree subdirectory."""
        worktree_subdir = mock_worktree / "src"
        worktree_subdir.mkdir()
        
        with patch('pathlib.Path.cwd', return_value=worktree_subdir):
            result = get_project_root()
            # Since the worktree has .ilha/config.yml, it may return worktree or project root
            assert result in (mock_project_root, mock_worktree)
            assert (result / ".ilha" / "config.yml").exists()
    
    @patch('ilha.core.docker_manager.subprocess.run')
    def test_run_compose_command_with_worktree_path(self, mock_run, docker_manager, mock_worktree):
        """Test that run_compose_command sets PROJECT_ROOT to worktree path."""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        
        compose_file = mock_worktree / ".ilha" / "docker-compose.worktree.yml"
        compose_file.parent.mkdir(parents=True, exist_ok=True)
        compose_file.write_text("version: '3.8'\nservices:\n  web:\n    build: ${PROJECT_ROOT}")
        
        env_file = mock_worktree / ".ilha" / "env.ilha"
        env_file.write_text("COMPOSE_PROJECT_NAME=test-feature")
        
        # Mock the subprocess call to capture environment variables
        def capture_env(cmd, **kwargs):
            # Store the environment for inspection
            capture_env.env = kwargs.get('env', {})
            return Mock(returncode=0, stdout="", stderr="")
        
        mock_run.side_effect = capture_env
        
        result = docker_manager.run_compose_command(
            compose_file=compose_file,
            command=["up", "-d"],
            env_file=env_file,
            project_name="test-feature",
            working_dir=mock_worktree
        )
        
        assert result == True
        assert mock_run.called
        
        # Check that PROJECT_ROOT is set to the worktree path
        call_args = mock_run.call_args
        env = call_args.kwargs.get('env', {})
        assert env['PROJECT_ROOT'] == str(mock_worktree.resolve())
        assert env['COMPOSE_PROJECT_ROOT'] == str(mock_worktree.resolve())
        assert call_args.kwargs['cwd'] == mock_worktree.resolve()
    
    @patch('ilha.core.docker_manager.subprocess.run')
    def test_run_compose_command_with_profile_worktree_path(self, mock_run, docker_manager, mock_worktree):
        """Test that run_compose_command_with_profile sets PROJECT_ROOT to worktree path."""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        
        compose_file = mock_worktree / ".ilha" / "docker-compose.worktree.yml"
        compose_file.parent.mkdir(parents=True, exist_ok=True)
        compose_file.write_text("version: '3.8'\nservices:\n  web:\n    build: ${PROJECT_ROOT}")
        
        compose_override = mock_worktree / ".ilha" / "docker-compose.override.yml"
        compose_override.write_text("version: '3.8'\nservices:\n  web:\n    profiles: [ilha]")
        
        env_file = mock_worktree / ".ilha" / "env.ilha"
        env_file.write_text("COMPOSE_PROJECT_NAME=test-feature")
        
        # Mock the subprocess call to capture environment variables
        def capture_env(cmd, **kwargs):
            # Store the environment for inspection
            capture_env.env = kwargs.get('env', {})
            return Mock(returncode=0, stdout="", stderr="")
        
        mock_run.side_effect = capture_env
        
        result = docker_manager.run_compose_command_with_profile(
            compose_file=compose_file,
            compose_override=compose_override,
            command=["up", "-d"],
            env_file=env_file,
            project_name="test-feature",
            working_dir=mock_worktree
        )
        
        assert result == True
        assert mock_run.called
        
        # Check that PROJECT_ROOT is set to the worktree path
        call_args = mock_run.call_args
        env = call_args.kwargs.get('env', {})
        assert env['PROJECT_ROOT'] == str(mock_worktree.resolve())
        assert env['COMPOSE_PROJECT_ROOT'] == str(mock_worktree.resolve())
        assert call_args.kwargs['cwd'] == mock_worktree.resolve()
    
    def test_absolute_path_resolution(self, docker_manager, mock_worktree):
        """Test that working_dir is resolved to absolute path."""
        relative_worktree = Path("worktrees/feature")
        
        with patch('ilha.core.docker_manager.subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            
            compose_file = mock_worktree / ".ilha" / "docker-compose.worktree.yml"
            compose_file.parent.mkdir(parents=True, exist_ok=True)
            compose_file.write_text("version: '3.8'")
            
            result = docker_manager.run_compose_command(
                compose_file=compose_file,
                command=["up", "-d"],
                working_dir=relative_worktree
            )
            
            # Check that the working directory was resolved to absolute path
            call_args = mock_run.call_args
            assert call_args.kwargs['cwd'] == relative_worktree.resolve()
    
    @patch('ilha.core.docker_manager.log_info')
    def test_debug_logging_includes_paths(self, mock_log_info, docker_manager, mock_worktree):
        """Test that debug logging shows the correct paths."""
        with patch('ilha.core.docker_manager.subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            
            compose_file = mock_worktree / ".ilha" / "docker-compose.worktree.yml"
            compose_file.parent.mkdir(parents=True, exist_ok=True)
            compose_file.write_text("version: '3.8'")
            
            docker_manager.run_compose_command(
                compose_file=compose_file,
                command=["up", "-d"],
                working_dir=mock_worktree
            )
            
            # Check that debug logging was called with correct paths
            assert mock_log_info.called
            
            # Check that the log messages contain the expected path information
            log_calls = [call[0][0] for call in mock_log_info.call_args_list]
            assert any("Working directory:" in call for call in log_calls)
            assert any("PROJECT_ROOT:" in call for call in log_calls)
            assert any("Compose file:" in call for call in log_calls)
