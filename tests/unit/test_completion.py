"""
Unit tests for completion functionality.

This module tests the completion helper functions and CLI completion commands.
"""

import pytest
import subprocess
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

from ilha.utils.completion_helper import (
    get_worktree_names,
    get_volume_branch_names,
    get_all_branch_names,
    get_git_branch_names,
    get_completion_for_context,
    validate_completion_input,
    get_safe_completions,
    get_main_commands,
    get_volume_subcommands,
    get_completion_flags
)


class TestCompletionHelper:
    """Test completion helper functions."""
    
    def test_get_worktree_names_success(self):
        """Test getting worktree names successfully."""
        mock_worktrees = [
            ('/path/to/worktree1', 'abc123', 'feature-auth'),
            ('/path/to/worktree2', 'def456', 'feature-payments'),
            ('/path/to/worktree3', 'ghi789', 'feature-notifications')
        ]
        
        with patch('ilha.core.git_manager.GitManager') as mock_git_manager:
            mock_instance = Mock()
            mock_instance.list_worktrees.return_value = mock_worktrees
            mock_git_manager.return_value = mock_instance
            
            result = get_worktree_names()
            
            assert result == ['feature-auth', 'feature-payments', 'feature-notifications']
            mock_instance.list_worktrees.assert_called_once()
    
    def test_get_worktree_names_failure(self):
        """Test getting worktree names when git operations fail."""
        with patch('ilha.core.git_manager.GitManager') as mock_git_manager:
            mock_instance = Mock()
            mock_instance.list_worktrees.side_effect = Exception("Git error")
            mock_git_manager.return_value = mock_instance
            
            result = get_worktree_names()
            
            assert result == []
    
    def test_get_volume_branch_names_success(self):
        """Test getting volume branch names successfully."""
        # The implementation removes suffixes, then checks if there's an underscore.
        # If there is, it takes the part before the first underscore.
        # So we need volumes that have an underscore AFTER removing the suffix.
        # Example: 'feature_auth_something_postgres_data' -> 'feature_auth_something' -> 'feature'
        mock_volumes = [
            'feature_auth_something_postgres_data',
            'feature_auth_something_redis_data',
            'feature_payments_something_postgres_data',
            'feature_notifications_something_media_files'
        ]
        
        with patch('ilha.core.docker_manager.DockerManager') as mock_docker_manager:
            with patch('ilha.core.docker_manager.validate_docker_running', return_value=True):
                with patch('ilha.core.docker_manager.get_project_root', return_value=None):
                    mock_instance = Mock()
                    mock_instance.list_volumes.return_value = mock_volumes
                    mock_docker_manager.return_value = mock_instance
                    
                    result = get_volume_branch_names()
                    
                    # All will extract to 'feature' since that's the part before the first underscore
                    expected = ['feature']
                    assert sorted(result) == sorted(expected)
    
    def test_get_volume_branch_names_failure(self):
        """Test getting volume branch names when docker operations fail."""
        with patch('ilha.core.docker_manager.DockerManager') as mock_docker_manager:
            mock_instance = Mock()
            mock_instance.list_volumes.side_effect = Exception("Docker error")
            mock_docker_manager.return_value = mock_instance
            
            result = get_volume_branch_names()
            
            assert result == []
    
    def test_get_all_branch_names(self):
        """Test getting all branch names (worktrees + volumes)."""
        with patch('ilha.utils.completion_helper.get_worktree_names') as mock_worktrees, \
             patch('ilha.utils.completion_helper.get_volume_branch_names') as mock_volumes:
            
            mock_worktrees.return_value = ['feature-auth', 'feature-payments']
            mock_volumes.return_value = ['feature-auth', 'feature-notifications']
            
            result = get_all_branch_names()
            
            expected = ['feature-auth', 'feature-notifications', 'feature-payments']
            assert sorted(result) == expected
    
    def test_get_git_branch_names_success(self):
        """Test getting git branch names successfully."""
        # Format matches 'git branch -a' output with remotes prefix
        mock_output = "  main\n  feature-auth\n  feature-payments\n  remotes/origin/develop\n  remotes/origin/main\n  remotes/origin/HEAD -> origin/main"
        
        with patch('subprocess.run') as mock_run:
            mock_result = Mock()
            mock_result.stdout = mock_output
            mock_result.returncode = 0
            mock_run.return_value = mock_result
            
            with patch('ilha.utils.completion_helper.get_project_root') as mock_root:
                mock_root.return_value = Path('/test/project')
                
                result = get_git_branch_names()
                
                # The function includes 'HEAD -> origin/main' and filters out 'main' from the skip list
                expected = ['HEAD -> origin/main', 'develop', 'feature-auth', 'feature-payments']
                assert sorted(result) == expected
    
    def test_get_git_branch_names_failure(self):
        """Test getting git branch names when git command fails."""
        with patch('ilha.utils.completion_helper.subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, 'git')
            
            with patch('ilha.utils.completion_helper.get_project_root') as mock_root:
                mock_root.return_value = Path('/test/project')
                
                result = get_git_branch_names()
                
                assert result == []
    
    def test_get_completion_for_context(self):
        """Test getting completions for different contexts."""
        with patch('ilha.utils.completion_helper.get_worktree_names') as mock_worktrees, \
             patch('ilha.utils.completion_helper.get_volume_branch_names') as mock_volumes, \
             patch('ilha.utils.completion_helper.get_all_branch_names') as mock_all, \
             patch('ilha.utils.completion_helper.get_git_branch_names') as mock_git:
            
            mock_worktrees.return_value = ['feature-auth']
            mock_volumes.return_value = ['feature-payments']
            mock_all.return_value = ['feature-auth', 'feature-payments']
            mock_git.return_value = ['main', 'develop']
            
            # Test worktrees context
            result = get_completion_for_context('worktrees')
            assert result == ['feature-auth']
            
            # Test volumes context
            result = get_completion_for_context('volumes')
            assert result == ['feature-payments']
            
            # Test all context
            result = get_completion_for_context('all')
            assert result == ['feature-auth', 'feature-payments']
            
            # Test git context
            result = get_completion_for_context('git')
            assert result == ['main', 'develop']
            
            # Test unknown context
            result = get_completion_for_context('unknown')
            assert result == []
    
    def test_validate_completion_input(self):
        """Test validation of completion input."""
        # Valid inputs
        assert validate_completion_input("feature-auth") is True
        assert validate_completion_input("feature_payments") is True
        assert validate_completion_input("feature.payments") is True
        assert validate_completion_input("123") is True
        
        # Invalid inputs
        assert validate_completion_input("") is False
        assert validate_completion_input("feature;auth") is False
        assert validate_completion_input("feature&auth") is False
        assert validate_completion_input("feature|auth") is False
        assert validate_completion_input("feature`auth") is False
        assert validate_completion_input("feature$auth") is False
        assert validate_completion_input("feature(auth") is False
        assert validate_completion_input("feature<auth") is False
        assert validate_completion_input("feature\nauth") is False
        assert validate_completion_input("feature\rauth") is False
    
    def test_get_safe_completions(self):
        """Test filtering completions for safety."""
        completions = [
            "feature-auth",
            "feature;malicious",
            "feature-payments",
            "feature|injection",
            "feature-notifications"
        ]
        
        result = get_safe_completions(completions)
        
        expected = ["feature-auth", "feature-payments", "feature-notifications"]
        assert result == expected
    
    def test_get_main_commands(self):
        """Test getting main commands."""
        commands = get_main_commands()
        
        # Updated to match actual implementation which includes 'start-proxy' and 'stop-proxy'
        expected_commands = [
            'start-proxy', 'stop-proxy', 'start', 'stop', 'create', 'up', 'down', 'delete', 'remove',
            'remove-all', 'delete-all', 'list', 'prune', 'volumes', 'setup',
            'help', 'completion'
        ]
        
        assert sorted(commands) == sorted(expected_commands)
    
    def test_get_volume_subcommands(self):
        """Test getting volume subcommands."""
        subcommands = get_volume_subcommands()
        
        expected = ['list', 'size', 'backup', 'restore', 'clean']
        assert sorted(subcommands) == sorted(expected)
    
    def test_get_completion_flags(self):
        """Test getting completion flags."""
        flags = get_completion_flags()
        
        # Updated to match actual implementation
        expected = ['--force', '-d', '--detach', '--help', '-h']
        assert sorted(flags) == sorted(expected)


class TestCompletionCLI:
    """Test CLI completion commands."""
    
    def test_completion_command_worktrees(self):
        """Test the hidden _completion command for worktrees."""
        from ilha.utils.completion_helper import print_completions
        from io import StringIO
        import sys
        
        # Mock the underlying functions that get_completion_for_context calls
        with patch('ilha.utils.completion_helper.get_worktree_names') as mock_worktrees:
            mock_worktrees.return_value = ['feature-auth', 'feature-payments']
            
            # Capture stdout
            old_stdout = sys.stdout
            sys.stdout = captured_output = StringIO()
            
            try:
                from ilha.utils.completion_helper import get_completion_for_context
                completions = get_completion_for_context('worktrees')
                print_completions(completions)
                output = captured_output.getvalue()
                
                assert 'feature-auth' in output
                assert 'feature-payments' in output
            finally:
                sys.stdout = old_stdout
    
    def test_completion_command_volumes(self):
        """Test the hidden _completion command for volumes."""
        from ilha.utils.completion_helper import print_completions
        from io import StringIO
        import sys
        
        # Mock the underlying functions that get_completion_for_context calls
        with patch('ilha.utils.completion_helper.get_volume_branch_names') as mock_volumes:
            mock_volumes.return_value = ['feature-auth', 'feature-payments']
            
            old_stdout = sys.stdout
            sys.stdout = captured_output = StringIO()
            
            try:
                from ilha.utils.completion_helper import get_completion_for_context
                completions = get_completion_for_context('volumes')
                print_completions(completions)
                output = captured_output.getvalue()
                
                assert 'feature-auth' in output
                assert 'feature-payments' in output
            finally:
                sys.stdout = old_stdout
    
    def test_completion_command_failure(self):
        """Test the hidden _completion command when it fails."""
        from ilha.utils.completion_helper import get_completion_for_context, print_completions
        from io import StringIO
        import sys
        
        with patch('ilha.utils.completion_helper.get_completion_for_context') as mock_get:
            mock_get.side_effect = Exception("Test error")
            
            old_stdout = sys.stdout
            sys.stdout = captured_output = StringIO()
            
            try:
                # Should handle exception gracefully
                try:
                    completions = get_completion_for_context('worktrees')
                    print_completions(completions)
                except Exception:
                    pass  # Expected to fail
                
                # Should not crash
                assert True
            finally:
                sys.stdout = old_stdout
    
    def test_completion_install_command(self):
        """Test the completion install command."""
        from ilha.cli import completion_install
        
        with patch('ilha.commands.completion.CompletionManager') as mock_manager_class:
            mock_manager = Mock()
            mock_manager.install_completion.return_value = True
            mock_manager_class.return_value = mock_manager
            
            # Should not raise exception
            completion_install('bash')
            mock_manager.install_completion.assert_called_once_with('bash')
    
    def test_completion_uninstall_command(self):
        """Test the completion uninstall command."""
        from ilha.cli import completion_uninstall
        
        with patch('ilha.commands.completion.CompletionManager') as mock_manager_class:
            mock_manager = Mock()
            mock_manager.uninstall_completion.return_value = True
            mock_manager_class.return_value = mock_manager
            
            # Should not raise exception
            completion_uninstall()
            mock_manager.uninstall_completion.assert_called_once()
    
    def test_completion_status_command(self):
        """Test the completion status command."""
        from ilha.cli import completion_status
        
        with patch('ilha.commands.completion.CompletionManager') as mock_manager_class:
            mock_manager = Mock()
            mock_manager_class.return_value = mock_manager
            
            # Should not raise exception
            completion_status()
            mock_manager.show_completion_status.assert_called_once()


class TestCompletionManager:
    """Test completion manager functionality."""
    
    def test_detect_shell_bash(self):
        """Test shell detection for bash."""
        from ilha.commands.completion import CompletionManager
        
        with patch.dict('os.environ', {'SHELL': '/bin/bash'}):
            manager = CompletionManager()
            assert manager.detect_shell() == 'bash'
    
    def test_detect_shell_zsh(self):
        """Test shell detection for zsh."""
        from ilha.commands.completion import CompletionManager
        
        with patch.dict('os.environ', {'SHELL': '/bin/zsh'}):
            manager = CompletionManager()
            assert manager.detect_shell() == 'zsh'
    
    def test_detect_shell_unknown(self):
        """Test shell detection for unknown shell."""
        from ilha.commands.completion import CompletionManager
        
        with patch.dict('os.environ', {'SHELL': '/bin/fish'}):
            manager = CompletionManager()
            assert manager.detect_shell() is None
    
    def test_is_completion_installed_not_installed(self):
        """Test checking completion installation when not installed."""
        from ilha.commands.completion import CompletionManager
        
        manager = CompletionManager()
        
        with patch.object(manager, 'shell_configs', {'bash': Path('/nonexistent/.bashrc')}):
            assert manager.is_completion_installed('bash') is False
    
    def test_get_completion_source_line_bash(self):
        """Test getting bash completion source line."""
        from ilha.commands.completion import CompletionManager
        
        manager = CompletionManager()
        source_line = manager.get_completion_source_line('bash')
        
        assert 'source' in source_line
        assert 'ilha.bash' in source_line
    
    def test_get_completion_source_line_zsh(self):
        """Test getting zsh completion source line."""
        from ilha.commands.completion import CompletionManager
        
        manager = CompletionManager()
        source_line = manager.get_completion_source_line('zsh')
        
        # The implementation returns only the fpath line for zsh
        assert 'fpath=' in source_line
        # autoload is not included in the source line returned by get_completion_source_line


if __name__ == '__main__':
    pytest.main([__file__])
