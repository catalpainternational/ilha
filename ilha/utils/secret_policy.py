"""Single source of truth for secrets that must never leave the operator machine.

Control-plane DigitalOcean tokens can create/destroy droplets and rewrite DNS.
A registry read token is required for `docker login` on a worker host, but it
must not be copied into app env or cloud-init user_data (DO metadata stores
user_data in plaintext).
"""

from __future__ import annotations

from typing import Iterable, MutableMapping

CONTROL_PLANE_ENV_KEYS = frozenset(
    {
        "DIGITALOCEAN_API_TOKEN",
        "DIGITALOCEAN_TOKEN",
        "DO_API_TOKEN",
        "DO_TOKEN",
    }
)

REGISTRY_LOGIN_ENV_KEYS = frozenset({"DOCR_READ_TOKEN"})

# Never in worker env.ilha, never in cloud-init, never allowlisted onto workers.
NEVER_ON_WORKER_ENV = CONTROL_PLANE_ENV_KEYS | REGISTRY_LOGIN_ENV_KEYS


def is_control_plane_env_key(key: str) -> bool:
    return key.upper() in CONTROL_PLANE_ENV_KEYS


def is_never_on_worker_env(key: str) -> bool:
    return key.upper() in NEVER_ON_WORKER_ENV


def blocked_worker_allowlist_keys(keys: Iterable[str]) -> tuple[str, ...]:
    """Return allowlist entries that are forbidden on worker env files."""
    return tuple(key for key in keys if is_never_on_worker_env(key))


def pop_control_plane_env(env: MutableMapping[str, str]) -> dict[str, str]:
    """Remove control-plane keys from ``env`` in place, returning what was removed.

    Callers decide the removed keys' fate: discarded for ordinary deploys,
    diverted to a root-only ``.env.control-plane`` for worker_pool projects
    whose central-droplet autoscaler legitimately needs the token.
    """
    return {key: env.pop(key) for key in list(env) if key in CONTROL_PLANE_ENV_KEYS}
