"""
Package management for ilha.

This module provides comprehensive environment export/import functionality
to enable sharing complete isolated development environments between team members.
"""

import json
import shutil
import subprocess
import tarfile
import tempfile
import yaml
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config.settings import get_project_root, get_project_name
from ..core.docker_manager import DockerManager
from ..core.git_manager import GitManager
from ..core.environment_manager import EnvironmentManager
from ..core.worktree_orchestrator import WorktreeOrchestrator
from ..core.droplet_manager import DropletInfo
from ..utils.logging import log_info, log_success, log_warning, log_error
from ..utils.checksum import calculate_file_checksum, verify_file_checksum
from ..utils.confirmation import confirm_use_existing_worktree
from ..utils.container_selector import resolve_service_dependencies
from ..core.worker_configurator import WorkerConfigurator


class PackageManager:
    """Manages environment package export/import operations."""

    def __init__(self, project_root: Optional[Path] = None):
        """Initialize package manager.

        Args:
            project_root: Project root directory. If None, uses get_project_root().
        """
        self.project_root = project_root or get_project_root()
        self.docker_manager = DockerManager(project_root=self.project_root)
        self.git_manager = GitManager(project_root=self.project_root, validate=False)
        self.env_manager = EnvironmentManager(project_root=self.project_root)
        self.orchestrator = WorktreeOrchestrator(project_root=self.project_root)
        self._worker_config = WorkerConfigurator()
    
    def _apply_domain_or_ip_override(self, worktree_path: Path, domain: Optional[str], 
                                      ip: Optional[str], debug: bool = False,
                                      env_manager: EnvironmentManager = None) -> bool:
        """Apply domain or IP override to worktree configuration files.
        
        This is a DRY helper method used by both _normal_import() and _standalone_import().
        
        Args:
            worktree_path: Path to worktree directory
            domain: Optional domain override (subdomain.domain.tld)
            ip: Optional IP override
            env_manager: Optional EnvironmentManager instance (uses self.env_manager if not provided)
            
        Returns:
            True if successful or no override needed, False on failure
        """
        if not domain and not ip:
            return True  # No override needed
        
        if not worktree_path.exists():
            log_error(f"Worktree path not found: {worktree_path}. Cannot apply domain/IP overrides.")
            return False
        
        manager = env_manager or self.env_manager
        
        if domain:
            log_info(f"Applying domain overrides to worktree: {domain}")
            log_info(f"Worktree path: {worktree_path}")
            success = manager.apply_domain_overrides(worktree_path, domain, debug=debug)
            if not success:
                log_error(f"Failed to apply domain overrides to worktree at {worktree_path}")
                log_error(f"This may cause containers to use localhost domain instead of {domain}")
                log_error(f"Manual intervention required: edit {worktree_path}/.ilha/docker-compose.overlay.yml")
                log_error(f"and {worktree_path}/.ilha/env.ilha to set domain to {domain}")
                return False
            
            # Verify domain configuration was applied correctly
            log_info("Verifying domain configuration...")
            verification = manager.verify_domain_configuration(worktree_path, domain)
            if verification.get("compose_labels") and verification.get("env_variables"):
                log_success("Domain configuration verified: compose labels and env variables updated correctly")
            else:
                if not verification.get("compose_labels"):
                    log_warning("Domain configuration verification: compose labels not found or incorrect")
                if not verification.get("env_variables"):
                    log_warning("Domain configuration verification: env variables not found or incorrect")
        elif ip:
            log_info(f"Applying IP overrides to worktree: {ip}")
            log_info(f"Worktree path: {worktree_path}")
            success = manager.apply_ip_overrides(worktree_path, ip, debug=debug)
            if not success:
                log_error(f"Failed to apply IP overrides to worktree at {worktree_path}")
                log_error(f"This may cause containers to use localhost domain instead of {ip}")
                log_error(f"Manual intervention required: edit {worktree_path}/.ilha/docker-compose.overlay.yml")
                log_error(f"and {worktree_path}/.ilha/env.ilha to set IP to {ip}")
                return False
        
        return True
    
    def _is_in_existing_project(self) -> bool:
        """Check if we're in an existing ilha project.
        
        Returns:
            True if in existing ilha project with git repo, False otherwise
        """
        from ..utils.validation import validate_git_repository
        
        # Check for .ilha directory with config
        ilha_config = self.project_root / ".ilha" / "config.yml"
        if not ilha_config.exists():
            return False
        
        # Check for git repository
        if not validate_git_repository(self.project_root):
            return False
        
        return True
    
    def export_package(self, branch_name: str, output_dir: Path, 
                      include_code: bool = False, compressed: bool = True, skip_volumes: bool = False,
                      container_filter: Optional[List[Dict[str, str]]] = None,
                      exclude_deps: Optional[List[str]] = None,
                      droplet_info: Optional[DropletInfo] = None,
                      central_droplet_info: Optional[DropletInfo] = None) -> Dict[str, Any]:
        """Export worktree to package - orchestrates existing managers.
        
        Args:
            branch_name: Name of the branch to export
            output_dir: Directory to save the package
            include_code: Whether to include project tar archive
            compressed: Whether to compress the final package
            skip_volumes: Whether to skip volume backup (fallback option)
            container_filter: Optional list of dicts with 'worktree' and 'container' keys
                             to filter which containers/volumes to export
            exclude_deps: Optional list of service names to exclude from dependency resolution
            
        Returns:
            Dictionary with success status, package path, and metadata
        """
        try:
            # 1. Validate worktree exists
            if not self.git_manager.validate_worktree_exists(branch_name):
                return {
                    "success": False,
                    "error": f"Worktree for branch '{branch_name}' does not exist"
                }
            
            worktree_path = self.git_manager.find_worktree_path(branch_name)
            if not worktree_path:
                return {
                    "success": False,
                    "error": f"Could not find worktree directory for branch '{branch_name}'"
                }
            
            # 2. Create output directory
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # 3. Create temporary package directory
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            package_name = f"{branch_name}_{timestamp}.ilha-package"
            temp_package_dir = output_dir / package_name
            temp_package_dir.mkdir(exist_ok=True)
            
            # 4. Backup volumes using existing DockerManager (unless skipped)
            backup_file = None
            if not skip_volumes:
                volumes_backup_path = temp_package_dir / "volumes" / f"backup_{branch_name}.tar"
                volumes_backup_path.parent.mkdir(exist_ok=True)
                
                # If container_filter is provided, only backup volumes for selected containers
                if container_filter:
                    log_info(f"Backing up volumes for selected containers in {branch_name}...")
                    # Collect volumes for all selected containers
                    selected_volumes = set()
                    for selection in container_filter:
                        if selection.get('worktree') == branch_name:
                            container_name = selection.get('container')
                            volumes = self.docker_manager.get_volumes_for_service(branch_name, container_name)
                            selected_volumes.update(volumes)
                            log_info(f"  Container '{container_name}': {len(volumes)} volume(s)")
                    
                    if selected_volumes:
                        log_info(f"Backing up {len(selected_volumes)} selected volume(s)...")
                        backup_file = self._backup_selected_volumes(
                            branch_name, 
                            list(selected_volumes), 
                            temp_package_dir / "volumes"
                        )
                        # If backup was attempted but failed, return error
                        if not backup_file:
                            return {
                                "success": False,
                                "error": "Failed to backup selected volumes"
                            }
                    else:
                        log_warning("No volumes found for selected containers")
                        backup_file = None
                        # No volumes found is valid - continue with export
                else:
                    log_info(f"Backing up all volumes for {branch_name}...")
                    backup_file = self.docker_manager.backup_volumes(branch_name, temp_package_dir / "volumes")
                    # If backup was attempted but failed, return error
                    if not backup_file:
                        return {
                            "success": False,
                            "error": "Failed to backup volumes"
                        }
            else:
                log_warning("Skipping volume backup as requested")
            
            # 5. Create project archive if requested (includes .ilha directories)
            code_archive_path = None
            if include_code:
                code_archive_path = temp_package_dir / "code" / f"{branch_name}.tar.gz"
                code_archive_path.parent.mkdir(exist_ok=True)
                
                log_info(f"Creating project archive for {branch_name}...")
                if not self._create_project_archive(branch_name, worktree_path, code_archive_path):
                    return {
                        "success": False,
                        "error": "Failed to create project archive"
                    }
            
            # 6. Generate metadata with checksums
            metadata = self._generate_metadata(
                branch_name, temp_package_dir, include_code, skip_volumes, container_filter,
                exclude_deps=exclude_deps, droplet_info=droplet_info, central_droplet_info=central_droplet_info
            )
            
            # 7. Compress package if requested
            final_package_path = temp_package_dir
            if compressed:
                final_package_path = output_dir / f"{package_name}.tar.gz"
                log_info(f"Compressing package to {final_package_path}...")
                if not self._compress_package(temp_package_dir, final_package_path):
                    return {
                        "success": False,
                        "error": "Failed to compress package"
                    }
                # Clean up temp directory
                shutil.rmtree(temp_package_dir)
            
            log_success(f"Package exported successfully: {final_package_path}")
            return {
                "success": True,
                "package_path": str(final_package_path),
                "metadata": metadata
            }
            
        except Exception as e:
            log_error(f"Error exporting package: {e}")
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }
    
    def import_package(self, package_path: Path, target_branch: Optional[str] = None,
                      restore_data: bool = True, standalone: Optional[bool] = None,
                      target_directory: Optional[Path] = None, domain: Optional[str] = None,
                      ip: Optional[str] = None, debug: bool = False, non_interactive: bool = False) -> Dict[str, Any]:
        """Import package with automatic standalone detection.
        
        Args:
            package_path: Path to the package file
            target_branch: Target branch name (for normal mode, defaults to package branch)
            restore_data: Whether to restore volume data
            standalone: Force standalone mode (None = auto-detect)
            target_directory: Target directory for standalone import
            domain: Optional domain override (subdomain.domain.tld) for production/staging
            
        Returns:
            Dictionary with success status and import info
        """
        try:
            # Auto-detect standalone mode if not explicitly set
            if standalone is None:
                standalone = not self._is_in_existing_project()
                if standalone:
                    log_info("No existing ilha project detected - using standalone mode")
            
            # Validate mutually exclusive domain/ip
            if domain and ip:
                return {
                    "success": False,
                    "error": "Options --domain and --ip are mutually exclusive"
                }

            # Route to appropriate import method
            if standalone:
                return self._standalone_import(package_path, target_directory, restore_data, domain, ip, debug, non_interactive)
            else:
                return self._normal_import(package_path, target_branch, restore_data, domain, ip, debug)
                
        except Exception as e:
            log_error(f"Error importing package: {e}")
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }
    
    def _extract_and_validate_package(self, package_path: Path) -> Tuple[Path, Path, Dict[str, Any]]:
        """Extract package and validate integrity.
        
        Args:
            package_path: Path to the package file
            
        Returns:
            Tuple of (temp_extract_dir, package_dir, metadata)
            
        Raises:
            Exception: If validation fails
        """
        # Validate package file
        if not package_path.exists():
            raise FileNotFoundError(f"Package file not found: {package_path}")
        
        # Create temporary extraction directory
        temp_extract_dir = Path(tempfile.mkdtemp())
        
        # Check if it's a compressed tar.gz file
        is_compressed = package_path.name.endswith('.tar.gz')
        
        if is_compressed:
            # Extract compressed package
            log_info("Extracting compressed package...")
            with tarfile.open(package_path, 'r:gz') as tar:
                tar.extractall(temp_extract_dir)
        else:
            # Copy uncompressed package
            log_info("Copying uncompressed package...")
            shutil.copytree(package_path, temp_extract_dir / package_path.name)
            temp_extract_dir = temp_extract_dir / package_path.name
        
        # Verify package integrity - look for the package directory
        package_dirs = [d for d in temp_extract_dir.iterdir() if d.is_dir() and d.name.endswith('.ilha-package')]
        if not package_dirs:
            raise ValueError("Invalid package: package directory not found")
        
        package_dir = package_dirs[0]
        metadata_path = package_dir / "metadata.json"
        if not metadata_path.exists():
            raise ValueError("Invalid package: metadata.json not found")
        
        with open(metadata_path) as f:
            metadata = json.load(f)
        
        # Verify checksums
        if not self._verify_package_checksums(package_dir, metadata):
            raise ValueError("Package integrity check failed")
        
        return (temp_extract_dir, package_dir, metadata)
    
    def _normal_import(self, package_path: Path, target_branch: Optional[str] = None,
                      restore_data: bool = True, domain: Optional[str] = None,
                      ip: Optional[str] = None, debug: bool = False) -> Dict[str, Any]:
        """Import package to existing project as new worktree.
        
        Args:
            package_path: Path to the package file
            target_branch: Target branch name (defaults to package branch name)
            restore_data: Whether to restore volume data
            domain: Optional domain override (subdomain.domain.tld) for production/staging
            
        Returns:
            Dictionary with success status and worktree info
        """
        # Validate git repository for normal import
        from ..utils.validation import validate_git_repository
        if not validate_git_repository(self.project_root):
            return {
                "success": False,
                "error": "Normal import requires a git repository. Use --standalone for new projects."
            }
        
        temp_extract_dir = None
        try:
            # Extract and validate package
            temp_extract_dir, package_dir, metadata = self._extract_and_validate_package(package_path)
            
            # Determine target branch
            if not target_branch:
                target_branch = metadata.get("branch_name")
                if not target_branch:
                    return {
                        "success": False,
                        "error": "Could not determine target branch from package"
                    }
            
            # Check if target branch already exists
            if self.git_manager.validate_worktree_exists(target_branch):
                # Check if volumes exist for this branch
                from ..config.settings import get_volume_names
                from ..utils.validation import validate_volume_exists
                
                volume_names = get_volume_names(target_branch)
                existing_volumes = [name for name in volume_names.values() 
                                  if validate_volume_exists(name)]
                
                if existing_volumes and restore_data:
                    if not confirm_use_existing_worktree(target_branch):
                        return {
                            "success": False,
                            "error": "Import cancelled by user"
                        }
            
            # Create worktree using orchestrator
            log_info(f"Creating worktree for branch '{target_branch}'...")
            create_result = self.orchestrator.create_worktree(target_branch)
            if not create_result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to create worktree: {create_result.get('error')}"
                }
            
            worktree_path = self.git_manager.find_worktree_path(target_branch)
            if not worktree_path:
                return {
                    "success": False,
                    "error": "Worktree created but path not found"
                }
            
            # Restore environment files
            # NOTE: This may overwrite domain/IP settings, so we'll re-apply them after restore
            preserve_domain = domain is not None or ip is not None
            env_success = self._restore_environment_files(package_dir, worktree_path, preserve_domain_settings=preserve_domain)
            if not env_success:
                log_warning("Failed to restore some environment files")
            
            # Configure worker environment if metadata indicates worker deployment
            if metadata.get('vpc_deployment', {}).get('is_worker'):
                log_info("Detected worker deployment, configuring environment variables...")
                self._worker_config.configure_worker_environment(worktree_path, metadata)
            
            # Apply domain/ip overrides if provided (DRY: uses shared helper method)
            self._apply_domain_or_ip_override(worktree_path, domain, ip, debug=debug)
            
            # Restore volumes if requested
            # restore_volumes() handles stopping containers safely before restore
            if restore_data:
                volumes_backup = package_dir / "volumes" / f"backup_{metadata['branch_name']}.tar"
                if volumes_backup.exists():
                    log_info(f"Restoring volumes for {target_branch}...")
                    if not self.docker_manager.restore_volumes(target_branch, volumes_backup):
                        log_warning("Failed to restore volumes")
                else:
                    log_warning("Volume backup not found in package")
            
            # Extract code archive if present
            # New format: tar contains worktrees/{branch}/ - extract those contents to worktree_path
            code_archive = package_dir / "code" / f"{metadata['branch_name']}.tar.gz"
            if code_archive.exists():
                log_info(f"Extracting code archive to {worktree_path}...")
                try:
                    with tarfile.open(code_archive, 'r:gz') as tar:
                        # Extract only worktree contents, remapping paths
                        worktree_prefix = f"worktrees/{metadata['branch_name']}/"
                        for member in tar.getmembers():
                            if member.name.startswith(worktree_prefix):
                                # Remap path: worktrees/{branch}/foo -> foo
                                member.name = member.name[len(worktree_prefix):]
                                if member.name:  # Skip empty names (the directory itself)
                                    tar.extract(member, worktree_path)
                except Exception as e:
                    log_warning(f"Failed to extract code archive: {e}")
            
            # Get worktree info
            worktree_info = self.orchestrator.get_worktree_info(target_branch)
            
            log_success(f"Package imported successfully to branch '{target_branch}'")
            return {
                "success": True,
                "mode": "normal",
                "worktree_info": worktree_info.get("data", {}),
                "metadata": metadata
            }
            
        except Exception as e:
            log_error(f"Error in normal import: {e}")
            return {
                "success": False,
                "error": f"Normal import failed: {str(e)}"
            }
        finally:
            # Clean up temporary directory
            if temp_extract_dir and temp_extract_dir.exists():
                shutil.rmtree(temp_extract_dir, ignore_errors=True)
    
    def _standalone_import(self, package_path: Path, target_directory: Path = None,
                          restore_data: bool = True, domain: Optional[str] = None,
                          ip: Optional[str] = None, debug: bool = False, non_interactive: bool = False) -> Dict[str, Any]:
        """Import package in standalone mode - extracts complete deployment.
        
        Simply extracts code archive and environment files from package.
        No git initialization or setup_project() needed - package has everything.
        
        Args:
            package_path: Path to the package file
            target_directory: Target directory for new project
            restore_data: Whether to restore volume data
            domain: Optional domain override (subdomain.domain.tld) for production/staging
            
        Returns:
            Dictionary with success status and project info
        """
        temp_extract_dir = None
        try:
            # Extract and validate package
            temp_extract_dir, package_dir, metadata = self._extract_and_validate_package(package_path)
            
            # Check if package includes code (required for standalone)
            if not metadata.get("include_code"):
                return {
                    "success": False,
                    "error": "Standalone import requires package with code (export with --include-code)"
                }
            
            branch_name = metadata.get("branch_name")
            if not branch_name:
                return {
                    "success": False,
                    "error": "Branch name missing from package metadata"
                }
            
            # Determine target directory
            if not target_directory:
                project_name = metadata.get("project_name", "ilha-project")
                target_directory = Path.cwd() / project_name
            
            target_directory = Path(target_directory).resolve()
            
            # Create target directory
            if target_directory.exists():
                return {
                    "success": False,
                    "error": f"Target directory already exists: {target_directory}"
                }
            
            target_directory.mkdir(parents=True, exist_ok=True)
            
            # Extract project archive to target directory
            # Archive contains complete structure: .ilha/, worktrees/{branch}/
            code_archive = package_dir / "code" / f"{branch_name}.tar.gz"
            if code_archive.exists():
                log_info(f"Extracting project archive to {target_directory}...")
                with tarfile.open(code_archive, 'r:gz') as tar:
                    tar.extractall(target_directory)
                log_success(f"Extracted project with worktree: worktrees/{branch_name}/")
            else:
                return {
                    "success": False,
                    "error": f"Code archive not found in package: {code_archive}"
                }
            
            # Verify worktree was extracted
            worktree_path = target_directory / "worktrees" / branch_name
            if not worktree_path.exists():
                return {
                    "success": False,
                    "error": f"Worktree not found in archive: worktrees/{branch_name}"
                }
            
            # Initialize git repository for standalone deployment
            # This removes invalid .git worktree references and creates a fresh repo
            # so that ilha commands (which require git) will work on the server
            if not self._initialize_git_for_standalone(target_directory):
                log_warning("Failed to initialize git repository, ilha commands may not work")
            
            # Fix env_file paths in docker-compose.overlay.yml for standalone deployment
            # The compose file may reference ${PROJECT_ROOT}/.env but on standalone deployments
            # the .env is in the worktree directory, so we use relative paths
            if not self._fix_env_file_paths_for_standalone(worktree_path):
                log_warning("Failed to fix env_file paths, docker compose may fail to find .env")
            
            # Configure worker environment if metadata indicates worker deployment
            if metadata.get('vpc_deployment', {}).get('is_worker'):
                log_info("Detected worker deployment, configuring environment variables...")
                self._worker_config.configure_worker_environment(worktree_path, metadata)
            
            # Fix PROJECT_ROOT and build context for standalone deployment
            # The .ilha/ was extracted with source machine paths, now update for target server.
            # PROJECT_ROOT must be the worktree path so ${PROJECT_ROOT}/postgres-config etc.
            # resolve to the extracted worktree (not project root, where they may not exist).
            env_manager = EnvironmentManager(project_root=target_directory)
            
            if not env_manager.update_project_root(worktree_path, worktree_path):
                log_warning("Failed to update PROJECT_ROOT, but continuing...")
            
            # Fix build context and volume mounts to use worktree path instead of PROJECT_ROOT
            if not env_manager.fix_standalone_paths(worktree_path, target_directory):
                log_warning("Failed to fix standalone paths, but continuing...")
            
            # Apply domain/IP overrides to worktree's .ilha/ if provided
            # The .ilha/ was extracted with localhost settings, now update for deployment
            self._apply_domain_or_ip_override(worktree_path, domain, ip, debug=debug, env_manager=env_manager)
            
            # Restore volumes if requested
            # IMPORTANT: Stop any running containers first to ensure volumes can be restored safely.
            # Volume restoration works at the file level (extracting tar.gz directly into volumes),
            # but we must stop containers first because:
            # 1. PostgreSQL locks database files while running - overwriting them causes corruption
            # 2. Database containers expect clean shutdowns - file-level restoration requires unmounted volumes
            # 3. The restoration process extracts files directly into the volume (no database commands needed)
            if restore_data:
                if branch_name:
                    volumes_backup = package_dir / "volumes" / f"backup_{branch_name}.tar"
                    if volumes_backup.exists():
                        log_info(f"Restoring volumes...")
                        # Ensure containers are stopped before restoration (DRY: use shared function)
                        from ..core.docker_manager import DockerManager
                        docker_manager = DockerManager(project_root=target_directory)
                        # restore_volumes() handles stopping containers safely before restore
                        docker_manager.restore_volumes(branch_name, volumes_backup)
            
            log_success(f"Standalone import completed: {target_directory}")
            return {
                "success": True,
                "mode": "standalone",
                "project_directory": str(target_directory),
                "branch_name": metadata.get("branch_name"),
                "metadata": metadata
            }
            
        except Exception as e:
            log_error(f"Error in standalone import: {e}")
            return {
                "success": False,
                "error": f"Standalone import failed: {str(e)}"
            }
        finally:
            # Clean up temporary directory
            if temp_extract_dir and temp_extract_dir.exists():
                shutil.rmtree(temp_extract_dir, ignore_errors=True)
    
    def validate_package(self, package_path: Path) -> Dict[str, Any]:
        """Validate package integrity.
        
        Args:
            package_path: Path to the package file
            
        Returns:
            Dictionary with validation results
        """
        try:
            if not package_path.exists():
                return {
                    "success": False,
                    "error": "Package file not found"
                }
            
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_extract_dir = Path(temp_dir)
                
                # Extract package
                if package_path.suffix == '.tar.gz':
                    with tarfile.open(package_path, 'r:gz') as tar:
                        tar.extractall(temp_extract_dir)
                else:
                    shutil.copytree(package_path, temp_extract_dir / package_path.name)
                    temp_extract_dir = temp_extract_dir / package_path.name
                
                # Check metadata - look for the package directory
                package_dirs = [d for d in temp_extract_dir.iterdir() if d.is_dir() and d.name.endswith('.ilha-package')]
                if not package_dirs:
                    return {
                        "success": False,
                        "error": "Invalid package: package directory not found"
                    }
                
                package_dir = package_dirs[0]
                metadata_path = package_dir / "metadata.json"
                if not metadata_path.exists():
                    return {
                        "success": False,
                        "error": "Invalid package: metadata.json not found"
                    }
                
                with open(metadata_path) as f:
                    metadata = json.load(f)
                
                # Verify checksums
                checksum_valid = self._verify_package_checksums(package_dir, metadata)
                
                return {
                    "success": True,
                    "valid": checksum_valid,
                    "metadata": metadata,
                    "checksum_valid": checksum_valid
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Validation error: {str(e)}"
            }
    
    def list_packages(self, package_dir: Path) -> List[Dict[str, Any]]:
        """List available packages in directory.
        
        Args:
            package_dir: Directory to search for packages
            
        Returns:
            List of package information dictionaries
        """
        packages = []
        
        if not package_dir.exists():
            return packages
        
        for item in package_dir.iterdir():
            if item.is_file() and ('.tar.gz' in item.name or item.name.endswith('.ilha-package')):
                # Validate package
                validation = self.validate_package(item)
                if validation.get("success"):
                    packages.append({
                        "name": item.name,
                        "path": str(item),
                        "size": item.stat().st_size,
                        "metadata": validation.get("metadata", {}),
                        "valid": validation.get("valid", False)
                    })
        
        return sorted(packages, key=lambda x: x["name"])
    
    def _create_project_archive(self, branch_name: str, worktree_path: Path, output_path: Path) -> bool:
        """Create tar archive of project including .ilha directories.
        
        Creates a complete tar of the project structure preserving the fractal
        .ilha configuration. Includes:
        - Project root .ilha/
        - worktrees/{branch}/ with its .ilha/
        - All project files
        
        Args:
            branch_name: Name of the branch/worktree
            worktree_path: Path to the worktree directory
            output_path: Path where the archive should be created
            
        Returns:
            True if archive was created successfully, False otherwise
        """
        try:
            # Ensure output directory exists
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Get project root (parent of worktrees directory)
            # worktree_path is like /project/worktrees/{branch}
            project_root = worktree_path.parent.parent
            
            log_info(f"Creating project archive from {project_root}")
            log_info(f"Including worktree: worktrees/{branch_name}")
            
            # Create tar archive of project
            # Include: .ilha/, worktrees/{branch}/, and key project files
            with tarfile.open(output_path, 'w:gz') as tar:
                # Add project root .ilha/
                ilha_dir = project_root / ".ilha"
                if ilha_dir.exists():
                    tar.add(ilha_dir, arcname=".ilha")
                    log_info("  Added: .ilha/")
                
                # Add the worktree directory (includes its .ilha/)
                worktrees_dir = project_root / "worktrees"
                worktree_subdir = worktrees_dir / branch_name
                if worktree_subdir.exists():
                    tar.add(worktree_subdir, arcname=f"worktrees/{branch_name}")
                    log_info(f"  Added: worktrees/{branch_name}/")
                
                # Add key project files from root (if they exist)
                for filename in ["docker-compose.yml", "docker-compose.yaml", 
                                "Dockerfile", ".env.example", "requirements.txt",
                                "package.json", "Makefile"]:
                    filepath = project_root / filename
                    if filepath.exists():
                        tar.add(filepath, arcname=filename)
                        log_info(f"  Added: {filename}")
            
            log_success(f"Created project archive: {output_path}")
            return True
            
        except Exception as e:
            log_error(f"Failed to create project archive: {e}")
            return False
    
    def _copy_environment_files(self, worktree_path: Path, package_dir: Path, 
                                container_filter: Optional[List[Dict[str, str]]] = None,
                                exclude_deps: Optional[List[str]] = None,
                                droplet_info: Optional[DropletInfo] = None) -> bool:
        """Copy environment files to package directory.
        
        Args:
            worktree_path: Path to worktree directory
            package_dir: Path to package directory
            container_filter: Optional list of dicts with 'worktree' and 'container' keys
                             to filter which services to include in compose file
            exclude_deps: Optional list of service names to exclude from dependency resolution
            droplet_info: Optional droplet info containing private IP for VPC port bindings
        """
        try:
            env_dir = package_dir / "environment"
            env_dir.mkdir(exist_ok=True)
            
            # Copy .env file
            env_file = worktree_path / ".env"
            if env_file.exists():
                shutil.copy2(env_file, env_dir / ".env")
            
            # Copy .ilha directory
            ilha_dir = worktree_path / ".ilha"
            if ilha_dir.exists():
                shutil.copytree(ilha_dir, env_dir / ".ilha")
            
            # Copy docker-compose.ilha.yml
            compose_file = worktree_path / "docker-compose.ilha.yml"
            if compose_file.exists():
                shutil.copy2(compose_file, env_dir / "docker-compose.ilha.yml")
            
            # Extract private IP from droplet info if available
            private_ip = None
            if droplet_info:
                private_ip = getattr(droplet_info, 'private_ip_address', None)
            
            # If container_filter is provided, create filtered compose file
            if container_filter:
                worktree_compose_file = worktree_path / ".ilha" / "docker-compose.overlay.yml"
                if worktree_compose_file.exists():
                    filtered_compose = self._worker_config.filter_compose_services(
                        worktree_compose_file, container_filter, worktree_path, exclude_deps, private_ip
                    )
                    if filtered_compose:
                        # Remove version field if it's null (Docker Compose v2 doesn't require it)
                        from ..utils.file_utils import clean_compose_version_field
                        clean_compose_version_field(filtered_compose)
                        
                        filtered_path = env_dir / ".ilha" / "docker-compose.worktree.filtered.yml"
                        filtered_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(filtered_path, 'w') as f:
                            yaml.dump(filtered_compose, f, default_flow_style=False, sort_keys=False)
                        log_info(f"Created filtered compose file with {len(filtered_compose.get('services', {}))} service(s)")
            
            return True
            
        except Exception as e:
            log_error(f"Failed to copy environment files: {e}")
            return False
    
    def _restore_environment_files(self, package_dir: Path, worktree_path: Path, preserve_domain_settings: bool = False) -> bool:
        """Restore environment files from package to worktree.
        
        Args:
            package_dir: Directory containing package files
            worktree_path: Path to worktree directory
            preserve_domain_settings: If True, preserve compose file if it has domain/IP settings
                                      (used when domain/IP override will be applied after restore)
        """
        try:
            env_dir = package_dir / "environment"
            if not env_dir.exists():
                return False
            
            # Restore .env file
            env_file = env_dir / ".env"
            if env_file.exists():
                shutil.copy2(env_file, worktree_path / ".env")
            
            # Restore .ilha directory
            ilha_src = env_dir / ".ilha"
            ilha_dst = worktree_path / ".ilha"
            if ilha_src.exists():
                # If preserving domain settings, check if existing compose file has domain/IP labels
                existing_compose_file = ilha_dst / "docker-compose.overlay.yml"
                preserve_compose = False
                if preserve_domain_settings and existing_compose_file.exists():
                    try:
                        import yaml
                        with open(existing_compose_file) as f:
                            existing_data = yaml.safe_load(f) or {}
                        # Check if compose file has domain/IP in labels (not localhost)
                        if 'services' in existing_data:
                            for svc_config in existing_data['services'].values():
                                if 'labels' in svc_config:
                                    labels = svc_config['labels']
                                    if isinstance(labels, list):
                                        for label in labels:
                                            if isinstance(label, str) and 'caddy.proxy=' in label:
                                                if '.localhost' not in label and 'localhost' not in label.lower():
                                                    preserve_compose = True
                                                    log_info("Preserving existing compose file with domain/IP settings")
                                                    break
                                    elif isinstance(labels, dict):
                                        proxy_val = labels.get('caddy.proxy', '')
                                        if '.localhost' not in str(proxy_val) and 'localhost' not in str(proxy_val).lower():
                                            preserve_compose = True
                                            log_info("Preserving existing compose file with domain/IP settings")
                                            break
                                if preserve_compose:
                                    break
                    except Exception:
                        # If we can't read the file, don't preserve it
                        pass
                
                backup_compose_path = None
                if ilha_dst.exists():
                    # If preserving compose, backup it first
                    if preserve_compose and existing_compose_file.exists():
                        import tempfile
                        backup_compose = tempfile.NamedTemporaryFile(delete=False, suffix='.yml')
                        backup_compose_path = backup_compose.name
                        backup_compose.close()
                        shutil.copy2(existing_compose_file, backup_compose_path)
                        log_info(f"Backed up existing compose file before restore")
                    
                    shutil.rmtree(ilha_dst)
                
                shutil.copytree(ilha_src, ilha_dst)
                
                # Restore preserved compose file if we backed it up
                if preserve_compose and backup_compose_path:
                    try:
                        restored_compose_file = ilha_dst / "docker-compose.overlay.yml"
                        shutil.copy2(backup_compose_path, restored_compose_file)
                        log_info("Restored preserved compose file with domain/IP settings")
                        os.unlink(backup_compose_path)
                    except Exception as e:
                        log_warning(f"Failed to restore preserved compose file: {e}")
                        if backup_compose_path and os.path.exists(backup_compose_path):
                            os.unlink(backup_compose_path)
            
            # Check for filtered compose file and use it if available
            filtered_compose_file = ilha_dst / "docker-compose.worktree.filtered.yml"
            worktree_compose_file = ilha_dst / "docker-compose.overlay.yml"
            if filtered_compose_file.exists():
                log_info("Found filtered compose file in package, using it as worktree compose file")
                # Load filtered compose to get service count
                with open(filtered_compose_file) as f:
                    filtered_data = yaml.safe_load(f) or {}
                service_count = len(filtered_data.get('services', {}))
                # Replace the worktree compose file with the filtered one
                if worktree_compose_file.exists():
                    worktree_compose_file.unlink()
                shutil.copy2(filtered_compose_file, worktree_compose_file)
                log_info(f"Restored filtered compose file with {service_count} service(s)")
            
            # Restore docker-compose.ilha.yml
            compose_file = env_dir / "docker-compose.ilha.yml"
            if compose_file.exists():
                shutil.copy2(compose_file, worktree_path / "docker-compose.ilha.yml")
            
            return True
            
        except Exception as e:
            log_error(f"Failed to restore environment files: {e}")
            return False
    
    def _backup_selected_volumes(self, branch_name: str, volume_names: List[str], backup_dir: Path) -> Optional[Path]:
        """Backup only selected volumes to a tar file.
        
        Args:
            branch_name: Branch name for the worktree
            volume_names: List of volume names to backup
            backup_dir: Directory to save the backup
            
        Returns:
            Path to backup file if successful, None otherwise
        """
        import tempfile
        backup_file = backup_dir / f"backup_{branch_name}.tar"
        
        log_info(f"Backing up {len(volume_names)} selected volume(s) for {branch_name} to {backup_file}")
        
        # Check if worktree is running before backup
        was_running = self.docker_manager._is_worktree_running(branch_name)
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
            from ..utils.validation import validate_volume_exists
            
            # Backup each selected volume
            for volume_name in volume_names:
                if not validate_volume_exists(volume_name):
                    log_warning(f"Volume {volume_name} not found, skipping")
                    continue
                
                log_info(f"Backing up volume: {volume_name}")
                
                # Use file-level backup
                volume_backup = temp_backup_dir / f"{volume_name}.tar.gz"
                
                try:
                    subprocess.run([
                        "docker", "run", "--rm",
                        "-v", f"{volume_name}:/data",
                        "-v", f"{temp_backup_dir.absolute()}:/backup",
                        "alpine", "tar", "czf", f"/backup/{volume_name}.tar.gz", "-C", "/data", "."
                    ], check=True, capture_output=True, text=True)
                    log_success(f"Volume backup created: {volume_name}.tar.gz")
                except subprocess.CalledProcessError as e:
                    log_error(f"Failed to backup volume {volume_name}: {e}")
                    if e.stderr:
                        log_error(f"Error details: {e.stderr}")
                    continue
            
            # Create combined backup
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
    
    def _generate_metadata(self, branch_name: str, package_dir: Path, include_code: bool, 
                          skip_volumes: bool = False, container_filter: Optional[List[Dict[str, str]]] = None,
                          exclude_deps: Optional[List[str]] = None, droplet_info: Optional[DropletInfo] = None,
                          central_droplet_info: Optional[DropletInfo] = None) -> Dict[str, Any]:
        """Generate package metadata with checksums."""
        metadata = {
            "package_version": "1.0",
            "ilha_version": "0.9.4",
            "created_at": datetime.now().isoformat(),
            "branch_name": branch_name,
            "project_name": get_project_name(),
            "include_code": include_code,
            "skip_volumes": skip_volumes,
            "container_filter": container_filter if container_filter else None,
            "checksums": {}
        }
        
        # Add VPC deployment metadata if relevant
        # Only include when: droplet has VPC info OR exclude_deps is used (indicates worker deployment)
        vpc_deployment = None
        
        # Get central server private IP from central_droplet_info if available
        central_server_private_ip = None
        if central_droplet_info:
            central_server_private_ip = getattr(central_droplet_info, 'private_ip_address', None)
        
        if droplet_info:
            # Check if droplet has VPC information
            private_ip = getattr(droplet_info, 'private_ip_address', None)
            vpc_uuid = getattr(droplet_info, 'vpc_uuid', None)
            
            if private_ip or vpc_uuid or exclude_deps:
                vpc_deployment = {
                    "is_worker": bool(exclude_deps),
                    "excluded_services": exclude_deps if exclude_deps else [],
                    "central_server_private_ip": central_server_private_ip,  # Populated from central_droplet_info
                    "vpc_uuid": vpc_uuid,
                    "private_ip_address": private_ip
                }
        elif exclude_deps:
            # Worker deployment without droplet info (e.g., manual export)
            vpc_deployment = {
                "is_worker": True,
                "excluded_services": exclude_deps,
                "central_server_private_ip": central_server_private_ip,  # Populated from central_droplet_info
                "vpc_uuid": None,
                "private_ip_address": None
            }
        
        if vpc_deployment:
            metadata["vpc_deployment"] = vpc_deployment
        
        # Calculate checksums for all files
        for file_path in package_dir.rglob('*'):
            if file_path.is_file():
                try:
                    checksum = calculate_file_checksum(file_path)
                    relative_path = file_path.relative_to(package_dir)
                    metadata["checksums"][str(relative_path)] = checksum
                except Exception:
                    continue
        
        # Save metadata
        metadata_path = package_dir / "metadata.json"
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        return metadata
    
    def _verify_package_checksums(self, package_dir: Path, metadata: Dict[str, Any]) -> bool:
        """Verify package checksums."""
        checksums = metadata.get("checksums", {})
        
        for relative_path, expected_checksum in checksums.items():
            file_path = package_dir / relative_path
            if not file_path.exists():
                log_warning(f"File not found in package: {relative_path}")
                return False
            
            if not verify_file_checksum(file_path, expected_checksum):
                log_warning(f"Checksum mismatch for: {relative_path}")
                return False
        
        return True
    
    def _compress_package(self, source_dir: Path, output_path: Path) -> bool:
        """Compress package directory to tar.gz."""
        try:
            with tarfile.open(output_path, 'w:gz') as tar:
                tar.add(source_dir, arcname=source_dir.name)
            return True
        except Exception as e:
            log_error(f"Failed to compress package: {e}")
            return False

    def _initialize_git_for_standalone(self, target_directory: Path) -> bool:
        """Initialize a git repository for standalone deployment.
        
        Standalone deployments may have invalid .git worktree reference files
        that point to the source machine's paths. This method:
        1. Removes invalid .git worktree reference files
        2. Initializes a fresh git repository
        3. Creates an initial commit with all files
        
        Args:
            target_directory: Root directory of the extracted project
            
        Returns:
            True if git was initialized successfully, False otherwise
        """
        import subprocess
        
        try:
            # Find and remove invalid .git files (worktree references)
            # These are small files containing "gitdir: /path/to/original/.git/worktrees/..."
            for git_file in target_directory.rglob('.git'):
                if git_file.is_file():
                    # Read first line to check if it's a worktree reference
                    try:
                        content = git_file.read_text().strip()
                        if content.startswith('gitdir:'):
                            log_info(f"Removing invalid .git worktree reference: {git_file}")
                            git_file.unlink()
                    except Exception as e:
                        log_warning(f"Could not read .git file {git_file}: {e}")
            
            # Initialize git repository at project root
            log_info("Initializing git repository for standalone deployment...")
            result = subprocess.run(
                ["git", "init"],
                cwd=target_directory,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                log_warning(f"git init failed: {result.stderr}")
                return False
            
            # Configure git user if not already set (needed for commit)
            subprocess.run(
                ["git", "config", "user.email", "ilha@localhost"],
                cwd=target_directory,
                capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.name", "ilha"],
                cwd=target_directory,
                capture_output=True
            )
            
            # Add all files
            result = subprocess.run(
                ["git", "add", "-A"],
                cwd=target_directory,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                log_warning(f"git add failed: {result.stderr}")
                return False
            
            # Create initial commit
            result = subprocess.run(
                ["git", "commit", "-m", "Initial import from ilha package"],
                cwd=target_directory,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                # May fail if nothing to commit (empty repo)
                log_warning(f"git commit failed: {result.stderr}")
                return False
            
            # Register extracted worktree directories as git worktrees
            # This ensures 'ilha <branch> up' and other commands work correctly
            worktrees_dir = target_directory / "worktrees"
            if worktrees_dir.exists() and worktrees_dir.is_dir():
                log_info("Registering extracted worktrees with git...")
                
                for wt_dir in worktrees_dir.iterdir():
                    if not wt_dir.is_dir():
                        continue
                    
                    # Only register directories that have .ilha/ (valid worktrees)
                    if not (wt_dir / ".ilha").exists():
                        continue
                    
                    branch_name = wt_dir.name
                    log_info(f"Registering worktree: {branch_name}")
                    
                    try:
                        # 1. Create branch for this worktree
                        result = subprocess.run(
                            ["git", "branch", branch_name],
                            cwd=target_directory,
                            capture_output=True,
                            text=True
                        )
                        if result.returncode != 0:
                            log_warning(f"Failed to create branch {branch_name}: {result.stderr}")
                            continue
                        
                        # 2. Move extracted content to temp directory
                        temp_dir = Path(tempfile.mkdtemp())
                        temp_wt = temp_dir / branch_name
                        shutil.move(str(wt_dir), str(temp_wt))
                        
                        # 3. Register git worktree (this creates the directory fresh)
                        result = subprocess.run(
                            ["git", "worktree", "add", f"worktrees/{branch_name}", branch_name],
                            cwd=target_directory,
                            capture_output=True,
                            text=True
                        )
                        if result.returncode != 0:
                            log_warning(f"Failed to register worktree {branch_name}: {result.stderr}")
                            # Restore the directory since registration failed
                            shutil.move(str(temp_wt), str(wt_dir))
                            shutil.rmtree(temp_dir, ignore_errors=True)
                            continue
                        
                        # 4. Copy back all files from temp (overwriting what git worktree add created)
                        # git worktree add creates a fresh checkout, but we want the extracted files
                        for item in temp_wt.iterdir():
                            dest = wt_dir / item.name
                            if item.is_dir():
                                # Remove git's fresh checkout and replace with extracted version
                                if dest.exists():
                                    shutil.rmtree(dest)
                                shutil.copytree(item, dest)
                            else:
                                if dest.exists():
                                    dest.unlink()
                                shutil.copy2(item, dest)
                        
                        # 5. Clean up temp directory
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        
                        log_success(f"Registered worktree: {branch_name}")
                        
                    except Exception as e:
                        log_warning(f"Failed to register worktree {branch_name}: {e}")
                        # Try to restore original state
                        if temp_wt.exists():
                            if wt_dir.exists():
                                shutil.rmtree(wt_dir, ignore_errors=True)
                            shutil.move(str(temp_wt), str(wt_dir))
                        if temp_dir.exists():
                            shutil.rmtree(temp_dir, ignore_errors=True)
                        continue
            
            log_success("Git repository initialized for standalone deployment")
            return True
            
        except Exception as e:
            log_warning(f"Failed to initialize git for standalone: {e}")
            return False

    def _fix_env_file_paths_for_standalone(self, worktree_path: Path) -> bool:
        """Fix env_file paths in docker-compose.overlay.yml for standalone deployment.
        
        The docker-compose.overlay.yml may have env_file paths like:
            - ${PROJECT_ROOT}/.env
            - ${PROJECT_ROOT}/.ilha/env.ilha
        
        For standalone deployments, these should be relative to the docker-compose file
        location (.ilha/), not the worktree root:
            - ../.env (go up from .ilha/ to worktree root where .env is)
            - env.ilha (same directory as docker-compose file)
        
        Args:
            worktree_path: Path to the worktree directory
            
        Returns:
            True if paths were fixed successfully, False otherwise
        """
        import yaml
        
        try:
            compose_file = worktree_path / ".ilha" / "docker-compose.overlay.yml"
            if not compose_file.exists():
                log_warning(f"docker-compose.overlay.yml not found at {compose_file}")
                return False
            
            # Read the compose file
            with open(compose_file, 'r') as f:
                compose_data = yaml.safe_load(f)
            
            if not compose_data or 'services' not in compose_data:
                log_warning("Invalid docker-compose.overlay.yml structure")
                return False
            
            modified = False
            
            # Fix env_file paths in each service
            # Paths are relative to the docker-compose file location (.ilha/)
            for service_name, service_config in compose_data.get('services', {}).items():
                if 'env_file' in service_config:
                    env_files = service_config['env_file']
                    if isinstance(env_files, list):
                        new_env_files = []
                        for ef in env_files:
                            # Replace ${PROJECT_ROOT}/ paths with relative paths
                            # ../.env goes up from .ilha/ to worktree root
                            if '${PROJECT_ROOT}/.env' in ef:
                                new_env_files.append('../.env')
                                modified = True
                            # env.ilha is in the same directory as docker-compose
                            elif '${PROJECT_ROOT}/.ilha/env.ilha' in ef:
                                new_env_files.append('env.ilha')
                                modified = True
                            else:
                                new_env_files.append(ef)
                        service_config['env_file'] = new_env_files
                    elif isinstance(env_files, str):
                        if '${PROJECT_ROOT}/.env' in env_files:
                            service_config['env_file'] = '../.env'
                            modified = True
                        elif '${PROJECT_ROOT}/.ilha/env.ilha' in env_files:
                            service_config['env_file'] = 'env.ilha'
                            modified = True
            
            if modified:
                # Write the updated compose file
                with open(compose_file, 'w') as f:
                    yaml.dump(compose_data, f, default_flow_style=False, sort_keys=False)
                log_info("Fixed env_file paths in docker-compose.overlay.yml for standalone deployment")
            
            return True
            
        except Exception as e:
            log_warning(f"Failed to fix env_file paths: {e}")
            return False
