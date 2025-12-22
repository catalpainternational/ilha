"""
Server import command for ilha.

This command runs on remote servers to import packages and start services.
It's called via SSH from the push command.
"""

from __future__ import annotations

import click

from ilha.cli.helpers import add_verbose_option, command_wrapper
from ilha.core.server_import_orchestrator import ServerImportOrchestrator
from ilha.exceptions import ilhaCommandError
from ilha.utils.logging import log_error, log_success


def register_commands(cli) -> None:
    @cli.command("server-import")
    @click.argument("package_path", type=click.Path(exists=True))
    @click.option("--branch-name", required=True, help="Branch name for the worktree")
    @click.option("--domain", help="Domain for Caddy routing (subdomain.domain.tld)")
    @click.option("--ip", help="IP for HTTP-only routing (no TLS)")
    @click.option("--build", is_flag=True, default=False, help="Rebuild Docker images after import")
    @click.option(
        "--debug",
        is_flag=True,
        default=False,
        help="Enable DEBUG mode in production deployment (default: False for production)"
    )
    @click.option("--start/--no-start", default=True, help="Start services after import (default: True)")
    @click.option("--use-staging-certificates", is_flag=True, default=False, help="Use Let's Encrypt staging certificates (doesn't count against rate limits)")
    @add_verbose_option
    @command_wrapper(require_setup=False, require_prerequisites=False)
    def server_import(package_path: str, branch_name: str, domain: str, ip: str, build: bool, debug: bool, start: bool, use_staging_certificates: bool):
        """Import package and start services (runs on remote server).
        
        This command is typically invoked via SSH from the push command.
        It imports a ilha package and optionally starts the services.
        """
        if domain and ip:
            raise ilhaCommandError("Options --domain and --ip are mutually exclusive")
        
        orchestrator = ServerImportOrchestrator()
        result = orchestrator.import_and_start(
            package_path=package_path,
            branch_name=branch_name,
            domain=domain,
            ip=ip,
            build=build,
            debug=debug,
            start=start
        )
        
        if not result.get("success"):
            error_msg = result.get("error", "Unknown error")
            log_error(f"Server import failed: {error_msg}")
            raise ilhaCommandError(f"Server import failed: {error_msg}")
        
        log_success("Server import completed successfully")
        
        if result.get("containers"):
            containers = result["containers"]
            log_success(f"{containers['running']} container(s) running for branch {branch_name}")

