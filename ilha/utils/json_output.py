"""
JSON output utilities for ilha CLI.

This module provides standardized JSON output formatting for CLI commands
to support MCP server integration.
"""

import json
import sys
from typing import Any, Dict, List, Optional, Union
from pathlib import Path

from .logging import log_info, log_success, log_warning, log_error


class JSONOutput:
    """Standardized JSON output formatter for ilha CLI commands."""
    
    @staticmethod
    def success(message: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Format a successful operation result."""
        result = {
            "success": True,
            "message": message,
            "timestamp": __import__("datetime").datetime.now().isoformat()
        }
        if data:
            result["data"] = data
        return result
    
    @staticmethod
    def error(message: str, error_code: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Format an error result."""
        result = {
            "success": False,
            "error": message,
            "timestamp": __import__("datetime").datetime.now().isoformat()
        }
        if error_code:
            result["error_code"] = error_code
        if details:
            result["details"] = details
        return result
    
    @staticmethod
    def worktree_info(branch_name: str, path: str, status: str, commit: str = None) -> Dict[str, Any]:
        """Format worktree information."""
        info = {
            "branch": branch_name,
            "path": str(Path(path).resolve()),
            "status": status
        }
        if commit:
            info["commit"] = commit
        return info
    
    @staticmethod
    def volume_info(name: str, size: str = None, mountpoint: str = None) -> Dict[str, Any]:
        """Format volume information."""
        info = {
            "name": name,
            "type": "worktree_volume"
        }
        if size:
            info["size"] = size
        if mountpoint:
            info["mountpoint"] = mountpoint
        return info
    
    @staticmethod
    def container_info(name: str, status: str, ports: List[str] = None, image: str = None) -> Dict[str, Any]:
        """Format container information."""
        info = {
            "name": name,
            "status": status
        }
        if ports:
            info["ports"] = ports
        if image:
            info["image"] = image
        return info
    
    @staticmethod
    def print_json(data: Union[Dict[str, Any], List[Any]]) -> None:
        """Print data as JSON to stdout."""
        print(json.dumps(data, indent=2))
        sys.stdout.flush()
    
    @staticmethod
    def print_success(message: str, data: Optional[Dict[str, Any]] = None, json_output: bool = False) -> None:
        """Print success message, optionally as JSON."""
        if json_output:
            JSONOutput.print_json(JSONOutput.success(message, data))
        else:
            log_success(message)
    
    @staticmethod
    def print_error(message: str, error_code: Optional[str] = None, details: Optional[Dict[str, Any]] = None, json_output: bool = False) -> None:
        """Print error message, optionally as JSON."""
        if json_output:
            JSONOutput.print_json(JSONOutput.error(message, error_code, details))
        else:
            log_error(message)
    
    @staticmethod
    def print_info(message: str, data: Optional[Dict[str, Any]] = None, json_output: bool = False) -> None:
        """Print info message, optionally as JSON."""
        if json_output:
            JSONOutput.print_json(JSONOutput.success(message, data))
        else:
            log_info(message)
    
    @staticmethod
    def print_warning(message: str, data: Optional[Dict[str, Any]] = None, json_output: bool = False) -> None:
        """Print warning message, optionally as JSON."""
        if json_output:
            JSONOutput.print_json(JSONOutput.success(message, data))
        else:
            log_warning(message)


def add_json_option(f):
    """Decorator to add --json option to CLI commands."""
    import click
    return click.option('--json', is_flag=True, default=False, 
                        help='Output as JSON format')(f)


def handle_json_output(func):
    """Decorator to handle JSON output for CLI commands."""
    def wrapper(*args, **kwargs):
        json_output = kwargs.pop('json', False)
        try:
            result = func(*args, **kwargs)
            if json_output:
                if isinstance(result, dict):
                    JSONOutput.print_json(result)
                elif isinstance(result, bool):
                    JSONOutput.print_json(JSONOutput.success("Operation completed" if result else "Operation failed"))
                else:
                    JSONOutput.print_json(JSONOutput.success("Operation completed", {"result": result}))
            return result
        except Exception as e:
            if json_output:
                JSONOutput.print_error(str(e), "command_error")
            else:
                raise
    return wrapper
