"""
MCP resources for worktree data access.

This module provides MCP resource implementations for read-only access
to ilha worktree data with enhanced project context.
"""

from typing import Any, Dict
from pathlib import Path

from ..utils.cli_wrapper import ilhaCLIWrapper
from ..api.ilha_api import ilhaAPI


class WorktreeResources:
    """MCP resources for worktree data access with project context."""
    
    def __init__(self, config):
        """Initialize worktree resources."""
        self.config = config
        self.cli_wrapper = ilhaCLIWrapper(config)
        self.api = ilhaAPI(config)
    
    async def get_project_context(self) -> Dict[str, Any]:
        """Get current project context and configuration."""
        try:
            context = self.config.get_project_context()
            
            # Add ilha concept explanation
            context.update({
                "ilha_concept": "ilha creates isolated development environments for Git branches using Git worktrees, Docker Compose, and Caddy proxy",
                "url_pattern": self.config.get_url_pattern(),
                "benefits": [
                    "Complete environment isolation - each branch gets its own database, Redis, and media storage",
                    "No port conflicts - uses dynamic routing with Caddy proxy",
                    "Easy parallel development - work on multiple features simultaneously",
                    "Safe database testing - test migrations and data changes in isolation"
                ]
            })
            
            return {
                "success": True,
                "data": context
            }
        except Exception as e:
            return {"error": f"Failed to get project context: {str(e)}"}
    
    async def get_worktrees_with_urls(self) -> Dict[str, Any]:
        """Get all worktrees with their actual access URLs."""
        try:
            result = await self.api.list_worktrees_api()
            
            if result.get("success"):
                worktrees = result.get("data", [])
                for worktree in worktrees:
                    # Project name is already sanitized by MCPConfig, so we can use it directly
                    worktree["access_url"] = f"http://{self.config.project_name}-{worktree['branch']}.localhost"
                    worktree["ilha_context"] = {
                        "concept": "Isolated development environment with branch-specific containers and data",
                        "isolation": "Complete separation from other branches and main development"
                    }
                
                return {
                    "success": True,
                    "data": worktrees,
                    "ilha_context": {
                        "concept": f"Found {len(worktrees)} isolated development environments",
                        "project": self.config.project_name,
                        "url_pattern": self.config.get_url_pattern(),
                        "benefits": "Each worktree runs independently with isolated data"
                    }
                }
            else:
                return result
        except Exception as e:
            return {"error": f"Failed to get worktrees with URLs: {str(e)}"}
    
    async def get_volumes_with_branches(self) -> Dict[str, Any]:
        """Get volumes with branch mapping and naming explanation."""
        try:
            result = await self.cli_wrapper.run_command(["volumes", "list", "--json"])
            
            if result.get("success"):
                volumes = result.get("data", [])
                
                # Add ilha context to volumes
                for volume in volumes:
                    volume["ilha_context"] = {
                        "naming_pattern": f"{self.config.project_name}_{{branch_name}}_{{volume_type}}",
                        "isolation": "Each worktree has completely separate data volumes",
                        "benefits": "No data conflicts between branches, safe testing"
                    }
                
                return {
                    "success": True,
                    "data": volumes,
                    "ilha_context": {
                        "concept": "Branch-specific volumes ensure complete data isolation",
                        "naming": f"Volumes follow pattern: {self.config.project_name}_{{branch}}_{{type}}",
                        "isolation": "Each worktree has its own database, Redis, and media storage",
                        "examples": [
                            f"{self.config.project_name}_feature-auth_postgres_data",
                            f"{self.config.project_name}_feature-auth_redis_data",
                            f"{self.config.project_name}_feature-auth_media_files"
                        ]
                    }
                }
            else:
                return result
        except Exception as e:
            return {"error": f"Failed to get volumes with branches: {str(e)}"}
    
    async def get_proxy_with_routes(self) -> Dict[str, Any]:
        """Get proxy status with current routing table."""
        try:
            result = await self.cli_wrapper.run_command(["start-proxy", "--json"])
            
            if result.get("success"):
                return {
                    "success": True,
                    "data": {
                        "status": "running",
                        "message": "Caddy proxy is running",
                        "routing": {
                            "pattern": f"{self.config.project_name}-{{branch}}.localhost",
                            "examples": [
                                f"{self.config.project_name}-feature-auth.localhost",
                                f"{self.config.project_name}-bugfix-payment.localhost"
                            ]
                        }
                    },
                    "ilha_context": {
                        "concept": "Global Caddy proxy provides dynamic routing to worktree containers",
                        "benefits": "No port conflicts, automatic service discovery, unique URLs per worktree",
                        "routing": f"Routes {self.config.project_name}-{{branch}}.localhost to worktree containers"
                    }
                }
            else:
                return {
                    "success": False,
                    "data": {
                        "status": "stopped",
                        "message": "Caddy proxy is not running"
                    },
                    "ilha_context": {
                        "concept": "Caddy proxy is required for worktree access via URLs",
                        "next_steps": [
                            "Use start_proxy() to start the global proxy",
                            "Then create and start worktrees for isolated development"
                        ]
                    }
                }
        except Exception as e:
            return {"error": f"Failed to get proxy with routes: {str(e)}"}
    
    async def get_complete_state(self) -> Dict[str, Any]:
        """Get complete current state summary."""
        try:
            # Get all state information
            project_context = await self.get_project_context()
            worktrees = await self.get_worktrees_with_urls()
            volumes = await self.get_volumes_with_branches()
            proxy = await self.get_proxy_with_routes()
            
            return {
                "success": True,
                "data": {
                    "project": project_context.get("data", {}),
                    "worktrees": worktrees.get("data", []),
                    "volumes": volumes.get("data", []),
                    "proxy": proxy.get("data", {})
                },
                "ilha_context": {
                    "concept": "Complete ilha system state with isolated development environments",
                    "project": self.config.project_name,
                    "summary": f"Project '{self.config.project_name}' with {len(worktrees.get('data', []))} worktrees",
                    "benefits": "Isolated development environments with complete data separation"
                }
            }
        except Exception as e:
            return {"error": f"Failed to get complete state: {str(e)}"}
    
    # Legacy methods for backward compatibility
    async def get_worktrees(self) -> Dict[str, Any]:
        """Get list of all active worktrees (legacy method)."""
        return await self.get_worktrees_with_urls()
    
    async def get_volumes(self) -> Dict[str, Any]:
        """Get list of all worktree volumes (legacy method)."""
        return await self.get_volumes_with_branches()
    
    async def get_proxy_status(self) -> Dict[str, Any]:
        """Get status of the global Caddy proxy (legacy method)."""
        return await self.get_proxy_with_routes()
