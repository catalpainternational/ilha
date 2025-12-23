"""
Push manager for ilha deployment operations.

This module orchestrates the complete push workflow: export, transfer, and remote import.
"""

import subprocess
import socket
import re
import shlex
from pathlib import Path
from typing import Optional, Dict, Tuple, List

from ...core.package_manager import PackageManager
from ...core.dns_manager import DNSManager, parse_domain, is_domain
from ...core.droplet_manager import DropletManager, DropletInfo
from ...core.environment_manager import EnvironmentManager
from ...core import dns_providers  # noqa: F401 - trigger registration
from ...utils.logging import log_info, log_success, log_warning, log_error, print_plain
from ...utils.path_utils import detect_execution_context, get_worktree_branch_name
from ...utils.confirmation import confirm_action
from ...utils.ssh_utils import add_ssh_host_key
from ...utils.ssh_manager import SSHConnectionManager, SCPTarget
from .server_preparer import ServerPreparer
from .transfer_manager import TransferManager


class PushManager:
    """Manages push operations for deploying packages to remote servers."""
    
    def __init__(self, project_root: Optional[Path] = None):
        """Initialize push manager.
        
        Args:
            project_root: Project root directory. If None, uses current working directory.
        """
        if project_root is None:
            from ...config.settings import get_project_root
            self.project_root = get_project_root()
        else:
            self.project_root = Path(project_root).resolve()
        
        self.package_manager = PackageManager(project_root=self.project_root)
        self.server_preparer = ServerPreparer()
        self.transfer = TransferManager()
        self.ssh = SSHConnectionManager()
    
    def _validate_scp_target(self, scp_target: str) -> bool:
        """Validate SCP target format.
        
        Args:
            scp_target: SCP target string to validate
            
        Returns:
            True if valid, False otherwise
        """
        try:
            SCPTarget(scp_target)
            return True
        except ValueError:
            return False
    
    def _parse_scp_target(self, scp_target: str) -> Tuple[str, str, str]:
        """Parse SCP target into components.
        
        Args:
            scp_target: SCP target string
            
        Returns:
            Tuple of (username, server, remote_path)
            
        Raises:
            ValueError: If format is invalid
        """
        target = SCPTarget(scp_target)
        return (target.username, target.server, target.remote_path)
    
    def _detect_current_branch(self) -> Optional[str]:
        """Detect current branch name from working directory.
        
        Returns:
            Branch name if detected, None otherwise
        """
        try:
            # Try to detect execution context (worktree or git root)
            worktree_path, branch_name, is_worktree = detect_execution_context()
            
            if is_worktree and branch_name:
                return branch_name
            
            # Fallback: try git branch --show-current
            try:
                result = subprocess.run(
                    ["git", "branch", "--show-current"],
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=self.project_root
                )
                branch = result.stdout.strip()
                if branch:
                    return branch
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
            
            # Fallback: extract from directory name
            current_path = Path.cwd()
            if "worktrees" in str(current_path):
                branch = get_worktree_branch_name(current_path)
                if branch:
                    return branch
            
            return None
            
        except Exception:
            return None
    
    def _resolve_server_ip(self, server: str) -> str:
        """Resolve server hostname to IP address.
        
        Args:
            server: Server hostname or IP address
            
        Returns:
            IP address string
        """
        # Check if it's already an IP address
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', server):
            return server
        
        # Try to resolve hostname
        try:
            ip = socket.gethostbyname(server)
            log_info(f"Resolved {server} to {ip}")
            return ip
        except socket.gaierror:
            log_warning(f"Could not resolve {server} to IP, using as-is")
            return server
    
    def _handle_dns_management(self, domain: str, server: str, 
                               dns_token: Optional[str] = None,
                               create_droplet: bool = False) -> bool:
        """Handle DNS management for domain deployment using Digital Ocean DNS.
        
        Args:
            domain: Full domain name (e.g., 'app.example.com')
            server: Server hostname or IP
            dns_token: Digital Ocean API token
            create_droplet: Whether a new droplet was created (auto-updates DNS if True)
            
        Returns:
            True if DNS is configured successfully, False otherwise
        """
        try:
            log_info(f"Starting DNS management for domain: {domain}")
            
            # Validate domain format
            log_info("Validating domain format...")
            if not is_domain(domain):
                log_error(f"Invalid domain format: {domain}")
                return False
            
            # Parse domain into components
            try:
                subdomain, base_domain = parse_domain(domain)
                log_info(f"Parsed domain - Subdomain: {subdomain}, Base Domain: {base_domain}")
            except ValueError as e:
                log_error(str(e))
                return False
            
            # Resolve API token
            log_info("Resolving DNS API token...")
            api_token = DNSManager.resolve_dns_token(dns_token)
            if not api_token:
                log_error("DNS API token not found. Set DIGITALOCEAN_API_TOKEN environment variable, or use --dns-token")
                return False
            log_info("DNS API token resolved successfully")
            
            # Create Digital Ocean provider instance
            log_info("Creating Digital Ocean DNS provider...")
            provider = DNSManager.create_provider('digitalocean', api_token)
            if not provider:
                log_error("Failed to create DNS provider")
                return False
            log_info("DNS provider created successfully")
            
            log_info(f"Checking DNS records for {domain}...")
            
            # Check if domain exists
            exists, current_ip = provider.check_domain_exists(subdomain, base_domain)
            log_info(f"DNS record exists: {exists}, Current IP: {current_ip}")
            
            # Extract server IP from server string (could be hostname or IP)
            log_info(f"Resolving server IP for: {server}")
            server_ip = self._resolve_server_ip(server)
            log_info(f"Server IP resolved to: {server_ip}")
            
            if exists:
                if current_ip == server_ip:
                    log_success(f"DNS record already exists and points to {server_ip}")
                    log_info("No DNS changes needed")
                    return True
                else:
                    log_warning(f"DNS record exists but points to {current_ip} (expected {server_ip})")
                    
                    # Auto-update DNS when creating new droplet (automated deployment)
                    if create_droplet:
                        log_info(f"Auto-updating DNS for new droplet: {domain} -> {server_ip}")
                        log_info(f"Updating DNS A record: {domain} -> {server_ip}")
                        if provider.update_subdomain(subdomain, base_domain, server_ip):
                            log_success(f"DNS record updated successfully: {domain} -> {server_ip}")
                            return True
                        else:
                            log_error("Failed to update DNS record")
                            return False
                    else:
                        # Manual deployment: ask for confirmation
                        if confirm_action(f"Update DNS record to point to {server_ip}?"):
                            log_info(f"Updating DNS A record: {domain} -> {server_ip}")
                            if provider.update_subdomain(subdomain, base_domain, server_ip):
                                log_success(f"DNS record updated successfully: {domain} -> {server_ip}")
                                return True
                            else:
                                log_error("Failed to update DNS record")
                                return False
                        else:
                            log_info("Continuing with existing DNS configuration...")
                            return True
            else:
                # Domain doesn't exist, auto-create it
                log_info(f"No DNS record found for {domain}")
                log_info(f"Creating DNS A record: {domain} -> {server_ip}")
                log_info("Sending DNS creation request to Digital Ocean API...")
                if provider.create_subdomain(subdomain, base_domain, server_ip):
                    log_success(f"DNS record created successfully: {domain} -> {server_ip}")
                    return True
                else:
                    log_error("Failed to create DNS record")
                    return False
                    
        except Exception as e:
            log_error(f"DNS management error: {e}")
            return False
    
    def _run_remote_import(self, username: str, server: str, remote_file: str, 
                           branch_name: str, domain: Optional[str], ip: Optional[str],
                           build: bool = False, debug: bool = False,
                           use_staging_certificates: bool = False) -> bool:
        """Run remote import using ilha server-import command.
        
        Args:
            username: SSH username
            server: Server hostname or IP
            remote_file: Path to package file on remote server
            branch_name: Branch name
            domain: Optional domain for Caddy routing
            ip: Optional IP for HTTP-only routing
            build: Whether to rebuild Docker images
            debug: Whether to enable debug mode
            use_staging_certificates: Whether to use staging certificates
            
        Returns:
            True if successful, False otherwise
        """
        try:
            log_info("Executing remote import via ilha server-import command...")
            log_info("This will: import package, start proxy, and bring up the worktree environment")
            
            # Build command arguments
            cmd_parts = [
                "ilha", "server-import",
                remote_file,
                "--branch-name", branch_name
            ]
            
            if domain:
                cmd_parts.extend(["--domain", domain])
            elif ip:
                cmd_parts.extend(["--ip", ip])
            
            if build:
                cmd_parts.append("--build")
                
            if debug:
                cmd_parts.append("--debug")
                
            if use_staging_certificates:
                cmd_parts.append("--use-staging-certificates")
            
            # Build SSH command - properly escape arguments for bash -lc
            # bash -lc expects a single quoted string, so we need to escape
            # special characters in each argument and join them with spaces
            escaped_parts = [shlex.quote(part) for part in cmd_parts]
            command_string = " ".join(escaped_parts)
            
            ssh_cmd = self.ssh.build_ssh_command(
                username,
                server,
                command_string,
                use_control_master=True
            )
            
            log_info(f"Executing: {' '.join(ssh_cmd)}")
            
            # Execute with streaming
            from ...utils.streaming import execute_with_streaming
            
            success, stdout_lines, stderr_lines = execute_with_streaming(
                ssh_cmd,
                script=None,  # No script, just command
                timeout=1800,  # 30 minutes
                progress_interval=30,
                prefix="  ",
                filter_keywords=None
            )
            
            if not success:
                log_error("Remote import failed")
                if stderr_lines:
                    log_error("Import errors:")
                    for line in stderr_lines:
                        log_error(f"  {line}")
                return False
            
            log_success("Remote import completed successfully")
            return True
            
        except Exception as e:
            log_error(f"Remote import failed: {e}")
            import traceback
            log_error(f"Traceback: {traceback.format_exc()}")
            return False
    
    def push_package(self, branch_name: Optional[str], scp_target: Optional[str], 
                    output_dir: Path = None, keep_package: bool = False,
                    auto_import: bool = False, domain: Optional[str] = None,
                    ip: Optional[str] = None, prepare_server: bool = False,
                    dns_token: Optional[str] = None,
                    skip_dns_check: bool = False,
                    create_droplet: bool = False,
                    droplet_name: Optional[str] = None,
                    droplet_region: Optional[str] = None,
                    droplet_size: Optional[str] = None,
                    droplet_image: Optional[str] = None,
                    droplet_ssh_keys: Optional[list] = None,
                    resume: bool = False,
                    code_only: bool = False,
                    build: bool = False,
                    debug: bool = False,
                    containers: Optional[str] = None,
                    exclude_deps: Optional[List[str]] = None,
                    vpc_uuid: Optional[str] = None,
                    droplet_info: Optional[DropletInfo] = None,
                    central_droplet_info: Optional[DropletInfo] = None,
                    use_staging_certificates: bool = False) -> bool:
        """Export and push package to remote server via SCP.
        
        Args:
            branch_name: Branch/worktree name (optional, auto-detects if not provided)
            scp_target: SCP target in format username@server:path (optional when create_droplet is True)
            output_dir: Temporary package location (default: ./packages)
            keep_package: Don't delete package after successful push
            auto_import: Automatically import package on remote server
            domain: Domain for Caddy routing
            ip: IP for HTTP-only routing
            prepare_server: Prepare server (install dependencies) before push
            dns_token: DNS API token
            skip_dns_check: Skip DNS management
            create_droplet: Create new droplet before pushing
            build: Rebuild Docker images on remote server
            containers: Optional comma-separated list of 'worktree.container' patterns
            exclude_deps: Optional list of services to exclude from dependencies
            droplet_info: Droplet info if droplet was created
            central_droplet_info: Central droplet info for VPC deployments
            
        Returns:
            True if successful, False otherwise
        """
        try:
            log_info("Starting push operation...")
            
            # Handle code-only push (delegated to separate method)
            if code_only:
                if output_dir is None:
                    output_dir = self.project_root / "packages"
                return self._push_code_only(branch_name, scp_target, domain, ip, output_dir, build)
            
            # Auto-detect branch name if not provided
            if not branch_name:
                log_info("Branch name not provided, attempting auto-detection...")
                branch_name = self._detect_current_branch()
                if not branch_name:
                    log_error("Could not detect branch name. Please specify branch_name.")
                    return False
                log_info(f"Auto-detected branch: {branch_name}")
            else:
                log_info(f"Using provided branch name: {branch_name}")
            
            # Validate and parse SCP target (required unless creating droplet or droplet_info is provided)
            if not scp_target and not create_droplet and not droplet_info:
                log_error("scp_target is required (unless creating droplet)")
                return False
            
            # Handle droplet creation if requested (only if droplet_info is not already provided)
            if create_droplet and not droplet_info:
                if not droplet_name:
                    log_error("droplet_name is required when create_droplet is True")
                    return False
                
                log_info("Creating droplet for deployment...")
                droplet = self._create_droplet_for_push(
                    droplet_name=droplet_name,
                    droplet_region=droplet_region,
                    droplet_size=droplet_size,
                    droplet_image=droplet_image,
                    droplet_ssh_keys=droplet_ssh_keys,
                    vpc_uuid=vpc_uuid
                )
                
                if not droplet or not droplet.ip_address:
                    log_error("Failed to create droplet or get IP address")
                    return False
                
                # Update scp_target to use droplet IP
                if scp_target:
                    # Parse existing scp_target and replace server with droplet IP
                    try:
                        target = SCPTarget(scp_target)
                        scp_target = f"{target.username}@{droplet.ip_address}:{target.remote_path}"
                    except ValueError:
                        # Fallback: assume root@droplet_ip:/root
                        scp_target = f"root@{droplet.ip_address}:/root"
                else:
                    # Default to root@droplet_ip:/root
                    scp_target = f"root@{droplet.ip_address}:/root"
                
                droplet_info = droplet
                log_info(f"Using droplet IP for SCP target: {scp_target}")
            
            # Validate SCP target format
            log_info(f"Validating SCP target format: {scp_target}")
            try:
                scp_target_obj = SCPTarget(scp_target)
            except ValueError as e:
                log_error(f"Invalid SCP target format: {scp_target}")
                log_info("Expected format: username@server:path")
                return False
            log_info("SCP target format is valid")
            
            # Handle DNS management if domain is provided
            if domain and not skip_dns_check:
                log_info(f"Domain provided: {domain}, managing DNS records...")
                # Use droplet IP directly if available (more reliable than resolving from server string)
                dns_server = scp_target_obj.server
                if droplet_info and droplet_info.ip_address:
                    dns_server = droplet_info.ip_address
                    log_info(f"Using droplet IP directly for DNS: {dns_server}")
                dns_success = self._handle_dns_management(domain, dns_server, dns_token, create_droplet)
                if not dns_success:
                    log_warning("DNS management failed, but continuing with push...")
            elif skip_dns_check:
                log_info("DNS check skipped (--skip-dns-check flag)")
            else:
                log_info("No domain provided, skipping DNS management")
            
            # Export package with --include-code (required for production deployments)
            if output_dir is None:
                output_dir = self.project_root / "packages"
            
            log_info(f"Exporting package for branch: {branch_name}")
            log_info(f"Output directory: {output_dir}")
            log_info("Package will include code and be compressed for faster transfer")
            
            # Check if worktree exists before export
            from ...core.git_manager import GitManager
            git_manager = GitManager(project_root=self.project_root, validate=False)
            if not git_manager.validate_worktree_exists(branch_name):
                log_error(f"Worktree for branch '{branch_name}' does not exist")
                log_info("Available worktrees:")
                worktrees = git_manager.list_worktrees()
                for wt in worktrees:
                    log_info(f"  - {wt}")
                return False
            
            # Ensure remote directory exists
            if not self.transfer.ensure_remote_dir(scp_target_obj):
                log_warning("Failed to ensure remote directory exists, but continuing...")
            
            # Resume mode: detect what's already done
            package_already_on_server = False
            server_already_prepared = False
            remote_file_path = None
            package_path = None
            
            if resume:
                log_info("Resume mode enabled: checking what's already completed...")
                
                # Check if server is already prepared
                if self.server_preparer.verify_ilha_installed(scp_target_obj.username, scp_target_obj.server):
                    server_already_prepared = True
                    log_info("✓ Server already prepared (ilha found)")
                    log_info("Skipping server preparation...")
                else:
                    log_info("Server not prepared, will prepare if needed")
                
                # Try to find existing package on server
                found_package = self.transfer.find_existing_package(scp_target_obj, branch_name)
                if found_package:
                    remote_file_path = found_package
                    package_already_on_server = True
                    log_info(f"✓ Found existing package on server: {found_package}")
                    log_info("Skipping package export and transfer...")
                else:
                    log_info("No existing package found on server, will export and transfer")
            
            # Parse container filter if provided
            container_filter = None
            if containers:
                log_info(f"Parsing container selection: {containers}")
                try:
                    from ...utils.container_selector import parse_container_selection
                    container_filter = parse_container_selection(containers, self.project_root)
                    log_info(f"Selected {len(container_filter)} container(s) for export")
                    for selection in container_filter:
                        log_info(f"  - {selection['worktree']}.{selection['container']}")
                    if exclude_deps:
                        log_info(f"Excluding services from dependencies: {', '.join(exclude_deps)}")
                except ValueError as e:
                    log_error(f"Invalid container selection: {e}")
                    return False
            
            # Set staging certificate flag if requested (before export)
            if use_staging_certificates:
                from ...core.environment_manager import EnvironmentManager
                env_manager = EnvironmentManager(project_root=self.project_root)
                if not env_manager.set_staging_certificate_flag(branch_name, value=True):
                    log_warning("Failed to set USE_STAGING_CERTIFICATES flag, but continuing with export...")
            
            # Export package only if not already on server
            if not package_already_on_server:
                log_info("Worktree validated, proceeding with export...")
                export_result = self.package_manager.export_package(
                    branch_name=branch_name,
                    output_dir=output_dir,
                    include_code=True,  # Always include code for deployments
                    compressed=True,    # Always compress for faster transfer
                    container_filter=container_filter,
                    exclude_deps=exclude_deps,
                    droplet_info=droplet_info,
                    central_droplet_info=central_droplet_info
                )
                
                if not export_result.get("success"):
                    log_error(f"Failed to export package: {export_result.get('error')}")
                    return False
                
                package_path = Path(export_result.get("package_path"))
                log_info(f"Package exported successfully: {package_path}")
                if not package_path.exists():
                    log_error(f"Package file not found: {package_path}")
                    return False
                
                package_size_mb = package_path.stat().st_size / 1024 / 1024
                log_success(f"Package size: {package_size_mb:.2f} MB")
                
                # Log package metadata if available
                metadata = export_result.get("metadata", {})
                if metadata:
                    log_info("Package metadata:")
                    log_info(f"  - Branch: {metadata.get('branch_name', 'unknown')}")
                    log_info(f"  - Project: {metadata.get('project_name', 'unknown')}")
                    log_info(f"  - Includes code: {metadata.get('include_code', False)}")
                    log_info(f"  - Volumes included: {not metadata.get('skip_volumes', False)}")
            else:
                # Extract package name from remote path for display
                if remote_file_path:
                    import os
                    package_name = os.path.basename(remote_file_path)
                    log_info(f"Using existing package: {package_name}")
            
            # Auto-import requires server preparation
            if auto_import and not prepare_server and not server_already_prepared:
                log_info("Auto-import requires server preparation. Enabling --prepare-server...")
                prepare_server = True
            
            # Prepare server dependencies if requested (before upload/import)
            if prepare_server and not server_already_prepared:
                log_info("Server preparation requested, installing dependencies...")
                log_info("This may take several minutes depending on server state...")
                ok = self.server_preparer.prepare_server(scp_target_obj.username, scp_target_obj.server)
                if not ok:
                    log_error("Server preparation failed. Aborting push.")
                    return False
                log_info("Server preparation completed successfully")
                
                # Verify ilha is installed (required for auto-import)
                if auto_import:
                    log_info("Verifying ilha installation on server...")
                    if not self.server_preparer.verify_ilha_installed(scp_target_obj.username, scp_target_obj.server):
                        log_error("ilha not found on server. Server preparation may have failed.")
                        return False
            elif server_already_prepared:
                log_info("Server preparation skipped (already prepared)")
            else:
                log_info("Server preparation skipped (--prepare-server not specified)")
            
            # Transfer package via SCP (skip if already on server in resume mode)
            if not (resume and package_already_on_server):
                if not package_path:
                    log_error("Package path not available for transfer")
                    return False
                log_info(f"Starting transfer to {scp_target_obj.server}...")
                log_info(f"Source: {package_path}")
                log_info(f"Destination: {scp_target_obj}")
                if not self.transfer.transfer_package(package_path, scp_target_obj):
                    log_error("Failed to transfer package to remote server")
                    return False
                
                log_success(f"Package pushed successfully to {scp_target_obj}")
                log_info("Transfer completed")
            else:
                log_info(f"Package already on server, skipping transfer")
            
            # Determine remote package path for import
            if resume and package_already_on_server and remote_file_path:
                remote_package_path = remote_file_path
            elif package_path:
                remote_package_path = scp_target_obj.get_remote_file_path(package_path.name)
            else:
                log_error("Cannot determine remote package path")
                return False
            
            # Display package info and next steps
            if package_path:
                package_size_mb = package_path.stat().st_size / 1024 / 1024
                package_name = package_path.name
            else:
                # In resume mode with existing package
                import os
                package_name = os.path.basename(remote_package_path) if remote_package_path else "existing package"
                package_size_mb = 0  # Size unknown for existing package
            
            print_plain(f"\n📦 Package: {package_name}")
            print_plain(f"📁 Remote Location: {scp_target_obj}")
            if package_size_mb > 0:
                print_plain(f"💾 Size: {package_size_mb:.1f} MB")
            print_plain(f"\n📋 Next Steps:")
            print_plain(f"   1. SSH to the server:")
            print_plain(f"      ssh {scp_target_obj.username}@{scp_target_obj.server}")
            print_plain(f"   ")
            print_plain(f"   2. Import the package:")
            print_plain(f"      ilha server-import {remote_package_path} --branch-name {branch_name}")
            if domain:
                print_plain(f"         --domain {domain}")
            if ip:
                print_plain(f"         --ip {ip}")
            print_plain(f"   ")
            print_plain(f"   3. Start the services (if not started automatically):")
            print_plain(f"      ilha {branch_name} up -d")
            
            # Optional: show server versions after prepare
            if prepare_server:
                log_info("Checking server requirements and versions...")
                self._check_server_requirements(scp_target_obj.username, scp_target_obj.server)
            
            # Optional: auto import on server
            if auto_import:
                log_info("Auto-import enabled, running remote import and startup...")
                log_info(f"Remote package path: {remote_package_path}")
                log_info(f"Branch name: {branch_name}")
                if domain:
                    log_info(f"Domain override: {domain}")
                if ip:
                    log_info(f"IP override: {ip}")
                self._run_remote_import(
                    scp_target_obj.username,
                    scp_target_obj.server,
                    remote_package_path,
                    branch_name,
                    domain,
                    ip,
                    build=build,
                    debug=debug,
                    use_staging_certificates=use_staging_certificates,
                )
                log_info("Remote import process completed")
                
                # Configure VPC firewall if enabled and droplet info is available
                if droplet_info:
                    self._configure_vpc_firewall(scp_target_obj.username, scp_target_obj.server, droplet_info)
            else:
                log_info("Auto-import disabled, manual import required")
                if build:
                    log_warning("Build flag specified but auto-import disabled. Run 'ilha "
                                f"{branch_name} build' on the server after importing.")
            
            # Clean up package unless keep_package is True (only if we created it locally)
            if package_path and not keep_package and package_path.exists():
                log_info("Cleaning up local package file...")
                try:
                    package_path.unlink()
                    log_info(f"Cleaned up local package: {package_path.name}")
                except Exception as e:
                    log_warning(f"Failed to clean up package: {e}")
            elif package_path:
                log_info(f"Keeping local package (--keep-package flag or file not found)")
            
            log_info("Push operation completed successfully")
            
            # Save push configuration for future use
            env_manager = EnvironmentManager(project_root=self.project_root)
            env_manager.save_push_config(
                branch_name,
                str(scp_target_obj),
                domain,
                ip
            )
            
            return True
            
        except Exception as e:
            log_error(f"Error pushing package: {e}")
            import traceback
            log_error(f"Traceback: {traceback.format_exc()}")
            return False
        finally:
            # Cleanup SSH connections
            self.transfer.cleanup()
            self.ssh.cleanup()
    
    def _check_server_requirements(self, username: str, server: str) -> None:
        """Run a basic non-fatal check for required tools on the server."""
        try:
            cmd = self.ssh.build_ssh_command(
                username,
                server,
                'set -e; (docker --version || true); (docker compose version || true); (git --version || true); (ilha --version || true)',
                use_control_master=True
            )
            log_info("Checking server requirements...")
            subprocess.run(cmd, check=False)
        except Exception as e:
            log_warning(f"Server requirement check failed: {e}")
    
    def _configure_vpc_firewall(self, username: str, server: str, droplet_info: DropletInfo) -> bool:
        """Configure UFW firewall rules for VPC-accessible services.
        
        Only activates when:
        - Droplet has VPC information (private IP)
        - Config allows it (opt-in via config.yml)
        
        Args:
            username: SSH username
            server: Server hostname or IP
            droplet_info: Droplet info containing private IP
            
        Returns:
            True if firewall was configured, False otherwise
        """
        try:
            # Check if droplet has VPC info
            private_ip = getattr(droplet_info, 'private_ip_address', None)
            if not private_ip:
                log_info("No private IP found in droplet info, skipping firewall configuration")
                return False
            
            log_info("Configuring VPC firewall rules for secure Redis/DB access...")
            log_info(f"Private IP: {private_ip}")
            
            # Build firewall configuration script
            script = """#!/bin/bash
set -euo pipefail

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

log_success() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $*" >&2
}

log_warning() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ $*" >&2
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ $*" >&2
}

log "=== Configuring VPC Firewall Rules ==="

# Check if UFW is installed
if ! command -v ufw >/dev/null 2>&1; then
    log_warning "UFW not installed, skipping firewall configuration"
    exit 0
fi

# Check if UFW is active
if ! ufw status | grep -q "Status: active"; then
    log "UFW is not active, enabling..."
    ufw --force enable || {
        log_error "Failed to enable UFW"
        exit 1
    }
    log_success "UFW enabled"
fi

# Configure firewall rules for VPC network (10.0.0.0/8)
VPC_NETWORK="10.0.0.0/8"
PORTS=(5432 6379)

for port in "${PORTS[@]}"; do
    # Check if rule already exists
    if ufw status | grep -q "from ${VPC_NETWORK} to any port ${port}"; then
        log "Firewall rule for port ${port} already exists, skipping"
    else
        log "Adding firewall rule: allow from ${VPC_NETWORK} to port ${port}"
        if ufw allow from ${VPC_NETWORK} to any port ${port} comment "VPC access for ilha"; then
            log_success "Firewall rule added for port ${port}"
        else
            log_error "Failed to add firewall rule for port ${port}"
        fi
    fi
done

# Reload UFW to apply changes
log "Reloading UFW..."
ufw reload || {
    log_warning "UFW reload failed, but rules may still be applied"
}

log_success "VPC firewall configuration completed"
"""
            
            # Execute firewall configuration script
            exec_cmd = "cat > /tmp/dtfw.sh && chmod +x /tmp/dtfw.sh && /tmp/dtfw.sh && rm -f /tmp/dtfw.sh"
            cmd = self.ssh.build_ssh_command(username, server, exec_cmd, use_control_master=True)
            
            log_info("Executing firewall configuration script...")
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Use communicate() with input to avoid I/O errors from closing stdin manually
            stdout, stderr = process.communicate(input=script, timeout=60)
            
            if process.returncode == 0:
                log_success("VPC firewall configuration completed successfully")
                if stdout:
                    for line in stdout.splitlines():
                        if line.strip():
                            log_info(f"[FIREWALL] {line}")
                return True
            else:
                log_warning(f"Firewall configuration returned non-zero exit code: {process.returncode}")
                if stderr:
                    for line in stderr.splitlines():
                        if line.strip():
                            log_warning(f"[FIREWALL] {line}")
                # Don't fail the whole operation if firewall config fails
                return False
                
        except subprocess.TimeoutExpired:
            log_warning("Firewall configuration script timed out")
            return False
        except Exception as e:
            log_warning(f"Failed to configure firewall: {e}")
            # Don't fail the whole operation if firewall config fails
            return False
    
    def _create_droplet_for_push(self, droplet_name: str,
                                 droplet_region: Optional[str] = None,
                                 droplet_size: Optional[str] = None,
                                 droplet_image: Optional[str] = None,
                                 droplet_ssh_keys: Optional[list] = None,
                                 api_token: Optional[str] = None,
                                 vpc_uuid: Optional[str] = None) -> Optional[DropletInfo]:
        """Create a droplet for push deployment.
        
        Always waits for the droplet to be ready before returning, as the push
        operation requires the droplet IP address and SSH access.
        
        Args:
            droplet_name: Name for the droplet
            droplet_region: Droplet region
            droplet_size: Droplet size
            droplet_image: Droplet image
            droplet_ssh_keys: List of SSH key IDs or fingerprints
            api_token: Digital Ocean API token
            vpc_uuid: VPC UUID for VPC deployment
            
        Returns:
            DropletInfo if successful, None otherwise
        """
        try:
            log_info("Starting droplet creation process...")
            
            # Resolve API token
            log_info("Resolving Digital Ocean API token...")
            token = DropletManager.resolve_droplet_token(api_token)
            if not token:
                log_error("Digital Ocean API token not found. Set DIGITALOCEAN_API_TOKEN environment variable")
                return None
            log_info("API token resolved successfully")
            
            # Get defaults from environment
            log_info("Loading droplet defaults from environment...")
            defaults = DropletManager.get_droplet_defaults()
            
            # Use provided values or defaults
            region = droplet_region or defaults.get('region', 'nyc1')
            size = droplet_size or defaults.get('size', 's-1vcpu-1gb')
            image = droplet_image or defaults.get('image', 'ubuntu-22-04-x64')
            
            log_info(f"Droplet configuration:")
            log_info(f"  Name: {droplet_name}")
            log_info(f"  Region: {region}")
            log_info(f"  Size: {size}")
            log_info(f"  Image: {image}")
            
            # Create provider (needed for SSH key resolution)
            log_info("Creating Digital Ocean provider...")
            provider = DropletManager.create_provider('digitalocean', token)
            if not provider:
                log_error("Failed to create droplet provider")
                return None
            log_info("Provider created successfully")
            
            # Resolve SSH keys (required for droplet creation)
            log_info("Resolving SSH keys for droplet...")
            ssh_keys = self._resolve_ssh_keys(droplet_ssh_keys, defaults, provider)
            if not ssh_keys:
                log_error("No SSH keys specified. Use --droplet-ssh-keys or set DROPLET_DEFAULT_SSH_KEYS environment variable.")
                return None
            log_info(f"SSH keys resolved: {', '.join(ssh_keys)}")
            
            log_info(f"Creating droplet: {droplet_name}")
            log_info("Sending droplet creation request to Digital Ocean API...")
            
            # Create droplet
            droplet = provider.create_droplet(
                name=droplet_name,
                region=region,
                size=size,
                image=image,
                ssh_keys=ssh_keys if ssh_keys else None,
                tags=None,
                vpc_uuid=vpc_uuid
            )
            
            if not droplet:
                log_error("Failed to create droplet")
                return None
            
            log_success(f"Droplet created: {droplet_name} (ID: {droplet.id})")
            
            # Always wait for droplet to be ready (required for push operation)
            log_info("Waiting for droplet to be ready (this may take 1-2 minutes)...")
            log_info("Checking droplet status and SSH availability...")
            if not provider.wait_for_droplet_ready(droplet.id, check_ssh=True):
                log_error("Droplet created but not ready within timeout. Cannot proceed with push.")
                return None
            
            log_info("Droplet is ready and SSH is accessible")
            
            # Refresh droplet info to get IP address
            log_info("Refreshing droplet information to get IP address...")
            droplet = provider.get_droplet(droplet.id)
            
            if droplet and droplet.ip_address:
                log_success(f"Droplet IP address: {droplet.ip_address}")
                log_info("Adding SSH host key for new droplet...")
                # Add SSH host key for the new droplet
                add_ssh_host_key(droplet.ip_address)
                log_info("SSH host key added successfully")
            else:
                log_error("Droplet IP address not available. Cannot proceed with push.")
                return None
            
            log_info("Droplet creation process completed")
            return droplet
            
        except Exception as e:
            log_error(f"Error creating droplet: {e}")
            return None
    
    def _resolve_ssh_keys(self, provided_keys: Optional[list], defaults: dict, provider) -> Optional[list]:
        """Resolve SSH keys for droplet creation.
        
        Checks in order:
        1. Provided keys (from CLI flag)
        2. Default keys from environment
        3. Auto-detect first available key from Digital Ocean account
        
        Args:
            provided_keys: SSH keys provided via CLI flag
            defaults: Default values from environment
            provider: Digital Ocean provider instance
            
        Returns:
            List of SSH key names, or None if no keys found
        """
        # Use provided keys if available
        if provided_keys:
            log_info(f"Using provided SSH keys: {', '.join(provided_keys)}")
            return provided_keys
        
        # Check defaults from environment
        default_keys = defaults.get('ssh_keys', [])
        if default_keys:
            log_info(f"Using default SSH keys from environment: {', '.join(default_keys)}")
            return default_keys
        
        # Auto-detect: get first available SSH key from Digital Ocean account
        try:
            ssh_keys_list = provider.list_ssh_keys()
            if ssh_keys_list:
                # Get the first key's name
                first_key = ssh_keys_list[0]
                key_name = first_key.get('name', 'unnamed')
                log_info(f"Auto-detected SSH key: {key_name} (no keys specified, using first available)")
                return [key_name]
            else:
                log_warning("No SSH keys found in Digital Ocean account")
                return None
        except Exception as e:
            log_warning(f"Failed to list SSH keys from Digital Ocean: {e}")
            return None
    
    def _push_code_only(self, branch_name: Optional[str], scp_target: Optional[str],
                       domain: Optional[str], ip: Optional[str], output_dir: Path, build: bool = False) -> bool:
        """Push code-only update to remote server.
        
        This is a simplified version that only transfers code, not full packages.
        Implementation would go here - for now, just log that it's not implemented.
        
        Args:
            branch_name: Branch name (optional, can be from config)
            scp_target: SCP target (optional, can be from config)
            domain: Domain override (optional)
            ip: IP override (optional)
            output_dir: Directory for temporary files
            build: Whether to rebuild Docker images after code update
            
        Returns:
            True if successful, False otherwise
        """
        # TODO: Implement code-only push
        log_error("Code-only push is not yet implemented in refactored version")
        return False

