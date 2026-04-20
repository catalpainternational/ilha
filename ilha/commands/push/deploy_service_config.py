"""Deploy-side service configuration.

Captures the service names, port, and health endpoint that the slim deploy
pipeline assumes about the target project. Defaults match the historical
hardcoded values so existing deploys continue to work unchanged. Projects
that use different service names or ports can override via
``.ilha/config.yml``::

    deploy:
      web_service: web
      web_port: 8000
      health_path: /health-check/
      dependency_services: [db, redis]
      db_service: db
      worker_services: [rq-worker-1]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...utils.logging import log_warning


@dataclass(frozen=True)
class DeployServiceConfig:
    """Which compose services the deploy pipeline should target."""

    web_service: str = "web"
    web_port: int = 8000
    health_path: str = "/health-check/"
    dependency_services: List[str] = field(default_factory=lambda: ["db", "redis"])
    db_service: str = "db"
    worker_services: List[str] = field(default_factory=lambda: ["rq-worker-1"])

    @classmethod
    def from_mapping(cls, data: Optional[Dict[str, Any]]) -> "DeployServiceConfig":
        """Build a config from the ``deploy:`` block of config.yml.

        Unknown keys are ignored with a warning; missing keys fall back to
        defaults.
        """
        if not data:
            return cls()

        known = {
            "web_service",
            "web_port",
            "health_path",
            "dependency_services",
            "db_service",
            "worker_services",
        }
        unknown = set(data.keys()) - known
        if unknown:
            log_warning(
                f"Ignoring unknown keys in deploy config: {sorted(unknown)}. "
                f"Known keys: {sorted(known)}."
            )

        kwargs: Dict[str, Any] = {}
        for key in known:
            if key in data and data[key] is not None:
                kwargs[key] = data[key]

        # Normalize lists: accept comma-strings too, for convenience.
        for list_key in ("dependency_services", "worker_services"):
            if list_key in kwargs and isinstance(kwargs[list_key], str):
                kwargs[list_key] = [s.strip() for s in kwargs[list_key].split(",") if s.strip()]

        # Validate web_port shape.
        if "web_port" in kwargs:
            try:
                kwargs["web_port"] = int(kwargs["web_port"])
            except (TypeError, ValueError):
                log_warning(
                    f"Invalid deploy.web_port {kwargs['web_port']!r}; falling back to default 8000."
                )
                kwargs.pop("web_port")

        # Ensure health_path starts with / so downstream URL building is safe.
        if "health_path" in kwargs:
            hp = str(kwargs["health_path"]).strip()
            if not hp.startswith("/"):
                hp = "/" + hp
            kwargs["health_path"] = hp

        return cls(**kwargs)


def load_deploy_service_config(project_root: Path) -> DeployServiceConfig:
    """Load the deploy service config from ``.ilha/config.yml`` under ``deploy:``.

    Missing file or missing ``deploy:`` block returns the defaults.
    """
    from ...config.settings import ILHA_DIR

    config_path = project_root / ILHA_DIR / "config.yml"
    if not config_path.exists():
        return DeployServiceConfig()

    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text()) or {}
    except Exception as exc:
        log_warning(
            f"Could not parse {config_path} for deploy service config: {exc}. Using defaults."
        )
        return DeployServiceConfig()

    return DeployServiceConfig.from_mapping(raw.get("deploy"))
