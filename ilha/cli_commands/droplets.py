"""
DigitalOcean droplet management commands.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

import click

from ilha.cli.helpers import add_json_option, add_remote_push_options, add_verbose_option
from ilha.commands.droplets import DropletCommands
from ilha.commands.push.push_manager import PushManager
from ilha.utils.json_output import JSONOutput
from ilha.utils.ssh_manager import SCPTarget
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
        pass  # expected when not in a git directory; fall through to worktree path detection

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
    """Resolve host to IP: already-IP, domain (via DO when token present), DNS, or droplet id/name."""
    import re

    if re.match(r'^\d+\.\d+\.\d+\.\d+$', host):
        return host

    # Domain + token: resolve via DO only (subdomain = droplet name), no DNS fallback
    if "." in host and api_token:
        from ilha.core.dns_manager import parse_domain
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
        ip = push_manager._resolve_server_ip(host)
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip) and ip != host:
            return ip
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


def _parse_rsync_target_or_droplet(arg: str, api_token: Optional[str] = None) -> str:
    """Parse rsync/connection target patterns and resolve to user@ip:path (used for rsync -e ssh).

    Supports:
    - Full target: `user@host:path` → resolve host to IP, return user@ip:path
    - Username + domain: `user@example.com` → resolve domain, return user@ip:/root
    - Domain/IP + path: `example.com:/path` or `192.168.1.100:/path` → return root@ip:path
    - Domain/IP only: `example.com` or `192.168.1.100` → return root@ip:/root
    - Droplet ID or name → resolve to IP, return root@ip:/root
    """
    from ..commands.push.push_manager import PushManager

    push_manager = PushManager()

    # Full target format user@host:path — resolve host so we use current droplet IP
    if '@' in arg and ':' in arg:
        at_pos = arg.index('@')
        colon_pos = arg.index(':')
        if at_pos < colon_pos:
            username = arg[:at_pos]
            host = arg[at_pos + 1:colon_pos]
            path = arg[colon_pos + 1:]
            ip = _resolve_host_to_ip(host, push_manager, api_token)
            return f"{username}@{ip}:{path}"

    # Username + domain/IP: user@example.com or user@192.168.1.100
    if '@' in arg and ':' not in arg:
        username, host = arg.split('@', 1)
        ip = _resolve_host_to_ip(host, push_manager, api_token)
        return f"{username}@{ip}:/root"

    # Domain/IP + path: example.com:/path or 192.168.1.100:/path
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


def _load_and_merge_config(branch_name, cli_args):
    """Merge CLI arguments with stored droplet config.

    Priority: CLI argument > stored config value > None.
    Returns (merged_dict, env_manager, stored_config).
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

    return result, env_manager, stored_config


def _attach_deploy_report_data(data: Dict[str, Any], deploy_report: Dict[str, Any]) -> Dict[str, Any]:
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


