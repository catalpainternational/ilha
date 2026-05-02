"""
Domain management commands.
"""

from __future__ import annotations

from typing import Optional

import click

from ilha.cli.helpers import add_json_option, add_verbose_option, command_wrapper
from ilha.commands.domains import DomainCommands
from ilha.exceptions import ilhaCommandError
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import error_exit, log_success
from ilha.utils.validation import check_prerequisites_no_git


def register_commands(cli) -> None:
    @cli.group()
    @add_verbose_option
    def domains():
        """Manage DNS domains, zones, and DNS A records."""

    @domains.command("create")
    @click.argument("subdomain")
    @click.argument("domain")
    @click.argument("ip")
    @click.option("--dns-token", help="DNS API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_setup=False, require_prerequisites=False)
    def domains_create(subdomain: str, domain: str, ip: str, dns_token: Optional[str], json: bool):
        check_prerequisites_no_git()
        domain_commands = DomainCommands()
        success = domain_commands.create_domain(
            subdomain=subdomain,
            domain=domain,
            ip=ip,
            dns_token=dns_token,
            json=json,
        )
        if not success:
            raise ilhaCommandError(f"Failed to create DNS A record: {subdomain}.{domain}")
        log_success(f"Created DNS A record: {subdomain}.{domain}")
        if json:
            return JSONOutput.success("DNS record created", {"domain": f"{subdomain}.{domain}", "ip": ip})

    @domains.command("list")
    @click.option("--domain", help="Base domain to filter by (optional, lists all domains if not provided)")
    @click.option("--dns-token", help="DNS API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @click.option("--as-json", "--json", "output_json", is_flag=True, default=False, help="Output results as JSON format")
    @click.option("--as-csv", "output_csv", is_flag=True, default=False, help="Output results as CSV format")
    @add_verbose_option
    def domains_list(domain: Optional[str], dns_token: Optional[str], output_json: bool, output_csv: bool):
        try:
            check_prerequisites_no_git()
            domain_commands = DomainCommands()
            success = domain_commands.list_domains(
                domain=domain,
                dns_token=dns_token,
                json=output_json,
                csv=output_csv,
            )
            if not success:
                if output_json or output_csv:
                    JSONOutput.print_error("Failed to list DNS A records")
                else:
                    error_exit("Failed to list DNS A records")
        except Exception as exc:
            if output_json or output_csv:
                JSONOutput.print_error(f"Error listing DNS A records: {exc}")
            else:
                error_exit(f"Error listing DNS A records: {exc}")

    @domains.command("zones")
    @click.option("--dns-token", help="DNS API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @click.option("--as-json", "--json", "output_json", is_flag=True, default=False, help="Output results as JSON format")
    @click.option("--as-csv", "output_csv", is_flag=True, default=False, help="Output results as CSV format")
    @add_verbose_option
    def domains_zones(dns_token: Optional[str], output_json: bool, output_csv: bool):
        try:
            check_prerequisites_no_git()
            domain_commands = DomainCommands()
            success = domain_commands.list_zones(
                dns_token=dns_token,
                json=output_json,
                csv=output_csv,
            )
            if not success:
                if output_json or output_csv:
                    JSONOutput.print_error("Failed to list DNS zones")
                else:
                    error_exit("Failed to list DNS zones")
        except Exception as exc:
            if output_json or output_csv:
                JSONOutput.print_error(f"Error listing DNS zones: {exc}")
            else:
                error_exit(f"Error listing DNS zones: {exc}")

    @domains.command("delete")
    @click.argument("full_domain")
    @click.option("--force", is_flag=True, default=False, help="Skip confirmation prompt")
    @click.option("--dns-token", help="DNS API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_setup=False, require_prerequisites=False)
    def domains_delete(full_domain: str, force: bool, dns_token: Optional[str], json: bool):
        from ilha.core.dns_manager import parse_domain

        try:
            subdomain, domain = parse_domain(full_domain)
        except ValueError as exc:
            raise ilhaCommandError(f"Invalid domain format: {exc}")

        check_prerequisites_no_git()
        domain_commands = DomainCommands()
        success = domain_commands.delete_domain(
            subdomain=subdomain,
            domain=domain,
            force=force,
            dns_token=dns_token,
            json=json,
        )
        if not success:
            raise ilhaCommandError(f"Failed to delete DNS A record: {full_domain}")
        log_success(f"Deleted DNS A record: {full_domain}")
        if json:
            return JSONOutput.success("DNS record deleted", {"domain": full_domain})

    @domains.command("info")
    @click.argument("full_domain")
    @click.option("--dns-token", help="DNS API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_setup=False, require_prerequisites=False)
    def domains_info(full_domain: str, dns_token: Optional[str], json: bool):
        from ilha.core.dns_manager import parse_domain

        try:
            subdomain, domain = parse_domain(full_domain)
        except ValueError as exc:
            raise ilhaCommandError(f"Invalid domain format: {exc}")

        check_prerequisites_no_git()
        domain_commands = DomainCommands()
        success = domain_commands.get_domain_info(
            subdomain=subdomain,
            domain=domain,
            dns_token=dns_token,
            json=json,
        )
        if not success:
            raise ilhaCommandError(f"Failed to get DNS A record info: {full_domain}")


