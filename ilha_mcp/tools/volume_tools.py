"""
MCP tools for volume management.

This module provides MCP tool implementations for ilha volume operations.
"""

from typing import Any, Dict

from ..utils.cli_wrapper import ilhaCLIWrapper


class VolumeTools:
    """MCP tools for volume management."""
    
    def __init__(self, config):
        """Initialize volume tools."""
        self.config = config
        self.cli_wrapper = ilhaCLIWrapper(config)
    
    async def list_volumes(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """List all worktree volumes."""
        try:
            result = await self.cli_wrapper.run_command(["volumes", "list", "--json"])
            return result
        except Exception as e:
            return {"error": f"Failed to list volumes: {str(e)}"}
    
    async def get_volume_sizes(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Get sizes of all worktree volumes."""
        try:
            result = await self.cli_wrapper.run_command(["volumes", "size", "--json"])
            return result
        except Exception as e:
            return {"error": f"Failed to get volume sizes: {str(e)}"}
    
    async def backup_volumes(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Backup volumes for a specific worktree."""
        branch_name = arguments.get("branch_name")
        backup_dir = arguments.get("backup_dir")
        
        if not branch_name:
            return {"error": "branch_name is required"}
        
        try:
            cmd = ["volumes", "backup", branch_name, "--json"]
            if backup_dir:
                cmd.extend(["--backup-dir", backup_dir])
            
            result = await self.cli_wrapper.run_command(cmd)
            return result
        except Exception as e:
            return {"error": f"Failed to backup volumes: {str(e)}"}
    
    async def restore_volumes(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Restore volumes for a specific worktree from backup."""
        branch_name = arguments.get("branch_name")
        backup_file = arguments.get("backup_file")
        
        if not branch_name:
            return {"error": "branch_name is required"}
        if not backup_file:
            return {"error": "backup_file is required"}
        
        try:
            result = await self.cli_wrapper.run_command([
                "volumes", "restore", branch_name, backup_file, "--json"
            ])
            return result
        except Exception as e:
            return {"error": f"Failed to restore volumes: {str(e)}"}
    
    async def clean_volumes(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Clean up volumes for a specific worktree."""
        branch_name = arguments.get("branch_name")
        
        if not branch_name:
            return {"error": "branch_name is required"}
        
        try:
            result = await self.cli_wrapper.run_command([
                "volumes", "clean", branch_name, "--json"
            ])
            return result
        except Exception as e:
            return {"error": f"Failed to clean volumes: {str(e)}"}


