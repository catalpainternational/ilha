"""
Worktree lifecycle management commands for ilha CLI.

This module provides commands for creating, starting, stopping, and removing worktrees.
"""

import os
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

from ..config.settings import get_project_root, get_script_dir
from ..core.worktree_orchestrator import WorktreeOrchestrator
from ..core.docker_manager import DockerManager as CoreDockerManager
from ..core.git_manager import GitManager as CoreGitManager
from ..core.environment_manager import EnvironmentManager as CoreEnvironmentManager
from ..utils.logging import log_info, log_success, log_warning, log_error, log_step
from ..utils.path_utils import (
    get_worktree_branch_name,
    ensure_main_repo,
)
from ..utils.validation import validate_branch_exists, validate_worktree_name_not_reserved
from ..utils.validation import validate_worktree_directory
from ..utils.pattern_matcher import has_wildcard, get_matching_branches
from ..utils.confirmation import confirm_batch_operation, confirm_use_existing_worktree


DockerManager = CoreDockerManager  # Backwards-compatible export for legacy tests
GitManager = CoreGitManager
EnvironmentManager = CoreEnvironmentManager


class WorktreeManager:
    """CLI interface to worktree orchestration."""

    def __init__(self, project_root: Optional[Path] = None):
        """Initialize worktree manager."""
        self.project_root = (
            Path(project_root).resolve()
            if project_root
            else None
        )
        self.orchestrator: Optional[WorktreeOrchestrator] = None
        self.git_manager = None
        self.docker_manager = None
        self.env_manager = None

    def _ensure_orchestrator(self) -> None:
        """Lazily create the orchestrator and dependent managers.

        Legacy unit tests patch module-level dependencies (e.g., GitManager)
        before setting ``project_root``. Deferring initialization prevents git
        subprocess calls from running with placeholder paths.
        """
        if self.orchestrator is not None:
            return

        root = self.project_root or get_project_root()
        if not isinstance(root, Path):
            root = Path(root)
        self.project_root = root.resolve()
        self.orchestrator = WorktreeOrchestrator(self.project_root)
        self.git_manager = self.orchestrator.git_manager
        self.docker_manager = self.orchestrator.docker_manager
        self.env_manager = self.orchestrator.env_manager

    
    def _handle_create_success(
        self, result: Dict[str, Any], branch_name: str, interactive: bool
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Handle successful create_worktree result (DRY logging and prompts)."""
        data = result.get("data", {})
        worktree_path = Path(data.get("worktree_path", ""))
        status = data.get("status", "created")
        if status == "env_already_exists":
            log_success(f"Worktree for {branch_name} is ready at: {worktree_path}")
            return True, result
        if status in ("env_created", "env_recreated"):
            log_success(result.get("message", f"Environment ready for {branch_name}"))
            log_info(f"Next: ilha {branch_name} up")
            return True, result
        if status == "already_exists":
            if interactive and not confirm_use_existing_worktree(branch_name):
                log_info("Please use a different name for your worktree.")
                log_info(f"Example: ilha create {branch_name}-v2")
                return False, result
            log_success(f"Worktree for {branch_name} is ready at: {worktree_path}")
            return True, result
        # status == "created"
        log_success(f"Worktree created for {branch_name}")
        try:
            relative_path = worktree_path.relative_to(self.project_root)
            log_info(f"📁 Location: {relative_path}")
            log_info(f"💡 Navigate: cd {relative_path}")
        except ValueError:
            log_info(f"📁 Location: {worktree_path}")
        log_info(f"🚀 Next: ilha {branch_name} up")
        return True, result

    def create_worktree(self, branch_name: str, interactive: bool = True, force: bool = False) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Create a new worktree or ilha environment for an existing worktree - CLI interface."""
        self._ensure_orchestrator()
        log_step(f"Creating worktree for {branch_name}")

        result = self.orchestrator.create_worktree(branch_name, force=force)
        
        if result['success']:
            return self._handle_create_success(result, branch_name, interactive)
        else:
            log_error(result['error'])
            return False, result
    
    def start_worktree(
        self,
        branch_name: str,
        profile: Optional[str] = None,
        secrets_profile: Optional[str] = None,
    ) -> bool:
        """Start worktree environment - CLI interface.
        
        Args:
            branch_name: Name of the branch/worktree to start
            profile: Optional Docker Compose profile to use
            secrets_profile: Optional SOPS secret profile name
        """
        self._ensure_orchestrator()
        extra = f" (profile: {profile})" if profile else ""
        if secrets_profile:
            extra += f" (secrets: {secrets_profile})"
        log_step(f"Starting worktree {branch_name}{extra}")

        result = self.orchestrator.start_worktree(
            branch_name,
            profile=profile,
            secrets_profile=secrets_profile,
        )
        
        if result['success']:
            data = result['data']
            domain_name = data['domain_name']
            
            # CLI-specific: Pretty logging and output formatting
            if data.get('caddy_configured'):
                log_info("✅ Caddy routing configuration completed")
            else:
                log_warning("⚠️ Caddy routing configuration may have failed")

            if data.get('smoke_ok'):
                log_success(f"🌐 Application available at: http://{domain_name}/")
            else:
                log_warning(f"⚠️ Smoke check failed: http://{domain_name}/ is not responding yet")
                log_info(f"🔗 URL: http://{domain_name}/")

            return True
        else:
            log_error(result['error'])
            return False
    
    
    def stop_worktree(self, branch_name: str, remove_images: bool = False) -> bool:
        """Stop worktree environment - CLI interface."""
        self._ensure_orchestrator()
        log_step(f"Stopping worktree {branch_name}")

        result = self.orchestrator.stop_worktree(branch_name, remove_images)
        
        # CLI-specific: Pretty logging and output formatting
        if result['success']:
            data = result['data']
            if data.get('status') == 'stopped':
                if remove_images:
                    log_success("Worktree environment stopped and images removed")
                else:
                    log_success("Worktree environment stopped")
            else:
                log_info(f"Worktree status: {data.get('message', 'stopped')}")
        else:
            log_warning("Failed to stop some services (may not be running)")
        
        return True  # Always return True for stop operations
    
    def remove_worktree(self, branch_name: str, force: bool = False, delete_branch: bool = True) -> bool:
        """Remove worktree completely - CLI interface."""
        self._ensure_orchestrator()
        if not branch_name:
            log_error("Branch name is required")
            return False
        
        # Ensure we're in the main repository directory
        ensure_main_repo()
        
        log_step(f"Removing worktree {branch_name}")

        result = self.orchestrator.remove_worktree(branch_name, force, delete_branch)
        
        # CLI-specific: Pretty logging and output formatting
        if result['success']:
            data = result['data']
            action = data.get('action', 'removed')
            
            if action == 'branch_deleted':
                log_success(f"Branch '{branch_name}' deleted successfully")
            elif action == 'branch_preserved':
                log_info(f"Branch '{branch_name}' exists but worktree removal was skipped")
            else:
                log_success(f"Worktree removed for {branch_name}")
            
            return True
        else:
            log_error(result['error'])
            return False
    
    def list_worktrees(self) -> list:
        """List active worktrees - CLI interface."""
        self._ensure_orchestrator()
        log_info("Active worktrees:")
        
        result = self.orchestrator.list_worktrees()
        
        if result['success']:
            worktrees = result['data']
            for worktree in worktrees:
                log_info(f"{worktree['path']} {worktree['commit']} [{worktree['branch']}]")
            return worktrees
        else:
            log_error("Failed to list worktrees")
            return []
    
    def prune_worktrees(self) -> int:
        """Prune worktrees."""
        self._ensure_orchestrator()
        log_info("Pruning worktrees...")
        pruned_count = self.git_manager.prune_worktrees()
        return pruned_count
    
    def remove_all_worktrees(self, force: bool = False, delete_branch: bool = True) -> bool:
        """Remove all worktrees, containers, and volumes."""
        self._ensure_orchestrator()
        # Ensure we're in the main repository directory
        ensure_main_repo()
        
        log_info("Removing all worktrees...")
        
        # Get all active worktrees
        worktrees = self.git_manager.list_worktrees()
        
        if not worktrees:
            log_info("No worktrees found to remove")
            return True
        
        # Filter out the main repository to avoid removing it
        current_branch = self.git_manager.get_current_branch()
        filtered_worktrees = []
        
        for path, commit, branch in worktrees:
            # Skip the main repository (current branch)
            if branch == current_branch:
                log_info(f"Skipping main repository worktree: {branch}")
                continue
            filtered_worktrees.append((path, commit, branch))
        
        if not filtered_worktrees:
            log_info("No worktrees found to remove (excluding main repository)")
            return True
        
        log_info(f"Found {len(filtered_worktrees)} worktree(s) to remove")
        
        # Track results
        success_count = 0
        failure_count = 0
        failed_branches = []
        
        # Process each worktree
        for path, commit, branch in filtered_worktrees:
            log_info(f"Removing worktree: {branch}")
            try:
                # Use existing remove_worktree method for each worktree
                if self.remove_worktree(branch, force, delete_branch):
                    success_count += 1
                    log_success(f"Successfully removed worktree: {branch}")
                else:
                    failure_count += 1
                    failed_branches.append(branch)
                    log_error(f"Failed to remove worktree: {branch}")
            except Exception as e:
                failure_count += 1
                failed_branches.append(branch)
                log_error(f"Error removing worktree {branch}: {e}")
        
        # Summary
        if failure_count == 0:
            log_success(f"Successfully removed all {success_count} worktree(s)")
            return True
        else:
            log_warning(f"Removed {success_count} worktree(s), failed to remove {failure_count} worktree(s)")
            if failed_branches:
                log_warning(f"Failed branches: {', '.join(failed_branches)}")
            return failure_count == 0  # Return True only if all succeeded
    
    def get_worktree_info(self, branch_name: str) -> dict:
        """Get information about a worktree - CLI interface."""
        self._ensure_orchestrator()
        result = self.orchestrator.get_worktree_info(branch_name)
        
        if result['success']:
            return result['data']
        else:
            log_error(result['error'])
            return {
                "branch_name": branch_name,
                "exists": False,
                "error": result['error']
            }
    
    def remove_worktrees_by_pattern(self, pattern: str, force: bool = False, delete_branch: bool = True) -> bool:
        """Remove worktrees matching a wildcard pattern.
        
        Args:
            pattern: Wildcard pattern to match branch names
            force: Force removal even with unmerged changes
            delete_branch: Whether to delete the git branch as well
            
        Returns:
            True if all operations succeeded, False if any failed
        """
        self._ensure_orchestrator()
        if not pattern:
            log_error("Pattern is required")
            return False
        
        # Ensure we're in the main repository directory
        ensure_main_repo()
        
        log_info(f"Removing worktrees matching pattern: {pattern}")
        
        # Get all branches and find matches
        all_branches = self.git_manager.list_all_branches()
        if not all_branches:
            log_error("No branches found")
            return False
        
        # Get current branch to exclude it from deletion
        current_branch = self.git_manager.get_current_branch()
        
        # Find matching branches
        matching_branches = get_matching_branches(pattern, all_branches, current_branch)
        
        if not matching_branches:
            log_info(f"No branches found matching pattern: {pattern}")
            return True
        
        # Show confirmation prompt
        operation = "delete" if delete_branch else "remove"
        if not confirm_batch_operation(matching_branches, operation):
            log_info("Operation cancelled by user")
            return True
        
        # Track results
        success_count = 0
        failure_count = 0
        failed_branches = []
        
        # Process each matching branch
        for branch_name in matching_branches:
            log_info(f"Removing worktree: {branch_name}")
            try:
                if self.remove_worktree(branch_name, force, delete_branch):
                    success_count += 1
                    log_success(f"Successfully removed worktree: {branch_name}")
                else:
                    failure_count += 1
                    failed_branches.append(branch_name)
                    log_error(f"Failed to remove worktree: {branch_name}")
            except Exception as e:
                failure_count += 1
                failed_branches.append(branch_name)
                log_error(f"Error removing worktree {branch_name}: {e}")
        
        # Summary
        if failure_count == 0:
            log_success(f"Successfully removed all {success_count} worktree(s)")
            return True
        else:
            log_warning(f"Removed {success_count} worktree(s), failed to remove {failure_count} worktree(s)")
            if failed_branches:
                log_warning(f"Failed branches: {', '.join(failed_branches)}")
            return failure_count == 0  # Return True only if all succeeded
