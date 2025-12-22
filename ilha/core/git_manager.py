"""
Git worktree management for ilha CLI.

This module provides Git operations including worktree creation, removal,
branch management, and repository validation.
"""

import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from ..config.settings import PROTECTED_BRANCHES, get_worktree_paths, get_project_root
from ..utils.logging import log_info, log_success, log_warning, log_error
from ..utils.validation import (
    validate_branch_exists, 
    validate_worktree_exists, 
    validate_current_branch,
    validate_branch_merged,
    validate_branch_protection
)


class GitManager:
    """Manages Git operations for ilha CLI."""
    
    def __init__(self, project_root: Optional[Path] = None, validate: bool = True):
        """Initialize Git manager.
        
        Args:
            project_root: Project root directory. If None, uses get_project_root().
            validate: If True, raise exception if not in git repo. If False, just log warning.
        """
        # Use the provided project_root directly, don't fall back to get_project_root()
        # This ensures MCP server uses the correct working directory
        if project_root is None:
            self.project_root = get_project_root()
        else:
            self.project_root = Path(project_root).resolve()
        if validate:
            self._validate_git_repo()
        else:
            # Just check without raising
            try:
                subprocess.run(["git", "rev-parse", "--git-dir"], 
                              capture_output=True, check=True, cwd=self.project_root)
            except subprocess.CalledProcessError:
                log_warning("Not in a git repository. Some operations may fail.")
    
    def _validate_git_repo(self) -> None:
        """Validate we're in a git repository."""
        try:
            subprocess.run(["git", "rev-parse", "--git-dir"], 
                          capture_output=True, check=True, cwd=self.project_root)
        except subprocess.CalledProcessError:
            raise RuntimeError("Not in a git repository. Please run this command from the project root.")
    
    def get_current_branch(self) -> Optional[str]:
        """Get the current git branch name."""
        return validate_current_branch(self.project_root)
    
    def create_branch(self, branch_name: str) -> bool:
        """Create a new git branch."""
        if validate_branch_exists(branch_name, self.project_root):
            log_info(f"Branch {branch_name} already exists")
            return True
        
        log_info(f"Branch {branch_name} doesn't exist, creating from current branch")
        try:
            subprocess.run(["git", "branch", branch_name], 
                          capture_output=True, check=True, cwd=self.project_root)
            log_success(f"Branch {branch_name} created")
            return True
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to create branch {branch_name}: {e}")
            return False
    
    def _parse_git_error(self, stderr: str) -> str:
        """Parse git error stderr to determine error type.
        
        Args:
            stderr: Error output from git command
            
        Returns:
            Error type: "already_exists", "permission_denied", or "other"
        """
        stderr_lower = stderr.lower()
        if "already exists" in stderr_lower or "already checked out" in stderr_lower:
            return "already_exists"
        elif "permission denied" in stderr_lower or "not a directory" in stderr_lower:
            return "permission_denied"
        else:
            return "other"

    def create_worktree(self, branch_name: str, worktree_path: Path) -> Tuple[bool, Optional[str]]:
        """Create a git worktree.
        
        Returns:
            Tuple of (success, error_type) where error_type is:
            - None if successful
            - "already_exists" if worktree already exists
            - "permission_denied" if permission issues
            - "other" for other errors
        """
        try:
            # Ensure parent directory exists
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Create branch if it doesn't exist
            if not validate_branch_exists(branch_name, self.project_root):
                subprocess.run([
                    "git", "branch", branch_name
                ], capture_output=True, check=True, cwd=self.project_root)
                log_info(f"Created branch {branch_name}")
            
            result = subprocess.run([
                "git", "worktree", "add", str(worktree_path), branch_name
            ], capture_output=True, text=True, cwd=self.project_root)
            
            if result.returncode == 0:
                log_success(f"Git worktree created for {branch_name}")
                return True, None
            else:
                error_type = self._parse_git_error(result.stderr)
                return False, error_type
                    
        except subprocess.CalledProcessError as e:
            # Parse stderr from the exception
            stderr = getattr(e, 'stderr', '').decode('utf-8', errors='ignore') if hasattr(e, 'stderr') else str(e)
            error_type = self._parse_git_error(stderr)
            return False, error_type
    
    def remove_worktree(self, worktree_path: Path, force: bool = False) -> bool:
        """Remove a git worktree with improved error handling for permission issues."""
        try:
            cmd = ["git", "worktree", "remove"]
            if force:
                cmd.append("--force")
            cmd.append(str(worktree_path))
            
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.project_root)
            
            if result.returncode == 0:
                log_success(f"Git worktree removed: {worktree_path}")
                return True
            
            # If git worktree remove fails with exit code 255 (permission issues)
            if result.returncode == 255:
                log_warning("Git worktree remove failed due to permission issues, attempting manual cleanup...")
                
                # First, prune the worktree from git's tracking
                subprocess.run(["git", "worktree", "prune"], cwd=self.project_root, capture_output=True)
                
                # Then try to remove the directory manually
                import shutil
                try:
                    shutil.rmtree(worktree_path, ignore_errors=True)
                    
                    # Verify it's actually gone
                    if not worktree_path.exists():
                        log_success(f"Manually removed worktree directory: {worktree_path}")
                        return True
                    else:
                        # Directory still exists (probably has root-owned files)
                        log_warning(f"Could not completely remove {worktree_path}")
                        log_info("This usually happens when Docker created files owned by root")
                        log_info("To fix manually:")
                        log_info(f"  1. sudo rm -rf {worktree_path}")
                        log_info(f"  2. git worktree prune")
                        return False
                except Exception as cleanup_error:
                    log_error(f"Error during cleanup: {cleanup_error}")
                    log_info("This usually happens when Docker created files owned by root")
                    log_info("To fix manually:")
                    log_info(f"  1. sudo rm -rf {worktree_path}")
                    log_info(f"  2. git worktree prune")
                    return False
            
            log_error(f"Failed to remove git worktree {worktree_path}: {result.stderr}")
            return False
            
        except Exception as e:
            log_error(f"Error removing worktree {worktree_path}: {e}")
            return False
    
    def list_worktrees(self) -> List[Tuple[str, str, str]]:
        """List all git worktrees.
        
        Returns:
            List of tuples (path, commit, branch)
        """
        worktrees = []
        try:
            result = subprocess.run(["git", "worktree", "list"], 
                                  capture_output=True, text=True, check=True, cwd=self.project_root)
            
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    parts = line.split()
                    if len(parts) >= 3:
                        path = parts[0]
                        commit = parts[1]
                        branch = parts[2].strip('[]') if parts[2].startswith('[') else parts[2]
                        worktrees.append((path, commit, branch))
                        
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to list worktrees: {e}")
        
        return worktrees
    
    def prune_worktrees(self) -> int:
        """Prune worktrees and return count of pruned worktrees."""
        try:
            # Check if there are any prunable worktrees
            result = subprocess.run(["git", "worktree", "list"], 
                                  capture_output=True, text=True, check=True, cwd=self.project_root)
            
            prunable_count = 0
            for line in result.stdout.strip().split('\n'):
                if 'prunable' in line:
                    prunable_count += 1
            
            if prunable_count == 0:
                log_info("No prunable worktrees found")
                return 0
            
            log_info(f"Found {prunable_count} prunable worktree(s)")
            
            # Show what will be pruned
            log_info("Prunable worktrees:")
            for line in result.stdout.strip().split('\n'):
                if 'prunable' in line:
                    log_info(line)
            
            # Prune the worktrees
            subprocess.run(["git", "worktree", "prune"], 
                          capture_output=True, check=True, cwd=self.project_root)
            
            log_success(f"Successfully pruned {prunable_count} worktree(s)")
            return prunable_count
            
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to prune worktrees: {e}")
            return 0
    
    def delete_branch_safely(self, branch_name: str, force: bool = False) -> bool:
        """Safely delete a git branch."""
        # Check if branch exists
        if not validate_branch_exists(branch_name, self.project_root):
            log_info(f"Branch {branch_name} does not exist, skipping branch deletion")
            return True
        
        # Check if branch is currently checked out
        current_branch = self.get_current_branch()
        if current_branch == branch_name:
            log_warning(f"Cannot delete branch {branch_name} - it is currently checked out")
            return True
        
        # Check if branch is protected
        if validate_branch_protection(branch_name):
            log_warning(f"Cannot delete protected branch: {branch_name}")
            return False
        
        log_info(f"Attempting to delete branch: {branch_name}")
        
        # Check if branch is merged (only for non-force deletions)
        if not force:
            is_merged = validate_branch_merged(branch_name, self.project_root)
            if not is_merged:
                log_warning(f"Branch {branch_name} has unmerged changes. Use --force to delete it anyway")
                return True
        
        # Try to delete merged branch first
        try:
            subprocess.run(["git", "branch", "-d", branch_name], 
                          capture_output=True, check=True, cwd=self.project_root)
            log_success(f"Branch {branch_name} deleted (was merged)")
            return True
        except subprocess.CalledProcessError:
            pass
        
        # If not merged and force flag is set, delete with force
        if force:
            try:
                subprocess.run(["git", "branch", "-D", branch_name], 
                              capture_output=True, check=True, cwd=self.project_root)
                log_success(f"Branch {branch_name} force deleted (was not merged)")
                return True
            except subprocess.CalledProcessError as e:
                log_warning(f"Failed to force delete branch {branch_name}: {e}")
                return False
        else:
            log_warning(f"Branch {branch_name} has unmerged changes. Use --force to delete it anyway")
            return True
    
    def get_worktree_paths(self, branch_name: str) -> Tuple[Path, Path]:
        """Get worktree paths for a branch (new and legacy)."""
        from ..config.settings import get_worktree_dir
        worktree_dir = get_worktree_dir()
        new_path = self.project_root / worktree_dir / branch_name
        legacy_path = self.project_root.parent / branch_name
        return new_path, legacy_path
    
    def find_worktree_path(self, branch_name: str) -> Optional[Path]:
        """Find the actual worktree path for a branch with exact matching."""
        try:
            result = subprocess.run(["git", "worktree", "list"], 
                                  capture_output=True, text=True, check=True, cwd=self.project_root)
            
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    parts = line.split()
                    if len(parts) >= 3:
                        path = parts[0]
                        branch = parts[2].strip('[]') if parts[2].startswith('[') else parts[2]
                        # Explicit exact match check
                        if branch == branch_name:
                            log_info(f"Exact match found: worktree for '{branch_name}' at {path}")
                            return Path(path)
            
            log_warning(f"No exact worktree match found for '{branch_name}'")
            return None
        except subprocess.CalledProcessError:
            return None
    
    def validate_worktree_creation(self, branch_name: str) -> Tuple[bool, str]:
        """Validate that a worktree can be created for the given branch."""
        # Check if branch exists
        if not validate_branch_exists(branch_name, self.project_root):
            return False, f"Branch {branch_name} does not exist"
        
        # Check if we're already on the target branch
        current_branch = validate_current_branch(self.project_root)
        if current_branch == branch_name:
            return False, f"Cannot create worktree for current branch. Please switch to a different branch first."
        
        # Check if branch is protected
        if validate_branch_protection(branch_name):
            return False, f"Cannot create worktree for protected branch: {branch_name}"
        
        # Check if worktree already exists
        if validate_worktree_exists(branch_name, self.project_root):
            return False, f"Worktree for branch {branch_name} already exists"
        
        return True, ""
    
    def validate_worktree_exists(self, branch_name: str) -> bool:
        """Check if a worktree exists for the given branch."""
        return validate_worktree_exists(branch_name, self.project_root)
    
    def get_branch_info(self, branch_name: str) -> dict:
        """Get information about a branch."""
        try:
            result = subprocess.run([
                "git", "log", "-1", "--format=%H %s", branch_name
            ], capture_output=True, text=True, check=True, cwd=self.project_root)
            
            commit, message = result.stdout.strip().split(' ', 1)
            
            info = {
                "exists": True,
                "commit": commit,
                "message": message,
                "is_protected": validate_branch_protection(branch_name),
                "is_merged": validate_branch_merged(branch_name, self.project_root),
                "worktree_exists": validate_worktree_exists(branch_name, self.project_root),
                "worktree_path": self.find_worktree_path(branch_name)
            }
            
            return info
        except subprocess.CalledProcessError:
            return None
    
    def list_all_branches(self) -> List[str]:
        """Get all local branch names.
        
        Returns:
            List of all local branch names
        """
        try:
            result = subprocess.run([
                "git", "branch", "--format=%(refname:short)"
            ], capture_output=True, text=True, check=True, cwd=self.project_root)
            
            branches = []
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    branches.append(line.strip())
            
            return branches
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to list branches: {e}")
            return []
    
    def create_worktree_archive(self, branch_name: str, output_path: Path) -> bool:
        """Create git archive of worktree code (full archive by default).
        
        Args:
            branch_name: Name of the branch/worktree to archive
            output_path: Path where the archive should be created
            
        Returns:
            True if archive was created successfully, False otherwise
        """
        worktree_path = self.find_worktree_path(branch_name)
        if not worktree_path:
            log_error(f"Worktree for branch '{branch_name}' not found")
            return False
        
        if not worktree_path.exists():
            log_error(f"Worktree directory not found: {worktree_path}")
            return False
        
        try:
            # Ensure output directory exists
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Create git archive from the worktree
            result = subprocess.run([
                "git", "archive", "--format=tar.gz",
                f"--output={output_path.absolute()}",
                "HEAD"
            ], cwd=worktree_path, check=True, capture_output=True, text=True)
            
            log_success(f"Created git archive: {output_path}")
            return True
            
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to create git archive for {branch_name}: {e}")
            if e.stderr:
                log_error(f"Git error: {e.stderr}")
            return False
        except Exception as e:
            log_error(f"Unexpected error creating archive: {e}")
            return False