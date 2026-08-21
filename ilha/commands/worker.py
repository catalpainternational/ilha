"""Ephemeral worker droplet bundle generation and lifecycle helpers."""

from __future__ import annotations

import base64
import copy
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import yaml

from ilha.commands.image import ImageManager
from ilha.commands.push.deploy_preflight import DeployPreflight
from ilha.commands.push.deploy_service_config import load_deploy_service_config
from ilha.config.settings import ILHA_DIR, get_project_root, get_script_dir
from ilha.core.droplet_manager import DropletInfo
from ilha.core.worker_compose import WorkerComposeFilter
from ilha.exceptions import ilhaCommandError
from ilha.commands.secrets import SecretsManager
from ilha.utils.env_loader import format_env_text, is_sensitive_env_key, load_env_file, merge_env_layers
from ilha.utils.logging import log_success
from ilha.utils.secret_policy import NEVER_ON_WORKER_ENV, blocked_worker_allowlist_keys
from ilha.utils.published_images import pin_service_to_image_ref
from ilha.utils.registry_login import docker_login_remote
from ilha.utils.ssh_manager import SSHConnectionManager


DEFAULT_WORKER_TAG = "worker"
DEFAULT_MANAGED_TAG = "ilha-managed"
PROJECT_TAG_PREFIX = "ilha-project"
DEFAULT_WORKER_SERVICE = "rq-worker-1"
CLOUD_INIT_TEMPLATE = "worker_cloud_init.sh.tpl"
REQUIRED_WORKER_ENV_KEYS = ("DB_HOST", "REDIS_HOST", "COMPOSE_PROJECT_NAME")
DEFAULT_ENV_EXCLUDE = tuple(
    sorted(
        NEVER_ON_WORKER_ENV
        | {
            "EMAIL_HOST_PASSWORD",
            "DJANGO_SECRET_KEY",
        }
    )
)
REMOTE_WORKER_DIR = "/app"
DOCKER_READY_RETRIES = 30
DOCKER_READY_DELAY_SECONDS = 10.0
ENV_VAR_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


