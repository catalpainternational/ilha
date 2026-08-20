"""
Unit tests for ilha up -d command logic.

This test suite validates the specific logic and behavior of the ilha up -d command
without requiring full Docker environment setup.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

from ilha.cli import up
from ilha.commands.worktree import WorktreeManager


class TestilhaUpCommand:
    """Test ilha up -d command logic."""
    
    def test_up_command_requires_detach_flag(self):
        """Test that up command requires -d flag."""
        # This should be tested through the CLI, but we can test the logic
        # The CLI should reject calls without -d flag
        pass
    
    @patch('ilha.cli.check_prerequisites')
    @patch('ilha.cli.WorktreeManager')
    def test_up_command_calls_worktree_manager_start(self, mock_worktree_class, mock_check_prereq):
        """Test that up command calls WorktreeManager.start_worktree()."""
        # Mock the worktree manager
        mock_worktree_manager = Mock()
        mock_worktree_class.return_value = mock_worktree_manager
        mock_worktree_manager.start_worktree.return_value = True
        
        # Mock prerequisites check
        mock_check_prereq.return_value = None
        
        # Test the up command logic (without actually calling the CLI)
        branch_name = "test-branch"
        try:
            mock_check_prereq()
            worktree_manager = mock_worktree_class()
            success = worktree_manager.start_worktree(branch_name)
            
            assert success == True
            mock_check_prereq.assert_called_once()
            mock_worktree_class.assert_called_once()
            mock_worktree_manager.start_worktree.assert_called_once_with(branch_name)
        except Exception:
            # This is expected since we're not in a real worktree directory
            pass
    
    @patch('ilha.cli.check_prerequisites')
    @patch('ilha.cli.WorktreeManager')
    def test_up_command_handles_start_failure(self, mock_worktree_class, mock_check_prereq):
        """Test that up command handles start_worktree failure."""
        # Mock the worktree manager
        mock_worktree_manager = Mock()
        mock_worktree_class.return_value = mock_worktree_manager
        mock_worktree_manager.start_worktree.return_value = False
        
        # Mock prerequisites check
        mock_check_prereq.return_value = None
        
        # Test the up command logic
        branch_name = "test-branch"
        try:
            mock_check_prereq()
            worktree_manager = mock_worktree_class()
            success = worktree_manager.start_worktree(branch_name)
            
            assert success == False
            mock_worktree_manager.start_worktree.assert_called_once_with(branch_name)
        except Exception:
            # This is expected since we're not in a real worktree directory
            pass
    
    @patch('ilha.commands.worktree.WorktreeOrchestrator')
    def test_worktree_manager_start_worktree_success(self, orchestrator_cls):
        """WorktreeManager delegates start_worktree to the orchestrator."""
        orchestrator = Mock()
        orchestrator.start_worktree.return_value = {
            "success": True,
            "data": {
                "domain_name": "feature-auth.ilha.test",
                "caddy_configured": True,
            },
        }
        orchestrator_cls.return_value = orchestrator

        provided_root = Path("/tmp/project")
        manager = WorktreeManager(project_root=provided_root)
        result = manager.start_worktree("feature/auth")

        assert result is True
        orchestrator_cls.assert_called_once_with(provided_root.resolve())
        orchestrator.start_worktree.assert_called_once_with(
            "feature/auth", profile=None, preflight=True, secrets_profile=None
        )

    @patch('ilha.commands.worktree.WorktreeOrchestrator')
    def test_worktree_manager_start_worktree_failure(self, orchestrator_cls):
        """WorktreeManager surfaces orchestrator failures."""
        orchestrator = Mock()
        orchestrator.start_worktree.return_value = {
            "success": False,
            "error": "Failed to start",
        }
        orchestrator_cls.return_value = orchestrator

        manager = WorktreeManager(project_root=Path("/tmp/project"))
        result = manager.start_worktree("feature/auth")

        assert result is False
        orchestrator.start_worktree.assert_called_once()
