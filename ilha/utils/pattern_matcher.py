"""
Pattern matching utilities for ilha CLI.

This module provides wildcard pattern matching functionality for branch names.
"""

import fnmatch
import re
from typing import List, Set

from ..config.settings import PROTECTED_BRANCHES
from .logging import log_info, log_warning


def has_wildcard(pattern: str) -> bool:
    """Check if a string contains wildcard characters.
    
    Args:
        pattern: The string to check for wildcard characters
        
    Returns:
        True if the string contains wildcard characters (*, ?, [, ])
    """
    return bool(re.search(r'[*?\[\]]', pattern))


def match_branches(pattern: str, branches: List[str]) -> List[str]:
    """Match branch names against a wildcard pattern (case-insensitive).
    
    Args:
        pattern: The wildcard pattern to match against
        branches: List of branch names to match
        
    Returns:
        List of branch names that match the pattern
    """
    matches = []
    pattern_lower = pattern.lower()
    
    for branch in branches:
        if fnmatch.fnmatch(branch.lower(), pattern_lower):
            matches.append(branch)
    
    return matches


def filter_protected_branches(branches: List[str]) -> List[str]:
    """Filter out protected branches from a list.
    
    Args:
        branches: List of branch names to filter
        
    Returns:
        List of branch names excluding protected branches
    """
    return [branch for branch in branches if branch not in PROTECTED_BRANCHES]


def filter_current_branch(branches: List[str], current_branch: str) -> List[str]:
    """Filter out the current branch from a list.
    
    Args:
        branches: List of branch names to filter
        current_branch: The currently checked out branch
        
    Returns:
        List of branch names excluding the current branch
    """
    if not current_branch:
        return branches
    
    return [branch for branch in branches if branch != current_branch]


def get_matching_branches(pattern: str, all_branches: List[str], current_branch: str = None) -> List[str]:
    """Get all branches matching a pattern, filtering out protected and current branches.
    
    Args:
        pattern: The wildcard pattern to match against
        all_branches: List of all available branch names
        current_branch: The currently checked out branch (optional)
        
    Returns:
        List of matching branch names (filtered)
    """
    # Get all matches
    matches = match_branches(pattern, all_branches)
    
    if not matches:
        return []
    
    # Filter out protected branches
    filtered_matches = filter_protected_branches(matches)
    
    # Filter out current branch if provided
    if current_branch:
        filtered_matches = filter_current_branch(filtered_matches, current_branch)
    
    # Log any filtered branches
    protected_filtered = set(matches) - set(filtered_matches)
    if protected_filtered:
        protected_list = ', '.join(sorted(protected_filtered))
        log_warning(f"Excluded protected/current branches: {protected_list}")
    
    return filtered_matches
