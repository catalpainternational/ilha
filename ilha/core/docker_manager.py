"""
Docker management for ilha CLI.

This module provides Docker operations including network creation, volume management,
container lifecycle, and compose file execution.
"""

import subprocess
import shutil
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config.settings import (
    CADDY_NETWORK,
    get_compose_command,
    get_volume_names,
    get_volume_names_for_project_root,
    get_source_volume_names,
    get_source_volume_names_for_project_root,
    DEFAULT_ENV_VARS,
    get_project_root,
    get_project_name,
    sanitize_project_name,
    POSTGRES_VOLUME_SUFFIX,
)
from ..exceptions import PrerequisiteError
from ..utils.logging import log_info, log_success, log_warning, log_error, show_progress
from ..utils.file_utils import ensure_entrypoint_executable
from ..utils.validation import (
    validate_docker_running, validate_network_exists, validate_volume_exists,
    get_containers_using_volume, are_containers_running, get_postgres_container_for_volume
)
from ..core.git_manager import GitManager


class DockerManager:
    """Manages Docker operations for ilha CLI."""
    
    def __init__(self, project_root: Optional[Path] = None, validate: bool = True):
        """Initialize Docker manager.
        
        Args:
            project_root: Project root directory. If None, uses get_project_root().
            validate: If True, raise exception if Docker not running. If False, just log warning.
        """
        # Use the provided project_root directly, don't fall back to get_project_root()
        if project_root is None:
            self.project_root = get_project_root()
        else:
            self.project_root = Path(project_root).resolve()
        self.compose_cmd = get_compose_command()
        if validate:
            self._validate_docker()
        else:
            # Just check without raising
            if not validate_docker_running():
                log_warning("Docker is not running. Some operations may fail.")
    
    def _validate_docker(self) -> None:
        """Validate Docker is running."""
        if not validate_docker_running():
            raise PrerequisiteError(
                message="Docker is not running. Please start Docker and try again.",
                error_code="docker_not_running"
            )
    
    def create_network(self, network_name: str = CADDY_NETWORK) -> bool:
        """Create external network if it doesn't exist."""
        if validate_network_exists(network_name):
            log_info(f"Network {network_name} already exists")
            return True
        
        log_info(f"Creating external network: {network_name}")
        try:
            subprocess.run(
                ["docker", "network", "create", network_name],
                check=True,
                capture_output=True
            )
            log_success(f"Network {network_name} created")
            return True
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to create network {network_name}: {e}")
            return False
        except Exception as e:
            log_error(f"Failed to create network {network_name}: {e}")
            return False

    def remove_project_networks(self, compose_project_name: str) -> None:
        """Remove Docker networks for a compose project (prefix match).

        Used after down or on pool-overlap to avoid stale networks causing
        'Pool overlaps with other one on this address space' on next up.

        Args:
            compose_project_name: Project name (e.g. business-intelligence-ainaro).
        """
        prefix = f"{compose_project_name}_"
        try:
            result = subprocess.run(
                ["docker", "network", "ls", "--format", "{{.Name}}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            names = [n.strip() for n in (result.stdout or "").splitlines() if n.strip()]
            for name in names:
                if name.startswith(prefix):
                    try:
                        subprocess.run(
                            ["docker", "network", "rm", name],
                            capture_output=True,
                            text=True,
                            check=True,
                            timeout=30,
                        )
                        log_info(f"Removed network {name}")
                    except subprocess.CalledProcessError as e:
                        if "has active endpoints" in (e.stderr or ""):
                            log_info(f"Network {name} in use, skipping")
                        else:
                            log_warning(f"Could not remove network {name}: {e.stderr or e}")
                    except Exception as e:
                        log_warning(f"Could not remove network {name}: {e}")
        except Exception as e:
            log_warning(f"Could not list networks for cleanup: {e}")

    def copy_volume(self, source_volume: str, target_volume: str, source_project_name: str = None) -> bool:
        """Copy volume data from source to target using file-level copy.
        
        This method works for all volume types (PostgreSQL, Redis, media, etc.).
        The caller is responsible for ensuring containers are stopped before calling
        this method to ensure data consistency.
        
        Args:
            source_volume: Source volume name
            target_volume: Target volume name
            source_project_name: Project name (unused, kept for backward compatibility)
            
        Returns:
            True if copy succeeded, False otherwise
        """
        log_info(f"Copying volume {source_volume} to {target_volume}...")
        
        # Check if source volume exists
        if not validate_volume_exists(source_volume):
            log_warning(f"Source volume {source_volume} does not exist, creating empty target volume")
            return self._create_volume(target_volume)
        
        # Create target volume if it doesn't exist
        if not self._create_volume(target_volume):
            return False
        
        # Use generic file copy for all volume types
        return self._copy_volume_files(source_volume, target_volume)
    
    def _get_postgres_container_name(self, branch_name: str) -> Optional[str]:
        """Get PostgreSQL container name for a branch.
        
        Args:
            branch_name: Branch name for the worktree
            
        Returns:
            Container name in format: {project_name}-{branch_name}-db, or None if not found
        """
        from ..config.settings import get_project_name, sanitize_project_name
        project_name = sanitize_project_name(get_project_name())
        container_name = f"{project_name}-{branch_name}-db"
        
        # Verify container exists
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.stdout.strip() == container_name:
            return container_name
        return None
    
    def _ensure_containers_stopped_for_volume_operation(
        self, volume_name: str, project_name: str, operation_type: str = "operation"
    ) -> Optional[str]:
        """Stop containers using a volume before volume operations.
        
        This is a shared method used by both volume copying and archiving operations.
        It finds the container using the volume and stops it if running.
        
        Args:
            volume_name: Volume name to find containers for
            project_name: Project name for finding the container
            operation_type: Description of operation (for logging)
            
        Returns:
            Container name if stopped, None if not found or already stopped
        """
        # Find the PostgreSQL container using this volume
        postgres_container = get_postgres_container_for_volume(volume_name, project_name)
        
        if not postgres_container:
            log_info(f"No container found using volume {volume_name}")
            return None
        
        # Check if container is running
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={postgres_container}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False
        )
        is_running = result.stdout.strip() == postgres_container
        
        if not is_running:
            log_info(f"Container {postgres_container} is already stopped")
            return postgres_container
        
        log_info(f"Stopping container {postgres_container} before {operation_type}...")
        
        # Stop using docker stop (works for any container)
        result = subprocess.run(
            ["docker", "stop", postgres_container],
            capture_output=True,
            text=True,
            check=False,
            timeout=30
        )
        
        if result.returncode == 0:
            log_success(f"Container {postgres_container} stopped successfully")
            return postgres_container
        else:
            log_warning(f"Failed to stop container {postgres_container}: {result.stderr}")
            return None
    
    def _restart_container(self, container_name: str) -> bool:
        """Restart a container by name.
        
        This is a shared method used by both volume copying and archiving operations.
        
        Args:
            container_name: Name of the container to restart
            
        Returns:
            True if started successfully, False otherwise
        """
        log_info(f"Starting container {container_name}...")
        
        result = subprocess.run(
            ["docker", "start", container_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=30
        )
        
        if result.returncode == 0:
            log_success(f"Container {container_name} started successfully")
            return True
        else:
            log_warning(f"Failed to start container {container_name}: {result.stderr}")
            return False
    
    def _copy_volume_files(self, source_volume: str, target_volume: str) -> bool:
        """Copy volume files using Alpine container (works for all volume types).
        
        This is a generic file copy method that works for PostgreSQL, Redis, media files,
        and any other volume type. The container must be stopped before calling this method
        to ensure data consistency.
        
        Args:
            source_volume: Source volume name
            target_volume: Target volume name
            
        Returns:
            True if copy succeeded, False otherwise
        """
        try:
            subprocess.run([
                "docker", "run", "--rm",
                "-v", f"{source_volume}:/source:ro",
                "-v", f"{target_volume}:/dest",
                "alpine", "sh", "-c", "cp -r /source/* /dest/ 2>/dev/null || true"
            ], check=True, capture_output=True)
            log_success(f"Volume files copied successfully: {source_volume} -> {target_volume}")
            return True
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to copy volume files: {e}")
            return False
    
    def _create_volume(self, volume_name: str) -> bool:
        """Create a Docker volume."""
        try:
            subprocess.run(
                ["docker", "volume", "create", volume_name],
                check=True,
                capture_output=True
            )
            return True
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to create volume {volume_name}: {e}")
            return False
    
    def create_worktree_volumes(self, branch_name: str, project_name: str = None, force_copy: bool = False) -> bool:
        """Create worktree-specific volumes, copying only if needed.
        
        For PostgreSQL volumes, this method will stop the original database container
        before copying to ensure data consistency, then restart it after copying.
        
        Note: Creates postgres, redis, media, and sqlite volumes. Caddy volumes 
        are shared globally across all worktrees and should not be copied.
        
        Uses centralized volume naming functions for consistency and DRY principles.
        """
        # Get target volumes using centralized function (worktree volumes use sanitized names)
        volume_names = get_volume_names(branch_name)
        
        # Get source volumes from this project root (not cwd) so we use the correct main DB
        project_volumes = get_source_volume_names_for_project_root(self.project_root)
        
        # Derive sanitized project name from same project root (for container matching)
        if project_name is None:
            source_postgres = project_volumes.get("postgres", "")
            unsanitized = source_postgres.replace(f"_{POSTGRES_VOLUME_SUFFIX}", "") if source_postgres else self.project_root.name
            project_name = sanitize_project_name(unsanitized)
        
        # Check if all volumes already exist
        all_volumes_exist = all(validate_volume_exists(vol_name) for vol_name in volume_names.values())
        
        if all_volumes_exist and not force_copy:
            # Volumes already exist - do NOT overwrite them (non-destructive)
            # This is critical: volumes may contain restored data from backups
            log_info(f"Worktree volumes already exist for {branch_name}, skipping creation (non-destructive)")
            return True
        
        # Determine appropriate log message
        if all_volumes_exist and force_copy:
            log_info(f"Recreating worktree-specific volumes for {branch_name}")
        else:
            log_info(f"Creating worktree-specific volumes for {branch_name}")
        
        # For PostgreSQL volumes, stop the original container before copying
        original_db_container = None
        if 'postgres' in project_volumes:
            source_postgres_volume = project_volumes['postgres']
            original_db_container = self._ensure_containers_stopped_for_volume_operation(
                source_postgres_volume, project_name, "volume copy"
            )
            log_info("PostgreSQL volumes will be copied safely to prevent database corruption")
        
        success = True
        for volume_type, source_volume in project_volumes.items():
            target_volume = volume_names[volume_type]
            if not self.copy_volume(source_volume, target_volume, project_name):
                success = False
        
        # Restart the original database container if we stopped it
        if original_db_container:
            self._restart_container(original_db_container)
        
        if success:
            log_success(f"Worktree volumes created for {branch_name}")
        return success
    
    def remove_volumes(self, branch_name: str) -> bool:
        """Remove worktree-specific volumes.
        
        Note: Removes postgres, redis, media, and sqlite volumes. Caddy volumes 
        are shared globally across all worktrees and should never be deleted 
        during worktree removal.
        """
        log_info(f"Removing worktree-specific volumes for {branch_name}")
        
        volume_names = get_volume_names(branch_name)
        success = True
        
        for volume_type, volume_name in volume_names.items():
            if validate_volume_exists(volume_name):
                try:
                    subprocess.run(
                        ["docker", "volume", "rm", volume_name],
                        check=True,
                        capture_output=True
                    )
                    log_info(f"Removed volume: {volume_name}")
                except subprocess.CalledProcessError:
                    log_warning(f"Failed to remove volume {volume_name}")
                    success = False
            else:
                log_warning(f"Volume {volume_name} not found")
        
        return success
    
    def backup_volumes(
        self,
        branch_name: str,
        backup_dir: Path,
        volume_names_override: Optional[Dict[str, str]] = None,
    ) -> Optional[Path]:
        """Backup worktree volumes to a tar file using file-level copy for all volumes.

        Stops containers safely before backup, then restarts them if they were running.
        All volumes (including PostgreSQL) are backed up using file-level copy.

        ``volume_names_override`` (``{key: actual_docker_volume_name}``) wins
        over the legacy ``get_volume_names_for_project_root`` convention.
        Callers that have done compose-based discovery should pass it; the
        legacy lookup is kept only as a fallback for older code paths.
        """
        backup_file = Path(backup_dir) / f"backup_{branch_name}.tar"
        if volume_names_override is not None:
            volume_names = volume_names_override
        else:
            volume_names = get_volume_names_for_project_root(self.project_root, branch_name)
        
        log_info(f"Backing up volumes for {branch_name} to {backup_file}")
        
        # Check if worktree is running before backup
        was_running = self._is_worktree_running(branch_name)
        if was_running:
            log_info(f"Worktree containers are running, stopping them safely before backup...")
            from ..core.worktree_orchestrator import WorktreeOrchestrator
            orchestrator = WorktreeOrchestrator(project_root=self.project_root)
            stop_result = orchestrator.stop_worktree(branch_name)
            if not stop_result.get("success"):
                log_warning("Failed to stop containers, but continuing with backup")
            else:
                log_success("Containers stopped successfully")
        
        # Create backup directory
        backup_dir.mkdir(parents=True, exist_ok=True)
        temp_backup_dir = backup_dir / "temp_backup"
        temp_backup_dir.mkdir(exist_ok=True)
        
        try:
            # Backup each volume in parallel (uncompressed tar per volume,
            # single gzip pass on the combined archive to avoid triple compression)
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _backup_one_volume(volume_type, volume_name):
                if not validate_volume_exists(volume_name):
                    log_warning(f"Volume {volume_name} not found, skipping")
                    return False
                log_info(f"Backing up volume: {volume_name} ({volume_type})")
                try:
                    subprocess.run([
                        "docker", "run", "--rm",
                        "-v", f"{volume_name}:/data",
                        "-v", f"{temp_backup_dir.absolute()}:/backup",
                        "alpine", "tar", "cf", f"/backup/{volume_name}.tar", "-C", "/data", "."
                    ], check=True, capture_output=True, text=True)
                    log_success(f"Volume backup created: {volume_name}.tar")
                    return True
                except subprocess.CalledProcessError as e:
                    log_error(f"Failed to backup volume {volume_name}: {e}")
                    if e.stderr:
                        log_error(f"Error details: {e.stderr}")
                    return False

            failed_volumes: list[str] = []
            with ThreadPoolExecutor(max_workers=min(4, len(volume_names))) as executor:
                futures = {
                    executor.submit(_backup_one_volume, vtype, vname): vname
                    for vtype, vname in volume_names.items()
                }
                for future in as_completed(futures):
                    vname = futures[future]
                    try:
                        ok = future.result()
                    except Exception as exc:  # noqa: BLE001 — any failure means the backup is incomplete
                        log_error(f"Volume backup raised for {vname}: {exc}")
                        ok = False
                    if not ok:
                        failed_volumes.append(vname)

            # Do not emit a partial archive — if any per-volume tar is missing or
            # failed to produce, abort the whole backup so callers don't mistake
            # a truncated archive for a successful one (silent data loss on restore).
            if failed_volumes:
                log_error(
                    "Aborting backup: one or more volumes failed "
                    f"({', '.join(failed_volumes)}). No archive will be produced."
                )
                if temp_backup_dir.exists():
                    shutil.rmtree(temp_backup_dir)
                if was_running:
                    log_info("Attempting to restart containers after backup failure...")
                    try:
                        from ..core.worktree_orchestrator import WorktreeOrchestrator
                        orchestrator = WorktreeOrchestrator(project_root=self.project_root)
                        orchestrator.start_worktree(branch_name)
                    except Exception as restart_error:
                        log_error(f"Failed to restart containers: {restart_error}")
                return None

            # Sanity check: every expected .tar file exists before we combine.
            missing = [
                vname for vname in volume_names.values()
                if not (temp_backup_dir / f"{vname}.tar").exists()
            ]
            if missing:
                log_error(f"Aborting backup: expected per-volume tar files missing: {missing}")
                if temp_backup_dir.exists():
                    shutil.rmtree(temp_backup_dir)
                return None

            # Single compression pass on the combined archive
            subprocess.run([
                "tar", "czf", str(backup_file), "-C", str(temp_backup_dir), "."
            ], check=True, capture_output=True)
            
            # Cleanup temp directory
            shutil.rmtree(temp_backup_dir)
            
            log_success(f"Backup created: {backup_file}")
            
            # Restart containers if they were running before
            if was_running:
                log_info("Restarting worktree containers in background...")
                from ..core.worktree_orchestrator import WorktreeOrchestrator
                orchestrator = WorktreeOrchestrator(project_root=self.project_root)
                import threading
                
                def start_in_background():
                    start_result = orchestrator.start_worktree(branch_name)
                    if start_result.get("success"):
                        log_success("Containers restarted successfully")
                    else:
                        log_warning(f"Failed to restart containers: {start_result.get('error', 'Unknown error')}")
                
                thread = threading.Thread(target=start_in_background, daemon=True)
                thread.start()
            
            return backup_file
            
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to create backup: {e}")
            # Cleanup temp directory
            if temp_backup_dir.exists():
                shutil.rmtree(temp_backup_dir)
            
            # Try to restart containers if they were running
            if was_running:
                log_info("Attempting to restart containers after backup failure...")
                try:
                    from ..core.worktree_orchestrator import WorktreeOrchestrator
                    orchestrator = WorktreeOrchestrator(project_root=self.project_root)
                    orchestrator.start_worktree(branch_name)
                except Exception as restart_error:
                    log_error(f"Failed to restart containers: {restart_error}")
            
            return None
    
    def get_volumes_for_service(self, branch_name: str, service_name: str) -> List[str]:
        """Get list of volume names associated with a specific service.
        
        Args:
            branch_name: Branch name for the worktree
            service_name: Service name from docker-compose.yml
            
        Returns:
            List of volume names (with worktree prefix) associated with the service
        """
        # Get worktree path
        git_manager = GitManager(project_root=self.project_root, validate=False)
        worktree_path = git_manager.find_worktree_path(branch_name)
        if not worktree_path:
            log_error(f"Could not find worktree path for '{branch_name}'")
            return []
        
        # Inspect the user's docker-compose.yml — it's authoritative for
        # service shape (volumes, image, etc.). The ilha overlay only adds
        # deltas, so reading the base is the right source for volume listing.
        compose_file = worktree_path / "docker-compose.yml"

        if not compose_file.exists():
            log_error(f"No docker-compose.yml found for worktree '{branch_name}'")
            return []

        try:
            with open(compose_file) as f:
                compose_data = yaml.safe_load(f) or {}
        except Exception as e:
            log_error(f"Failed to parse docker-compose.yml for worktree '{branch_name}': {e}")
            return []
        
        # Get service definition
        services = compose_data.get('services', {})
        if service_name not in services:
            log_error(f"Service '{service_name}' not found in worktree '{branch_name}'")
            return []
        
        service_config = services[service_name]
        
        # Get project name for volume prefix
        project_name = None
        config_path = self.project_root / ".ilha" / "config.yml"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                    project_name = config.get("project_name")
            except Exception:
                pass
        
        if not project_name:
            project_name = self.project_root.name
        
        project_name = sanitize_project_name(project_name)
        compose_project_name = f"{project_name}-{sanitize_project_name(branch_name)}"
        
        # Extract volumes from service
        volumes = []
        service_volumes = service_config.get('volumes', [])
        
        # Handle both list and dict formats
        if isinstance(service_volumes, list):
            for volume_spec in service_volumes:
                if isinstance(volume_spec, str):
                    # Format: "volume_name:/path/in/container" or "/path/in/container"
                    if ':' in volume_spec:
                        volume_name = volume_spec.split(':')[0].strip()
                    else:
                        # Anonymous volume, skip
                        continue
                elif isinstance(volume_spec, dict):
                    # Format: {"type": "volume", "source": "volume_name", ...}
                    volume_name = volume_spec.get('source') or volume_spec.get('target')
                    if not volume_name:
                        continue
                else:
                    continue
                
                # Check if it's a named volume (not anonymous)
                compose_volumes = compose_data.get('volumes', {})
                if volume_name in compose_volumes:
                    # This is a named volume from the volumes section
                    # Apply worktree prefix pattern: {project_name}-{branch_name}_{volume_name}
                    prefixed_volume_name = f"{compose_project_name}_{volume_name}"
                    volumes.append(prefixed_volume_name)
                elif volume_name.startswith('/') or ':' not in volume_spec if isinstance(volume_spec, str) else False:
                    # Anonymous volume or bind mount, skip
                    continue
                else:
                    # Might be an external volume or already prefixed
                    # Check if it matches our pattern
                    if f"{branch_name}_" in volume_name or compose_project_name in volume_name:
                        volumes.append(volume_name)
        
        # Also check for volume mounts in the service config
        # Some compose files use different structures
        if 'volumes' in service_config and isinstance(service_config['volumes'], dict):
            for volume_name in service_config['volumes'].keys():
                if volume_name in compose_data.get('volumes', {}):
                    prefixed_volume_name = f"{compose_project_name}_{volume_name}"
                    if prefixed_volume_name not in volumes:
                        volumes.append(prefixed_volume_name)
        
        return volumes
    
    def ensure_containers_stopped_before_restore(self, branch_name: str, worktree_path: Path) -> bool:
        """Ensure containers are stopped before volume restoration.
        
        This is critical because PostgreSQL locks database files while running,
        and file-level restoration requires unmounted volumes.
        
        Args:
            branch_name: Branch name for the worktree
            worktree_path: Path to the worktree directory
            
        Returns:
            True if containers are stopped (or were not running), False on error
        """
        from ..utils.path_utils import compose_files_for_invocation
        base_compose, overlay_compose = compose_files_for_invocation(worktree_path)
        if base_compose is None and overlay_compose is None:
            # No compose files means no containers to stop
            return True

        log_info("Ensuring containers are stopped before volume restoration...")
        try:
            from ..config.settings import sanitize_project_name
            import yaml

            # Get project name from config file in worktree's project root
            # Worktree path structure: <project_root>/worktrees/<branch_name>
            # So project root is worktree_path.parent.parent
            project_root = worktree_path.parent.parent
            config_file = project_root / ".ilha" / "config.yml"

            if config_file.exists():
                with open(config_file) as f:
                    config = yaml.safe_load(f) or {}
                    project_name = config.get("project_name", project_root.name)
            else:
                # Fallback to directory name
                project_name = project_root.name

            project_name = sanitize_project_name(project_name)
            compose_project_name = f"{project_name}-{sanitize_project_name(branch_name)}"

            cmd = ["docker", "compose"]
            if base_compose is not None:
                cmd += ["-f", str(base_compose)]
            if overlay_compose is not None:
                cmd += ["-f", str(overlay_compose)]
            cmd += ["-p", compose_project_name, "down"]
            result = subprocess.run(
                cmd,
                cwd=worktree_path,
                check=False,
                capture_output=True,
                timeout=60
            )
            
            if result.returncode == 0:
                log_success("Containers stopped successfully")
            else:
                # Check if containers were already stopped
                if "No such service" in result.stderr.decode('utf-8', errors='ignore') or \
                   "not found" in result.stderr.decode('utf-8', errors='ignore').lower():
                    log_info("No running containers found (already stopped)")
                else:
                    log_warning(f"Could not stop containers: {result.stderr.decode('utf-8', errors='ignore')}")
            
            return True
        except subprocess.TimeoutExpired:
            log_warning("Timeout stopping containers, but continuing with restoration")
            return True
        except Exception as e:
            log_warning(f"Could not stop containers before restore: {e}")
            return False
    
    def restore_volumes(self, branch_name: str, backup_file: Path) -> bool:
        """Restore worktree volumes from a backup file using file-level restore for all volumes.
        
        Stops containers safely before restore, then restarts them if they were running.
        All volumes (including PostgreSQL) are restored using file-level copy.
        
        Args:
            branch_name: Branch name for the worktree
            backup_file: Path to backup file. Can be either:
                - A package file (.ilha-package.tar.gz) containing nested backup_test.tar
                - A direct backup file (backup_test.tar) containing volume backups
        """
        if not backup_file.exists():
            log_error(f"Backup file {backup_file} not found")
            return False
        
        backup_size = backup_file.stat().st_size / (1024 * 1024)  # Size in MB
        log_info(f"Restoring volumes for {branch_name} from {backup_file} ({backup_size:.2f} MB)")
        
        # Check if worktree is running before restore
        was_running = self._is_worktree_running(branch_name)
        if was_running:
            log_info(f"Worktree containers are running, stopping them safely before restore...")
            from ..core.worktree_orchestrator import WorktreeOrchestrator
            orchestrator = WorktreeOrchestrator(project_root=self.project_root)
            stop_result = orchestrator.stop_worktree(branch_name)
            if not stop_result.get("success"):
                log_warning("Failed to stop containers, but continuing with restore")
            else:
                log_success("Containers stopped successfully")
        
        # Use project-root-based volume names so standalone import (cwd=/root) gets
        # correct names (e.g. business-intelligence-lipstick_*) instead of cwd-based (root-lipstick_*)
        volume_names = get_volume_names_for_project_root(self.project_root, branch_name)
        restore_temp_dir = backup_file.parent / "restore_temp"
        
        try:
            # Extract backup
            log_info(f"Extracting backup archive to temporary directory...")
            restore_temp_dir.mkdir(exist_ok=True)
            
            # Check if this is a package file (.ilha-package.tar.gz) or a direct backup file
            is_package_file = backup_file.name.endswith('.ilha-package.tar.gz')
            
            if is_package_file:
                # Extract the package file to get the nested backup_test.tar
                log_info("Detected package file, extracting to find nested backup archive...")
                extract_result = subprocess.run([
                    "tar", "xzf", str(backup_file), "-C", str(restore_temp_dir)
                ], check=True, capture_output=True, text=True)
                
                if extract_result.stderr:
                    log_info(f"Extraction output: {extract_result.stderr}")
                
                # Find the nested backup_test.tar file
                nested_backup_tar = None
                for pattern in [f"**/backup_{branch_name}.tar", "**/backup_*.tar"]:
                    matches = list(restore_temp_dir.glob(pattern))
                    if matches:
                        nested_backup_tar = matches[0]
                        break
                
                if nested_backup_tar and nested_backup_tar.exists():
                    log_info(f"Found nested backup archive: {nested_backup_tar.name}")
                    # Extract the nested tar to get the actual volume backup files
                    nested_extract_result = subprocess.run([
                        "tar", "xzf", str(nested_backup_tar), "-C", str(restore_temp_dir)
                    ], check=True, capture_output=True, text=True)
                    
                    if nested_extract_result.stderr:
                        log_info(f"Nested extraction output: {nested_extract_result.stderr}")
                    
                    log_success("Nested backup archive extracted successfully")
                else:
                    log_warning("No nested backup archive found in package file")
                    # Try to find volume backups directly in extracted package
            else:
                # Direct backup file - extract it directly
                log_info("Detected direct backup file, extracting...")
                extract_result = subprocess.run([
                    "tar", "xzf", str(backup_file), "-C", str(restore_temp_dir)
                ], check=True, capture_output=True, text=True)
                
                if extract_result.stderr:
                    log_info(f"Extraction output: {extract_result.stderr}")
            
            log_success("Backup archive extracted successfully")
            
            # List available backups (only .tar.gz format for file-level restore)
            available_tar_backups = list(restore_temp_dir.glob("*.tar.gz"))
            log_info(f"Found {len(available_tar_backups)} file backup(s) in archive")
            for backup in available_tar_backups:
                backup_size_mb = backup.stat().st_size / (1024 * 1024)
                log_info(f"  - {backup.name} ({backup_size_mb:.2f} MB)")
            
            # Create mapping of backup files to volume types
            # Backup files may have different project names, so we need to match by volume type suffix
            # Volume types map to suffixes: postgres -> postgres_data, redis -> redis_data, media -> media_files
            volume_type_suffixes = {
                "postgres": "postgres_data",
                "redis": "redis_data", 
                "media": "media_files"
            }
            backup_map_tar = {}
            log_info(f"Creating backup file mapping...")
            for backup_file in available_tar_backups:
                # Remove extension (.tar.gz)
                backup_name = backup_file.stem  # Remove .tar.gz
                
                log_info(f"Processing backup file: {backup_file.name}")
                for volume_type, volume_name in volume_names.items():
                    # Check if this backup matches the expected volume type by suffix
                    expected_suffix = f"_{volume_type_suffixes.get(volume_type, volume_type)}"
                    if backup_name.endswith(expected_suffix):
                        # File-level backup
                        if volume_type not in backup_map_tar:
                            backup_map_tar[volume_type] = backup_file
                            log_info(f"Mapped file backup {backup_file.name} to volume type {volume_type}")
                        break
            
            if backup_map_tar:
                log_info(f"Successfully mapped {len(backup_map_tar)} file backup(s) to volume types")
            else:
                log_warning("No backup files could be mapped to volume types - this may indicate a naming mismatch")
            
            # Restore each volume
            restored_count = 0
            skipped_count = 0
            failed_count = 0
            
            for volume_type, volume_name in volume_names.items():
                # Try to find file-level backup
                tar_backup = None
                # Try exact match first
                tar_backup = restore_temp_dir / f"{volume_name}.tar.gz"
                if not tar_backup.exists() and volume_type in backup_map_tar:
                    tar_backup = backup_map_tar[volume_type]
                    log_info(f"Using mapped file backup: {tar_backup.name} for volume {volume_name}")
                
                # Use file-level restore for all volumes
                if tar_backup and tar_backup.exists():
                    backup_to_use = tar_backup
                else:
                    log_warning(f"Volume backup for {volume_name} not found in backup archive")
                    log_warning(f"  Expected filename: {volume_name}.tar.gz")
                    skipped_count += 1
                    continue
                
                if backup_to_use.exists():
                    backup_size_mb = backup_to_use.stat().st_size / (1024 * 1024)
                    log_info(f"Restoring volume: {volume_name} ({volume_type}, {backup_size_mb:.2f} MB)")
                    
                    # Check if volume already exists
                    if validate_volume_exists(volume_name):
                        # For PostgreSQL volumes, check if it only contains empty initialization
                        # (PostgreSQL creates empty database on first start, which we want to overwrite)
                        should_remove_volume = False
                        
                        if volume_type == "postgres":
                            # Check if PostgreSQL volume only has initialization files (empty database)
                            try:
                                pg_check = subprocess.run([
                                    "docker", "run", "--rm",
                                    "-v", f"{volume_name}:/data",
                                    "alpine", "sh", "-c", "test -f /data/PG_VERSION && test -d /data/base && find /data/base -mindepth 2 -type f 2>/dev/null | head -1 | grep -q . && echo 'has_data' || echo 'empty_init'"
                                ], check=False, capture_output=True, text=True, timeout=5)
                                
                                if pg_check.stdout.strip() == "empty_init":
                                    log_warning(f"PostgreSQL volume {volume_name} exists but only contains empty initialization")
                                    log_info("Will remove empty volume to allow restoration of actual data...")
                                    should_remove_volume = True
                            except Exception as e:
                                log_warning(f"Could not check PostgreSQL volume contents: {e}")
                        
                        # Check volume size for other volume types
                        if not should_remove_volume:
                            try:
                                vol_size_check = subprocess.run([
                                    "docker", "run", "--rm",
                                    "-v", f"{volume_name}:/data",
                                    "alpine", "sh", "-c", "du -sb /data 2>/dev/null | cut -f1 || echo 0"
                                ], check=False, capture_output=True, text=True, timeout=5)
                                vol_size_bytes = int(vol_size_check.stdout.strip() or "0")
                                
                                if vol_size_bytes < 10000:  # Less than 10KB is likely empty
                                    log_warning(f"Volume {volume_name} exists but appears empty ({vol_size_bytes} bytes)")
                                    should_remove_volume = True
                            except Exception as e:
                                log_warning(f"Could not check volume size: {e}")
                        
                        # Remove volume if it's empty or only has empty initialization
                        if should_remove_volume:
                            log_info("Removing empty/initialized volume to allow restoration...")
                            containers = get_containers_using_volume(volume_name)
                            if containers:
                                log_warning(f"Volume {volume_name} is in use by containers: {', '.join(containers)}")
                                log_warning("Stopping containers to allow volume removal...")
                                # Try to stop containers using this volume
                                for container in containers:
                                    try:
                                        subprocess.run(["docker", "stop", container], 
                                                      check=False, capture_output=True, timeout=10)
                                        log_info(f"Stopped container: {container}")
                                    except Exception as e:
                                        log_warning(f"Could not stop container {container}: {e}")
                                
                                # Re-check if volume is still in use
                                containers = get_containers_using_volume(volume_name)
                                if containers:
                                    log_warning(f"Volume {volume_name} still in use, skipping restoration")
                                    skipped_count += 1
                                    continue
                            
                            # Remove empty/initialized volume
                            try:
                                subprocess.run(["docker", "volume", "rm", volume_name], 
                                              check=True, capture_output=True, timeout=10)
                                log_success(f"Removed empty/initialized volume: {volume_name}")
                            except subprocess.CalledProcessError as e:
                                log_error(f"Failed to remove volume {volume_name}: {e}")
                                if e.stderr:
                                    log_error(f"Error: {e.stderr}")
                                skipped_count += 1
                                continue
                        else:
                            # Volume has actual data - check if containers are using it
                            containers = get_containers_using_volume(volume_name)
                            if containers:
                                log_warning(f"Volume {volume_name} already exists with data and is in use by containers")
                                log_warning("Skipping restoration of this volume to avoid data loss")
                                skipped_count += 1
                                continue
                            else:
                                # Volume has data but no containers - safe to overwrite
                                log_info(f"Volume {volume_name} exists with data but no containers are using it")
                                log_info("Removing existing volume to restore from backup...")
                                try:
                                    subprocess.run(["docker", "volume", "rm", volume_name], 
                                                  check=True, capture_output=True, timeout=10)
                                    log_success(f"Removed existing volume: {volume_name}")
                                except subprocess.CalledProcessError as e:
                                    log_error(f"Failed to remove volume {volume_name}: {e}")
                                    skipped_count += 1
                                    continue
                    
                    # File-level restore for all volumes
                    # Create volume first
                    log_info(f"Creating volume: {volume_name}")
                    if not self._create_volume(volume_name):
                        log_error(f"Failed to create volume: {volume_name}")
                        failed_count += 1
                        continue
                    log_success(f"Volume created: {volume_name}")
                    
                    # Restore data using tar
                    backup_filename = backup_to_use.name
                    log_info(f"Restoring data to volume {volume_name} from backup {backup_filename}...")
                    try:
                        restore_result = subprocess.run([
                            "docker", "run", "--rm",
                            "-v", f"{volume_name}:/data",
                            "-v", f"{restore_temp_dir.absolute()}:/backup",
                            "alpine", "sh", "-c", f"cd /data && rm -rf * .[^.]* 2>/dev/null || true && tar xzf /backup/{backup_filename}"
                        ], check=True, capture_output=True, text=True, timeout=300)
                        
                        if restore_result.stderr:
                            # tar may output warnings to stderr that are not errors
                            if "Removing leading" in restore_result.stderr:
                                log_info(f"tar output: {restore_result.stderr.strip()}")
                        
                        # Verify volume has data
                        try:
                            verify_result = subprocess.run([
                                "docker", "run", "--rm",
                                "-v", f"{volume_name}:/data",
                                "alpine", "sh", "-c", "du -sh /data | cut -f1"
                            ], check=True, capture_output=True, text=True, timeout=10)
                            volume_data_size = verify_result.stdout.strip()
                            log_success(f"Volume {volume_name} restored successfully (data size: {volume_data_size})")
                            restored_count += 1
                        except subprocess.TimeoutExpired:
                            log_warning(f"Timeout verifying volume {volume_name}, but restoration may have succeeded")
                            restored_count += 1
                        except subprocess.CalledProcessError as e:
                            log_error(f"Failed to verify volume {volume_name}: {e}")
                            failed_count += 1
                    except subprocess.CalledProcessError as e:
                        log_error(f"Failed to restore volume {volume_name}: {e}")
                        if e.stderr:
                            log_error(f"Error details: {e.stderr}")
                        failed_count += 1
            
            # Summary
            log_info(f"Volume restoration summary:")
            log_info(f"  - Restored: {restored_count}")
            log_info(f"  - Skipped: {skipped_count}")
            log_info(f"  - Failed: {failed_count}")
            
            if failed_count > 0:
                log_error(f"Failed to restore {failed_count} volume(s)")
                # Cleanup
                if restore_temp_dir.exists():
                    shutil.rmtree(restore_temp_dir)
                return False
            
            # Warn if no volumes were restored but backups were expected
            total_backups = len(available_tar_backups)
            if restored_count == 0 and total_backups > 0:
                log_warning(f"No volumes were restored despite {total_backups} backup file(s) being found")
                log_warning("This may indicate a volume name mismatch between source and target projects")
                log_warning("Volumes will be created empty when containers start")
            
            # Cleanup
            log_info("Cleaning up temporary extraction directory...")
            shutil.rmtree(restore_temp_dir)
            
            if restored_count > 0:
                log_success(f"Volumes restored for {branch_name} ({restored_count} volume(s))")
            else:
                log_warning(f"No volumes were restored for {branch_name} (backup may be empty or names don't match)")
            
            # Restart containers if they were running before
            if was_running:
                log_info("Restarting worktree containers in background...")
                from ..core.worktree_orchestrator import WorktreeOrchestrator
                orchestrator = WorktreeOrchestrator(project_root=self.project_root)
                import threading
                
                def start_in_background():
                    start_result = orchestrator.start_worktree(branch_name)
                    if start_result.get("success"):
                        log_success("Containers restarted successfully")
                    else:
                        log_warning(f"Failed to restart containers: {start_result.get('error', 'Unknown error')}")
                
                thread = threading.Thread(target=start_in_background, daemon=True)
                thread.start()
            
            return True
            
        except subprocess.CalledProcessError as e:
            log_error(f"Failed to restore volumes: {e}")
            if e.stdout:
                log_error(f"Command stdout: {e.stdout}")
            if e.stderr:
                log_error(f"Command stderr: {e.stderr}")
            # Cleanup
            if restore_temp_dir.exists():
                shutil.rmtree(restore_temp_dir)
            
            # Try to restart containers if they were running
            if was_running:
                log_info("Attempting to restart containers after restore failure...")
                try:
                    from ..core.worktree_orchestrator import WorktreeOrchestrator
                    orchestrator = WorktreeOrchestrator(project_root=self.project_root)
                    orchestrator.start_worktree(branch_name)
                except Exception as restart_error:
                    log_error(f"Failed to restart containers: {restart_error}")
            
            return False
        except Exception as e:
            log_error(f"Unexpected error during volume restoration: {e}")
            import traceback
            log_error(f"Traceback: {traceback.format_exc()}")
            # Cleanup
            if restore_temp_dir.exists():
                shutil.rmtree(restore_temp_dir)
            
            # Try to restart containers if they were running
            if was_running:
                log_info("Attempting to restart containers after restore failure...")
                try:
                    from ..core.worktree_orchestrator import WorktreeOrchestrator
                    orchestrator = WorktreeOrchestrator(project_root=self.project_root)
                    orchestrator.start_worktree(branch_name)
                except Exception as restart_error:
                    log_error(f"Failed to restart containers: {restart_error}")
            
            return False
    
    def _build_compose_base_command(self) -> List[str]:
        """Build the base docker compose command array.
        
        Returns:
            List containing the docker compose command (either ["docker", "compose"] or ["docker-compose"])
        """
        if self.compose_cmd == "docker compose":
            return ["docker", "compose"]
        else:
            return [self.compose_cmd]
    
    def _resolve_working_directory(self, working_dir: Optional[Path]) -> Path:
        """Resolve and normalize working directory for docker compose commands.
        
        Args:
            working_dir: Optional working directory. If None, uses project_root.
            
        Returns:
            Resolved absolute Path to working directory
        """
        if working_dir is None:
            working_dir = self.project_root
        
        # Convert to absolute path to ensure Docker resolves it correctly
        if isinstance(working_dir, Path):
            return working_dir.resolve()
        else:
            return Path(working_dir).resolve()
    
    def _prepare_compose_environment(self, working_dir: Path, project_name: Optional[str] = None) -> Dict[str, str]:
        """Prepare environment variables for docker compose commands.
        
        Args:
            working_dir: Resolved working directory path
            project_name: Optional project name to set COMPOSE_PROJECT_NAME
            
        Returns:
            Dictionary of environment variables (copy of os.environ with additions)
        """
        import os
        env = os.environ.copy()
        env["PROJECT_ROOT"] = str(working_dir)  # Absolute path to worktree or project root
        env["COMPOSE_PROJECT_ROOT"] = str(working_dir)
        env["PWD"] = str(working_dir)
        
        # Set COMPOSE_PROJECT_NAME to ensure Docker Compose uses correct project name for volumes
        if project_name:
            env["COMPOSE_PROJECT_NAME"] = project_name
        
        return env
    
    def _handle_compose_error(self, e: subprocess.CalledProcessError, context: str = "") -> None:
        """Handle errors from docker compose commands with consistent logging.
        
        Args:
            e: CalledProcessError from subprocess.run
            context: Optional context message to include in error log
        """
        error_msg = f"Docker compose command failed: {e}"
        if context:
            error_msg = f"{context} - {error_msg}"
        log_error(error_msg)
        if e.stdout:
            log_error(f"STDOUT: {e.stdout}")
        if e.stderr:
            log_error(f"STDERR: {e.stderr}")
    
    def run_compose_command(self,
                          compose_file: Path,
                          command: List[str],
                          env_file: Optional[Path] = None,
                          project_name: Optional[str] = None,
                          working_dir: Optional[Path] = None,
                          extra_flags: Optional[List[str]] = None,
                          profile: Optional[str] = None,
                          base_compose_file: Optional[Path] = None) -> Tuple[bool, Optional[str]]:
        """Run a docker compose command.

        Compose merges files in the order given; the LAST ``-f`` wins on
        conflicts. In the overlay model that means callers pass the user's
        ``docker-compose.yml`` as ``base_compose_file`` (first) and the ilha
        overlay as ``compose_file`` (second, wins on Caddy labels etc.).

        Args:
            compose_file: Compose file passed last; overrides anything in
                ``base_compose_file`` on conflicts.
            command: Command to run (e.g., ["up", "-d"]).
            env_file: Optional ``--env-file``.
            project_name: Optional ``-p`` value.
            working_dir: Optional working directory; cwd of the subprocess.
            extra_flags: Optional flags appended after ``command``.
            profile: Optional ``--profile`` value.
            base_compose_file: Compose file passed first; "the base." When
                None, only ``compose_file`` is passed.

        Returns:
            (True, None) on success; (False, stderr_text) on failure.
        """
        # Build base command
        cmd = self._build_compose_base_command()

        # Resolve working directory
        working_dir = self._resolve_working_directory(working_dir)
        # Set +x on entrypoint scripts referenced by the base compose's /app
        # mounts (the user's docker-compose.yml has the volume bindings; the
        # overlay does not).
        ensure_entrypoint_executable(base_compose_file or compose_file, working_dir)

        # Explicitly load .env from the working directory first, if it exists
        main_env_file = working_dir / ".env"
        if main_env_file.exists():
            cmd.extend(["--env-file", str(main_env_file)])

        # Then, load the worktree-specific env.ilha, which will override .env
        if env_file and env_file.exists():
            cmd.extend(["--env-file", str(env_file)])

        if project_name:
            cmd.extend(["-p", project_name])

        # Compose merges files in the order given; the LAST -f wins on conflicts.
        # In the overlay model we want the user's compose first (base) and ilha's
        # overlay second so the overlay's deltas win — callers pass the user's
        # compose as ``base_compose_file`` and the overlay as ``compose_file``.
        if base_compose_file is not None:
            cmd.extend(["-f", str(base_compose_file)])
        cmd.extend(["-f", str(compose_file)])

        # Add profile flag if provided
        if profile:
            cmd.extend(["--profile", profile])

        cmd.extend(command)
        
        # Add any extra flags at the end
        if extra_flags:
            cmd.extend(extra_flags)
        
        # Prepare environment variables
        env = self._prepare_compose_environment(working_dir, project_name)
        
        # Debug logging for path resolution
        log_info(f"Docker Compose execution context:")
        log_info(f"  Working directory: {working_dir}")
        log_info(f"  PROJECT_ROOT: {env['PROJECT_ROOT']}")
        log_info(f"  Compose file: {compose_file}")
        log_info(f"  Command: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=working_dir, env=env)
            return (True, None)
        except subprocess.CalledProcessError as e:
            self._handle_compose_error(e)
            return (False, e.stderr or "")

    def start_services(self, compose_file: Path, env_file: Optional[Path] = None,
                      project_name: Optional[str] = None, working_dir: Optional[Path] = None) -> bool:
        """Start services using docker compose."""
        success, _ = self.run_compose_command(compose_file, ["up", "-d"], env_file, project_name, working_dir)
        return success


    def stop_services(self, compose_file: Path, env_file: Optional[Path] = None,
                     project_name: Optional[str] = None, working_dir: Optional[Path] = None) -> bool:
        """Stop services using docker compose."""
        success, _ = self.run_compose_command(compose_file, ["down"], env_file, project_name, working_dir)
        return success
    
    def get_volume_sizes(self) -> Dict[str, str]:
        """Get sizes of all worktree volumes."""
        sizes = {}
        try:
            result = subprocess.run([
                "docker", "volume", "ls", "-q"
            ], capture_output=True, text=True, check=True)
            
            volumes = result.stdout.strip().split('\n')
            for volume in volumes:
                if volume and any(suffix in volume for suffix in ["_postgres_data", "_redis_data", "_media_files"]):
                    try:
                        size_result = subprocess.run([
                            "docker", "run", "--rm", "-v", f"{volume}:/data",
                            "alpine", "du", "-sh", "/data"
                        ], capture_output=True, text=True, check=True)
                        size = size_result.stdout.split()[0]
                        sizes[volume] = size
                    except subprocess.CalledProcessError:
                        sizes[volume] = "unknown"
            
        except subprocess.CalledProcessError:
            pass
        
        return sizes
    
    def list_volumes(self) -> List[str]:
        """List all worktree volumes."""
        volumes = []
        try:
            result = subprocess.run([
                "docker", "volume", "ls", "-q"
            ], capture_output=True, text=True, check=True)
            
            for volume in result.stdout.strip().split('\n'):
                if volume and any(suffix in volume for suffix in ["_postgres_data", "_redis_data", "_media_files"]):
                    volumes.append(volume)
                    
        except subprocess.CalledProcessError:
            pass
        
        return volumes
    
    def run_compose_passthrough(self, branch_name: str, compose_args: List[str]) -> bool:
        """Run docker compose command with automatic base+overlay resolution.

        Args:
            branch_name: Name of the worktree/branch
            compose_args: Docker compose arguments to pass through

        Returns:
            True if command succeeded, False otherwise
        """
        from ..core.git_manager import GitManager
        from ..utils.path_utils import compose_files_for_invocation, get_env_compose_file_path
        from ..config.settings import get_project_name, sanitize_project_name

        # Validate worktree exists
        git_manager = GitManager()
        if not git_manager.validate_worktree_exists(branch_name):
            log_error(f"Worktree for branch '{branch_name}' does not exist")
            return False

        # Get worktree path
        worktree_path = git_manager.find_worktree_path(branch_name)
        if not worktree_path:
            log_error(f"Could not find worktree directory for branch '{branch_name}'")
            return False

        # Get base + overlay compose files
        base_compose, overlay_compose = compose_files_for_invocation(worktree_path)
        if base_compose is None:
            log_error(f"docker-compose.yml not found in worktree: {worktree_path}")
            return False
        if overlay_compose is None:
            log_error(
                f"ilha overlay not found at {worktree_path}/.ilha/docker-compose.overlay.yml. "
                f"Re-run `ilha setup` to regenerate."
            )
            return False

        # Get environment file
        env_file = get_env_compose_file_path(worktree_path)
        if not env_file.exists():
            log_error(f"Environment file not found: {env_file}")
            return False
        
        # Find project root from worktree path (search upward for .ilha/config.yml)
        # This ensures we get the correct project name even when command is run from wrong directory
        project_root_from_worktree = worktree_path
        from ..config.settings import ILHA_DIR
        while project_root_from_worktree != project_root_from_worktree.parent:
            config_path = project_root_from_worktree / ILHA_DIR / "config.yml"
            if config_path.exists():
                break
            project_root_from_worktree = project_root_from_worktree.parent
        
        # Get project name from the project root we found
        import yaml
        project_name = None
        config_path = project_root_from_worktree / ILHA_DIR / "config.yml"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                    project_name = config.get("project_name")
            except Exception:
                pass
        
        # Fallback to project root directory name if config doesn't have project_name
        if not project_name:
            project_name = project_root_from_worktree.name
        
        project_name = sanitize_project_name(project_name)
        compose_project_name = f"{project_name}-{sanitize_project_name(branch_name)}"
        
        # Build base command
        cmd = self._build_compose_base_command()

        # Add environment file
        cmd.extend(["--env-file", str(env_file)])

        # Add project name
        cmd.extend(["-p", compose_project_name])

        # Pass user's compose first (base) then ilha's overlay (wins on merges)
        cmd.extend(["-f", str(base_compose)])
        cmd.extend(["-f", str(overlay_compose)])

        # Add the passthrough arguments
        cmd.extend(compose_args)

        # Resolve working directory (already resolved as worktree_path, but ensure consistency)
        working_dir = self._resolve_working_directory(worktree_path)

        # Prepare environment variables
        env = self._prepare_compose_environment(working_dir, compose_project_name)

        log_info(f"Running docker compose command for worktree '{branch_name}':")
        log_info(f"  Working directory: {working_dir}")
        log_info(f"  Base compose: {base_compose}")
        log_info(f"  Overlay compose: {overlay_compose}")
        log_info(f"  Environment file: {env_file}")
        log_info(f"  Project name: {compose_project_name}")
        log_info(f"  Command: {' '.join(cmd)}")
        
        try:
            # Run command and stream output to user
            result = subprocess.run(cmd, cwd=working_dir, env=env)
            return result.returncode == 0
        except Exception as e:
            log_error(f"Failed to run docker compose command: {e}")
            return False

    async def start_worktree_containers(self, branch_name: str, worktree_path: Path,
                                       project_root: Path) -> Dict[str, Any]:
        """Start containers for a worktree asynchronously."""
        try:
            from ..utils.path_utils import compose_files_for_invocation, get_env_compose_file_path
            from ..config.settings import get_project_name, sanitize_project_name

            base_compose, overlay_compose = compose_files_for_invocation(worktree_path)
            env_file = get_env_compose_file_path(worktree_path)

            if base_compose is None:
                return {"success": False, "error": f"docker-compose.yml not found in worktree '{branch_name}'"}
            if overlay_compose is None:
                return {"success": False, "error": f"ilha overlay missing for worktree '{branch_name}' — re-run `ilha setup`"}

            project_name = sanitize_project_name(get_project_name())
            compose_project_name = f"{project_name}-{sanitize_project_name(branch_name)}"

            success, _ = self.run_compose_command(
                overlay_compose,
                ["up", "-d"],
                env_file=env_file,
                project_name=compose_project_name,
                working_dir=worktree_path,
                base_compose_file=base_compose,
            )

            return {
                "success": success,
                "message": f"Started containers for worktree '{branch_name}'" if success else "Failed to start containers"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def stop_worktree_containers(self, branch_name: str) -> Dict[str, Any]:
        """Stop containers for a worktree asynchronously."""
        try:
            from ..core.git_manager import GitManager
            from ..utils.path_utils import compose_files_for_invocation, get_env_compose_file_path
            from ..config.settings import get_project_name, sanitize_project_name

            git_manager = GitManager(validate=False)
            worktree_path = git_manager.find_worktree_path(branch_name)

            if not worktree_path:
                return {"success": False, "error": f"Worktree for branch '{branch_name}' not found"}

            base_compose, overlay_compose = compose_files_for_invocation(worktree_path)
            env_file = get_env_compose_file_path(worktree_path)

            project_name = sanitize_project_name(get_project_name())
            compose_project_name = f"{project_name}-{sanitize_project_name(branch_name)}"

            if overlay_compose is None or base_compose is None:
                # Without overlay we can still try to stop using whatever compose
                # is at the worktree root; missing files are non-fatal for `down`.
                compose_to_use = overlay_compose or base_compose
                if compose_to_use is None:
                    return {"success": True, "message": "No compose files; nothing to stop"}
                success, _ = self.run_compose_command(
                    compose_to_use,
                    ["down"],
                    env_file=env_file,
                    project_name=compose_project_name,
                    working_dir=worktree_path,
                )
            else:
                success, _ = self.run_compose_command(
                    overlay_compose,
                    ["down"],
                    env_file=env_file,
                    project_name=compose_project_name,
                    working_dir=worktree_path,
                    base_compose_file=base_compose,
                )

            return {
                "success": success,
                "message": f"Stopped containers for worktree '{branch_name}'" if success else "Failed to stop containers"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _is_worktree_running(self, branch_name: str) -> bool:
        """Check if worktree containers are currently running.
        
        Args:
            branch_name: Branch name for the worktree
            
        Returns:
            True if any containers are running, False otherwise
        """
        try:
            from ..config.settings import get_project_name, sanitize_project_name
            
            project_name = sanitize_project_name(get_project_name())
            compose_project_name = f"{project_name}-{sanitize_project_name(branch_name)}"
            
            # Get running containers for this project
            result = subprocess.run([
                "docker", "ps", 
                "--filter", f"label=com.docker.compose.project={compose_project_name}",
                "--format", "{{.Names}}"
            ], capture_output=True, text=True, check=False)
            
            # If Docker is not running, return False
            if result.returncode != 0:
                return False
            
            # Check if any containers are running
            running_containers = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
            return len(running_containers) > 0
        except Exception as e:
            log_warning(f"Failed to check if worktree is running: {e}")
            return False
    
    async def get_worktree_containers(self, branch_name: str) -> List[Dict[str, Any]]:
        """Get container status for a worktree asynchronously."""
        try:
            from ..config.settings import get_project_name, sanitize_project_name
            
            project_name = sanitize_project_name(get_project_name())
            compose_project_name = f"{project_name}-{sanitize_project_name(branch_name)}"
            
            # Get containers for this project
            result = subprocess.run([
                "docker", "ps", "-a", 
                "--filter", f"label=com.docker.compose.project={compose_project_name}",
                "--format", "{{.Names}}|{{.Status}}|{{.Ports}}|{{.Image}}"
            ], capture_output=True, text=True, check=False)
            
            # If Docker is not running, return empty list
            if result.returncode != 0:
                return []
            
            containers = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    parts = line.split('|')
                    if len(parts) >= 4:
                        containers.append({
                            "name": parts[0],
                            "status": parts[1],
                            "state": "running" if "Up" in parts[1] else "stopped",
                            "ports": parts[2],
                            "image": parts[3]
                        })
            
            return containers
        except Exception as e:
            log_warning(f"Failed to get containers: {e}")
            return []

    async def get_worktree_volumes(self, branch_name: str) -> List[Dict[str, Any]]:
        """Get volumes for a worktree asynchronously."""
        try:
            from ..config.settings import get_volume_names
            from ..utils.validation import validate_volume_exists
            
            volume_names = get_volume_names(branch_name)
            volumes = []
            
            for volume_type, volume_name in volume_names.items():
                if validate_volume_exists(volume_name):
                    volumes.append({
                        "name": volume_name,
                        "type": volume_type,
                        "branch": branch_name,
                        "exists": True
                    })
            
            return volumes
        except Exception as e:
            log_warning(f"Failed to get volumes: {e}")
            return []

    async def clean_worktree_volumes(self, branch_name: str) -> Dict[str, Any]:
        """Clean up volumes for a worktree asynchronously."""
        try:
            success = self.remove_volumes(branch_name)
            return {
                "success": success,
                "message": f"Cleaned volumes for worktree '{branch_name}'" if success else "Failed to clean volumes"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def get_worktree_containers_sync(self, branch_name: str) -> List[Dict[str, Any]]:
        """Get container status for a worktree synchronously.
        
        This is a synchronous wrapper around the async get_worktree_containers() method.
        Used by WorktreeOrchestrator.get_worktree_info() which is synchronous.
        
        Args:
            branch_name: Branch name for the worktree
            
        Returns:
            List of container dictionaries with name, status, state, ports, and image
        """
        import asyncio
        try:
            return asyncio.run(self.get_worktree_containers(branch_name))
        except Exception as e:
            log_warning(f"Failed to get containers synchronously: {e}")
            return []
    
    def get_worktree_volumes_sync(self, branch_name: str) -> List[Dict[str, Any]]:
        """Get volumes for a worktree synchronously.
        
        This is a synchronous wrapper around the async get_worktree_volumes() method.
        Used by WorktreeOrchestrator.get_worktree_info() which is synchronous.
        
        Args:
            branch_name: Branch name for the worktree
            
        Returns:
            List of volume dictionaries with name, type, branch, and exists status
        """
        import asyncio
        try:
            return asyncio.run(self.get_worktree_volumes(branch_name))
        except Exception as e:
            log_warning(f"Failed to get volumes synchronously: {e}")
            return []