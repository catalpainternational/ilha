"""Ephemeral worker droplet bundle generation and lifecycle helpers."""

from __future__ import annotations

import base64
import copy
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ilha.commands.image import ImageManager
from ilha.commands.push.deploy_service_config import load_deploy_service_config
from ilha.config.settings import ILHA_DIR, get_project_root, get_script_dir
from ilha.core.worker_compose import WorkerComposeFilter
from ilha.exceptions import ilhaCommandError
from ilha.commands.secrets import SecretsManager
from ilha.utils.env_loader import is_sensitive_env_key, load_env_file, merge_env_layers
from ilha.utils.logging import log_info, log_success


DEFAULT_WORKER_TAG = "worker"
DEFAULT_MANAGED_TAG = "ilha-managed"
PROJECT_TAG_PREFIX = "ilha-project"
DEFAULT_WORKER_SERVICE = "rq-worker-1"
CLOUD_INIT_TEMPLATE = "worker_cloud_init.sh.tpl"
REQUIRED_WORKER_ENV_KEYS = ("DB_HOST", "REDIS_HOST", "COMPOSE_PROJECT_NAME")
DEFAULT_ENV_EXCLUDE = (
    "DIGITALOCEAN_API_TOKEN",
    "DOCR_READ_TOKEN",
    "EMAIL_HOST_PASSWORD",
    "DJANGO_SECRET_KEY",
)
ENV_VAR_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


def project_scope_tag(project_root: Path) -> str:
    """Tag that pins a worker fleet to one project.

    Fleet selection by ``worker`` + ``branch:<name>`` alone is account-wide:
    two ilha projects in the same DigitalOcean account, both with a
    ``staging`` branch, would otherwise list — and destroy — each other's
    droplets.
    """
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", project_root.name).strip("-") or "project"
    return f"{PROJECT_TAG_PREFIX}:{name}"


def has_project_context(project_root: Path) -> bool:
    """True when ``project_root`` is a real ilha project (has .ilha/config.yml).

    ``get_project_root`` falls back to the cwd, so a bare cwd name must not be
    trusted as a fleet scope for destructive operations.
    """
    return (project_root / ILHA_DIR / "config.yml").exists()


@dataclass(frozen=True)
class WorkerPoolConfig:
    """Resolved worker pool settings from config + CLI overrides."""

    worker_service: str = DEFAULT_WORKER_SERVICE
    size: str = "s-2vcpu-4gb"
    region: str = "nyc1"
    image: str = "ubuntu-22-04-x64"
    ssh_keys: Tuple[str, ...] = ()
    max_workers: int = 10
    idle_destroy_after_seconds: int = 300
    tags: Tuple[str, ...] = (DEFAULT_WORKER_TAG, DEFAULT_MANAGED_TAG)
    registry_token_env: str = "DOCR_READ_TOKEN"
    central_droplet_name: Optional[str] = None
    env_allowlist: Tuple[str, ...] = ()
    env_exclude: Tuple[str, ...] = DEFAULT_ENV_EXCLUDE
    env_overrides: Tuple[Tuple[str, str], ...] = ()


@dataclass
class WorkerBundle:
    """Minimal compose bundle for an ephemeral worker droplet."""

    branch: str
    worker_service: str
    image_ref: str
    central_private_ip: str
    compose_data: Dict[str, Any]
    env_vars: Dict[str, str]
    output_dir: Path

    @property
    def compose_path(self) -> Path:
        return self.output_dir / "docker-compose.yml"

    @property
    def env_path(self) -> Path:
        return self.output_dir / "env.ilha"


