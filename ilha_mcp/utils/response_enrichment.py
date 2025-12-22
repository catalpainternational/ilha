"""
Response enrichment utilities for ilha MCP server.

This module provides utilities to enrich MCP tool responses with
ilha-specific context, next steps, and troubleshooting guidance.
"""

from typing import Any, Dict, List, Optional
from ..config import MCPConfig


class ResponseEnrichment:
    """Enriches responses with ilha context and guidance."""
    
    def __init__(self, config: MCPConfig):
        """Initialize response enrichment."""
        self.config = config
        self.project_name = config.project_name
        self.url_pattern = config.get_url_pattern()
    
    def add_ilha_context(
        self, 
        response: Dict[str, Any], 
        operation: str,
        branch_name: Optional[str] = None,
        success: bool = True
    ) -> Dict[str, Any]:
        """Add comprehensive ilha context to any response."""
        
        # Base ilha concept explanation
        concept_explanations = {
            "create_worktree": "ilha creates isolated development environments by combining Git worktrees (separate working directories) with Docker Compose (isolated containers) and Caddy proxy (dynamic routing). Each branch gets its own database, Redis, media storage, and unique URL.",
            "start_worktree": "ilha starts isolated Docker containers for a worktree, providing complete environment isolation. Each worktree runs independently with its own services and data.",
            "stop_worktree": "ilha stops containers while preserving all data in branch-specific volumes. The worktree directory and volumes remain intact for later restart.",
            "remove_worktree": "ilha removes the isolated environment and containers but preserves the Git branch. This is useful when you want to keep your code changes but clean up the development environment.",
            "delete_worktree": "ilha completely removes the isolated environment, containers, volumes, and Git branch. This is a destructive operation that cannot be undone.",
            "list_worktrees": "ilha shows all active isolated development environments with their current status, URLs, and container information.",
            "get_worktree_info": "ilha provides detailed information about a specific isolated environment including container status, volumes, and access URLs."
        }
        
        # Add ilha context
        ilha_context = {
            "concept": concept_explanations.get(operation, "ilha creates isolated development environments for Git branches"),
            "project": self.project_name,
            "workflow_stage": self._get_workflow_stage(operation, success)
        }
        
        # Add branch-specific context if available
        if branch_name:
            ilha_context.update({
                "branch": branch_name,
                "url": self.url_pattern.format(branch_name=branch_name)
            })
        
        # Add next steps
        ilha_context["next_steps"] = self._get_next_steps(operation, branch_name, success)
        
        # Add related operations
        ilha_context["related_operations"] = self._get_related_operations(operation)
        
        # Add troubleshooting if error
        if not success:
            ilha_context["troubleshooting"] = self._get_troubleshooting(operation, branch_name)
        
        # Add to response
        response["ilha_context"] = ilha_context
        return response
    
    def _get_workflow_stage(self, operation: str, success: bool) -> str:
        """Determine the current workflow stage."""
        if not success:
            return "error"
        
        stage_mapping = {
            "create_worktree": "created",
            "start_worktree": "running", 
            "stop_worktree": "stopped",
            "remove_worktree": "removed",
            "delete_worktree": "deleted",
            "list_worktrees": "discovery",
            "get_worktree_info": "inspection"
        }
        return stage_mapping.get(operation, "unknown")
    
    def _get_next_steps(
        self, 
        operation: str, 
        branch_name: Optional[str], 
        success: bool
    ) -> List[str]:
        """Get workflow-aware next steps."""
        
        if not success:
            return [
                "Check the error message and troubleshooting tips above",
                "Verify Docker is running: docker ps",
                "Ensure you're in a Git repository with ilha initialized"
            ]
        
        next_steps_mapping = {
            "create_worktree": [
                f"Use start_worktree('{branch_name}') to launch the environment",
                f"Access at {self.url_pattern.format(branch_name=branch_name)}",
                f"Use get_worktree_info('{branch_name}') to see detailed status"
            ],
            "start_worktree": [
                f"Access your environment at {self.url_pattern.format(branch_name=branch_name)}",
                f"Use get_worktree_info('{branch_name}') to see container status",
                f"Use stop_worktree('{branch_name}') to stop when done"
            ],
            "stop_worktree": [
                f"Data is preserved in branch-specific volumes",
                f"Use start_worktree('{branch_name}') to restart the environment",
                f"Use remove_worktree('{branch_name}') to clean up completely"
            ],
            "remove_worktree": [
                f"Git branch '{branch_name}' is preserved",
                f"Use create_worktree('{branch_name}') to recreate environment",
                f"Use delete_worktree('{branch_name}') to also delete git branch"
            ],
            "delete_worktree": [
                f"Environment and git branch '{branch_name}' are completely removed",
                f"Create new branch and environment: create_worktree('new-feature')",
                f"Use remove_worktree() to keep git branch in future"
            ],
            "list_worktrees": [
                f"Access any running environment at {self.url_pattern.format(branch_name='{branch}')}",
                f"Use get_worktree_info('branch-name') for detailed status",
                f"Create new environment: create_worktree('new-feature')"
            ],
            "get_worktree_info": [
                f"Access at {self.url_pattern.format(branch_name=branch_name)}" if branch_name else "Check the URL in the response",
                f"Use stop_worktree('{branch_name}') to stop" if branch_name else "Use stop_worktree() to stop",
                f"Use start_worktree('{branch_name}') to start" if branch_name else "Use start_worktree() to start"
            ]
        }
        
        return next_steps_mapping.get(operation, [
            f"Check the response data for details",
            f"Use list_worktrees() to see all environments",
            f"Use get_worktree_info() for detailed status"
        ])
    
    def _get_related_operations(self, operation: str) -> List[str]:
        """Get related operations for the current operation."""
        
        related_mapping = {
            "create_worktree": ["start_worktree", "get_worktree_info", "list_worktrees"],
            "start_worktree": ["get_worktree_info", "stop_worktree", "list_worktrees"],
            "stop_worktree": ["start_worktree", "remove_worktree", "get_worktree_info"],
            "remove_worktree": ["create_worktree", "delete_worktree", "list_worktrees"],
            "delete_worktree": ["create_worktree", "list_worktrees"],
            "list_worktrees": ["get_worktree_info", "create_worktree", "start_worktree"],
            "get_worktree_info": ["start_worktree", "stop_worktree", "remove_worktree"]
        }
        
        return related_mapping.get(operation, ["list_worktrees", "get_worktree_info"])
    
    def _get_troubleshooting(self, operation: str, branch_name: Optional[str]) -> List[str]:
        """Get troubleshooting guidance for errors."""
        
        base_troubleshooting = [
            "Check if Docker is running: docker ps",
            "Verify you're in a Git repository",
            "Ensure ilha is initialized: ilha setup"
        ]
        
        operation_specific = {
            "create_worktree": [
                "Check if branch exists: git branch -a",
                "Verify worktree doesn't already exist",
                "Ensure .ilha directory exists"
            ],
            "start_worktree": [
                "Check if worktree exists",
                "Verify docker-compose.yml exists",
                "Check container logs for errors"
            ],
            "stop_worktree": [
                "Check if containers are actually running",
                "Verify branch name is correct",
                "Check Docker daemon status"
            ],
            "remove_worktree": [
                "Check if worktree exists",
                "Verify Docker is running: docker ps",
                "Check for permission issues"
            ],
            "delete_worktree": [
                "Check if worktree exists",
                "Verify git branch exists",
                "Use --force flag if branch has unmerged changes"
            ],
            "list_worktrees": [
                "Check if .ilha/worktrees directory exists",
                "Verify Docker daemon status",
                "Check for permission issues"
            ],
            "get_worktree_info": [
                "Check if worktree exists",
                "Verify Docker is running: docker ps",
                "Check for container naming conflicts"
            ]
        }
        
        specific = operation_specific.get(operation, [])
        return base_troubleshooting + specific
    
    def enrich_volume_response(
        self, 
        response: Dict[str, Any], 
        operation: str,
        branch_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Enrich volume-related responses with ilha context."""
        
        volume_concept = "ilha creates branch-specific volumes for complete data isolation. Each worktree gets its own database, Redis, and media storage volumes with names like '{branch_name}_postgres_data'."
        
        ilha_context = {
            "concept": volume_concept,
            "project": self.project_name,
            "volume_naming": f"Volumes follow pattern: {self.project_name}_{{branch_name}}_{{volume_type}}",
            "isolation": "Each worktree has completely isolated data - no conflicts between branches"
        }
        
        if branch_name:
            ilha_context.update({
                "branch": branch_name,
                "example_volumes": [
                    f"{self.project_name}_{branch_name}_postgres_data",
                    f"{self.project_name}_{branch_name}_redis_data", 
                    f"{self.project_name}_{branch_name}_media_files"
                ]
            })
        
        response["ilha_context"] = ilha_context
        return response
    
    def enrich_proxy_response(
        self, 
        response: Dict[str, Any], 
        operation: str
    ) -> Dict[str, Any]:
        """Enrich proxy-related responses with ilha context."""
        
        proxy_concept = "ilha uses a global Caddy proxy for dynamic routing. The proxy automatically routes requests to the correct worktree based on subdomain patterns like 'project-branch.localhost'."
        
        ilha_context = {
            "concept": proxy_concept,
            "project": self.project_name,
            "routing": f"Routes {self.project_name}-{{branch}}.localhost to worktree containers",
            "global": "Single proxy handles all worktrees - no port conflicts"
        }
        
        if operation == "start_proxy":
            ilha_context["next_steps"] = [
                "Proxy is now running and ready to route worktree requests",
                f"Create worktrees and access at {self.url_pattern.format(branch_name='{branch}')}",
                "Use get_proxy_status() to check routing table"
            ]
        elif operation == "stop_proxy":
            ilha_context["next_steps"] = [
                "All worktrees are now inaccessible via URLs",
                "Worktree containers may still be running but not accessible",
                "Use start_proxy() to restore access to all worktrees"
            ]
        
        response["ilha_context"] = ilha_context
        return response
