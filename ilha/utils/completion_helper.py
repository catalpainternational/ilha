"""
Completion helper utilities for ilha CLI.

This module provides functions for both static and dynamic command completion.
"""

import subprocess
import re
from pathlib import Path
from typing import List, Set

from ..utils.path_utils import get_project_root


def get_main_commands() -> List[str]:
    """Get list of main ilha commands for completion."""
    return [
        'start-proxy', 'stop-proxy', 'start', 'stop', 'create', 'up', 'down', 'delete', 'remove',
        'remove-all', 'delete-all', 'list', 'prune', 'volumes', 'setup',
        'help', 'completion'
    ]


def get_volume_subcommands() -> List[str]:
    """Get list of volumes subcommands for completion."""
    return ['list', 'size', 'backup', 'restore', 'clean']


def get_completion_subcommands() -> List[str]:
    """Get list of completion subcommands for completion."""
    return ['install', 'uninstall', 'status']


def get_completion_flags() -> List[str]:
    """Get list of common flags for completion."""
    return ['--force', '-d', '--detach', '--help', '-h']


def get_worktree_names() -> List[str]:
    """Get list of existing worktree names for completion."""
    try:
        from ..core.git_manager import GitManager
        git_manager = GitManager()
        worktrees = git_manager.list_worktrees()
        return [worktree[2] for worktree in worktrees]  # Extract branch names
    except Exception:
        return []


def get_volume_branch_names() -> List[str]:
    """Get list of branch names that have associated volumes."""
    try:
        from ..core.docker_manager import DockerManager
        docker_manager = DockerManager()
        volumes = docker_manager.list_volumes()
        
        # Extract branch names from volume names
        # Volume names typically follow pattern: {branch_name}_{service}_data
        branch_names = set()
        for volume in volumes:
            # Remove common suffixes to extract branch name
            name = volume.replace('_postgres_data', '').replace('_redis_data', '').replace('_media_files', '')
            if '_' in name:
                # Take the first part as branch name
                branch_name = name.split('_')[0]
                if branch_name and not branch_name.startswith('ilha_'):
                    branch_names.add(branch_name)
        
        return list(branch_names)
    except Exception:
        return []


def get_all_branch_names() -> List[str]:
    """Get all branch names (worktrees + volumes) for completion."""
    worktree_names = set(get_worktree_names())
    volume_names = set(get_volume_branch_names())
    all_names = worktree_names.union(volume_names)
    return sorted(list(all_names))


def get_git_branch_names() -> List[str]:
    """Get list of git branch names for completion."""
    try:
        project_root = get_project_root()
        if not project_root:
            return []
        
        # Get all branches (local and remote)
        result = subprocess.run(
            ['git', 'branch', '-a'],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode != 0:
            return []
        
        branches = []
        for line in result.stdout.split('\n'):
            line = line.strip()
            if not line or line.startswith('*'):
                continue
            
            # Remove remote prefix and clean up
            if line.startswith('remotes/'):
                branch = line.replace('remotes/origin/', '').replace('remotes/', '')
            else:
                branch = line
            
            # Skip HEAD and other special refs
            if branch in ['HEAD', 'main', 'master'] or branch.startswith('origin/HEAD'):
                continue
            
            branches.append(branch)
        
        return sorted(list(set(branches)))
    except Exception:
        return []


def get_completion_for_context(context: str) -> List[str]:
    """Get completions for a specific context."""
    if context == 'worktrees':
        return get_worktree_names()
    elif context == 'volumes':
        return get_volume_branch_names()
    elif context == 'all':
        return get_all_branch_names()
    elif context == 'git':
        return get_git_branch_names()
    else:
        return []


def validate_completion_input(input_str: str) -> bool:
    """Validate completion input to prevent shell injection."""
    if not input_str:
        return False
    
    # Check for dangerous characters that could be used for shell injection
    dangerous_chars = [';', '&', '|', '`', '$', '(', ')', '<', '\n', '\r']
    
    for char in dangerous_chars:
        if char in input_str:
            return False
    
    return True


def get_safe_completions(completions: List[str]) -> List[str]:
    """Filter completions to remove potentially unsafe entries."""
    safe_completions = []
    for completion in completions:
        if validate_completion_input(completion):
            safe_completions.append(completion)
    return safe_completions


def print_completions(completions: List[str]) -> None:
    """Print completions to stdout for shell consumption."""
    safe_completions = get_safe_completions(completions)
    for completion in safe_completions:
        print(completion)
