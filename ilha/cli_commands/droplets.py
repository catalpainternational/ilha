"""
DigitalOcean droplet management commands.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import click

from ilha.cli.helpers import add_json_option, add_verbose_option
from ilha.commands.droplets import DropletCommands
from ilha.commands.push.push_manager import PushManager
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import (
    error_exit,
    format_elapsed_time,
    log_error,
    log_info,
    log_success,
    log_warning,
    print_plain,
)
from ilha.utils.path_utils import detect_execution_context, get_worktree_branch_name
from ilha.utils.validation import (
    check_prerequisites,
    check_prerequisites_no_git,
    check_setup_or_prompt,
)


def _auto_detect_branch_name(branch_name: Optional[str], json: bool) -> str:
    """Autodetect the branch name when not explicitly provided."""
    if branch_name:
        return branch_name

    if not json:
        log_info("Branch name not provided, attempting auto-detection...")

    worktree_path, detected_branch, is_worktree = detect_execution_context()
    if is_worktree and detected_branch:
        return detected_branch

    try:
        from ilha.config.settings import get_project_root

        project_root = get_project_root()
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
            cwd=project_root,
        )
        branch = result.stdout.strip()
        if branch:
            return branch
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    current_path = Path.cwd()
    if "worktrees" in str(current_path):
        branch = get_worktree_branch_name(current_path)
        if branch:
            return branch

    message = (
        "Could not detect branch name. Please specify branch_name as argument or use --domain "
        "to auto-detect from subdomain."
    )
    if json:
        JSONOutput.print_error(message)
    else:
        error_exit(message)
    return ""


def _resolve_droplet_name(branch_name: str, domain: Optional[str], json: bool) -> str:
    """Resolve droplet name from branch/domain settings."""
    if not domain:
        if not json:
            log_info(f"Using branch name '{branch_name}' as droplet name")
        return branch_name

    try:
        from ilha.core.dns_manager import parse_domain

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


def _resolve_host_to_ip(host: str, push_manager, api_token: Optional[str] = None) -> str:
    """Resolve host (IP, domain, droplet ID, or droplet name) to IP address.
    
    DRY: Reuses PushManager._resolve_server_ip() for domain/IP DNS resolution.
    Only adds new logic for droplet ID/name resolution.
    
    Args:
        host: Host identifier (IP, domain, droplet ID, or droplet name)
        push_manager: PushManager instance for DNS resolution
        api_token: Digital Ocean API token for droplet lookup
        
    Returns:
        IP address string
        
    Raises:
        ValueError: If host cannot be resolved as domain, droplet ID, or droplet name
    """
    import re
    
    # First try: Check if already an IP address
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', host):
        return host
    
    # Second try: Use existing PushManager method for domain DNS resolution (reuses existing code!)
    try:
        ip = push_manager._resolve_server_ip(host)
        # If it resolved successfully to an IP (different from host), return it
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip) and ip != host:
            return ip
        # If it returned the host unchanged (DNS failed), continue to droplet lookup
        if ip == host:
            pass  # Fall through to droplet resolution
        else:
            # Shouldn't happen, but if we get here, return the result
            return ip
    except Exception:
        pass  # Fall through to droplet resolution
    
    # Second try: Resolve as droplet ID or name (new logic, but minimal)
    droplet_commands = DropletCommands()
    droplet_id, error = droplet_commands._resolve_droplet_identifier(host, api_token)
    if error:
        raise ValueError(f"Could not resolve '{host}' as domain, droplet ID, or droplet name: {error}")
    
    provider, _ = droplet_commands._get_provider(api_token)
    if not provider:
        raise ValueError(f"Failed to create droplet provider: {error}")
    
    droplet = provider.get_droplet(droplet_id)
    if not droplet or not droplet.ip_address:
        raise ValueError(f"Droplet {host} has no IP address")
    
    return droplet.ip_address


def _parse_scp_target_or_droplet(arg: str, api_token: Optional[str] = None) -> str:
    """Parse progressive SCP target patterns and resolve to full SCP target.
    
    Supports:
    - Full SCP target: `user@host:path` → use as-is
    - Username + domain: `user@example.com` → resolve domain, construct `user@<ip>:/root`
    - Domain/IP + path: `example.com:/path` or `192.168.1.100:/path` → construct `root@<ip>:<path>`
    - Domain/IP only: `example.com` or `192.168.1.100` → construct `root@<ip>:/root`
    - Droplet ID: `12345678` → resolve to IP, construct `root@<ip>:/root`
    - Droplet name: `my-app` → resolve to IP, construct `root@<ip>:/root`
    
    DRY: Reuses PushManager._resolve_server_ip() for domain/IP resolution.
    
    Args:
        arg: SCP target or droplet identifier
        api_token: Digital Ocean API token for droplet lookup
        
    Returns:
        Full SCP target string in format user@ip:path
    """
    from ..commands.push.push_manager import PushManager
    
    # Full SCP target format (contains both @ and : with @ before :)
    if '@' in arg and ':' in arg:
        at_pos = arg.index('@')
        colon_pos = arg.index(':')
        if at_pos < colon_pos:
            return arg  # Backward compatible: full SCP target
    
    push_manager = PushManager()
    
    # Username + domain/IP: `user@example.com` or `user@192.168.1.100`
    if '@' in arg and ':' not in arg:
        username, host = arg.split('@', 1)
        ip = _resolve_host_to_ip(host, push_manager, api_token)
        return f"{username}@{ip}:/root"
    
    # Domain/IP + path: `example.com:/path` or `192.168.1.100:/path`
    if ':' in arg and '@' not in arg:
        host, path = arg.rsplit(':', 1)
        ip = _resolve_host_to_ip(host, push_manager, api_token)
        return f"root@{ip}:{path}"
    
    # Just host (domain, IP, droplet ID, or droplet name)
    ip = _resolve_host_to_ip(arg, push_manager, api_token)
    return f"root@{ip}:/root"


def _log_creation_context(
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


def register_commands(cli) -> None:
    @cli.group()
    @add_verbose_option
    def droplet():
        """Manage DigitalOcean droplets."""

    def _generate_unique_droplet_name(base_name: str, provider) -> str:
        existing_droplets = provider.list_droplets()
        existing_names = {droplet.name for droplet in existing_droplets}
        if base_name not in existing_names:
            return base_name
        counter = 1
        while True:
            candidate_name = f"{base_name}-{counter}"
            if candidate_name not in existing_names:
                return candidate_name
            counter += 1

    @droplet.command("create")
    @click.argument("branch_name", required=False)
    @click.option("--region", help="Droplet region (default: nyc1 or from DIGITALOCEAN_REGION env var)")
    @click.option(
        "--size",
        help='Droplet size slug (e.g., s-1vcpu-1gb, s-2vcpu-4gb). Use "ilha droplet sizes" to list all available sizes. Default: s-1vcpu-1gb or from DIGITALOCEAN_SIZE env var',
    )
    @click.option("--image", help="Droplet image (default: ubuntu-22-04-x64 or from DIGITALOCEAN_IMAGE env var)")
    @click.option("--ssh-keys", type=str, help="SSH key names (comma-separated, e.g., anders,peter)")
    @click.option("--tags", multiple=True, help="Tags for the droplet (can be specified multiple times)")
    @click.option("--wait", is_flag=True, default=False, help="Wait for droplet to be ready before returning")
    @click.option("--api-token", help="DigitalOcean API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @click.option(
        "--create-only",
        is_flag=True,
        default=False,
        help="Only create droplet, do not push environment (default: creates droplet and pushes environment)",
    )
    @click.option("--scp-target", help="SCP target in format username@server:path (optional, defaults to root@<droplet-ip>:/root)")
    @click.option("--output-dir", type=click.Path(), default="./packages", help="Temporary package location (default: ./packages)")
    @click.option("--keep-package", is_flag=True, default=False, help="Keep package file after successful push (default: delete after push)")
    @click.option(
        "--no-auto-import",
        is_flag=True,
        default=False,
        help="Skip automatic import and start on remote server after push (default: auto-import is enabled)",
    )
    @click.option("--prepare-server", is_flag=True, default=False, help="Check remote server for required dependencies before push")
    @click.option(
        "--domain",
        help="Domain override for remote import (subdomain.domain.tld). DNS A record will be automatically created if it does not exist.",
    )
    @click.option("--ip", help="IP override for remote import (HTTP-only, no TLS)")
    @click.option("--dns-token", help="DigitalOcean API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @click.option("--skip-dns-check", is_flag=True, default=False, help="Skip DNS validation and management")
    @click.option("--resume", is_flag=True, default=False, help="Resume a failed push operation by detecting what's already completed")
    @click.option("--code-only", is_flag=True, default=False, help="Push code-only update to pre-existing server")
    @click.option("--build", is_flag=True, default=False, help="Rebuild Docker images on the remote server after deployment")
    @click.option(
        "--containers",
        help="Comma-separated list of worktree.container patterns to push only specific containers and their volumes (e.g., feature-auth.db,feature-auth.redis)",
    )
    @click.option(
        "--exclude-deps",
        help="Comma-separated list of service names to exclude from dependency resolution (e.g., db,redis). Useful when deploying workers that connect to remote services.",
    )
    @click.option("--vpc-uuid", help="VPC UUID for the droplet. If not provided, will use the default VPC for the region.")
    @click.option("--central-droplet-name", help="Name of central droplet to reuse VPC UUID from (for worker deployments)")
    @click.option("--use-staging-certificates", is_flag=True, default=False, help="Use Let's Encrypt staging certificates (doesn't count against rate limits)")
    @add_json_option
    @add_verbose_option
    def droplet_create(
        branch_name: Optional[str],
        region: Optional[str],
        size: Optional[str],
        image: Optional[str],
        ssh_keys: Optional[str],
        tags: tuple,
        wait: bool,
        api_token: Optional[str],
        json: bool,
        create_only: bool,
        scp_target: Optional[str],
        output_dir: str,
        keep_package: bool,
        no_auto_import: bool,
        prepare_server: bool,
        domain: str,
        ip: str,
        dns_token: str,
        skip_dns_check: bool,
        resume: bool,
        code_only: bool,
        build: bool,
        containers: Optional[str],
        exclude_deps: Optional[str],
        vpc_uuid: Optional[str],
        central_droplet_name: Optional[str],
        use_staging_certificates: bool,
    ):
        from ilha.core.droplet_manager import DropletManager
        from ilha.utils.logging import format_elapsed_time

        start_time = time.time()
        try:
            branch_name = _auto_detect_branch_name(branch_name, json)
            if not branch_name:
                return
            
            # Load stored droplet config and apply to parameters if not provided via CLI
            from ..core.environment_manager import EnvironmentManager
            from ..config.settings import get_project_root
            
            env_manager = EnvironmentManager(project_root=get_project_root())
            stored_config = env_manager.get_droplet_config(branch_name)
            
            # Apply stored config values if CLI arguments are not provided
            # Priority: CLI argument > stored config > global defaults
            if not region:
                region = stored_config.get('region')
            if not size:
                size = stored_config.get('size')
            if not image:
                image = stored_config.get('image')
            if not ssh_keys:
                ssh_keys = stored_config.get('ssh_keys')
            if not tags:
                stored_tags = stored_config.get('tags')
                if stored_tags:
                    tags = tuple(t.strip() for t in stored_tags.split(",") if t.strip())
            if not vpc_uuid:
                vpc_uuid = stored_config.get('vpc_uuid')
            if not central_droplet_name:
                central_droplet_name = stored_config.get('central_droplet_name')
            if not scp_target:
                scp_target = stored_config.get('scp_target')
            if not domain:
                domain = stored_config.get('domain') or ''
            if not ip:
                ip = stored_config.get('ip') or ''
            if not dns_token:
                dns_token = stored_config.get('dns_token') or ''
            if output_dir == "./packages":  # Only use stored if default value
                stored_output_dir = stored_config.get('output_dir')
                if stored_output_dir:
                    output_dir = stored_output_dir
            if not keep_package:  # Only use stored if False (default)
                stored_keep_package = stored_config.get('keep_package')
                if stored_keep_package:
                    keep_package = stored_keep_package.lower() in ('true', '1', 'yes')
            if not prepare_server:  # Only use stored if False (default)
                stored_prepare_server = stored_config.get('prepare_server')
                if stored_prepare_server:
                    prepare_server = stored_prepare_server.lower() in ('true', '1', 'yes')
            if not resume:  # Only use stored if False (default)
                stored_resume = stored_config.get('resume')
                if stored_resume:
                    resume = stored_resume.lower() in ('true', '1', 'yes')
            if not build:  # Only use stored if False (default)
                stored_build = stored_config.get('build')
                if stored_build:
                    build = stored_build.lower() in ('true', '1', 'yes')
            if not containers:
                containers = stored_config.get('containers')
            if not exclude_deps:
                exclude_deps = stored_config.get('exclude_deps')
            
            droplet_name = _resolve_droplet_name(branch_name, domain, json)

            defaults = DropletManager.get_droplet_defaults()
            resolved_region = region or defaults.get("region", "nyc1")
            resolved_size = size or defaults.get("size", "s-1vcpu-1gb")
            resolved_image = image or defaults.get("image", "ubuntu-22-04-x64")
            _log_creation_context(
                droplet_name,
                resolved_region,
                resolved_size,
                resolved_image,
                ssh_keys,
                tags,
                containers,
                exclude_deps,
                json,
            )

            check_prerequisites_no_git()
            droplet_commands = DropletCommands()
            ssh_keys_list = [k.strip() for k in ssh_keys.split(",")] if ssh_keys else None
            tags_list = list(tags) if tags else None

            resolved_vpc_uuid = vpc_uuid
            central_droplet = None
            provider = None
            token = None
            if central_droplet_name and not vpc_uuid:
                if not json:
                    log_info("Resolving VPC configuration...")
                    log_info(f"Looking up VPC UUID from central droplet: {central_droplet_name}")
                token = DropletManager.resolve_droplet_token(api_token or dns_token)
                if token:
                    provider = DropletManager.create_provider("digitalocean", token)
                    if provider:
                        droplets = provider.list_droplets()
                        for current_droplet in droplets:
                            if current_droplet.name == central_droplet_name:
                                central_droplet = current_droplet
                                break
                        if central_droplet and central_droplet.vpc_uuid:
                            resolved_vpc_uuid = central_droplet.vpc_uuid
                            if not json:
                                log_info(f"Found central droplet '{central_droplet_name}' in region {central_droplet.region}")
                                if central_droplet.region != resolved_region:
                                    log_warning("⚠️  Region mismatch detected!")
                                    log_warning(f"   Central droplet '{central_droplet_name}' is in region: {central_droplet.region}")
                                    log_warning(f"   Worker droplet will be created in region: {resolved_region}")
                                    log_warning("   VPCs are region-specific. Ensure both droplets are in the same region.")
                                log_info(f"Using VPC UUID from central droplet: {resolved_vpc_uuid}")
                                if central_droplet.private_ip_address:
                                    log_info(f"Central droplet private IP: {central_droplet.private_ip_address}")
                        elif not json:
                            log_warning(f"Central droplet '{central_droplet_name}' not found or has no VPC UUID, using default VPC")
            elif not json and not vpc_uuid:
                log_info("Resolving VPC configuration...")
                log_info(f"Using default VPC for region {resolved_region}")

            central_droplet_info = central_droplet if central_droplet_name and central_droplet else None

            if not provider:
                token = DropletManager.resolve_droplet_token(api_token or dns_token)
                if token:
                    provider = DropletManager.create_provider("digitalocean", token)

            if provider:
                unique_droplet_name = _generate_unique_droplet_name(droplet_name, provider)
                if unique_droplet_name != droplet_name:
                    if not json:
                        log_info(f"Droplet name '{droplet_name}' already exists, using '{unique_droplet_name}' instead")
                    droplet_name = unique_droplet_name

            wait_for_push = not create_only
            success = droplet_commands.create_droplet(
                name=droplet_name,
                region=resolved_region,
                size=resolved_size,
                image=resolved_image,
                ssh_keys=ssh_keys_list,
                tags=tags_list,
                wait=wait or wait_for_push,
                api_token=api_token,
                json=json,
                containers=containers,
                vpc_uuid=resolved_vpc_uuid,
            )
            if not success:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error(f"Failed to create droplet: {droplet_name}")
                else:
                    error_exit(f"Failed to create droplet: {droplet_name}")
                return

            if create_only:
                # Save droplet creation configuration even if not pushing
                config_to_save = {
                    'region': resolved_region,
                    'size': resolved_size,
                    'image': resolved_image,
                    'ssh_keys': ssh_keys,
                    'tags': ','.join(tags) if tags else None,
                    'vpc_uuid': resolved_vpc_uuid,
                    'central_droplet_name': central_droplet_name,
                }
                env_manager.save_droplet_config(branch_name, config_to_save)
                
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                return

            if not json:
                log_info("")
                log_info("Droplet created successfully. Preparing to push environment...")
                log_info(f"Will push worktree '{branch_name}' to {droplet_name}")
                if containers:
                    log_info(f"Selected containers: {containers}")
                if exclude_deps:
                    exclude_deps_preview = [d.strip() for d in exclude_deps.split(",")]
                    log_info(f"Excluding dependencies: {', '.join(exclude_deps_preview)}")
                log_info("")

            token = DropletManager.resolve_droplet_token(api_token or dns_token)
            if not token:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error("Digital Ocean API token not found for push operation")
                else:
                    error_exit("Digital Ocean API token not found for push operation")
                return

            provider = DropletManager.create_provider("digitalocean", token)
            if not provider:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error("Failed to create droplet provider for push")
                else:
                    error_exit("Failed to create droplet provider for push")
                return

            droplets = provider.list_droplets()
            found_droplet = None
            for droplet_obj in droplets:
                if droplet_obj.name == droplet_name:
                    found_droplet = droplet_obj
                    break
            droplet_info = found_droplet

            if not droplet_info or not droplet_info.ip_address:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error(f"Droplet {droplet_name} created but IP address not available")
                else:
                    error_exit(f"Droplet {droplet_name} created but IP address not available")
                return

            if scp_target:
                push_manager = PushManager()
                if not push_manager._validate_scp_target(scp_target):
                    elapsed_time = time.time() - start_time
                    if not json:
                        print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                    if json:
                        JSONOutput.print_error(f"Invalid SCP target format: {scp_target}")
                    else:
                        error_exit(f"Invalid SCP target format: {scp_target}")
                    return
                username, _, remote_path = push_manager._parse_scp_target(scp_target)
            else:
                username = "root"
                remote_path = "/root"
            final_scp_target = f"{username}@{droplet_info.ip_address}:{remote_path}"

            check_setup_or_prompt()
            check_prerequisites()

            if domain and ip:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error("Options --domain and --ip are mutually exclusive")
                else:
                    error_exit("Options --domain and --ip are mutually exclusive")
                return

            push_manager = PushManager()
            droplet_ssh_keys_list = ssh_keys_list if ssh_keys_list else None
            exclude_deps_list = [d.strip() for d in exclude_deps.split(",")] if exclude_deps else None

            try:
                success = push_manager.push_package(
                    branch_name=branch_name,
                    scp_target=final_scp_target,
                    output_dir=Path(output_dir),
                    keep_package=keep_package,
                    auto_import=not no_auto_import,
                    containers=containers,
                    exclude_deps=exclude_deps_list,
                    domain=domain,
                    ip=ip,
                    prepare_server=prepare_server,
                    dns_token=dns_token or api_token,
                    skip_dns_check=skip_dns_check,
                    create_droplet=True,
                    droplet_name=None,
                    droplet_region=None,
                    droplet_size=None,
                    droplet_image=None,
                    droplet_ssh_keys=droplet_ssh_keys_list,
                    resume=resume,
                    code_only=code_only,
                    build=build,
                    droplet_info=droplet_info,
                    central_droplet_info=central_droplet_info,
                    use_staging_certificates=use_staging_certificates,
                )
            except click.exceptions.ClickException as exc:
                import traceback

                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error(f"Click error during push: {exc}")
                else:
                    error_exit(f"Click error during push: {exc}\n{traceback.format_exc()}")
                return
            except Exception as exc:
                import traceback

                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error(f"Error during push: {exc}")
                else:
                    error_exit(f"Error during push: {exc}\n{traceback.format_exc()}")
                return

            elapsed_time = time.time() - start_time
            if not success:
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error("Failed to push package to droplet")
                else:
                    error_exit("Failed to push package to droplet")
            else:
                # Save droplet configuration for future use
                config_to_save = {
                    'region': resolved_region,
                    'size': resolved_size,
                    'image': resolved_image,
                    'ssh_keys': ssh_keys,
                    'tags': ','.join(tags) if tags else None,
                    'vpc_uuid': resolved_vpc_uuid,
                    'central_droplet_name': central_droplet_name,
                    'scp_target': final_scp_target,
                    'domain': domain if domain else None,
                    'ip': ip if ip else None,
                    'dns_token': dns_token if dns_token else None,
                    'output_dir': str(output_dir),
                    'keep_package': keep_package,
                    'prepare_server': prepare_server,
                    'resume': resume,
                    'build': build,
                    'containers': containers,
                    'exclude_deps': exclude_deps,
                }
                env_manager.save_droplet_config(branch_name, config_to_save)
                
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                
                # Determine deployment URL
                url = None
                if domain:
                    url = f"https://{domain}"
                elif ip:
                    url = f"http://{ip}"
                elif droplet_info and droplet_info.ip_address:
                    url = f"http://{droplet_info.ip_address}"
                
                if json:
                    data = {"droplet_name": droplet_name}
                    if url:
                        data["url"] = url
                    if droplet_info:
                        data["ip_address"] = droplet_info.ip_address
                    JSONOutput.print_success("Droplet created and package pushed successfully", data=data)
                else:
                    log_success("Droplet created and package pushed successfully")
                    
                    # Output deployment URL
                    if url:
                        if domain:
                            log_success(f"🌐 Your site is available at: {url}")
                        elif ip:
                            log_success(f"🌐 Your site is available at: {url}")
                        else:
                            log_info(f"🌐 Your site is available at: {url}")
                            log_info("   (Use --domain to enable HTTPS)")
                        print_plain("")
        except Exception as exc:
            from ilha.utils.logging import format_elapsed_time

            elapsed_time = time.time() - start_time
            if not json:
                print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
            if json:
                JSONOutput.print_error(f"Error creating droplet: {exc}")
            else:
                error_exit(f"Error creating droplet: {exc}")

    @droplet.command("list")
    @click.option("--api-token", help="DigitalOcean API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @click.option("--as-json", "--json", "output_json", is_flag=True, default=False, help="Output results as JSON format")
    @click.option("--as-csv", "output_csv", is_flag=True, default=False, help="Output results as CSV format")
    @add_verbose_option
    def droplet_list(api_token: Optional[str], output_json: bool, output_csv: bool):
        try:
            check_prerequisites_no_git()
            droplet_commands = DropletCommands()
            success = droplet_commands.list_droplets(api_token=api_token, json=output_json, csv=output_csv)
            if not success:
                if output_json or output_csv:
                    JSONOutput.print_error("Failed to list droplets")
                else:
                    error_exit("Failed to list droplets")
        except Exception as exc:
            if output_json or output_csv:
                JSONOutput.print_error(f"Error listing droplets: {exc}")
            else:
                error_exit(f"Error listing droplets: {exc}")

    @droplet.command("sizes")
    @click.option("--api-token", help="DigitalOcean API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @click.option("--as-json", "--json", "output_json", is_flag=True, default=False, help="Output results as JSON format")
    @click.option("--as-csv", "output_csv", is_flag=True, default=False, help="Output results as CSV format")
    @add_verbose_option
    def droplet_sizes(api_token: Optional[str], output_json: bool, output_csv: bool):
        try:
            check_prerequisites_no_git()
            droplet_commands = DropletCommands()
            success = droplet_commands.list_sizes(api_token=api_token, json=output_json, csv=output_csv)
            if not success:
                if output_json or output_csv:
                    JSONOutput.print_error("Failed to list droplet sizes")
                else:
                    error_exit("Failed to list droplet sizes")
        except Exception as exc:
            if output_json or output_csv:
                JSONOutput.print_error(f"Error listing droplet sizes: {exc}")
            else:
                error_exit(f"Error listing droplet sizes: {exc}")

    @droplet.command("destroy")
    @click.argument("droplet_ids", type=str)
    @click.option("--force", is_flag=True, default=False, help="Skip confirmation prompts")
    @click.option("--only-droplet", is_flag=True, default=False, help="Only destroy droplet, skip DNS deletion")
    @click.option("--only-domain", is_flag=True, default=False, help="Only destroy DNS records, skip droplet deletion")
    @click.option("--domain", help="Domain name for DNS deletion (optional, auto-detects if not provided)")
    @click.option("--api-token", help="DigitalOcean API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @click.option("--dns-token", help="DNS API token (if different from droplet token)")
    @add_json_option
    @add_verbose_option
    def droplet_destroy(
        droplet_ids: str,
        force: bool,
        only_droplet: bool,
        only_domain: bool,
        domain: Optional[str],
        api_token: Optional[str],
        dns_token: Optional[str],
        json: bool,
    ):
        try:
            if only_droplet and only_domain:
                if json:
                    JSONOutput.print_error("Cannot specify both --only-droplet and --only-domain")
                else:
                    error_exit("Cannot specify both --only-droplet and --only-domain")
                return

            check_prerequisites_no_git()
            droplet_commands = DropletCommands()
            droplet_id_list, resolution_errors = droplet_commands._resolve_droplet_identifiers(droplet_ids, api_token, json)

            if resolution_errors:
                if not droplet_id_list:
                    if json:
                        error_summary = {
                            "success": False,
                            "error": "Failed to resolve any droplet identifiers",
                            "errors": resolution_errors,
                        }
                        JSONOutput.print_json(error_summary)
                    else:
                        error_exit(
                            f"Failed to resolve droplet identifiers: {', '.join([e['error'] for e in resolution_errors])}"
                        )
                    return
                elif not json:
                    for error_info in resolution_errors:
                        log_error(f"Failed to resolve '{error_info['identifier']}': {error_info['error']}")

            if not droplet_id_list:
                if json:
                    JSONOutput.print_error("No valid droplet identifiers provided")
                else:
                    error_exit("No valid droplet identifiers provided")
                return

            results = []
            success_count = 0
            failure_count = 0
            total = len(droplet_id_list)
            suppress_json = json and total > 1

            for droplet_id in droplet_id_list:
                try:
                    success = droplet_commands.destroy_droplet(
                        droplet_id=droplet_id,
                        force=force,
                        api_token=api_token,
                        json=json if not suppress_json else False,
                        only_droplet=only_droplet,
                        only_domain=only_domain,
                        domain=domain,
                        dns_token=dns_token,
                    )
                    if success:
                        success_count += 1
                        results.append({"droplet_id": droplet_id, "success": True})
                    else:
                        failure_count += 1
                        results.append({"droplet_id": droplet_id, "success": False, "error": "Failed to destroy droplet"})
                except Exception as exc:
                    failure_count += 1
                    results.append({"droplet_id": droplet_id, "success": False, "error": str(exc)})
                    if not json:
                        log_error(f"Error destroying droplet {droplet_id}: {exc}")

            if json:
                if suppress_json:
                    result = {
                        "success": failure_count == 0,
                        "total": total,
                        "succeeded": success_count,
                        "failed": failure_count,
                        "results": results,
                    }
                    JSONOutput.print_json(result)

            if not json and total > 1:
                if success_count == total:
                    log_success(f"Successfully destroyed {success_count} of {total} droplet(s)")
                elif success_count > 0:
                    log_error(f"Destroyed {success_count} of {total} droplet(s) successfully ({failure_count} failed)")
                else:
                    log_error(f"Failed to destroy all {total} droplet(s)")

            if failure_count > 0:
                if json and suppress_json:
                    sys.exit(1)
                elif not json:
                    error_exit(f"Failed to destroy {failure_count} of {total} droplet(s)")
        except Exception as exc:
            if json:
                JSONOutput.print_error(f"Error destroying droplet(s): {exc}")
            else:
                error_exit(f"Error destroying droplet(s): {exc}")

    @droplet.command("info")
    @click.argument("droplet_id", type=int)
    @click.option("--api-token", help="DigitalOcean API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @add_json_option
    @add_verbose_option
    def droplet_info(droplet_id: int, api_token: Optional[str], json: bool):
        try:
            check_prerequisites_no_git()
            droplet_commands = DropletCommands()
            success = droplet_commands.get_droplet_info(droplet_id=droplet_id, api_token=api_token, json=json)
            if not success:
                if json:
                    JSONOutput.print_error(f"Failed to get droplet info: {droplet_id}")
                else:
                    error_exit(f"Failed to get droplet info: {droplet_id}")
        except ValueError:
            if json:
                JSONOutput.print_error(f"Invalid droplet ID: {droplet_id}. Must be an integer.")
            else:
                error_exit(f"Invalid droplet ID: {droplet_id}. Must be an integer.")
        except Exception as exc:
            if json:
                JSONOutput.print_error(f"Error getting droplet info: {exc}")
            else:
                error_exit(f"Error getting droplet info: {exc}")

    @droplet.command("push")
    @click.argument("branch_name", required=False)
    @click.argument("scp_target", required=False)
    @click.option("--output-dir", type=click.Path(), default="./packages", help="Temporary package location (default: ./packages)")
    @click.option("--keep-package", is_flag=True, default=False, help="Keep package file after successful push (default: delete after push)")
    @click.option(
        "--no-auto-import",
        is_flag=True,
        default=False,
        help="Skip automatic import and start on remote server after push (default: auto-import is enabled)",
    )
    @click.option("--prepare-server", is_flag=True, default=False, help="Check remote server for required dependencies before push")
    @click.option(
        "--domain",
        help="Domain override for remote import (subdomain.domain.tld). DNS A record will be automatically created if it does not exist.",
    )
    @click.option("--ip", help="IP override for remote import (HTTP-only, no TLS)")
    @click.option("--dns-token", help="DigitalOcean API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @click.option("--skip-dns-check", is_flag=True, default=False, help="Skip DNS validation and management")
    @click.option(
        "--resume",
        is_flag=True,
        default=False,
        help="Resume a failed push operation by detecting what's already completed (skips export/transfer if package exists, skips server prep if already done)",
    )
    @click.option("--code-only", is_flag=True, default=False, help="Push code-only update to pre-existing server (uses stored push config from env.ilha if available)")
    @click.option("--build", is_flag=True, default=False, help="Rebuild Docker images on the remote server after deployment")
    @click.option(
        "--debug",
        is_flag=True,
        default=False,
        help="Enable DEBUG mode in production deployment (default: False for production)"
    )
    @click.option(
        "--containers",
        help="Comma-separated list of worktree.container patterns to push only specific containers and their volumes (e.g., feature-auth.db,feature-auth.redis)",
    )
    @click.option(
        "--exclude-deps",
        help="Comma-separated list of service names to exclude from dependency resolution (e.g., db,redis). Useful when deploying workers that connect to remote services.",
    )
    @click.option("--use-staging-certificates", is_flag=True, default=False, help="Use Let's Encrypt staging certificates (doesn't count against rate limits)")
    @add_json_option
    @add_verbose_option
    def droplet_push(
        branch_name: Optional[str],
        scp_target: Optional[str],
        output_dir: str,
        keep_package: bool,
        no_auto_import: bool,
        prepare_server: bool,
        domain: str,
        ip: str,
        dns_token: str,
        skip_dns_check: bool,
        resume: bool,
        code_only: bool,
        build: bool,
        debug: bool,
        containers: Optional[str],
        exclude_deps: Optional[str],
        json: bool,
        use_staging_certificates: bool,
    ):
        start_time = time.time()
        try:
            check_setup_or_prompt()
            check_prerequisites()
            
            # Load stored droplet config and apply to parameters if not provided via CLI
            from ..core.environment_manager import EnvironmentManager
            from ..config.settings import get_project_root
            
            env_manager = EnvironmentManager(project_root=get_project_root())
            stored_config = env_manager.get_droplet_config(branch_name or '')
            
            # Apply stored config values if CLI arguments are not provided
            # Priority: CLI argument > stored config > default
            if not scp_target:
                scp_target = stored_config.get('scp_target')
            if not domain:
                domain = stored_config.get('domain') or ''
            if not ip:
                ip = stored_config.get('ip') or ''
            if not dns_token:
                dns_token = stored_config.get('dns_token') or ''
            if output_dir == "./packages":  # Only use stored if default value
                stored_output_dir = stored_config.get('output_dir')
                if stored_output_dir:
                    output_dir = stored_output_dir
            if not keep_package:  # Only use stored if False (default)
                stored_keep_package = stored_config.get('keep_package')
                if stored_keep_package:
                    keep_package = stored_keep_package.lower() in ('true', '1', 'yes')
            if not prepare_server:  # Only use stored if False (default)
                stored_prepare_server = stored_config.get('prepare_server')
                if stored_prepare_server:
                    prepare_server = stored_prepare_server.lower() in ('true', '1', 'yes')
            if not resume:  # Only use stored if False (default)
                stored_resume = stored_config.get('resume')
                if stored_resume:
                    resume = stored_resume.lower() in ('true', '1', 'yes')
            if not build:  # Only use stored if False (default)
                stored_build = stored_config.get('build')
                if stored_build:
                    build = stored_build.lower() in ('true', '1', 'yes')
            if not debug:  # Only use stored if False (default)
                stored_debug = stored_config.get('debug')
                if stored_debug:
                    debug = stored_debug.lower() in ('true', '1', 'yes')
            if not containers:
                containers = stored_config.get('containers')
            if not exclude_deps:
                exclude_deps = stored_config.get('exclude_deps')
            
            if domain and ip:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error("Options --domain and --ip are mutually exclusive")
                else:
                    error_exit("Options --domain and --ip are mutually exclusive")
                return
            
            # Infer scp_target from domain or stored config if not explicitly provided
            if not scp_target and not code_only:
                # Priority 1: Use domain option if provided
                if domain:
                    scp_target = domain
                    if not json:
                        log_info(f"Inferred SCP target from domain: {domain}")
                elif stored_config.get('scp_target'):
                    scp_target = stored_config.get('scp_target')
                    if not json:
                        log_info(f"Using stored SCP target from config: {scp_target}")
            
            if not code_only and not scp_target:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error("scp_target is required")
                else:
                    error_exit("scp_target is required")
                return
            
            # Parse and resolve SCP target (supports progressive patterns)
            # This ensures resolved IP is used for DNS management (fixes domain setup issue)
            try:
                # api_token is not in scope for this command; use the provided DNS token
                resolved_scp_target = _parse_scp_target_or_droplet(scp_target, dns_token)
                if resolved_scp_target != scp_target and not json:
                    log_info(f"Resolved SCP target: {scp_target} → {resolved_scp_target}")
                scp_target = resolved_scp_target
            except ValueError as e:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error(str(e))
                else:
                    error_exit(str(e))
                return
            except Exception as e:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error(f"Error resolving SCP target: {e}")
                else:
                    error_exit(f"Error resolving SCP target: {e}")
                return
            
            push_manager = PushManager()
            exclude_deps_list = [d.strip() for d in exclude_deps.split(",")] if exclude_deps else None
            success = push_manager.push_package(
                branch_name=branch_name,
                scp_target=scp_target,
                output_dir=Path(output_dir),
                keep_package=keep_package,
                auto_import=not no_auto_import,
                domain=domain,
                ip=ip,
                prepare_server=prepare_server,
                dns_token=dns_token,
                use_staging_certificates=use_staging_certificates,
                skip_dns_check=skip_dns_check,
                create_droplet=False,
                droplet_name=None,
                droplet_region=None,
                droplet_size=None,
                droplet_image=None,
                droplet_ssh_keys=None,
                resume=resume,
                code_only=code_only,
                build=build,
                debug=debug,
                containers=containers,
                exclude_deps=exclude_deps_list,
            )
            elapsed_time = time.time() - start_time
            if not success:
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error("Failed to push package")
                else:
                    error_exit("Failed to push package")
            else:
                # Save droplet configuration for future use
                config_to_save = {
                    'scp_target': scp_target,
                    'domain': domain if domain else None,
                    'ip': ip if ip else None,
                    'dns_token': dns_token if dns_token else None,
                    'output_dir': str(output_dir),
                    'keep_package': keep_package,
                    'prepare_server': prepare_server,
                    'resume': resume,
                    'build': build,
                    'debug': debug,
                    'containers': containers,
                    'exclude_deps': exclude_deps,
                }
                env_manager.save_droplet_config(branch_name or '', config_to_save)
                
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_success("Package pushed successfully")
        except Exception as exc:
            elapsed_time = time.time() - start_time
            if not json:
                print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
            if json:
                JSONOutput.print_error(f"Error pushing package: {exc}")
            else:
                error_exit(f"Error pushing package: {exc}")


