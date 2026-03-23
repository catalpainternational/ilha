"""
Global Caddy management commands for ilha CLI.

This module provides commands for starting and stopping the global Caddy container.
"""

import subprocess
import tempfile
from pathlib import Path

from ..config.settings import get_project_root, get_script_dir, CADDY_NETWORK
from ..core.docker_manager import DockerManager
from ..utils.path_utils import get_env_compose_file_path
from ..utils.logging import log_info, log_success, log_warning, log_error
from ..utils.validation import validate_container_running, validate_container_exists, validate_volume_exists


class CaddyManager:
    """Manages global Caddy container operations."""
    
    def __init__(self):
        """Initialize Caddy manager."""
        self.docker_manager = DockerManager()
        self.project_root = get_project_root()
        
        # Always use package templates
        script_dir = get_script_dir()
        self.compose_file = script_dir / "config" / "docker-compose.global-caddy.yml"
        self.caddyfile = script_dir / "config" / "Caddyfile.ilha"
    
    def _get_compose_content_with_paths(self) -> str:
        """Get compose file content with runtime path substitutions."""
        template_content = self.compose_file.read_text()
        
        # Substitute paths
        caddyfile_path = str(self.caddyfile)
        script_dir = get_script_dir()
        monitor_script_path = str(script_dir / "scripts" / "caddy-docker-monitor.py")
        
        return template_content.replace(
            '{CADDYFILE_PATH}', caddyfile_path
        ).replace(
            '{MONITOR_SCRIPT_PATH}', monitor_script_path
        )
    
    def start_global_caddy(self) -> bool:
        """Start global Caddy container."""
        log_info("Starting global Caddy container")
        
        # Create network if it doesn't exist
        if not self.docker_manager.create_network():
            return False
        
        # Ensure required volumes exist before starting compose
        if not self._ensure_caddy_volumes():
            log_warning("Failed to create Caddy volumes, but continuing...")
        
        # Handle existing Caddy container
        if not self._handle_existing_container("ilha_caddy_proxy"):
            return False
        
        # Check if container is now running after handling existing container
        if self.is_caddy_running():
            # Verify network attachment even if container is running
            if not self._is_container_on_network("ilha_caddy_proxy"):
                log_warning("Caddy container is running but not on network, reconnecting...")
                if not self._ensure_network_attachment("ilha_caddy_proxy"):
                    log_error("Failed to reconnect Caddy to network")
                    return False
            # Ensure caddy_monitor is also running so routes stay up to date
            if not validate_container_running("caddy_monitor"):
                log_info("Caddy is running but caddy_monitor is not; starting full proxy stack")
            else:
                log_success("Global Caddy container is already running and on network")
                return True
        
        # Create temporary compose file with substituted paths
        compose_content = self._get_compose_content_with_paths()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write(compose_content)
            temp_compose = Path(f.name)
        
        # Try to find env.ilha file to pass USE_STAGING_CERTIFICATES
        env_file = get_env_compose_file_path(self.project_root)
        if not env_file.exists():
            env_file = None
        
        try:
            success = self.docker_manager.start_services(
                temp_compose, 
                env_file=env_file,
                project_name="ilha-proxy"
            )
        finally:
            temp_compose.unlink()  # Clean up temp file
        if success:
            # Verify and ensure network attachment
            if not self._ensure_network_attachment("ilha_caddy_proxy"):
                log_warning("Caddy container started but network attachment verification failed")
            else:
                log_success("Global Caddy container started and verified on network")
        else:
            log_error("Failed to start global Caddy container")
        
        return success
    
    def stop_global_caddy(self) -> bool:
        """Stop global Caddy container."""
        log_info("Stopping global Caddy container")
        
        # Create temporary compose file with substituted paths
        compose_content = self._get_compose_content_with_paths()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write(compose_content)
            temp_compose = Path(f.name)
        
        try:
            success = self.docker_manager.stop_services(
                temp_compose, 
                project_name="ilha-proxy"
            )
        finally:
            temp_compose.unlink()  # Clean up temp file
        if success:
            log_success("Global Caddy container stopped")
        else:
            log_warning("Failed to stop Caddy container (may not be running)")
        
        return success
    
    def is_caddy_running(self) -> bool:
        """Check if global Caddy container is running."""
        return validate_container_running("ilha_caddy_proxy")
    
    def get_caddy_status(self) -> dict:
        """Get status information about the global Caddy container."""
        is_on_network = False
        if validate_container_exists("ilha_caddy_proxy"):
            is_on_network = self._is_container_on_network("ilha_caddy_proxy")
        
        return {
            "running": self.is_caddy_running(),
            "on_network": is_on_network,
            "compose_file_exists": self.compose_file.exists(),
            "caddyfile_exists": self.caddyfile.exists(),
            "network_exists": self.docker_manager.create_network()
        }
    
    def _ensure_caddy_volumes(self) -> bool:
        """Ensure Caddy volumes exist before starting compose."""
        volumes = ["ilha_caddy_data", "ilha_caddy_config"]
        success = True
        
        for volume_name in volumes:
            if not validate_volume_exists(volume_name):
                log_info(f"Creating volume: {volume_name}")
                try:
                    subprocess.run(
                        ["docker", "volume", "create", volume_name],
                        check=True,
                        capture_output=True
                    )
                except subprocess.CalledProcessError as e:
                    log_error(f"Failed to create volume {volume_name}: {e}")
                    success = False
        
        return success
    
    def _handle_existing_container(self, container_name: str) -> bool:
        """Handle existing Caddy container (stopped or bad state)."""
        # If container is already running, we're good
        if validate_container_running(container_name):
            log_info(f"Container {container_name} is already running")
            return True
        
        # Check if container exists but is stopped
        if validate_container_exists(container_name):
            log_info(f"Found existing {container_name} container, attempting to restart...")
            try:
                # Try to start the existing container
                result = subprocess.run(
                    ["docker", "start", container_name],
                    capture_output=True,
                    text=True,
                    check=True
                )
                log_success(f"Successfully restarted {container_name}")
                return True
            except subprocess.CalledProcessError as e:
                log_warning(f"Failed to restart {container_name}, removing and will recreate")
                # If we can't start it, remove it and let compose recreate
                try:
                    subprocess.run(
                        ["docker", "rm", "-f", container_name],
                        capture_output=True,
                        check=True
                    )
                    log_info(f"Removed old {container_name} container")
                    # Also try to remove the monitor container
                    try:
                        subprocess.run(
                            ["docker", "rm", "-f", "caddy_monitor"],
                            capture_output=True,
                            check=False  # Don't fail if monitor doesn't exist
                        )
                    except:
                        pass
                except subprocess.CalledProcessError as remove_error:
                    log_error(f"Failed to remove {container_name}: {remove_error}")
                    return False
        
        # Container doesn't exist or was successfully removed
        return True
    
    def _is_container_on_network(self, container_name: str, network_name: str = CADDY_NETWORK) -> bool:
        """Check if container is connected to the specified network."""
        try:
            result = subprocess.run(
                ["docker", "inspect", container_name, "--format", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}"],
                capture_output=True,
                text=True,
                check=True
            )
            networks = result.stdout.strip().split()
            return network_name in networks
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
    
    def _ensure_network_attachment(self, container_name: str, network_name: str = CADDY_NETWORK) -> bool:
        """Ensure container is attached to the network, reconnect if needed."""
        if not validate_container_exists(container_name):
            return False
        
        if self._is_container_on_network(container_name, network_name):
            log_info(f"Container {container_name} is already on network {network_name}")
            return True
        
        # Container exists but not on network - connect it
        log_warning(f"Container {container_name} is not on network {network_name}, connecting...")
        try:
            # Stop container first to avoid port conflicts
            if validate_container_running(container_name):
                log_info(f"Stopping {container_name} to reconnect network...")
                subprocess.run(
                    ["docker", "stop", container_name],
                    capture_output=True,
                    check=True,
                    timeout=30
                )
            
            # Connect to network
            result = subprocess.run(
                ["docker", "network", "connect", network_name, container_name],
                capture_output=True,
                text=True,
                check=True
            )
            
            # Restart container if it was running
            if validate_container_exists(container_name):
                log_info(f"Restarting {container_name}...")
                subprocess.run(
                    ["docker", "start", container_name],
                    capture_output=True,
                    check=True,
                    timeout=30
                )
            
            # Verify connection
            if self._is_container_on_network(container_name, network_name):
                log_success(f"Successfully connected {container_name} to network {network_name}")
                return True
            else:
                log_error(f"Failed to verify network connection for {container_name}")
                return False
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to connect {container_name} to network {network_name}: {e.stderr if hasattr(e, 'stderr') else str(e)}")
            return False
        except subprocess.TimeoutExpired:
            log_error(f"Timeout connecting {container_name} to network {network_name}")
            return False
