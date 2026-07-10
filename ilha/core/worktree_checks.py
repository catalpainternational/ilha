"""Worktree health checks — one engine, two consumers.

``run_worktree_checks`` is a pure function over a ``WorktreeCheckContext`` (which
does all the I/O of loading compose/env/footprint once). Two consumers share it:

- ``ilha doctor`` renders the full report and never blocks.
- The ``up`` / ``create`` preflight runs the error-severity subset and blocks
  with the ``remedy`` text when something is wrong.

Keeping the checks pure over a pre-loaded context means every check is unit
testable without Docker or a real worktree — construct a context and assert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

OK = "ok"
WARNING = "warning"
ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    """One check outcome. ``remedy`` is the exact, user-actionable fix."""

    check_id: str
    severity: str
    message: str
    remedy: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "check_id": self.check_id,
            "severity": self.severity,
            "message": self.message,
            "remedy": self.remedy,
        }


@dataclass
class WorktreeCheckContext:
    """Everything the checks read, loaded once. Checks never do their own I/O."""

    branch: str
    worktree_path: Optional[Path]
    base_compose: Optional[Dict[str, Any]] = None
    overlay_compose: Optional[Dict[str, Any]] = None
    merged_env: Dict[str, str] = field(default_factory=dict)
    footprint: Any = None
    manifest: Any = None
    env_error: Optional[str] = None  # set when an env file exists but wouldn't load
    overlay_error: Optional[str] = None  # set when the overlay exists but wouldn't parse
    manifest_error: Optional[str] = None  # set when .ilha/ilha.yml exists but wouldn't load

    @classmethod
    def load(cls, branch: str, project_root: Optional[Path] = None) -> "WorktreeCheckContext":
        """Build a context by loading a worktree's compose/env/footprint."""
        import yaml

        from ..config.settings import get_worktree_paths
        from ..utils.env_loader import load_env_file, merge_env_layers
        from ..utils.path_utils import (
            compose_files_for_invocation,
            get_env_compose_file_path,
            get_env_file_path,
        )
        from ..exceptions import EnvFileError

        new_path, legacy_path = get_worktree_paths(branch)
        worktree_path: Optional[Path]
        if new_path.exists():
            worktree_path = new_path
        elif legacy_path.exists():
            worktree_path = legacy_path
        else:
            worktree_path = None

        ctx = cls(branch=branch, worktree_path=worktree_path)
        if worktree_path is None:
            return ctx

        base_path, overlay_path = compose_files_for_invocation(worktree_path)
        if base_path is not None:
            try:
                ctx.base_compose = yaml.safe_load(base_path.read_text()) or {}
            except (yaml.YAMLError, OSError):
                # Left as None; _check_files reports it as missing/unparseable.
                ctx.base_compose = None
        if overlay_path is not None:
            try:
                ctx.overlay_compose = yaml.safe_load(overlay_path.read_text()) or {}
            except (yaml.YAMLError, OSError) as exc:
                ctx.overlay_error = str(exc)

        env_file = get_env_file_path(worktree_path)
        env_ilha = get_env_compose_file_path(worktree_path)
        try:
            layers = []
            if env_file.exists():
                layers.append(load_env_file(env_file))
            if env_ilha.exists():
                layers.append(load_env_file(env_ilha))
            ctx.merged_env = merge_env_layers(*layers)
        except EnvFileError as exc:
            ctx.env_error = str(exc)
            ctx.merged_env = {}

        from ..utils.project_footprint import detect_project_footprint

        ctx.footprint = detect_project_footprint(worktree_path)

        from ..config.manifest import load_manifest

        try:
            ctx.manifest = load_manifest(worktree_path)
        except Exception as exc:
            ctx.manifest_error = str(exc)
        return ctx


# --- individual checks (pure over the context) ------------------------------

def _check_worktree_exists(ctx: WorktreeCheckContext) -> List[CheckResult]:
    if ctx.worktree_path is None:
        return [
            CheckResult(
                "worktree.exists",
                ERROR,
                f"No worktree found for branch '{ctx.branch}'.",
                f"Run `ilha create {ctx.branch}` to create it.",
            )
        ]
    return []


def _check_files(ctx: WorktreeCheckContext) -> List[CheckResult]:
    results: List[CheckResult] = []
    if ctx.overlay_error is not None:
        results.append(
            CheckResult(
                "files.overlay",
                ERROR,
                f"Overlay compose failed to parse: {ctx.overlay_error}",
                "Re-run `ilha setup` to regenerate .ilha/docker-compose.overlay.yml.",
            )
        )
    elif ctx.overlay_compose is None:
        results.append(
            CheckResult(
                "files.overlay",
                ERROR,
                "Missing .ilha/docker-compose.overlay.yml.",
                "Re-run `ilha setup` in the project root.",
            )
        )
    if ctx.base_compose is None:
        results.append(
            CheckResult(
                "files.base",
                ERROR,
                "Missing or unparseable docker-compose.yml at the worktree root.",
                "Restore your project's docker-compose.yml.",
            )
        )
    return results


def _check_env(ctx: WorktreeCheckContext) -> List[CheckResult]:
    if ctx.env_error is not None:
        return [
            CheckResult(
                "env.readable",
                ERROR,
                f"An env file exists but could not be read: {ctx.env_error}",
                "Fix the file's permissions/encoding, or delete it and re-run `ilha create --force`.",
            )
        ]
    from ..config.env_schema import validate_env

    results: List[CheckResult] = []
    for issue in validate_env(ctx.merged_env, ctx.footprint):
        results.append(
            CheckResult(
                f"env.{issue.key.lower()}",
                issue.severity,
                issue.message,
                _env_remedy(issue.key),
            )
        )
    return results


