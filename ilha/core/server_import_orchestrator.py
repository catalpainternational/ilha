"""
Server-side import orchestrator for ilha.

This module provides orchestration logic that runs on remote servers
to import packages and start services. Replaces the 1000+ line Bash script
with Python code that reuses existing ilha modules.
"""

import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

from ..core.package_manager import PackageManager
from ..core.docker_manager import DockerManager
from ..commands.caddy import CaddyManager
from ..utils.logging import log_info, log_success, log_warning, log_error
from ..utils.validation import validate_git_repository


class ServerImportOrchestrator:
    """Orchestrates package import and service startup on remote servers."""
    
    def __init__(self, project_root: Optional[Path] = None):
        """Initialize server import orchestrator.
        
        Args:
            project_root: Project root directory. If None, auto-detects.
        """
        if project_root is None:
            # Try to find existing project
            project_root = self._find_existing_project()
            if project_root is None:
                # Standalone mode - use /root as default
                project_root = Path("/root")
        
        self.project_root = Path(project_root).resolve()
        self.package_manager = PackageManager(project_root=self.project_root)
        self.docker_manager = DockerManager(project_root=self.project_root)
        self.caddy_manager = CaddyManager()
    
    def _find_existing_project(self) -> Optional[Path]:
        """Find existing ilha project by locating .ilha/config.yml.
        
        Returns:
            Path to project root if found, None otherwise
        """
        # Search in common locations
        search_paths = [Path("/root"), Path.home()]
        
        for base_path in search_paths:
            if not base_path.exists():
                continue
            
            # Search up to 3 levels deep
            for depth in range(1, 4):
                for path in base_path.rglob(".ilha/config.yml"):
                    if path.parent.parent == base_path or depth <= 3:
                        project_root = path.parent.parent
                        # Verify it's a valid project (has git or is standalone)
                        if (project_root / ".ilha" / "config.yml").exists():
                            return project_root
        
        return None
    
    def _ensure_git_identity(self) -> None:
        """Ensure git identity is configured to avoid commit failures."""
        try:
            # Check if git email is set
            result = subprocess.run(
                ["git", "config", "--global", "user.email"],
                capture_output=True,
                text=True,
                check=False
            )
            if result.returncode != 0 or not result.stdout.strip():
                subprocess.run(
                    ["git", "config", "--global", "user.email", "ilha@local"],
                    check=False
                )
                log_info("Set git user.email to ilha@local")
            
            # Check if git name is set
            result = subprocess.run(
                ["git", "config", "--global", "user.name"],
                capture_output=True,
                text=True,
                check=False
            )
            if result.returncode != 0 or not result.stdout.strip():
                subprocess.run(
                    ["git", "config", "--global", "user.name", "ilha"],
                    check=False
                )
                log_info("Set git user.name to ilha")
        except Exception as e:
            log_warning(f"Failed to configure git identity: {e}")
    
    def _cleanup_existing_worktree(self, branch_name: str, project_root: Path) -> bool:
        """Clean up existing worktree if it exists.
        
        Args:
            branch_name: Branch name
            project_root: Project root directory
            
        Returns:
            True if cleanup succeeded or no worktree existed, False on critical failure
        """
        from ..core.git_manager import GitManager
        
        git_manager = GitManager(project_root=project_root, validate=False)
        worktree_path = git_manager.find_worktree_path(branch_name)
        
        if not worktree_path or not worktree_path.exists():
            log_info(f"No existing worktree found for branch '{branch_name}'")
            return True
        
        log_info(f"Worktree for branch '{branch_name}' already exists at: {worktree_path}")
        log_info("Cleaning up existing worktree and volumes before import...")
        
        # Try to remove via ilha command first
        try:
            result = subprocess.run(
                ["ilha", "remove", branch_name, "--force"],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=60
            )
            if result.returncode == 0:
                log_success("Existing worktree and volumes removed via ilha")
                return True
            else:
                log_warning("Failed to remove existing worktree via ilha, attempting manual cleanup...")
        except Exception as e:
            log_warning(f"Error removing worktree via ilha: {e}")
        
        # Manual cleanup: stop containers, remove directory, clean git references
        try:
            # Stop containers
            result = subprocess.run(
                ["docker", "ps", "--filter", f"name={branch_name}", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                check=False
            )
            if result.stdout.strip():
                for container in result.stdout.strip().split('\n'):
                    if container:
                        subprocess.run(
                            ["docker", "stop", container],
                            capture_output=True,
                            check=False
                        )
            
            # Remove directory
            import shutil
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
            
            # Clean git worktree references
            if validate_git_repository(project_root):
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=project_root,
                    capture_output=True,
                    check=False
                )
                # Try to remove worktree reference
                subprocess.run(
                    ["git", "worktree", "remove", str(worktree_path), "--force"],
                    cwd=project_root,
                    capture_output=True,
                    check=False
                )
            
            log_success("Worktree cleanup completed")
            return True
        except Exception as e:
            log_error(f"Failed to cleanup worktree: {e}")
            return False
    
    def _verify_volumes(self, branch_name: str, project_root: Path) -> Dict[str, any]:
        """Verify volumes were restored correctly.
        
        Args:
            branch_name: Branch name
            project_root: Project root directory
            
        Returns:
            Dictionary with volume verification results
        """
        from ..config.settings import get_project_name, sanitize_project_name
        
        project_name = sanitize_project_name(get_project_name())
        volumes_found = 0
        volumes_missing = 0
        empty_volumes = 0
        need_restore = False
        
        volume_types = ["postgres_data", "redis_data", "media_files", "sqlite_data"]
        
        for vol_type in volume_types:
            if project_name:
                vol_name = f"{project_name}-{branch_name}_{vol_type}"
            else:
                vol_name = f"{sanitize_project_name(project_root.name)}-{branch_name}_{vol_type}"
            
            # Check if volume exists
            result = subprocess.run(
                ["docker", "volume", "inspect", vol_name],
                capture_output=True,
                text=True,
                check=False
            )
            
            if result.returncode == 0:
                # Check volume size
                mountpoint_result = subprocess.run(
                    ["docker", "volume", "inspect", vol_name, "--format", "{{.Mountpoint}}"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                mountpoint = mountpoint_result.stdout.strip()
                
                size_result = subprocess.run(
                    ["du", "-sb", mountpoint],
                    capture_output=True,
                    text=True,
                    check=False
                )
                
                if size_result.returncode == 0:
                    size_bytes = int(size_result.stdout.split()[0])
                    min_size = 1048576 if vol_type == "postgres_data" else 10000
                    
                    if size_bytes < min_size:
                        log_error(f"Volume {vol_name} appears empty (size: {size_bytes} bytes)")
                        empty_volumes += 1
                        need_restore = True
                    else:
                        log_success(f"Volume found: {vol_name}")
                        volumes_found += 1
                else:
                    volumes_found += 1
            else:
                # Try to find by pattern
                result = subprocess.run(
                    ["docker", "volume", "ls", "--format", "{{.Name}}"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                found = False
                for vol in result.stdout.strip().split('\n'):
                    if vol.endswith(f"{branch_name}_{vol_type}"):
                        log_success(f"Volume found: {vol}")
                        volumes_found += 1
                        found = True
                        break
                
                if not found:
                    log_error(f"Volume missing: pattern *{branch_name}_{vol_type}*")
                    volumes_missing += 1
                    need_restore = True
        
        return {
            "volumes_found": volumes_found,
            "volumes_missing": volumes_missing,
            "empty_volumes": empty_volumes,
            "need_restore": need_restore
        }
    
    def _restore_volumes_if_needed(self, package_path: str, branch_name: str, 
                                   project_root: Path, is_standalone: bool) -> bool:
        """Restore volumes if verification indicates they're missing or empty.
        
        Args:
            package_path: Path to package file
            branch_name: Branch name
            project_root: Project root directory
            is_standalone: Whether in standalone mode
            
        Returns:
            True if restore succeeded or not needed, False on failure
        """
        verification = self._verify_volumes(branch_name, project_root)
        
        if not verification["need_restore"]:
            log_success(f"All volumes verified: {verification['volumes_found']} volume(s) found")
            return True
        
        log_warning(f"Volumes need restoration: {verification['volumes_missing']} missing, "
                   f"{verification['empty_volumes']} empty")
        
        # Convert package_path to absolute path to avoid path resolution issues
        # when cwd is different from where the package file is located
        package_file = Path(package_path)
        if not package_file.is_absolute():
            # Try to resolve relative to original location first, then current directory
            if not package_file.exists():
                # Try resolving from /root (common location for server imports)
                root_package = Path("/root") / package_file
                if root_package.exists():
                    package_file = root_package.resolve()
                else:
                    # Try resolving from current working directory
                    package_file = Path.cwd() / package_file
            package_file = package_file.resolve()
        else:
            package_file = package_file.resolve()
        
        # Verify the absolute path exists
        if not package_file.exists():
            log_error(f"Package file not found at absolute path: {package_file}")
            return False
        
        package_path_absolute = str(package_file)
        
        if is_standalone:
            # In standalone mode, use direct ilha command
            log_info("Restoring volumes using ilha command...")
            try:
                result = subprocess.run(
                    ["ilha", "volumes", "restore", branch_name, package_path_absolute],
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=600
                )
                if result.returncode == 0:
                    log_success("Volumes restored successfully")
                    return True
                else:
                    log_error(f"Volume restoration failed: {result.stderr}")
                    return False
            except Exception as e:
                log_error(f"Failed to restore volumes: {e}")
                return False
        else:
            # Normal mode: use ilha command
            log_info("Restoring volumes using ilha command...")
            try:
                result = subprocess.run(
                    ["ilha", "volumes", "restore", branch_name, package_path_absolute],
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=600
                )
                if result.returncode == 0:
                    log_success("Volumes restored successfully")
                    return True
                else:
                    log_error(f"Volume restoration failed: {result.stderr}")
                    return False
            except Exception as e:
                log_error(f"Failed to restore volumes: {e}")
                return False
    
    def _build_images_if_needed(self, branch_name: str, project_root: Path, build: bool) -> bool:
        """Build Docker images if requested.
        
        Args:
            branch_name: Branch name
            project_root: Project root directory
            build: Whether to build images
            
        Returns:
            True if build succeeded or not needed, False on failure
        """
        if not build:
            return True
        
        log_info("Rebuilding Docker images for branch: {branch_name}")
        
        # Clear BuildKit cache
        log_info("Clearing Docker BuildKit cache...")
        subprocess.run(
            ["docker", "builder", "prune", "-f", "--filter", "type=exec.cachemount"],
            capture_output=True,
            check=False
        )
        
        # Try build with cache first
        log_info("Building Docker images...")
        result = subprocess.run(
            ["ilha", branch_name, "build"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=600
        )
        
        if result.returncode == 0:
            log_success("Images rebuilt successfully")
            return True
        
        # Fallback to --no-cache
        log_warning("Build failed, retrying with --no-cache flag...")
        result = subprocess.run(
            ["ilha", branch_name, "build", "--no-cache"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=600
        )
        
        if result.returncode == 0:
            log_success("Images rebuilt successfully (without cache)")
            return True
        else:
            log_error("Failed to rebuild images even with --no-cache")
            return False
    
    def _start_services(self, branch_name: str, project_root: Path, 
                       is_standalone: bool) -> bool:
        """Start services for the worktree.
        
        Args:
            branch_name: Branch name
            project_root: Project root directory
            is_standalone: Whether in standalone mode
            
        Returns:
            True if services started successfully, False otherwise
        """
        log_info(f"Bringing up worktree environment for branch: {branch_name}")
        log_info("This may take a few minutes if containers need to be pulled or built...")
        
        if is_standalone:
            # Standalone mode: use docker compose directly
            worktree_path = project_root / "worktrees" / branch_name
            compose_file = worktree_path / ".ilha" / "docker-compose.worktree.yml"
            
            if not compose_file.exists():
                log_error(f"Docker Compose file not found: {compose_file}")
                return False
            
            from ..utils.env_loader import load_env_file
            from ..utils.path_utils import get_env_compose_file_path
            import os
            
            # Read COMPOSE_PROJECT_NAME from env.ilha (already set correctly during import)
            env_file_path = get_env_compose_file_path(worktree_path)
            env_vars = load_env_file(env_file_path)
            compose_project_name = env_vars.get("COMPOSE_PROJECT_NAME")
            
            # Fallback: reconstruct if not found in env.ilha using self.project_root
            if not compose_project_name:
                from ..config.settings import ilha_DIR, sanitize_project_name
                import yaml
                
                # Get project name from self.project_root's config.yml
                project_name = None
                config_path = self.project_root / ilha_DIR / "config.yml"
                if config_path.exists():
                    try:
                        with open(config_path) as f:
                            config = yaml.safe_load(f) or {}
                            project_name = config.get("project_name")
                    except Exception:
                        pass
                
                # Fallback to project root directory name if config doesn't have project_name
                if not project_name:
                    project_name = self.project_root.name
                
                project_name = sanitize_project_name(project_name)
                compose_project_name = f"{project_name}-{branch_name}"
            
            # Set environment variables
            os.environ["COMPOSE_PROJECT_NAME"] = compose_project_name
            os.environ["PROJECT_ROOT"] = str(worktree_path)
            
            # Stop existing containers first
            subprocess.run(
                ["docker", "compose", "-f", str(compose_file.relative_to(worktree_path)),
                 "--env-file", ".ilha/env.ilha", "-p", compose_project_name, "down"],
                cwd=worktree_path,
                capture_output=True,
                check=False
            )
            
            # Start containers
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file.relative_to(worktree_path)),
                 "--env-file", ".ilha/env.ilha", "-p", compose_project_name,
                 "up", "-d", "--force-recreate", "--remove-orphans"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=600
            )
            
            if result.returncode == 0:
                log_success("Services started successfully")
                return True
            else:
                log_error(f"Failed to start services: {result.stderr}")
                return False
        else:
            # Normal mode: use ilha command
            # Stop existing containers first
            subprocess.run(
                ["ilha", branch_name, "down"],
                cwd=project_root,
                capture_output=True,
                check=False
            )
            
            # Start containers
            result = subprocess.run(
                ["ilha", branch_name, "up", "-d"],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=600
            )
            
            if result.returncode == 0:
                log_success("Services started successfully")
                return True
            else:
                log_error(f"Failed to start services: {result.stderr}")
                return False
    
    def _verify_containers(self, branch_name: str) -> Dict[str, any]:
        """Verify containers are running.
        
        Args:
            branch_name: Branch name
            
        Returns:
            Dictionary with container status
        """
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={branch_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True
        )
        running = [c for c in result.stdout.strip().split('\n') if c]
        
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={branch_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True
        )
        total = [c for c in result.stdout.strip().split('\n') if c]
        
        # Check for exited containers
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={branch_name}", 
             "--filter", "status=exited", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True
        )
        exited = [c for c in result.stdout.strip().split('\n') if c]
        
        return {
            "running": len(running),
            "total": len(total),
            "exited": len(exited),
            "container_names": running
        }
    
    def import_and_start(self, package_path: str, branch_name: str,
                        domain: Optional[str] = None, ip: Optional[str] = None,
                        build: bool = False, debug: bool = False, start: bool = True) -> Dict[str, any]:
        """Import package and optionally start services.
        
        Args:
            package_path: Path to package file
            branch_name: Branch name
            domain: Optional domain for Caddy routing
            ip: Optional IP for HTTP-only routing
            build: Whether to rebuild Docker images
            start: Whether to start services after import
            
        Returns:
            Dictionary with import and startup results
        """
        log_info("=== Starting remote import process ===")
        log_info(f"Package file: {package_path}")
        log_info(f"Branch name: {branch_name}")
        
        # Ensure git identity
        self._ensure_git_identity()
        
        # Verify package file exists
        package_file = Path(package_path)
        if not package_file.exists():
            return {
                "success": False,
                "error": f"Package file not found: {package_path}"
            }
        
        log_success(f"Package file found: {package_path}")
        
        # Detect existing project or standalone mode
        existing_project = self._find_existing_project()
        is_standalone = existing_project is None
        
        if is_standalone:
            log_info("No existing project found, using standalone mode")
            project_root = Path("/root")
        else:
            log_info(f"Found existing project at: {existing_project}")
            project_root = existing_project
            
            # Clean up existing worktree if it exists
            if not self._cleanup_existing_worktree(branch_name, project_root):
                return {
                    "success": False,
                    "error": f"Failed to cleanup existing worktree for branch '{branch_name}'"
                }
        
        # Import package
        log_info(f"Importing package in {'standalone' if is_standalone else 'normal'} mode...")
        try:
            import_result = self.package_manager.import_package(
                package_path=package_file,
                target_branch=branch_name,
                domain=domain,
                ip=ip,
                debug=debug,
                standalone=is_standalone,
                non_interactive=True
            )
            
            if not import_result.get("success"):
                return {
                    "success": False,
                    "error": f"Import failed: {import_result.get('error', 'Unknown error')}"
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Import failed with exception: {e}"
            }
        
        # Find project root after import (may have changed in standalone mode)
        project_root = self._find_existing_project() or Path("/root")
        log_success(f"Import completed, project located at: {project_root}")
        
        # Start Caddy proxy
        log_info("Starting global Caddy proxy...")
        if not self.caddy_manager.start_global_caddy():
            log_warning("Failed to start proxy, but continuing...")
        
        # Give Caddy a moment to initialize
        time.sleep(2)
        
        # Verify and restore volumes if needed
        if not self._restore_volumes_if_needed(package_path, branch_name, project_root, is_standalone):
            log_warning("Volume restoration had issues, but continuing...")
        
        # Build images if requested
        if not self._build_images_if_needed(branch_name, project_root, build):
            return {
                "success": False,
                "error": "Failed to build Docker images"
            }
        
        # Start services if requested
        if start:
            if not self._start_services(branch_name, project_root, is_standalone):
                return {
                    "success": False,
                    "error": "Failed to start services"
                }
            
            # Verify containers
            time.sleep(5)  # Wait for containers to initialize
            container_status = self._verify_containers(branch_name)
            
            log_info(f"Container status: {container_status['running']} running out of {container_status['total']} total")
            
            if container_status['exited'] > 0:
                log_warning(f"Found {container_status['exited']} exited container(s)")
                # Show logs for exited containers
                for container in container_status.get('exited_containers', []):
                    result = subprocess.run(
                        ["docker", "logs", "--tail", "50", container],
                        capture_output=True,
                        text=True,
                        check=False
                    )
                    if result.stdout:
                        for line in result.stdout.split('\n')[:20]:
                            log_error(f"  {line}")
            
            if container_status['running'] == 0:
                return {
                    "success": False,
                    "error": "No containers are running - deployment may have failed"
                }
        
        log_success("=== Remote import process completed ===")
        
        return {
            "success": True,
            "project_root": str(project_root),
            "is_standalone": is_standalone,
            "containers": container_status if start else None
        }

