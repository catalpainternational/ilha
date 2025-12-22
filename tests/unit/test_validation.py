"""
Unit tests for validation utilities.
"""

import pytest
from unittest.mock import patch, Mock
from ilha.utils.validation import (
    validate_branch_name, validate_git_repository, validate_docker_running,
    validate_docker_compose, validate_worktree_directory, validate_branch_protection,
    validate_branch_exists, validate_worktree_exists, validate_current_branch,
    validate_branch_merged, validate_volume_exists, validate_network_exists,
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
    
    @patch('subprocess.run')
    def test_validate_git_repository_success(self, mock_run):
        """Test git repository validation success."""
        mock_run.return_value = Mock(returncode=0)
        assert validate_git_repository() == True
        # The actual call may include cwd parameter, so check for the call with any cwd
        mock_run.assert_called()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["git", "rev-parse", "--git-dir"]
        assert call_args[1]["capture_output"] == True
        assert call_args[1]["check"] == True
    
    @patch('subprocess.run')
    def test_validate_git_repository_failure(self, mock_run):
        """Test git repository validation failure."""
        mock_run.side_effect = Exception("Not a git repo")
        assert validate_git_repository() == False
    
    @patch('subprocess.run')
    def test_validate_docker_running_success(self, mock_run):
        """Test Docker running validation success."""
        mock_run.return_value = Mock(returncode=0)
        assert validate_docker_running() == True
        mock_run.assert_called_once_with(["docker", "info"], 
                                        capture_output=True, check=True)
    
    @patch('subprocess.run')
    def test_validate_docker_running_failure(self, mock_run):
        """Test Docker running validation failure."""
        mock_run.side_effect = Exception("Docker not running")
        assert validate_docker_running() == False
    
    @patch('subprocess.run')
    def test_validate_docker_compose_success(self, mock_run):
        """Test Docker Compose validation success."""
        mock_run.return_value = Mock(returncode=0)
        assert validate_docker_compose() == True
        mock_run.assert_called_once_with(["docker", "compose", "version"], 
                                        capture_output=True, check=True)
    
    @patch('subprocess.run')
    def test_validate_docker_compose_fallback_success(self, mock_run):
        """Test Docker Compose validation with fallback success."""
        # First call fails (docker compose), second succeeds (docker-compose)
        mock_run.side_effect = [Exception("docker compose not found"), Mock(returncode=0)]
        assert validate_docker_compose() == True
        assert mock_run.call_count == 2
    
    @patch('subprocess.run')
    def test_validate_docker_compose_failure(self, mock_run):
        """Test Docker Compose validation failure."""
        mock_run.side_effect = [Exception("docker compose not found"), Exception("docker-compose not found")]
        assert validate_docker_compose() == False
    
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
        assert validate_branch_protection("develop") == True
        assert validate_branch_protection("production") == True
        assert validate_branch_protection("staging") == True
        assert validate_branch_protection("test-branch") == False
        assert validate_branch_protection("feature-auth") == False
    
    @patch('subprocess.run')
    def test_validate_branch_exists_success(self, mock_run):
        """Test branch exists validation success."""
        mock_run.return_value = Mock(returncode=0)
        assert validate_branch_exists("test-branch") == True
        from ilha.config.settings import get_project_root
        project_root = get_project_root()
        mock_run.assert_called_once_with(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/test-branch"],
            capture_output=True, check=True, cwd=project_root
        )
    
    @patch('subprocess.run')
    def test_validate_branch_exists_failure(self, mock_run):
        """Test branch exists validation failure."""
        mock_run.side_effect = Exception("Branch not found")
        assert validate_branch_exists("test-branch") == False
    
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
    def test_validate_branch_merged_success(self, mock_run):
        """Test branch merged validation success."""
        mock_run.return_value = Mock(returncode=0)
        assert validate_branch_merged("test-branch") == True
        from ilha.config.settings import get_project_root
        project_root = get_project_root()
        mock_run.assert_called_once_with(["git", "branch", "-d", "test-branch"], 
                                        capture_output=True, check=True, cwd=project_root)
    
    @patch('subprocess.run')
    def test_validate_branch_merged_failure(self, mock_run):
        """Test branch merged validation failure."""
        mock_run.side_effect = Exception("Not merged")
        assert validate_branch_merged("test-branch") == False
    
    @patch('subprocess.run')
    def test_validate_volume_exists_success(self, mock_run):
        """Test volume exists validation success."""
        mock_run.return_value = Mock(returncode=0)
        assert validate_volume_exists("test-volume") == True
        mock_run.assert_called_once_with(["docker", "volume", "inspect", "test-volume"], 
                                        capture_output=True, check=True)
    
    @patch('subprocess.run')
    def test_validate_volume_exists_failure(self, mock_run):
        """Test volume exists validation failure."""
        mock_run.side_effect = Exception("Volume not found")
        assert validate_volume_exists("test-volume") == False
    
    @patch('subprocess.run')
    def test_validate_network_exists_success(self, mock_run):
        """Test network exists validation success."""
        mock_run.return_value = Mock(returncode=0)
        assert validate_network_exists("test-network") == True
        mock_run.assert_called_once_with(["docker", "network", "inspect", "test-network"], 
                                        capture_output=True, check=True)
    
    @patch('subprocess.run')
    def test_validate_network_exists_failure(self, mock_run):
        """Test network exists validation failure."""
        mock_run.side_effect = Exception("Network not found")
        assert validate_network_exists("test-network") == False
    
    @patch('subprocess.run')
    def test_validate_container_running_success(self, mock_run):
        """Test container running validation success."""
        mock_run.return_value = Mock(stdout="Up 2 hours", returncode=0)
        assert validate_container_running("test-container") == True
        mock_run.assert_called_once_with(
            ["docker", "ps", "--filter", "name=test-container", "--format", "{{.Status}}"],
            capture_output=True, text=True, check=True
        )
    
    @patch('subprocess.run')
    def test_validate_container_running_failure(self, mock_run):
        """Test container running validation failure."""
        mock_run.return_value = Mock(stdout="Exited", returncode=0)
        assert validate_container_running("test-container") == False
    
    @patch('ilha.utils.validation.validate_git_repository')
    @patch('ilha.utils.validation.validate_docker_running')
    @patch('ilha.utils.validation.validate_docker_compose')
    def test_check_prerequisites_success(self, mock_compose, mock_docker, mock_git):
        """Test prerequisites check success."""
        mock_git.return_value = True
        mock_docker.return_value = True
        mock_compose.return_value = True
        
        # Should not raise an exception
        check_prerequisites()
    
    @patch('ilha.utils.validation.validate_git_repository')
    def test_check_prerequisites_git_failure(self, mock_git):
        """Test prerequisites check git failure."""
        mock_git.return_value = False
        
        with pytest.raises(SystemExit):
            check_prerequisites()
    
    @patch('ilha.utils.validation.validate_git_repository')
    @patch('ilha.utils.validation.validate_docker_running')
    def test_check_prerequisites_docker_failure(self, mock_docker, mock_git):
        """Test prerequisites check docker failure."""
        mock_git.return_value = True
        mock_docker.return_value = False
        
        with pytest.raises(SystemExit):
            check_prerequisites()
    
    @patch('ilha.utils.validation.validate_git_repository')
    @patch('ilha.utils.validation.validate_docker_running')
    @patch('ilha.utils.validation.validate_docker_compose')
    def test_check_prerequisites_compose_failure(self, mock_compose, mock_docker, mock_git):
        """Test prerequisites check compose failure."""
        mock_git.return_value = True
        mock_docker.return_value = True
        mock_compose.return_value = False
        
        with pytest.raises(SystemExit):
            check_prerequisites()
