"""
SSH utilities for ilha CLI.

This module provides utilities for managing SSH connections, including
automatic host key management.
"""

import subprocess
import os
from pathlib import Path

from .logging import log_info, log_warning


def add_ssh_host_key(host: str, port: int = 22) -> bool:
    """Add SSH host key to known_hosts file.
    
    Uses ssh-keyscan to fetch the host key and appends it to ~/.ssh/known_hosts.
    If the host key already exists, this is a no-op.
    
    Args:
        host: Hostname or IP address
        port: SSH port (default: 22)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Get home directory
        home_dir = Path.home()
        ssh_dir = home_dir / '.ssh'
        known_hosts = ssh_dir / 'known_hosts'
        
        # Create .ssh directory if it doesn't exist
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        
        # Check if host key already exists using ssh-keygen -F (exact match)
        if known_hosts.exists():
            try:
                lookup = subprocess.run(
                    ['ssh-keygen', '-F', host, '-f', str(known_hosts)],
                    capture_output=True, text=True, timeout=5, check=False
                )
                if lookup.returncode == 0 and lookup.stdout.strip():
                    log_info(f"SSH host key for {host} already in known_hosts")
                    return True
            except Exception:
                # If ssh-keygen lookup fails, continue to add the key
                pass
        
        # Use ssh-keyscan to fetch host key
        log_info(f"Adding SSH host key for {host}:{port}...")
        cmd = ['ssh-keyscan', '-p', str(port), '-H', host]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False
        )
        
        if result.returncode != 0:
            log_warning(f"Failed to fetch SSH host key for {host}: {result.stderr}")
            return False
        
        host_key = result.stdout.strip()
        if not host_key:
            log_warning(f"No host key returned for {host}")
            return False
        
        # Append to known_hosts
        try:
            with open(known_hosts, 'a') as f:
                f.write(host_key + '\n')
            
            # Set appropriate permissions
            known_hosts.chmod(0o600)
            
            log_info(f"Successfully added SSH host key for {host}")
            return True
        except Exception as e:
            log_warning(f"Failed to write to known_hosts: {e}")
            return False
            
    except Exception as e:
        log_warning(f"Error adding SSH host key for {host}: {e}")
        return False