class WorkerManager:
    """Build worker bundles and cloud-init payloads for ephemeral droplets."""

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = Path(project_root or get_project_root()).resolve()
        self._worker_config = WorkerComposeFilter()
        self._image_manager = ImageManager(self.project_root)

    @property
    def config_path(self) -> Path:
        return self.project_root / ILHA_DIR / "config.yml"

    def load_pool_config(self, overrides: Optional[Dict[str, Any]] = None) -> WorkerPoolConfig:
        """Load worker_pool settings from .ilha/config.yml with optional overrides."""
        data: Dict[str, Any] = {}
        if self.config_path.exists():
            loaded = yaml.safe_load(self.config_path.read_text()) or {}
            if isinstance(loaded, dict):
                pool = loaded.get("worker_pool")
                if isinstance(pool, dict):
                    data = pool

        overrides = overrides or {}
        merged = {**data, **overrides}

        ssh_keys = merged.get("ssh_keys") or []
        if isinstance(ssh_keys, str):
            ssh_keys = [k.strip() for k in ssh_keys.split(",") if k.strip()]

        tags = merged.get("tags") or [DEFAULT_WORKER_TAG, DEFAULT_MANAGED_TAG]
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]

        env_allowlist = merged.get("env_allowlist") or []
        if isinstance(env_allowlist, str):
            env_allowlist = [k.strip() for k in env_allowlist.split(",") if k.strip()]

        env_exclude = merged.get("env_exclude")
        if env_exclude is None:
            env_exclude = list(DEFAULT_ENV_EXCLUDE)
        elif isinstance(env_exclude, str):
            env_exclude = [k.strip() for k in env_exclude.split(",") if k.strip()]

        env_overrides = merged.get("env_overrides") or {}
        if not isinstance(env_overrides, dict):
            raise ilhaCommandError(
                "worker_pool.env_overrides must be a mapping of ENV_VAR: value"
            )

        return WorkerPoolConfig(
            worker_service=str(merged.get("worker_service") or DEFAULT_WORKER_SERVICE),
            size=str(merged.get("size") or "s-2vcpu-4gb"),
            region=str(merged.get("region") or "nyc1"),
            image=str(merged.get("image") or "ubuntu-22-04-x64"),
            ssh_keys=tuple(str(k) for k in ssh_keys),
            max_workers=int(merged.get("max_workers") or 10),
            idle_destroy_after_seconds=int(merged.get("idle_destroy_after_seconds") or 300),
            tags=tuple(str(t) for t in tags),
            registry_token_env=str(merged.get("registry_token_env") or "DOCR_READ_TOKEN"),
            central_droplet_name=merged.get("central_droplet_name"),
            env_allowlist=tuple(str(k) for k in env_allowlist),
            env_exclude=tuple(str(k) for k in env_exclude),
            env_overrides=tuple(sorted((str(k), str(v)) for k, v in env_overrides.items())),
        )

    def resolve_worktree_path(self, branch_name: str) -> Path:
        from ilha.commands.push.deploy_preflight import DeployPreflight

        return DeployPreflight(self.project_root).resolve_worktree_path(branch_name)

    def resolve_compose_file(self, worktree_path: Path) -> Path:
        base = worktree_path / "docker-compose.yml"
        if base.exists():
            return base
        overlay = worktree_path / ILHA_DIR / "docker-compose.overlay.yml"
        if overlay.exists():
            return overlay
        raise ilhaCommandError(f"No compose file found in worktree: {worktree_path}")

    def _excluded_services(self, worker_service: str) -> List[str]:
        deploy_cfg = load_deploy_service_config(self.project_root)
        excluded = list(deploy_cfg.dependency_services or [])
        if deploy_cfg.web_service and deploy_cfg.web_service not in excluded:
            excluded.append(deploy_cfg.web_service)
        if deploy_cfg.frontend_service and deploy_cfg.frontend_service not in excluded:
            excluded.append(deploy_cfg.frontend_service)
        return list(dict.fromkeys(excluded))

    def _rewrite_env_for_central(
        self,
        env_vars: Dict[str, str],
        compose_data: Dict[str, Any],
        central_private_ip: str,
        excluded_services: List[str],
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """Point worker env vars at the central server private IP."""
        updated_env = dict(env_vars)
        compose_copy = copy.deepcopy(compose_data)

        for service_name in excluded_services:
            for env_var in self._worker_config._find_env_vars_for_service(compose_copy, service_name):
                old_value = updated_env.get(env_var, "")
                if old_value and service_name.lower() in old_value.lower():
                    pattern = re.compile(r"\b" + re.escape(service_name) + r"\b", re.IGNORECASE)
                    updated_env[env_var] = pattern.sub(central_private_ip, old_value)
                elif env_var not in updated_env and "HOST" in env_var.upper():
                    updated_env[env_var] = central_private_ip

        for svc_name, svc_config in (compose_copy.get("services") or {}).items():
            env_block = svc_config.get("environment")
            if not isinstance(env_block, dict):
                continue
            for env_var, old_value in list(env_block.items()):
                if not isinstance(old_value, str):
                    continue
                for service_name in excluded_services:
                    if service_name.lower() in old_value.lower():
                        pattern = re.compile(r"\b" + re.escape(service_name) + r"\b", re.IGNORECASE)
                        env_block[env_var] = pattern.sub(central_private_ip, old_value)

        if central_private_ip:
            updated_env["REDIS_HOST"] = central_private_ip
            updated_env["DB_HOST"] = central_private_ip
            redis_port = str(updated_env.get("REDIS_PORT") or "6379")
            redis_db = str(updated_env.get("REDIS_DB") or "0")
            updated_env["REDIS_URL"] = f"redis://{central_private_ip}:{redis_port}/{redis_db}"
            # DATABASE_URL is only assembled when its credentials are actually
            # present. Fabricated fallbacks would produce a URL that connects
            # nowhere while looking configured; the compose file's own
            # interpolation remains the source of truth otherwise.
            db_user = updated_env.get("POSTGRES_USER")
            db_pass = updated_env.get("POSTGRES_PASSWORD")
            db_name = updated_env.get("POSTGRES_DB")
            if db_user and db_pass and db_name:
                db_port = str(updated_env.get("DB_PORT") or "5432")
                updated_env["DATABASE_URL"] = (
                    f"postgres://{db_user}:{db_pass}@{central_private_ip}:{db_port}/{db_name}"
                )

        return updated_env, compose_copy

    @staticmethod
    def _extract_env_var_refs(value: Any) -> set[str]:
        if not isinstance(value, str):
            return set()
        return set(ENV_VAR_REF_PATTERN.findall(value))

    def _worker_env_keys(self, compose_data: Dict[str, Any], worker_service: str) -> set[str]:
        keys = set(REQUIRED_WORKER_ENV_KEYS)
        service_cfg = (compose_data.get("services") or {}).get(worker_service) or {}
        env_block = service_cfg.get("environment")
        if isinstance(env_block, dict):
            keys.update(str(k) for k in env_block.keys())
            for value in env_block.values():
                keys.update(self._extract_env_var_refs(value))
        elif isinstance(env_block, list):
            for entry in env_block:
                if isinstance(entry, str) and "=" in entry:
                    keys.add(entry.split("=", 1)[0].strip())
        return keys

    @staticmethod
    def _is_sensitive_env_key(key: str, *, exclude: Tuple[str, ...]) -> bool:
        return is_sensitive_env_key(key, extra_exclude=exclude)

    def _filter_worker_env(
        self,
        source_env: Dict[str, str],
        *,
        compose_data: Dict[str, Any],
        worker_service: str,
        pool_cfg: WorkerPoolConfig,
    ) -> Dict[str, str]:
        allowlist = {k.upper() for k in pool_cfg.env_allowlist}
        exclude = pool_cfg.env_exclude
        needed_keys = self._worker_env_keys(compose_data, worker_service)

        # Sensitive keys are stripped first; the allowlist then re-adds
        # specific keys as explicit opt-ins. It must never disable the filter
        # wholesale — allowlisting one password is not consent to ship every
        # API key the compose file references into cloud-init user data.
        filtered = {
            key: source_env[key]
            for key in sorted(needed_keys)
            if key in source_env
            and not self._is_sensitive_env_key(key, exclude=exclude)
        }
        for key, value in source_env.items():
            if key.upper() in allowlist:
                filtered[key] = value
        return filtered

    @staticmethod
    def _write_private_file(path: Path, content: str, *, mode: int = 0o600) -> None:
        path.write_text(content)
        path.chmod(mode)

    def build_bundle(
        self,
        branch_name: str,
        central_private_ip: str,
        *,
        worker_service: Optional[str] = None,
        output_dir: Optional[Path] = None,
        prefer_digest: bool = True,
        secrets_profile: Optional[str] = None,
    ) -> WorkerBundle:
        """Build a minimal 1-service compose bundle for an ephemeral worker droplet."""
        pool_cfg = self.load_pool_config()
        worker_service = worker_service or pool_cfg.worker_service
        worktree_path = self.resolve_worktree_path(branch_name)
        compose_file = self.resolve_compose_file(worktree_path)
        excluded = self._excluded_services(worker_service)

        filtered = self._worker_config.filter_compose_services(
            compose_file,
            container_filter=[{"worktree": branch_name, "container": worker_service}],
            worktree_path=worktree_path,
            exclude_deps=excluded,
            private_ip=central_private_ip,
        )
        if not filtered or worker_service not in (filtered.get("services") or {}):
            raise ilhaCommandError(
                f"Could not filter compose to worker service {worker_service!r} for branch {branch_name}"
            )

        image_ref = self._image_manager.image_ref_for_service(
            branch_name,
            worker_service,
            prefer_digest=prefer_digest,
        )

        service_cfg = filtered["services"][worker_service]
        service_cfg.pop("build", None)
        service_cfg["image"] = image_ref
        service_cfg.pop("depends_on", None)
        service_cfg.pop("volumes", None)
        service_cfg["env_file"] = ["env.ilha"]
        service_cfg["networks"] = ["default"]

        compose_networks = filtered.get("networks") or {}
        filtered["networks"] = {
            name: cfg
            for name, cfg in compose_networks.items()
            if not (isinstance(cfg, dict) and cfg.get("external"))
        } or {"default": {"driver": "bridge"}}
        if "volumes" in filtered and not filtered["volumes"]:
            filtered.pop("volumes")

        env_file = worktree_path / ILHA_DIR / "env.ilha"
        source_env = load_env_file(env_file) if env_file.exists() else {}
        if secrets_profile:
            secrets_mgr = SecretsManager(self.project_root)
            source_env = merge_env_layers(source_env, secrets_mgr.decrypt_profile(secrets_profile))
        env_vars = self._filter_worker_env(
            source_env,
            compose_data=filtered,
            worker_service=worker_service,
            pool_cfg=pool_cfg,
        )
        env_vars, filtered = self._rewrite_env_for_central(
            env_vars,
            filtered,
            central_private_ip,
            excluded,
        )
        # Pool-level overrides apply last: they exist to pin values that must
        # differ on ephemeral workers (e.g. RQ_QUEUE_NAMES=scraping) no matter
        # what the central environment or compose defaults say.
        env_vars.update(dict(pool_cfg.env_overrides))
        env_vars.setdefault("COMPOSE_PROJECT_NAME", f"worker-{branch_name}")

        missing = [key for key in REQUIRED_WORKER_ENV_KEYS if not str(env_vars.get(key, "")).strip()]
        if missing:
            raise ilhaCommandError(
                f"Worker bundle missing required env var(s): {', '.join(missing)}. "
                "Ensure the worker service environment references them or set worker_pool.env_allowlist."
            )

        bundle_dir = output_dir or Path(tempfile.mkdtemp(prefix=f"ilha-worker-{branch_name}-"))
        bundle_dir.mkdir(parents=True, exist_ok=True)

        compose_path = bundle_dir / "docker-compose.yml"
        env_path = bundle_dir / "env.ilha"
        if filtered.get("version") is None:
            filtered.pop("version", None)
        compose_path.write_text(yaml.dump(filtered, default_flow_style=False, sort_keys=False, allow_unicode=True))
        self._write_private_file(env_path, "".join(f"{k}={v}\n" for k, v in env_vars.items()))

        log_success(f"Worker bundle written to {bundle_dir}")
        return WorkerBundle(
            branch=branch_name,
            worker_service=worker_service,
            image_ref=image_ref,
            central_private_ip=central_private_ip,
            compose_data=filtered,
            env_vars=env_vars,
            output_dir=bundle_dir,
        )

    def render_cloud_init(
        self,
        bundle: WorkerBundle,
        *,
        registry_token_env: Optional[str] = None,
        secrets_profile: Optional[str] = None,
    ) -> str:
        """Render cloud-init user data that bootstraps Docker and starts the worker."""
        template_path = get_script_dir() / "scripts" / CLOUD_INIT_TEMPLATE
        if not template_path.exists():
            raise ilhaCommandError(f"Cloud-init template not found: {template_path}")

        compose_b64 = base64.b64encode(bundle.compose_path.read_bytes()).decode("ascii")
        env_b64 = base64.b64encode(bundle.env_path.read_bytes()).decode("ascii")
        token_env = registry_token_env or self.load_pool_config().registry_token_env
        secrets_mgr = SecretsManager(self.project_root)
        registry_token = secrets_mgr.resolve_registry_token(
            token_env,
            profile_name=secrets_profile,
            profile_env=bundle.env_vars,
        )
        token_b64 = base64.b64encode(registry_token.encode("utf-8")).decode("ascii") if registry_token else ""

        template = template_path.read_text()
        rendered = (
            template.replace("{{WORKER_SERVICE}}", bundle.worker_service)
            .replace("{{IMAGE_REF}}", bundle.image_ref)
            .replace("{{COMPOSE_B64}}", compose_b64)
            .replace("{{ENV_B64}}", env_b64)
            .replace("__REGISTRY_TOKEN_B64__", token_b64)
        )
        return rendered

    def worker_tags(self, branch_name: str, extra_tags: Optional[List[str]] = None) -> List[str]:
        """Build droplet tags for worker pool members."""
        pool_cfg = self.load_pool_config()
        tags = list(pool_cfg.tags)
        branch_tag = f"branch:{branch_name}"
        if branch_tag not in tags:
            tags.append(branch_tag)
        scope_tag = self.project_tag()
        if scope_tag not in tags:
            tags.append(scope_tag)
        for tag in extra_tags or []:
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    def project_tag(self) -> str:
        return project_scope_tag(self.project_root)

    def has_project_context(self) -> bool:
        return has_project_context(self.project_root)

    def generate_worker_name(self, branch_name: str, index: int) -> str:
        safe_branch = re.sub(r"[^a-zA-Z0-9-]", "-", branch_name).strip("-") or "worker"
        return f"worker-{safe_branch}-{index}"

    @staticmethod
    def next_worker_index(existing_names: List[str]) -> int:
        """First index that cannot collide with a live droplet's name.

        ``len(existing) + 1`` reuses an index as soon as any lower-numbered
        droplet is destroyed, minting a second droplet with an existing name.
        """
        indices = []
        for name in existing_names:
            match = re.search(r"-(\d+)$", name or "")
            if match:
                indices.append(int(match.group(1)))
        return (max(indices) + 1) if indices else 1
