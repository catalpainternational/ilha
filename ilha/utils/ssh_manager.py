"""
SSH connection manager for ilha CLI.

This module provides SSH connection management with connection reuse,
host key caching, and unified remote command execution.
"""

import subprocess
import os
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
from contextlib import contextmanager

from .logging import log_info, log_warning, log_error
from .ssh_utils import add_ssh_host_key


class SSHConnectionManager:
    """Manages SSH connections with reuse and caching."""
    
    def __init__(self):
        """Initialize SSH connection manager."""
        self._host_keys_added: set = set()
        self._control_paths: Dict[str, str] = {}
        self._temp_dir: Optional[Path] = None
    
    def _get_temp_dir(self) -> Path:
        """Get or create temporary directory for SSH control sockets.
        
        Uses /tmp/ with a short path to avoid Unix socket path length limits (~104 bytes on macOS).
        """
        if self._temp_dir is None:
            # Use /tmp with short path to avoid Unix socket length limits (~104 bytes)
            import secrets
            short_id = secrets.token_hex(4)  # 8 characters
            temp_path = Path(f"/tmp/ilha-{short_id}")
            temp_path.mkdir(parents=True, exist_ok=True)
            self._temp_dir = temp_path
        return self._temp_dir
    
    def _get_control_path(self, username: str, server: str) -> str:
        """Get control path for SSH connection reuse."""
        key = f"{username}@{server}"
        if key not in self._control_paths:
            temp_dir = self._get_temp_dir()
            # Use a safe filename based on server
            safe_name = server.replace('.', '_').replace(':', '_')
            control_path = temp_dir / f"control-{safe_name}"
            self._control_paths[key] = str(control_path)
        return self._control_paths[key]
    
    def ensure_host_key(self, server: str, port: int = 22) -> bool:
        """Ensure SSH host key is added (cached check).
        
        Args:
            server: Server hostname or IP
            port: SSH port
            
        Returns:
            True if host key is available, False otherwise
        """
        key = f"{server}:{port}"
        if key not in self._host_keys_added:
            if add_ssh_host_key(server, port):
                self._host_keys_added.add(key)
                return True
            return False
        return True
    
    def build_ssh_command(self, username: str, server: str, 
                         command: Optional[str] = None,
                         use_control_master: bool = True) -> List[str]:
        """Build SSH command with connection reuse.
        
        Args:
            username: SSH username
            server: Server hostname or IP
            command: Remote command to execute (optional)
            use_control_master: Whether to use ControlMaster for connection reuse
            
        Returns:
            List of command arguments
        """
        cmd = ["ssh"]
        
        # Add ControlMaster options for connection reuse
        if use_control_master:
            control_path = self._get_control_path(username, server)
            cmd.extend([
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={control_path}",
                "-o", "ControlPersist=300",  # Keep connection alive for 5 minutes
                "-o", "StrictHostKeyChecking=accept-new",
            ])
        
        # Add connection timeout
        cmd.extend([
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=60",
            "-o", "ServerAliveCountMax=3",
        ])
        
        # Add target
        cmd.append(f"{username}@{server}")
        
        # Add command if provided
        if command:
            cmd.extend(["bash", "-lc", command])
        
        return cmd
    
    def execute_remote(self, username: str, server: str, command: str,
                      timeout: Optional[int] = None,
                      check: bool = True,
                      capture_output: bool = True) -> subprocess.CompletedProcess:
        """Execute remote command via SSH.
        
        Args:
            username: SSH username
            server: Server hostname or IP
            command: Remote command to execute
            timeout: Command timeout in seconds
            check: Whether to raise exception on non-zero exit code
            capture_output: Whether to capture stdout/stderr
            
        Returns:
            CompletedProcess result
        """
        # Ensure host key is added
        self.ensure_host_key(server)
        
        # Build SSH command
        ssh_cmd = self.build_ssh_command(username, server, command)
        
        # Execute
        return subprocess.run(
            ssh_cmd,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            check=check
        )
    
    def execute_remote_script(self, username: str, server: str, script: str,
                              script_name: str = "script.sh",
                              timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        """Execute remote script via SSH stdin.
        
        Args:
            username: SSH username
            server: Server hostname or IP
            script: Script content to execute
            script_name: Name for temporary script file
            timeout: Command timeout in seconds
            
        Returns:
            CompletedProcess result
        """
        # Ensure host key is added
        self.ensure_host_key(server)
        
        # Build command to write script and execute
        exec_cmd = f"cat > /tmp/{script_name} && chmod +x /tmp/{script_name} && /tmp/{script_name} && rm -f /tmp/{script_name}"
        
        # Build SSH command
        ssh_cmd = self.build_ssh_command(username, server, exec_cmd)
        
        # Execute with script as stdin
        return subprocess.run(
            ssh_cmd,
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False
        )
    
    def build_scp_command(self, source: str, target: str,
                         use_control_master: bool = True) -> List[str]:
        """Build SCP command with connection reuse.
        
        Args:
            source: Source path (local or remote)
            target: Target path (local or remote)
            use_control_master: Whether to use ControlMaster
            
        Returns:
            List of command arguments
        """
        cmd = ["scp", "-C"]  # Enable compression
        
        # Add ControlMaster options if source or target is remote
        if use_control_master and ("@" in source or "@" in target):
            # Extract server from source or target
            server = None
            username = None
            if "@" in source:
                username, server = source.split("@", 1)[0], source.split("@", 1)[1].split(":")[0]
            elif "@" in target:
                username, server = target.split("@", 1)[0], target.split("@", 1)[1].split(":")[0]
            
            if server and username:
                control_path = self._get_control_path(username, server)
                cmd.extend([
                    "-o", "ControlMaster=auto",
                    "-o", f"ControlPath={control_path}",
                    "-o", "ControlPersist=300",
                    "-o", "StrictHostKeyChecking=accept-new",
                ])
        
        cmd.extend([
            "-o", "ConnectTimeout=10",
            source,
            target
        ])
        
        return cmd
    
    def cleanup(self):
        """Clean up temporary files and connections."""
        if self._temp_dir and self._temp_dir.exists():
            import shutil
            try:
                shutil.rmtree(self._temp_dir)
            except Exception:
                pass
            self._temp_dir = None
        self._control_paths.clear()


class SCPTarget:
    """Represents an SCP target with parsing and validation."""
    
    def __init__(self, scp_target: str):
        """Parse SCP target.
        
        Args:
            scp_target: SCP target in format username@server:path
            
        Raises:
            ValueError: If format is invalid
        """
        if not self._validate_format(scp_target):
            raise ValueError(f"Invalid SCP target format: {scp_target}. Expected: username@server:path")
        
        parts = scp_target.split('@', 1)
        self.username = parts[0]
        
        server_path = parts[1].split(':', 1)
        self.server = server_path[0]
        self.remote_path = server_path[1] if len(server_path) > 1 else '.'
        self._original = scp_target
    
    @staticmethod
    def _validate_format(scp_target: str) -> bool:
        """Validate SCP target format."""
        import re
        pattern = r'^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+:.+$'
        return bool(re.match(pattern, scp_target))
    
    def __str__(self) -> str:
        """Return original SCP target string."""
        return self._original
    
    def with_server(self, new_server: str) -> 'SCPTarget':
        """Create new SCPTarget with different server."""
        new_target = f"{self.username}@{new_server}:{self.remote_path}"
        return SCPTarget(new_target)
    
    def get_remote_file_path(self, filename: str) -> str:
        """Get full remote file path."""
        if self.remote_path.endswith('/'):
            return f"{self.remote_path}{filename}"
        return f"{self.remote_path}/{filename}"

