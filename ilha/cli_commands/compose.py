"""
Docker compose passthrough command.
"""

from __future__ import annotations

import click

from ilha.cli.helpers import add_verbose_option, command_wrapper
from ilha.core.docker_manager import DockerManager
from ilha.core.git_manager import GitManager
from ilha.exceptions import ilhaCommandError


def register_commands(cli) -> None:
    @cli.command(
        context_settings=dict(
            ignore_unknown_options=True,
            allow_extra_args=True,
        )
    )
    @click.argument("branch_name")
    @add_verbose_option
    @command_wrapper()
    def compose(branch_name: str):
        """Run docker compose command for a worktree (base + overlay merged)."""
        ctx = click.get_current_context()
        compose_args_list = ctx.args

        git_manager = GitManager()
        if not git_manager.validate_worktree_exists(branch_name):
            raise ilhaCommandError(
                f"Worktree for branch '{branch_name}' does not exist. Please create it first with: ilha create {branch_name}"
            )

        # Existence of base + overlay is checked by run_compose_passthrough;
        # it surfaces specific error messages for each missing file.
        docker_manager = DockerManager()
        success = docker_manager.run_compose_passthrough(branch_name, compose_args_list)
        if not success:
            raise ilhaCommandError(f"Failed to run docker compose command for worktree '{branch_name}'")

