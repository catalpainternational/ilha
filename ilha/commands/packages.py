"""
Package management commands for ilha CLI.

This module provides CLI commands for exporting and importing environment packages
to enable sharing complete isolated development environments.
"""

from pathlib import Path
from typing import Dict, List, Optional

from ..core.package_manager import PackageManager
from ..utils.logging import log_info, log_success, log_warning, log_error, print_plain


class PackageCommands:
    """CLI interface for package management operations."""
    
    def __init__(self):
        """Initialize package commands."""
        self.package_manager = PackageManager()
    
    def export(self, branch_name: str, output_dir: Path, 
              include_code: bool, compressed: bool, skip_volumes: bool = False,
              use_staging_certificates: bool = False) -> bool:
        """Export package - CLI interface with logging.
        
        Args:
            branch_name: Name of the branch to export
            output_dir: Directory to save the package
            include_code: Whether to include git archive of code
            compressed: Whether to compress the final package
            skip_volumes: Whether to skip volume backup (fallback option)
            use_staging_certificates: Whether to set USE_STAGING_CERTIFICATES=1 in env.ilha
            
        Returns:
            True if export succeeded, False otherwise
        """
        log_info(f"Exporting package for branch: {branch_name}")
        
        if skip_volumes:
            log_warning("Skipping volume backup (--skip-volumes flag enabled)")
        
        # Set staging certificate flag if requested (before export)
        if use_staging_certificates:
            from ..core.environment_manager import EnvironmentManager
            env_manager = EnvironmentManager()
            if not env_manager.set_staging_certificate_flag(branch_name, value=True):
                log_warning("Failed to set USE_STAGING_CERTIFICATES flag, but continuing with export...")
        
        result = self.package_manager.export_package(
            branch_name, output_dir, include_code, compressed, skip_volumes
        )
        
        if result.get("success"):
            package_path = result.get("package_path")
            metadata = result.get("metadata", {})
            
            log_success(f"Package exported successfully: {package_path}")
            
            # Show package details
            print_plain(f"📦 Package: {Path(package_path).name}")
            print_plain(f"📁 Location: {package_path}")
            print_plain(f"🌿 Branch: {metadata.get('branch_name', branch_name)}")
            print_plain(f"📅 Created: {metadata.get('created_at', 'unknown')}")
            print_plain(f"💾 Size: {Path(package_path).stat().st_size / 1024 / 1024:.1f} MB")
            
            if include_code:
                print_plain("📝 Code: Included")
            else:
                print_plain("📝 Code: Not included")
            
            return True
        else:
            log_error(f"Failed to export package: {result.get('error')}")
            return False
    
    def import_package(self, package_file: Path, target_branch: str = None,
                      restore_data: bool = True, standalone: bool = None,
                      target_directory: Path = None, domain: Optional[str] = None,
                      ip: Optional[str] = None, non_interactive: bool = False) -> bool:
        """Import package - CLI interface with auto-detection.
        
        Args:
            package_file: Path to the package file
            target_branch: Target branch name (for normal mode)
            restore_data: Whether to restore volume data
            standalone: Force standalone mode (None = auto-detect)
            target_directory: Target directory for standalone import
            domain: Optional domain override (subdomain.domain.tld) for production/staging
            
        Returns:
            True if import succeeded, False otherwise
        """
        log_info(f"Importing package: {package_file}")
        if domain:
            log_info(f"Using domain override: {domain}")
        
        # Pass through to PackageManager (core logic)
        # Validate mutual exclusivity
        if domain and ip:
            log_error("Options --domain and --ip are mutually exclusive")
            return False
        
        result = self.package_manager.import_package(
            package_file, target_branch, restore_data, standalone, target_directory, domain, ip, non_interactive
        )
        
        if result.get("success"):
            mode = result.get("mode", "normal")
            
            if mode == "standalone":
                # Standalone import success
                project_dir = result.get("project_directory")
                branch_name = result.get("branch_name")
                
                log_success(f"Standalone import completed")
                print_plain(f"📁 Project Directory: {project_dir}")
                print_plain(f"🌿 Branch: {branch_name}")
                print_plain(f"💡 Next Steps:")
                print_plain(f"   cd {project_dir}")
                print_plain(f"   ilha start-proxy")
                print_plain(f"   ilha {branch_name} up -d")
            else:
                # Normal import success
                worktree_info = result.get("worktree_info", {})
                metadata = result.get("metadata", {})
                
                log_success(f"Package imported successfully to branch: {worktree_info.get('branch', 'unknown')}")
                
                # Show worktree details
                print_plain(f"🌿 Branch: {worktree_info.get('branch', 'unknown')}")
                print_plain(f"📁 Path: {worktree_info.get('worktree_path', 'unknown')}")
                print_plain(f"🌐 Domain: {worktree_info.get('domain_name', 'unknown')}")
                print_plain(f"📊 Status: {worktree_info.get('status', 'unknown')}")
                
                if worktree_info.get('status') == 'running':
                    print_plain(f"🔗 URL: http://{worktree_info.get('domain_name', 'unknown')}/")
            
            return True
        else:
            log_error(f"Failed to import package: {result.get('error')}")
            return False
    
    def list_packages(self, package_dir: Path) -> List[Dict]:
        """List available packages.
        
        Args:
            package_dir: Directory to search for packages
            
        Returns:
            List of package information dictionaries
        """
        log_info(f"Listing packages in: {package_dir}")
        
        packages = self.package_manager.list_packages(package_dir)
        
        if not packages:
            print_plain("No packages found")
            return []
        
        print_plain(f"Found {len(packages)} package(s):")
        print_plain("")
        
        for package in packages:
            metadata = package.get("metadata", {})
            size_mb = package.get("size", 0) / 1024 / 1024
            
            print_plain(f"📦 {package['name']}")
            print_plain(f"   Branch: {metadata.get('branch_name', 'unknown')}")
            print_plain(f"   Created: {metadata.get('created_at', 'unknown')}")
            print_plain(f"   Size: {size_mb:.1f} MB")
            print_plain(f"   Valid: {'✅' if package.get('valid') else '❌'}")
            if metadata.get('include_code'):
                print_plain(f"   Code: 📝 Included")
            else:
                print_plain(f"   Code: 📝 Not included")
            print_plain("")
        
        return packages
    
    def validate_package(self, package_file: Path) -> bool:
        """Validate package integrity.
        
        Args:
            package_file: Path to the package file
            
        Returns:
            True if package is valid, False otherwise
        """
        log_info(f"Validating package: {package_file}")
        
        result = self.package_manager.validate_package(package_file)
        
        if not result.get("success"):
            log_error(f"Package validation failed: {result.get('error')}")
            return False
        
        metadata = result.get("metadata", {})
        is_valid = result.get("valid", False)
        
        if is_valid:
            log_success("Package validation passed")
            print_plain(f"📦 Package: {package_file.name}")
            print_plain(f"🌿 Branch: {metadata.get('branch_name', 'unknown')}")
            print_plain(f"📅 Created: {metadata.get('created_at', 'unknown')}")
            print_plain(f"🏷️  Version: {metadata.get('ilha_version', 'unknown')}")
            print_plain(f"✅ Status: Valid")
        else:
            log_warning("Package validation failed - checksum mismatch")
            print_plain(f"📦 Package: {package_file.name}")
            print_plain(f"❌ Status: Invalid (checksum mismatch)")
        
        return is_valid
    
    def get_package_info(self, package_file: Path) -> Dict:
        """Get detailed package information.
        
        Args:
            package_file: Path to the package file
            
        Returns:
            Dictionary with package information
        """
        result = self.package_manager.validate_package(package_file)
        
        if not result.get("success"):
            return {
                "success": False,
                "error": result.get("error")
            }
        
        metadata = result.get("metadata", {})
        is_valid = result.get("valid", False)
        
        return {
            "success": True,
            "name": package_file.name,
            "path": str(package_file),
            "size": package_file.stat().st_size,
            "valid": is_valid,
            "metadata": metadata
        }
    
    def list_packages_json(self, package_dir: Path) -> List[Dict]:
        """List packages as JSON for programmatic use.
        
        Args:
            package_dir: Directory to search for packages
            
        Returns:
            List of package information dictionaries
        """
        return self.package_manager.list_packages(package_dir)
    
    def validate_package_json(self, package_file: Path) -> Dict:
        """Validate package and return JSON result.
        
        Args:
            package_file: Path to the package file
            
        Returns:
            Dictionary with validation results
        """
        return self.package_manager.validate_package(package_file)
