"""
Droplet management commands for ilha CLI.

This module provides commands for managing Digital Ocean droplets including
creating, listing, and destroying droplets.
"""

from typing import Optional, List, Dict, Tuple, Any, Union
from pathlib import Path
from ..core.droplet_manager import DropletManager, DropletProvider, DropletInfo
from ..core.dns_manager import DNSManager
from ..core import dns_providers  # noqa: F401 - Import to trigger registration
from ..core.dns_providers.digitalocean import DigitalOceanProvider
from ..utils.logging import log_info, log_success, log_warning, log_error, log_step, print_plain, emit_error
from ..utils.confirmation import confirm_action, confirm_by_typing_name
from ..utils.json_output import JSONOutput
from ..utils.path_utils import detect_execution_context, get_worktree_branch_name
from ..config.settings import get_project_root
from ..exceptions import ilhaCommandError
from ._constants import DO_TOKEN_NOT_FOUND
from .models import RemoteTarget, MergedConfig
from rich.table import Table
from rich.console import Console


class DropletCommands:
    """Manages droplet operations for ilha CLI."""
    
    def __init__(self):
        """Initialize droplet commands."""
        pass
    
    def _get_provider(self, api_token: Optional[str] = None, json: bool = False) -> Tuple[Optional[DropletProvider], Optional[str]]:
        """Get a droplet provider instance with centralized error handling.
        
        Args:
            api_token: Digital Ocean API token
            json: Output as JSON
            
        Returns:
            Tuple of (provider, error_message). If successful, provider is set and error_message is None.
            If failed, provider is None and error_message contains the error.
        """
        # Resolve API token
        token = DropletManager.resolve_droplet_token(api_token)
        if not token:
            emit_error(DO_TOKEN_NOT_FOUND, json)
            return None, DO_TOKEN_NOT_FOUND
        
        # Create provider
        provider = DropletManager.create_provider('digitalocean', token)
        if not provider:
            error_msg = "Failed to create droplet provider"
            emit_error(error_msg, json)
            return None, error_msg
        
        return provider, None
    
    def _find_droplet_by_name(self, name: str, provider: DropletProvider) -> Optional[DropletInfo]:
        """Find a droplet by name.
        
        Args:
            name: Droplet name to search for
            provider: DropletProvider instance
            
        Returns:
            DropletInfo if exactly one match found, None otherwise (caller handles multiple matches)
        """
        droplets = provider.list_droplets()
        matches = [droplet for droplet in droplets if droplet.name == name]
        
        if len(matches) == 0:
            return None
        elif len(matches) == 1:
            return matches[0]
        else:
            # Multiple matches - return None, caller will handle error
            return None
    
    def _resolve_droplet_identifier(self, identifier: str, api_token: Optional[str] = None, json: bool = False) -> Tuple[Optional[int], Optional[str]]:
        """Resolve a droplet identifier (ID or name) to a droplet ID.
        
        Args:
            identifier: Droplet ID (numeric string) or name (non-numeric string)
            api_token: Digital Ocean API token
            json: Output as JSON
            
        Returns:
            Tuple of (droplet_id, error_message). If successful, droplet_id is set and error_message is None.
            If failed, droplet_id is None and error_message contains the error.
        """
        # Check if identifier is numeric (ID) or non-numeric (name)
        identifier = identifier.strip()
        
        # Try to parse as integer (ID)
        try:
            droplet_id = int(identifier)
            # Validate that the ID exists
            provider, error_msg = self._get_provider(api_token, json)
            if error_msg:
                return None, error_msg
            
            droplet = provider.get_droplet(droplet_id)
            if not droplet:
                error_msg = f"Droplet {droplet_id} not found"
                emit_error(error_msg, json)
                return None, error_msg
            
            return droplet_id, None
        except ValueError:
            # Not a numeric ID, treat as name
            pass
        
        # Resolve by name
        provider, error_msg = self._get_provider(api_token, json)
        if error_msg:
            return None, error_msg
        
        droplet = self._find_droplet_by_name(identifier, provider)
        if not droplet:
            # Check if there were multiple matches
            droplets = provider.list_droplets()
            matches = [d for d in droplets if d.name == identifier]
            if len(matches) > 1:
                error_msg = f"Multiple droplets found with name '{identifier}'. Please use droplet ID instead."
            else:
                error_msg = f"Droplet '{identifier}' not found"
            
            emit_error(error_msg, json)
            return None, error_msg
        
        return droplet.id, None
    
    def list_droplets_by_tag(
        self,
        tag: str,
        *,
        api_token: Optional[str] = None,
        branch: Optional[str] = None,
        require_tags: Optional[List[str]] = None,
    ) -> List[DropletInfo]:
        """List droplets that include the given tag (and every ``require_tags``
        entry), optionally filtered by branch tag."""
        provider, error_msg = self._get_provider(api_token, json=False)
        if error_msg or not provider:
            raise ilhaCommandError(error_msg or "DigitalOcean provider unavailable")
        droplets = provider.list_droplets()
        branch_tag = f"branch:{branch}" if branch else None
        wanted = [tag, *(require_tags or [])]
        if branch_tag:
            wanted.append(branch_tag)
        return [
            droplet
            for droplet in droplets
            if all(w in (droplet.tags or []) for w in wanted)
        ]
    
    def _resolve_droplet_identifiers(self, identifiers: str, api_token: Optional[str] = None, json: bool = False) -> Tuple[List[int], List[Dict[str, Any]]]:
        """Resolve comma-separated droplet identifiers (IDs or names) to droplet IDs.
        
        Args:
            identifiers: Comma-separated string of droplet IDs or names
            api_token: Digital Ocean API token
            json: Output as JSON
            
        Returns:
            Tuple of (resolved_ids, errors). resolved_ids contains successfully resolved IDs.
            errors contains list of dicts with 'identifier' and 'error' keys for failed resolutions.
        """
        resolved_ids = []
        errors = []
        
        # Parse comma-separated identifiers
        identifier_list = [id_str.strip() for id_str in identifiers.split(',') if id_str.strip()]
        
        if not identifier_list:
            error_msg = "No valid droplet identifiers provided"
            emit_error(error_msg, json)
            return [], [{"identifier": "", "error": error_msg}]
        
        # Resolve each identifier
        for identifier in identifier_list:
            droplet_id, error_msg = self._resolve_droplet_identifier(identifier, api_token, json)
            if error_msg:
                errors.append({"identifier": identifier, "error": error_msg})
            else:
                resolved_ids.append(droplet_id)
        
        return resolved_ids, errors
    
    def create_droplet(self, name: str, region: Optional[str] = None,
                      size: Optional[str] = None, image: Optional[str] = None,
                      ssh_keys: Optional[List[str]] = None,
                      tags: Optional[List[str]] = None,
                      wait: bool = False, api_token: Optional[str] = None,
                      json: bool = False, containers: Optional[str] = None,
                      vpc_uuid: Optional[str] = None,
                      user_data: Optional[str] = None,
                      return_droplet: bool = False) -> Union[bool, Optional[DropletInfo]]:
        """Create a new droplet.
        
        Args:
            name: Droplet name
            region: Droplet region (defaults from env or 'nyc1')
            size: Droplet size (defaults from env or 's-1vcpu-1gb')
            image: Droplet image (defaults from env or 'ubuntu-22-04-x64')
            ssh_keys: List of SSH key IDs or fingerprints
            tags: List of tags for the droplet
            wait: Wait for droplet to be ready
            api_token: Digital Ocean API token
            json: Output as JSON
            containers: Optional comma-separated list of 'worktree.container' patterns
                       for selective container/volume deployment (stored for future use)
            user_data: Optional cloud-init user data script
            
        Returns:
            True if successful, False otherwise. If return_droplet=True,
            returns DropletInfo on success and None on failure.
        """
        try:
            # Resolve API token
            token = DropletManager.resolve_droplet_token(api_token)
            if not token:
                emit_error(DO_TOKEN_NOT_FOUND, json)
                return False
            
            # Get defaults from environment
            defaults = DropletManager.get_droplet_defaults()
            
            # Use provided values or defaults
            region = region or defaults.get('region', 'nyc1')
            size = size or defaults.get('size', 's-1vcpu-1gb')
            image = image or defaults.get('image', 'ubuntu-22-04-x64')
            ssh_keys = ssh_keys or defaults.get('ssh_keys', [])
            
            # Create provider
            provider = DropletManager.create_provider('digitalocean', token)
            if not provider:
                emit_error("Failed to create droplet provider", json)
                return None if return_droplet else False
            
            if not json:
                log_step(f"Creating droplet {name}")
                log_info(f"  Region: {region}")
                log_info(f"  Size: {size}")
                log_info(f"  Image: {image}")
                if ssh_keys:
                    log_info(f"  SSH Keys: {', '.join(ssh_keys)}")
                if tags:
                    log_info(f"  Tags: {', '.join(tags)}")
            
            # Create droplet
            droplet = provider.create_droplet(
                name=name,
                region=region,
                size=size,
                image=image,
                ssh_keys=ssh_keys if ssh_keys else None,
                tags=tags if tags else None,
                vpc_uuid=vpc_uuid,
                user_data=user_data,
            )
            
            if not droplet:
                if json:
                    JSONOutput.print_error("Failed to create droplet")
                else:
                    log_error("Failed to create droplet")
                return None if return_droplet else False
            
            # Wait for droplet to be ready if requested
            if wait:
                if not json:
                    log_step("Waiting for droplet to be ready")
                if not provider.wait_for_droplet_ready(droplet.id):
                    if json:
                        JSONOutput.print_error("Droplet created but not ready within timeout")
                    else:
                        log_warning("Droplet created but not ready within timeout")
                    # Still return success since droplet was created
            
            # Refresh droplet info to get IP address
            droplet = provider.get_droplet(droplet.id)
            
            if json:
                result = {
                    "success": True,
                    "droplet": {
                        "id": droplet.id,
                        "name": droplet.name,
                        "ip_address": droplet.ip_address,
                        "status": droplet.status,
                        "region": droplet.region,
                        "size": droplet.size,
                        "image": droplet.image,
                        "tags": droplet.tags,
                        "private_ip_address": droplet.private_ip_address,
                        "vpc_uuid": droplet.vpc_uuid
                    }
                }
                JSONOutput.print_json(result)
            else:
                log_success(f"Droplet created successfully: {name} (ID: {droplet.id})")
                print_plain("")
                print_plain("Droplet Information:")
                if droplet.ip_address:
                    print_plain(f"  Public IP: {droplet.ip_address}")
                if droplet.private_ip_address:
                    print_plain(f"  Private IP: {droplet.private_ip_address}")
                if droplet.vpc_uuid:
                    print_plain(f"  VPC UUID: {droplet.vpc_uuid}")
                print_plain(f"  Status: {droplet.status}")
                print_plain(f"  Region: {droplet.region}")
                print_plain(f"  Size: {droplet.size}")
                print_plain(f"  Image: {droplet.image}")
                if droplet.tags:
                    print_plain(f"  Tags: {', '.join(droplet.tags)}")
            
            if return_droplet:
                return droplet
            return True
            
        except Exception as e:
            if json:
                JSONOutput.print_error(f"Error creating droplet: {e}")
            else:
                log_error(f"Error creating droplet: {e}")
            return None if return_droplet else False
    
    def list_droplets(self, api_token: Optional[str] = None, json: bool = False, csv: bool = False) -> bool:
        """List all droplets.
        
        Args:
            api_token: Digital Ocean API token
            json: Output as JSON
            csv: Output as CSV
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Resolve API token
            token = DropletManager.resolve_droplet_token(api_token)
            if not token:
                emit_error(DO_TOKEN_NOT_FOUND, json)
                return False
            
            # Create provider
            provider = DropletManager.create_provider('digitalocean', token)
            if not provider:
                emit_error("Failed to create droplet provider", json)
                return False
            
            # List droplets
            droplets = provider.list_droplets()
            # Lookup domains for each droplet: precompute all A records by IP once (O(domains) API calls)
            # instead of per-droplet find_dns_records_by_ip (O(droplets * domains) calls).
            droplet_domains = {}
            if isinstance(provider, DigitalOceanProvider):
                all_by_ip = provider.get_all_dns_a_records_by_ip()
                for droplet in droplets:
                    if droplet.ip_address:
                        dns_records = all_by_ip.get(droplet.ip_address, [])
                        droplet_domains[droplet.id] = [f"{s}.{d}" for s, d, _ in dns_records]
                    else:
                        droplet_domains[droplet.id] = []
            else:
                for droplet in droplets:
                    if droplet.ip_address:
                        try:
                            dns_records = provider.find_dns_records_by_ip(droplet.ip_address)
                            if dns_records:
                                droplet_domains[droplet.id] = [f"{subdomain}.{domain}" for subdomain, domain, _ in dns_records]
                            else:
                                droplet_domains[droplet.id] = []
                        except Exception:
                            droplet_domains[droplet.id] = []
                    else:
                        droplet_domains[droplet.id] = []
            if csv:
                # Output as CSV
                import csv
                import sys
                writer = csv.writer(sys.stdout)
                # Write header
                writer.writerow(["ID", "Name", "IP Address", "Domain", "Status", "Region", "Size", "Image", "Tags", "Created At"])
                # Write rows
                for droplet in droplets:
                    tags_str = ', '.join(droplet.tags) if droplet.tags else ""
                    created_at_str = droplet.created_at.isoformat() if droplet.created_at else ""
                    # Format domains as comma-separated string
                    domains = droplet_domains.get(droplet.id, [])
                    domain_str = ', '.join(domains) if domains else ""
                    writer.writerow([
                        droplet.id,
                        droplet.name,
                        droplet.ip_address or "",
                        domain_str,
                        droplet.status,
                        droplet.region,
                        droplet.size,
                        droplet.image or "",
                        tags_str,
                        created_at_str
                    ])
                return True
            elif json:
                result = {
                    "success": True,
                    "droplets": [
                        {
                            "id": droplet.id,
                            "name": droplet.name,
                            "ip_address": droplet.ip_address,
                            "domains": droplet_domains.get(droplet.id, []) or None,
                            "status": droplet.status,
                            "region": droplet.region,
                            "size": droplet.size,
                            "image": droplet.image,
                            "tags": droplet.tags,
                            "created_at": droplet.created_at.isoformat() if droplet.created_at else None,
                            "private_ip_address": droplet.private_ip_address,
                            "vpc_uuid": droplet.vpc_uuid
                        }
                        for droplet in droplets
                    ]
                }
                JSONOutput.print_json(result)
            else:
                if not droplets:
                    print_plain("No droplets found")
                    return True
                
                # Create a formatted table
                console = Console()
                table = Table(
                    title=f"Droplets ({len(droplets)})",
                    show_header=True,
                    header_style="bold",
                    box=None,
                    padding=(0, 1)
                )
                
                table.add_column("ID", style="cyan", no_wrap=True, min_width=10)
                table.add_column("Name", style="green", min_width=12)
                table.add_column("IP Address", style="yellow", min_width=15)
                table.add_column("Domain", style="cyan", min_width=20, overflow="ellipsis")
                table.add_column("Status", style="magenta", min_width=8)
                table.add_column("Region", style="blue", min_width=6)
                table.add_column("Size", style="white", min_width=12)
                table.add_column("Image", style="dim white", min_width=20, overflow="ellipsis")
                table.add_column("Tags", style="dim white", min_width=10, overflow="ellipsis")
                
                for droplet in droplets:
                    # Format status with color
                    if droplet.status == 'active':
                        status = f"[green]{droplet.status}[/green]"
                    elif droplet.status == 'off':
                        status = f"[red]{droplet.status}[/red]"
                    else:
                        status = f"[yellow]{droplet.status}[/yellow]"
                    
                    # Format image (handle None)
                    image = droplet.image or "N/A"
                    
                    # Format tags
                    tags = ', '.join(droplet.tags) if droplet.tags else "—"
                    
                    # Format domains
                    domains = droplet_domains.get(droplet.id, [])
                    domain_str = ', '.join(domains) if domains else "—"
                    
                    table.add_row(
                        str(droplet.id),
                        droplet.name,
                        droplet.ip_address or "N/A",
                        domain_str,
                        status,
                        droplet.region,
                        droplet.size,
                        image,
                        tags
                    )
                
                console.print(table)
            
            return True
            
        except Exception as e:
            if json:
                JSONOutput.print_error(f"Error listing droplets: {e}")
            else:
                log_error(f"Error listing droplets: {e}")
            return False
    
    def destroy_droplet(self, droplet_id: int, force: bool = False,
                       api_token: Optional[str] = None, json: bool = False,
                       only_droplet: bool = False, only_domain: bool = False,
                       domain: Optional[str] = None, dns_token: Optional[str] = None) -> bool:
        """Destroy a droplet and/or associated DNS records.
        
        Args:
            droplet_id: Droplet ID to destroy
            force: Skip confirmation
            api_token: Digital Ocean API token
            json: Output as JSON
            only_droplet: Only destroy droplet, skip DNS
            only_domain: Only destroy DNS records, skip droplet
            domain: Optional domain override for DNS deletion
            dns_token: DNS API token (may differ from droplet token)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Validate flags
            if only_droplet and only_domain:
                if json:
                    JSONOutput.print_error("Cannot specify both --only-droplet and --only-domain")
                else:
                    log_error("Cannot specify both --only-droplet and --only-domain")
                return False
            
            # Handle DNS-only mode
            if only_domain:
                return self._destroy_dns_only(droplet_id, force, domain, dns_token, json)
            
            # Resolve API token for droplet operations
            token = DropletManager.resolve_droplet_token(api_token)
            if not token:
                emit_error(DO_TOKEN_NOT_FOUND, json)
                return False
            
            # Create provider
            provider = DropletManager.create_provider('digitalocean', token)
            if not provider:
                emit_error("Failed to create droplet provider", json)
                return False
            
            # Get droplet info for confirmation
            droplet = provider.get_droplet(droplet_id)
            if not droplet:
                if json:
                    JSONOutput.print_error(f"Droplet {droplet_id} not found")
                else:
                    log_error(f"Droplet {droplet_id} not found")
                return False
            
            # Confirm destruction unless force flag is set
            if not force and not json:
                # Display droplet information
                log_info(f"Droplet to be destroyed:")
                log_info(f"  ID: {droplet.id}")
                log_info(f"  Name: {droplet.name}")
                if droplet.ip_address:
                    log_info(f"  IP Address: {droplet.ip_address}")
                log_info(f"  Status: {droplet.status}")
                log_info(f"  Region: {droplet.region}")
                log_info("")
                
                # Require user to type the droplet name to confirm
                if not confirm_by_typing_name(
                    droplet.name,
                    f"WARNING: This will permanently destroy droplet '{droplet.name}' (ID: {droplet_id}). This action cannot be undone."
                ):
                    log_info("Cancelled droplet destruction")
                    return False
            
            # Destroy droplet
            if not json:
                log_step(f"Destroying droplet {droplet.name} (ID: {droplet_id})")
            
            success = provider.destroy_droplet(droplet_id)
            
            if success:
                if json:
                    result = {
                        "success": True,
                        "message": f"Droplet {droplet_id} destroyed successfully"
                    }
                    JSONOutput.print_json(result)
                else:
                    log_success(f"Droplet {droplet_id} destroyed successfully")
                
                # If not only_droplet, also try to destroy DNS records
                if not only_droplet and droplet.ip_address:
                    self._destroy_dns_for_ip(droplet.ip_address, force, domain, dns_token, json)
                
                return True
            else:
                if json:
                    JSONOutput.print_error(f"Failed to destroy droplet {droplet_id}")
                else:
                    log_error(f"Failed to destroy droplet {droplet_id}")
                return False
                
        except ValueError:
            # Handle invalid droplet_id (not an integer)
            if json:
                JSONOutput.print_error(f"Invalid droplet ID: {droplet_id}. Must be an integer.")
            else:
                log_error(f"Invalid droplet ID: {droplet_id}. Must be an integer.")
            return False
        except Exception as e:
            if json:
                JSONOutput.print_error(f"Error destroying droplet: {e}")
            else:
                log_error(f"Error destroying droplet: {e}")
            return False
    
    def _destroy_dns_only(self, droplet_id: int, force: bool, domain: Optional[str],
                          dns_token: Optional[str], json: bool) -> bool:
        """Destroy DNS records only (droplet is not destroyed).
        
        Args:
            droplet_id: Droplet ID to get IP from
            force: Skip confirmation
            domain: Optional domain to limit search
            dns_token: DNS API token
            json: Output as JSON
            
        Returns:
            True if successful, False otherwise
        """
        # Get droplet IP first
        token = DropletManager.resolve_droplet_token(None)
        if not token:
            emit_error(DO_TOKEN_NOT_FOUND, json)
            return False
        
        provider = DropletManager.create_provider('digitalocean', token)
        if not provider:
            if json:
                JSONOutput.print_error("Failed to create droplet provider")
            else:
                log_error("Failed to create droplet provider")
            return False
        
        droplet = provider.get_droplet(droplet_id)
        if not droplet:
            if json:
                JSONOutput.print_error(f"Droplet {droplet_id} not found")
            else:
                log_error(f"Droplet {droplet_id} not found")
            return False
        
        if not droplet.ip_address:
            if json:
                JSONOutput.print_error(f"Droplet {droplet_id} has no IP address")
            else:
                log_error(f"Droplet {droplet_id} has no IP address")
            return False
        
        return self._destroy_dns_for_ip(droplet.ip_address, force, domain, dns_token, json)
    
    def _destroy_dns_for_ip(self, ip: str, force: bool, domain: Optional[str],
                            dns_token: Optional[str], json: bool) -> bool:
        """Destroy DNS records pointing to a specific IP address.
        
        Args:
            ip: IP address to search for
            force: Skip confirmation
            domain: Optional domain to limit search
            dns_token: DNS API token
            json: Output as JSON
            
        Returns:
            True if successful, False otherwise
        """
        # Resolve DNS token
        dns_api_token = DNSManager.resolve_dns_token(dns_token)
        if not dns_api_token:
            if json:
                log_warning("DNS API token not found. Skipping DNS deletion.")
            else:
                log_warning("DNS API token not found. Skipping DNS deletion.")
            return True  # Don't fail if DNS token not available
        
        # Create DNS provider
        dns_provider = DNSManager.create_provider('digitalocean', dns_api_token)
        if not dns_provider:
            if json:
                log_warning("Failed to create DNS provider. Skipping DNS deletion.")
            else:
                log_warning("Failed to create DNS provider. Skipping DNS deletion.")
            return True  # Don't fail if DNS provider creation fails
        
        # Find DNS records pointing to this IP
        if not hasattr(dns_provider, 'find_dns_records_by_ip'):
            if json:
                log_warning("DNS provider does not support finding records by IP")
            else:
                log_warning("DNS provider does not support finding records by IP")
            return True
        
        dns_records = dns_provider.find_dns_records_by_ip(ip, domain)
        
        if not dns_records:
            if not json:
                log_info(f"No DNS records found pointing to {ip}")
            return True
        
        if not json:
            log_info(f"Found {len(dns_records)} DNS record(s) pointing to {ip}")
        
        # Delete each DNS record
        success_count = 0
        for subdomain, base_domain, record_id in dns_records:
            full_domain = f"{subdomain}.{base_domain}" if subdomain else base_domain
            
            # Confirm deletion unless force flag is set
            if not force and not json:
                log_info(f"DNS record to be deleted:")
                log_info(f"  Domain: {full_domain}")
                log_info(f"  IP Address: {ip}")
                log_info("")
                
                if not confirm_by_typing_name(
                    full_domain,
                    f"WARNING: This will permanently delete DNS record '{full_domain}'. This action cannot be undone."
                ):
                    log_info(f"Skipping DNS record deletion for {full_domain}")
                    continue
            
            # Delete the DNS record
            if dns_provider.delete_subdomain(subdomain, base_domain):
                if json:
                    # JSON output handled separately
                    pass
                else:
                    log_success(f"Deleted DNS record: {full_domain}")
                success_count += 1
            else:
                if json:
                    JSONOutput.print_error(f"Failed to delete DNS record: {full_domain}")
                else:
                    log_error(f"Failed to delete DNS record: {full_domain}")
        
        if json:
            result = {
                "success": True,
                "message": f"Deleted {success_count} of {len(dns_records)} DNS record(s)"
            }
            JSONOutput.print_json(result)
        
        return success_count > 0 or len(dns_records) == 0
    
    def get_droplet_info(self, droplet_id: int, api_token: Optional[str] = None,
                        json: bool = False) -> bool:
        """Get detailed information about a droplet.
        
        Args:
            droplet_id: Droplet ID
            api_token: Digital Ocean API token
            json: Output as JSON
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Resolve API token
            token = DropletManager.resolve_droplet_token(api_token)
            if not token:
                emit_error(DO_TOKEN_NOT_FOUND, json)
                return False
            
            # Create provider
            provider = DropletManager.create_provider('digitalocean', token)
            if not provider:
                emit_error("Failed to create droplet provider", json)
                return False
            
            # Get droplet info
            droplet = provider.get_droplet(droplet_id)
            
            if not droplet:
                if json:
                    JSONOutput.print_error(f"Droplet {droplet_id} not found")
                else:
                    log_error(f"Droplet {droplet_id} not found")
                return False
            
            if json:
                result = {
                    "success": True,
                    "droplet": {
                        "id": droplet.id,
                        "name": droplet.name,
                        "ip_address": droplet.ip_address,
                        "status": droplet.status,
                        "region": droplet.region,
                        "size": droplet.size,
                        "image": droplet.image,
                        "tags": droplet.tags,
                        "created_at": droplet.created_at.isoformat() if droplet.created_at else None,
                        "private_ip_address": droplet.private_ip_address,
                        "vpc_uuid": droplet.vpc_uuid
                    }
                }
                JSONOutput.print_json(result)
            else:
                print_plain(f"Droplet Information:")
                print_plain(f"  ID: {droplet.id}")
                print_plain(f"  Name: {droplet.name}")
                if droplet.ip_address:
                    print_plain(f"  IP Address: {droplet.ip_address}")
                print_plain(f"  Status: {droplet.status}")
                print_plain(f"  Region: {droplet.region}")
                print_plain(f"  Size: {droplet.size}")
                print_plain(f"  Image: {droplet.image}")
                if droplet.tags:
                    print_plain(f"  Tags: {', '.join(droplet.tags)}")
                if droplet.created_at:
                    print_plain(f"  Created: {droplet.created_at}")
            
            return True
            
        except ValueError:
            # Handle invalid droplet_id (not an integer)
            if json:
                JSONOutput.print_error(f"Invalid droplet ID: {droplet_id}. Must be an integer.")
            else:
                log_error(f"Invalid droplet ID: {droplet_id}. Must be an integer.")
            return False
        except Exception as e:
            if json:
                JSONOutput.print_error(f"Error getting droplet info: {e}")
            else:
                log_error(f"Error getting droplet info: {e}")
            return False
    
    def list_sizes(self, api_token: Optional[str] = None, json: bool = False, csv: bool = False, 
                   show_all: bool = False) -> bool:
        """List droplet sizes.
        
        Args:
            api_token: Digital Ocean API token
            json: Output as JSON
            csv: Output as CSV
            show_all: Show all sizes including unavailable (default: show only available)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Resolve API token
            token = DropletManager.resolve_droplet_token(api_token)
            if not token:
                emit_error(DO_TOKEN_NOT_FOUND, json)
                return False
            
            # Create provider
            provider = DropletManager.create_provider('digitalocean', token)
            if not provider:
                emit_error("Failed to create droplet provider", json)
                return False
            
            # List sizes (now handles pagination exhaustively)
            sizes = provider.list_sizes()
            
            if not sizes:
                if json:
                    JSONOutput.print_error("No sizes found or failed to retrieve sizes")
                else:
                    log_error("No sizes found or failed to retrieve sizes")
                return False
            
            # Filter to only available sizes by default (unless show_all is True)
            if not show_all:
                sizes = [s for s in sizes if s.get('available', False)]
            
            # Sort by price and memory
            sizes.sort(key=lambda x: (x.get('price_monthly') or 0, x.get('memory', 0)))
            
            if csv:
                # Output as CSV
                import csv
                import sys
                writer = csv.writer(sys.stdout)
                # Write header
                header = ["Slug", "Memory (MB)", "vCPUs", "Disk (GB)", "Price/Month", "Price/Hour"]
                if show_all:
                    header.append("Available")
                writer.writerow(header)
                # Write rows
                for s in sizes:
                    price_monthly = f"${s.get('price_monthly', 0):.2f}" if s.get('price_monthly') else "N/A"
                    price_hourly = f"${s.get('price_hourly', 0):.6f}" if s.get('price_hourly') else "N/A"
                    row = [
                        s.get('slug', ''),
                        s.get('memory', 0),
                        s.get('vcpus', 0),
                        s.get('disk', 0),
                        price_monthly,
                        price_hourly
                    ]
                    if show_all:
                        row.append('Yes' if s.get('available', False) else 'No')
                    writer.writerow(row)
                return True
            elif json:
                result = {
                    "success": True,
                    "sizes": sizes,
                    "total": len(sizes),
                    "available": len([s for s in sizes if s.get('available', False)])
                }
                JSONOutput.print_json(result)
            else:
                if not sizes:
                    print_plain("No sizes found")
                    return True
                
                # Create a formatted table
                console = Console()
                if show_all:
                    # Count available sizes when showing all
                    available_count = len([s for s in sizes if s.get('available', False)])
                    title = f"Available Droplet Sizes ({len(sizes)} total, {available_count} available)"
                    # Add Available column when showing all
                    add_available_column = True
                else:
                    title = f"Available Droplet Sizes ({len(sizes)})"
                    add_available_column = False
                
                table = Table(
                    title=title,
                    show_header=True,
                    header_style="bold",
                    box=None,
                    padding=(0, 1)
                )
                
                table.add_column("Slug", style="cyan", no_wrap=True, min_width=15)
                table.add_column("Memory", style="green", min_width=10, justify="right")
                table.add_column("vCPUs", style="yellow", min_width=6, justify="right")
                table.add_column("Disk", style="blue", min_width=8, justify="right")
                table.add_column("Price/Month", style="magenta", min_width=12, justify="right")
                table.add_column("Price/Hour", style="white", min_width=12, justify="right")
                if add_available_column:
                    table.add_column("Available", style="dim white", min_width=9, justify="center")
                
                for size in sizes:
                    # Format memory
                    memory_mb = size.get('memory', 0)
                    if memory_mb >= 1024:
                        memory_str = f"{memory_mb / 1024:.1f} GB"
                    else:
                        memory_str = f"{memory_mb} MB"
                    
                    # Format disk
                    disk_gb = size.get('disk', 0)
                    disk_str = f"{disk_gb} GB"
                    
                    # Format prices
                    price_monthly = size.get('price_monthly')
                    price_monthly_str = f"${price_monthly:.2f}" if price_monthly else "N/A"
                    
                    price_hourly = size.get('price_hourly')
                    price_hourly_str = f"${price_hourly:.6f}" if price_hourly else "N/A"
                    
                    # Build row
                    row_data = [
                        size.get('slug', 'unknown'),
                        memory_str,
                        str(size.get('vcpus', 0)),
                        disk_str,
                        price_monthly_str,
                        price_hourly_str
                    ]
                    
                    # Add availability column only when showing all
                    if add_available_column:
                        is_available = size.get('available', False)
                        available_str = "[green]Yes[/green]" if is_available else "[red]No[/red]"
                        row_data.append(available_str)
                    
                    table.add_row(*row_data)
                
                console.print(table)
            
            return True
            
        except Exception as e:
            if json:
                JSONOutput.print_error(f"Error listing sizes: {e}")
            else:
                log_error(f"Error listing sizes: {e}")
            return False
    
    def list_regions(self, api_token: Optional[str] = None, json: bool = False, csv: bool = False,
                     show_all: bool = False) -> bool:
        """List droplet regions.
        
        Args:
            api_token: Digital Ocean API token
            json: Output as JSON
            csv: Output as CSV
            show_all: Show unavailable regions as well
        
        Returns:
            True if successful, False otherwise
        """
        try:
            token = DropletManager.resolve_droplet_token(api_token)
            if not token:
                emit_error(DO_TOKEN_NOT_FOUND, json)
                return False
            
            provider = DropletManager.create_provider('digitalocean', token)
            if not provider:
                emit_error("Failed to create droplet provider", json)
                return False
            
            all_regions = provider.list_regions()
            if all_regions is None:
                all_regions = []
            
            available_regions = [r for r in all_regions if r.get('available', False)]
            regions = all_regions if show_all else available_regions
            
            # Sort regions by slug then name
            regions.sort(key=lambda r: (r.get('slug') or '', r.get('name') or ''))
            
            if csv:
                import csv
                import sys
                writer = csv.writer(sys.stdout)
                writer.writerow(["Slug", "Name", "Available", "Features", "Sizes"])
                for region in regions:
                    features = ', '.join(region.get('features') or [])
                    writer.writerow([
                        region.get('slug', ''),
                        region.get('name', ''),
                        'Yes' if region.get('available', False) else 'No',
                        features,
                        len(region.get('sizes') or [])
                    ])
                return True
            elif json:
                result = {
                    "success": True,
                    "regions": regions,
                    "total": len(regions),
                    "available": len([r for r in regions if r.get('available', False)])
                }
                JSONOutput.print_json(result)
            else:
                if not regions:
                    if show_all:
                        print_plain("No regions found")
                    else:
                        print_plain("No available regions found (use --show-all to include unavailable regions)")
                    return True
                
                console = Console()
                if show_all:
                    title = f"DigitalOcean Regions ({len(regions)} total, {len([r for r in regions if r.get('available', False)])} available)"
                else:
                    title = f"Available DigitalOcean Regions ({len(regions)})"
                
                table = Table(
                    title=title,
                    show_header=True,
                    header_style="bold",
                    box=None,
                    padding=(0, 1)
                )
                
                table.add_column("Slug", style="cyan", no_wrap=True, min_width=10)
                table.add_column("Name", style="green", min_width=18)
                table.add_column("Status", style="magenta", min_width=10)
                table.add_column("Features", style="yellow", min_width=20, overflow="ellipsis")
                table.add_column("Sizes", style="blue", justify="right", min_width=5)
                
                for region in regions:
                    status = "[green]Available[/green]" if region.get('available', False) else "[red]Unavailable[/red]"
                    features = ', '.join(region.get('features') or []) or "—"
                    size_count = len(region.get('sizes') or [])
                    
                    table.add_row(
                        region.get('slug', 'unknown'),
                        region.get('name', 'N/A'),
                        status,
                        features,
                        str(size_count)
                    )
                
                console.print(table)
            
            return True
        
        except Exception as e:
            if json:
                JSONOutput.print_error(f"Error listing regions: {e}")
            else:
                log_error(f"Error listing regions: {e}")
            return False


