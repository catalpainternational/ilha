"""
Validation utilities for ilha CLI.

This module provides validation functions for various inputs and system states.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, List, Tuple

from .logging import error_exit, fail_prerequisite, log_error
from ..config.settings import BRANCH_NAME_PATTERN, PROTECTED_BRANCHES

# Bound `docker info` / `docker compose version` so wedged daemons cannot hang the CLI indefinitely.
DEFAULT_DOCKER_HEALTH_TIMEOUT_SECONDS = 20

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


def check_docker_daemon_responsive(
    timeout_seconds: int = DEFAULT_DOCKER_HEALTH_TIMEOUT_SECONDS,
) -> Tuple[bool, Optional[str]]:
    """Return (True, None) if the Docker daemon responds; else (False, reason)."""
    if not shutil.which("docker"):
        return False, "The `docker` command was not found on PATH."
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"`docker info` timed out after {timeout_seconds}s (daemon not responding or still starting).",
        )
    except OSError as exc:
        return False, f"Could not run `docker info`: {exc}"
    if result.returncode == 0:
        return True, None
    blob = (result.stderr or result.stdout or "").strip()
    first = blob.splitlines()[0] if blob else "no output from docker"
    if len(first) > 200:
        first = first[:197] + "..."
    return False, f"`docker info` failed (exit {result.returncode}): {first}"


def validate_docker_running(timeout_seconds: int = DEFAULT_DOCKER_HEALTH_TIMEOUT_SECONDS) -> bool:
    """Check if Docker is running (bounded; avoids hang on unresponsive daemon)."""
    ok, _ = check_docker_daemon_responsive(timeout_seconds)
    return ok


def check_docker_compose_responsive(
    timeout_seconds: int = DEFAULT_DOCKER_HEALTH_TIMEOUT_SECONDS,
) -> Tuple[bool, Optional[str]]:
    """Return (True, None) if compose CLI works; else (False, reason)."""
    try:
        r = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if r.returncode == 0:
            return True, None
        err = (r.stderr or r.stdout or "").strip()
        compose_hint = err.splitlines()[0][:200] if err else "unknown error"
    except subprocess.TimeoutExpired:
        return (
            False,
            f"`docker compose version` timed out after {timeout_seconds}s.",
        )
    except OSError as exc:
        compose_hint = str(exc)
    except (subprocess.CalledProcessError, FileNotFoundError, Exception):
        compose_hint = "docker compose failed"

    try:
        r2 = subprocess.run(
            ["docker-compose", "version"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if r2.returncode == 0:
            return True, None
    except subprocess.TimeoutExpired:
        return (
            False,
            f"Neither `docker compose` nor `docker-compose` responded within {timeout_seconds}s.",
        )
    except OSError:
        pass

    return (
        False,
        "Neither `docker compose version` nor `docker-compose version` succeeded. "
        f"(docker compose: {compose_hint})",
    )


def validate_docker_compose(timeout_seconds: int = DEFAULT_DOCKER_HEALTH_TIMEOUT_SECONDS) -> bool:
    """Check if Docker Compose is available (bounded)."""
    ok, _ = check_docker_compose_responsive(timeout_seconds)
    return ok


def validate_worktree_directory(worktree_path: Path, project_root: Optional[Path] = None) -> bool:
    """Validate that we're in a valid ilha worktree directory.

    Presence of ``.ilha/docker-compose.overlay.yml`` is the canonical signal
    that ``ilha setup`` has been run for this worktree.
    """
    overlay_path = worktree_path / ".ilha" / "docker-compose.overlay.yml"
    return overlay_path.exists()


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
    except Exception:
        return None


def validate_branch_merged(branch_name: str, project_root: Optional[Path] = None) -> bool:
    """Check whether *branch_name* has been merged into HEAD.

    Non-destructive: uses ``git merge-base --is-ancestor`` (exit 0 when the
    branch tip is an ancestor of HEAD, i.e. fully merged). This is only a
    predicate — it must never mutate the repository. (It previously ran
    ``git branch -d``, which *deletes* a merged branch as a side effect, so
    merely reading branch info could destroy a branch.)
    """
    try:
        if project_root is None:
            from ..config.settings import get_project_root
            project_root = get_project_root()
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch_name, "HEAD"],
            capture_output=True, cwd=project_root
        )
        return result.returncode == 0
    except Exception:
        return False


def _check_docker_prerequisites() -> None:
    """Fail with guidance if the Docker daemon or Compose CLI is not usable.

    Single source of truth for the Docker/Compose prerequisite checks shared by
    ``check_prerequisites`` and ``check_prerequisites_no_git`` (previously ~40
    copy-pasted lines in each).
    """
    timeout = DEFAULT_DOCKER_HEALTH_TIMEOUT_SECONDS
    ok, reason = check_docker_daemon_responsive(timeout_seconds=timeout)
    if not ok:
        fail_prerequisite(
            "Docker is required for this command, but the daemon is not usable right now.",
            hints=[
                reason,
                "Start Docker Desktop (macOS/Windows) or the Docker service (Linux) and wait until it is fully running.",
                f"If Docker is slow to start, wait and retry (health check timeout: {timeout}s).",
                "Sanity check in a terminal: `docker info`",
            ],
        )

    ok, reason = check_docker_compose_responsive(timeout_seconds=timeout)
    if not ok:
        fail_prerequisite(
            "Docker Compose is required for this command but is missing or failing.",
            hints=[
                reason,
                "Install Docker Compose v2 (`docker compose`) or the legacy `docker-compose` CLI.",
                f"Sanity check: `docker compose version` (timeout: {timeout}s)",
            ],
        )


def check_prerequisites(project_root: Optional[Path] = None) -> None:
    """Check all prerequisites (git repo + Docker/Compose) and exit if any fail."""
    if not validate_git_repository(project_root):
        fail_prerequisite(
            "This command must be run from a git repository (project root).",
            hints=[
                "Change directory to your project root or an ilha worktree, then retry.",
                "Example: `cd path/to/repo` or `cd worktrees/<branch>`.",
            ],
        )
    _check_docker_prerequisites()


def check_prerequisites_no_git() -> None:
    """Check Docker prerequisites only (skip git validation)."""
    _check_docker_prerequisites()


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
        # Anchor the name filter (^/name$) so 'web' does not match 'web2' /
        # 'myproj-web-old' — matching validate_container_exists. A bare
        # substring filter reported unrelated containers as running.
        result = subprocess.run(["docker", "ps", "--filter", f"name=^/{container_name}$",
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
        
        # Main compose often uses directory name (e.g. business_intelligence-db); we may have sanitized (business-intelligence)
        project_name_alt = project_name.replace("-", "_") if "-" in project_name else project_name.replace("_", "-")
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split('|')
                if len(parts) >= 2:
                    container_name, image = parts[0], parts[1]
                    is_postgres = 'postgres' in image.lower() or 'postgres' in container_name.lower()
                    matches = project_name in container_name or project_name_alt in container_name
                    if is_postgres and matches:
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
