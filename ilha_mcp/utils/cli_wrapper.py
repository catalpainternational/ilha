"""
CLI wrapper utility for ilha MCP server.

This module provides a wrapper for invoking ilha CLI commands
with JSON output for MCP server integration.
"""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


class ilhaCLIWrapper:
    """Wrapper for ilha CLI commands with JSON output."""
    
    def __init__(self, config):
        """Initialize CLI wrapper."""
        self.config = config
        self.ilha_path = self._find_ilha_executable()
    
    def _find_ilha_executable(self) -> str:
        """Find the ilha executable."""
        # Try to find ilha in PATH
        import shutil
        ilha_path = shutil.which("ilha")
        if ilha_path:
            return ilha_path
        
        # Fallback to python -m ilha
        return "python"
    
    async def run_command(self, args: List[str]) -> Dict[str, Any]:
        """Run a ilha command and return JSON result."""
        try:
            # Build command
            if self.ilha_path == "python":
                cmd = ["python", "-m", "ilha"] + args
            else:
                cmd = [self.ilha_path] + args
            
            # Run command
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config.working_directory
            )
            
            stdout, stderr = await process.communicate()
            
            # Parse JSON output
            if process.returncode == 0:
                try:
                    result = json.loads(stdout.decode())
                    return result
                except json.JSONDecodeError:
                    # If JSON parsing fails, return the raw output
                    return {
                        "success": True,
                        "message": stdout.decode(),
                        "raw_output": stdout.decode()
                    }
            else:
                # Command failed
                error_msg = stderr.decode() if stderr else stdout.decode()
                return {
                    "success": False,
                    "error": error_msg,
                    "return_code": process.returncode
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to execute command: {str(e)}"
            }
    
    async def run_command_sync(self, args: List[str]) -> Dict[str, Any]:
        """Run a ilha command synchronously (for compatibility)."""
        try:
            # Build command
            if self.ilha_path == "python":
                cmd = ["python", "-m", "ilha"] + args
            else:
                cmd = [self.ilha_path] + args
            
            # Run command synchronously
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.config.working_directory
            )
            
            # Parse JSON output
            if result.returncode == 0:
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError:
                    return {
                        "success": True,
                        "message": result.stdout,
                        "raw_output": result.stdout
                    }
            else:
                return {
                    "success": False,
                    "error": result.stderr,
                    "return_code": result.returncode
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to execute command: {str(e)}"
            }


