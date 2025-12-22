"""
Environment package commands.
"""

from __future__ import annotations

from pathlib import Path

import click

from ilha.cli.helpers import add_json_option, add_verbose_option, command_wrapper
from ilha.commands.packages import PackageCommands
from ilha.exceptions import ilhaCommandError
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import error_exit, log_success
from ilha.utils.validation import check_prerequisites, check_prerequisites_no_git, check_setup_or_prompt


def register_commands(cli) -> None:
    @cli.group()
    @add_verbose_option
    def packages():
        """Manage environment packages for sharing and deployment."""

    @packages.command("export")
    @click.argument("branch_name")
    @click.option("--output-dir", type=click.Path(), default="./packages", help="Output directory for packages (default: ./packages)")
    @click.option("--include-code/--no-code", default=True, help="Include git archive of code in package (default: True)")
    @click.option("--compressed/--no-compress", default=True, help="Compress package to .tar.gz format (default: True)")
    @click.option("--skip-volumes", is_flag=True, default=False, help="Skip volume backup (fallback when volume backup fails)")
    @click.option("--use-staging-certificates", is_flag=True, default=False, help="Use Let's Encrypt staging certificates (doesn't count against rate limits)")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_setup=True, require_prerequisites=True)
    def export_package(branch_name: str, output_dir: str, include_code: bool, compressed: bool, skip_volumes: bool, json: bool, use_staging_certificates: bool):
        package_commands = PackageCommands()
        success = package_commands.export(branch_name, Path(output_dir), include_code, compressed, skip_volumes, use_staging_certificates)
        if not success:
            raise ilhaCommandError(f"Failed to export package for {branch_name}")
        log_success(f"Package exported successfully for {branch_name}")
        if json:
            return JSONOutput.success("Package exported", {"branch_name": branch_name, "output_dir": output_dir})

    @packages.command("import")
    @click.argument("package_file", type=click.Path(exists=True))
    @click.option("--target-branch", help="Target branch name (for normal mode, optional)")
    @click.option("--restore-data/--no-data", default=True, help="Restore volume data from backup (default: True)")
    @click.option("--standalone", is_flag=True, default=None, help="Force standalone mode (create new project from package)")
    @click.option(
        "--target-dir",
        type=click.Path(),
        help="Target directory for standalone import (default: {project_name}-standalone)",
    )
    @click.option("--domain", help="Domain override (subdomain.domain.tld) for production/staging deployments")
    @click.option("--ip", help="IP override for HTTP-only deployments (no TLS)")
    @click.option("--non-interactive", is_flag=True, default=False, help="Run import/setup non-interactively (auto-accept safe defaults)")
    @add_json_option
    @add_verbose_option
    @command_wrapper(require_setup=False, require_prerequisites=False)
    def import_package(
        package_file: str,
        target_branch: str,
        restore_data: bool,
        standalone: bool,
        target_dir: str,
        domain: str,
        ip: str,
        non_interactive: bool,
        json: bool,
    ):
        if standalone is False:
            check_setup_or_prompt()
        check_prerequisites_no_git()
        package_commands = PackageCommands()
        if domain and ip:
            raise ilhaCommandError("Options --domain and --ip are mutually exclusive")
        success = package_commands.import_package(
            Path(package_file),
            target_branch,
            restore_data,
            standalone=standalone,
            target_directory=Path(target_dir) if target_dir else None,
            domain=domain,
            ip=ip,
            non_interactive=non_interactive,
        )
        if not success:
            raise ilhaCommandError(f"Failed to import package {package_file}")
        log_success(f"Package imported from {package_file}")
        if json:
            return JSONOutput.success("Package imported", {"package_file": package_file, "target_branch": target_branch})

    @packages.command("list")
    @click.option("--package-dir", type=click.Path(), default="./packages", help="Package directory to search (default: ./packages)")
    @add_json_option
    @add_verbose_option
    def list_packages(package_dir: str, json: bool):
        try:
            check_setup_or_prompt()
            check_prerequisites()
            package_commands = PackageCommands()
            if json:
                packages_data = package_commands.list_packages_json(Path(package_dir))
                JSONOutput.print_json(packages_data)
            else:
                package_commands.list_packages(Path(package_dir))
        except Exception as exc:
            if json:
                JSONOutput.print_error(f"Error listing packages: {exc}")
            else:
                error_exit(f"Error listing packages: {exc}")

    @packages.command("validate")
    @click.argument("package_file", type=click.Path(exists=True))
    @add_json_option
    @add_verbose_option
    def validate_package(package_file: str, json: bool):
        try:
            check_setup_or_prompt()
            check_prerequisites()
            package_commands = PackageCommands()
            if json:
                result = package_commands.validate_package_json(Path(package_file))
                JSONOutput.print_json(result)
            else:
                success = package_commands.validate_package(Path(package_file))
                if not success:
                    error_exit(f"Package validation failed for {package_file}")
        except Exception as exc:
            if json:
                JSONOutput.print_error(f"Error validating package: {exc}")
            else:
                error_exit(f"Error validating package: {exc}")


