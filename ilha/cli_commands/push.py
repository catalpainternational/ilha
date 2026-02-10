"""
Push commands for ilha deployment operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, List

import click

from ilha.cli.helpers import add_json_option, add_verbose_option, command_wrapper
from ilha.commands.push import PushManager
from ilha.exceptions import ilhaCommandError
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import error_exit, log_success
from ilha.utils.validation import check_prerequisites, check_setup_or_prompt


def register_commands(cli) -> None:
    @cli.command("push")
    @click.argument("scp_target", required=False)
    @click.option("--branch-name", help="Branch/worktree name (auto-detected if not provided)")
    @click.option("--output-dir", type=click.Path(), default="./packages", help="Output directory for packages (default: ./packages)")
    @click.option("--keep-package", is_flag=True, default=False, help="Don't delete package after successful push")
    @click.option("--auto-import", is_flag=True, default=False, help="Automatically import package on remote server")
    @click.option("--domain", help="Domain for Caddy routing (subdomain.domain.tld)")
    @click.option("--ip", help="IP for HTTP-only routing (no TLS)")
    @click.option("--prepare-server", is_flag=True, default=False, help="Prepare server (install dependencies) before push")
    @click.option("--dns-token", help="Digital Ocean API token for DNS management")
    @click.option("--skip-dns-check", is_flag=True, default=False, help="Skip DNS management")
    @click.option("--create-droplet", is_flag=True, default=False, help="Create new droplet before pushing")
    @click.option("--droplet-name", help="Name for the droplet (required with --create-droplet)")
    @click.option("--droplet-region", help="Droplet region (default: from environment)")
    @click.option("--droplet-size", help="Droplet size (default: from environment)")
    @click.option("--droplet-image", help="Droplet image (default: from environment)")
    @click.option("--droplet-ssh-keys", multiple=True, help="SSH key IDs or fingerprints for droplet (can be specified multiple times)")
    @click.option("--resume", is_flag=True, default=False, help="Resume interrupted push (skip completed steps)")
    @click.option("--code-only", is_flag=True, default=False, help="[NOT IMPLEMENTED] Push code-only update (not full package)")
    @click.option("--build", is_flag=True, default=False, help="Rebuild Docker images on remote server")
    @click.option("--containers", help="Comma-separated list of 'worktree.container' patterns to push only specific containers")
    @click.option("--exclude-deps", multiple=True, help="Services to exclude from dependencies (can be specified multiple times)")
    @click.option("--vpc-uuid", help="VPC UUID for VPC deployment")
    @click.option("--use-staging-certificates", is_flag=True, default=False, help="Use Let's Encrypt staging certificates (doesn't count against rate limits)")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_setup=True, require_prerequisites=True)
    def push(
        scp_target: Optional[str],
        branch_name: Optional[str],
        output_dir: str,
        keep_package: bool,
        auto_import: bool,
        domain: Optional[str],
        ip: Optional[str],
        prepare_server: bool,
        dns_token: Optional[str],
        skip_dns_check: bool,
        create_droplet: bool,
        droplet_name: Optional[str],
        droplet_region: Optional[str],
        droplet_size: Optional[str],
        droplet_image: Optional[str],
        droplet_ssh_keys: tuple,
        resume: bool,
        code_only: bool,
        build: bool,
        containers: Optional[str],
        exclude_deps: tuple,
        vpc_uuid: Optional[str],
        json: bool,
        use_staging_certificates: bool,
    ):
        """Push package to remote server for deployment.
        
        Exports a ilha package and transfers it to a remote server.
        Optionally prepares the server, manages DNS, and automatically imports the package.
        """
        if domain and ip:
            raise ilhaCommandError("Options --domain and --ip are mutually exclusive")
        
        if create_droplet and not droplet_name:
            raise ilhaCommandError("--droplet-name is required when --create-droplet is specified")
        
        try:
            push_manager = PushManager()
            
            # Convert tuple to list for exclude_deps
            exclude_deps_list = list(exclude_deps) if exclude_deps else None
            
            # Convert tuple to list for droplet_ssh_keys
            droplet_ssh_keys_list = list(droplet_ssh_keys) if droplet_ssh_keys else None
            
            success = push_manager.push_package(
                branch_name=branch_name,
                scp_target=scp_target,
                output_dir=Path(output_dir),
                keep_package=keep_package,
                auto_import=auto_import,
                domain=domain,
                ip=ip,
                prepare_server=prepare_server,
                dns_token=dns_token,
                skip_dns_check=skip_dns_check,
                create_droplet=create_droplet,
                droplet_name=droplet_name,
                droplet_region=droplet_region,
                droplet_size=droplet_size,
                droplet_image=droplet_image,
                droplet_ssh_keys=droplet_ssh_keys_list,
                resume=resume,
                code_only=code_only,
                build=build,
                containers=containers,
                exclude_deps=exclude_deps_list,
                vpc_uuid=vpc_uuid,
                droplet_info=None,  # Will be set if droplet is created
                central_droplet_info=None,  # Will be set if VPC deployment
                use_staging_certificates=use_staging_certificates,
            )
            
            if not success:
                raise ilhaCommandError("Push operation failed")
            
            log_success("Push operation completed successfully")
            
            if json:
                return JSONOutput.success("Push completed", {
                    "branch_name": branch_name,
                    "scp_target": scp_target,
                    "domain": domain,
                    "ip": ip
                })
                
        except Exception as exc:
            if json:
                JSONOutput.print_error(f"Error pushing package: {exc}")
            else:
                error_exit(f"Error pushing package: {exc}")

