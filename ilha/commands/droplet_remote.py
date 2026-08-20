"""Shared remote-droplet operations for slim deploys.

CLI commands (logs, ssh, dump-db, status, exec, rename hostname) and
``SlimDeployManager.put`` all go through these helpers so SSH target parsing,
compose exec, and pg_dump are not copied a fourth time.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence, Tuple

from ilha.commands.droplets import auto_detect_branch_name, resolve_existing_remote_target
from ilha.commands.push.remote_layout import REMOTE_APP_ROOT, REMOTE_DEPLOY_IDENTITY
from ilha.exceptions import ilhaCommandError
from ilha.utils.compose_yaml import compose_flags_str
from ilha.utils.file_utils import add_to_gitignore
from ilha.utils.logging import log_info, log_success
from ilha.utils.ssh_manager import SCPTarget, SSHConnectionManager

COMPOSE_FLAGS = compose_flags_str()
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class RemoteAppTarget:
    """Resolved SSH target for an existing slim droplet."""

    username: str
    server: str
    scp_target: str
    domain: Optional[str]
    ip: Optional[str]
    branch_name: str
    app_root: str = REMOTE_APP_ROOT


def parse_scp_user_host(scp_target: str) -> Tuple[str, str]:
    """Return ``(username, server)`` from an SCP target string."""
    try:
        target = SCPTarget(scp_target)
    except ValueError as exc:
        raise ilhaCommandError(str(exc)) from exc
    return target.username, target.server


def resolve_remote_app_target(
    *,
    branch_name: Optional[str],
    scp_target: Optional[str] = None,
    domain: Optional[str] = None,
    ip: Optional[str] = None,
    api_token: Optional[str] = None,
    json: bool = False,
) -> RemoteAppTarget:
    """Resolve branch + SCP target the same way ``ilha droplet logs`` does."""
    if domain and ip:
        raise ilhaCommandError("--domain and --ip are mutually exclusive")
    resolved_branch = auto_detect_branch_name(branch_name, json)
    try:
        resolved_scp, resolved_domain, resolved_ip, _, _ = resolve_existing_remote_target(
            branch_name=resolved_branch,
            scp_target=scp_target,
            domain=domain,
            ip=ip,
            api_token=api_token,
            json=json,
        )
    except ValueError as exc:
        raise ilhaCommandError(str(exc)) from exc
    except ilhaCommandError:
        raise
    except Exception as exc:
        raise ilhaCommandError(f"Error resolving rsync target: {exc}") from exc
    username, server = parse_scp_user_host(resolved_scp)
    return RemoteAppTarget(
        username=username,
        server=server,
        scp_target=resolved_scp,
        domain=resolved_domain or None,
        ip=resolved_ip or None,
        branch_name=resolved_branch,
    )


def remote_compose_prefix(app_root: str = REMOTE_APP_ROOT) -> str:
    """``cd /app && docker compose --env-file .env -f ...`` prefix."""
    return (
        f"cd {shlex.quote(app_root)} && "
        f"docker compose --env-file .env {COMPOSE_FLAGS}"
    )


def remote_compose_exec_command(
    service: str,
    command: Sequence[str],
    *,
    app_root: str = REMOTE_APP_ROOT,
    tty: bool = False,
) -> str:
    """Build a remote ``docker compose exec`` command."""
    tty_flag = "" if tty else "-T "
    cmd = " ".join(shlex.quote(part) for part in command)
    return f"{remote_compose_prefix(app_root)} exec {tty_flag}{shlex.quote(service)} {cmd}"


def dump_postgres_command(
    db_service: str,
    *,
    app_root: str = REMOTE_APP_ROOT,
) -> str:
    """``pg_dump -Fc`` inside the db container using *container* env."""
    inner = (
        'export PGPASSWORD="${POSTGRES_PASSWORD:-}"; '
        'pg_dump -Fc --no-owner -U "${POSTGRES_USER:-postgres}" '
        '"${POSTGRES_DB:-${POSTGRES_USER:-postgres}}"'
    )
    return (
        f"{remote_compose_prefix(app_root)} exec -T {shlex.quote(db_service)} "
        f"sh -c {shlex.quote(inner)}"
    )


def sanitize_backup_label(label: str) -> str:
    cleaned = _SAFE_LABEL.sub("-", (label or "").strip()).strip("-")
    return cleaned or "droplet"


def resolve_dump_output_path(
    project_root: Path,
    *,
    output: Optional[Path],
    label: str,
) -> Path:
    """Default dumps land in ``backups/``. Explicit ``--output`` may be anywhere."""
    backups = (Path(project_root) / "backups").resolve()
    if output is None:
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return backups / f"{sanitize_backup_label(label)}-{stamp}.pgdump"
    resolved = Path(output).expanduser()
    if not resolved.is_absolute():
        resolved = (Path.cwd() / resolved).resolve()
    else:
        resolved = resolved.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def dump_remote_postgres(
    target: RemoteAppTarget,
    *,
    db_service: str,
    project_root: Path,
    output: Optional[Path] = None,
    ssh: Optional[SSHConnectionManager] = None,
    timeout: int = 600,
) -> Path:
    """Stream a custom-format Postgres dump from the remote db container."""
    if not db_service:
        raise ilhaCommandError(
            "No database service configured for this project. "
            "Set deploy.db_service in .ilha/config.yml."
        )
    add_to_gitignore(project_root, "backups/")
    label = target.domain or target.branch_name or target.server
    dest = resolve_dump_output_path(project_root, output=output, label=label)
    conn = ssh or SSHConnectionManager()
    conn.ensure_host_key(target.server)
    cmd = dump_postgres_command(db_service, app_root=target.app_root)
    log_info(f"Dumping remote database ({db_service}) from {target.username}@{target.server}...")
    result = conn.execute_remote_bytes(
        target.username,
        target.server,
        cmd,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise ilhaCommandError(
            f"Remote pg_dump failed (exit {result.returncode}): {err or 'no stderr'}"
        )
    payload = result.stdout or b""
    if not payload:
        raise ilhaCommandError("Remote pg_dump produced no output")
    dest.write_bytes(payload)
    log_success(f"Wrote database dump: {dest} ({len(payload)} bytes)")
    return dest


def read_remote_text_file(
    target: RemoteAppTarget,
    remote_path: str,
    *,
    ssh: Optional[SSHConnectionManager] = None,
    timeout: int = 30,
) -> str:
    """Read a remote file as text. Empty string if missing."""
    conn = ssh or SSHConnectionManager()
    conn.ensure_host_key(target.server)
    quoted = shlex.quote(remote_path)
    result = conn.execute_remote(
        target.username,
        target.server,
        f"test -f {quoted} && cat {quoted} || true",
        timeout=timeout,
        check=False,
    )
    return result.stdout or ""


def read_remote_deploy_identity(
    target: RemoteAppTarget,
    *,
    ssh: Optional[SSHConnectionManager] = None,
) -> str:
    return read_remote_text_file(target, REMOTE_DEPLOY_IDENTITY, ssh=ssh)


def set_remote_hostname(
    target: RemoteAppTarget,
    hostname: str,
    *,
    ssh: Optional[SSHConnectionManager] = None,
) -> None:
    """Set the OS hostname via hostnamectl (best-effort fallback to hostname)."""
    safe = hostname.strip()
    if not safe or not re.match(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$", safe):
        raise ilhaCommandError(f"Invalid hostname: {hostname!r}")
    conn = ssh or SSHConnectionManager()
    conn.ensure_host_key(target.server)
    cmd = (
        f"hostnamectl set-hostname {shlex.quote(safe)} 2>/dev/null "
        f"|| hostname {shlex.quote(safe)}"
    )
    result = conn.execute_remote(
        target.username, target.server, cmd, timeout=30, check=False
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise ilhaCommandError(f"Failed to set hostname {safe!r}: {err}")
    log_success(f"Set remote hostname to {safe}")


def compose_ps_health_command(
    services: Sequence[str],
    *,
    app_root: str = REMOTE_APP_ROOT,
) -> str:
    """Check that each service has a running container."""
    checks = []
    for service in services:
        name = str(service or "").strip()
        if not name:
            continue
        quoted = shlex.quote(name)
        checks.append(
            f"cid=$({remote_compose_prefix(app_root)} ps -q {quoted} 2>/dev/null | head -1); "
            f'test -n "$cid" || {{ echo missing:{name}; exit 1; }}; '
            f'status=$(docker inspect -f "{{{{.State.Status}}}}" "$cid" 2>/dev/null || true); '
            f'test "$status" = running || {{ echo unhealthy:{name}:$status; exit 1; }}'
        )
    body = " && ".join(checks) if checks else "true"
    return f"set -euo pipefail; {body}; echo ok"
