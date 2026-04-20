"""
SSH connection manager for ilha CLI.

This module provides SSH connection management with connection reuse,
host key caching, and unified remote command execution.
"""

import shlex
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
        self._expected_fingerprint: Optional[str] = None

    def set_expected_fingerprint(self, fingerprint: Optional[str]) -> None:
        """Pin the SHA256 host-key fingerprint used by ``ensure_host_key``.

        When set, subsequent first-time host-key adds verify the scanned key
        against this fingerprint and refuse to trust it on mismatch. Pass
        ``None`` to clear.
        """
        self._expected_fingerprint = fingerprint

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
    
    def _get_control_path(self, username: str, server: str, port: int = 22) -> str:
        """Get control path for SSH connection reuse.

        The filename includes the port so simultaneous connections to the same
        host on different ports (e.g. one droplet behind a jumphost on :2222)
        don't collide on the same Unix socket.
        """
        key = f"{username}@{server}:{port}"
        if key not in self._control_paths:
            temp_dir = self._get_temp_dir()
            safe_name = f"{server.replace('.', '_').replace(':', '_')}-{port}"
            control_path = temp_dir / f"control-{safe_name}"
            self._control_paths[key] = str(control_path)
        return self._control_paths[key]
    
    def ensure_host_key(
        self,
        server: str,
        port: int = 22,
        expected_fingerprint: Optional[str] = None,
    ) -> bool:
        """Ensure SSH host key is added (cached check).

        Args:
            server: Server hostname or IP
            port: SSH port
            expected_fingerprint: Optional SHA256:… fingerprint to pin. When
                supplied and the scanned key does not match, the connection is
                rejected (MITM defense). When omitted, TOFU applies.

        Returns:
            True if host key is available, False otherwise
        """
        fp = expected_fingerprint if expected_fingerprint is not None else self._expected_fingerprint
        key = f"{server}:{port}"
        if key not in self._host_keys_added:
            if add_ssh_host_key(server, port, expected_fingerprint=fp):
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
            "-o", "ConnectTimeout=30",
            "-o", "ServerAliveInterval=60",
            "-o", "ServerAliveCountMax=3",
        ])

        # Add target
        cmd.append(f"{username}@{server}")
        
        # Add command if provided. Pass as a single string so the server runs
        # "bash -lc 'full command'" and the -c script is the entire command.
        # Passing ["bash", "-lc", command] causes ssh to send separate args, so
        # the server only gets "ilha" as the -c script and the rest is lost.
        if command:
            cmd.append("bash -lc " + shlex.quote(command))
        
        return cmd

    def get_rsync_ssh_command(self, username: str, server: str) -> str:
        """Return the ssh command string for rsync -e, reusing the same ControlPath as build_ssh_command.
        Call after mkdir (or any SSH) so rsync reuses that connection instead of opening a new one.
        """
        control_path = self._get_control_path(username, server)
        parts = [
            "ssh",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={control_path}",
            "-o", "ControlPersist=300",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=30",
        ]
        return " ".join(shlex.quote(p) for p in parts)

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
        
        # Sanitize script_name: strip path separators and shell metacharacters
        import re
        safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', script_name)
        if not safe_name:
            safe_name = "script.sh"

        # Build command to write script and execute. Always clean up the /tmp
        # file regardless of script exit status — the `&&` chain previously
        # skipped the `rm` when the script exited non-zero, which accumulated
        # stale scripts under /tmp on every failed remote exec.
        exec_cmd = (
            f"cat > /tmp/{safe_name} && chmod +x /tmp/{safe_name}; "
            f"/tmp/{safe_name}; rc=$?; rm -f /tmp/{safe_name}; exit $rc"
        )
        
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
            "-o", "ConnectTimeout=30",
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

