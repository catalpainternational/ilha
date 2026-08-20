"""Remote ``docker login`` for DigitalOcean Container Registry.

The registry token is passed on SSH stdin, never argv or a remote file.
Workers and slim deploy share this helper.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ilha.commands.secrets import SecretsManager
from ilha.config.settings import ILHA_DIR
from ilha.exceptions import ilhaCommandError
from ilha.utils.ssh_manager import SSHConnectionManager

DEFAULT_REGISTRY = "registry.digitalocean.com"
DEFAULT_REGISTRY_TOKEN_ENV = "DOCR_READ_TOKEN"


def registry_token_env_name(project_root: Path) -> str:
    """Token env var: ``worker_pool.registry_token_env`` or ``DOCR_READ_TOKEN``."""
    config_path = Path(project_root) / ILHA_DIR / "config.yml"
    if not config_path.exists():
        return DEFAULT_REGISTRY_TOKEN_ENV
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return DEFAULT_REGISTRY_TOKEN_ENV
    pool = loaded.get("worker_pool") if isinstance(loaded, dict) else None
    if not isinstance(pool, dict):
        return DEFAULT_REGISTRY_TOKEN_ENV
    name = str(pool.get("registry_token_env") or "").strip()
    return name or DEFAULT_REGISTRY_TOKEN_ENV


def resolve_registry_read_token(
    project_root: Path,
    *,
    secrets_profile: Optional[str] = None,
) -> str:
    """Resolve the DOCR read token from the shell or a SOPS profile."""
    token_env = registry_token_env_name(project_root)
    token = SecretsManager(project_root).resolve_registry_token(
        token_env,
        profile_name=secrets_profile,
    )
    if not token:
        raise ilhaCommandError(
            f"Registry token {token_env} is required to pull published images. "
            "Set it in the environment or configure a SOPS secrets profile."
        )
    return token


def docker_login_remote(
    ssh: SSHConnectionManager,
    username: str,
    host: str,
    token: str,
    *,
    registry: str = DEFAULT_REGISTRY,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    """Log the remote Docker daemon into *registry* using *token* on stdin."""
    if not token:
        raise ilhaCommandError("Registry token is required to docker login")
    login_cmd = (
        "read -r t; "
        f'printf %s "$t" | docker login {shlex.quote(registry)} -u "$t" --password-stdin'
    )
    ssh_login = ssh.build_ssh_command(username, host, login_cmd)
    login_result = run(
        ssh_login,
        input=token + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if login_result.returncode != 0:
        err = (login_result.stderr or login_result.stdout or "").strip()
        raise ilhaCommandError(f"docker login failed on {host}: {err}")
