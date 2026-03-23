"""
Shell completion commands.
"""

from __future__ import annotations

import click

from ilha.cli.helpers import add_verbose_option, wrap_click_errors


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
    @wrap_click_errors("Failed to install shell completion")
    def completion_install(shell: str):
        """Install shell completion for ilha."""
        from ilha.commands.completion import CompletionManager

        completion_manager = CompletionManager()
        success = completion_manager.install_completion(shell)
        if not success:
            raise click.ClickException("Failed to install shell completion")

    @completion.command("uninstall")
    @add_verbose_option
    @wrap_click_errors("Failed to uninstall shell completion")
    def completion_uninstall():
        """Remove shell completion for ilha."""
        from ilha.commands.completion import CompletionManager

        completion_manager = CompletionManager()
        success = completion_manager.uninstall_completion()
        if not success:
            raise click.ClickException("Failed to uninstall shell completion")

    @completion.command("status")
    @add_verbose_option
    @wrap_click_errors("Failed to read shell completion status")
    def completion_status():
        """Show shell completion installation status."""
        from ilha.commands.completion import CompletionManager

        completion_manager = CompletionManager()
        completion_manager.show_completion_status()

    @cli.command(hidden=True)
    @click.argument("completion_type")
    @add_verbose_option
    def _completion(completion_type: str):
        """Hidden command for shell completion support."""
        from ilha.utils.completion_helper import get_completion_for_context, print_completions

        try:
            completions = get_completion_for_context(completion_type)
        except Exception:
            completions = []
        print_completions(completions)

