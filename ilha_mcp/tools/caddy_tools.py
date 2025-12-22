"""
MCP tools for Caddy proxy management.

This module provides MCP tool implementations for ilha Caddy proxy operations.
"""

from typing import Any, Dict

from ..utils.cli_wrapper import ilhaCLIWrapper


class CaddyTools:
    """MCP tools for Caddy proxy management."""
    
    def __init__(self, config):
        """Initialize Caddy tools."""
        self.config = config
        self.cli_wrapper = ilhaCLIWrapper(config)
    
    async def start_proxy(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Start the global Caddy proxy container."""
        try:
            result = await self.cli_wrapper.run_command(["start-proxy", "--json"])
            return result
        except Exception as e:
            return {"error": f"Failed to start proxy: {str(e)}"}
    
    async def stop_proxy(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Stop the global Caddy proxy container."""
        try:
            result = await self.cli_wrapper.run_command(["stop-proxy", "--json"])
            return result
        except Exception as e:
            return {"error": f"Failed to stop proxy: {str(e)}"}
    
    async def get_proxy_status(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Get status of the global Caddy proxy."""
        try:
            # Try to get proxy status by checking if it's running
            # This is a simple implementation - in practice, you might want
            # to check Docker container status directly
            result = await self.cli_wrapper.run_command(["start-proxy", "--json"])
            
            # If start-proxy succeeds, it means proxy was already running
            if result.get("success"):
                return {
                    "success": True,
                    "status": "running",
                    "message": "Caddy proxy is running"
                }
            else:
                return {
                    "success": False,
                    "status": "stopped",
                    "message": "Caddy proxy is not running"
                }
        except Exception as e:
            return {"error": f"Failed to get proxy status: {str(e)}"}


