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

from ilha.cli.helpers import add_json_option, add_verbose_option
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
from ilha.utils.json_output import JSONOutput
from ilha.utils.network import resolve_server_ip
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
        "--strict-env-contract/--no-strict-env-contract",
        default=True,
        show_default=True,
        help="Block deploy when env values have known-bad shapes (e.g. SITE_DOMAIN with http:// prefix). Prevents the CSRF-breakage class; pass --no-strict-env-contract to bypass.",
    )
    @click.option(
        "--expected-ssh-fingerprint",
        help="SHA256:… fingerprint of the server's SSH host key. If set, a mismatch aborts the deploy (MITM defense). Without it, the first-seen key is trusted (TOFU) and logged so you can pin it on the next deploy.",
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
        restore_volumes: bool,
        no_build: bool,
        force_password_reset: bool,
        strict_deploy_drift: bool,
        strict_env_contract: bool,
        expected_ssh_fingerprint: Optional[str],
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

            # Auto-detect branch
            branch_name = auto_detect_branch_name(branch_name, json)
            if not branch_name:
                return

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
                    strict_env_contract=strict_env_contract,
                    smoke_timeout=smoke_timeout,
                    expected_ssh_fingerprint=expected_ssh_fingerprint,
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
    @click.option(
        "--force-password-reset",
        is_flag=True,
        default=False,
        help="Skip Postgres password validation after volume restore (dangerous; use only when intentional).",
    )
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
        force_password_reset: bool,
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

            branch_name = auto_detect_branch_name(branch_name, json)
            if not branch_name:
                return

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
                    force_password_reset=force_password_reset,
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
            else:
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

        if domain and ip:
            if json:
                JSONOutput.print_error("--domain and --ip are mutually exclusive")
            else:
                error_exit("--domain and --ip are mutually exclusive")
            return

        branch_name = auto_detect_branch_name(branch_name, json)
        if not branch_name:
            return

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
            if json:
                JSONOutput.print_error(str(exc))
            else:
                error_exit(str(exc))
            return
        except Exception as exc:
            if json:
                JSONOutput.print_error(f"Error resolving target: {exc}")
            else:
                error_exit(f"Error resolving target: {exc}")
            return

        # Parse scp_target → username, server
        if "@" not in scp_target:
            if json:
                JSONOutput.print_error(f"Cannot parse SCP target: {scp_target}")
            else:
                error_exit(f"Cannot parse SCP target: {scp_target}")
            return
        user_host, _, _ = scp_target.partition(":")
        username, _, server = user_host.partition("@")

        from ilha.utils.ssh_manager import SSHManager
        ssh = SSHManager()
        remote_app_root = "/root/app"

        # Build docker compose logs command
        svc_args = " ".join(shlex.quote(s) for s in service) if service else ""
        tail_arg = f"--tail {max(1, lines)}"
        follow_arg = "-f" if follow else ""
        cmd = f"cd {shlex.quote(remote_app_root)} && docker compose --env-file .env logs {tail_arg} {follow_arg} {svc_args} 2>&1"

        if follow:
            # Stream mode: pipe directly to terminal
            ssh_cmd = ssh.build_ssh_command(username, server, cmd)
            try:
                proc = subprocess.Popen(ssh_cmd, stdout=sys.stdout, stderr=sys.stderr)
                proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                proc.wait()
        else:
            try:
                result = ssh.execute_remote(username, server, cmd, timeout=60, check=False)
                output = (result.stdout or "") + (result.stderr or "")
                if json:
                    JSONOutput.print_json({"logs": output})
                else:
                    print_plain(output.rstrip())
            except Exception as exc:
                if json:
                    JSONOutput.print_error(f"Error fetching logs: {exc}")
                else:
                    error_exit(f"Error fetching logs: {exc}")

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

        branch_name = auto_detect_branch_name(branch_name, json)
        if not branch_name:
            return

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
