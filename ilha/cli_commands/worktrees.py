"""
Worktree lifecycle ilha CLI commands.
"""

from __future__ import annotations

import click

from typing import Optional

from ilha.cli.helpers import add_json_option, add_secrets_option, add_verbose_option, command_wrapper
from ilha.commands.utility import UtilityManager
from ilha.commands.worktree import WorktreeManager
from ilha.exceptions import ilhaCommandError
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import log_info, log_success
from ilha.utils.pattern_matcher import has_wildcard


def register_commands(cli) -> None:
    """Register worktree management commands."""

    @cli.command()
    @click.argument("branch_name")
    @click.option(
        "--force",
        is_flag=True,
        help="Recreate ilha environment for existing worktrees (volumes, env files). Use when worktree exists but env is missing or you want a fresh copy from main.",
    )
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def create(branch_name: str, force: bool, json: bool):
        """Create a new worktree or ilha environment for an existing worktree (database, caddy network, env files)."""
        worktree_manager = WorktreeManager()
        success, result_data = worktree_manager.create_worktree(branch_name, interactive=not json, force=force)
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
    @add_secrets_option
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def up(
        branch_name: str,
        detach: bool,
        profile: Optional[str],
        secrets_profile: Optional[str],
        json: bool,
    ):
        """Start the worktree environment for the specified branch."""
        if not detach:
            raise ilhaCommandError("Usage: ilha <worktree_name> up -d")
        worktree_manager = WorktreeManager()
        success = worktree_manager.start_worktree(
            branch_name,
            profile=profile,
            secrets_profile=secrets_profile,
        )
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

