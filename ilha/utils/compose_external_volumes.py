"""Resolve Docker host volume names for ``external: true`` compose volumes."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, MutableMapping

_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def expand_compose_vars(template: str, env: MutableMapping[str, str]) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` using *env* (iterative until stable)."""

    def _replace_one(s: str) -> str:
        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            default = match.group(2)
            if key in env:
                return str(env[key])
            if default is not None:
                return default
            return match.group(0)

        return _VAR_PATTERN.sub(repl, s)

    result = str(template)
    for _ in range(20):
        nxt = _replace_one(result)
        if nxt == result:
            return nxt
        result = nxt
    return result


def list_external_volume_host_names(
    compose_path: Path,
    env: Dict[str, str],
) -> List[str]:
    """Return Docker volume names required for top-level ``external: true`` volumes.

    Expands ``${COMPOSE_PROJECT_NAME}`` (and similar) using *env* before reading YAML.
    """
    try:
        import yaml
    except ImportError:
        return []

    raw_text = compose_path.read_text(encoding="utf-8")
    expanded_lines: List[str] = []
    for line in raw_text.splitlines():
        expanded_lines.append(expand_compose_vars(line, env))
    data: Dict[str, Any] = yaml.safe_load("\n".join(expanded_lines)) or {}
    volumes = data.get("volumes") or {}
    out: List[str] = []
    for key, spec in volumes.items():
        if not isinstance(spec, dict):
            continue
        if not spec.get("external"):
            continue
        name_field = spec.get("name")
        if name_field:
            name = str(name_field)
        else:
            project = env.get("COMPOSE_PROJECT_NAME", "").strip()
            name = f"{project}_{key}" if project else str(key)
        if name and name not in out:
            out.append(name)
    return out


def compose_web_image_reference(
    compose_path: Path,
    env: Dict[str, str],
    web_service: str,
) -> str:
    """Resolve the image reference used for *web_service* (for ``docker image inspect``).

    When the compose file only declares ``build:`` and no ``image:``, Compose defaults
    to ``{COMPOSE_PROJECT_NAME}_{service}`` with tag ``latest`` on the Docker host.
    """
    try:
        import yaml
    except ImportError:
        return ""

    raw_text = compose_path.read_text(encoding="utf-8")
    expanded = "\n".join(expand_compose_vars(line, env) for line in raw_text.splitlines())
    data: Dict[str, Any] = yaml.safe_load(expanded) or {}
    services = data.get("services") or {}
    svc = services.get(web_service) or {}
    if not isinstance(svc, dict):
        return ""
    img = svc.get("image")
    if img:
        return str(img).strip()
    project = env.get("COMPOSE_PROJECT_NAME", "").strip()
    if project:
        return f"{project}_{web_service}:latest"
    return f"{web_service}:latest"
