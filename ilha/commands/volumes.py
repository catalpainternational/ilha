"""
Volume management commands for ilha CLI.

This module provides commands for managing worktree volumes including
listing, sizing, backup, restore, and cleanup operations.
"""

from pathlib import Path
from typing import Dict, List, Optional

from ..core.docker_manager import DockerManager
from ..core.environment_manager import EnvironmentManager
from ..utils.logging import log_info, log_success, log_warning, log_error, print_plain


class VolumeManager:
    """Manages volume operations for ilha CLI."""
    
    def __init__(self):
        """Initialize volume manager."""
        self.docker_manager = DockerManager()
        self.env_manager = EnvironmentManager()
    
    def list_volumes(self) -> None:
        """List all worktree volumes."""
        print_plain("Listing all worktree volumes:")
        volumes = self.docker_manager.list_volumes()
        
        if not volumes:
            print_plain("No worktree volumes found")
            return
        
        for volume in volumes:
            print_plain(f"  {volume}")
    
    def list_volumes_json(self) -> list:
        """List all worktree volumes as JSON."""
        volumes = self.docker_manager.list_volumes()
        sizes = self.docker_manager.get_volume_sizes()
        
        result = []
        for volume in volumes:
            result.append({
                "name": volume,
                "size": sizes.get(volume, "unknown"),
                "type": "worktree_volume"
            })
        
        return result
    
    def show_volume_sizes(self) -> None:
        """Show volume sizes."""
        print_plain("Volume sizes:")
        sizes = self.docker_manager.get_volume_sizes()
        
        if not sizes:
            print_plain("No worktree volumes found")
            return
        
        for volume, size in sizes.items():
            print_plain(f"  {volume}: {size}")
    
    def get_volume_sizes_json(self) -> dict:
        """Get volume sizes as JSON."""
        sizes = self.docker_manager.get_volume_sizes()
        return {"volumes": sizes}
    
    def backup_volumes(self, branch_name: str, backup_dir: Optional[Path] = None) -> bool:
        """Backup worktree volumes."""
        if not branch_name:
            log_error("Branch name required for backup")
            return False
        
        if backup_dir is None:
            backup_dir = Path.cwd() / "backups"
        
        backup_file = self.docker_manager.backup_volumes(branch_name, backup_dir)
        if backup_file:
            log_success(f"Backup created: {backup_file}")
            return True
        else:
            log_error("Failed to create backup")
            return False
    
    def restore_volumes(self, branch_name: str, backup_file: Path) -> bool:
        """Restore worktree volumes from backup."""
        if not branch_name:
            log_error("Branch name required for restore")
            return False
        
        if not backup_file.exists():
            log_error(f"Backup file {backup_file} not found")
            return False
        
        success = self.docker_manager.restore_volumes(branch_name, backup_file)
        if success:
            log_success(f"Volumes restored for {branch_name}")
        else:
            log_error("Failed to restore volumes")
        
        return success
    
    def clean_volumes(self, branch_name: str) -> bool:
        """Clean up worktree volumes."""
        if not branch_name:
            log_error("Branch name required for cleanup")
            return False
        
        log_info(f"Cleaning volumes for {branch_name}")
        success = self.docker_manager.remove_volumes(branch_name)
        
        if success:
            log_success(f"Volumes cleaned for {branch_name}")
        else:
            log_warning("Some volumes could not be cleaned")
        
        return success
    
    def get_volume_info(self, branch_name: str) -> Dict[str, any]:
        """Get volume information for a worktree."""
        volume_names = self.env_manager.get_worktree_volume_names(branch_name)
        sizes = self.docker_manager.get_volume_sizes()
        all_volumes = self.docker_manager.list_volumes()
        
        volume_info = {}
        for volume_type, volume_name in volume_names.items():
            volume_info[volume_type] = {
                "name": volume_name,
                "size": sizes.get(volume_name, "unknown"),
                "exists": volume_name in all_volumes
            }
        
        return {
            "branch_name": branch_name,
            "volume_names": volume_names,
            "volume_sizes": sizes,
            "total_volumes": len(volume_names),
            "volumes_exist": any(volume_name in all_volumes for volume_name in volume_names.values())
        }
    
    def list_all_volume_info(self) -> Dict[str, Dict[str, any]]:
        """Get information about all worktree volumes."""
        all_volumes = self.docker_manager.list_volumes()
        sizes = self.docker_manager.get_volume_sizes()
        
        volume_info = {}
        for volume in all_volumes:
            # Extract branch name from volume name
            if "_postgres_data" in volume:
                branch_name = volume.replace("_postgres_data", "")
                volume_info[branch_name] = self.get_volume_info(branch_name)
        
        return volume_info
    
    def cleanup_orphaned_volumes(self) -> int:
        """Clean up volumes that don't have corresponding worktrees.
        
        Note: Only cleans up worktree-specific volumes (postgres, redis, media).
        Caddy volumes are shared globally and intentionally excluded from cleanup.
        """
        from ..core.git_manager import GitManager
        
        git_manager = GitManager()
        worktrees = git_manager.list_worktrees()
        active_branches = {branch for _, _, branch in worktrees}
        
        all_volumes = self.docker_manager.list_volumes()
        orphaned_count = 0
        
        for volume in all_volumes:
            # Extract branch name from volume name
            # Only check worktree-specific volumes (caddy volumes are intentionally excluded)
            branch_name = None
            for suffix in ["_postgres_data", "_redis_data", "_media_files"]:
                if volume.endswith(suffix):
                    branch_name = volume.replace(suffix, "")
                    break
            
            if branch_name and branch_name not in active_branches:
                log_info(f"Found orphaned volume: {volume} (branch: {branch_name})")
                if self.docker_manager.remove_volumes(branch_name):
                    orphaned_count += 1
        
        if orphaned_count > 0:
            log_success(f"Cleaned up {orphaned_count} orphaned volume(s)")
        else:
            log_info("No orphaned volumes found")
        
        return orphaned_count
