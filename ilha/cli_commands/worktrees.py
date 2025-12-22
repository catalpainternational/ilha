"""
Worktree lifecycle ilha CLI commands.
"""

from __future__ import annotations

from typing import Optional

import click

from pathlib import Path
from typing import Optional

from ilha.cli.helpers import add_json_option, add_verbose_option, command_wrapper
from ilha.commands.push.push_manager import PushManager
from ilha.commands.utility import UtilityManager
from ilha.commands.worktree import WorktreeManager
from ilha.exceptions import ilhaCommandError
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import error_exit, log_info, log_success, log_warning
from ilha.utils.pattern_matcher import has_wildcard


def register_commands(cli) -> None:
    """Register worktree management commands."""

    @cli.command()
    @click.argument("branch_name")
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def create(branch_name: str, json: bool):
        """Create a new worktree for the specified branch."""
        worktree_manager = WorktreeManager()
        success, result_data = worktree_manager.create_worktree(branch_name, interactive=not json)
        if not success:
            raise ilhaCommandError(f"Failed to create worktree for {branch_name}")
        data = (result_data or {}).get("data", {}) if result_data else {}
        worktree_path = data.get("worktree_path")
        status = data.get("status", "created")
        log_success(f"Worktree ready for {branch_name}")
        if json:
            return JSONOutput.success(
                f"Worktree ready for {branch_name}",
                {
                    "branch_name": branch_name,
                    "worktree_path": worktree_path,
                    "status": status,
                },
            )

    @cli.command()
    @click.argument("branch_name")
    @click.option(
        "-d",
        "--detach",
        is_flag=True,
        default=True,
        help="Run containers in detached mode (default: True)",
    )
    @click.option("--profile", help="Docker Compose profile to use (e.g., worker)")
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def up(branch_name: str, detach: bool, profile: Optional[str], json: bool):
        """Start the worktree environment for the specified branch."""
        if not detach:
            raise ilhaCommandError("Usage: ilha <worktree_name> up -d")
        worktree_manager = WorktreeManager()
        success = worktree_manager.start_worktree(branch_name, profile=profile)
        if not success:
            raise ilhaCommandError(f"Failed to start worktree environment for {branch_name}")
        log_success(f"Worktree environment started for {branch_name}")
        if json:
            access_url = worktree_manager.env_manager.get_access_url(branch_name)
            return JSONOutput.success(
                f"Worktree environment started for {branch_name}",
                {"branch_name": branch_name, "url": access_url, "profile": profile},
            )

    @cli.command()
    @click.argument("branch_name")
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def down(branch_name: str, json: bool):
        """Stop the worktree environment for the specified branch."""
        worktree_manager = WorktreeManager()
        success = worktree_manager.stop_worktree(branch_name)
        if not success:
            raise ilhaCommandError(f"Failed to stop worktree environment for {branch_name}")
        log_success(f"Worktree environment stopped for {branch_name}")
        if json:
            return JSONOutput.success(
                f"Worktree environment stopped for {branch_name}",
                {"branch_name": branch_name},
            )

    def _handle_pattern_operation(
        branch_name: str,
        force: bool,
        delete_branch: bool,
        action: str,
        json: bool,
    ):
        worktree_manager = WorktreeManager()
        if has_wildcard(branch_name):
            success = worktree_manager.remove_worktrees_by_pattern(branch_name, force, delete_branch=delete_branch)
            if not success:
                raise ilhaCommandError(f"Failed to remove worktrees matching pattern: {branch_name}")
            log_success(f"Successfully removed worktrees matching pattern: {branch_name}")
            if json:
                return JSONOutput.success(
                    f"Successfully removed worktrees matching pattern: {branch_name}",
                    {"pattern": branch_name, "action": action},
                )
        else:
            success = worktree_manager.remove_worktree(branch_name, force, delete_branch=delete_branch)
            if not success:
                raise ilhaCommandError(f"Failed to remove worktree for {branch_name}")
            log_success(f"Successfully removed worktree for {branch_name}")
            if json:
                return JSONOutput.success(
                    f"Successfully removed worktree for {branch_name}",
                    {"branch_name": branch_name, "action": action},
                )

    @cli.command()
    @click.argument("branch_name")
    @click.option("--force", is_flag=True, help="Force deletion even with unmerged changes (skip confirmation)")
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def delete(branch_name: str, force: bool, json: bool):
        """Delete worktree and Git branch completely."""
        return _handle_pattern_operation(branch_name, force, True, "delete", json)

    @cli.command()
    @click.option("--force", is_flag=True, help="Force deletion without confirmation")
    @add_verbose_option
    @command_wrapper()
    def delete_all(force: bool):
        """Delete all worktrees, containers, volumes, and Git branches."""
        worktree_manager = WorktreeManager()
        success = worktree_manager.remove_all_worktrees(force)
        if not success:
            raise ilhaCommandError("Failed to remove all worktrees")
        log_success("Removed all worktrees")

    @cli.command()
    @click.argument("branch_name")
    @click.option("--force", is_flag=True, help="Force removal even with unmerged changes (skip confirmation)")
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def remove(branch_name: str, force: bool, json: bool):
        """Remove worktree and containers/volumes but keep the Git branch."""
        return _handle_pattern_operation(branch_name, force, False, "remove", json)

    @cli.command()
    @click.option("--force", is_flag=True, help="Force removal without confirmation")
    @add_verbose_option
    @command_wrapper()
    def remove_all(force: bool):
        """Remove all worktrees and containers/volumes but keep Git branches."""
        worktree_manager = WorktreeManager()
        success = worktree_manager.remove_all_worktrees(force, delete_branch=False)
        if not success:
            raise ilhaCommandError("Failed to remove all worktrees")
        log_success("Removed all worktrees (branches preserved)")

    @cli.command()
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def list(json: bool):
        """List all active worktrees."""
        utility_manager = UtilityManager()
        if json:
            worktrees = utility_manager.list_worktrees_json()
            return worktrees
        utility_manager.list_worktrees()

    @cli.command()
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def prune(json: bool):
        """Remove prunable worktree references."""
        utility_manager = UtilityManager()
        if json:
            pruned_count = utility_manager.prune_worktrees_json()
            return JSONOutput.success("Pruned worktrees", {"pruned_count": pruned_count})
        pruned_count = utility_manager.prune_worktrees()
        log_info(f"Pruned {pruned_count} worktree(s)")

    @cli.command()
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
        "--containers",
        help="Comma-separated list of worktree.container patterns to push only specific containers and their volumes (e.g., feature-auth.db,feature-auth.redis)",
    )
    @click.option(
        "--exclude-deps",
        help="Comma-separated list of service names to exclude from dependency resolution (e.g., db,redis). Useful when deploying workers that connect to remote services.",
    )
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_setup=True, require_prerequisites=True)
    def push(
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
        containers: Optional[str],
        exclude_deps: Optional[str],
        json: bool,
    ):
        """Push worktree package to remote server via SCP."""
        if domain and ip:
            if json:
                JSONOutput.print_error("Options --domain and --ip are mutually exclusive")
            else:
                error_exit("Options --domain and --ip are mutually exclusive")
            return
        
        if not code_only and not scp_target:
            if json:
                JSONOutput.print_error("scp_target is required (or use --code-only with stored config)")
            else:
                error_exit("scp_target is required (or use --code-only with stored config)")
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
            containers=containers,
            exclude_deps=exclude_deps_list,
        )
        
        if not success:
            if json:
                JSONOutput.print_error("Failed to push package")
            else:
                error_exit("Failed to push package")
        else:
            if json:
                JSONOutput.print_success("Package pushed successfully")
            else:
                log_success("Package pushed successfully")


