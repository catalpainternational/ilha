"""
MCP tools for worktree management.

This module provides MCP tool implementations for ilha worktree operations
with enhanced context and response enrichment.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

from ..utils.cli_wrapper import ilhaCLIWrapper
from ..api.ilha_api import ilhaAPI
from ..utils.response_enrichment import ResponseEnrichment


class WorktreeTools:
    """MCP tools for worktree management with enhanced context."""
    
    def __init__(self, config):
        """Initialize worktree tools."""
        self.config = config
        self.cli_wrapper = ilhaCLIWrapper(config)
        self.api = ilhaAPI(config)
        self.enrichment = ResponseEnrichment(config)
    
    async def create_worktree(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new worktree for the specified branch."""
        branch_name = arguments.get("branch_name")
        if not branch_name:
            return self.enrichment.add_ilha_context(
                {"error": "branch_name is required"}, 
                "create_worktree", 
                None, 
                False
            )
        
        try:
            # Use direct API for richer responses
            result = await self.api.create_worktree_api(branch_name)
            return result
        except Exception as e:
            return self.enrichment.add_ilha_context(
                {"error": f"Failed to create worktree: {str(e)}"}, 
                "create_worktree", 
                branch_name, 
                False
            )
    
    async def start_worktree(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Start containers for the specified worktree."""
        branch_name = arguments.get("branch_name")
        if not branch_name:
            return self.enrichment.add_ilha_context(
                {"error": "branch_name is required"}, 
                "start_worktree", 
                None, 
                False
            )
        
        try:
            # Use direct API for richer responses
            result = await self.api.start_worktree_api(branch_name)
            return result
        except Exception as e:
            return self.enrichment.add_ilha_context(
                {"error": f"Failed to start worktree: {str(e)}"}, 
                "start_worktree", 
                branch_name, 
                False
            )
    
    async def stop_worktree(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Stop containers for the specified worktree."""
        branch_name = arguments.get("branch_name")
        if not branch_name:
            return self.enrichment.add_ilha_context(
                {"error": "branch_name is required"}, 
                "stop_worktree", 
                None, 
                False
            )
        
        try:
            # Use direct API for richer responses
            result = await self.api.stop_worktree_api(branch_name)
            return result
        except Exception as e:
            return self.enrichment.add_ilha_context(
                {"error": f"Failed to stop worktree: {str(e)}"}, 
                "stop_worktree", 
                branch_name, 
                False
            )
    
    async def remove_worktree(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Remove worktree and containers but keep git branch."""
        branch_name = arguments.get("branch_name")
        force = arguments.get("force", False)
        if not branch_name:
            return self.enrichment.add_ilha_context(
                {"error": "branch_name is required"}, 
                "remove_worktree", 
                None, 
                False
            )
        
        try:
            # Use direct API for richer responses
            result = await self.api.remove_worktree_api(branch_name, force)
            return result
        except Exception as e:
            return self.enrichment.add_ilha_context(
                {"error": f"Failed to remove worktree: {str(e)}"}, 
                "remove_worktree", 
                branch_name, 
                False
            )
    
    async def delete_worktree(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Delete worktree and branch completely."""
        branch_name = arguments.get("branch_name")
        force = arguments.get("force", False)
        if not branch_name:
            return self.enrichment.add_ilha_context(
                {"error": "branch_name is required"}, 
                "delete_worktree", 
                None, 
                False
            )
        
        try:
            # Use direct API for richer responses
            result = await self.api.delete_worktree_api(branch_name, force)
            return result
        except Exception as e:
            return self.enrichment.add_ilha_context(
                {"error": f"Failed to delete worktree: {str(e)}"}, 
                "delete_worktree", 
                branch_name, 
                False
            )
    
    async def list_worktrees(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """List all active worktrees."""
        try:
            # Use direct API for richer responses
            result = await self.api.list_worktrees_api()
            return result
        except Exception as e:
            return self.enrichment.add_ilha_context(
                {"error": f"Failed to list worktrees: {str(e)}"}, 
                "list_worktrees", 
                None, 
                False
            )
    
    async def get_worktree_info(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Get detailed information about a specific worktree."""
        branch_name = arguments.get("branch_name")
        if not branch_name:
            return self.enrichment.add_ilha_context(
                {"error": "branch_name is required"}, 
                "get_worktree_info", 
                None, 
                False
            )
        
        try:
            # Use direct API for richer responses
            result = await self.api.get_worktree_status_api(branch_name)
            return result
        except Exception as e:
            return self.enrichment.add_ilha_context(
                {"error": f"Failed to get worktree info: {str(e)}"}, 
                "get_worktree_info", 
                branch_name, 
                False
            )
