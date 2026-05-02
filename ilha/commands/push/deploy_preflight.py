"""
Deploy preflight checks: manifest gathering, drift detection, and source validation.

Extracted from SlimDeployManager to keep deploy orchestration and pre-deploy
validation in separate modules.
"""

import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from ...config.settings import get_worktree_dir
from ...utils.checksum import calculate_file_checksum
from ...utils.logging import (
    log_warning,
    log_error,
    print_plain,
)
from ...utils.project_footprint import detect_project_footprint


class DeployPreflight:
    """Pre-deploy validation: resolve paths, gather manifests, detect drift."""

    _DRIFT_RULES: List[Tuple[str, str]] = [
        ("Dockerfile", "warning"),
        ("docker-compose.yml", "error"),
        (".ilha/docker-compose.overlay.yml", "error"),
        ("pyproject.toml", "error"),
        ("uv.lock", "error"),
    ]

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()

    # ── path resolution ──────────────────────────────────────────────

    def resolve_worktree_path(self, branch_name: str) -> Path:
        worktree_dir = get_worktree_dir()
        candidate = self.project_root / worktree_dir / branch_name
        if candidate.exists():
            return candidate.resolve()
        current_candidate = self.project_root / ".ilha" / "docker-compose.overlay.yml"
        if current_candidate.exists():
            return self.project_root.resolve()
        return candidate.resolve()

    def resolve_comparison_root(self, worktree_path: Path) -> Path:
        if worktree_path != self.project_root.resolve():
            return self.project_root.resolve()

        worktree_dir = get_worktree_dir()
        for parent in worktree_path.parents:
            candidate = parent / worktree_dir
            if candidate.exists() and candidate.is_dir():
                return parent.resolve()
        return self.project_root.resolve()

    # ── git helpers ──────────────────────────────────────────────────

    def _get_git_branch(self, path: Path) -> Optional[str]:
        from ...utils.path_utils import get_worktree_branch_name
        try:
            branch = get_worktree_branch_name(path)
            return branch
        except (FileNotFoundError, Exception):
            return None

    def _is_git_dirty(self, path: Path) -> Optional[bool]:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
                cwd=path,
                timeout=10,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return None
        return bool((result.stdout or "").strip())

    def _git_rev_parse_head(self, path: Path) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
                cwd=path,
                timeout=10,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return None
        head = (result.stdout or "").strip()
        return head or None

    # ── checksum / smoke path ────────────────────────────────────────

    def _optional_checksum(self, path: Path) -> Optional[str]:
        if not path.exists() or not path.is_file():
            return None
        try:
            return calculate_file_checksum(path)
        except (FileNotFoundError, IOError):
            return None

    def _warn_if_django_missing_collectstatic(
        self, worktree_path: Path, footprint: Any,
    ) -> None:
        """Surface the ``/static/admin/*`` 404 trap early.

        Django's prod static (admin CSS, app statics) requires ``collectstatic``
        to populate ``STATIC_ROOT`` before the server starts. Whitenoise
        respects the ``DEBUG`` flag — on a prod deploy (DEBUG=False, which
        ilha sets) it serves only the collected files, not the source tree.
        Projects that forget the ``collectstatic`` step in their compose
        ``command`` end up with a working Django admin login *page* but
        unstyled assets.
        """
        if not getattr(footprint, "has_django", False):
            return
        backend_service = getattr(footprint, "backend_service", None) or "web"

        try:
            import yaml

            compose_path = worktree_path / "docker-compose.yml"
            if not compose_path.exists():
                return
            data = yaml.safe_load(compose_path.read_text()) or {}
        except Exception:
            return

        service = (data.get("services") or {}).get(backend_service)
        if not isinstance(service, dict):
            return

        cmd = service.get("command")
        if isinstance(cmd, list):
            cmd_str = " ".join(str(part) for part in cmd)
        else:
            cmd_str = str(cmd or "")

        if not cmd_str:
            # No command means the image's entrypoint runs; we can't see what
            # it does. Stay quiet rather than nag with a false-positive.
            return

        if "collectstatic" in cmd_str.lower():
            return

        log_warning(
            f"Django backend '{backend_service}' command does not invoke "
            "`manage.py collectstatic`. Static admin assets (e.g. "
            "/static/admin/css/base.css) will 404 in prod. Add "
            "`python manage.py collectstatic --noinput &&` to the compose "
            "`command` before your server start."
        )

    def _detect_smoke_path(self, worktree_compose: Path, comparison_root: Path) -> str:
        try:
            import yaml

            compose_data = yaml.safe_load(worktree_compose.read_text()) or {}
            services = compose_data.get("services") or {}
            for service_data in services.values():
                labels = service_data.get("labels") or {}
                if isinstance(labels, list):
                    label_items = []
                    for item in labels:
                        if isinstance(item, str) and "=" in item:
                            key, value = item.split("=", 1)
                            label_items.append((key, value))
                    labels = dict(label_items)
                if isinstance(labels, dict) and labels.get("caddy.proxy.health_check"):
                    return str(labels["caddy.proxy.health_check"]).strip()
        except Exception as exc:
            log_warning(f"Could not parse {worktree_compose} for smoke path: {exc}")

        for candidate in (
            comparison_root / "README.md",
            comparison_root / "documentation" / "PROJECT_SETUP.md",
        ):
            if candidate.exists():
                try:
                    if "/api/health/" in candidate.read_text():
                        return "/api/health/"
                except Exception as exc:
                    log_warning(f"Could not read {candidate} for smoke path hint: {exc}")
                    continue
        return "/"

    # ── manifest & drift ─────────────────────────────────────────────

    def gather_source_manifest(
        self,
        branch_name: str,
        worktree_path: Path,
        comparison_root: Path,
    ) -> Dict[str, Any]:
        files = {
            "Dockerfile": worktree_path / "Dockerfile",
            "docker-compose.yml": worktree_path / "docker-compose.yml",
            ".ilha/docker-compose.overlay.yml": worktree_path / ".ilha" / "docker-compose.overlay.yml",
            "pyproject.toml": worktree_path / "pyproject.toml",
            "uv.lock": worktree_path / "uv.lock",
        }
        return {
            "branch_name": branch_name,
            "source_path": str(worktree_path),
            "comparison_root": str(comparison_root),
            "git_branch": self._get_git_branch(worktree_path),
            "git_dirty": self._is_git_dirty(worktree_path),
            "checksums": {
                name: self._optional_checksum(path)
                for name, path in files.items()
            },
        }

    def build_drift_report(
        self,
        worktree_path: Path,
        comparison_root: Path,
        strict_deploy_drift: bool = False,
    ) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "source_path": str(worktree_path),
            "comparison_root": str(comparison_root),
            "status": "ok",
            "differences": [],
            "skipped": False,
            "skip_reason": None,
        }
        if comparison_root == worktree_path:
            return report

        if not strict_deploy_drift:
            worktree_head = self._git_rev_parse_head(worktree_path)
            comparison_head = self._git_rev_parse_head(comparison_root)
            if worktree_head and comparison_head and worktree_head != comparison_head:
                report["skipped"] = True
                report["skip_reason"] = "different_git_heads"
                return report

        for relative_path, severity in self._DRIFT_RULES:
            source_file = worktree_path / relative_path
            root_file = comparison_root / relative_path
            if not source_file.exists() or not root_file.exists():
                continue

            source_checksum = self._optional_checksum(source_file)
            root_checksum = self._optional_checksum(root_file)
            if not source_checksum or not root_checksum or source_checksum == root_checksum:
                continue

            entry = {
                "file": relative_path,
                "severity": severity,
                "source_path": str(source_file),
                "comparison_path": str(root_file),
                "source_checksum": source_checksum,
                "comparison_checksum": root_checksum,
            }
            report["differences"].append(entry)
            if severity == "error":
                report["status"] = "error"
            elif report["status"] == "ok":
                report["status"] = "warning"
        return report

    # ── display ──────────────────────────────────────────────────────

    def _print_source_manifest(
        self,
        manifest: Dict[str, Any],
        drift_report: Dict[str, Any],
        smoke_path: str,
        footprint: Optional[Any] = None,
    ) -> None:
        print_plain("")
        print_plain("Deploy source manifest:")
        print_plain(f"  Source path: {manifest['source_path']}")
        print_plain(f"  Branch: {manifest['branch_name']}")
        print_plain(f"  Comparison root: {manifest['comparison_root']}")
        print_plain(f"  Git branch: {manifest.get('git_branch') or 'unknown'}")
        dirty = manifest.get("git_dirty")
        print_plain(f"  Dirty: {dirty if dirty is not None else 'unknown'}")
        for name, checksum in manifest.get("checksums", {}).items():
            print_plain(f"  {name}: {checksum or 'missing'}")
        wt = Path(manifest["source_path"])
        if (
            not (wt / "Dockerfile").exists()
            and (wt / "docker-compose.yml").exists()
        ):
            print_plain(
                "  Note: No Dockerfile is OK when "
                "`docker-compose.yml` defines an image-based app "
                "(e.g. a Caddy-based static site)."
            )
        print_plain(f"  Smoke path: {smoke_path}")
        if footprint is not None:
            arche = getattr(footprint.archetype, "value", footprint.archetype)
            print_plain(f"  Archetype: {arche}")
            if footprint.backend_service:
                print_plain(f"  Backend service: {footprint.backend_service}")
            if footprint.frontend_service:
                print_plain(f"  Frontend service: {footprint.frontend_service}")
        if drift_report.get("differences"):
            print_plain("  Drift detected:")
            for entry in drift_report["differences"]:
                print_plain(
                    f"    {entry['severity'].upper()}: {entry['file']} differs "
                    f"({entry['comparison_path']} vs {entry['source_path']})"
                )
        if drift_report.get("skipped") and drift_report.get("skip_reason") == "different_git_heads":
            print_plain("  Drift check skipped: comparison root and worktree are on different git commits")
        print_plain("")

    # ── orchestrator ─────────────────────────────────────────────────

    def run_deploy_preflight(
        self,
        branch_name: str,
        strict_deploy_drift: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Run preflight checks and return context dict, or None on failure.

        The returned dict contains:
          - worktree_path, comparison_root, manifest, drift_report, smoke_path,
            footprint

        Checks performed are scoped to ilha-owned concerns: source manifest,
        critical-file drift between worktree and comparison root, and smoke-path
        detection. App-owned env values (DJANGO_SECRET_KEY, POSTGRES_PASSWORD,
        SITE_DOMAIN shape) are not policed here — the user owns their compose
        and .env.
        """
        worktree_path = self.resolve_worktree_path(branch_name)
        if not worktree_path.exists():
            log_error(f"Worktree directory not found: {worktree_path}")
            return None

        comparison_root = self.resolve_comparison_root(worktree_path)
        footprint = detect_project_footprint(worktree_path)
        manifest = self.gather_source_manifest(branch_name, worktree_path, comparison_root)
        drift_report = self.build_drift_report(
            worktree_path,
            comparison_root,
            strict_deploy_drift=strict_deploy_drift,
        )
        smoke_path = self._detect_smoke_path(worktree_path / ".ilha" / "docker-compose.overlay.yml", comparison_root)

        self._print_source_manifest(manifest, drift_report, smoke_path, footprint=footprint)

        # Project-side checks: warn early so the user has a chance to fix the
        # compose before sitting through a full deploy that surfaces a 404.
        self._warn_if_django_missing_collectstatic(worktree_path, footprint)

        for entry in drift_report.get("differences", []):
            message = (
                f"Critical deploy file drift: {entry['file']} differs between "
                f"{entry['comparison_path']} and {entry['source_path']}"
            )
            if entry["severity"] == "warning":
                log_warning(message)
            else:
                log_error(message)

        if drift_report.get("status") == "error":
            return None

        return {
            "worktree_path": worktree_path,
            "comparison_root": comparison_root,
            "manifest": manifest,
            "drift_report": drift_report,
            "smoke_path": smoke_path,
            "footprint": footprint,
        }
