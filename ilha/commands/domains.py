"""
Domain management commands for ilha CLI.

This module provides commands for managing DNS domains including
creating, listing, and deleting DNS A records.
"""

from typing import Optional, List, Tuple
from ..core.dns_manager import DNSManager, parse_domain
from ..core import dns_providers  # noqa: F401 - Import to trigger registration
from ..utils.logging import log_info, log_success, log_warning, log_error, print_plain
from ..utils.confirmation import confirm_by_typing_name
from ..utils.json_output import JSONOutput
from ._constants import DNS_TOKEN_NOT_FOUND
from rich.table import Table
from rich.console import Console


class DomainCommands:
    """Manages domain operations for ilha CLI."""
    
    def __init__(self):
        """Initialize domain commands."""
        pass
    
    def create_domain(self, subdomain: str, domain: str, ip: str,
                     dns_token: Optional[str] = None, json: bool = False) -> bool:
        """Create a new DNS A record.
        
        Args:
            subdomain: Subdomain name (e.g., 'app') or '@' for root domain
            domain: Base domain (e.g., 'example.com')
            ip: IP address to point to
            dns_token: DNS API token
            json: Output as JSON
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Normalize "@" to empty string for root domain
            if subdomain == '@':
                subdomain = ''
            
            # Resolve API token
            token = DNSManager.resolve_dns_token(dns_token)
            if not token:
                if json:
                    JSONOutput.print_error(DNS_TOKEN_NOT_FOUND)
                else:
                    log_error(DNS_TOKEN_NOT_FOUND)
                return False
            
            # Create provider
            provider = DNSManager.create_provider('digitalocean', token)
            if not provider:
                if json:
                    JSONOutput.print_error("Failed to create DNS provider")
                else:
                    log_error("Failed to create DNS provider")
                return False
            
            full_domain = f"{subdomain}.{domain}" if subdomain else domain
            
            if not json:
                log_info(f"Creating DNS A record: {full_domain} -> {ip}")
            
            # Check if domain already exists
            exists, current_ip = provider.check_domain_exists(subdomain, domain)
            if exists:
                if json:
                    JSONOutput.print_error(f"DNS A record already exists for {full_domain} (points to {current_ip})")
                else:
                    log_error(f"DNS A record already exists for {full_domain} (points to {current_ip})")
                return False
            
            # Create DNS record
            success = provider.create_subdomain(subdomain, domain, ip)
            
            if not success:
                if json:
                    JSONOutput.print_error("Failed to create DNS A record")
                else:
                    log_error("Failed to create DNS A record")
                return False
            
            if json:
                result = {
                    "success": True,
                    "domain": {
                        "subdomain": subdomain,
                        "domain": domain,
                        "full_domain": full_domain,
                        "ip_address": ip
                    }
                }
                JSONOutput.print_json(result)
            else:
                log_success(f"DNS A record created successfully: {full_domain} -> {ip}")
            
            return True
            
        except Exception as e:
            if json:
                JSONOutput.print_error(f"Error creating DNS A record: {e}")
            else:
                log_error(f"Error creating DNS A record: {e}")
            return False
    
    def list_domains(self, domain: Optional[str] = None, dns_token: Optional[str] = None,
                    json: bool = False, csv: bool = False) -> bool:
        """List all DNS A records.
        
        Args:
            domain: Optional base domain to filter by
            dns_token: DNS API token
            json: Output as JSON
            csv: Output as CSV
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Resolve API token
            token = DNSManager.resolve_dns_token(dns_token)
            if not token:
                if json:
                    JSONOutput.print_error(DNS_TOKEN_NOT_FOUND)
                else:
                    log_error(DNS_TOKEN_NOT_FOUND)
                return False
            
            # Create provider
            provider = DNSManager.create_provider('digitalocean', token)
            if not provider:
                if json:
                    JSONOutput.print_error("Failed to create DNS provider")
                else:
                    log_error("Failed to create DNS provider")
                return False
            
            # Get all DNS A records
            all_records = []
            
            if domain:
                # List subdomains for specified domain
                domains_to_search = [domain]
            else:
                # Get all domains in the account
                if hasattr(provider, 'list_all_domains'):
                    domains_to_search = provider.list_all_domains()
                else:
                    if json:
                        JSONOutput.print_error("Provider does not support listing all domains. Please specify --domain")
                    else:
                        log_error("Provider does not support listing all domains. Please specify --domain")
                    return False
            
            # Get A records for each domain
            for search_domain in domains_to_search:
                records = self._get_domain_records(provider, search_domain)
                all_records.extend(records)
            
            if csv:
                # Output as CSV
                import csv
                import sys
                writer = csv.writer(sys.stdout)
                # Write header
                writer.writerow(["Subdomain", "Domain", "Full Domain", "IP Address", "TTL"])
                # Write rows
                for record in all_records:
                    subdomain, base_domain, ip, ttl = record
                    full_domain = f"{subdomain}.{base_domain}" if subdomain else base_domain
                    writer.writerow([
                        subdomain or "",
                        base_domain,
                        full_domain,
                        ip,
                        str(ttl) if ttl else ""
                    ])
                return True
            elif json:
                result = {
                    "success": True,
                    "records": [
                        {
                            "subdomain": subdomain or "",
                            "domain": base_domain,
                            "full_domain": f"{subdomain}.{base_domain}" if subdomain else base_domain,
                            "ip_address": ip,
                            "ttl": ttl
                        }
                        for subdomain, base_domain, ip, ttl in all_records
                    ]
                }
                JSONOutput.print_json(result)
            else:
                if not all_records:
                    print_plain("No DNS A records found")
                    return True
                
                # Create a formatted table
                console = Console()
                table = Table(
                    title=f"DNS A Records ({len(all_records)})",
                    show_header=True,
                    header_style="bold",
                    box=None,
                    padding=(0, 1)
                )
                
                table.add_column("Subdomain", style="cyan", min_width=15)
                table.add_column("Domain", style="green", min_width=20)
                table.add_column("Full Domain", style="yellow", min_width=25)
                table.add_column("IP Address", style="magenta", min_width=15)
                table.add_column("TTL", style="blue", min_width=8)
                
                for subdomain, base_domain, ip, ttl in all_records:
                    full_domain = f"{subdomain}.{base_domain}" if subdomain else base_domain
                    table.add_row(
                        subdomain or "(root)",
                        base_domain,
                        full_domain,
                        ip,
                        str(ttl) if ttl else "N/A"
                    )
                
                console.print(table)
            
            return True
            
        except Exception as e:
            if json:
                JSONOutput.print_error(f"Error listing DNS A records: {e}")
            else:
                log_error(f"Error listing DNS A records: {e}")
            return False
    
    def _get_domain_records(self, provider, domain: str) -> List[Tuple[str, str, str, Optional[int]]]:
        """Get all A records for a domain with details.
        
        Args:
            provider: DNS provider instance
            domain: Base domain name
            
        Returns:
            List of tuples: (subdomain, domain, ip, ttl)
        """
        records = []
        
        # Use the provider's internal method to get records
        # We need to make a direct API call to get TTL information
        if hasattr(provider, '_make_request'):
            response = provider._make_request('GET', f'/domains/{domain}/records')
            if response:
                try:
                    data = response.json()
                    domain_records = data.get('domain_records', [])
                    
                    for record in domain_records:
                        if record.get('type') == 'A':
                            subdomain = record.get('name', '')
                            # Normalize "@" to empty string for root domain
                            if subdomain == '@':
                                subdomain = ''
                            ip = record.get('data', '')
                            ttl = record.get('ttl')
                            records.append((subdomain, domain, ip, ttl))
                except (KeyError, ValueError) as e:
                    log_warning(f"Error parsing records for {domain}: {e}")
        
        return records
    
    def delete_domain(self, subdomain: str, domain: str, force: bool = False,
                     dns_token: Optional[str] = None, json: bool = False) -> bool:
        """Delete a DNS A record.
        
        Args:
            subdomain: Subdomain name (e.g., 'app') or '@' for root domain
            domain: Base domain (e.g., 'example.com')
            force: Skip confirmation
            dns_token: DNS API token
            json: Output as JSON
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Normalize "@" to empty string for root domain
            if subdomain == '@':
                subdomain = ''
            
            # Resolve API token
            token = DNSManager.resolve_dns_token(dns_token)
            if not token:
                if json:
                    JSONOutput.print_error(DNS_TOKEN_NOT_FOUND)
                else:
                    log_error(DNS_TOKEN_NOT_FOUND)
                return False
            
            # Create provider
            provider = DNSManager.create_provider('digitalocean', token)
            if not provider:
                if json:
                    JSONOutput.print_error("Failed to create DNS provider")
                else:
                    log_error("Failed to create DNS provider")
                return False
            
            full_domain = f"{subdomain}.{domain}" if subdomain else domain
            
            # Check if domain exists
            exists, current_ip = provider.check_domain_exists(subdomain, domain)
            if not exists:
                if json:
                    JSONOutput.print_error(f"DNS A record not found: {full_domain}")
                else:
                    log_error(f"DNS A record not found: {full_domain}")
                return False
            
            # Confirm deletion unless force flag is set
            if not force and not json:
                log_info(f"DNS A record to be deleted:")
                log_info(f"  Domain: {full_domain}")
                log_info(f"  IP Address: {current_ip}")
                log_info("")
                
                if not confirm_by_typing_name(
                    full_domain,
                    f"WARNING: This will permanently delete DNS A record '{full_domain}'. This action cannot be undone."
                ):
                    log_info("Cancelled DNS A record deletion")
                    return False
            
            # Delete DNS record
            if not json:
                log_info(f"Deleting DNS A record: {full_domain}")
            
            success = provider.delete_subdomain(subdomain, domain)
            
            if not success:
                if json:
                    JSONOutput.print_error(f"Failed to delete DNS A record: {full_domain}")
                else:
                    log_error(f"Failed to delete DNS A record: {full_domain}")
                return False
            
            if json:
                result = {
                    "success": True,
                    "message": f"DNS A record {full_domain} deleted successfully"
                }
                JSONOutput.print_json(result)
            else:
                log_success(f"DNS A record {full_domain} deleted successfully")
            
            return True
            
        except Exception as e:
            if json:
                JSONOutput.print_error(f"Error deleting DNS A record: {e}")
            else:
                log_error(f"Error deleting DNS A record: {e}")
            return False
    
    def get_domain_info(self, subdomain: str, domain: str, dns_token: Optional[str] = None,
                       json: bool = False) -> bool:
        """Get detailed information about a DNS A record.
        
        Args:
            subdomain: Subdomain name (e.g., 'app') or '@' for root domain
            domain: Base domain (e.g., 'example.com')
            dns_token: DNS API token
            json: Output as JSON
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Normalize "@" to empty string for root domain
            if subdomain == '@':
                subdomain = ''
            
            # Resolve API token
            token = DNSManager.resolve_dns_token(dns_token)
            if not token:
                if json:
                    JSONOutput.print_error(DNS_TOKEN_NOT_FOUND)
                else:
                    log_error(DNS_TOKEN_NOT_FOUND)
                return False
            
            # Create provider
            provider = DNSManager.create_provider('digitalocean', token)
            if not provider:
                if json:
                    JSONOutput.print_error("Failed to create DNS provider")
                else:
                    log_error("Failed to create DNS provider")
                return False
            
            full_domain = f"{subdomain}.{domain}" if subdomain else domain
            
            # Check if domain exists and get IP
            exists, ip = provider.check_domain_exists(subdomain, domain)
            
            if not exists:
                if json:
                    JSONOutput.print_error(f"DNS A record not found: {full_domain}")
                else:
                    log_error(f"DNS A record not found: {full_domain}")
                return False
            
            # Get additional details (TTL) if possible
            ttl = None
            if hasattr(provider, '_make_request'):
                response = provider._make_request('GET', f'/domains/{domain}/records')
                if response:
                    try:
                        data = response.json()
                        records = data.get('domain_records', [])
                        for record in records:
                            record_name = record.get('name', '')
                            # Normalize "@" to empty string for root domain
                            if record_name == '@':
                                record_name = ''
                            if (record.get('type') == 'A' and 
                                record_name == subdomain):
                                ttl = record.get('ttl')
                                break
                    except (KeyError, ValueError):
                        pass
            
            if json:
                result = {
                    "success": True,
                    "domain": {
                        "subdomain": subdomain or "",
                        "domain": domain,
                        "full_domain": full_domain,
                        "ip_address": ip,
                        "ttl": ttl
                    }
                }
                JSONOutput.print_json(result)
            else:
                print_plain(f"DNS A Record Information:")
                print_plain(f"  Subdomain: {subdomain or '(root)'}")
                print_plain(f"  Domain: {domain}")
                print_plain(f"  Full Domain: {full_domain}")
                print_plain(f"  IP Address: {ip}")
                if ttl:
                    print_plain(f"  TTL: {ttl}")
            
            return True
            
        except Exception as e:
            if json:
                JSONOutput.print_error(f"Error getting DNS A record info: {e}")
            else:
                log_error(f"Error getting DNS A record info: {e}")
            return False

