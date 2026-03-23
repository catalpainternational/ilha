"""
Server preparation for ilha push operations.

This module handles preparing remote servers by installing dependencies
before ilha can be used.
"""

from pathlib import Path
from typing import Optional

from ...config.settings import get_script_dir
from ...utils.logging import log_info, log_success, log_error, log_warning, is_verbose
from ...utils.ssh_utils import add_ssh_host_key
from ...utils.streaming import execute_with_streaming


class ServerPreparer:
    """Prepares remote servers by installing dependencies."""
    
    def __init__(self):
        """Initialize server preparer."""
        self.script_path = self._get_prep_script_path()
    
    def _get_prep_script_path(self) -> Path:
        """Get path to server preparation script.
        
        Returns:
            Path to server_prep.sh script
        """
        script_dir = get_script_dir()
        return script_dir / "scripts" / "server_prep.sh"
    
    def _load_prep_script(self) -> str:
        """Load server preparation script from file.
        
        Returns:
            Script content as string
        """
        if not self.script_path.exists():
            raise FileNotFoundError(f"Server prep script not found: {self.script_path}")
        
        return self.script_path.read_text()
    
    def prepare_server(self, username: str, server: str) -> bool:
        """Prepare remote server by installing dependencies.
        
        Installs: curl, git, Python 3.11+, Docker (Engine + Compose v2),
        and ilha from GitHub via pip.
        
        Args:
            username: SSH username
            server: Server hostname or IP
            
        Returns:
            True if successful, False otherwise
        """
        try:
            log_info(f"Preparing server: {username}@{server}")
            log_info("This will install: curl, git, Python 3.11+, Docker, and ilha")
            
            # Get local ilha version to install same version on server
            try:
                from ...config.settings import VERSION
                ilha_version = VERSION
                log_info(f"Will install ilha version {ilha_version} on server to match local client")
            except (ImportError, AttributeError):
                # Fallback to master if version not available
                ilha_version = "master"
                log_warning("Could not determine local ilha version, will install latest from master")
            
            # Add SSH host key before connection
            log_info("Adding SSH host key...")
            add_ssh_host_key(server)
            
            # Load script
            script = self._load_prep_script()
            
            # Send script via SSH stdin and execute under bash -lc
            # Pass ILHA_VERSION environment variable to the script
            exec_cmd = f"export ILHA_VERSION={ilha_version} && cat > /tmp/dtprep.sh && chmod +x /tmp/dtprep.sh && /tmp/dtprep.sh && rm -f /tmp/dtprep.sh"
            ssh_cmd = ["ssh", f"{username}@{server}", "bash", "-lc", exec_cmd]
            log_info("Executing server preparation script via SSH...")
            log_info("This may take 5-10 minutes depending on server state and network speed...")
            
            # Use streaming utility for consistent output handling
            success, stdout_lines, stderr_lines = execute_with_streaming(
                ssh_cmd,
                script=script,
                timeout=600,  # 10 minute timeout for server prep
                progress_interval=30,
                prefix="  ",
                filter_keywords=None  # Show all output in verbose mode, filtered in non-verbose
            )
            
            if not success:
                log_error("Remote preparation failed")
                if stderr_lines:
                    log_error("Server preparation errors:")
                    for line in stderr_lines:
                        log_error(f"  {line}")
                return False
            
            # Show output summary if not in verbose mode (verbose mode already showed it)
            if not is_verbose() and stdout_lines:
                log_info("Server preparation output:")
                for line in stdout_lines:
                    log_info(f"  {line}")
            
            log_info("Server preparation script completed successfully")
            return True
        except Exception as e:
            log_error(f"Error preparing server: {e}")
            return False
    
    def verify_ilha_installed(self, username: str, server: str) -> bool:
        """Verify that ilha is installed on the remote server.
        
        Args:
            username: SSH username
            server: Server hostname or IP
            
        Returns:
            True if ilha is found, False otherwise
        """
        try:
            # Check for ilha in common locations
            cmd = [
                "ssh", f"{username}@{server}",
                "bash -lc 'which ilha || [ -x /opt/ilha-venv/bin/ilha ] || echo NOT_FOUND'"
            ]
            import subprocess
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=10
            )
            
            if result.returncode == 0:
                output = result.stdout.strip()
                if output and output != "NOT_FOUND":
                    log_info("Verified ilha is installed on server")
                    return True
                else:
                    log_error("ilha not found in PATH or /opt/ilha-venv/bin/")
                    return False
            else:
                log_warning(f"Failed to verify ilha installation: {result.stderr}")
                return False
        except Exception as e:
            log_warning(f"Error verifying ilha installation: {e}")
            return False

