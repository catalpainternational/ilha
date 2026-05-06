"""
Docker compose passthrough command.
"""

from __future__ import annotations

import click

from ilha.cli.helpers import add_verbose_option, command_wrapper
from ilha.core.docker_manager import DockerManager


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
        # Worktree, compose files, env file, and docker compose invocation
        # are all validated inside run_compose_passthrough, which raises
        # ilhaCommandError on any real ilha/docker failure. A non-zero return
        # value here is the inner command's exit code and is propagated
        # transparently to the parent shell.
        exit_code = DockerManager().run_compose_passthrough(branch_name, ctx.args)
        if exit_code != 0:
            ctx.exit(exit_code)