# ===== Helper functions (moved from cli_commands/droplets.py) =====


def auto_detect_branch_name(branch_name: Optional[str], json: bool) -> str:
    """Autodetect the branch name when not explicitly provided.

    Raises:
        ilhaCommandError: when detection fails. Callers must not print a second
        error or return success; ``command_wrapper`` turns this into a nonzero exit.
    """
    if branch_name:
        return branch_name

    if not json:
        log_info("Branch name not provided, attempting auto-detection...")

    worktree_path, detected_branch, is_worktree = detect_execution_context()
    if is_worktree and detected_branch:
        return detected_branch

    try:
        project_root = get_project_root()
        branch = get_worktree_branch_name(project_root)
        if branch:
            return branch
    except (FileNotFoundError, Exception):
        pass

    current_path = Path.cwd()
    if "worktrees" in str(current_path):
        branch = get_worktree_branch_name(current_path)
        if branch:
            return branch

    raise ilhaCommandError(
        "Could not detect branch name. Please specify branch_name as argument or use --domain "
        "to auto-detect from subdomain."
    )


def resolve_droplet_name(branch_name: str, domain: Optional[str], json: bool) -> str:
    """Resolve droplet name from branch/domain settings."""
    if not domain:
        if not json:
            log_info(f"Using branch name '{branch_name}' as droplet name")
        return branch_name

    try:
        from ..core.dns_manager import parse_domain

        subdomain, _ = parse_domain(domain)
        if not subdomain:
            if not json:
                log_info(
                    f"Domain '{domain}' is a root domain (no subdomain), using branch name '{branch_name}' as droplet name"
                )
            return branch_name
        if not json:
            log_info(f"Using subdomain '{subdomain}' from domain '{domain}' as droplet name")
        return subdomain
    except ValueError:
        if not json:
            log_warning(f"Could not parse domain '{domain}', using branch name '{branch_name}' as droplet name")
        return branch_name


