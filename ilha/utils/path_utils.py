"""
Path utilities for ilha CLI.

This module provides path resolution and compatibility functions.
"""

import os
from pathlib import Path
from typing import Optional, Tuple

from ..config.settings import get_project_root, get_worktree_paths, get_worktree_dir


def get_compose_override_path(worktree_path: Optional[Path] = None) -> Optional[Path]:
    """Get the path to the compose worktree file.
    
    Always returns worktrees/{branch}/.ilha/docker-compose.worktree.yml
    """
    if worktree_path is None:
        worktree_path = Path.cwd()

    # Always use worktree's .ilha directory
    compose_path = worktree_path / ".ilha" / "docker-compose.worktree.yml"
    return compose_path if compose_path.exists() else None


def get_worktree_branch_name(worktree_path: Optional[Path] = None) -> Optional[str]:
    """Get branch name from worktree directory path."""
    if worktree_path is None:
        worktree_path = Path.cwd()

    # First try to get the branch name from git
    try:
        import subprocess
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
            cwd=worktree_path
        )
        branch_name = result.stdout.strip()
        if branch_name:
            return branch_name
    except Exception:
        pass

    # Fallback: Extract branch name from directory path
    # /path/to/worktrees/test -> test
    # Use absolute path without resolving symlinks to avoid following git worktree structure
    absolute_path = worktree_path.absolute()
    branch_name = absolute_path.name
    return branch_name if branch_name else None


def resolve_worktree_path(branch_name: str) -> Tuple[Path, Path]:
    """Resolve worktree paths for a branch (new and legacy)."""
    return get_worktree_paths(branch_name)


def ensure_main_repo() -> Path:
    """Ensure we're in the main repository directory."""
    project_root = get_project_root()
    if Path.cwd() != project_root:
        os.chdir(project_root)
    return project_root


def get_relative_path(path: Path, base: Optional[Path] = None) -> str:
    """Get relative path from base directory."""
    if base is None:
        base = Path.cwd()
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def normalize_path(path: str) -> Path:
    """Normalize a path string to a Path object."""
    return Path(path).resolve()


def ensure_directory_exists(path: Path) -> None:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)


def get_env_file_path(worktree_path: Path) -> Path:
    """Get the path to the .env file in a worktree."""
    return worktree_path / ".env"


def get_env_compose_file_path(worktree_path: Path) -> Path:
    """Get the path to the env.ilha file in a worktree's .ilha directory."""
    return worktree_path / ".ilha" / "env.ilha"


def copy_env_file(source_path: Path, target_path: Path) -> bool:
    """Copy .env file from source to target if it exists."""
    source_env = get_env_file_path(source_path)
    target_env = get_env_file_path(target_path)
    
    if source_env.exists():
        import shutil
        try:
            # Resolve both paths to detect if they're the same file
            resolved_source = source_env.resolve()
            resolved_target = target_env.resolve()
            
            # If source and target are the same file, no need to copy
            if resolved_source == resolved_target:
                # File is already in place
                return True
            
            shutil.copy2(source_env, target_env)
            # Verify the copy was successful
            if target_env.exists():
                return True
            else:
                return False
        except Exception as e:
            from ..utils.logging import log_warning
            log_warning(f"Failed to copy .env file: {e}")
            return False
    return False


def get_compose_file_path(worktree_path: Path, compose_type: str = "worktree") -> Path:
    """Get the path to a compose file."""
    if compose_type == "worktree":
        return worktree_path / "ilha" / "config" / "docker-compose.worktree.yml"
    elif compose_type == "global-caddy":
        return worktree_path / "ilha" / "config" / "docker-compose.global-caddy.yml"
    else:
        raise ValueError(f"Unknown compose type: {compose_type}")


def get_caddyfile_path(worktree_path: Path) -> Path:
    """Get the path to the Caddyfile."""
    return worktree_path / "ilha" / "Caddyfile.ilha"


def is_worktree_directory(path: Path) -> bool:
    """Check if a path is a worktree directory."""
    from .file_utils import find_compose_files
    
    # Check for any compose file
    compose_files = find_compose_files(path)
    if not compose_files:
        return False
    
    # Check for ilha directory
    if not (path / "ilha").exists():
        return False
    
    return True


def find_worktree_directories(base_path: Optional[Path] = None) -> list[Path]:
    """Find all worktree directories under a base path."""
    if base_path is None:
        base_path = get_project_root()
    
    worktrees = []
    worktree_dir = base_path / "ilha" / "worktrees"
    
    if worktree_dir.exists():
        for item in worktree_dir.iterdir():
            if item.is_dir() and is_worktree_directory(item):
                worktrees.append(item)
    
    return worktrees


def detect_execution_context() -> tuple[Optional[Path], Optional[str], bool]:
    """Detect if we're running from git root or worktree directory.
    
    Returns:
        tuple: (worktree_path, branch_name, is_worktree_context)
        - worktree_path: Path to worktree if in worktree, None if in git root
        - branch_name: Branch name if in worktree, None if in git root  
        - is_worktree_context: True if in worktree, False if in git root
    """
    current_path = Path.cwd()
    
    # Primary detection: check if we have a local .ilha/config.yml
    # This indicates we're in a fractal worktree or project root
    if (current_path / ".ilha" / "config.yml").exists():
        # Check if this is a worktree by looking at git worktree list
        # or by checking if we're under a "worktrees" directory
        try:
            import subprocess
            result = subprocess.run(["git", "worktree", "list"], 
                                 capture_output=True, text=True, check=True, cwd=current_path)
            # If we can run git worktree list from here, we're in a git repo
            # Check if current path is listed as a worktree
            if f"[{current_path.name}]" in result.stdout:
                branch_name = get_worktree_branch_name(current_path)
                return current_path, branch_name, True
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        
        # Check if we're under a worktrees directory by path
        if "worktrees" in str(current_path):
            branch_name = get_worktree_branch_name(current_path)
            return current_path, branch_name, True
    
    # Fallback: use the old method with project_root detection
    project_root = get_project_root()
    worktree_dir = get_worktree_dir()
    worktrees_path = project_root / worktree_dir
    
    # Check if current directory is under worktrees
    try:
        current_path.relative_to(worktrees_path)
        # We're in a worktree directory
        branch_name = get_worktree_branch_name(current_path)
        return current_path, branch_name, True
    except ValueError:
        # Not in worktrees directory, we're in git root
        return None, None, False


def get_parent_project_root() -> Optional[Path]:
    """Get the parent project root directory when running from a worktree.
    
    This function detects if we're in a worktree context and finds the parent
    project root (the directory containing the worktrees/ directory).
    
    Returns:
        Path to parent project root if in worktree, None otherwise
    """
    worktree_path, branch_name, is_worktree = detect_execution_context()
    
    if not is_worktree or worktree_path is None:
        return None
    
    # Walk up from the worktree path to find the directory containing worktrees/
    worktree_dir_name = get_worktree_dir()
    
    current = worktree_path.resolve()
    
    # Check each parent directory for the worktrees directory
    for parent in current.parents:
        worktrees_path = parent / worktree_dir_name
        if worktrees_path.exists() and worktrees_path.is_dir():
            # Found the parent project root
            return parent
    
    return None
