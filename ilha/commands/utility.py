"""
Utility commands for ilha CLI.

This module provides utility commands like listing worktrees, pruning, and showing help.
"""

from ..core.git_manager import GitManager
from ..utils.logging import log_info, log_success, print_plain, show_version, show_help


class UtilityManager:
    """Manages utility commands."""
    
    def __init__(self):
        """Initialize utility manager."""
        self.git_manager = GitManager()
    
    def list_worktrees(self) -> None:
        """List active worktrees."""
        worktrees = self.git_manager.list_worktrees()
        
        if not worktrees:
            print_plain("No worktrees found")
            return
        
        for path, commit, branch in worktrees:
            print_plain(f"{branch}")
    
    def list_worktrees_json(self) -> list:
        """List active worktrees as JSON."""
        worktrees = self.git_manager.list_worktrees()
        
        result = []
        for path, commit, branch in worktrees:
            result.append({
                "branch": branch,
                "path": str(path),
                "commit": commit,
                "status": "active"
            })
        
        return result
    
    def prune_worktrees(self) -> None:
        """Prune worktrees."""
        log_info("Pruning worktrees...")
        pruned_count = self.git_manager.prune_worktrees()
        
        if pruned_count == 0:
            log_info("No prunable worktrees found")
        else:
            log_success(f"Successfully pruned {pruned_count} worktree(s)")
    
    def prune_worktrees_json(self) -> int:
        """Prune worktrees and return count."""
        return self.git_manager.prune_worktrees()
    
    def show_version_info(self) -> None:
        """Show version information."""
        show_version()
    
    def show_help_info(self) -> None:
        """Show help information."""
        show_help()
    
    def get_system_info(self) -> dict:
        """Get system information."""
        return {
            "current_branch": self.git_manager.get_current_branch(),
            "worktrees": self.git_manager.list_worktrees(),
            "git_info": {
                "current_branch": self.git_manager.get_current_branch(),
                "worktree_count": len(self.git_manager.list_worktrees())
            }
        }
