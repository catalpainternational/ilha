"""
Configuration management for ilha MCP server.

This module provides configuration management for the MCP server with
enhanced project detection and context awareness.
"""

import os
import yaml
from pathlib import Path
from typing import Optional, Dict, Any


class MCPConfig:
    """Configuration for ilha MCP server with project detection."""
    
    def __init__(
        self,
        working_directory: Optional[Path] = None,
        ilha_path: Optional[str] = None,
        timeout: int = 300,
        verbose: bool = False
    ):
        """Initialize MCP configuration.
        
        Args:
            working_directory: Target project directory where ilha should operate.
                              Defaults to current working directory if not provided.
                              This is the directory containing the project's docker-compose.yml
                              and where .ilha/ directory will be created.
            ilha_path: Path to ilha executable (optional)
            timeout: Command timeout in seconds
            verbose: Enable verbose logging
        """
        # Validate and set working directory
        if working_directory is None:
            working_directory = Path.cwd()
        elif isinstance(working_directory, str):
            working_directory = Path(working_directory)
        
        # Track whether the directory currently exists but avoid hard failures so
        # tests (and callers eagerly configuring future paths) can still proceed.
        missing_directory = not working_directory.exists()
        if not missing_directory and not working_directory.is_dir():
            raise ValueError(f"Working directory is not a directory: {working_directory}")

        # Resolve to absolute path even if the directory does not currently exist.
        self.working_directory = working_directory.resolve()
        self.working_directory_exists = not missing_directory
        self.ilha_path = ilha_path
        self.timeout = timeout
        self.verbose = verbose
        
        # Environment variables
        self.env = os.environ.copy()
        
        # Project context (detected)
        self.project_name = self._detect_project_name()
        self.ilha_initialized = self._is_ilha_initialized()
        self.ilha_config = self._read_ilha_config()
        self.available_services = self._get_available_services()
    
    def _detect_project_name(self) -> str:
        """Detect project name from ilha config or directory."""
        try:
            # Try to read from .ilha/config.yml
            config_path = self.working_directory / ".ilha" / "config.yml"
            if config_path.exists():
                with open(config_path, 'r') as f:
                    config_data = yaml.safe_load(f)
                    raw_name = config_data.get('project_name', 'unknown')
            else:
                # Fallback to directory name
                raw_name = self.working_directory.name
            
            # Sanitize the project name for URL compatibility
            from ilha.config.settings import sanitize_project_name
            return sanitize_project_name(raw_name)
        except Exception:
            # Fallback to directory name with sanitization
            from ilha.config.settings import sanitize_project_name
            return sanitize_project_name(self.working_directory.name)
    
    def _is_ilha_initialized(self) -> bool:
        """Check if ilha is initialized in this project."""
        ilha_dir = self.working_directory / ".ilha"
        # Check if .ilha exists and has either config.yml OR worktrees directory
        return ilha_dir.exists() and (
            (ilha_dir / "config.yml").exists() or 
            (ilha_dir / "worktrees").exists()
        )
    
    def _read_ilha_config(self) -> Dict[str, Any]:
        """Read ilha configuration if available."""
        try:
            config_path = self.working_directory / ".ilha" / "config.yml"
            if config_path.exists():
                with open(config_path, 'r') as f:
                    return yaml.safe_load(f) or {}
        except Exception:
            pass
        return {}
    
    def _get_available_services(self) -> list:
        """Get available services from docker-compose.yml."""
        try:
            compose_path = self.working_directory / "docker-compose.yml"
            if compose_path.exists():
                with open(compose_path, 'r') as f:
                    compose_data = yaml.safe_load(f)
                    return list(compose_data.get('services', {}).keys())
        except Exception:
            pass
        return []
    
    def get_url_pattern(self) -> str:
        """Generate actual URL pattern for this project."""
        return f"http://{self.project_name}-{{branch_name}}.localhost"
    
    def validate_for_ilha(self) -> Dict[str, Any]:
        """Validate that the working directory is suitable for ilha operations.
        
        Returns:
            Dict with validation results including warnings and errors
        """
        results = {
            "valid": True,
            "warnings": [],
            "errors": [],
            "suggestions": []
        }
        
        # Check for docker-compose.yml
        compose_file = self.working_directory / "docker-compose.yml"
        if not compose_file.exists():
            results["warnings"].append("No docker-compose.yml found in working directory")
            results["suggestions"].append("Ensure you're in the correct project directory with docker-compose.yml")
        
        # Check if it's a git repository
        git_dir = self.working_directory / ".git"
        if not git_dir.exists():
            results["warnings"].append("Not a Git repository")
            results["suggestions"].append("Initialize with 'git init' or navigate to a Git repository")
        
        # Check for existing ilha setup
        ilha_dir = self.working_directory / ".ilha"
        if ilha_dir.exists():
            results["suggestions"].append("ilha is already initialized in this directory")
        else:
            results["suggestions"].append("Run 'ilha setup' to initialize ilha in this directory")
        
        # Check write permissions
        try:
            test_file = self.working_directory / ".ilha_test_write"
            test_file.touch()
            test_file.unlink()
        except PermissionError:
            results["errors"].append("No write permission in working directory")
            results["valid"] = False
        
        return results
    
    def get_project_context(self) -> Dict[str, Any]:
        """Get comprehensive project context."""
        return {
            "project_name": self.project_name,
            "working_directory": str(self.working_directory),
            "ilha_initialized": self.ilha_initialized,
            "url_pattern": self.get_url_pattern(),
            "ilha_dir": str(self.working_directory / ".ilha"),
            "worktrees_dir": str(self.working_directory / ".ilha" / "worktrees"),
            "available_services": self.available_services,
            "config": self.ilha_config
        }
    
    @classmethod
    def from_env(cls) -> "MCPConfig":
        """Create configuration from environment variables."""
        working_dir = os.getenv("ilha_WORKING_DIR")
        ilha_path = os.getenv("ilha_PATH")
        timeout = int(os.getenv("ilha_TIMEOUT", "300"))
        verbose = os.getenv("ilha_VERBOSE", "false").lower() == "true"
        
        return cls(
            working_directory=Path(working_dir) if working_dir else None,
            ilha_path=ilha_path,
            timeout=timeout,
            verbose=verbose
        )
    
    def to_dict(self) -> dict:
        """Convert configuration to dictionary."""
        return {
            "working_directory": str(self.working_directory),
            "ilha_path": self.ilha_path,
            "timeout": self.timeout,
            "verbose": self.verbose,
            "project_name": self.project_name,
            "ilha_initialized": self.ilha_initialized,
            "available_services": self.available_services
        }