def resolve_host_to_ip(host: str, api_token: Optional[str] = None) -> str:
    """Resolve host to IP: already-IP, domain (via DO when token present), DNS, or droplet id/name."""
    import re
    from ..utils.network import resolve_server_ip

    if re.match(r'^\d+\.\d+\.\d+\.\d+$', host):
        return host

    # Domain + token: resolve via DO only (subdomain = droplet name), no DNS fallback
    if "." in host and api_token:
        from ..core.dns_manager import parse_domain
        subdomain, _ = parse_domain(host)
        if subdomain:
            droplet_commands = DropletCommands()
            droplet_id, error = droplet_commands._resolve_droplet_identifier(subdomain, api_token)
            if error:
                raise ValueError(f"Could not resolve domain '{host}' (droplet name '{subdomain}'): {error}")
            if droplet_id:
                provider, _ = droplet_commands._get_provider(api_token)
                if provider:
                    droplet = provider.get_droplet(droplet_id)
                    if droplet and droplet.ip_address:
                        return droplet.ip_address
                raise ValueError(f"No droplet or IP for '{subdomain}' (domain '{host}')")

    # DNS (when no token or not domain-like)
    try:
        ip = resolve_server_ip(host)
        if ip != host:
            return ip
    except Exception as exc:
        log_warning(f"DNS lookup for '{host}' failed: {exc}")

    # Droplet ID or name
    droplet_commands = DropletCommands()
    droplet_id, error = droplet_commands._resolve_droplet_identifier(host, api_token)
    if error:
        raise ValueError(f"Could not resolve '{host}': {error}")

    provider, _ = droplet_commands._get_provider(api_token)
    if not provider:
        raise ValueError(f"Failed to create droplet provider: {error}")

    droplet = provider.get_droplet(droplet_id)
    if not droplet or not droplet.ip_address:
        raise ValueError(f"Droplet {host} has no IP address")

    return droplet.ip_address