def _env_remedy(key: str) -> str:
    if key == "DJANGO_SECRET_KEY":
        return "Set a unique DJANGO_SECRET_KEY in .env (ilha generates one for fresh worktrees)."
    if key.startswith("POSTGRES_"):
        return f"Set {key} in .env so the app and the db container agree."
    return f"Set {key} in .env."


def _routable_services(overlay: Optional[Dict[str, Any]]) -> List[str]:
    services = (overlay or {}).get("services") or {}
    if not isinstance(services, dict):
        return []
    routable = []
    for name, sc in services.items():
        if isinstance(sc, dict) and "caddy.proxy" in (sc.get("labels") or {}):
            routable.append(name)
    return routable


def _check_routing(ctx: WorktreeCheckContext) -> List[CheckResult]:
    # If the overlay is missing/broken, files.overlay already covers it.
    if ctx.overlay_compose is None:
        return []
    routable = _routable_services(ctx.overlay_compose)
    if not routable:
        return [
            CheckResult(
                "routing.routable",
                ERROR,
                "No service is routable: nothing carries Caddy proxy labels, so the "
                "worktree will start but no domain will reach it.",
                "Name your web service one of web/app/frontend/api/backend, or add "
                "`caddy.proxy` labels to it, then re-run `ilha setup`.",
            )
        ]
    # Warn when a routable service's port was inferred (no expose/ports in base).
    results: List[CheckResult] = []
    for name in routable:
        if not _service_declares_port(ctx.base_compose, name):
            results.append(
                CheckResult(
                    "routing.port",
                    WARNING,
                    f"Service '{name}' declares no expose/ports; its proxy port was "
                    "inferred and may be wrong.",
                    f"Add an `expose:` entry to '{name}' in docker-compose.yml matching "
                    "the port your app listens on.",
                )
            )
    return results


def _service_declares_port(base_compose: Optional[Dict[str, Any]], service_name: str) -> bool:
    svc = ((base_compose or {}).get("services") or {}).get(service_name) or {}
    if not isinstance(svc, dict):
        return False
    return bool(svc.get("expose") or svc.get("ports"))


def _check_manifest(ctx: WorktreeCheckContext) -> List[CheckResult]:
    if ctx.manifest_error is not None:
        return [
            CheckResult(
                "manifest.valid",
                ERROR,
                f"Manifest .ilha/ilha.yml is invalid: {ctx.manifest_error}",
                "Fix the fields in .ilha/ilha.yml, or re-run `ilha setup --force` to regenerate it.",
            )
        ]
    if ctx.manifest is None:
        return [
            CheckResult(
                "manifest.exists",
                WARNING,
                "No .ilha/ilha.yml manifest — ilha is falling back to detection heuristics.",
                "Run `ilha setup` in the project root to ratify how services are routed.",
            )
        ]
    results: List[CheckResult] = []
    if ctx.base_compose is not None:
        from ..config.manifest import diff_manifest_vs_compose

        for msg in diff_manifest_vs_compose(ctx.manifest, ctx.base_compose):
            results.append(
                CheckResult(
                    "manifest.drift",
                    ERROR,
                    msg,
                    "Update .ilha/ilha.yml (or your compose) so they agree, then re-run `ilha setup`.",
                )
            )
    return results


def _check_conflicts(ctx: WorktreeCheckContext) -> List[CheckResult]:
    if ctx.base_compose is None:
        return []
    from ..utils.overlay_generator import detect_conflicting_services

    conflicts = detect_conflicting_services(ctx.base_compose)
    if not conflicts:
        return []
    joined = ", ".join(conflicts)
    return [
        CheckResult(
            "compose.conflicts",
            WARNING,
            f"Your compose defines service(s) that conflict with ilha's global proxy: {joined}.",
            f"Remove {joined} from docker-compose.yml — ilha provides the proxy.",
        )
    ]


def _check_docker() -> List[CheckResult]:
    from ..utils.validation import validate_docker_running, validate_docker_compose

    results: List[CheckResult] = []
    if not validate_docker_running():
        results.append(
            CheckResult(
                "docker.running",
                ERROR,
                "Docker does not appear to be running.",
                "Start Docker Desktop / the Docker daemon and retry.",
            )
        )
    elif not validate_docker_compose():
        results.append(
            CheckResult(
                "docker.compose",
                ERROR,
                "`docker compose` is not available.",
                "Install Docker Compose v2 (bundled with recent Docker).",
            )
        )
    return results


def run_worktree_checks(
    ctx: WorktreeCheckContext, *, include_docker: bool = True
) -> List[CheckResult]:
    """Run all checks over a loaded context, ordered most-fundamental first.

    ``include_docker`` gates the Docker daemon probes (skip in pure unit tests).
    """
    # A missing worktree short-circuits: nothing else can be meaningfully checked.
    missing = _check_worktree_exists(ctx)
    if missing:
        return missing

    results: List[CheckResult] = []
    results.extend(_check_files(ctx))
    results.extend(_check_manifest(ctx))
    results.extend(_check_env(ctx))
    results.extend(_check_routing(ctx))
    results.extend(_check_conflicts(ctx))
    if include_docker:
        results.extend(_check_docker())
    return results


def has_errors(results: List[CheckResult]) -> bool:
    return any(r.severity == ERROR for r in results)


def errors_only(results: List[CheckResult]) -> List[CheckResult]:
    return [r for r in results if r.severity == ERROR]
