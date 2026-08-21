"""Pure compose-dict transforms applied while assembling a deploy package.

Extracted from ``SlimDeployManager`` so the deploy-time compose rewriting —
closing host-port publishes, dropping unneeded privileges, and retargeting
relative paths into ``./code`` — is independently testable: these are pure
functions over a compose dict, with no SSH and no deploy state. The manager
keeps thin delegating methods so its public surface and every call site are
unchanged.
"""

from __future__ import annotations

from typing import List

from ...utils.logging import log_info

# Subdirectory (relative to the deploy root) where the worktree code is copied.
# The base compose lives one level above, so user-written relative paths must be
# retargeted into this subdirectory before deploy.
CODE_SUBDIR = "./code"

# Services matching these patterns keep elevated privileges (Playwright/Chromium).
PRIVILEGED_SERVICE_PATTERNS = ("web", "rq-worker")

# Keys removed from non-privileged services during deploy assembly.
PRIVILEGE_KEYS = ("security_opt", "cap_add", "ipc", "shm_size")


def strip_host_port_publishes(compose_data: dict) -> None:
    """Remove ``ports:`` from every service in the (base) compose copy.

    Caddy reaches each service over the ``ilha_caddy_proxy`` docker network, so
    dropping host port publishes never breaks routing — it only closes the
    surface that bots scan. Postgres/Redis on ``0.0.0.0:5433/6379`` is the exact
    attack surface this prevents.

    Applied to the BASE compose copy (not the overlay) because compose v2's merge
    ignores ``ports: []`` from a later file — the only reliable wipe is to remove
    the key from the source compose itself.
    """
    services = compose_data.get("services") or {}
    stripped: List[str] = []
    for svc_name, svc_config in services.items():
        if not isinstance(svc_config, dict):
            continue
        if "ports" not in svc_config:
            continue
        del svc_config["ports"]
        stripped.append(svc_name)
    if stripped:
        log_info(
            f"Closed host port publishes for prod deploy: {', '.join(stripped)} "
            f"(Caddy reaches services over the docker network, not host ports)"
        )


def strip_unnecessary_privileges(compose_data: dict) -> None:
    """Remove elevated Docker privileges from services that don't need them.

    Services whose name starts with one of ``PRIVILEGED_SERVICE_PATTERNS`` (e.g.
    ``web``, ``rq-worker-1``) keep their privileges because they run
    Playwright/Chromium. All other services (``db``, ``redis``, ``caddy``, etc.)
    have ``security_opt``, ``cap_add``, ``ipc``, and ``shm_size`` stripped for
    defense in depth.
    """
    services = compose_data.get("services")
    if not services:
        return
    for svc_name, svc_config in services.items():
        if not isinstance(svc_config, dict):
            continue
        if any(svc_name.startswith(prefix) for prefix in PRIVILEGED_SERVICE_PATTERNS):
            continue
        removed = []
        for key in PRIVILEGE_KEYS:
            if key in svc_config:
                del svc_config[key]
                removed.append(key)
        if removed:
            log_info(f"Stripped privileges from service '{svc_name}': {', '.join(removed)}")


def retarget_relative_path(value: str) -> str:
    """Prefix ``./code/`` onto a relative path; leave absolute/var refs alone.

    Callers must ensure the value is unambiguously a path (e.g. an ``env_file``
    entry, ``build.context``, or a volume source already confirmed to start with
    ``.``). For volume short-syntax, bare names like ``foo:/x`` are named volumes
    — the caller filters those out before calling here.
    """
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(("/", "~", "$")):
        return value
    if value == ".":
        return CODE_SUBDIR
    if value.startswith("./"):
        return f"{CODE_SUBDIR}/{value[2:]}"
    if value.startswith("../"):
        return f"{CODE_SUBDIR}/{value}"
    return f"{CODE_SUBDIR}/{value}"


def retarget_paths_to_code_subdir(compose_data: dict) -> None:
    """Rewrite relative paths in each service so they resolve under ``./code/``.

    Why: the base compose is copied to ``<deploy_dir>/docker-compose.yml`` but the
    worktree code is copied to ``<deploy_dir>/code/``. Without this retarget,
    ``build.context: .`` resolves to the deploy root (no Dockerfile there) and
    bind-mount sources like ``./frontend`` miss the project files.
    """
    services = compose_data.get("services") or {}
    for svc in services.values():
        if not isinstance(svc, dict):
            continue

        build = svc.get("build")
        if isinstance(build, str):
            svc["build"] = retarget_relative_path(build)
        elif isinstance(build, dict) and "context" in build:
            build["context"] = retarget_relative_path(build["context"])

        volumes = svc.get("volumes")
        if isinstance(volumes, list):
            new_volumes = []
            for vol in volumes:
                if isinstance(vol, str):
                    # Short form: SOURCE:TARGET[:MODE]. Only retarget when the
                    # source is a path (starts with '.'); named volumes have no
                    # leading dot/slash and are left alone.
                    if vol.startswith("."):
                        head, sep, tail = vol.partition(":")
                        if sep:
                            vol = f"{retarget_relative_path(head)}:{tail}"
                    new_volumes.append(vol)
                elif isinstance(vol, dict):
                    if vol.get("type") == "bind" and isinstance(vol.get("source"), str):
                        vol["source"] = retarget_relative_path(vol["source"])
                    new_volumes.append(vol)
                else:
                    new_volumes.append(vol)
            svc["volumes"] = new_volumes

        env_file = svc.get("env_file")
        if isinstance(env_file, str):
            svc["env_file"] = retarget_relative_path(env_file)
        elif isinstance(env_file, list):
            svc["env_file"] = [
                retarget_relative_path(p) if isinstance(p, str) else p
                for p in env_file
            ]

        extends = svc.get("extends")
        if isinstance(extends, dict) and isinstance(extends.get("file"), str):
            extends["file"] = retarget_relative_path(extends["file"])

