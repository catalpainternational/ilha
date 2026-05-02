"""
Remote Compose discovery for slim deploy and doctor.

``docker compose config --services`` alone is fragile: it can exit non-zero
(interpolation errors, warnings treated as errors on some versions, profiles)
even after ``docker compose up`` succeeded. We prefer ``docker compose ps -q
<service>`` when a container exists, then fall back to ``config --services``.
"""

from __future__ import annotations

import re
import shlex
from typing import Callable, Tuple

RunRemote = Callable[[str, str, str, int], Tuple[int, str, str]]

_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

# Compose flags shared with slim_deploy_manager: every remote `docker compose`
# call passes both the user's compose (base) and ilha's overlay.
_COMPOSE_FLAGS = "-f docker-compose.yml -f docker-compose.overlay.yml"


def assert_safe_compose_service_name(name: str) -> str:
    if not name or not _SERVICE_NAME_RE.match(name):
        raise ValueError(f"Invalid compose service name: {name!r}")
    return name


def remote_compose_service_known(
    run_remote: RunRemote,
    *,
    username: str,
    server: str,
    remote_app_root: str,
    service_name: str,
    timeout: int = 15,
) -> bool:
    """Return True if the project has *service_name* (running container or compose config)."""
    assert_safe_compose_service_name(service_name)
    qroot = shlex.quote(remote_app_root)
    qsvc = shlex.quote(service_name)

    ps_cmd = f"cd {qroot} && docker compose --env-file .env {_COMPOSE_FLAGS} ps -q {qsvc} 2>/dev/null | head -1"
    code, stdout, _ = run_remote(username, server, ps_cmd, timeout)
    if code == 0 and stdout.strip():
        return True

    cfg_cmd = f"cd {qroot} && docker compose --env-file .env {_COMPOSE_FLAGS} config --services 2>/dev/null"
    code2, stdout2, _ = run_remote(username, server, cfg_cmd, timeout)
    if code2 != 0:
        return False
    services = {line.strip() for line in stdout2.splitlines() if line.strip()}
    return service_name in services


def remote_compose_service_logs_tail(
    run_remote: RunRemote,
    *,
    username: str,
    server: str,
    remote_app_root: str,
    service_name: str,
    lines: int = 100,
    timeout: int = 45,
) -> str:
    """Fetch ``docker compose logs --tail N`` for *service_name* (combined stdout/stderr)."""
    assert_safe_compose_service_name(service_name)
    n = max(10, min(int(lines), 300))
    qroot = shlex.quote(remote_app_root)
    qsvc = shlex.quote(service_name)
    cmd = f"cd {qroot} && docker compose --env-file .env {_COMPOSE_FLAGS} logs --tail {n} {qsvc} 2>&1"
    _code, out, err = run_remote(username, server, cmd, timeout)
    return (out + err).strip()
