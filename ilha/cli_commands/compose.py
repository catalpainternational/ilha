"""
Docker compose passthrough command.
"""

from __future__ import annotations

from typing import Optional

import click

from ilha.cli.helpers import add_secrets_option, add_verbose_option, command_wrapper
from ilha.commands.secrets import resolve_cli_secrets_profile
from ilha.core.docker_manager import DockerManager


def register_commands(cli) -> None:
    @cli.command(
        context_settings=dict(
            ignore_unknown_options=True,
            allow_extra_args=True,
        )
    )
    @click.argument("branch_name")
    @add_secrets_option
    @add_verbose_option
    @command_wrapper()
    def compose(branch_name: str, secrets_profile: Optional[str], no_secrets: bool):
        """Run docker compose command for a worktree (base + overlay merged)."""
        ctx = click.get_current_context()
        secrets_profile = resolve_cli_secrets_profile(
            secrets_profile, no_secrets=no_secrets
        )
        # Worktree, compose files, env file, and docker compose invocation
        # are all validated inside run_compose_passthrough, which raises
        # ilhaCommandError on any real ilha/docker failure. A non-zero return
        # value here is the inner command's exit code and is propagated
        # transparently to the parent shell.
        exit_code = DockerManager().run_compose_passthrough(
            branch_name,
            ctx.args,
            secrets_profile=secrets_profile,
        )
        if exit_code != 0:
            ctx.exit(exit_code)

