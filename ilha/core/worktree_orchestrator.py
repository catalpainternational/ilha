"""
Core worktree orchestration for ilha.

This module provides the core orchestration logic that both CLI and MCP interfaces use.
It contains all the business logic for worktree operations without any interface-specific
formatting or presentation concerns.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config.settings import get_project_root, get_script_dir, get_project_name, sanitize_project_name
from ..core.docker_manager import DockerManager
from ..core.git_manager import GitManager
from ..core.environment_manager import EnvironmentManager
from ..utils.path_utils import (
    get_compose_override_path, 
    get_worktree_branch_name,
    ensure_main_repo
)
from ..utils.validation import validate_branch_exists, validate_worktree_name_not_reserved
from ..utils.pattern_matcher import get_matching_branches
from ..utils.confirmation import confirm_batch_operation
from ..utils.logging import set_mcp_mode, log_info, log_success, log_warning, log_error


class WorktreeOrchestrator:
    """Core worktree orchestration - used by both CLI and MCP."""
    
    def __init__(self, project_root: Optional[Path] = None, mcp_mode: bool = False):
        """Initialize worktree orchestrator."""
        if project_root is None:
            # Detect if we're in a worktree context for fractal operation
            from ..utils.path_utils import detect_execution_context
            worktree_path, branch_name, is_worktree = detect_execution_context()
            
            if is_worktree and worktree_path:
                # Use worktree as project root for fractal operation
                self.project_root = worktree_path
            else:
                # Use standard project root detection
                self.project_root = get_project_root()
        else:
            self.project_root = Path(project_root).resolve()
        
        # Enable MCP mode to suppress stdout logging
        if mcp_mode:
            set_mcp_mode(True)
        
        # Store mcp_mode as instance attribute for use in methods
        self.mcp_mode = mcp_mode
        
        self.git_manager = GitManager(project_root=self.project_root, validate=False)
        self.docker_manager = DockerManager(project_root=self.project_root)
        self.env_manager = EnvironmentManager(project_root=self.project_root)
    
    def _get_project_name(self) -> str:
        """Get project name from config using instance project_root.
        
        Ensures we use the correct project_root context from the orchestrator instance.
        """
        # If we have a project_root with config, use it directly
        from ..config.settings import ilha_DIR
        config_path = self.project_root / ilha_DIR / "config.yml"
        if config_path.exists():
            try:
                import yaml
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                    project_name = config.get("project_name")
                    if project_name:
                        return project_name
            except Exception:
                pass
        
        # Fallback to project root directory name (sanitized)
        from ..config.settings import sanitize_project_name
        return sanitize_project_name(self.project_root.name)
    
    def _find_true_project_root(self) -> Optional[Path]:
        """Find the true project root, respecting fractal worktree configs."""
        current = self.project_root
        
        # If current project_root has config.yml, it's the root (fractal or main)
        if (current / ".ilha" / "config.yml").exists():
            return current
        
        # Search upward for parent config
        for parent in current.parents:
            if (parent / ".ilha" / "config.yml").exists():
                return parent
        
        return self.project_root

    def _handle_worktree_creation_error(self, branch_name: str, error_type: str) -> Dict[str, Any]:
        """Handle worktree creation errors with appropriate error messages.
        
        Args:
            branch_name: Name of the branch that failed
            error_type: Type of error from git_manager.create_worktree()
            
        Returns:
            Error response dictionary
        """
        if error_type == "already_exists":
            # This shouldn't happen due to our earlier checks, but handle gracefully
            worktree_path = self.git_manager.find_worktree_path(branch_name)
            if worktree_path:
                return {
                    "success": True,
                    "data": {
                        "branch": branch_name,
                        "worktree_path": str(worktree_path),
                        "status": "already_exists"
                    }
                }
            else:
                return {
                    "success": False,
                    "error": f"Worktree for branch '{branch_name}' already exists but path not found"
                }
        elif error_type == "permission_denied":
            return {
                "success": False,
                "error": f"Permission denied creating worktree for '{branch_name}'. Check directory permissions."
            }
        else:
            return {
                "success": False,
                "error": f"Failed to create worktree for '{branch_name}': {error_type or 'unknown error'}"
            }

    def _copy_ilha_to_worktree(self, worktree_path: Path) -> bool:
        """Copy .ilha directory from project root to worktree.
        
        This implements the fractal design where each worktree gets its own
        .ilha configuration directory.
        """
        # Find the true project root
        true_project_root = self._find_true_project_root()
        if not true_project_root:
            return False
        
        source_ilha = true_project_root / ".ilha"
        target_ilha = worktree_path / ".ilha"
        
        # Verify source exists
        if not source_ilha.exists():
            return False
        
        # Don't copy if target already exists
        if target_ilha.exists():
            return True
        
        try:
            # Define ignore function to skip worktrees subdirectory
            def ignore_worktrees(dir_path, names):
                """Ignore worktrees/ subdirectory during copy."""
                if dir_path == str(source_ilha):
                    # Skip the worktrees subdirectory at the root level
                    return ['worktrees'] if 'worktrees' in names else []
                return []
            
            # Copy the entire .ilha directory, excluding worktrees
            shutil.copytree(source_ilha, target_ilha, ignore=ignore_worktrees)
            
            # Ensure README.md is copied (in case it was missing)
            self._ensure_readme_in_worktree(target_ilha)
            
            return True
            
        except Exception:
            return False
    
    def _ensure_readme_in_worktree(self, worktree_ilha_dir: Path) -> bool:
        """Ensure README.md exists in worktree .ilha directory."""
        try:
            # Check if README.md already exists
            readme_path = worktree_ilha_dir / "README.md"
            if readme_path.exists():
                return True
            
            # Get source README.md from package
            script_dir = get_script_dir()
            source_readme = script_dir / "config" / "README.md"
            
            if not source_readme.exists():
                return False
            
            # Copy README.md to worktree .ilha directory
            shutil.copy2(source_readme, readme_path)
            return True
            
        except Exception:
            return False
    
    def create_worktree(self, branch_name: str) -> Dict[str, Any]:
        """Create a new worktree with complete orchestration."""
        try:
            if not branch_name:
                return {
                    "success": False,
                    "error": "Branch name is required"
                }
            
            # Check if branch name is a reserved command name
            if not validate_worktree_name_not_reserved(branch_name):
                return {
                    "success": False,
                    "error": f"Branch name '{branch_name}' is reserved and cannot be used as a worktree name"
                }
            
            # Check if worktree already exists (enhanced detection)
            worktree_exists = self.git_manager.validate_worktree_exists(branch_name)
            worktree_path = self.git_manager.find_worktree_path(branch_name)
            
            # Check for edge cases: branch exists but worktree doesn't, or worktree directory exists but not tracked
            branch_exists = validate_branch_exists(branch_name, self.project_root)
            worktree_dir_exists = False
            if worktree_path:
                worktree_dir_exists = worktree_path.exists()
            
            # If worktree exists in git or directory exists, treat as existing
            if worktree_exists or (worktree_path and worktree_dir_exists):
                if worktree_path and worktree_path.exists():
                    return {
                        "success": True,
                        "data": {
                            "branch": branch_name,
                            "worktree_path": str(worktree_path),
                            "status": "already_exists"
                        }
                    }
                else:
                    return {
                        "success": False,
                        "error": f"Worktree directory not found for branch '{branch_name}'. The worktree may be corrupted."
                    }
            
            # Create branch if it doesn't exist
            if not self.git_manager.create_branch(branch_name):
                return {
                    "success": False,
                    "error": "Failed to create branch"
                }
            
            # Validate worktree creation (after branch is created)
            can_create, error_msg = self.git_manager.validate_worktree_creation(branch_name)
            if not can_create:
                return {
                    "success": False,
                    "error": error_msg
                }
            
            # Get worktree paths
            new_path, legacy_path = self.git_manager.get_worktree_paths(branch_name)
            
            # Create worktree
            success, error_type = self.git_manager.create_worktree(branch_name, new_path)
            if not success:
                return self._handle_worktree_creation_error(branch_name, error_type)
            
            # Copy .ilha configuration to worktree for fractal design
            ilha_copied = self._copy_ilha_to_worktree(new_path)
            
            # Create worktree-specific volumes
            volumes_created = self.docker_manager.create_worktree_volumes(branch_name, project_name=None, force_copy=True)
            
            # Create environment file
            env_created = self.env_manager.create_worktree_env(branch_name, new_path)
            
            # Get worktree path for return data
            worktree_path = self.git_manager.find_worktree_path(branch_name)
            
            return {
                "success": True,
                "data": {
                    "branch": branch_name,
                    "worktree_path": str(worktree_path) if worktree_path else str(new_path),
                    "ilha_copied": ilha_copied,
                    "volumes_created": volumes_created,
                    "env_created": env_created,
                    "status": "created"
                },
                "message": f"Worktree created for {branch_name}"
            }
            
        except Exception as e:
            # Catch and return any exceptions
            return {
                "success": False,
                "error": f"Exception during worktree creation: {str(e)}"
            }
    
    def start_worktree(self, branch_name: str, profile: Optional[str] = None) -> Dict[str, Any]:
        """Start worktree environment with complete orchestration.
        
        Args:
            branch_name: Name of the branch/worktree to start
            profile: Optional Docker Compose profile to use
        """
        # Validate worktree exists
        if not self.git_manager.validate_worktree_exists(branch_name):
            return {
                "success": False,
                "error": f"Worktree for branch '{branch_name}' does not exist. Please create it first."
            }
        
        # Resolve worktree path
        worktree_path = self.git_manager.find_worktree_path(branch_name)
        if not worktree_path:
            return {
                "success": False,
                "error": f"Could not find worktree directory for branch '{branch_name}'"
            }
        
        # Get the correct path to the compose override file
        compose_override_path = get_compose_override_path(worktree_path)
        if not compose_override_path:
            return {
                "success": False,
                "error": "Could not find compose override file. Please ensure ilha directory exists."
            }
        
        # Get branch name and ensure worktree volumes exist
        resolved_branch_name = get_worktree_branch_name(worktree_path)
        if not resolved_branch_name:
            return {
                "success": False,
                "error": "Could not determine branch name from worktree path"
            }
        
        # Ensure volumes exist
        self.docker_manager.create_worktree_volumes(resolved_branch_name, project_name=None, force_copy=False)
        
        # Create network if it doesn't exist
        if not self.docker_manager.create_network():
            return {
                "success": False,
                "error": "Failed to create Docker network"
            }
        
        # Ensure global Caddy is running
        from ..commands.caddy import CaddyManager
        caddy_manager = CaddyManager()
        if not caddy_manager.is_caddy_running():
            if not caddy_manager.start_global_caddy():
                return {
                    "success": False,
                    "error": "Failed to start global Caddy"
                }
        
        # Validate environment files exist
        env_file = worktree_path / ".ilha" / "env.ilha"
        main_env_file = worktree_path / ".env"

        if not env_file.exists() or not main_env_file.exists():
            # Try to create missing environment files
            if not self.env_manager.create_worktree_env(resolved_branch_name, worktree_path):
                return {
                    "success": False,
                    "error": "Failed to create environment files"
                }
            
            # Re-validate after creation
            if not env_file.exists() or not main_env_file.exists():
                return {
                    "success": False,
                    "error": "Environment files not found after creation"
                }

        # Use the compose file found by get_compose_override_path
        compose_file = compose_override_path

        if not compose_file.exists():
            return {
                "success": False,
                "error": f"Compose worktree file not found: {compose_file}"
            }

        # Get the full compose project name (project-branch format)
        project_name = sanitize_project_name(self._get_project_name())
        compose_project_name = f"{project_name}-{resolved_branch_name}"
        
        success = self.docker_manager.run_compose_command(
            compose_file, ["up", "-d"], env_file, compose_project_name, worktree_path, profile=profile
        )
        
        if not success:
            return {
                "success": False,
                "error": "Failed to start worktree environment"
            }
        
        # Give containers time to initialize before configuring Caddy
        time.sleep(5)

        # Configure Caddy routes for dynamic routing
        caddy_success = self._configure_caddy_routes()

        # Get the correct domain name with project prefix
        domain_name = self.env_manager.get_domain_name(resolved_branch_name)
        
        return {
            "success": True,
            "data": {
                "branch": resolved_branch_name,
                "worktree_path": str(worktree_path),
                "compose_project_name": compose_project_name,
                "domain_name": domain_name,
                "caddy_configured": caddy_success
            }
        }
    
    def _configure_caddy_routes(self) -> bool:
        """Configure Caddy routes for dynamic routing."""
        try:
            # Get the path to the dynamic config script
            script_path = get_script_dir() / "scripts" / "caddy-dynamic-config.py"

            if not script_path.exists():
                return False
            
            # Run the dynamic configuration script using current Python interpreter
            result = subprocess.run([
                sys.executable, str(script_path)
            ], capture_output=True, text=True, check=False)

            return result.returncode == 0 or "Using container label-based routing" in result.stdout

        except Exception:
            return False
    
    def stop_worktree(self, branch_name: str, remove_images: bool = False) -> Dict[str, Any]:
        """Stop worktree environment."""
        # Validate worktree exists (allow stopping even if not found for cleanup)
        if not self.git_manager.validate_worktree_exists(branch_name):
            return {
                "success": True,
                "data": {
                    "branch": branch_name,
                    "status": "not_found",
                    "message": "Worktree does not exist, skipping container stop"
                }
            }
        
        # Resolve worktree path
        worktree_path = self.git_manager.find_worktree_path(branch_name)
        if not worktree_path:
            return {
                "success": True,
                "data": {
                    "branch": branch_name,
                    "status": "not_found",
                    "message": "Worktree directory not found, skipping container stop"
                }
            }
        
        # Get the correct path to the compose override file
        compose_file = get_compose_override_path(worktree_path)
        if not compose_file:
            return {
                "success": True,
                "data": {
                    "branch": branch_name,
                    "status": "no_compose",
                    "message": "Could not find compose override file, skipping container stop"
                }
            }
        
        env_file = (worktree_path / ".ilha" / "env.ilha").resolve()

        if not compose_file.exists() or not env_file.exists():
            return {
                "success": True,
                "data": {
                    "branch": branch_name,
                    "status": "no_files",
                    "message": "Compose or environment file not found, skipping container stop"
                }
            }

        # Add --rmi local flag if remove_images is True
        extra_flags = ["--rmi", "local"] if remove_images else None
        
        # Get the full compose project name (project-branch format)
        project_name = sanitize_project_name(self._get_project_name())
        compose_project_name = f"{project_name}-{branch_name}"
        
        success = self.docker_manager.run_compose_command(
            compose_file, ["down"], env_file, compose_project_name, worktree_path, extra_flags
        )
        
        return {
            "success": True,
            "data": {
                "branch": branch_name,
                "status": "stopped",
                "compose_success": success,
                "images_removed": remove_images
            }
        }
    
    def _check_volumes_exist(self, branch_name: str) -> List[str]:
        """Check which volumes exist for this exact branch name."""
        from ..config.settings import get_volume_names
        from ..utils.validation import validate_volume_exists
        
        volume_names = get_volume_names(branch_name)
        existing = []
        for volume_type, volume_name in volume_names.items():
            if validate_volume_exists(volume_name):
                existing.append(volume_name)
        return existing
    
    def remove_worktree(self, branch_name: str, force: bool = False, delete_branch: bool = True) -> Dict[str, Any]:
        """Remove worktree completely with exact match validation."""
        if not branch_name:
            return {
                "success": False,
                "error": "Branch name is required"
            }
        
        # Ensure we're in the main repository directory
        ensure_main_repo()
        
        # STEP 1: Verify exact matches exist
        worktree_exists = self.git_manager.validate_worktree_exists(branch_name)
        branch_exists = validate_branch_exists(branch_name, self.project_root)
        volumes_exist = self._check_volumes_exist(branch_name)
        
        # STEP 2: If nothing exists for exact branch name, report and exit
        if not worktree_exists and not branch_exists and not volumes_exist:
            return {
                "success": False,
                "error": f"No exact match found for '{branch_name}'. "
                         f"Checked: worktrees, branches, and docker volumes. "
                         f"Please verify the exact branch name."
            }
        
        # STEP 3: Report what was found for this exact branch name
        if not self.mcp_mode:
            log_info(f"Found for branch '{branch_name}':")
            if worktree_exists:
                worktree_path = self.git_manager.find_worktree_path(branch_name)
                log_info(f"  - Worktree at: {worktree_path}")
            if branch_exists:
                log_info(f"  - Git branch: {branch_name}")
            if volumes_exist:
                log_info(f"  - Docker volumes: {', '.join(volumes_exist)}")
        
        # STEP 4: Handle case where only branch exists (no worktree)
        if not worktree_exists:
            # Check if branch exists to determine appropriate action
            if branch_exists:
                if delete_branch:
                    branch_deleted = self.git_manager.delete_branch_safely(branch_name, force)
                    if branch_deleted:
                        return {
                            "success": True,
                            "data": {
                                "branch": branch_name,
                                "action": "branch_deleted",
                                "message": f"Branch '{branch_name}' deleted successfully"
                            }
                        }
                    else:
                        return {
                            "success": False,
                            "error": f"Failed to delete branch '{branch_name}'"
                        }
                else:
                    return {
                        "success": True,
                        "data": {
                            "branch": branch_name,
                            "action": "branch_preserved",
                            "message": f"Branch '{branch_name}' exists but worktree removal was skipped"
                        }
                    }
        
        # Stop worktree environment if running
        worktree_path = self.git_manager.find_worktree_path(branch_name)
        if worktree_path:
            stop_result = self.stop_worktree(branch_name, remove_images=True)
            # Continue even if stop fails

        # Remove worktree-specific volumes
        volume_removal_success = self.docker_manager.remove_volumes(branch_name)
        
        # Remove worktree
        if worktree_path:
            if not self.git_manager.remove_worktree(worktree_path, force=True):
                return {
                    "success": False,
                    "error": f"Failed to remove worktree for {branch_name}"
                }
        else:
            return {
                "success": False,
                "error": f"Worktree directory not found for branch '{branch_name}'. The worktree may be corrupted."
            }
        
        # Delete the git branch if requested
        branch_deleted = False
        if delete_branch:
            branch_deleted = self.git_manager.delete_branch_safely(branch_name, force)
        
        return {
            "success": True,
            "data": {
                "branch": branch_name,
                "action": "removed",
                "worktree_removed": worktree_path is not None,
                "volumes_removed": volume_removal_success,
                "branch_deleted": branch_deleted,
                "message": f"Successfully removed worktree '{branch_name}'"
            }
        }
    
    def delete_worktree(self, branch_name: str, force: bool = False) -> Dict[str, Any]:
        """Delete worktree and branch completely."""
        return self.remove_worktree(branch_name, force=force, delete_branch=True)
    
    def list_worktrees(self) -> Dict[str, Any]:
        """List all worktrees with status."""
        worktrees = self.git_manager.list_worktrees()
        
        worktree_data = []
        for path, commit, branch in worktrees:
            worktree_data.append({
            "branch": branch,
            "path": str(path),
            "commit": commit,
            "status": "active"
        })
        
        return {
            "success": True,
            "data": worktree_data
        }
    
    def get_worktree_info(self, branch_name: str) -> Dict[str, Any]:
        """Get detailed worktree information."""
        worktree_exists = self.git_manager.validate_worktree_exists(branch_name)
        worktree_path = self.git_manager.find_worktree_path(branch_name) if worktree_exists else None
        
        if not worktree_exists:
            return {
                "success": False,
                "error": f"Worktree for branch '{branch_name}' does not exist"
            }
        
        # Get container status
        containers = []
        volumes = []
        
        try:
            # Get container information
            containers = self.docker_manager.get_worktree_containers_sync(branch_name)
            volumes = self.docker_manager.get_worktree_volumes_sync(branch_name)
        except Exception:
            pass
        
        # Determine overall status
        running_containers = [c for c in containers if c.get("state") == "running"]
        is_running = len(running_containers) > 0
        
        # Get domain name
        domain_name = self.env_manager.get_domain_name(branch_name)
        
        return {
            "success": True,
            "data": {
                "branch": branch_name,
                "worktree_path": str(worktree_path) if worktree_path else None,
                "status": "running" if is_running else "stopped",
                "containers": containers,
                "volumes": volumes,
                "domain_name": domain_name,
                "project_name": self._get_project_name()
            }
        }
