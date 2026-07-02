"""
Unit tests for validation utilities.
"""

import pytest
from unittest.mock import patch, Mock
from ilha.utils.validation import (
    validate_branch_name, validate_docker_running,
    validate_docker_compose, validate_worktree_directory, validate_branch_protection,
    validate_worktree_exists, validate_current_branch,
    validate_container_running, check_prerequisites
)


class TestValidation:
    """Test validation functions."""
    
    def test_validate_branch_name(self):
        """Test branch name validation."""
        # Valid names
        assert validate_branch_name("test-branch") == True
        assert validate_branch_name("feature_auth") == True
        assert validate_branch_name("bugfix123") == True
        
        # Invalid names
        assert validate_branch_name("test@branch") == False
        assert validate_branch_name("test branch") == False
        assert validate_branch_name("") == False
    
    @patch('ilha.utils.validation.shutil.which', return_value="/usr/local/bin/docker")
    @patch('subprocess.run')
    def test_validate_docker_running_success(self, mock_run, mock_which):
        """Test Docker running validation success.

        ``validate_docker_running`` runs ``docker info`` with ``check=False`` and
        inspects ``returncode`` (so a non-responsive daemon is reported as a
        bool rather than as a raised exception).
        """
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        assert validate_docker_running() is True
        mock_run.assert_called_once_with(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    @patch('ilha.utils.validation.shutil.which', return_value="/usr/local/bin/docker")
    @patch('subprocess.run')
    def test_validate_docker_running_failure(self, mock_run, mock_which):
        """Non-zero exit from ``docker info`` is reported as False (not raised)."""
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="Cannot connect to the Docker daemon")
        assert validate_docker_running() is False

    @patch('ilha.utils.validation.shutil.which', return_value="/usr/local/bin/docker")
    @patch('subprocess.run')
    def test_validate_docker_running_timeout(self, mock_run, mock_which):
        """A wedged daemon (TimeoutExpired) is reported as False."""
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd=["docker", "info"], timeout=20)
        assert validate_docker_running() is False

    @patch('subprocess.run')
    def test_validate_docker_compose_success(self, mock_run):
        """``docker compose version`` returning 0 is success."""
        mock_run.return_value = Mock(returncode=0, stdout="Docker Compose v2", stderr="")
        assert validate_docker_compose() is True
        mock_run.assert_called_once_with(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    @patch('subprocess.run')
    def test_validate_docker_compose_fallback_success(self, mock_run):
        """If `docker compose` fails, fall back to legacy `docker-compose`."""
        mock_run.side_effect = [
            Mock(returncode=1, stdout="", stderr="docker compose not found"),
            Mock(returncode=0, stdout="docker-compose 1.29", stderr=""),
        ]
        assert validate_docker_compose() is True
        assert mock_run.call_count == 2

    @patch('subprocess.run')
    def test_validate_docker_compose_failure(self, mock_run):
        """Both `docker compose` and `docker-compose` failing is reported as False."""
        mock_run.side_effect = [
            Mock(returncode=1, stdout="", stderr="docker compose not found"),
            Mock(returncode=1, stdout="", stderr="docker-compose not found"),
        ]
        assert validate_docker_compose() is False
    
    @patch('pathlib.Path.exists')
    def test_validate_worktree_directory_success(self, mock_exists):
        """Test worktree directory validation success."""
        # Mock that both docker-compose.yml and compose override exist
        def exists_side_effect():
            # This will be called on the Path object, so we need to check the call
            # For simplicity, just return True for all calls
            return True
        
        mock_exists.side_effect = exists_side_effect
        from pathlib import Path
        assert validate_worktree_directory(Path("/test/path")) == True
    
    @patch('pathlib.Path.exists')
    def test_validate_worktree_directory_failure(self, mock_exists):
        """Test worktree directory validation failure."""
        mock_exists.return_value = False
        from pathlib import Path
        assert validate_worktree_directory(Path("/test/path")) == False
    
    def test_validate_branch_protection(self):
        """Test branch protection validation."""
        assert validate_branch_protection("main") == True
        assert validate_branch_protection("master") == True
        assert validate_branch_protection("develop") == False
        assert validate_branch_protection("production") == False
        assert validate_branch_protection("staging") == False
        assert validate_branch_protection("test-branch") == False
        assert validate_branch_protection("feature-auth") == False
    
    @patch('subprocess.run')
    def test_validate_worktree_exists_success(self, mock_run):
        """Test worktree exists validation success."""
        mock_run.return_value = Mock(stdout="[test-branch] /path/to/worktree abc123", returncode=0)
        assert validate_worktree_exists("test-branch") == True
        from ilha.config.settings import get_project_root
        project_root = get_project_root()
        mock_run.assert_called_once_with(["git", "worktree", "list"], 
                                        capture_output=True, text=True, check=True, cwd=project_root)
    
    @patch('subprocess.run')
    def test_validate_worktree_exists_failure(self, mock_run):
        """Test worktree exists validation failure."""
        mock_run.return_value = Mock(stdout="No worktrees", returncode=0)
        assert validate_worktree_exists("test-branch") == False
    
    @patch('subprocess.run')
    def test_validate_current_branch_success(self, mock_run):
        """Test current branch validation success."""
        mock_run.return_value = Mock(stdout="main\n", returncode=0)
        assert validate_current_branch() == "main"
        from ilha.config.settings import get_project_root
        project_root = get_project_root()
        mock_run.assert_called_once_with(["git", "branch", "--show-current"], 
                                        capture_output=True, text=True, check=True, cwd=project_root)
    
    @patch('subprocess.run')
    def test_validate_current_branch_failure(self, mock_run):
        """Test current branch validation failure."""
        mock_run.side_effect = Exception("Git error")
        assert validate_current_branch() is None
    
    @patch('subprocess.run')
    def test_validate_container_running_success(self, mock_run):
        """Test container running validation success."""
        mock_run.return_value = Mock(stdout="Up 2 hours", returncode=0)
        assert validate_container_running("test-container") == True
        mock_run.assert_called_once_with(
            ["docker", "ps", "--filter", "name=^/test-container$", "--format", "{{.Status}}"],
            capture_output=True, text=True, check=True
        )
    
    @patch('subprocess.run')
    def test_validate_container_running_failure(self, mock_run):
        """Test container running validation failure."""
        mock_run.return_value = Mock(stdout="Exited", returncode=0)
        assert validate_container_running("test-container") == False
    
    @patch('ilha.utils.validation.validate_git_repository')
    @patch('ilha.utils.validation.check_docker_daemon_responsive')
    @patch('ilha.utils.validation.check_docker_compose_responsive')
    def test_check_prerequisites_success(self, mock_compose, mock_docker, mock_git):
        """Test prerequisites check success."""
        mock_git.return_value = True
        mock_docker.return_value = (True, None)
        mock_compose.return_value = (True, None)

        # Should not raise an exception
        check_prerequisites()

    @patch('ilha.utils.validation.validate_git_repository')
    def test_check_prerequisites_git_failure(self, mock_git):
        """Test prerequisites check git failure."""
        mock_git.return_value = False

        with pytest.raises(SystemExit):
            check_prerequisites()

    @patch('ilha.utils.validation.validate_git_repository')
    @patch('ilha.utils.validation.check_docker_daemon_responsive')
    def test_check_prerequisites_docker_failure(self, mock_docker, mock_git):
        """Test prerequisites check docker failure."""
        mock_git.return_value = True
        mock_docker.return_value = (False, "daemon not running")

        with pytest.raises(SystemExit):
            check_prerequisites()

    @patch('ilha.utils.validation.validate_git_repository')
    @patch('ilha.utils.validation.check_docker_daemon_responsive')
    @patch('ilha.utils.validation.check_docker_compose_responsive')
    def test_check_prerequisites_compose_failure(self, mock_compose, mock_docker, mock_git):
        """Test prerequisites check compose failure."""
        mock_git.return_value = True
        mock_docker.return_value = (True, None)
        mock_compose.return_value = (False, "no compose CLI")

        with pytest.raises(SystemExit):
            check_prerequisites()