def parse_rsync_target_or_droplet(arg: str, api_token: Optional[str] = None) -> str:
    """Parse rsync/connection target patterns and resolve to user@ip:path.

    Supports:
    - Full target: `user@host:path` → resolve host to IP, return user@ip:path
    - Username + domain: `user@example.com` → resolve domain, return user@ip:/root
    - Domain/IP + path: `example.com:/path` → return root@ip:path
    - Domain/IP only: `example.com` → return root@ip:/root
    - Droplet ID or name → resolve to IP, return root@ip:/root
    """
    # Full target format user@host:path
    if '@' in arg and ':' in arg:
        at_pos = arg.index('@')
        colon_pos = arg.index(':')
        if at_pos < colon_pos:
            username = arg[:at_pos]
            host = arg[at_pos + 1:colon_pos]
            path = arg[colon_pos + 1:]
            ip = resolve_host_to_ip(host, api_token)
            return f"{username}@{ip}:{path}"

    # Username + domain/IP
    if '@' in arg and ':' not in arg:
        username, host = arg.split('@', 1)
        ip = resolve_host_to_ip(host, api_token)
        return f"{username}@{ip}:/root"

    # Domain/IP + path
    if ':' in arg and '@' not in arg:
        host, path = arg.rsplit(':', 1)
        ip = resolve_host_to_ip(host, api_token)
        return f"root@{ip}:{path}"

    # Just host
    ip = resolve_host_to_ip(arg, api_token)
    return f"root@{ip}:/root"


