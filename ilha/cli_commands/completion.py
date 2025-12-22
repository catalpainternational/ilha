"""
Shell completion commands.
"""

from __future__ import annotations

import click

from ilha.cli.helpers import add_verbose_option


def register_commands(cli) -> None:
    @cli.command("help")
    @add_verbose_option
    def show_help():
        """Show help information."""
        click.echo(cli.get_help(click.Context(cli)))

    @cli.group()
    @add_verbose_option
    def completion():
        """Manage shell completion for ilha."""
        pass

    @completion.command("install")
    @click.argument("shell", required=False)
    @add_verbose_option
    def completion_install(shell: str):
        """Install shell completion for ilha."""
        try:
            from ilha.commands.completion import CompletionManager

            completion_manager = CompletionManager()
            success = completion_manager.install_completion(shell)
            if not success:
                raise RuntimeError("Failed to install shell completion")
        except Exception as exc:
            raise click.ClickException(str(exc))

    @completion.command("uninstall")
    @add_verbose_option
    def completion_uninstall():
        """Remove shell completion for ilha."""
        try:
            from ilha.commands.completion import CompletionManager

            completion_manager = CompletionManager()
            success = completion_manager.uninstall_completion()
            if not success:
                raise RuntimeError("Failed to uninstall shell completion")
        except Exception as exc:
            raise click.ClickException(str(exc))

    @completion.command("status")
    @add_verbose_option
    def completion_status():
        """Show shell completion installation status."""
        try:
            from ilha.commands.completion import CompletionManager

            completion_manager = CompletionManager()
            completion_manager.show_completion_status()
        except Exception as exc:
            raise click.ClickException(str(exc))

    @cli.command(hidden=True)
    @click.argument("completion_type")
    @add_verbose_option
    def _completion(completion_type: str):
        """Hidden command for shell completion support."""
        from ilha.utils.completion_helper import get_completion_for_context, print_completions

        try:
            completions = get_completion_for_context(completion_type)
            print_completions(completions)
        except Exception:
            pass


