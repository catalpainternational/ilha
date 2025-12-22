"""
Unit tests for confirmation utilities.
"""

import pytest
from unittest.mock import patch, Mock
import sys
from io import StringIO

from ilha.utils.confirmation import confirm_use_existing_worktree, confirm_deletion, confirm_batch_operation


class TestConfirmationUtilities:
    """Test confirmation utility functions."""
    
    def test_confirm_use_existing_worktree_user_confirms_yes(self):
        """Test confirm_use_existing_worktree when user enters 'y'."""
        with patch('builtins.input', return_value='y'):
            result = confirm_use_existing_worktree("test-branch")
            assert result == True
    
    def test_confirm_use_existing_worktree_user_confirms_yes_capital(self):
        """Test confirm_use_existing_worktree when user enters 'Y'."""
        with patch('builtins.input', return_value='Y'):
            result = confirm_use_existing_worktree("test-branch")
            assert result == True
    
    def test_confirm_use_existing_worktree_user_confirms_yes_word(self):
        """Test confirm_use_existing_worktree when user enters 'yes'."""
        with patch('builtins.input', return_value='yes'):
            result = confirm_use_existing_worktree("test-branch")
            assert result == True
    
    def test_confirm_use_existing_worktree_user_confirms_empty(self):
        """Test confirm_use_existing_worktree when user presses Enter (empty input)."""
        with patch('builtins.input', return_value=''):
            result = confirm_use_existing_worktree("test-branch")
            assert result == True
    
    def test_confirm_use_existing_worktree_user_declines_no(self):
        """Test confirm_use_existing_worktree when user enters 'n'."""
        with patch('builtins.input', return_value='n'):
            result = confirm_use_existing_worktree("test-branch")
            assert result == False
    
    def test_confirm_use_existing_worktree_user_declines_no_capital(self):
        """Test confirm_use_existing_worktree when user enters 'N'."""
        with patch('builtins.input', return_value='N'):
            result = confirm_use_existing_worktree("test-branch")
            assert result == False
    
    def test_confirm_use_existing_worktree_user_declines_no_word(self):
        """Test confirm_use_existing_worktree when user enters 'no'."""
        with patch('builtins.input', return_value='no'):
            result = confirm_use_existing_worktree("test-branch")
            assert result == False
    
    def test_confirm_use_existing_worktree_invalid_input_then_valid(self):
        """Test confirm_use_existing_worktree with invalid input followed by valid input."""
        with patch('builtins.input', side_effect=['invalid', 'y']):
            result = confirm_use_existing_worktree("test-branch")
            assert result == True
    
    def test_confirm_use_existing_worktree_keyboard_interrupt(self):
        """Test confirm_use_existing_worktree when user presses Ctrl+C."""
        with patch('builtins.input', side_effect=KeyboardInterrupt):
            result = confirm_use_existing_worktree("test-branch")
            assert result == False
    
    def test_confirm_use_existing_worktree_eof_error(self):
        """Test confirm_use_existing_worktree when stdin is not available."""
        with patch('builtins.input', side_effect=EOFError):
            result = confirm_use_existing_worktree("test-branch")
            assert result == False
    
    def test_confirm_deletion_empty_branches(self):
        """Test confirm_deletion with empty branch list."""
        result = confirm_deletion([])
        assert result == False
    
    def test_confirm_deletion_single_branch_user_confirms(self):
        """Test confirm_deletion with single branch and user confirms."""
        with patch('builtins.input', return_value='y'):
            result = confirm_deletion(["branch1"])
            assert result == True
    
    def test_confirm_deletion_multiple_branches_user_confirms(self):
        """Test confirm_deletion with multiple branches and user confirms."""
        with patch('builtins.input', return_value='y'):
            result = confirm_deletion(["branch1", "branch2", "branch3"])
            assert result == True
    
    def test_confirm_deletion_user_declines(self):
        """Test confirm_deletion when user declines."""
        with patch('builtins.input', return_value='n'):
            result = confirm_deletion(["branch1", "branch2"])
            assert result == False
    
    def test_confirm_deletion_remove_operation(self):
        """Test confirm_deletion with 'remove' operation."""
        with patch('builtins.input', return_value='y'):
            result = confirm_deletion(["branch1"], operation="remove")
            assert result == True
    
    def test_confirm_batch_operation_single_branch(self):
        """Test confirm_batch_operation with single branch (no confirmation needed)."""
        result = confirm_batch_operation(["branch1"])
        assert result == True
    
    def test_confirm_batch_operation_multiple_branches(self):
        """Test confirm_batch_operation with multiple branches."""
        with patch('ilha.utils.confirmation.confirm_deletion', return_value=True) as mock_confirm:
            result = confirm_batch_operation(["branch1", "branch2", "branch3"])
            assert result == True
            mock_confirm.assert_called_once_with(["branch1", "branch2", "branch3"], "delete")
    
    def test_confirm_batch_operation_remove_operation(self):
        """Test confirm_batch_operation with 'remove' operation."""
        with patch('ilha.utils.confirmation.confirm_deletion', return_value=True) as mock_confirm:
            result = confirm_batch_operation(["branch1", "branch2"], operation="remove")
            assert result == True
            mock_confirm.assert_called_once_with(["branch1", "branch2"], "remove")