def log_creation_context(
    droplet_name: str,
    resolved_region: str,
    resolved_size: str,
    resolved_image: str,
    ssh_keys: Optional[str],
    tags: tuple,
    containers: Optional[str],
    exclude_deps: Optional[str],
    json: bool,
) -> None:
    """Log droplet creation configuration."""
    if json:
        return
    log_info("Starting droplet creation process...")
    log_info("Droplet configuration:")
    log_info(f"  Name: {droplet_name}")
    log_info(f"  Region: {resolved_region}")
    log_info(f"  Size: {resolved_size}")
    log_info(f"  Image: {resolved_image}")
    if ssh_keys:
        log_info(f"  SSH Keys: {ssh_keys}")
    if tags:
        log_info(f"  Tags: {', '.join(tags)}")
    if containers:
        log_info(f"  Containers: {containers}")
    if exclude_deps:
        log_info(f"  Exclude Dependencies: {exclude_deps}")


def load_and_merge_config(branch_name: str, cli_args: Dict[str, Any]) -> MergedConfig:
    """Merge CLI arguments with stored droplet config.

    Priority: CLI argument > stored config value > None.
    Returns MergedConfig with merged dict, env_manager, and stored_config.
    """
    from ..core.environment_manager import EnvironmentManager
    from ..config.settings import get_project_root

    env_manager = EnvironmentManager(project_root=get_project_root())
    stored_config = env_manager.get_droplet_config(branch_name or '')

    result = dict(cli_args)

    # String fields: CLI wins if truthy, else stored
    for key in (
        'region', 'size', 'image', 'ssh_keys', 'vpc_uuid',
        'central_droplet_name', 'scp_target', 'containers', 'exclude_deps',
    ):
        if not result.get(key):
            result[key] = stored_config.get(key)

    # Empty-string fields: CLI wins if truthy, else stored or ''
    for key in ('domain', 'ip', 'dns_token'):
        if not result.get(key):
            result[key] = stored_config.get(key) or ''

    # Boolean fields: if CLI is falsy, parse stored string
    for key in ('keep_package', 'prepare_server', 'resume', 'build', 'debug'):
        if not result.get(key):
            stored_val = stored_config.get(key)
            if stored_val:
                result[key] = str(stored_val).lower() in ('true', '1', 'yes')

    # output_dir: only override if CLI is at the Click default
    if result.get('output_dir') == './packages':
        stored_dir = stored_config.get('output_dir')
        if stored_dir:
            result['output_dir'] = stored_dir

    # tags: CLI tuple wins; stored is comma-separated string
    if not result.get('tags'):
        stored_tags = stored_config.get('tags')
        if stored_tags:
            result['tags'] = tuple(
                t.strip() for t in stored_tags.split(',') if t.strip()
            )

    return MergedConfig(
        config=result,
        env_manager=env_manager,
        stored_config=stored_config
    )


