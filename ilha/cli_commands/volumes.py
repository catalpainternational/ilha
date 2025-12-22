"""
Volume management commands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ilha.cli.helpers import add_json_option, add_verbose_option, command_wrapper
from ilha.commands.volumes import VolumeManager
from ilha.exceptions import ilhaCommandError
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import log_success


def register_commands(cli) -> None:
    """Register the ``ilha volumes`` sub-commands."""

    @cli.group()
    @add_verbose_option
    def volumes():
        """Manage Docker volumes for worktrees."""

    @volumes.command("list")
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def volumes_list(json: bool):
        volume_manager = VolumeManager()
        if json:
            return volume_manager.list_volumes_json()
        volume_manager.list_volumes()

    @volumes.command("size")
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def volumes_size(json: bool):
        volume_manager = VolumeManager()
        if json:
            return volume_manager.get_volume_sizes_json()
        volume_manager.show_volume_sizes()

    @volumes.command("backup")
    @click.argument("branch_name")
    @click.option("--backup-dir", type=click.Path(), help="Directory to save backup (default: ./backups)")
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def volumes_backup(branch_name: str, backup_dir: Optional[str], json: bool):
        volume_manager = VolumeManager()
        backup_path = Path(backup_dir) if backup_dir else None
        success = volume_manager.backup_volumes(branch_name, backup_path)
        if not success:
            raise ilhaCommandError(f"Failed to backup volumes for {branch_name}")
        log_success(f"Successfully backed up volumes for {branch_name}")
        if json:
            return JSONOutput.success(f"Successfully backed up volumes for {branch_name}")

    @volumes.command("restore")
    @click.argument("branch_name")
    @click.argument("backup_file", type=click.Path(exists=True))
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def volumes_restore(branch_name: str, backup_file: str, json: bool):
        volume_manager = VolumeManager()
        success = volume_manager.restore_volumes(branch_name, Path(backup_file))
        if not success:
            raise ilhaCommandError(f"Failed to restore volumes for {branch_name}")
        log_success(f"Successfully restored volumes for {branch_name}")
        if json:
            return JSONOutput.success(f"Successfully restored volumes for {branch_name}")

    @volumes.command("clean")
    @click.argument("branch_name")
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def volumes_clean(branch_name: str, json: bool):
        volume_manager = VolumeManager()
        success = volume_manager.clean_volumes(branch_name)
        if not success:
            raise ilhaCommandError(f"Failed to clean volumes for {branch_name}")
        log_success(f"Successfully cleaned volumes for {branch_name}")
        if json:
            return JSONOutput.success(f"Successfully cleaned volumes for {branch_name}")


