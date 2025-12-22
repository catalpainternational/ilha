"""
User confirmation utilities for ilha CLI.

This module provides functions for prompting user confirmation with default values.
"""

import sys
from typing import List

from .logging import log_info, log_warning


def _prompt_with_default(message: str, default: str = "Y", eof_message: str = "Cannot prompt for confirmation (no stdin available). Use --force to skip confirmation.") -> bool:
    """Internal helper for prompting user with default value.
    
    Args:
        message: The prompt message to display (should include [Y/n] or similar)
        default: Default value ('Y' or 'N')
        eof_message: Message to show when stdin is not available
        
    Returns:
        True if user confirms (or default is 'Y' and user presses Enter), False otherwise
    """
    try:
        # Add space only if message doesn't end with colon or space
        prompt = message if (message.endswith(':') or message.endswith(': ')) else f"{message} "
        response = input(prompt).strip().lower()
        
        # Handle empty response (Enter key) - use default
        if not response:
            return default.upper() == 'Y'
        
        # Handle explicit responses
        if response in ['y', 'yes']:
            return True
        elif response in ['n', 'no']:
            return False
        else:
            log_warning("Invalid response. Please enter 'y' for yes or 'n' for no.")
            return _prompt_with_default(message, default, eof_message)  # Retry
            
    except KeyboardInterrupt:
        log_info("\nOperation cancelled by user")
        return False
    except EOFError:
        # Handle case where stdin is not available (e.g., in automated scripts)
        log_warning(eof_message)
        return False


def confirm_deletion(branches: List[str], operation: str = "delete") -> bool:
    """Prompt user for confirmation when deleting multiple branches/worktrees.
    
    Args:
        branches: List of branch names that will be deleted
        operation: The operation being performed ("delete" or "remove")
        
    Returns:
        True if user confirms, False if user cancels
    """
    if not branches:
        return False
    
    count = len(branches)
    branch_list = ', '.join(sorted(branches))
    
    # Show what will be deleted
    log_info(f"Found {count} matching branch(es): {branch_list}")
    
    # Create operation-specific message
    if operation == "delete":
        message = f"Delete {count} branch(es) and their worktrees? [Y/n]:"
    else:  # remove
        message = f"Remove {count} worktree(s) (keep branches)? [Y/n]:"
    
    # Prompt for confirmation with default 'Y'
    return _prompt_with_default(message, default="Y", eof_message="Cannot prompt for confirmation (no stdin available). Use --force to skip confirmation.")


def confirm_batch_operation(branches: List[str], operation: str = "delete") -> bool:
    """Confirm a batch operation with appropriate messaging.
    
    Args:
        branches: List of branch names that will be processed
        operation: The operation being performed ("delete" or "remove")
        
    Returns:
        True if user confirms, False if user cancels
    """
    if len(branches) <= 1:
        return True  # No confirmation needed for single branch
    
    return confirm_deletion(branches, operation)


def confirm_use_existing_worktree(branch_name: str) -> bool:
    """Prompt user for confirmation when a worktree already exists.
    
    Args:
        branch_name: Name of the branch/worktree that already exists
        
    Returns:
        True if user confirms to use existing worktree, False if user wants to use different name
    """
    log_warning(f"Branch/worktree '{branch_name}' already exists.")
    
    # Prompt for confirmation with default 'Y'
    return _prompt_with_default("Use existing worktree? [Y/n]:", default="Y", eof_message="Cannot prompt for confirmation (no stdin available). Use --force to skip confirmation.")


def confirm_action(message: str) -> bool:
    """Prompt user for confirmation of a generic action.
    
    Args:
        message: The confirmation message to display (should include [Y/n] or similar)
        
    Returns:
        True if user confirms, False if user cancels
    """
    # Ensure message has prompt format if not already included
    if "[Y/n]" not in message and "[y/N]" not in message:
        message = f"{message} [Y/n]:"
    
    return _prompt_with_default(message, default="Y", eof_message="Cannot prompt for confirmation (no stdin available).")


def confirm_by_typing_name(expected_name: str, message: str) -> bool:
    """Require user to type the exact name to confirm a destructive action.
    
    Args:
        expected_name: The exact name the user must type
        message: Descriptive message explaining what will happen
        
    Returns:
        True if user types the name correctly, False otherwise
    """
    try:
        log_warning(message)
        response = input(f"Type the droplet name '{expected_name}' to confirm: ").strip()
        
        # Check if the typed name matches exactly (case-sensitive)
        if response == expected_name:
            return True
        elif not response:
            log_warning("No name entered. Operation cancelled.")
            return False
        else:
            log_warning(f"Name mismatch. Expected '{expected_name}', got '{response}'. Operation cancelled.")
            return False
            
    except KeyboardInterrupt:
        log_info("\nOperation cancelled by user")
        return False
    except EOFError:
        # Handle case where stdin is not available (e.g., in automated scripts)
        log_warning("Cannot prompt for confirmation (no stdin available). Use --force to skip confirmation.")
        return False
