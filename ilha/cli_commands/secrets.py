"""CLI commands for SOPS-backed secret profiles."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ilha.cli.helpers import add_json_option, add_verbose_option, command_wrapper
from ilha.commands.secrets import SecretsManager, SopsBackend
from ilha.exceptions import ilhaCommandError
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import log_success, print_plain


def register_commands(cli) -> None:
    @cli.group()
    @add_verbose_option
    def secrets():
        """Manage SOPS-encrypted secret profiles."""

    @secrets.command("list")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_prerequisites=False)
    def secrets_list(json: bool):
        """List configured secret profiles."""
        mgr = SecretsManager()
        profiles = mgr.list_profiles()
        default_profile = mgr.default_profile_name()
        payload = {
            "profiles": profiles,
            "default_profile": default_profile,
            "provider": mgr.load_secrets_config().get("provider") or "sops",
        }
        if json:
            return JSONOutput.success("Secret profiles", payload)
        print_plain("Secret profiles:")
        for name in profiles:
            marker = " (default)" if name == default_profile else ""
            profile = mgr.resolve_profile(name)
            print_plain(f"  - {name}: {profile.file}{marker}")

    @secrets.command("edit")
    @click.argument("profile_name")
    @add_verbose_option
    @command_wrapper(require_prerequisites=False)
    def secrets_edit(profile_name: str):
        """Edit an encrypted secret profile with sops."""
        mgr = SecretsManager()
        profile = mgr.resolve_profile(profile_name)
        exit_code = SopsBackend.edit_file(profile.file)
        if exit_code != 0:
            raise ilhaCommandError(f"sops edit exited with code {exit_code}")
        log_success(f"Edited secret profile {profile_name}")

    @secrets.command("print")
    @click.argument("profile_name", required=False)
    @click.option("--redact", is_flag=True, default=False, help="Redact sensitive values")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_prerequisites=False)
    def secrets_print(profile_name: Optional[str], redact: bool, json: bool):
        """Print decrypted env vars for a profile."""
        mgr = SecretsManager()
        env = mgr.decrypt_profile(profile_name)
        if redact:
            env = mgr.redact(env)
        if json:
            return JSONOutput.success(
                "Secret profile env",
                {"profile": profile_name or mgr.default_profile_name(), "env": env},
            )
        for key in sorted(env):
            print_plain(f"{key}={env[key]}")

    @secrets.command("exec")
    @click.argument("profile_name")
    @click.argument("command", nargs=-1, required=True)
    @add_verbose_option
    @command_wrapper(require_prerequisites=False)
    def secrets_exec(profile_name: str, command: tuple):
        """Run a command with decrypted profile env injected via sops exec-env."""
        mgr = SecretsManager()
        profile = mgr.resolve_profile(profile_name)
        if not command:
            raise ilhaCommandError("Command is required after --")
        exit_code = SopsBackend.exec_with_file(profile.file, list(command))
        if exit_code != 0:
            raise ilhaCommandError(f"Command exited with code {exit_code}")

    @secrets.command("materialize")
    @click.argument("profile_name")
    @click.option(
        "--output",
        "output_path",
        type=click.Path(path_type=Path),
        required=True,
        help="Plaintext env file path to write",
    )
    @click.option("--force", is_flag=True, default=False, help="Overwrite existing output file")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_prerequisites=False)
    def secrets_materialize(
        profile_name: str,
        output_path: Path,
        force: bool,
        json: bool,
    ):
        """Decrypt a profile to a plaintext env file (use sparingly)."""
        mgr = SecretsManager()
        written = mgr.materialize_plaintext(profile_name, output_path, force=force)
        payload = {"profile": profile_name, "output": str(written)}
        if json:
            return JSONOutput.success("Secret profile materialized", payload)
        log_success(f"Wrote plaintext env to {written}")
