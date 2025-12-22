"""
Setup and maintenance commands.
"""

from __future__ import annotations

import click

from ilha.cli.helpers import add_verbose_option
from ilha.commands.setup import SetupManager
from ilha.utils.logging import error_exit
from ilha.utils.validation import check_prerequisites


def register_commands(cli) -> None:
    @cli.command()
    @click.option("--project-name", help="Project name (default: directory name)")
    @click.option(
        "--monkey-patch",
        is_flag=True,
        default=False,
        help="If a Django project is detected, auto-patch settings.py to read environment variables",
    )
    @click.option("--examples", is_flag=True, default=False, help="Regenerate example config files in examples/ directory")
    @click.option("--force", is_flag=True, default=False, help="Force overwrite existing .ilha configuration files")
    @add_verbose_option
    def setup(project_name: str, monkey_patch: bool, examples: bool, force: bool):
        """Initialize ilha for this project."""
        try:
            setup_manager = SetupManager()
            if examples:
                check_prerequisites(project_root=setup_manager.project_root)
                success = setup_manager._regenerate_example_files()
                if not success:
                    error_exit("Failed to regenerate example files")
                return
            check_prerequisites(project_root=setup_manager.project_root)
            success = setup_manager.setup_project(project_name, monkey_patch=monkey_patch, force=force)
            if not success:
                error_exit("Failed to setup ilha for this project")
        except Exception as exc:
            error_exit(f"Error setting up ilha: {exc}")

    @cli.command("clean-legacy")
    @add_verbose_option
    def clean_legacy():
        """Clean legacy ilha elements from docker-compose.yml."""
        try:
            setup_manager = SetupManager()
            success = setup_manager.clean_legacy_elements()
            if not success:
                error_exit("Failed to clean legacy ilha elements")
        except Exception as exc:
            error_exit(f"Error cleaning legacy elements: {exc}")


