"""
Validation utilities for ilha CLI.

This module provides validation functions for various inputs and system states.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, List

from .logging import log_error, error_exit
from ..config.settings import BRANCH_NAME_PATTERN, PROTECTED_BRANCHES

# Reserved command names that cannot be used as worktree names
RESERVED_COMMAND_NAMES = {
    'start-proxy', 'stop-proxy', 'start', 'stop', 'create', 'up', 'down', 
    'delete', 'remove', 'remove-all', 'delete-all', 'list', 'prune', 
    'volumes', 'setup', 'help', 'completion', '-D', '-r'
}


def validate_branch_name(branch_name: str) -> bool:
    """Validate branch name format."""
    if not branch_name:
        return False
    return bool(re.match(BRANCH_NAME_PATTERN, branch_name))


def validate_worktree_name_not_reserved(branch_name: str) -> bool:
    """Validate that worktree name is not a reserved command name."""
    if not branch_name:
        return False
    return branch_name not in RESERVED_COMMAND_NAMES


def validate_git_repository(project_root: Optional[Path] = None) -> bool:
    """Check if we're in a git repository."""
    try:
        if project_root is None:
            from ..config.settings import get_project_root
            project_root = get_project_root()
        subprocess.run(["git", "rev-parse", "--git-dir"], 
                      capture_output=True, check=True, cwd=project_root)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, Exception):
        return False


def validate_docker_running() -> bool:
    """Check if Docker is running."""
    try:
        subprocess.run(["docker", "info"], 
                      capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, Exception):
        return False


def validate_docker_compose() -> bool:
    """Check if Docker Compose is available."""
    try:
        subprocess.run(["docker", "compose", "version"], 
                      capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, Exception):
        try:
            subprocess.run(["docker-compose", "version"], 
                          capture_output=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, Exception):
            return False


def validate_worktree_directory(worktree_path: Path, project_root: Optional[Path] = None) -> bool:
    """Validate that we're in a valid worktree directory."""
    # Check for compose worktree file in worktree's .ilha directory
    compose_worktree_path = worktree_path / ".ilha" / "docker-compose.worktree.yml"
    return compose_worktree_path.exists()


def validate_compose_override_exists(branch_name: str, project_root: Optional[Path] = None) -> bool:
    """Check if compose override file exists for a worktree."""
    from ..core.git_manager import GitManager
    from ..utils.path_utils import get_compose_override_path
    
    # Get worktree path
    git_manager = GitManager(project_root=project_root)
    worktree_path = git_manager.find_worktree_path(branch_name)
    if not worktree_path:
        return False
    
    # Check for compose override file
    compose_override_path = get_compose_override_path(worktree_path)
    return compose_override_path and compose_override_path.exists()


def validate_branch_protection(branch_name: str) -> bool:
    """Check if branch is protected from deletion."""
    return branch_name in PROTECTED_BRANCHES


def validate_branch_exists(branch_name: str, project_root: Optional[Path] = None) -> bool:
    """Check if a git branch exists."""
    try:
        if project_root is None:
            from ..config.settings import get_project_root
            project_root = get_project_root()
        subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], 
                      capture_output=True, check=True, cwd=project_root)
        return True
    except (subprocess.CalledProcessError, Exception):
        return False


def validate_worktree_exists(branch_name: str, project_root: Optional[Path] = None) -> bool:
    """Check if a worktree exists for the given branch."""
    try:
        if project_root is None:
            from ..config.settings import get_project_root
            project_root = get_project_root()
        result = subprocess.run(["git", "worktree", "list"], 
                              capture_output=True, text=True, check=True, cwd=project_root)
        return f"[{branch_name}]" in result.stdout
    except (subprocess.CalledProcessError, Exception):
        return False


def validate_current_branch(project_root: Optional[Path] = None) -> Optional[str]:
    """Get the current git branch name."""
    try:
        if project_root is None:
            from ..config.settings import get_project_root
            project_root = get_project_root()
        result = subprocess.run(["git", "branch", "--show-current"], 
                              capture_output=True, text=True, check=True, cwd=project_root)
        return result.stdout.strip()
    except (subprocess.CalledProcessError, Exception):
        return None


def validate_branch_merged(branch_name: str, project_root: Optional[Path] = None) -> bool:
    """Check if a branch has been merged."""
    try:
        if project_root is None:
            from ..config.settings import get_project_root
            project_root = get_project_root()
        subprocess.run(["git", "branch", "-d", branch_name], 
                      capture_output=True, check=True, cwd=project_root)
        return True
    except (subprocess.CalledProcessError, Exception):
        return False


def check_prerequisites(project_root: Optional[Path] = None) -> None:
    """Check all prerequisites and exit if any fail."""
    if not validate_git_repository(project_root):
        error_exit("Not in a git repository. Please run this command from the project root.")
    
    if not validate_docker_running():
        error_exit("Docker is not running. Please start Docker and try again.")
    
    if not validate_docker_compose():
        error_exit("Docker Compose is not available. Please install Docker Compose.")


