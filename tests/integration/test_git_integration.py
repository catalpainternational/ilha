"""
Integration tests for Git operations.
"""

import pytest
import subprocess
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, Mock

from ilha.core.git_manager import GitManager
from ilha.utils.validation import validate_git_repository


class TestGitIntegration:
    """Test Git integration functionality."""
    
    @pytest.fixture(scope="class")
    def temp_git_repo(self):
        """Create a temporary Git repository for testing."""
        temp_dir = Path(tempfile.mkdtemp(prefix="ilha_git_test_"))
        
        # Initialize git repository
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        
        # Create initial commit
        (temp_dir / "README.md").write_text("# Test Repository")
        subprocess.run(["git", "add", "README.md"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=temp_dir, check=True, capture_output=True)
        
        yield temp_dir
        
        # Cleanup
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture(scope="class")
    def git_manager(self):
        """Create Git manager class."""
        return GitManager
    
    def test_git_repository_validation(self, temp_git_repo):
        """Test Git repository validation."""
        with patch('os.getcwd', return_value=str(temp_git_repo)):
            assert validate_git_repository(), "Git repository validation failed"
    
    def test_worktree_creation(self, git_manager, temp_git_repo):
        """Test worktree creation functionality."""
        with patch('ilha.core.git_manager.get_project_root', return_value=temp_git_repo), \
             patch('ilha.config.settings.get_project_root', return_value=temp_git_repo):
            git_manager_instance = git_manager()
            test_branch = "test-worktree-branch"
            worktree_path = temp_git_repo / "worktrees" / test_branch
            
            # Cleanup any existing branch and worktree first
            subprocess.run(["git", "branch", "-D", test_branch], 
                         cwd=temp_git_repo, capture_output=True, check=False)
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
            
            # Create worktree
            result = git_manager_instance.create_worktree(test_branch, worktree_path)
            assert result, "Failed to create worktree"
            
            # Verify worktree exists
            assert worktree_path.exists(), "Worktree directory was not created"
            assert (worktree_path / "README.md").exists(), "Worktree files were not copied"
            
            # Verify branch exists (check in worktree directory)
            result = subprocess.run(["git", "branch", "--list", test_branch],
                                  cwd=worktree_path, capture_output=True, text=True, check=True)
            assert test_branch in result.stdout, "Branch was not created"
            
            # Cleanup
            git_manager_instance.remove_worktree(worktree_path, force=True)
            subprocess.run(["git", "branch", "-D", test_branch], 
                         cwd=temp_git_repo, capture_output=True, check=False)
    
    def test_worktree_validation(self, git_manager, temp_git_repo):
        """Test worktree validation functionality."""
        with patch('os.getcwd', return_value=str(temp_git_repo)):
            git_manager_instance = git_manager()
            test_branch = "test-validation-branch"
            
            # Test non-existent worktree
            assert not git_manager_instance.validate_worktree_exists(test_branch), "Non-existent worktree should return False"
            
            # Create worktree
            worktree_path = temp_git_repo / "worktrees" / test_branch
            git_manager_instance.create_worktree(test_branch, worktree_path)
            
            # Test existing worktree
            assert git_manager_instance.validate_worktree_exists(test_branch), "Existing worktree should return True"
            
            # Cleanup
            git_manager_instance.remove_worktree(worktree_path, force=True)
            subprocess.run(["git", "branch", "-D", test_branch], 
                         cwd=temp_git_repo, capture_output=True, check=False)
    
    def test_branch_creation(self, git_manager, temp_git_repo):
        """Test branch creation functionality."""
        with patch('ilha.core.git_manager.get_project_root', return_value=temp_git_repo), \
             patch('ilha.config.settings.get_project_root', return_value=temp_git_repo):
            git_manager_instance = git_manager()
            test_branch = "test-branch-creation"
            
            # Cleanup any existing branch first
            subprocess.run(["git", "branch", "-D", test_branch], 
                         cwd=temp_git_repo, capture_output=True, check=False)
            
            # Test branch creation
            result = git_manager_instance.create_branch(test_branch)
            assert result, "Failed to create branch"
            
            # Verify branch exists (check all branches)
            result = subprocess.run(["git", "branch", "--list", test_branch],
                                  cwd=temp_git_repo, capture_output=True, text=True, check=True)
            assert test_branch in result.stdout, "Branch was not created"
            
            # Test creating existing branch (should succeed)
            result = git_manager_instance.create_branch(test_branch)
            assert result, "Failed to handle existing branch"
            
            # Cleanup
            subprocess.run(["git", "branch", "-D", test_branch], 
                         cwd=temp_git_repo, capture_output=True, check=False)
    
    def test_worktree_listing(self, git_manager, temp_git_repo):
        """Test worktree listing functionality."""
        with patch('os.getcwd', return_value=str(temp_git_repo)):
            git_manager_instance = git_manager()
            test_branch = "test-listing-branch"
            worktree_path = temp_git_repo / "worktrees" / test_branch
            
            # Create worktree
            git_manager_instance.create_worktree(test_branch, worktree_path)
            
            # Test listing
            worktrees = git_manager_instance.list_worktrees()
            assert len(worktrees) >= 1, "No worktrees found"
            
            # Find our test worktree
            test_worktree = None
            for path, commit, branch in worktrees:
                if branch == test_branch:
                    test_worktree = (path, commit, branch)
                    break
            
            assert test_worktree is not None, "Test worktree not found in list"
            assert str(worktree_path) in test_worktree[0], "Worktree path mismatch"
            
            # Cleanup
            git_manager_instance.remove_worktree(worktree_path, force=True)
            subprocess.run(["git", "branch", "-D", test_branch], 
                         cwd=temp_git_repo, capture_output=True, check=False)
    
    def test_branch_deletion(self, git_manager, temp_git_repo):
        """Test branch deletion functionality."""
        with patch('os.getcwd', return_value=str(temp_git_repo)):
            git_manager_instance = git_manager()
            test_branch = "test-deletion-branch"
            
            # Create branch
            git_manager_instance.create_branch(test_branch)
            
            # Test safe deletion (merged branch)
            result = git_manager_instance.delete_branch_safely(test_branch, force=False)
            assert result, "Failed to delete merged branch"
            
            # Verify branch is deleted
            result = subprocess.run(["git", "branch", "--list", test_branch],
                                  cwd=temp_git_repo, capture_output=True, text=True, check=True)
            assert test_branch not in result.stdout, "Branch was not deleted"
            
            # Test deletion of non-existent branch
            result = git_manager_instance.delete_branch_safely("non-existent-branch", force=False)
            assert result, "Should handle non-existent branch gracefully"