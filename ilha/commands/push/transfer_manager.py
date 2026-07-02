"""
File transfer management for ilha push operations.

This module handles transferring packages to remote servers using
rsync (preferred) or SCP (fallback).
"""

import subprocess
import shlex
from pathlib import Path
from typing import Optional

from ...utils.ssh_manager import SSHConnectionManager, SCPTarget
from ...utils.ssh_utils import add_ssh_host_key
from ...utils.logging import log_info, log_success, log_error, log_warning


class TransferManager:
    """Manages file transfers to remote servers."""
    
    def __init__(self):
        """Initialize transfer manager."""
        self.ssh = SSHConnectionManager()
    
    def transfer_package(self, package_path: Path, scp_target: SCPTarget) -> bool:
        """Transfer package to remote server via rsync (faster) or SCP (fallback).
        
        Uses rsync with compression for faster transfers, falls back to SCP if rsync unavailable.
        
        Args:
            package_path: Path to package file
            scp_target: SCP target object
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Add SSH host key before transfer
            log_info(f"Adding SSH host key for {scp_target.server}...")
            add_ssh_host_key(scp_target.server)
            
            remote_file_path = scp_target.get_remote_file_path(package_path.name)
            log_info(f"Remote file path: {remote_file_path}")
            
            # Try rsync first (faster with compression and progress)
            if subprocess.run(["which", "rsync"], capture_output=True).returncode == 0:
                log_info("Using rsync for faster transfer with compression...")
                package_size_mb = package_path.stat().st_size / 1024 / 1024
                log_info(f"Transferring {package_size_mb:.2f} MB package (this may take a while)...")
                
                # rsync with compression, progress, and partial transfer support.
                # Route the ssh transport through the connection manager so rsync
                # reuses the ControlPath master connection and honours the same
                # host-key policy as every other SSH call (rather than hand-rolling
                # a separate accept-new ssh string).
                rsync_ssh = self.ssh.get_rsync_ssh_command(scp_target.username, scp_target.server)
                cmd = [
                    "rsync",
                    "-avz",  # archive, verbose, compress
                    "--progress",  # show progress
                    "--partial",  # keep partial transfers
                    "--inplace",  # update in-place for faster completion
                    "-e", rsync_ssh,
                    str(package_path),
                    remote_file_path
                ]
                log_info(f"Executing rsync command: {' '.join(cmd)}")
                
                # Use Popen for real-time progress output
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                
                # Stream output for progress
                for line in iter(process.stdout.readline, ''):
                    if line:
                        line = line.rstrip()
                        # Show progress lines
                        if '%' in line or 'speedup' in line.lower():
                            log_info(f"  {line}")
                
                process.wait()
                
                if process.returncode != 0:
                    log_error(f"rsync transfer failed with exit code {process.returncode}")
                    log_info("Falling back to SCP...")
                    # Fall through to SCP fallback
                else:
                    log_success(f"Package transferred successfully via rsync")
                    log_info(f"Transfer completed: {package_path.name} -> {remote_file_path}")
                    return True
            
            # Fallback to SCP with compression
            log_info("Using SCP with compression for transfer...")
            package_size_mb = package_path.stat().st_size / 1024 / 1024
            log_info(f"Transferring {package_size_mb:.2f} MB package (this may take a while)...")
            
            # Build SCP command manually with proper format
            scp_cmd = [
                "scp",
                "-C",  # Enable compression
                "-o", "StrictHostKeyChecking=accept-new",  # Auto-accept new host keys
                "-o", "ConnectTimeout=10",
                str(package_path),
                f"{scp_target.username}@{scp_target.server}:{remote_file_path}"
            ]
            
            log_info(f"Executing SCP command: {' '.join(scp_cmd)}")
            log_info("Transfer in progress (this may take a while for large packages)...")
            
            result = subprocess.run(
                scp_cmd,
                capture_output=True,
                text=True,
                check=False
            )
            
            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown SCP error"
                log_error(f"SCP transfer failed: {error_msg}")
                if result.stdout:
                    log_info(f"SCP stdout: {result.stdout}")
                
                # Provide helpful error messages for common issues
                if "Connection refused" in error_msg or "Could not resolve" in error_msg:
                    log_info("Tip: Check that the server is accessible and SSH is running")
                elif "Permission denied" in error_msg or "Authentication failed" in error_msg:
                    log_info("Tip: Ensure SSH keys are set up or password authentication is enabled")
                elif "No space left" in error_msg or "disk full" in error_msg.lower():
                    log_info("Tip: Free up disk space on the remote server")
                
                return False
            
            log_success(f"Package transferred successfully")
            log_info(f"Transfer completed: {package_path.name} -> {remote_file_path}")
            return True
            
        except FileNotFoundError as e:
            cmd_name = "rsync" if "rsync" in str(e) else "scp"
            log_error(f"{cmd_name} command not found. Please ensure OpenSSH (and optionally rsync) is installed.")
            return False
        except Exception as e:
            log_error(f"Unexpected error during transfer: {e}")
            return False
    
    def ensure_remote_dir(self, target: SCPTarget) -> bool:
        """Create the remote directory with mkdir -p (best effort).
        
        Args:
            target: SCP target object
            
        Returns:
            True if directory exists or was created, False on error
        """
        try:
            # Add SSH host key before connection
            add_ssh_host_key(target.server)
            
            remote_dir = self._infer_remote_directory(target.remote_path)
            if remote_dir and remote_dir not in ['.', '']:
                log_info(f"Ensuring remote directory exists: {remote_dir}")
                
                cmd = self.ssh.build_ssh_command(
                    target.username,
                    target.server,
                    f"mkdir -p {shlex.quote(remote_dir)}",
                    use_control_master=True
                )
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10
                )
                
                if result.returncode == 0:
                    return True
                else:
                    log_warning(f"Failed to create remote directory: {result.stderr}")
                    return False
            else:
                log_info("Remote directory is current directory, skipping directory creation")
                return True
        except Exception as e:
            log_warning(f"Failed to ensure remote directory: {e}")
            return False
    
    def _infer_remote_directory(self, remote_path: str) -> str:
        """Infer the directory to create on the remote side for the given target path.

        If the target looks like a directory (no archive suffix and no filename),
        return it directly; otherwise return its parent directory.
        
        Args:
            remote_path: Remote path string
            
        Returns:
            Directory path to create
        """
        try:
            # Treat trailing slash as a directory
            if remote_path.endswith('/'):
                return remote_path.rstrip('/')
            # Heuristic: common archive/file suffixes
            lower = remote_path.lower()
            archive_suffixes = ('.tar.gz', '.tgz', '.tar', '.zip')
            if any(lower.endswith(suf) for suf in archive_suffixes):
                return str(Path(remote_path).parent)
            # If there is a dot in the last path segment, assume it's a file
            last = Path(remote_path).name
            if '.' in last:
                return str(Path(remote_path).parent)
            # Otherwise assume it's a directory
            return remote_path
        except Exception:
            return str(Path(remote_path).parent)
    
    def find_existing_package(self, target: SCPTarget, branch_name: str) -> Optional[str]:
        """Find an existing ilha package file on the remote server for the given branch.
        
        Args:
            target: SCP target object
            branch_name: Branch name to match
            
        Returns:
            Full path to package file if found, None otherwise
        """
        try:
            # Add SSH host key before connection
            add_ssh_host_key(target.server)
            
            # Search for ilha package files matching the branch name
            # Escape branch_name and remote_path to prevent shell injection
            escaped_path = shlex.quote(target.remote_path)
            escaped_branch = shlex.quote(branch_name)
            cmd = self.ssh.build_ssh_command(
                target.username,
                target.server,
                f'find {escaped_path} -maxdepth 1 -type f -name "*{escaped_branch}*.ilha-package.tar.gz" 2>/dev/null | head -1',
                use_control_master=True
            )
            
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                output = result.stdout.strip()
                if output:
                    return output
            return None
        except Exception as e:
            log_warning(f"Failed to search for existing package: {e}")
            return None
    
    def check_remote_file_exists(self, target: SCPTarget, remote_file_path: str) -> bool:
        """Check if a file exists on the remote server.
        
        Args:
            target: SCP target object
            remote_file_path: Full path to file on remote server
            
        Returns:
            True if file exists, False otherwise
        """
        try:
            # Add SSH host key before connection
            add_ssh_host_key(target.server)
            
            # build_ssh_command already wraps the command in `bash -lc <quoted>`;
            # a second nested bash -lc with an unquoted path was a shell-injection
            # vector. Quote the path and let the single outer shell handle it.
            quoted_path = shlex.quote(remote_file_path)
            cmd = self.ssh.build_ssh_command(
                target.username,
                target.server,
                f"test -f {quoted_path} && echo EXISTS || echo NOT_FOUND",
                use_control_master=True
            )
            
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                output = result.stdout.strip()
                return output == "EXISTS"
            return False
        except Exception as e:
            log_warning(f"Failed to check remote file existence: {e}")
            return False
    
    def cleanup(self):
        """Clean up SSH connections."""
        self.ssh.cleanup()

