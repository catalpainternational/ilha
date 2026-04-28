"""
Database management commands.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

import click

from ilha.cli.helpers import add_verbose_option, command_wrapper
from ilha.config.settings import get_project_root
from ilha.utils.logging import log_error, log_info, log_success, log_warning


def register_commands(cli) -> None:
    """Register the ``ilha db`` sub-commands."""

    @cli.group()
    @add_verbose_option
    def db():
        """Database operations on the local worktree."""

    @db.command("reset-password")
    @click.option(
        "--container",
        "container_override",
        help="Postgres container name (default: ${COMPOSE_PROJECT_NAME}-db).",
    )
    @click.option(
        "--yes",
        is_flag=True,
        default=False,
        help="Skip the interactive confirmation.",
    )
    @add_verbose_option
    @command_wrapper()
    def db_reset_password(container_override: Optional[str], yes: bool):
        """Re-align the local Postgres role password with .env.

        Use when ``psql`` rejects the password from .env: most often after a
        worktree's .env was rotated but pg_authid still holds the old hash, or
        after restoring a volume backup that was made under a different password.

        Reads POSTGRES_USER + POSTGRES_PASSWORD from the merged .env files,
        temporarily relaxes pg_hba.conf to trust local connections, runs
        ``ALTER USER`` over a loopback connection, then restores pg_hba.conf
        and restarts the db container. No data is touched.

        The db container is restarted twice during the operation; running web
        and worker containers will see brief connection errors and reconnect.
        """
        ok = _reset_local_pg_password(container_override=container_override, confirm=not yes)
        if not ok:
            raise SystemExit(1)


# ───────────────────── helpers ────────────────────────


def _reset_local_pg_password(*, container_override: Optional[str], confirm: bool) -> bool:
    project_root = get_project_root()
    env = _read_merged_env(project_root)
    user = env.get("POSTGRES_USER")
    password = env.get("POSTGRES_PASSWORD")
    db_name = env.get("POSTGRES_DB") or user or "postgres"
    project_name = env.get("COMPOSE_PROJECT_NAME")

    if not user or not password:
        log_error("POSTGRES_USER and POSTGRES_PASSWORD must both be set in .env or .ilha/env.ilha.")
        return False

    container = container_override or (f"{project_name}-db" if project_name else "")
    if not container:
        log_error(
            "Cannot determine db container name. Set COMPOSE_PROJECT_NAME in .env "
            "or pass --container <name>."
        )
        return False

    if not _container_running(container):
        log_error(
            f"Container {container!r} is not running. Bring the worktree up first "
            "(e.g. `ilha <branch> up`)."
        )
        return False

    pg_hba_host_path = _discover_pg_hba_mount(container)
    if pg_hba_host_path is None:
        log_error(
            f"Could not find a bind-mounted pg_hba.conf on container {container!r}. "
            "This command requires pg_hba.conf to be mounted from the host so it can "
            "be temporarily rewritten and the container restarted to pick up changes."
        )
        return False

    log_info(f"Container:      {container}")
    log_info(f"Role:           {user}")
    log_info(f"pg_hba.conf:    {pg_hba_host_path}")
    log_info("This will restart the db container twice (~5–10s of disconnection).")
    if confirm and not click.confirm("Proceed?", default=True):
        log_warning("Aborted.")
        return False

    original = pg_hba_host_path.read_text()
    if "scram-sha-256" not in original and "md5" not in original:
        log_error(
            "pg_hba.conf has no scram-sha-256 or md5 auth methods to flip to trust. "
            "This command's strategy (temporary trust override) cannot work here; "
            "either edit pg_hba.conf manually or reset the role's password through "
            "another channel (e.g. via a known-password connection)."
        )
        return False

    restore_failed_reason: Optional[str] = None
    try:
        # Step 1: temporary trust override (host-side; container's RO mount sees the new bytes after restart)
        trust_text = original.replace("scram-sha-256", "trust").replace("md5", "trust")
        pg_hba_host_path.write_text(trust_text)
        log_info("pg_hba.conf temporarily set to trust local connections.")

        # Step 2: restart db so it loads the new pg_hba.conf
        if not _docker_run(["restart", container], timeout=60):
            log_error(f"Failed to restart {container}.")
            return False
        if not _wait_pg_ready(container, attempts=60):
            log_error(f"{container} did not become ready after restart.")
            return False

        # Step 3: ALTER USER over loopback (now in trust auth)
        sql = f"ALTER USER {_quote_ident(user)} WITH PASSWORD {_quote_literal(password)};"
        proc = subprocess.run(
            [
                "docker", "exec", container,
                "psql", "-h", "127.0.0.1", "-U", user, "-d", db_name, "-w", "-c", sql,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            log_error(f"ALTER USER failed:\n{proc.stderr.strip()[:600]}")
            return False
        log_info("ALTER USER succeeded.")

    finally:
        # Step 4: always restore pg_hba.conf — even if the try block hit an
        # early `return False`, this finally still runs. Log the restore
        # failure HERE (not after the finally) so it's visible regardless of
        # how the try exited; otherwise a step-2/3 failure would short-circuit
        # the function before any post-finally code could warn that
        # pg_hba.conf is now stuck in trust mode.
        try:
            pg_hba_host_path.write_text(original)
            log_info("pg_hba.conf restored.")
        except Exception as exc:
            restore_failed_reason = str(exc)
            log_error(
                f"Failed to restore pg_hba.conf at {pg_hba_host_path}: {exc}. "
                "Restore manually before restarting the container!"
            )
    if restore_failed_reason is not None:
        return False

    # Step 5: restart again so the original pg_hba.conf takes effect
    if not _docker_run(["restart", container], timeout=60):
        log_error(f"Failed to restart {container} after restoring pg_hba.conf.")
        return False
    if not _wait_pg_ready(container, attempts=60):
        log_error(f"{container} did not become ready after the final restart.")
        return False

    # Step 6: verify the new password actually works under the original auth method
    verify = subprocess.run(
        [
            "docker", "exec", "-e", f"PGPASSWORD={password}", container,
            "psql", "-h", "127.0.0.1", "-U", user, "-d", db_name, "-w", "-c", "SELECT 1;",
        ],
        capture_output=True, text=True, timeout=15,
    )
    if verify.returncode != 0:
        log_error(
            "Post-reset auth check failed — pg_hba.conf is restored but the password "
            f"does not authenticate. stderr: {verify.stderr.strip()[:400]}"
        )
        return False

    log_success(f"Postgres role {user!r} on {container!r} now accepts the password from .env.")
    return True


def _read_env_file(path: Path) -> Dict[str, str]:
    """Parse a simple KEY=VALUE .env file, stripping wrap quotes."""
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def _read_merged_env(project_root: Path) -> Dict[str, str]:
    """.env then .ilha/env.ilha — later wins (matches compose env_file ordering)."""
    merged: Dict[str, str] = {}
    merged.update(_read_env_file(project_root / ".env"))
    merged.update(_read_env_file(project_root / ".ilha" / "env.ilha"))
    return merged


def _container_running(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", name],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _discover_pg_hba_mount(container: str) -> Optional[Path]:
    """Look at the container's mounts and return the host path for pg_hba.conf."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Mounts}}", container],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return None
    try:
        mounts: List[Dict[str, str]] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    for m in mounts:
        dest = m.get("Destination", "")
        src = m.get("Source", "")
        if dest.endswith("/pg_hba.conf") and src:
            # Docker Desktop on Mac reports bind-mount sources prefixed with
            # ``/host_mnt`` (the Linux VM's view). The host-side path drops it.
            if src.startswith("/host_mnt/"):
                src = src[len("/host_mnt"):]
            return Path(src)
    return None


def _docker_run(args: List[str], *, timeout: int) -> bool:
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _wait_pg_ready(container: str, *, attempts: int) -> bool:
    for _ in range(attempts):
        result = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-q"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            return True
        time.sleep(1)
    return False


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
