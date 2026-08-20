"""Registry-backed Docker image build/push support for ilha."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from ilha.commands.push.deploy_preflight import DeployPreflight
from ilha.commands.push.deploy_service_config import load_deploy_service_config
from ilha.config.settings import (
    ILHA_DIR,
    get_project_root,
    resolve_project_name,
    sanitize_project_name,
)
from ilha.core.droplet_manager import DropletManager
from ilha.core.digitalocean_registry import DigitalOceanRegistryClient
from ilha.exceptions import ilhaCommandError
from ilha.utils.compose_external_volumes import expand_compose_vars
from ilha.utils.confirmation import confirm_action
from ilha.utils.logging import log_info, log_success, log_warning
from ilha.utils.env_loader import is_sensitive_env_key
from ilha.utils.validation import validate_branch_name


DEFAULT_PROVIDER = "digitalocean"
DEFAULT_PLATFORM = "linux/amd64"
DEFAULT_TAG_TEMPLATE = "{branch}-{sha7}"
DOCR_HOST = "registry.digitalocean.com"
SUPPORTED_BUILD_KEYS = {"context", "dockerfile", "args", "target"}
TAG_TEMPLATE_FIELDS = "{branch}, {service}, {sha}, {sha7}"


@dataclass(frozen=True)
class DocrImageRef:
    """Parsed DigitalOcean Container Registry image reference."""

    registry_name: str
    repository: str
    tag: str
    image_ref: str


def parse_docr_image_ref(image_ref: str) -> DocrImageRef:
    """Parse ``registry.digitalocean.com/<registry>/<repo>:<tag>``."""
    ref = str(image_ref or "").strip()
    if not ref:
        raise ilhaCommandError("Image reference is empty")
    if "@sha256:" in ref:
        ref = ref.split("@sha256:", 1)[0]
    prefix = f"{DOCR_HOST}/"
    if not ref.startswith(prefix):
        raise ilhaCommandError(f"Not a DigitalOcean registry image reference: {image_ref}")
    remainder = ref[len(prefix) :]
    if ":" not in remainder:
        raise ilhaCommandError(f"Image reference missing tag: {image_ref}")
    path, tag = remainder.rsplit(":", 1)
    if not path or not tag:
        raise ilhaCommandError(f"Invalid image reference: {image_ref}")
    registry_name, _, repository = path.partition("/")
    if not registry_name or not repository:
        raise ilhaCommandError(f"Invalid image reference: {image_ref}")
    return DocrImageRef(
        registry_name=registry_name,
        repository=repository,
        tag=tag,
        image_ref=ref,
    )


@dataclass(frozen=True)
class ImageOptions:
    """CLI overrides for image commands."""

    provider: Optional[str] = None
    registry: Optional[str] = None
    repository: Optional[str] = None
    tag: Optional[str] = None
    platform: Optional[str] = None
    services: Tuple[str, ...] = ()
    save: bool = False
    no_cache: bool = False


@dataclass(frozen=True)
class ImageServicePlan:
    """Concrete Docker image action for one compose service."""

    service: str
    repository: str
    tag: str
    image_ref: str
    build: Dict[str, Any]
    build_signature: str
    primary_service: str

    @property
    def is_primary(self) -> bool:
        return self.service == self.primary_service


@dataclass
class ImagePlan:
    """Resolved image plan shared by build, push, and publish."""

    branch: str
    sha: str
    sha7: str
    provider: str
    registry: Optional[str]
    repository: str
    platform: str
    tag: str
    worktree_path: Path
    services: List[ImageServicePlan] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "branch": self.branch,
            "sha": self.sha,
            "sha7": self.sha7,
            "provider": self.provider,
            "registry": self.registry,
            "repository": self.repository,
            "platform": self.platform,
            "tag": self.tag,
            "worktree_path": str(self.worktree_path),
            "services": [
                {
                    "service": svc.service,
                    "repository": svc.repository,
                    "image": svc.image_ref,
                    "primary_service": svc.primary_service,
                }
                for svc in self.services
            ],
        }


class ImageManager:
    """Build and publish project images without mutating user compose files."""

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = Path(project_root or get_project_root()).resolve()

    @property
    def config_path(self) -> Path:
        return self.project_root / ILHA_DIR / "config.yml"

    def resolve_plan(self, branch_name: str, options: ImageOptions) -> ImagePlan:
        if not branch_name or not validate_branch_name(branch_name):
            raise ilhaCommandError(f"Invalid branch name: {branch_name!r}")

        worktree_path = DeployPreflight(self.project_root).resolve_worktree_path(branch_name)
        if not worktree_path.exists():
            raise ilhaCommandError(f"Worktree directory not found: {worktree_path}")

        compose_data = self._load_compose(worktree_path)
        image_cfg = self._load_image_config()
        provider = (options.provider or image_cfg.get("provider") or DEFAULT_PROVIDER).strip()
        if provider != DEFAULT_PROVIDER:
            raise ilhaCommandError(f"Unsupported image provider: {provider}")

        registry = self._clean_optional(options.registry or image_cfg.get("registry"))
        repository = (
            options.repository
            or image_cfg.get("repository")
            or sanitize_project_name(resolve_project_name(self.project_root))
        )
        repository = str(repository).strip().strip("/")
        platform = str(options.platform or image_cfg.get("platform") or DEFAULT_PLATFORM).strip()
        sha = self._git_sha(worktree_path)
        sha7 = sha[:7] if sha else "unknown"
        tag_template = str(options.tag or image_cfg.get("tag_template") or DEFAULT_TAG_TEMPLATE)
        tag = self._render_tag(
            tag_template,
            branch=branch_name,
            service="",
            sha=sha,
            sha7=sha7,
        )

        selected_services = self._select_services(compose_data, options.services)
        services_cfg = image_cfg.get("services") if isinstance(image_cfg.get("services"), dict) else {}
        preliminary = [
            self._build_service_plan(
                service,
                compose_data,
                branch=branch_name,
                sha=sha,
                sha7=sha7,
                provider=provider,
                registry=registry,
                repository=repository,
                tag=tag_template,
                services_cfg=services_cfg,
                primary_service=service,
            )
            for service in selected_services
        ]

        primary_by_signature: Dict[str, str] = {}
        for svc in preliminary:
            primary_by_signature.setdefault(svc.build_signature, svc.service)
        services = [
            ImageServicePlan(
                service=svc.service,
                repository=svc.repository,
                tag=svc.tag,
                image_ref=svc.image_ref,
                build=svc.build,
                build_signature=svc.build_signature,
                primary_service=primary_by_signature[svc.build_signature],
            )
            for svc in preliminary
        ]
        return ImagePlan(
            branch=branch_name,
            sha=sha,
            sha7=sha7,
            provider=provider,
            registry=registry,
            repository=repository,
            platform=platform,
            tag=tag,
            worktree_path=worktree_path,
            services=services,
        )

    def build(self, branch_name: str, options: ImageOptions) -> Dict[str, Any]:
        plan = self.resolve_plan(branch_name, options)
        if options.save:
            self.save_preferences(options)
        return self._build_plan(plan, no_cache=options.no_cache)

    def _build_plan(self, plan: ImagePlan, *, no_cache: bool) -> Dict[str, Any]:
        built_refs: set[str] = set()
        for primary, group in self._group_by_primary(plan).items():
            primary_plan = next(svc for svc in group if svc.service == primary)
            self._docker_build(primary_plan, plan, no_cache=no_cache)
            built_refs.add(primary_plan.image_ref)
            for svc in group:
                if svc.image_ref == primary_plan.image_ref:
                    continue
                self._run(["docker", "tag", primary_plan.image_ref, svc.image_ref])
                built_refs.add(svc.image_ref)
        log_success(f"Built {len(built_refs)} image tag(s)")
        return {"plan": plan.as_dict(), "built_images": sorted(built_refs)}

    def push(self, branch_name: str, options: ImageOptions) -> Dict[str, Any]:
        plan = self.resolve_plan(branch_name, options)
        if not plan.registry:
            raise ilhaCommandError("Image registry is required for push. Use --registry or image.registry in .ilha/config.yml.")
        if options.save:
            self.save_preferences(options)
        return self._push_plan(plan, branch_name=branch_name)

    def _push_plan(self, plan: ImagePlan, *, branch_name: str) -> Dict[str, Any]:
        self._docker_login(plan)
        pushed: Dict[str, Dict[str, Any]] = {}
        for svc in plan.services:
            if svc.image_ref in pushed:
                continue
            if not self._image_exists(svc.image_ref):
                raise ilhaCommandError(
                    f"Local image tag not found: {svc.image_ref}. "
                    f"Run `ilha image build {branch_name}` or `ilha image publish {branch_name}` first."
                )
            self._run(["docker", "push", svc.image_ref])
            pushed[svc.image_ref] = {"image": svc.image_ref, "digest": self._image_digest(svc.image_ref)}
        log_success(f"Pushed {len(pushed)} image tag(s)")
        return {"plan": plan.as_dict(), "pushed_images": list(pushed.values())}

    def publish(self, branch_name: str, options: ImageOptions) -> Dict[str, Any]:
        plan = self.resolve_plan(branch_name, options)
        if not plan.registry:
            raise ilhaCommandError("Image registry is required for publish. Use --registry or image.registry in .ilha/config.yml.")
        if options.save:
            self.save_preferences(options)
        build_result = self._build_plan(plan, no_cache=options.no_cache)
        push_result = self._push_plan(plan, branch_name=branch_name)
        manifest = self._write_manifest(plan, push_result["pushed_images"])
        return {
            "plan": build_result["plan"],
            "built_images": build_result["built_images"],
            "pushed_images": push_result["pushed_images"],
            "manifest": str(manifest),
        }

    def save_preferences(self, options: ImageOptions) -> None:
        """Persist provided reusable image preferences to project config."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {}
        if self.config_path.exists():
            data = yaml.safe_load(self.config_path.read_text()) or {}
            if not isinstance(data, dict):
                data = {}
        image_cfg = data.get("image") if isinstance(data.get("image"), dict) else {}
        image_cfg = dict(image_cfg)
        for key in ("provider", "registry", "repository", "platform"):
            value = getattr(options, key)
            if value:
                image_cfg[key] = value
        data["image"] = image_cfg
        self.config_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        log_success(f"Saved image preferences to {self.config_path}")

    def _load_image_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return {}
        data = yaml.safe_load(self.config_path.read_text()) or {}
        image_cfg = data.get("image") if isinstance(data, dict) else {}
        return image_cfg if isinstance(image_cfg, dict) else {}

    def _load_compose(self, worktree_path: Path) -> Dict[str, Any]:
        compose_path = worktree_path / "docker-compose.yml"
        if not compose_path.exists():
            raise ilhaCommandError(f"docker-compose.yml not found in worktree: {compose_path}")
        env = self._compose_env(worktree_path)
        expanded = "\n".join(expand_compose_vars(line, env) for line in compose_path.read_text().splitlines())
        data = yaml.safe_load(expanded) or {}
        if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
            raise ilhaCommandError(f"Invalid compose file: missing services in {compose_path}")
        return data

    def _compose_env(self, worktree_path: Path) -> Dict[str, str]:
        from ilha.utils.env_loader import load_env_file

        env: Dict[str, str] = dict(os.environ)
        for path in (self.project_root / ".env", worktree_path / ".env", worktree_path / ".ilha" / "env.ilha"):
            if path.exists():
                env.update(load_env_file(path))
        return env

    def _select_services(self, compose_data: Dict[str, Any], requested: Iterable[str]) -> List[str]:
        services = compose_data.get("services") or {}
        if requested:
            selected = list(dict.fromkeys(str(s) for s in requested))
        else:
            cfg = load_deploy_service_config(self.project_root)
            candidates = [cfg.web_service]
            if cfg.frontend_service:
                candidates.append(cfg.frontend_service)
            candidates.extend(cfg.worker_services)
            selected = [s for s in dict.fromkeys(candidates) if s in services and self._service_build(services[s])]
            for name, svc in services.items():
                if self._service_build(svc) and self._is_worker_like_service(name) and name not in selected:
                    selected.append(name)
        if not selected:
            raise ilhaCommandError("No buildable compose services found")
        missing = [s for s in selected if s not in services]
        if missing:
            raise ilhaCommandError(f"Service(s) not found in compose: {', '.join(missing)}")
        not_buildable = [s for s in selected if not self._service_build(services[s])]
        if not_buildable:
            raise ilhaCommandError(f"Service(s) have no build directive: {', '.join(not_buildable)}")
        return selected

    def _build_service_plan(
        self,
        service: str,
        compose_data: Dict[str, Any],
        *,
        branch: str,
        sha: str,
        sha7: str,
        provider: str,
        registry: Optional[str],
        repository: str,
        tag: str,
        services_cfg: Dict[str, Any],
        primary_service: str,
    ) -> ImageServicePlan:
        services = compose_data.get("services") or {}
        build = self._normalize_build(services[service].get("build"))
        svc_cfg = services_cfg.get(service) if isinstance(services_cfg.get(service), dict) else {}
        svc_repository = str(svc_cfg.get("repository") or f"{repository}/{service}").strip().strip("/")
        svc_tag = self._render_tag(tag, branch=branch, service=service, sha=sha, sha7=sha7)
        image_ref = self._image_ref(provider, registry, svc_repository, svc_tag)
        signature = json.dumps(build, sort_keys=True)
        return ImageServicePlan(
            service=service,
            repository=svc_repository,
            tag=svc_tag,
            image_ref=image_ref,
            build=build,
            build_signature=signature,
            primary_service=primary_service,
        )

    @staticmethod
    def _service_build(service: Any) -> Any:
        return service.get("build") if isinstance(service, dict) else None

    @staticmethod
    def _is_worker_like_service(service_name: str) -> bool:
        normalized = service_name.lower()
        return "worker" in normalized or normalized.startswith(("rq-", "celery"))

    def _normalize_build(self, build: Any) -> Dict[str, Any]:
        if isinstance(build, str):
            return {"context": build, "dockerfile": "Dockerfile", "args": {}, "target": None}
        if isinstance(build, dict):
            unsupported = sorted(set(build) - SUPPORTED_BUILD_KEYS)
            if unsupported:
                raise ilhaCommandError(
                    "Unsupported compose build key(s) for `ilha image`: "
                    f"{', '.join(unsupported)}. Use `docker compose build` for this service "
                    "or remove the unsupported build options before publishing with ilha."
                )
            return {
                "context": str(build.get("context") or "."),
                "dockerfile": str(build.get("dockerfile") or "Dockerfile"),
                "args": build.get("args") or {},
                "target": build.get("target"),
            }
        raise ilhaCommandError("Unsupported build directive")

    @staticmethod
    def _validate_build_args(build_args: Any) -> None:
        """Reject Docker build args that look like runtime secrets."""
        sensitive = [
            key
            for key, _ in ImageManager._iter_build_args(build_args)
            if is_sensitive_env_key(key)
        ]
        if sensitive:
            raise ilhaCommandError(
                "Sensitive values must not be passed as Docker build args: "
                f"{', '.join(sorted(sensitive))}. Use SOPS secret profiles at runtime instead."
            )

    def _docker_build(self, svc: ImageServicePlan, plan: ImagePlan, *, no_cache: bool) -> None:
        build = svc.build
        self._validate_build_args(build.get("args"))
        context = (plan.worktree_path / build["context"]).resolve()
        dockerfile = build["dockerfile"]
        dockerfile_path = Path(dockerfile)
        if not dockerfile_path.is_absolute():
            dockerfile_path = context / dockerfile
        cmd = ["docker", "build", "-t", svc.image_ref]
        if plan.platform:
            cmd.extend(["--platform", plan.platform])
        if build.get("target"):
            cmd.extend(["--target", str(build["target"])])
        for key, value in self._iter_build_args(build.get("args")):
            cmd.extend(["--build-arg", f"{key}={value}"])
        if no_cache:
            cmd.append("--no-cache")
        cmd.extend(["-f", str(dockerfile_path), str(context)])
        log_info(f"Building image for {svc.service}: {svc.image_ref}")
        self._run(cmd)

    def _group_by_primary(self, plan: ImagePlan) -> Dict[str, List[ImageServicePlan]]:
        groups: Dict[str, List[ImageServicePlan]] = {}
        for svc in plan.services:
            groups.setdefault(svc.primary_service, []).append(svc)
        return groups

    @staticmethod
    def _iter_build_args(args: Any) -> Iterable[Tuple[str, str]]:
        if isinstance(args, dict):
            for key, value in args.items():
                yield str(key), str(value)
        elif isinstance(args, list):
            for entry in args:
                if isinstance(entry, str) and "=" in entry:
                    key, value = entry.split("=", 1)
                    yield key, value

    @staticmethod
    def _render_tag(template: str, *, branch: str, service: str, sha: str, sha7: str) -> str:
        safe_branch = branch.replace("/", "-")
        try:
            return template.format(branch=safe_branch, service=service, sha=sha, sha7=sha7)
        except KeyError as exc:
            raise ilhaCommandError(
                f"Unknown image tag template field {exc!s}. Allowed fields: {TAG_TEMPLATE_FIELDS}."
            )

    @staticmethod
    def _clean_optional(value: Any) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @staticmethod
    def _image_ref(provider: str, registry: Optional[str], repository: str, tag: str) -> str:
        if provider == DEFAULT_PROVIDER and registry:
            return f"{DOCR_HOST}/{registry.strip('/')}/{repository}:{tag}"
        return f"{repository}:{tag}"

    @staticmethod
    def _git_sha(path: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            raise ilhaCommandError(f"Failed to resolve git SHA for {path}: {exc}") from exc
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        detail = (result.stderr or result.stdout or "").strip()
        raise ilhaCommandError(
            f"Failed to resolve git SHA for {path}"
            + (f": {detail}" if detail else "")
        )

    def _registry_client(self) -> DigitalOceanRegistryClient:
        token = DropletManager.resolve_droplet_token(None)
        if not token:
            raise ilhaCommandError("DigitalOcean API token not found")
        return DigitalOceanRegistryClient(token)

    def _docker_login(self, plan: ImagePlan) -> None:
        if plan.provider != DEFAULT_PROVIDER:
            return
        if not plan.registry:
            raise ilhaCommandError("Image registry is required for docker login")
        self._registry_client().docker_login(plan.registry)

    @staticmethod
    def _run(cmd: List[str]) -> None:
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            raise ilhaCommandError(f"Command failed ({result.returncode}): {' '.join(cmd)}")

    @staticmethod
    def _image_exists(image_ref: str) -> bool:
        result = subprocess.run(["docker", "image", "inspect", image_ref], capture_output=True, text=True)
        return result.returncode == 0

    @staticmethod
    def _image_digest(image_ref: str) -> Optional[str]:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", image_ref],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        digest_ref = result.stdout.strip()
        if "@sha256:" in digest_ref:
            return digest_ref.split("@", 1)[1]
        return digest_ref or None

    def _write_manifest(self, plan: ImagePlan, pushed_images: List[Dict[str, Any]]) -> Path:
        digest_by_image = {item["image"]: item.get("digest") for item in pushed_images}
        manifest = plan.as_dict()
        manifest["published_at"] = datetime.utcnow().isoformat() + "Z"
        for service in manifest["services"]:
            service["digest"] = digest_by_image.get(service["image"])
        images_dir = self.project_root / ILHA_DIR / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        path = images_dir / f"{plan.branch}.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        log_success(f"Wrote image manifest: {path}")
        return path

    def manifest_path(self, branch_name: str) -> Path:
        """Return the expected manifest path for a branch."""
        return self.project_root / ILHA_DIR / "images" / f"{branch_name}.json"

    def load_manifest(self, branch_name: str) -> Dict[str, Any]:
        """Load a published image manifest for a branch."""
        path = self.manifest_path(branch_name)
        if not path.exists():
            raise ilhaCommandError(
                f"Image manifest not found: {path}. "
                f"Run `ilha image publish {branch_name}` first."
            )
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ilhaCommandError(f"Invalid image manifest at {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ilhaCommandError(f"Invalid image manifest at {path}: expected object")
        return data

    def image_ref_for_service(
        self,
        branch_name: str,
        service: str,
        *,
        prefer_digest: bool = True,
    ) -> str:
        """Resolve the registry image reference for a compose service from the manifest."""
        manifest = self.load_manifest(branch_name)
        services = manifest.get("services") or []
        if not isinstance(services, list):
            raise ilhaCommandError(f"Invalid services list in manifest for {branch_name}")
        match = next((s for s in services if isinstance(s, dict) and s.get("service") == service), None)
        if not match:
            available = ", ".join(
                sorted(str(s.get("service")) for s in services if isinstance(s, dict) and s.get("service"))
            )
            raise ilhaCommandError(
                f"Service {service!r} not found in image manifest for {branch_name}. "
                f"Available: {available or '(none)'}"
            )
        image = str(match.get("image") or "").strip()
        if not image:
            raise ilhaCommandError(f"Manifest entry for {service!r} has no image reference")
        digest = match.get("digest")
        if prefer_digest and digest:
            digest = str(digest).strip()
            if digest and not digest.startswith("sha256:"):
                digest = f"sha256:{digest}"
            if digest:
                base = image.split("@", 1)[0]
                return f"{base}@{digest}"
        return image

    def images_dir(self) -> Path:
        return self.project_root / ILHA_DIR / "images"

    def list_manifests(self, branch: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load all published image manifests, optionally filtered by branch."""
        images_dir = self.images_dir()
        if not images_dir.exists():
            return []

        if branch:
            paths = [self.manifest_path(branch)]
            if not paths[0].exists():
                return []
        else:
            paths = sorted(images_dir.glob("*.json"))

        manifests: List[Dict[str, Any]] = []
        for path in paths:
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                log_warning(f"Skipping invalid manifest {path}: {exc}")
                continue
            if not isinstance(data, dict):
                log_warning(f"Skipping invalid manifest {path}: expected object")
                continue
            data.setdefault("branch", path.stem)
            data["_manifest_path"] = str(path)
            manifests.append(data)
        return manifests

    @staticmethod
    def flatten_manifest_rows(manifests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Expand manifests into one row per service for tabular display."""
        rows: List[Dict[str, Any]] = []
        for manifest in manifests:
            branch = manifest.get("branch", "")
            published_at = manifest.get("published_at", "")
            for svc in manifest.get("services") or []:
                if not isinstance(svc, dict):
                    continue
                rows.append(
                    {
                        "branch": branch,
                        "published_at": published_at,
                        "service": svc.get("service", ""),
                        "image": svc.get("image", ""),
                        "digest": svc.get("digest", ""),
                    }
                )
        return rows

    def list_remote_tags(
        self,
        registry: str,
        repository: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List tags in DigitalOcean Container Registry via the DO API."""
        registry = str(registry).strip()
        if not registry:
            raise ilhaCommandError("Registry name is required for remote listing")

        client = self._registry_client()
        repositories: List[str]
        if repository:
            repositories = [str(repository).strip().strip("/")]
        else:
            repositories = client.list_repositories(registry)

        tags: List[Dict[str, Any]] = []
        for repo in repositories:
            try:
                tag_rows = client.list_tags(registry, repo)
            except ilhaCommandError as exc:
                raise ilhaCommandError(f"Failed to list tags for {repo}: {exc}") from exc
            for row in tag_rows:
                if not isinstance(row, dict):
                    continue
                tag_name = row.get("tag")
                if not tag_name:
                    continue
                tags.append(
                    {
                        "registry": registry,
                        "repository": repo,
                        "tag": tag_name,
                        "image": f"{DOCR_HOST}/{registry}/{repo}:{tag_name}",
                        "updated_at": row.get("updated_at"),
                        "compressed_size_bytes": row.get("compressed_size_bytes"),
                    }
                )
        return tags

    def _delete_docr_tag(self, ref: DocrImageRef, *, registry_override: Optional[str] = None) -> None:
        registry_name = registry_override or ref.registry_name
        try:
            self._registry_client().delete_tag(registry_name, ref.repository, ref.tag)
        except ilhaCommandError as exc:
            raise ilhaCommandError(
                f"Failed to delete tag {ref.repository}:{ref.tag}: {exc}"
            ) from exc
        log_success(f"Deleted registry tag: {DOCR_HOST}/{registry_name}/{ref.repository}:{ref.tag}")

    def destroy_branch(
        self,
        branch: str,
        *,
        services: Optional[Iterable[str]] = None,
        manifest_only: bool = False,
        registry_only: bool = False,
        registry_override: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Delete published images for a branch from DOCR and/or local manifest."""
        if manifest_only and registry_only:
            raise ilhaCommandError("Cannot specify both --manifest-only and --registry-only")

        manifest = self.load_manifest(branch)
        provider = str(manifest.get("provider") or DEFAULT_PROVIDER)
        if provider != DEFAULT_PROVIDER and not manifest_only:
            raise ilhaCommandError(f"Unsupported image provider for destroy: {provider}")

        service_filter = {str(s) for s in services} if services else None
        entries = [
            svc
            for svc in (manifest.get("services") or [])
            if isinstance(svc, dict) and svc.get("image")
            and (not service_filter or svc.get("service") in service_filter)
        ]
        if service_filter:
            found = {str(svc.get("service")) for svc in entries}
            missing = sorted(service_filter - found)
            if missing:
                raise ilhaCommandError(
                    f"Service(s) not found in manifest for {branch}: {', '.join(missing)}"
                )
        if not entries and not manifest_only:
            raise ilhaCommandError(f"No image entries to destroy for branch {branch}")

        if not force:
            summary_lines = [
                f"  {svc.get('service')}: {svc.get('image')}"
                for svc in entries
            ]
            manifest_path = self.manifest_path(branch)
            if manifest_only and not summary_lines:
                prompt = f"Remove manifest for branch {branch!r}?\n  manifest: {manifest_path}"
            else:
                prompt = (
                    f"Destroy published images for branch {branch!r}?\n"
                    + "\n".join(summary_lines)
                    + (f"\n  manifest: {manifest_path}" if not registry_only else "")
                )
            if not confirm_action(f"{prompt}\nProceed?"):
                raise ilhaCommandError("Destroy cancelled")

        deleted_tags: List[str] = []
        if not manifest_only:
            for svc in entries:
                ref = parse_docr_image_ref(str(svc.get("image")))
                self._delete_docr_tag(ref, registry_override=registry_override)
                deleted_tags.append(ref.image_ref)

        manifest_removed = False
        if not registry_only:
            path = self.manifest_path(branch)
            if path.exists():
                path.unlink()
                manifest_removed = True
                log_success(f"Removed manifest: {path}")

        return {
            "branch": branch,
            "deleted_tags": deleted_tags,
            "manifest_removed": manifest_removed,
            "services": [svc.get("service") for svc in entries],
        }

    def destroy_branches(
        self,
        branches: Iterable[str],
        *,
        services: Optional[Iterable[str]] = None,
        manifest_only: bool = False,
        registry_only: bool = False,
        registry_override: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Destroy published images for multiple branches."""
        results = []
        for branch in branches:
            branch = branch.strip()
            if not branch:
                continue
            results.append(
                self.destroy_branch(
                    branch,
                    services=services,
                    manifest_only=manifest_only,
                    registry_only=registry_only,
                    registry_override=registry_override,
                    force=force,
                )
            )
        return {"results": results, "count": len(results)}
