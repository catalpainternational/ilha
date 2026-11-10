"""Ephemeral worker droplet CLI commands."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

import click

from ilha.cli.helpers import add_json_option, add_secrets_option, add_verbose_option, command_wrapper
from ilha.commands.droplets import DropletCommands, auto_detect_branch_name
from ilha.commands.secrets import resolve_cli_secrets_profile
from ilha.commands.worker import DEFAULT_WORKER_TAG, WorkerManager
from ilha.core.droplet_manager import DropletManager
from ilha.exceptions import ilhaCommandError
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import log_info, log_success, log_warning, print_plain


def _fleet_scope_tags(mgr: WorkerManager, *, destructive: bool) -> List[str]:
    """Project tags that scope fleet listings and destroys.

    Without a real project context the scope tag would be derived from the
    bare cwd name — fine to warn about for a read-only listing, but never an
    acceptable basis for a destroy.
    """
    if mgr.has_project_context():
        return [mgr.project_tag()]
    if destructive:
        raise ilhaCommandError(
            "No ilha project context here (.ilha/config.yml not found). "
            "Run from the project directory, or destroy specific droplets with --ids."
        )
    log_warning("No ilha project context; listing worker droplets across all projects")
    return []


def _resolve_central_private_ip(
    *,
    central_ip: Optional[str],
    central_droplet_name: Optional[str],
    api_token: Optional[str],
) -> str:
    if central_ip:
        return central_ip.strip()
    if not central_droplet_name:
        raise ilhaCommandError("Either --central-ip or --central-droplet-name is required")

    token = DropletManager.resolve_droplet_token(api_token)
    provider = DropletManager.create_provider("digitalocean", token) if token else None
    if not provider:
        raise ilhaCommandError("DigitalOcean API token required to resolve central droplet")

    droplets = provider.list_droplets()
    matches = [d for d in droplets if d.name == central_droplet_name]
    if not matches:
        raise ilhaCommandError(f"Central droplet not found: {central_droplet_name}")
    if len(matches) > 1:
        raise ilhaCommandError(f"Multiple droplets named {central_droplet_name!r}; use --central-ip")
    central = matches[0]
    if not central.private_ip_address:
        raise ilhaCommandError(
            f"Central droplet {central_droplet_name!r} has no private IP; ensure VPC networking is enabled"
        )
    return central.private_ip_address


def _resolve_vpc_uuid(
    *,
    vpc_uuid: Optional[str],
    central_droplet_name: Optional[str],
    api_token: Optional[str],
) -> Optional[str]:
    if vpc_uuid:
        return vpc_uuid
    if not central_droplet_name:
        return None

    token = DropletManager.resolve_droplet_token(api_token)
    provider = DropletManager.create_provider("digitalocean", token) if token else None
    if not provider:
        return None

    for droplet in provider.list_droplets():
        if droplet.name == central_droplet_name and droplet.vpc_uuid:
            return droplet.vpc_uuid
    return None


def register_commands(cli) -> None:
    @cli.group()
    @add_verbose_option
    def worker():
        """Manage ephemeral RQ worker droplets (bundle, deploy, list, reap, destroy)."""

    @worker.command("bundle")
    @click.argument("branch_name", required=False)
    @click.option("--central-ip", help="Central server private IP for Redis/DB")
    @click.option("--central-droplet-name", help="Resolve central private IP from droplet name")
    @click.option("--worker-service", help="Compose worker service name (default: rq-worker-1)")
    @click.option("--output-dir", type=click.Path(path_type=Path), help="Directory to write bundle files")
    @click.option("--cloud-init", is_flag=True, default=False, help="Print rendered cloud-init user data")
    @click.option("--api-token", help="DigitalOcean API token (for resolving central droplet)")
    @add_secrets_option
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def worker_bundle(
        branch_name: Optional[str],
        central_ip: Optional[str],
        central_droplet_name: Optional[str],
        worker_service: Optional[str],
        output_dir: Optional[click.Path],
        cloud_init: bool,
        secrets_profile: Optional[str],
        no_secrets: bool,
        json: bool,
        api_token: Optional[str] = None,
    ):
        """Generate a minimal worker compose bundle for ephemeral droplets.

        \b
        Examples:
          ilha worker bundle staging --central-droplet-name central
          ilha worker bundle staging --central-ip 10.0.0.5 --cloud-init
        """
        secrets_profile = resolve_cli_secrets_profile(
            secrets_profile, no_secrets=no_secrets
        )
        branch = auto_detect_branch_name(branch_name, json)

        mgr = WorkerManager()
        pool_cfg = mgr.load_pool_config()
        resolved_ip = _resolve_central_private_ip(
            central_ip=central_ip,
            central_droplet_name=central_droplet_name or pool_cfg.central_droplet_name,
            api_token=api_token,
        )
        bundle = mgr.build_bundle(
            branch,
            resolved_ip,
            worker_service=worker_service,
            output_dir=output_dir,
            secrets_profile=secrets_profile,
        )
        payload = {
            "branch": branch,
            "worker_service": bundle.worker_service,
            "image_ref": bundle.image_ref,
            "central_private_ip": bundle.central_private_ip,
            "output_dir": str(bundle.output_dir),
            "compose_path": str(bundle.compose_path),
            "env_path": str(bundle.env_path),
        }
        if cloud_init:
            payload["user_data"] = mgr.render_cloud_init(bundle)

        if json:
            return JSONOutput.success("Worker bundle generated", payload)
        log_success(f"Worker bundle: {bundle.output_dir}")
        print_plain(f"  Image: {bundle.image_ref}")
        print_plain(f"  Central IP: {bundle.central_private_ip}")
        if cloud_init:
            print_plain("")
            print_plain(payload["user_data"])

    def _deploy_worker_droplets(
        *,
        branch_name: Optional[str],
        count: int,
        central_ip: Optional[str],
        central_droplet_name: Optional[str],
        worker_service: Optional[str],
        region: Optional[str],
        size: Optional[str],
        image: Optional[str],
        ssh_keys: Optional[str],
        tags: tuple,
        vpc_uuid: Optional[str],
        api_token: Optional[str],
        wait: bool,
        secrets_profile: Optional[str],
        json: bool,
    ):
        if count < 1:
            raise ilhaCommandError("--count must be at least 1")

        branch = auto_detect_branch_name(branch_name, json)

        mgr = WorkerManager()
        pool_cfg = mgr.load_pool_config()
        central_name = central_droplet_name or pool_cfg.central_droplet_name
        ssh_keys_list = [k.strip() for k in ssh_keys.split(",")] if ssh_keys else list(pool_cfg.ssh_keys)
        droplet_cmds = DropletCommands()

        existing = droplet_cmds.list_droplets_by_tag(
            DEFAULT_WORKER_TAG,
            api_token=api_token,
            branch=branch,
            require_tags=_fleet_scope_tags(mgr, destructive=False),
        )
        WorkerManager.assert_worker_deploy_preflight(
            wait=wait,
            ssh_keys=ssh_keys_list,
            count=count,
            existing_count=len(existing),
            max_workers=pool_cfg.max_workers,
        )
        registry_token = mgr.resolve_registry_token(secrets_profile=secrets_profile)
        resolved_ip = _resolve_central_private_ip(
            central_ip=central_ip,
            central_droplet_name=central_name,
            api_token=api_token,
        )
        resolved_vpc = _resolve_vpc_uuid(
            vpc_uuid=vpc_uuid,
            central_droplet_name=central_name,
            api_token=api_token,
        )

        bundle = mgr.build_bundle(
            branch,
            resolved_ip,
            worker_service=worker_service,
            secrets_profile=secrets_profile,
        )
        user_data = mgr.render_cloud_init(bundle)
        start_index = WorkerManager.next_worker_index([d.name for d in existing])

        droplet_tags = mgr.worker_tags(branch, list(tags))

        region = region or pool_cfg.region
        size = size or pool_cfg.size
        image = image or pool_cfg.image
        names = [mgr.generate_worker_name(branch, start_index + offset) for offset in range(count)]

        def launch(name: str) -> dict:
            """Create one droplet and start its worker; destroy it if the start fails.

            Runs once per droplet, all at once: each worker's wait for boot,
            cloud-init and the image pull is minutes, and in sequence a fleet
            of four took four times that, so short batches ended before the
            later workers came up (2026-11-10: 2 of 4 during a 57-job run).
            """
            droplet = droplet_cmds.create_droplet(
                name=name,
                region=region,
                size=size,
                image=image,
                ssh_keys=ssh_keys_list or None,
                tags=droplet_tags,
                wait=True,
                api_token=api_token,
                vpc_uuid=resolved_vpc,
                user_data=user_data,
                # Never the outer json flag: with it, this inner step printed
                # its own envelope before ours, and stdout carried TWO JSON
                # objects — unparseable as one document, which the autoscaler
                # correctly refused to trust (2026-08-21).
                json=False,
                quiet=True,
                return_droplet=True,
            )
            if not droplet:
                raise ilhaCommandError(f"Failed to create worker droplet: {name}")
            row = {
                "id": droplet.id,
                "name": droplet.name,
                "ip_address": droplet.ip_address,
                "private_ip_address": droplet.private_ip_address,
                "status": droplet.status,
            }
            if not json:
                log_info(f"Created worker droplet {droplet.name} (id={droplet.id})")
            host = droplet.ip_address or droplet.private_ip_address
            try:
                mgr.bootstrap_worker(bundle, host or "", registry_token=registry_token)
            except ilhaCommandError as exc:
                # A droplet without a running worker bills and counts toward
                # max_workers while consuming nothing. It was created seconds
                # ago by this call, so its id is certain: destroy it now.
                destroyed = droplet_cmds.destroy_droplet(
                    droplet_id=droplet.id,
                    force=True,
                    api_token=api_token,
                    json=False,
                    only_droplet=True,
                )
                raise ilhaCommandError(
                    f"Created {name} but failed to start its worker: {exc.message}",
                    details={"droplet": {**row, "destroyed": bool(destroyed)}},
                ) from exc
            return row

        created: List[dict] = []
        failed: List[dict] = []
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = {pool.submit(launch, name): name for name in names}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    created.append(future.result())
                except Exception as exc:  # each droplet succeeds or fails on its own
                    entry = {"name": name, "error": getattr(exc, "message", None) or str(exc)}
                    entry.update((getattr(exc, "details", None) or {}).get("droplet", {}))
                    failed.append(entry)
        created.sort(key=lambda row: row["name"])
        failed.sort(key=lambda row: row["name"])

        if failed:
            # Started workers ride along in details: they are live and billing,
            # and the caller must be able to see them. So does any failed
            # droplet whose destroy did not go through ("destroyed": false).
            raise ilhaCommandError(
                f"Started {len(created)} of {count} worker droplet(s); failed: "
                + "; ".join(f"{f['name']}: {f['error']}" for f in failed),
                details={"created": created, "failed": failed},
            )

        payload = {"branch": branch, "count": len(created), "droplets": created}
        if json:
            return JSONOutput.success("Worker droplets created", payload)
        log_success(f"Created {len(created)} worker droplet(s)")

    @worker.command("deploy")
    @click.argument("branch_name", required=False)
    @click.option("--count", type=int, default=1, show_default=True, help="Number of worker droplets to create")
    @click.option("--central-ip", help="Central server private IP for Redis/DB")
    @click.option("--central-droplet-name", help="Resolve central private IP and VPC from droplet name")
    @click.option("--worker-service", help="Compose worker service name (default: rq-worker-1)")
    @click.option("--region", help="Droplet region override")
    @click.option("--size", help="Droplet size override")
    @click.option("--image", help="Droplet OS image override")
    @click.option("--ssh-keys", help="Comma-separated SSH key names")
    @click.option("--tags", multiple=True, help="Additional droplet tags")
    @click.option("--vpc-uuid", help="VPC UUID override")
    @click.option("--api-token", help="DigitalOcean API token")
    @click.option(
        "--wait/--no-wait",
        default=True,
        show_default=True,
        help="Wait for SSH and copy env/registry token. --no-wait is rejected (secrets are not in user_data).",
    )
    @add_secrets_option
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_prerequisites=False)
    def worker_deploy(
        branch_name: Optional[str],
        count: int,
        central_ip: Optional[str],
        central_droplet_name: Optional[str],
        worker_service: Optional[str],
        region: Optional[str],
        size: Optional[str],
        image: Optional[str],
        ssh_keys: Optional[str],
        tags: tuple,
        vpc_uuid: Optional[str],
        api_token: Optional[str],
        wait: bool,
        secrets_profile: Optional[str],
        no_secrets: bool,
        json: bool,
    ):
        """Deploy ephemeral worker droplets.

        Cloud-init only installs Docker. Env and the registry token are copied
        over SSH after the droplet is up (user_data is plaintext in DO metadata).

        \b
        Examples:
          ilha worker deploy staging --count 2 --central-droplet-name central
          ilha worker deploy staging --central-ip 10.0.0.5 --json
        """
        secrets_profile = resolve_cli_secrets_profile(
            secrets_profile, no_secrets=no_secrets
        )
        return _deploy_worker_droplets(
            branch_name=branch_name,
            count=count,
            central_ip=central_ip,
            central_droplet_name=central_droplet_name,
            worker_service=worker_service,
            region=region,
            size=size,
            image=image,
            ssh_keys=ssh_keys,
            tags=tags,
            vpc_uuid=vpc_uuid,
            api_token=api_token,
            wait=wait,
            secrets_profile=secrets_profile,
            json=json,
        )

    @worker.command("spawn", hidden=True)
    @click.argument("branch_name", required=False)
    @click.option("--count", type=int, default=1, show_default=True, help="Number of worker droplets to create")
    @click.option("--central-ip", help="Central server private IP for Redis/DB")
    @click.option("--central-droplet-name", help="Resolve central private IP and VPC from droplet name")
    @click.option("--worker-service", help="Compose worker service name (default: rq-worker-1)")
    @click.option("--region", help="Droplet region override")
    @click.option("--size", help="Droplet size override")
    @click.option("--image", help="Droplet OS image override")
    @click.option("--ssh-keys", help="Comma-separated SSH key names")
    @click.option("--tags", multiple=True, help="Additional droplet tags")
    @click.option("--vpc-uuid", help="VPC UUID override")
    @click.option("--api-token", help="DigitalOcean API token")
    @click.option(
        "--wait/--no-wait",
        default=True,
        show_default=True,
        help="Wait for SSH and copy env/registry token. --no-wait is rejected (secrets are not in user_data).",
    )
    @add_secrets_option
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_prerequisites=False)
    def worker_spawn(
        branch_name: Optional[str],
        count: int,
        central_ip: Optional[str],
        central_droplet_name: Optional[str],
        worker_service: Optional[str],
        region: Optional[str],
        size: Optional[str],
        image: Optional[str],
        ssh_keys: Optional[str],
        tags: tuple,
        vpc_uuid: Optional[str],
        api_token: Optional[str],
        wait: bool,
        secrets_profile: Optional[str],
        no_secrets: bool,
        json: bool,
    ):
        """Deprecated alias for ``worker deploy``."""
        secrets_profile = resolve_cli_secrets_profile(
            secrets_profile, no_secrets=no_secrets
        )
        return _deploy_worker_droplets(
            branch_name=branch_name,
            count=count,
            central_ip=central_ip,
            central_droplet_name=central_droplet_name,
            worker_service=worker_service,
            region=region,
            size=size,
            image=image,
            ssh_keys=ssh_keys,
            tags=tags,
            vpc_uuid=vpc_uuid,
            api_token=api_token,
            wait=wait,
            secrets_profile=secrets_profile,
            json=json,
        )

    @worker.command("list")
    @click.option("--branch", help="Filter by branch tag")
    @click.option("--api-token", help="DigitalOcean API token")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_prerequisites=False)
    def worker_list(branch: Optional[str], api_token: Optional[str], json: bool):
        """List ephemeral worker droplets.

        \b
        Examples:
          ilha worker list
          ilha worker list --branch staging --json
        """
        droplets = DropletCommands().list_droplets_by_tag(
            DEFAULT_WORKER_TAG,
            api_token=api_token,
            branch=branch,
            require_tags=_fleet_scope_tags(WorkerManager(), destructive=False),
        )
        rows = [
            {
                "id": d.id,
                "name": d.name,
                "status": d.status,
                "ip_address": d.ip_address,
                "private_ip_address": d.private_ip_address,
                "region": d.region,
                "size": d.size,
                "tags": d.tags,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in droplets
        ]
        if json:
            return JSONOutput.success("Worker droplets listed", {"droplets": rows, "count": len(rows)})

        if not rows:
            print_plain("No worker droplets found.")
            return
        for row in rows:
            print_plain(
                f"{row['name']} (id={row['id']}, status={row['status']}, ip={row['ip_address'] or 'pending'})"
            )

    def _destroy_worker_droplets(
        *,
        ids: Optional[str],
        destroy_all: bool,
        branch: Optional[str],
        force: bool,
        api_token: Optional[str],
        json: bool,
    ):
        droplet_cmds = DropletCommands()
        target_ids: List[int] = []

        if ids:
            # json=False: mid-command JSON error prints would corrupt stdout
            # (two JSON documents); resolution failures are raised instead.
            resolved, errors = droplet_cmds._resolve_droplet_identifiers(ids, api_token, False)
            if errors:
                raise ilhaCommandError(
                    "Could not resolve droplet identifier(s): "
                    + ", ".join(e["identifier"] or "?" for e in errors),
                    details={"unresolved": errors, "resolved": resolved},
                )
            target_ids = resolved
        elif destroy_all:
            droplets = droplet_cmds.list_droplets_by_tag(
                DEFAULT_WORKER_TAG,
                api_token=api_token,
                branch=branch,
                require_tags=_fleet_scope_tags(WorkerManager(), destructive=True),
            )
            target_ids = [d.id for d in droplets]
        else:
            raise ilhaCommandError("Specify --ids or --all")

        if not target_ids:
            if json:
                return JSONOutput.success("No worker droplets to destroy", {"destroyed": []})
            print_plain("No worker droplets to destroy.")
            return

        destroyed = []
        failed = []
        for droplet_id in target_ids:
            ok = droplet_cmds.destroy_droplet(
                droplet_id=droplet_id,
                force=force,
                api_token=api_token,
                json=False,
                only_droplet=True,
            )
            if ok:
                destroyed.append(droplet_id)
            else:
                failed.append(droplet_id)

        if failed:
            # A failed destroy leaves billing droplets behind; that is an
            # error outcome (success: false, exit != 0), never a success
            # message with a footnote.
            raise ilhaCommandError(
                f"Failed to destroy droplet(s): {', '.join(map(str, failed))}",
                details={"destroyed": destroyed, "failed": failed},
            )
        if json:
            return JSONOutput.success("Worker droplets destroyed", {"destroyed": destroyed, "failed": []})
        log_success(f"Destroyed {len(destroyed)} worker droplet(s)")

    @worker.command("destroy")
    @click.option("--ids", help="Comma-separated droplet IDs or names to destroy")
    @click.option("--all", "destroy_all", is_flag=True, default=False, help="Destroy all worker-tagged droplets")
    @click.option("--branch", help="When using --all, filter by branch tag")
    @click.option("--force", is_flag=True, default=False, help="Skip confirmation prompts")
    @click.option("--api-token", help="DigitalOcean API token")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_prerequisites=False)
    def worker_destroy(
        ids: Optional[str],
        destroy_all: bool,
        branch: Optional[str],
        force: bool,
        api_token: Optional[str],
        json: bool,
    ):
        """Destroy ephemeral worker droplets.

        \b
        Examples:
          ilha worker destroy --all --branch staging --force
          ilha worker destroy --ids worker-staging-1,12345678
        """
        return _destroy_worker_droplets(
            ids=ids,
            destroy_all=destroy_all,
            branch=branch,
            force=force,
            api_token=api_token,
            json=json,
        )

    @worker.command("reap")
    @click.option("--branch", help="Filter by branch tag")
    @click.option("--dry-run", is_flag=True, default=False, help="List idle workers without destroying them")
    @click.option("--force", is_flag=True, default=False, help="Skip confirmation prompts")
    @click.option("--api-token", help="DigitalOcean API token")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_prerequisites=False)
    def worker_reap(
        branch: Optional[str],
        dry_run: bool,
        force: bool,
        api_token: Optional[str],
        json: bool,
    ):
        """Destroy worker droplets older than worker_pool.idle_destroy_after_seconds.

        Droplets with no created_at timestamp are left alone. This is an age
        cap, not a Redis-idle probe.

        \b
        Examples:
          ilha worker reap --branch staging --dry-run
          ilha worker reap --branch staging --force
        """
        mgr = WorkerManager()
        pool_cfg = mgr.load_pool_config()
        droplets = DropletCommands().list_droplets_by_tag(
            DEFAULT_WORKER_TAG,
            api_token=api_token,
            branch=branch,
            require_tags=_fleet_scope_tags(mgr, destructive=True),
        )
        idle = WorkerManager.select_idle_droplets(
            droplets,
            idle_after_seconds=pool_cfg.idle_destroy_after_seconds,
        )
        rows = [
            {
                "id": d.id,
                "name": d.name,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in idle
        ]
        payload = {
            "idle_after_seconds": pool_cfg.idle_destroy_after_seconds,
            "count": len(rows),
            "droplets": rows,
        }
        if dry_run or not idle:
            if json:
                return JSONOutput.success(
                    "Idle worker droplets listed" if dry_run else "No idle worker droplets",
                    payload,
                )
            if not idle:
                print_plain("No idle worker droplets to reap.")
                return
            for row in rows:
                print_plain(f"{row['name']} (id={row['id']}, created_at={row['created_at']})")
            return

        if not force:
            if json:
                raise ilhaCommandError(
                    "Pass --force to destroy idle workers, or --dry-run to list them"
                )
            names = ", ".join(d.name for d in idle)
            if not click.confirm(f"Destroy {len(idle)} idle worker droplet(s)? {names}"):
                print_plain("Aborted.")
                return

        return _destroy_worker_droplets(
            ids=",".join(str(d.id) for d in idle),
            destroy_all=False,
            branch=branch,
            force=True,
            api_token=api_token,
            json=json,
        )