def check_prerequisites_no_git() -> None:
    """Check Docker prerequisites only (skip git validation)."""
    if not validate_docker_running():
        error_exit("Docker is not running. Please start Docker and try again.")
    
    if not validate_docker_compose():
        error_exit("Docker Compose is not available. Please install Docker Compose.")


def validate_volume_exists(volume_name: str) -> bool:
    """Check if a Docker volume exists."""
    try:
        subprocess.run(["docker", "volume", "inspect", volume_name], 
                      capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, Exception):
        return False


def validate_network_exists(network_name: str) -> bool:
    """Check if a Docker network exists."""
    try:
        subprocess.run(["docker", "network", "inspect", network_name], 
                      capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, Exception):
        return False


def validate_container_running(container_name: str) -> bool:
    """Check if a Docker container is running."""
    try:
        result = subprocess.run(["docker", "ps", "--filter", f"name={container_name}", 
                               "--format", "{{.Status}}"], 
                              capture_output=True, text=True, check=True)
        return "Up" in result.stdout
    except (subprocess.CalledProcessError, Exception):
        return False


def validate_container_exists(container_name: str) -> bool:
    """Check if a Docker container exists (running or stopped)."""
    try:
        result = subprocess.run(["docker", "ps", "-a", "--filter", f"name=^/{container_name}$", 
                               "--format", "{{.Names}}"], 
                              capture_output=True, text=True, check=True)
        return container_name in result.stdout.strip()
    except (subprocess.CalledProcessError, Exception):
        return False


def validate_environment_files(worktree_path: Path) -> bool:
    """Validate that both .env and env.ilha files exist in worktree."""
    from ..utils.path_utils import get_env_file_path, get_env_compose_file_path
    
    env_file = get_env_file_path(worktree_path)
    env_compose_file = get_env_compose_file_path(worktree_path)
    
    return env_file.exists() and env_compose_file.exists()


def validate_environment_file_content(env_file_path: Path) -> bool:
    """Validate that an environment file has content."""
    if not env_file_path.exists():
        return False
    
    try:
        content = env_file_path.read_text()
        return len(content.strip()) > 0
    except Exception:
        return False


def ensure_environment_files_exist(worktree_path: Path, branch_name: str, project_root: Optional[Path] = None) -> bool:
    """Ensure both environment files exist, creating them if necessary."""
    from ..core.environment_manager import EnvironmentManager
    from ..utils.logging import log_info, log_warning
    from ..utils.path_utils import get_env_file_path, get_env_compose_file_path
    
    env_manager = EnvironmentManager(project_root=project_root)
    
    # Check if both files exist and have content
    env_file = get_env_file_path(worktree_path)
    env_compose_file = get_env_compose_file_path(worktree_path)
    
    env_exists = env_file.exists() and validate_environment_file_content(env_file)
    env_compose_exists = env_compose_file.exists() and validate_environment_file_content(env_compose_file)
    
    if env_exists and env_compose_exists:
        return True
    
    log_info("Creating missing environment files...")
    
    # Create environment files
    success = env_manager.create_worktree_env(branch_name, worktree_path)
    
    if success:
        # Verify both files exist after creation
        return validate_environment_files(worktree_path)
    else:
        log_warning("Failed to create environment files")
        return False


def get_containers_using_volume(volume_name: str) -> List[str]:
    """Find all containers using a specific volume."""
    try:
        result = subprocess.run([
            "docker", "ps", "-a", "--filter", f"volume={volume_name}",
            "--format", "{{.Names}}"
        ], capture_output=True, text=True, check=True)
        
        containers = result.stdout.strip().split('\n')
        return [c for c in containers if c.strip()]
    except (subprocess.CalledProcessError, Exception):
        return []


def are_containers_running(container_names: List[str]) -> bool:
    """Check if any of the specified containers are running."""
    if not container_names:
        return False
    
    try:
        # Check each container
        for container_name in container_names:
            if validate_container_running(container_name):
                return True
        return False
    except Exception:
        return False


def get_postgres_container_for_volume(volume_name: str, project_name: str) -> Optional[str]:
    """Find the PostgreSQL container using a volume for a specific project."""
    try:
        # Look for containers with postgres in the name and using the volume
        result = subprocess.run([
            "docker", "ps", "-a", "--filter", f"volume={volume_name}",
            "--format", "{{.Names}}|{{.Image}}"
        ], capture_output=True, text=True, check=True)
        
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split('|')
                if len(parts) >= 2:
                    container_name, image = parts[0], parts[1]
                    # Check if it's a postgres container and matches project
                    if ('postgres' in image.lower() or 'postgres' in container_name.lower()) and project_name in container_name:
                        return container_name
        return None
    except (subprocess.CalledProcessError, Exception):
        return None


def check_setup_or_prompt(project_root: Optional[Path] = None) -> None:
    """Check if ilha is set up, prompt user if not."""
    from ..commands.setup import SetupManager
    from ..utils.logging import log_info
    
    setup_manager = SetupManager(project_root=project_root)
    if not setup_manager.is_setup_complete():
        log_error("ilha is not set up for this project.")
        log_info("Please run: ilha setup")
        error_exit("Setup required")
