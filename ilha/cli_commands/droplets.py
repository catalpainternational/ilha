"""
DigitalOcean droplet management commands.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any

import click

from ilha.cli.helpers import add_json_option, add_secrets_option, add_verbose_option
from ilha.commands.droplet_remote import (
    RemoteAppTarget,
    dump_remote_postgres,
    read_remote_deploy_identity,
    remote_compose_exec_command,
    remote_compose_prefix,
    resolve_remote_app_target,
    set_remote_hostname,
)
from ilha.commands.droplets import (
    DropletCommands,
    auto_detect_branch_name,
    resolve_droplet_name,
    resolve_host_to_ip,
    parse_rsync_target_or_droplet,
    log_creation_context,
    load_and_merge_config,
    attach_deploy_report_data,
    resolve_existing_remote_target,
)
from ilha.commands.push.deploy_identity import parse_deploy_identity
from ilha.commands.secrets import resolve_cli_secrets_profile
from ilha.core.droplet_manager import DropletManager
from ilha.utils.confirmation import confirm_action
from ilha.utils.json_output import JSONOutput
from ilha.utils.ssh_manager import SCPTarget, SSHConnectionManager
from ilha.utils.logging import (
    error_exit,
    format_elapsed_time,
    log_error,
    log_info,
    log_success,
    log_warning,
    print_plain,
)
from ilha.exceptions import ilhaCommandError
from ilha.utils.validation import (
    check_prerequisites,
    check_prerequisites_no_git,
    check_setup_or_prompt,
)


def _resolve_target_or_exit(
    *,
    branch_name: Optional[str],
    scp_target: Optional[str],
    domain: Optional[str],
    ip: Optional[str],
    api_token: Optional[str],
    json: bool,
) -> RemoteAppTarget:
    """Resolve a slim-droplet SSH target or exit with a CLI error."""
    try:
        return resolve_remote_app_target(
            branch_name=branch_name,
            scp_target=scp_target,
            domain=domain,
            ip=ip,
            api_token=api_token,
            json=json,
        )
    except ilhaCommandError as exc:
        if json:
            JSONOutput.print_error(exc.message)
            raise SystemExit(exc.exit_code or 1)
        error_exit(exc.message)


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

    def _find_existing_droplet_by_name(name: str, provider):
        """Return the first droplet matching `name` exactly, or None."""
        for d in provider.list_droplets():
            if d.name == name:
                return d
        return None

    @droplet.command("list")
    @click.option("--api-token", help="DigitalOcean API token (or use DIGITALOCEAN_API_TOKEN env var)")
    @click.option("--as-json", "--json", "output_json", is_flag=True, default=False, help="Output results as JSON format")
    @click.option("--as-csv", "output_csv", is_flag=True, default=False, help="Output results as CSV format")
    @add_verbose_option
    def droplet_list(api_token: Optional[str], output_json: bool, output_csv: bool):
        try:
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
    @click.option(
        "--reuse-existing",
        is_flag=True,
        default=False,
        help="If a droplet with the resolved name already exists, deploy to it without prompting.",
    )
    @click.option(
        "--force-new",
        is_flag=True,
        default=False,
        help="Always create a new droplet even if one with the resolved name exists (suffixed with -2, -3, …).",
    )
    @click.option("--vpc-uuid", help="VPC UUID for the droplet")
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
        "--restore-volumes/--no-restore-volumes",
        default=True,
        show_default=True,
        help=(
            "Copy local worktree Docker volumes (DB, media, redis) to the server."
            " Default is on so a fresh-droplet deploy lands as a working staging environment;"
            " pass --no-restore-volumes for a code-only deploy that lets the server initialize empty volumes."
        ),
    )
    @click.option(
        "--no-build",
        is_flag=True,
        default=False,
        help="Never run remote `docker compose build`; fail if the web image is missing on the server.",
    )
    @click.option(
        "--force-password-reset",
        is_flag=True,
        default=False,
        help="Skip Postgres password validation after volume restore (dangerous; use only when intentional).",
    )
    @click.option(
        "--strict-deploy-drift",
        is_flag=True,
        default=False,
        help="Fail deploy when critical files differ between comparison root and worktree, even across different commits",
    )
    @click.option(
        "--expected-ssh-fingerprint",
        help="SHA256:… fingerprint of the server's SSH host key. If set, a mismatch aborts the deploy (MITM defense). Without it, the first-seen key is trusted (TOFU) and logged so you can pin it on the next deploy.",
    )
    @click.option("--smoke-timeout", type=int, default=60, help="Timeout in seconds for post-deploy smoke checks (default: 60)")
    @click.option(
        "--allow-dirty",
        is_flag=True,
        default=False,
        help="Do not fail smoke checks when the worktree has uncommitted changes",
    )
    @click.option(
        "--set-hostname",
        is_flag=True,
        default=False,
        help="Set the droplet OS hostname to the droplet/branch name",
    )
    @add_secrets_option
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
        reuse_existing: bool,
        force_new: bool,
        vpc_uuid: Optional[str],
        use_staging_certificates: bool,
        disk_recovery: str,
        retry_on_disk_reclaim: bool,
        deployignore: Optional[Path],
        no_disk_check: bool,
        skip_volume_backup: bool,
        restore_volumes: bool,
        no_build: bool,
        force_password_reset: bool,
        strict_deploy_drift: bool,
        expected_ssh_fingerprint: Optional[str],
        smoke_timeout: int,
        allow_dirty: bool,
        set_hostname: bool,
        secrets_profile: Optional[str],
        no_secrets: bool,
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

        start_time = time.time()
        deploy_report: Dict[str, Any] = {}
        try:
            secrets_profile = resolve_cli_secrets_profile(
                secrets_profile, no_secrets=no_secrets
            )
            # Validate mutually exclusive flags
            if domain and ip:
                if json:
                    JSONOutput.print_error("--domain and --ip are mutually exclusive")
                else:
                    error_exit("--domain and --ip are mutually exclusive")
                return
            if build and no_build:
                if json:
                    JSONOutput.print_error("--build and --no-build are mutually exclusive")
                else:
                    error_exit("--build and --no-build are mutually exclusive")
                return
            if create_only and no_create:
                if json:
                    JSONOutput.print_error("--create-only and --no-create are mutually exclusive")
                else:
                    error_exit("--create-only and --no-create are mutually exclusive")
                return
            if reuse_existing and force_new:
                if json:
                    JSONOutput.print_error("--reuse-existing and --force-new are mutually exclusive")
                else:
                    error_exit("--reuse-existing and --force-new are mutually exclusive")
                return

            # Auto-detect branch
            branch_name = auto_detect_branch_name(branch_name, json)

            check_prerequisites_no_git()
            effective_token = api_token or dns_token

            # ── Droplet creation (unless --no-create) ────────────────
            droplet_info = None
            if not no_create:
                droplet_name = resolve_droplet_name(branch_name, domain, json)
                defaults = DropletManager.get_droplet_defaults()
                resolved_region = region or defaults.get("region", "nyc1")
                resolved_size = size or defaults.get("size", "s-1vcpu-1gb")
                resolved_image = image or defaults.get("image", "ubuntu-22-04-x64")

                if not json:
                    log_info("Slim deploy: creating droplet...")
                    log_creation_context(
                        droplet_name, resolved_region, resolved_size,
                        resolved_image, ssh_keys, tags, None, None, json,
                    )

                token = DropletManager.resolve_droplet_token(effective_token)
                provider = None
                if token:
                    provider = DropletManager.create_provider("digitalocean", token)

                # Existing-droplet detection: a previous deploy may have created
                # the droplet but failed mid-prep. Without this check, we'd
                # silently create <name>-2 and start billing for a second one.
                existing = _find_existing_droplet_by_name(droplet_name, provider) if provider else None
                if existing and not force_new:
                    if reuse_existing:
                        action = "reuse"
                    elif json:
                        _print_elapsed(start_time, json)
                        JSONOutput.print_error(
                            f"Droplet '{droplet_name}' already exists at {existing.ip_address or 'unknown'}. "
                            "Pass --reuse-existing to deploy to it, or --force-new to create another."
                        )
                        return
                    else:
                        log_info(
                            f"Droplet '{droplet_name}' already exists "
                            f"(id={existing.id}, ip={existing.ip_address or 'unknown'}, status={existing.status})."
                        )
                        action = "reuse" if click.confirm(
                            f"Deploy to existing droplet '{droplet_name}'?", default=True
                        ) else "abort"

                    if action == "abort":
                        _print_elapsed(start_time, json)
                        log_info("Aborted. Pass --force-new to create a fresh droplet, or destroy the existing one.")
                        return

                    # Reuse path: skip creation, fall through to deploy with this droplet's IP.
                    if not existing.ip_address:
                        _print_elapsed(start_time, json)
                        if json:
                            JSONOutput.print_error(f"Existing droplet '{droplet_name}' has no IP address")
                        else:
                            error_exit(f"Existing droplet '{droplet_name}' has no IP address")
                        return
                    droplet_info = existing
                    if not json:
                        log_info(f"Reusing droplet '{droplet_name}' at {existing.ip_address}")
                else:
                    # Generate unique name (suffixes -2, -3 if --force-new lands on a collision)
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
                        vpc_uuid=vpc_uuid,
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
                        scp_target = parse_rsync_target_or_droplet(domain, effective_token)
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
                        scp_target = parse_rsync_target_or_droplet(scp_target, effective_token)
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
                    restore_volumes=restore_volumes,
                    no_build=no_build,
                    force_password_reset=force_password_reset,
                    strict_deploy_drift=strict_deploy_drift,
                    smoke_timeout=smoke_timeout,
                    expected_ssh_fingerprint=expected_ssh_fingerprint,
                    secrets_profile=secrets_profile,
                    allow_dirty=allow_dirty,
                    set_hostname=(
                        (droplet_info.name if droplet_info else None) or branch_name
                    )
                    if set_hostname
                    else None,
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
                                **attach_deploy_report_data({}, deploy_report),
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
                    data = attach_deploy_report_data(data, deploy_report)
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
                        details=attach_deploy_report_data({}, deploy_report),
                    )
                )
                raise SystemExit(1)
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
    @click.option("--code-only", is_flag=True, default=False, help="Only sync code and config (default; kept as an explicit alias)")
    @click.option("--db-only", is_flag=True, default=False, help="Only sync volumes/data (no code transfer). Stops app during restore.")
    @click.option(
        "--restore-volumes",
        is_flag=True,
        default=False,
        help="Copy local Docker volumes (DB, media, redis) onto the server. Destructive; never the default.",
    )
    @click.option(
        "--i-mean-it",
        is_flag=True,
        default=False,
        help="Skip the confirmation prompt when --restore-volumes targets a live domain",
    )
    @click.option(
        "--dump-db-first",
        is_flag=True,
        default=False,
        help="Dump the remote database into backups/ before updating",
    )
    @click.option(
        "--allow-dirty",
        is_flag=True,
        default=False,
        help="Do not fail smoke checks when the worktree has uncommitted changes",
    )
    @click.option(
        "--set-hostname",
        is_flag=True,
        default=False,
        help="Set the droplet OS hostname to the branch name",
    )
    @click.option(
        "--force-password-reset",
        is_flag=True,
        default=False,
        help="Skip Postgres password validation after volume restore (dangerous; use only when intentional).",
    )
    @click.option("--api-token", help="API token for resolving domain/droplet to SCP target")
    @add_secrets_option
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
        restore_volumes: bool,
        i_mean_it: bool,
        dump_db_first: bool,
        allow_dirty: bool,
        set_hostname: bool,
        force_password_reset: bool,
        api_token: Optional[str],
        secrets_profile: Optional[str],
        no_secrets: bool,
        json: bool,
    ):
        """Update an existing slim-deployed server.

        Default: sync code and config only (does not copy the laptop database).
        Use --restore-volumes or --db-only to replace remote data. No DNS
        changes or server prep.

        \b
        Examples:
          ilha droplet put --domain beta.example.com
          ilha droplet put staging --domain staging.example.com
          ilha droplet put staging --domain staging.example.com --restore-volumes --i-mean-it
          ilha droplet put --domain beta.example.com --db-only
        """
        from ilha.commands.push.slim_deploy_manager import SlimDeployManager

        start_time = time.time()
        deploy_report: Dict[str, Any] = {}
        try:
            secrets_profile = resolve_cli_secrets_profile(
                secrets_profile, no_secrets=no_secrets
            )
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
            if code_only and restore_volumes:
                _print_elapsed(start_time, json)
                if json:
                    JSONOutput.print_error("--code-only and --restore-volumes are mutually exclusive")
                else:
                    error_exit("--code-only and --restore-volumes are mutually exclusive")
                return

            branch_name = auto_detect_branch_name(branch_name, json)

            check_prerequisites_no_git()

            try:
                scp_target, domain, ip, _, _ = resolve_existing_remote_target(
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

            will_restore = restore_volumes or db_only
            if will_restore and domain and not i_mean_it:
                if json:
                    _print_elapsed(start_time, json)
                    JSONOutput.print_error(
                        "Refusing to restore volumes onto a live domain without --i-mean-it"
                    )
                    raise SystemExit(1)
                confirmed = confirm_action(
                    f"This will REPLACE remote volumes on {domain} with local copies.",
                    default="N",
                )
                if not confirmed:
                    error_exit("Volume restore cancelled")

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
                    restore_volumes=restore_volumes,
                    force_password_reset=force_password_reset,
                    secrets_profile=secrets_profile,
                    dump_db_first=dump_db_first,
                    allow_dirty=allow_dirty,
                    set_hostname=branch_name if set_hostname else None,
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
                                **attach_deploy_report_data({}, deploy_report),
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
                data = attach_deploy_report_data(data, deploy_report)
                JSONOutput.print_json(JSONOutput.success("Slim put completed", data=data))

        except Exception as exc:
            _print_elapsed(start_time, json)
            if json:
                JSONOutput.print_json(
                    JSONOutput.error(
                        f"Put error: {exc}",
                        error_code="put_exception",
                        details=attach_deploy_report_data({}, deploy_report),
                    )
                )
                raise SystemExit(1)
            error_exit(f"Put error: {exc}")

    @droplet.command("logs")
    @click.argument("branch_name", required=False)
    @click.option("--scp-target", help="SCP target (user@host:path). Falls back to stored config.")
    @click.option("--domain", help="Domain for the deployment (e.g., beta.example.com)")
    @click.option("--ip", help="IP for HTTP-only. Mutually exclusive with --domain.")
    @click.option("--api-token", help="API token for resolving domain/droplet to SCP target")
    @click.option("--service", "-s", multiple=True, help="Service name(s) to show logs for (default: all services)")
    @click.option("--tail", "-n", "lines", type=int, default=100, show_default=True, help="Number of lines to show")
    @click.option("--follow", "-f", is_flag=True, default=False, help="Follow log output (stream)")
    @add_json_option
    @add_verbose_option
    def droplet_logs(
        branch_name: Optional[str],
        scp_target: Optional[str],
        domain: Optional[str],
        ip: Optional[str],
        api_token: Optional[str],
        service: tuple,
        lines: int,
        follow: bool,
        json: bool,
    ):
        """Tail Docker Compose logs from a remote droplet.

        \b
        Examples:
          ilha droplet logs staging
          ilha droplet logs --domain app.example.com
          ilha droplet logs staging -s web -s db -f
          ilha droplet logs staging -n 200
        """
        import shlex

        check_prerequisites_no_git()
        target = _resolve_target_or_exit(
            branch_name=branch_name,
            scp_target=scp_target,
            domain=domain,
            ip=ip,
            api_token=api_token,
            json=json,
        )
        ssh = SSHConnectionManager()
        svc_args = " ".join(shlex.quote(s) for s in service) if service else ""
        tail_arg = f"--tail {max(1, lines)}"
        follow_arg = "-f" if follow else ""
        cmd = (
            f"{remote_compose_prefix(target.app_root)} logs {tail_arg} {follow_arg} {svc_args} 2>&1"
        )

        if follow:
            ssh_cmd = ssh.build_ssh_command(target.username, target.server, cmd)
            proc = None
            try:
                proc = subprocess.Popen(ssh_cmd, stdout=sys.stdout, stderr=sys.stderr)
                proc.wait()
            except KeyboardInterrupt:
                if proc is not None:
                    proc.terminate()
                    proc.wait()
        else:
            try:
                result = ssh.execute_remote(
                    target.username, target.server, cmd, timeout=60, check=False
                )
                output = (result.stdout or "") + (result.stderr or "")
                if json:
                    JSONOutput.print_json({"logs": output})
                else:
                    print_plain(output.rstrip())
            except Exception as exc:
                if json:
                    JSONOutput.print_error(f"Error fetching logs: {exc}", json_output=True)
                    raise SystemExit(1)
                error_exit(f"Error fetching logs: {exc}")

    @droplet.command("ssh")
    @click.argument("branch_name", required=False)
    @click.argument("command", nargs=-1, type=click.UNPROCESSED)
    @click.option("--scp-target", help="SCP target (user@host:path). Falls back to stored config.")
    @click.option("--domain", help="Domain for the deployment")
    @click.option("--ip", help="IP for HTTP-only. Mutually exclusive with --domain.")
    @click.option("--api-token", help="API token for resolving domain/droplet to SCP target")
    @add_verbose_option
    def droplet_ssh(
        branch_name: Optional[str],
        command: tuple,
        scp_target: Optional[str],
        domain: Optional[str],
        ip: Optional[str],
        api_token: Optional[str],
    ):
        """Open an SSH session (or run a remote command) on a slim droplet.

        \b
        Examples:
          ilha droplet ssh staging
          ilha droplet ssh staging -- docker compose ps
          ilha droplet ssh --domain app.example.com -- uname -a
        """
        check_prerequisites_no_git()
        target = _resolve_target_or_exit(
            branch_name=branch_name,
            scp_target=scp_target,
            domain=domain,
            ip=ip,
            api_token=api_token,
            json=False,
        )
        ssh = SSHConnectionManager()
        remote_cmd = " ".join(command) if command else None
        raise SystemExit(ssh.interactive_ssh(target.username, target.server, remote_cmd))

    @droplet.command("dump-db")
    @click.argument("branch_name", required=False)
    @click.option("--scp-target", help="SCP target (user@host:path). Falls back to stored config.")
    @click.option("--domain", help="Domain for the deployment")
    @click.option("--ip", help="IP for HTTP-only. Mutually exclusive with --domain.")
    @click.option("--api-token", help="API token for resolving domain/droplet to SCP target")
    @click.option(
        "--output",
        type=click.Path(path_type=Path),
        help="Destination file. Default: backups/<label>-<utc>.pgdump",
    )
    @add_json_option
    @add_verbose_option
    def droplet_dump_db(
        branch_name: Optional[str],
        scp_target: Optional[str],
        domain: Optional[str],
        ip: Optional[str],
        api_token: Optional[str],
        output: Optional[Path],
        json: bool,
    ):
        """Dump the remote Postgres database (custom format) into backups/.

        Uses the container's POSTGRES_* environment, not the laptop .env.
        """
        from ilha.commands.push.deploy_service_config import load_deploy_service_config
        from ilha.config.settings import get_project_root

        check_prerequisites_no_git()
        target = _resolve_target_or_exit(
            branch_name=branch_name,
            scp_target=scp_target,
            domain=domain,
            ip=ip,
            api_token=api_token,
            json=json,
        )
        project_root = get_project_root()
        db_service = load_deploy_service_config(project_root).db_service
        try:
            dest = dump_remote_postgres(
                target,
                db_service=db_service,
                project_root=project_root,
                output=output,
            )
        except ilhaCommandError as exc:
            if json:
                JSONOutput.print_error(exc.message)
                raise SystemExit(exc.exit_code or 1)
            error_exit(exc.message)
        if json:
            JSONOutput.print_json(
                JSONOutput.success("Database dump written", {"path": str(dest)})
            )
        else:
            log_success(str(dest))

    @droplet.command("status")
    @click.argument("branch_name", required=False)
    @click.option("--scp-target", help="SCP target (user@host:path). Falls back to stored config.")
    @click.option("--domain", help="Domain for the deployment")
    @click.option("--ip", help="IP for HTTP-only. Mutually exclusive with --domain.")
    @click.option("--api-token", help="API token for resolving domain/droplet to SCP target")
    @add_json_option
    @add_verbose_option
    def droplet_status(
        branch_name: Optional[str],
        scp_target: Optional[str],
        domain: Optional[str],
        ip: Optional[str],
        api_token: Optional[str],
        json: bool,
    ):
        """Show remote /app/.ilha/deploy.json and compare it to local HEAD."""
        from ilha.commands.push.deploy_preflight import DeployPreflight
        from ilha.config.settings import get_project_root

        check_prerequisites_no_git()
        target = _resolve_target_or_exit(
            branch_name=branch_name,
            scp_target=scp_target,
            domain=domain,
            ip=ip,
            api_token=api_token,
            json=json,
        )
        raw = read_remote_deploy_identity(target)
        identity = parse_deploy_identity(raw)
        if not identity:
            message = "No /app/.ilha/deploy.json on the server. Redeploy with a current ilha."
            if json:
                JSONOutput.print_error(message, error_code="missing_deploy_identity")
                raise SystemExit(1)
            error_exit(message)

        project_root = get_project_root()
        preflight = DeployPreflight(project_root)
        worktree = preflight.resolve_worktree_path(target.branch_name)
        local_sha = preflight._git_rev_parse_head(worktree if worktree.exists() else project_root)
        remote_sha = identity.get("git_sha")
        in_sync = bool(local_sha) and remote_sha == local_sha
        payload = {
            "identity": identity,
            "local_sha": local_sha,
            "in_sync": in_sync,
            "server": f"{target.username}@{target.server}",
        }
        if json:
            JSONOutput.print_json(
                JSONOutput.success("Droplet status", payload)
                if in_sync
                else JSONOutput.error(
                    "Remote git_sha does not match local HEAD",
                    error_code="deploy_identity_mismatch",
                    details=payload,
                )
            )
            if not in_sync:
                raise SystemExit(1)
            return
        print_plain(f"Server:     {target.username}@{target.server}")
        print_plain(f"Branch:     {identity.get('branch_name')}")
        print_plain(f"Remote SHA: {remote_sha}")
        print_plain(f"Local SHA:  {local_sha}")
        print_plain(f"Dirty:      {identity.get('git_dirty')}")
        print_plain(f"Deployed:   {identity.get('deployed_at')}")
        print_plain(f"ilha:       {identity.get('ilha_version')}")
        refs = identity.get("image_refs") or {}
        if refs:
            print_plain("Images:")
            for name, ref in refs.items():
                print_plain(f"  {name}: {ref}")
        if in_sync:
            log_success("Remote SHA matches local HEAD")
        else:
            error_exit("Remote git_sha does not match local HEAD")

    @droplet.command("exec")
    @click.argument("branch_name", required=False)
    @click.argument("service")
    @click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
    @click.option("--scp-target", help="SCP target (user@host:path). Falls back to stored config.")
    @click.option("--domain", help="Domain for the deployment")
    @click.option("--ip", help="IP for HTTP-only. Mutually exclusive with --domain.")
    @click.option("--api-token", help="API token for resolving domain/droplet to SCP target")
    @add_verbose_option
    def droplet_exec(
        branch_name: Optional[str],
        service: str,
        command: tuple,
        scp_target: Optional[str],
        domain: Optional[str],
        ip: Optional[str],
        api_token: Optional[str],
    ):
        """Run a command in a remote compose service.

        \b
        Examples:
          ilha droplet exec staging web python manage.py migrate
          ilha droplet exec --domain app.example.com web python manage.py showmigrations
        """
        check_prerequisites_no_git()
        target = _resolve_target_or_exit(
            branch_name=branch_name,
            scp_target=scp_target,
            domain=domain,
            ip=ip,
            api_token=api_token,
            json=False,
        )
        ssh = SSHConnectionManager()
        cmd = remote_compose_exec_command(service, command, app_root=target.app_root)
        result = ssh.execute_remote(
            target.username, target.server, cmd, timeout=600, check=False
        )
        output = (result.stdout or "") + (result.stderr or "")
        if output:
            print_plain(output.rstrip())
        if result.returncode != 0:
            raise SystemExit(result.returncode)

    @droplet.command("rename")
    @click.argument("old_name")
    @click.argument("new_name")
    @click.option("--api-token", help="DigitalOcean API token")
    @click.option(
        "--set-hostname",
        is_flag=True,
        default=False,
        help="Also set the OS hostname on the droplet to NEW",
    )
    @add_json_option
    @add_verbose_option
    def droplet_rename(
        old_name: str,
        new_name: str,
        api_token: Optional[str],
        set_hostname: bool,
        json: bool,
    ):
        """Rename a DigitalOcean droplet (API name). Optionally set OS hostname."""
        token = DropletManager.resolve_droplet_token(api_token)
        provider = DropletManager.create_provider("digitalocean", token) if token else None
        if not provider:
            message = "DigitalOcean API token required to rename a droplet"
            if json:
                JSONOutput.print_error(message)
                raise SystemExit(1)
            error_exit(message)
        matches = [d for d in provider.list_droplets() if d.name == old_name]
        if not matches:
            message = f"Droplet not found: {old_name}"
            if json:
                JSONOutput.print_error(message)
                raise SystemExit(1)
            error_exit(message)
        if len(matches) > 1:
            message = f"Multiple droplets named {old_name!r}; rename by id is not supported yet"
            if json:
                JSONOutput.print_error(message)
                raise SystemExit(1)
            error_exit(message)
        droplet = matches[0]
        if not provider.rename_droplet(droplet.id, new_name):
            message = f"Failed to rename droplet {old_name} → {new_name}"
            if json:
                JSONOutput.print_error(message)
                raise SystemExit(1)
            error_exit(message)
        if set_hostname:
            host = droplet.ip_address or droplet.private_ip_address
            if not host:
                message = "Renamed, but droplet has no IP so hostname was not updated"
                if json:
                    JSONOutput.print_error(message)
                    raise SystemExit(1)
                error_exit(message)
            try:
                set_remote_hostname(
                    RemoteAppTarget(
                        username="root",
                        server=host,
                        scp_target=f"root@{host}:/root",
                        domain=None,
                        ip=host,
                        branch_name=new_name,
                    ),
                    new_name,
                )
            except ilhaCommandError as exc:
                if json:
                    JSONOutput.print_error(exc.message)
                    raise SystemExit(exc.exit_code or 1)
                error_exit(exc.message)
        payload = {"id": droplet.id, "old_name": old_name, "new_name": new_name}
        if json:
            JSONOutput.print_json(JSONOutput.success("Droplet renamed", payload))
        else:
            log_success(f"Renamed {old_name} → {new_name} (id={droplet.id})")

    @droplet.command("doctor")
    @click.argument("branch_name", required=False)
    @add_json_option
    @add_verbose_option
    def droplet_doctor(branch_name: Optional[str], json: bool):
        """Removed. Local worktrees: ``ilha doctor``. Remote: ``ilha droplet logs``."""
        message = (
            "`ilha droplet doctor` was removed. "
            "For a local worktree use `ilha doctor`. "
            "For a remote deployment use `ilha droplet logs`."
        )
        if json:
            JSONOutput.print_error(message, error_code="command_removed", json_output=True)
            raise SystemExit(1)
        error_exit(message)


def _print_elapsed(start_time: float, json: bool) -> None:
    elapsed = time.time() - start_time
    if not json:
        print_plain(f"Total elapsed time: {format_elapsed_time(elapsed)}")
