"""
Push tools for ilha MCP server.

This module provides MCP tool implementations for pushing ilha packages
to remote servers via SCP for deployment.
"""

from pathlib import Path
from typing import Any, Dict

from ..utils.cli_wrapper import ilhaCLIWrapper
from ..utils.response_enrichment import ResponseEnrichment


class PushTools:
    """MCP tools for pushing packages to remote servers."""
    
    def __init__(self, config=None):
        """Initialize push tools."""
        self.config = config
        self.cli_wrapper = ilhaCLIWrapper(config) if config else None
        self.response_enrichment = ResponseEnrichment(config) if config else None
    
    async def push_package(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Push ilha package to remote server via SCP.
        
        Args:
            arguments: Dictionary containing:
                - branch_name: Name of the branch to push (optional, auto-detects if not provided)
                - scp_target: SCP target in format username@server:path (required)
                - output_dir: Temporary package location (optional, defaults to ./packages)
                - keep_package: Don't delete package after successful push (optional, defaults to false)
                - working_directory: Working directory (required)
        
        Returns:
            Dictionary with success status and push information
        """
        try:
            scp_target = arguments.get("scp_target")
            if not scp_target:
                return {
                    "success": False,
                    "error": "scp_target is required"
                }
            
            branch_name = arguments.get("branch_name")
            output_dir = arguments.get("output_dir", "./packages")
            keep_package = arguments.get("keep_package", False)
            
            # Build command arguments
            cmd_args = ["push"]
            
            # Add branch_name if provided, otherwise let it auto-detect
            if branch_name:
                cmd_args.append(branch_name)
            
            cmd_args.append(scp_target)
            
            if output_dir:
                cmd_args.extend(["--output-dir", output_dir])
            
            if keep_package:
                cmd_args.append("--keep-package")
            
            cmd_args.append("--json")
            
            # Run the command
            if self.cli_wrapper:
                result = await self.cli_wrapper.run_command(cmd_args)
            else:
                return {
                    "success": False,
                    "error": "CLI wrapper not initialized"
                }
            
            if result.get("success"):
                enriched_result = result.copy()
                enriched_result.update({
                    "message": f"Package pushed successfully to {scp_target}",
                    "push_info": {
                        "branch_name": branch_name or "auto-detected",
                        "scp_target": scp_target,
                        "output_dir": output_dir,
                        "keep_package": keep_package
                    }
                })
                if self.response_enrichment:
                    enriched_result = self.response_enrichment.add_ilha_context(
                        enriched_result, "push_package", branch_name, True
                    )
                return enriched_result
            else:
                return {
                    "success": False,
                    "error": result.get("error", "Failed to push package")
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Error pushing package: {str(e)}"
            }