def attach_deploy_report_data(data: Dict[str, Any], deploy_report: Dict[str, Any]) -> Dict[str, Any]:
    """Attach shared deploy report metadata to JSON output payloads."""
    if not deploy_report:
        return data

    for key in (
        "disk_check",
        "failure_category",
        "source_manifest",
        "drift_report",
        "smoke_checks",
    ):
        if deploy_report.get(key) is not None:
            data[key] = deploy_report.get(key)
    data["recovery_attempted"] = deploy_report.get("recovery_attempted", False)
    data["recovery_succeeded"] = deploy_report.get("recovery_succeeded", False)
    data["stage_timings"] = deploy_report.get("stage_timings", {})
    return data


def resolve_existing_remote_target(
    branch_name: str,
    scp_target: Optional[str],
    domain: Optional[str],
    ip: Optional[str],
    api_token: Optional[str],
    json: bool,
) -> Tuple[str, Optional[str], Optional[str], Optional[str], Dict[str, Any]]:
    """Resolve stored config + CLI overrides to a concrete rsync/SSH target."""
    cli_target = scp_target
    cli_domain = domain or None

    merged_obj = load_and_merge_config(
        branch_name,
        {
            "scp_target": scp_target,
            "domain": domain,
            "ip": ip,
            "dns_token": api_token or "",
        },
    )
    merged = merged_obj.config
    stored_config = merged_obj.stored_config
    scp_target = merged["scp_target"]
    domain = merged["domain"] or None
    ip = merged["ip"] or None
    token = api_token or stored_config.get("dns_token")

    if cli_target:
        scp_target = cli_target
        if not json:
            log_info(f"Using rsync target from command line: {cli_target}")
    elif cli_domain:
        scp_target = cli_domain
        if not json:
            log_info(f"Using domain from command line: {cli_domain}")
    elif domain:
        scp_target = domain
        if not json:
            log_info(f"Using domain from config: {domain}")
    elif ip:
        scp_target = f"root@{ip}:/root"
    elif not scp_target:
        scp_target = stored_config.get("scp_target")
        if scp_target and not json:
            log_info(f"Using rsync target from config: {scp_target}")

    if not scp_target:
        raise ValueError("Rsync target required (--scp-target or --domain or --ip or stored config)")

    resolved = parse_rsync_target_or_droplet(scp_target, token)
    if resolved != scp_target and not json:
        log_info(f"Resolved rsync target: {scp_target} → {resolved}")
    return resolved, domain, ip, token, stored_config
