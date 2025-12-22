"""
Package management tools for ilha MCP server.

This module provides MCP tool wrappers for package export/import operations
to enable sharing complete isolated development environments.
"""

from pathlib import Path
from typing import Any, Dict

from ..utils.cli_wrapper import ilhaCLIWrapper
from ..utils.response_enrichment import ResponseEnrichment


class PackageTools:
    """MCP tools for package management operations."""
    
    def __init__(self, config=None):
        """Initialize package tools."""
        self.config = config
        self.cli_wrapper = ilhaCLIWrapper(config) if config else None
        self.response_enrichment = ResponseEnrichment(config) if config else None
    
    async def export_package(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Export worktree environment to shareable package.
        
        Args:
            arguments: Dictionary containing:
                - branch_name: Name of the branch to export
                - output_dir: Output directory for packages (optional, defaults to ./packages)
                - include_code: Whether to include git archive of code (optional, defaults to false)
                - compressed: Whether to compress package to .tar.gz (optional, defaults to true)
        
        Returns:
            Dictionary with success status and package information
        """
        try:
            branch_name = arguments.get("branch_name")
            if not branch_name:
                return {
                    "success": False,
                    "error": "branch_name is required"
                }
            
            output_dir = arguments.get("output_dir", "./packages")
            include_code = arguments.get("include_code", False)
            compressed = arguments.get("compressed", True)
            
            # Build command arguments
            cmd_args = [
                "packages", "export", branch_name,
                "--output-dir", output_dir
            ]
            
            if include_code:
                cmd_args.append("--include-code")
            else:
                cmd_args.append("--no-code")
            
            if compressed:
                cmd_args.append("--compressed")
            else:
                cmd_args.append("--no-compress")
            
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
                    "message": f"Package exported successfully for branch '{branch_name}'",
                    "package_info": {
                        "branch_name": branch_name,
                        "output_dir": output_dir,
                        "include_code": include_code,
                        "compressed": compressed
                    }
                })
                if self.response_enrichment:
                    enriched_result = self.response_enrichment.add_ilha_context(
                        enriched_result, "export_package", branch_name, True
                    )
                return enriched_result
            else:
                return {
                    "success": False,
                    "error": result.get("error", "Failed to export package")
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Error exporting package: {str(e)}"
            }
    
    async def import_package(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Import environment from package with auto-detection.
        
        Args:
            arguments: Dictionary containing:
                - package_file: Path to the package file
                - target_branch: Target branch name (optional, for normal mode)
                - restore_data: Whether to restore volume data (optional, defaults to true)
                - standalone: Force standalone mode (optional, None = auto-detect)
                - target_directory: Target directory for standalone import (optional)
                - domain: Domain override (subdomain.domain.tld) for production/staging (optional)
                - working_directory: Working directory (optional)
        
        Returns:
            Dictionary with success status and import information
        """
        try:
            package_file = arguments.get("package_file")
            if not package_file:
                return {
                    "success": False,
                    "error": "package_file is required"
                }
            
            target_branch = arguments.get("target_branch")
            restore_data = arguments.get("restore_data", True)
            standalone = arguments.get("standalone")
            target_directory = arguments.get("target_directory")
            domain = arguments.get("domain")
            ip = arguments.get("ip")
            
            # Build command arguments
            cmd_args = [
                "packages", "import", package_file
            ]
            
            if target_branch:
                cmd_args.extend(["--target-branch", target_branch])
            
            if restore_data:
                cmd_args.append("--restore-data")
            else:
                cmd_args.append("--no-data")
            
            if standalone is True:
                cmd_args.append("--standalone")
            
            if target_directory:
                cmd_args.extend(["--target-dir", target_directory])
            
            if domain:
                cmd_args.extend(["--domain", domain])
            if ip:
                cmd_args.extend(["--ip", ip])
            
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
                mode = result.get("mode", "normal")
                enriched_result = result.copy()
                
                if mode == "standalone":
                    enriched_result.update({
                        "message": f"Standalone import completed from {package_file}",
                        "import_mode": "standalone",
                        "import_info": {
                            "package_file": package_file,
                            "project_directory": result.get("project_directory"),
                            "branch_name": result.get("branch_name"),
                            "restore_data": restore_data
                        }
                    })
                else:
                    enriched_result.update({
                        "message": f"Package imported successfully from {package_file}",
                        "import_mode": "normal",
                        "import_info": {
                            "package_file": package_file,
                            "target_branch": target_branch,
                            "restore_data": restore_data
                        }
                    })
                
                if self.response_enrichment:
                    enriched_result = self.response_enrichment.add_ilha_context(
                        enriched_result, "import_package", target_branch, True
                    )
                return enriched_result
            else:
                return {
                    "success": False,
                    "error": result.get("error", "Failed to import package")
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Error importing package: {str(e)}"
            }
    
    async def list_packages(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """List available packages.
        
        Args:
            arguments: Dictionary containing:
                - package_dir: Package directory to search (optional, defaults to ./packages)
        
        Returns:
            Dictionary with success status and list of packages
        """
        try:
            package_dir = arguments.get("package_dir", "./packages")
            
            # Build command arguments
            cmd_args = [
                "packages", "list",
                "--package-dir", package_dir,
                "--json"
            ]
            
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
                    "message": f"Found packages in {package_dir}",
                    "package_dir": package_dir
                })
                if self.response_enrichment:
                    enriched_result = self.response_enrichment.add_ilha_context(
                        enriched_result, "list_packages", None, True
                    )
                return enriched_result
            else:
                return {
                    "success": False,
                    "error": result.get("error", "Failed to list packages")
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Error listing packages: {str(e)}"
            }
    
    async def validate_package(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Validate package integrity.
        
        Args:
            arguments: Dictionary containing:
                - package_file: Path to the package file
        
        Returns:
            Dictionary with success status and validation results
        """
        try:
            package_file = arguments.get("package_file")
            if not package_file:
                return {
                    "success": False,
                    "error": "package_file is required"
                }
            
            # Build command arguments
            cmd_args = [
                "packages", "validate", package_file,
                "--json"
            ]
            
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
                    "message": f"Package validation completed for {package_file}",
                    "package_file": package_file
                })
                if self.response_enrichment:
                    enriched_result = self.response_enrichment.add_ilha_context(
                        enriched_result, "validate_package", None, True
                    )
                return enriched_result
            else:
                return {
                    "success": False,
                    "error": result.get("error", "Package validation failed")
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Error validating package: {str(e)}"
            }
    
    async def get_package_info(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Get detailed package information.
        
        Args:
            arguments: Dictionary containing:
                - package_file: Path to the package file
        
        Returns:
            Dictionary with success status and package information
        """
        try:
            package_file = arguments.get("package_file")
            if not package_file:
                return {
                    "success": False,
                    "error": "package_file is required"
                }
            
            # Use validate command to get package info
            cmd_args = [
                "packages", "validate", package_file,
                "--json"
            ]
            
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
                    "message": f"Package information retrieved for {package_file}",
                    "package_file": package_file
                })
                if self.response_enrichment:
                    enriched_result = self.response_enrichment.add_ilha_context(
                        enriched_result, "get_package_info", None, True
                    )
                return enriched_result
            else:
                return {
                    "success": False,
                    "error": result.get("error", "Failed to get package information")
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Error getting package information: {str(e)}"
            }