def _resolve_existing_remote_target(
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

    merged, _, stored_config = _load_and_merge_config(
        branch_name,
        {
            "scp_target": scp_target,
            "domain": domain,
            "ip": ip,
            "dns_token": api_token or "",
        },
    )
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

    resolved = _parse_rsync_target_or_droplet(scp_target, token)
    if resolved != scp_target and not json:
        log_info(f"Resolved rsync target: {scp_target} → {resolved}")
    return resolved, domain, ip, token, stored_config


def _print_doctor_report(report: Dict[str, Any]) -> None:
    print_plain("")
    print_plain("Doctor report:")
    if report.get("source_manifest"):
        manifest = report["source_manifest"]
        print_plain(f"  Source path: {manifest.get('source_path')}")
        print_plain(f"  Branch: {manifest.get('branch_name')}")
        print_plain(f"  Git branch: {manifest.get('git_branch') or 'unknown'}")
        print_plain(f"  Dirty: {manifest.get('git_dirty')}")
    print_plain(f"  Smoke path: {report.get('smoke_path')}")
    print_plain("")
    for check in report.get("checks", []):
        title = check.get("title", "check")
        if check.get("skipped"):
            print_plain(f"{title}: skipped ({check.get('reason', 'not applicable')})")
            continue
        status = "ok" if check.get("success") else "failed"
        print_plain(f"{title}: {status}")
        if check.get("stdout"):
            for line in check["stdout"].splitlines():
                print_plain(f"  {line}")
        if check.get("stderr"):
            for line in check["stderr"].splitlines():
                print_plain(f"  {line}")
        print_plain("")


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
    @click.option("--region", help='Droplet region (e.g., nyc1, sfo3). Use "ilha droplet regions" to list available regions. Default: nyc1 or from DROPLET_DEFAULT_REGION env var')
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
    @click.option("--vpc-uuid", help="VPC UUID for the droplet. If not provided, will use the default VPC for the region.")
    @click.option("--central-droplet-name", help="Name of central droplet to reuse VPC UUID from (for worker deployments)")
    @add_remote_push_options(include_use_staging_certificates=True)
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
            
            # Load stored droplet config and merge with CLI arguments
            merged, env_manager, stored_config = _load_and_merge_config(
                branch_name, {
                    'region': region, 'size': size, 'image': image,
                    'ssh_keys': ssh_keys, 'tags': tags, 'vpc_uuid': vpc_uuid,
                    'central_droplet_name': central_droplet_name,
                    'scp_target': scp_target, 'domain': domain, 'ip': ip,
                    'dns_token': dns_token, 'output_dir': output_dir,
                    'keep_package': keep_package, 'prepare_server': prepare_server,
                    'resume': resume, 'build': build,
                    'containers': containers, 'exclude_deps': exclude_deps,
                },
            )
            region = merged['region']
            size = merged['size']
            image = merged['image']
            ssh_keys = merged['ssh_keys']
            tags = merged.get('tags') or ()
            vpc_uuid = merged['vpc_uuid']
            central_droplet_name = merged['central_droplet_name']
            scp_target = merged['scp_target']
            domain = merged['domain']
            ip = merged['ip']
            dns_token = merged['dns_token']
            output_dir = merged['output_dir']
            keep_package = merged['keep_package']
            prepare_server = merged['prepare_server']
            resume = merged['resume']
            build = merged['build']
            containers = merged['containers']
            exclude_deps = merged['exclude_deps']
            
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
                            if central_droplet.region != resolved_region:
                                msg = (
                                    f"Region mismatch: central droplet '{central_droplet_name}' "
                                    f"is in {central_droplet.region}, but worker would be in "
                                    f"{resolved_region}. Use --region {central_droplet.region}"
                                )
                                if json:
                                    JSONOutput.print_error(msg)
                                else:
                                    log_error("Region mismatch detected!")
                                    log_error(f"   Central droplet '{central_droplet_name}' is in region: {central_droplet.region}")
                                    log_error(f"   Worker droplet would be created in region: {resolved_region}")
                                    log_error("   VPCs are region-specific and cannot span regions.")
                                    log_error(f"   Please use --region {central_droplet.region} to match the central droplet's region.")
                                    error_exit(msg)
                                return
                            if not json:
                                log_info(f"Found central droplet '{central_droplet_name}' in region {central_droplet.region}")
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

    @droplet.command("regions")
    @click.option("--api-token", help="DigitalOcean API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @click.option("--as-json", "--json", "output_json", is_flag=True, default=False, help="Output results as JSON format")
    @click.option("--as-csv", "output_csv", is_flag=True, default=False, help="Output results as CSV format")
    @click.option("--show-all", is_flag=True, default=False, help="Include unavailable regions in output")
    @add_verbose_option
    def droplet_regions(api_token: Optional[str], output_json: bool, output_csv: bool, show_all: bool):
        try:
            check_prerequisites_no_git()
            droplet_commands = DropletCommands()
            success = droplet_commands.list_regions(api_token=api_token, json=output_json, csv=output_csv, show_all=show_all)
            if not success:
                if output_json or output_csv:
                    JSONOutput.print_error("Failed to list droplet regions")
                else:
                    error_exit("Failed to list droplet regions")
        except Exception as exc:
            if output_json or output_csv:
                JSONOutput.print_error(f"Error listing droplet regions: {exc}")
            else:
                error_exit(f"Error listing droplet regions: {exc}")

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
                    raise SystemExit(1)
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
    @click.argument("target", required=False)
    @add_remote_push_options(include_debug=True, include_use_staging_certificates=True)
    @add_json_option
    @add_verbose_option
    def droplet_push(
        branch_name: Optional[str],
        target: Optional[str],
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
            
            # CLI always wins over env file for rsync/connection target
            cli_target = target
            cli_domain = domain or None  # treat '' as not provided

            # Load stored droplet config and merge with CLI arguments
            merged, env_manager, stored_config = _load_and_merge_config(
                branch_name, {
                    'scp_target': target, 'domain': domain, 'ip': ip,
                    'dns_token': dns_token, 'output_dir': output_dir,
                    'keep_package': keep_package, 'prepare_server': prepare_server,
                    'resume': resume, 'build': build, 'debug': debug,
                    'containers': containers, 'exclude_deps': exclude_deps,
                },
            )
            target = merged['scp_target']
            domain = merged['domain']
            ip = merged['ip']
            dns_token = merged['dns_token']
            output_dir = merged['output_dir']
            keep_package = merged['keep_package']
            prepare_server = merged['prepare_server']
            resume = merged['resume']
            build = merged['build']
            debug = merged['debug']
            containers = merged['containers']
            exclude_deps = merged['exclude_deps']
            
            if domain and ip:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error("Options --domain and --ip are mutually exclusive")
                else:
                    error_exit("Options --domain and --ip are mutually exclusive")
                return
            
            # Rsync/connection target: CLI target > CLI domain > stored domain > stored target (always set when we have a value; --code-only does not skip this)
            if cli_target:
                target = cli_target
                if not json:
                    log_info(f"Using rsync target from command line: {cli_target}")
            elif cli_domain:
                target = cli_domain
                if not json:
                    log_info(f"Using domain from command line: {cli_domain}")
            elif domain:
                target = domain
                if not json:
                    log_info(f"Using domain from config: {domain}")
            elif not target:
                target = stored_config.get('scp_target')
                if target and not json:
                    log_info(f"Using rsync target from config: {target}")

            if not code_only and not target:
                elapsed_time = time.time() - start_time
                if not json:
                    print_plain(f"Total elapsed time: {format_elapsed_time(elapsed_time)}")
                if json:
                    JSONOutput.print_error("Rsync target (or --domain) is required")
                else:
                    error_exit("Rsync target (or --domain) is required")
                return

            # Resolve rsync target (domain, IP, or droplet id/name) to user@ip:path
            try:
                resolved = _parse_rsync_target_or_droplet(target, dns_token)
                if resolved != target and not json:
                    log_info(f"Resolved rsync target: {target} → {resolved}")
                rsync_target = resolved
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
                    JSONOutput.print_error(f"Error resolving rsync target: {e}")
                else:
                    error_exit(f"Error resolving rsync target: {e}")
                return
            
            push_manager = PushManager()
            exclude_deps_list = [d.strip() for d in exclude_deps.split(",")] if exclude_deps else None
            success = push_manager.push_package(
                branch_name=branch_name,
                scp_target=rsync_target,
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
                # Save droplet configuration for future use (stored key remains scp_target for compatibility)
                config_to_save = {
                    'scp_target': rsync_target,
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

    # ── droplet deploy (slim) ─────────────────────────────────────────

    @droplet.command("deploy")
    @click.argument("branch_name", required=False)
    @click.option("--region", help='Droplet region (e.g., nyc1, sfo3). Default: nyc1 or DROPLET_DEFAULT_REGION')
    @click.option("--size", help='Droplet size slug (e.g., s-1vcpu-1gb). Default: s-1vcpu-1gb or DIGITALOCEAN_SIZE')
    @click.option("--image", help="Droplet image (default: ubuntu-22-04-x64)")
    @click.option("--ssh-keys", type=str, help="SSH key names (comma-separated)")
    @click.option("--tags", multiple=True, help="Tags for the droplet")
    @click.option("--api-token", help="DigitalOcean API token")
    @click.option("--scp-target", help="SCP target (default: root@<droplet-ip>:/root)")
    @click.option("--domain", help="Domain for HTTPS deployment (e.g., app.example.com)")
    @click.option("--ip", help="IP for HTTP-only deployment")
    @click.option("--dns-token", help="DNS API token for domain management")
    @click.option("--skip-dns-check", is_flag=True, default=False, help="Skip DNS management")
    @click.option("--build", is_flag=True, default=False, help="Rebuild Docker images on server")
    @click.option("--debug", is_flag=True, default=False, help="Enable DEBUG mode on server")
    @click.option("--create-only", is_flag=True, default=False, help="Only create droplet, skip deploy")
    @click.option("--no-create", is_flag=True, default=False, help="Skip droplet creation, deploy to existing server (requires --scp-target or --domain/--ip)")
    @click.option("--no-prepare", is_flag=True, default=False, help="Skip server preparation")
    @click.option("--vpc-uuid", help="VPC UUID for the droplet")
    @click.option("--central-droplet-name", help="Reuse VPC UUID from central droplet")
    @click.option("--use-staging-certificates", is_flag=True, default=False, help="Use Let's Encrypt staging certificates")
    @click.option(
        "--disk-recovery",
        type=click.Choice(["off", "safe", "aggressive"], case_sensitive=False),
        default="safe",
        show_default=True,
        help="Disk recovery mode if remote disk fills during deploy",
    )
    @click.option("--retry-on-disk-reclaim", is_flag=True, default=False, help="Retry deploy once after successful disk recovery")
    @click.option("--deployignore", type=click.Path(path_type=Path), help="Path to deploy ignore file (defaults to .ilha/deployignore in worktree)")
    @click.option("--no-disk-check", is_flag=True, default=False, help="Skip remote disk preflight checks")
    @click.option("--skip-volume-backup", is_flag=True, default=False, help="Skip local volume backup for faster stateless deploys")
    @click.option(
        "--strict-deploy-drift",
        is_flag=True,
        default=False,
        help="Fail deploy when critical files differ between comparison root and worktree, even across different commits",
    )
    @click.option("--smoke-timeout", type=int, default=60, help="Timeout in seconds for post-deploy smoke checks (default: 60)")
    @add_json_option
    @add_verbose_option
    def droplet_deploy(
        branch_name: Optional[str],
        region: Optional[str],
        size: Optional[str],
        image: Optional[str],
        ssh_keys: Optional[str],
        tags: tuple,
        api_token: Optional[str],
        scp_target: Optional[str],
        domain: Optional[str],
        ip: Optional[str],
        dns_token: Optional[str],
        skip_dns_check: bool,
        build: bool,
        debug: bool,
        create_only: bool,
        no_create: bool,
        no_prepare: bool,
        vpc_uuid: Optional[str],
        central_droplet_name: Optional[str],
        use_staging_certificates: bool,
        disk_recovery: str,
        retry_on_disk_reclaim: bool,
        deployignore: Optional[Path],
        no_disk_check: bool,
        skip_volume_backup: bool,
        strict_deploy_drift: bool,
        smoke_timeout: int,
        json: bool,
    ):
        """Deploy a worktree to a DigitalOcean droplet (slim mode).

        Uses rsync + remote docker compose. No Python, git, or ilha needed on
        the server — only Docker.

        \b
        Examples:
          ilha droplet deploy --domain app.example.com
          ilha droplet deploy test --domain app.example.com --build
          ilha droplet deploy --no-create --scp-target root@1.2.3.4:/root
        """
        from ilha.commands.push.slim_deploy_manager import SlimDeployManager
        from ilha.core.droplet_manager import DropletManager

        start_time = time.time()
        deploy_report: Dict[str, Any] = {}
        try:
            # Validate mutually exclusive flags
            if domain and ip:
                if json:
                    JSONOutput.print_error("--domain and --ip are mutually exclusive")
                else:
                    error_exit("--domain and --ip are mutually exclusive")
                return
            if create_only and no_create:
                if json:
                    JSONOutput.print_error("--create-only and --no-create are mutually exclusive")
                else:
                    error_exit("--create-only and --no-create are mutually exclusive")
                return

            # Auto-detect branch
            branch_name = _auto_detect_branch_name(branch_name, json)
            if not branch_name:
                return

            check_prerequisites_no_git()
            effective_token = api_token or dns_token

            # ── Droplet creation (unless --no-create) ────────────────
            droplet_info = None
            if not no_create:
                droplet_name = _resolve_droplet_name(branch_name, domain, json)
                defaults = DropletManager.get_droplet_defaults()
                resolved_region = region or defaults.get("region", "nyc1")
                resolved_size = size or defaults.get("size", "s-1vcpu-1gb")
                resolved_image = image or defaults.get("image", "ubuntu-22-04-x64")

                if not json:
                    log_info("Slim deploy: creating droplet...")
                    _log_creation_context(
                        droplet_name, resolved_region, resolved_size,
                        resolved_image, ssh_keys, tags, None, None, json,
                    )

                # Resolve VPC UUID from central droplet if requested
                resolved_vpc_uuid = vpc_uuid
                token = DropletManager.resolve_droplet_token(effective_token)
                provider = None
                if token:
                    provider = DropletManager.create_provider("digitalocean", token)

                if central_droplet_name and not vpc_uuid and provider:
                    droplets_list = provider.list_droplets()
                    for d in droplets_list:
                        if d.name == central_droplet_name and d.vpc_uuid:
                            resolved_vpc_uuid = d.vpc_uuid
                            if not json:
                                log_info(f"Using VPC UUID from '{central_droplet_name}': {resolved_vpc_uuid}")
                            break

                # Generate unique name
                if provider:
                    droplet_name = _generate_unique_droplet_name(droplet_name, provider)

                droplet_commands = DropletCommands()
                ssh_keys_list = [k.strip() for k in ssh_keys.split(",")] if ssh_keys else None
                tags_list = list(tags) if tags else None

                created_droplet = droplet_commands.create_droplet(
                    name=droplet_name,
                    region=resolved_region,
                    size=resolved_size,
                    image=resolved_image,
                    ssh_keys=ssh_keys_list,
                    tags=tags_list,
                    wait=True,
                    api_token=effective_token,
                    json=json,
                    vpc_uuid=resolved_vpc_uuid,
                    return_droplet=True,
                )
                if not created_droplet:
                    _print_elapsed(start_time, json)
                    if json:
                        JSONOutput.print_error(f"Failed to create droplet: {droplet_name}")
                    else:
                        error_exit(f"Failed to create droplet: {droplet_name}")
                    return
                droplet_info = created_droplet

                if create_only:
                    _print_elapsed(start_time, json)
                    return

                if not droplet_info or not droplet_info.ip_address:
                    _print_elapsed(start_time, json)
                    if json:
                        JSONOutput.print_error("Droplet created but IP not available")
                    else:
                        error_exit("Droplet created but IP not available")
                    return

                # Build SCP target from droplet IP
                if scp_target:
                    try:
                        t = SCPTarget(scp_target)
                        scp_target = f"{t.username}@{droplet_info.ip_address}:{t.remote_path}"
                    except ValueError:
                        scp_target = f"root@{droplet_info.ip_address}:/root"
                else:
                    scp_target = f"root@{droplet_info.ip_address}:/root"

                if not json:
                    log_info(f"Droplet ready. IP: {droplet_info.ip_address}")
            else:
                # --no-create: need an explicit scp_target or domain/ip
                if not scp_target:
                    if domain:
                        scp_target = _parse_rsync_target_or_droplet(domain, effective_token)
                    elif ip:
                        scp_target = f"root@{ip}:/root"
                    else:
                        _print_elapsed(start_time, json)
                        if json:
                            JSONOutput.print_error("--no-create requires --scp-target, --domain, or --ip")
                        else:
                            error_exit("--no-create requires --scp-target, --domain, or --ip")
                        return
                else:
                    try:
                        scp_target = _parse_rsync_target_or_droplet(scp_target, effective_token)
                    except ValueError as exc:
                        _print_elapsed(start_time, json)
                        if json:
                            JSONOutput.print_error(str(exc))
                        else:
                            error_exit(str(exc))
                        return

            # ── Prerequisites for deploy ─────────────────────────────
            check_setup_or_prompt()
            check_prerequisites()

            # ── Run slim deploy ──────────────────────────────────────
            deploy_mgr = SlimDeployManager()
            try:
                success = deploy_mgr.deploy(
                    branch_name=branch_name,
                    scp_target=scp_target,
                    domain=domain or None,
                    ip=ip or None,
                    prepare_server=not no_prepare,
                    dns_token=dns_token or api_token,
                    skip_dns_check=skip_dns_check,
                    build=build,
                    debug=debug,
                    droplet_info=droplet_info,
                    use_staging_certificates=use_staging_certificates,
                    disk_recovery=disk_recovery.lower(),
                    retry_on_disk_reclaim=retry_on_disk_reclaim,
                    deployignore=str(deployignore) if deployignore else None,
                    no_disk_check=no_disk_check,
                    skip_volume_backup=skip_volume_backup,
                    strict_deploy_drift=strict_deploy_drift,
                    smoke_timeout=smoke_timeout,
                )
                deploy_report = deploy_mgr.get_last_deploy_report()
            finally:
                deploy_mgr.cleanup()

            _print_elapsed(start_time, json)

            if not success:
                if json:
                    JSONOutput.print_json(
                        JSONOutput.error(
                            "Slim deploy failed",
                            error_code=(deploy_report.get("error") or {}).get("error_code"),
                            details={
                                "stage": (deploy_report.get("error") or {}).get("stage"),
                                "root_cause": (deploy_report.get("error") or {}).get("root_cause"),
                                "suggested_actions": (deploy_report.get("error") or {}).get("suggested_actions", []),
                                **_attach_deploy_report_data({}, deploy_report),
                            },
                        )
                    )
                else:
                    error_exit("Slim deploy failed")
            else:
                url = None
                if domain:
                    url = f"https://{domain}"
                elif ip:
                    url = f"http://{ip}"
                elif droplet_info and droplet_info.ip_address:
                    url = f"http://{droplet_info.ip_address}"

                if json:
                    data = {"branch": branch_name}
                    if url:
                        data["url"] = url
                    if droplet_info:
                        data["ip_address"] = droplet_info.ip_address
                    data = _attach_deploy_report_data(data, deploy_report)
                    JSONOutput.print_json(JSONOutput.success("Slim deploy completed", data=data))
                else:
                    if url:
                        log_success(f"🌐 {url}")

        except Exception as exc:
            _print_elapsed(start_time, json)
            if json:
                JSONOutput.print_json(
                    JSONOutput.error(
                        f"Deploy error: {exc}",
                        error_code="deploy_exception",
                        details=_attach_deploy_report_data({}, deploy_report),
                    )
                )
            else:
                error_exit(f"Deploy error: {exc}")

    @droplet.command("put")
    @click.argument("branch_name", required=False)
    @click.option("--scp-target", help="SCP target (user@host:path). Falls back to stored config.")
    @click.option("--domain", help="Domain for the deployment (e.g., beta.example.com)")
    @click.option("--ip", help="IP for HTTP-only. Mutually exclusive with --domain.")
    @click.option("--build", is_flag=True, default=False, help="Rebuild Docker images on server")
    @click.option("--debug", is_flag=True, default=False, help="Enable DEBUG mode on server")
    @click.option("--use-staging-certificates", is_flag=True, default=False, help="Use Let's Encrypt staging certificates")
    @click.option("--use-production-certificates", is_flag=True, default=False, help="Use production certificates (explicit default)")
    @click.option("--code-only", is_flag=True, default=False, help="Only sync code and config (no volume/data transfer)")
    @click.option("--db-only", is_flag=True, default=False, help="Only sync volumes/data (no code transfer). Stops app during restore.")
    @click.option("--api-token", help="API token for resolving domain/droplet to SCP target")
    @add_json_option
    @add_verbose_option
    def droplet_put(
        branch_name: Optional[str],
        scp_target: Optional[str],
        domain: Optional[str],
        ip: Optional[str],
        build: bool,
        debug: bool,
        use_staging_certificates: bool,
        use_production_certificates: bool,
        code_only: bool,
        db_only: bool,
        api_token: Optional[str],
        json: bool,
    ):
        """Update an existing slim-deployed server.

        By default syncs code, config, and volumes (full update). Use --code-only
        or --db-only to limit scope. No DNS changes or server prep.

        \b
        Examples:
          ilha droplet put --domain beta.bower.bid
          ilha droplet put lipstick --scp-target root@1.2.3.4:/root --domain beta.bower.bid --build
          ilha droplet put --domain beta.bower.bid --code-only
          ilha droplet put --domain beta.bower.bid --db-only
        """
        from ilha.commands.push.slim_deploy_manager import SlimDeployManager

        start_time = time.time()
        deploy_report: Dict[str, Any] = {}
        try:
            if domain and ip:
                _print_elapsed(start_time, json)
                if json:
                    JSONOutput.print_error("--domain and --ip are mutually exclusive")
                else:
                    error_exit("--domain and --ip are mutually exclusive")
                return
            if use_staging_certificates and use_production_certificates:
                _print_elapsed(start_time, json)
                if json:
                    JSONOutput.print_error("--use-staging-certificates and --use-production-certificates are mutually exclusive")
                else:
                    error_exit("--use-staging-certificates and --use-production-certificates are mutually exclusive")
                return
            if code_only and db_only:
                _print_elapsed(start_time, json)
                if json:
                    JSONOutput.print_error("--code-only and --db-only are mutually exclusive")
                else:
                    error_exit("--code-only and --db-only are mutually exclusive")
                return

            branch_name = _auto_detect_branch_name(branch_name, json)
            if not branch_name:
                return

            check_prerequisites_no_git()

            try:
                scp_target, domain, ip, _, _ = _resolve_existing_remote_target(
                    branch_name=branch_name,
                    scp_target=scp_target,
                    domain=domain,
                    ip=ip,
                    api_token=api_token,
                    json=json,
                )
            except ValueError as exc:
                _print_elapsed(start_time, json)
                if json:
                    JSONOutput.print_error(str(exc))
                else:
                    error_exit(str(exc))
                return
            except Exception as exc:
                _print_elapsed(start_time, json)
                if json:
                    JSONOutput.print_error(f"Error resolving rsync target: {exc}")
                else:
                    error_exit(f"Error resolving rsync target: {exc}")
                return

            deploy_mgr = SlimDeployManager()
            try:
                success = deploy_mgr.put(
                    branch_name=branch_name,
                    scp_target=scp_target,
                    domain=domain or None,
                    ip=ip or None,
                    build=build,
                    debug=debug,
                    use_staging_certificates=use_staging_certificates,
                    code_only=code_only,
                    db_only=db_only,
                )
                deploy_report = deploy_mgr.get_last_deploy_report()
            finally:
                deploy_mgr.cleanup()

            _print_elapsed(start_time, json)
            if not success:
                if json:
                    JSONOutput.print_json(
                        JSONOutput.error(
                            "Slim put failed",
                            error_code=(deploy_report.get("error") or {}).get("error_code"),
                            details={
                                "stage": (deploy_report.get("error") or {}).get("stage"),
                                "root_cause": (deploy_report.get("error") or {}).get("root_cause"),
                                "suggested_actions": (deploy_report.get("error") or {}).get("suggested_actions", []),
                                **_attach_deploy_report_data({}, deploy_report),
                            },
                        )
                    )
                else:
                    error_exit("Slim put failed")
            elif json:
                data = {"branch": branch_name}
                if domain:
                    data["url"] = f"https://{domain}"
                elif ip:
                    data["url"] = f"http://{ip}"
                data = _attach_deploy_report_data(data, deploy_report)
                JSONOutput.print_json(JSONOutput.success("Slim put completed", data=data))

        except Exception as exc:
            _print_elapsed(start_time, json)
            if json:
                JSONOutput.print_json(
                    JSONOutput.error(
                        f"Put error: {exc}",
                        error_code="put_exception",
                        details=_attach_deploy_report_data({}, deploy_report),
                    )
                )
            else:
                error_exit(f"Put error: {exc}")

    @droplet.command("doctor")
    @click.argument("branch_name", required=False)
    @click.option("--scp-target", help="SCP target (user@host:path). Falls back to stored config.")
    @click.option("--domain", help="Domain for the deployment (e.g., beta.example.com)")
    @click.option("--ip", help="IP for HTTP-only. Mutually exclusive with --domain.")
    @click.option("--api-token", help="API token for resolving domain/droplet to SCP target")
    @add_json_option
    @add_verbose_option
    def droplet_doctor(
        branch_name: Optional[str],
        scp_target: Optional[str],
        domain: Optional[str],
        ip: Optional[str],
        api_token: Optional[str],
        json: bool,
    ):
        """Collect read-only diagnostics for an existing slim deploy target."""
        from ilha.commands.push.slim_deploy_manager import SlimDeployManager

        if domain and ip:
            if json:
                JSONOutput.print_error("--domain and --ip are mutually exclusive")
            else:
                error_exit("--domain and --ip are mutually exclusive")
            return

        branch_name = _auto_detect_branch_name(branch_name, json)
        if not branch_name:
            return

        check_prerequisites_no_git()

        try:
            scp_target, domain, ip, _, _ = _resolve_existing_remote_target(
                branch_name=branch_name,
                scp_target=scp_target,
                domain=domain,
                ip=ip,
                api_token=api_token,
                json=json,
            )
        except ValueError as exc:
            if json:
                JSONOutput.print_error(str(exc))
            else:
                error_exit(str(exc))
            return
        except Exception as exc:
            if json:
                JSONOutput.print_error(f"Error resolving rsync target: {exc}")
            else:
                error_exit(f"Error resolving rsync target: {exc}")
            return

        deploy_mgr = SlimDeployManager()
        try:
            report = deploy_mgr.doctor(
                branch_name=branch_name,
                scp_target=scp_target,
                domain=domain or None,
                ip=ip or None,
            )
        finally:
            deploy_mgr.cleanup()

        if json:
            JSONOutput.print_json(JSONOutput.success("Doctor report collected", data=report))
        else:
            _print_doctor_report(report)


def _print_elapsed(start_time: float, json: bool) -> None:
    elapsed = time.time() - start_time
    if not json:
        print_plain(f"Total elapsed time: {format_elapsed_time(elapsed)}")