def project_scope_tag(project_root: Path) -> str:
    """Tag that pins a worker fleet to one project.

    Fleet selection by ``worker`` + ``branch:<name>`` alone is account-wide:
    two ilha projects in the same DigitalOcean account, both with a
    ``staging`` branch, would otherwise list — and destroy — each other's
    droplets.

    The name comes from ``project_name`` in .ilha/config.yml, NOT the
    directory basename: the same project is ``staging/`` on a laptop worktree
    and ``/app`` inside the autoscaler container, and basename-derived tags
    split one fleet into two invisible halves — the container spawned workers
    tagged ``ilha-project:app`` that no laptop command could see (2026-08-21).
    The basename remains only as a fallback for projects with no config.
    """
    raw = ""
    config_path = project_root / ILHA_DIR / "config.yml"
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text()) or {}
            if isinstance(loaded, dict):
                raw = str(loaded.get("project_name") or "").strip()
        except (OSError, yaml.YAMLError):
            raw = ""
    raw = raw or project_root.name
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", raw).strip("-") or "project"
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

    @staticmethod
    def _service_host_pattern(service_name: str) -> "re.Pattern[str]":
        """Match ``service_name`` where it names a host — never a URI scheme.

        A compose service called ``redis`` appears twice in
        ``redis://${REDIS_HOST:-redis}:6379/0``: once as the scheme and once as
        the host default. Only the second is an address to redirect at the
        central droplet. Rewriting both produced ``10.0.0.1://10.0.0.1:6379/0``
        — a URL with an invalid scheme, which looks configured but connects
        nowhere. The trailing ``://`` lookahead is what tells them apart.
        """
        return re.compile(
            r"\b" + re.escape(service_name) + r"\b(?!://)", re.IGNORECASE
        )

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

        # Compiled once here, used by both passes below.
        patterns = [
            (name, self._service_host_pattern(name)) for name in excluded_services
        ]

        for service_name, pattern in patterns:
            for env_var in self._worker_config._find_env_vars_for_service(compose_copy, service_name):
                old_value = updated_env.get(env_var, "")
                if old_value and pattern.search(old_value):
                    updated_env[env_var] = pattern.sub(central_private_ip, old_value)
                elif env_var not in updated_env and "HOST" in env_var.upper():
                    updated_env[env_var] = central_private_ip

        for svc_config in (compose_copy.get("services") or {}).values():
            env_block = svc_config.get("environment")
            if not isinstance(env_block, dict):
                continue
            for env_var, old_value in list(env_block.items()):
                if not isinstance(old_value, str):
                    continue
                # Fold every service's substitution through one accumulating
                # value. Substituting each pattern against the *original*
                # string would let the last match overwrite the earlier ones,
                # so a value naming two excluded services kept only one.
                new_value = old_value
                for _name, pattern in patterns:
                    new_value = pattern.sub(central_private_ip, new_value)
                if new_value != old_value:
                    env_block[env_var] = new_value

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
        blocked = blocked_worker_allowlist_keys(pool_cfg.env_allowlist)
        if blocked:
            raise ilhaCommandError(
                "worker_pool.env_allowlist cannot include control-plane or "
                f"registry-login keys: {', '.join(blocked)}. Those stay on the "
                "operator machine; registry login is performed over SSH."
            )
        allowlist = {k.upper() for k in pool_cfg.env_allowlist}
        exclude = pool_cfg.env_exclude
        needed_keys = self._worker_env_keys(compose_data, worker_service)

        # Sensitive keys are stripped first; the allowlist then re-adds
        # specific app secrets as explicit opt-ins (LLM keys, DB password).
        # Control-plane tokens are rejected above, never re-added.
        filtered = {
            key: source_env[key]
            for key in sorted(needed_keys)
            if key in source_env
            and not self._is_sensitive_env_key(key, exclude=exclude)
            and key.upper() not in NEVER_ON_WORKER_ENV
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
            branch_name=branch_name,
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
        pin_service_to_image_ref(service_cfg, image_ref)
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
        self._write_private_file(env_path, format_env_text(env_vars))

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

    def render_cloud_init(self, bundle: WorkerBundle) -> str:
        """Render secret-free cloud-init: install Docker and write compose YAML.

        App env and the registry token are delivered later over SSH. DigitalOcean
        stores user_data in instance metadata as plaintext.
        """
        template_path = get_script_dir() / "scripts" / CLOUD_INIT_TEMPLATE
        if not template_path.exists():
            raise ilhaCommandError(f"Cloud-init template not found: {template_path}")

        compose_b64 = base64.b64encode(bundle.compose_path.read_bytes()).decode("ascii")
        template = template_path.read_text()
        return template.replace("{{COMPOSE_B64}}", compose_b64)

    def resolve_registry_token(
        self,
        *,
        secrets_profile: Optional[str] = None,
        registry_token_env: Optional[str] = None,
    ) -> str:
        """Resolve the registry read token from the shell or a SOPS profile."""
        pool_cfg = self.load_pool_config()
        token_env = registry_token_env or pool_cfg.registry_token_env
        secrets_mgr = SecretsManager(self.project_root)
        token = secrets_mgr.resolve_registry_token(token_env, profile_name=secrets_profile)
        if not token:
            raise ilhaCommandError(
                f"Registry token {token_env} is required to pull worker images. "
                "Set it in the environment or pass --secrets <profile>."
            )
        return token

    @staticmethod
    def assert_worker_deploy_preflight(
        *,
        wait: bool,
        ssh_keys: Sequence[str],
        count: int,
        existing_count: int,
        max_workers: int,
        registry_token: Optional[str] = None,
    ) -> None:
        """Fail before `create_droplet` when the deploy cannot be completed."""
        if count < 1:
            raise ilhaCommandError("--count must be at least 1")
        if not wait:
            raise ilhaCommandError(
                "Worker deploy must wait for SSH: env and registry tokens are not "
                "placed in cloud-init user_data. Omit --no-wait."
            )
        if not [key for key in ssh_keys if str(key).strip()]:
            raise ilhaCommandError(
                "SSH keys are required for worker deploy so secrets can be copied "
                "after the droplet boots. Set worker_pool.ssh_keys or pass --ssh-keys."
            )
        if existing_count + count > max_workers:
            raise ilhaCommandError(
                f"Would exceed worker_pool.max_workers={max_workers} "
                f"(existing={existing_count}, requested={count})"
            )
        if registry_token is not None and not str(registry_token).strip():
            raise ilhaCommandError("Registry token is required to pull worker images")

    @staticmethod
    def select_idle_droplets(
        droplets: Sequence[DropletInfo],
        *,
        idle_after_seconds: int,
        now: Optional[datetime] = None,
    ) -> List[DropletInfo]:
        """Droplets whose created_at is at least *idle_after_seconds* ago.

        Droplets with no created_at are skipped so a missing timestamp cannot
        look like an idle worker ready to destroy.
        """
        if idle_after_seconds <= 0:
            return []
        clock = now or datetime.now(timezone.utc)
        selected: List[DropletInfo] = []
        for droplet in droplets:
            created = droplet.created_at
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if clock.tzinfo is None:
                clock = clock.replace(tzinfo=timezone.utc)
            age = (clock - created).total_seconds()
            if age >= idle_after_seconds:
                selected.append(droplet)
        return selected

    def bootstrap_worker(
        self,
        bundle: WorkerBundle,
        host: str,
        *,
        username: str = "root",
        registry_token: str = "",
        ssh: Optional[SSHConnectionManager] = None,
        retries: int = DOCKER_READY_RETRIES,
        retry_delay: float = DOCKER_READY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        """Copy env + compose over SSH, docker login, and start the worker.

        The registry token is passed on SSH stdin, not written into user_data
        or a remote script file.
        """
        if not host:
            raise ilhaCommandError("Worker droplet has no IP address; cannot deliver secrets over SSH")
        if not registry_token:
            raise ilhaCommandError("Registry token is required to docker login on the worker")

        conn = ssh or SSHConnectionManager()
        conn.ensure_host_key(host)

        mkdir_rc, _, mkdir_err = conn.run_remote_text(
            username, host, f"mkdir -p {REMOTE_WORKER_DIR} && chmod 700 {REMOTE_WORKER_DIR}"
        )
        if mkdir_rc != 0:
            raise ilhaCommandError(f"Failed to prepare {REMOTE_WORKER_DIR} on {host}: {mkdir_err}")

        docker_err = ""
        for _ in range(max(retries, 1)):
            rc, _, docker_err = conn.run_remote_text(username, host, "docker info")
            if rc == 0:
                break
            sleep(retry_delay)
        else:
            raise ilhaCommandError(
                f"Docker is not ready on {host} after cloud-init: {docker_err}".strip()
            )

        for local_path, remote_name in (
            (bundle.compose_path, "docker-compose.yml"),
            (bundle.env_path, "env.ilha"),
        ):
            scp_cmd = conn.build_scp_command(
                str(local_path),
                f"{username}@{host}:{REMOTE_WORKER_DIR}/{remote_name}",
            )
            result = run(scp_cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                raise ilhaCommandError(f"Failed to copy {remote_name} to {host}: {err}")

        chmod_rc, _, chmod_err = conn.run_remote_text(
            username, host, f"chmod 600 {REMOTE_WORKER_DIR}/env.ilha {REMOTE_WORKER_DIR}/docker-compose.yml"
        )
        if chmod_rc != 0:
            raise ilhaCommandError(f"Failed to restrict env file mode on {host}: {chmod_err}")

        docker_login_remote(conn, username, host, registry_token, run=run)

        start_cmd = (
            f"cd {REMOTE_WORKER_DIR} && docker pull {shlex.quote(bundle.image_ref)} && "
            f"docker compose --env-file env.ilha up -d {shlex.quote(bundle.worker_service)}"
        )
        start_rc, _, start_err = conn.run_remote_text(username, host, start_cmd, timeout=300)
        if start_rc != 0:
            raise ilhaCommandError(f"Failed to start worker on {host}: {start_err}")
        log_success(f"Worker {bundle.worker_service} started on {host}")

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
