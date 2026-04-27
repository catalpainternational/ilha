"""
Deploy preflight checks: manifest gathering, drift detection, and source validation.

Extracted from SlimDeployManager to keep deploy orchestration and pre-deploy
validation in separate modules.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from ...config.settings import get_worktree_dir
from ...utils.checksum import calculate_file_checksum
from ...utils.deploy_hosts import normalize_deploy_host
from ...utils.env_loader import load_env_file
from ...utils.logging import (
    log_info,
    log_warning,
    log_error,
    print_plain,
)


class DeployPreflight:
    """Pre-deploy validation: resolve paths, gather manifests, detect drift."""

    _DRIFT_RULES: List[Tuple[str, str]] = [
        ("Dockerfile", "warning"),
        (".ilha/docker-compose.worktree.yml", "error"),
        ("pyproject.toml", "error"),
        ("uv.lock", "error"),
    ]

    # Environment variables and their known-insecure default values.
    # Deploy is blocked if any of these match, unless ILHA_ALLOW_INSECURE_DEFAULTS=1.
    # Values are exact matches; prefixes are checked separately below.
    _INSECURE_DEFAULTS: Dict[str, List[str]] = {
        "DJANGO_SECRET_KEY": ["django-insecure-secret-key", ""],
        "POSTGRES_PASSWORD": ["bipassword", ""],
    }
    # Values starting with these prefixes are also considered insecure.
    _INSECURE_PREFIXES: Dict[str, List[str]] = {
        "DJANGO_SECRET_KEY": ["django-insecure-"],
    }

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()

    # ── path resolution ──────────────────────────────────────────────

    def resolve_worktree_path(self, branch_name: str) -> Path:
        worktree_dir = get_worktree_dir()
        candidate = self.project_root / worktree_dir / branch_name
        if candidate.exists():
            return candidate.resolve()
        current_candidate = self.project_root / ".ilha" / "docker-compose.worktree.yml"
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

    def detect_env_contract_issues(self, worktree_path: Path) -> List[Dict[str, Any]]:
        warnings: List[Dict[str, Any]] = []
        env_candidates = [
            worktree_path / ".env",
            worktree_path / ".ilha" / "env.ilha",
        ]

        for env_path in env_candidates:
            if not env_path.exists():
                continue
            try:
                env_values = load_env_file(env_path)
            except Exception as exc:
                log_warning(f"Could not parse env file {env_path} for contract check: {exc}")
                continue

            site_domain = (env_values.get("SITE_DOMAIN") or "").strip()
            normalized_site_domain = normalize_deploy_host(site_domain)
            if site_domain and normalized_site_domain and site_domain != normalized_site_domain:
                warnings.append(
                    {
                        "file": str(env_path),
                        "variable": "SITE_DOMAIN",
                        "current_value": site_domain,
                        "normalized_value": normalized_site_domain,
                        "message": (
                            f"{env_path} stores SITE_DOMAIN={site_domain!r}; "
                            f"expected bare host {normalized_site_domain!r}"
                        ),
                    }
                )

        return warnings

    # ── insecure defaults ────────────────────────────────────────────

    def _check_insecure_defaults(self, worktree_path: Path) -> List[Dict[str, Any]]:
        """Check merged environment for known-insecure default values.

        Returns a list of issues (empty = all clear).  Each issue is a dict
        with keys: variable, value, file, message.
        """
        issues: List[Dict[str, Any]] = []
        env_candidates = [
            worktree_path / ".env",
            worktree_path / ".ilha" / "env.ilha",
        ]
        merged: Dict[str, str] = {}
        source_file: Dict[str, str] = {}
        for env_path in env_candidates:
            if not env_path.exists():
                continue
            try:
                env_values = load_env_file(env_path)
            except Exception:
                continue
            for k, v in env_values.items():
                merged[k] = v
                source_file[k] = str(env_path)

        for var_name, bad_values in self._INSECURE_DEFAULTS.items():
            value = merged.get(var_name)
            if value is None:
                # Variable not set at all — likely relying on compose-level default
                issues.append({
                    "variable": var_name,
                    "value": "(unset)",
                    "file": "N/A",
                    "message": (
                        f"{var_name} is not set in any env file. "
                        f"It will fall back to the insecure compose default. "
                        f"Set it explicitly before deploying."
                    ),
                })
            elif value in bad_values:
                issues.append({
                    "variable": var_name,
                    "value": value or "(empty)",
                    "file": source_file.get(var_name, "unknown"),
                    "message": (
                        f"{var_name} is set to a known-insecure default ({value!r}). "
                        f"Set a strong, unique value before deploying."
                    ),
                })
            else:
                # Check insecure prefixes (e.g. django-insecure-*)
                for prefix in self._INSECURE_PREFIXES.get(var_name, []):
                    if value.startswith(prefix):
                        issues.append({
                            "variable": var_name,
                            "value": value[:40] + ("..." if len(value) > 40 else ""),
                            "file": source_file.get(var_name, "unknown"),
                            "message": (
                                f"{var_name} starts with insecure prefix {prefix!r}. "
                                f"Set a strong, unique value before deploying."
                            ),
                        })
                        break
        return issues

    # ── manifest & drift ─────────────────────────────────────────────

    def gather_source_manifest(
        self,
        branch_name: str,
        worktree_path: Path,
        comparison_root: Path,
    ) -> Dict[str, Any]:
        files = {
            "Dockerfile": worktree_path / "Dockerfile",
            ".ilha/docker-compose.worktree.yml": worktree_path / ".ilha" / "docker-compose.worktree.yml",
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
        env_contract_warnings: List[Dict[str, Any]],
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
        print_plain(f"  Smoke path: {smoke_path}")
        if drift_report.get("differences"):
            print_plain("  Drift detected:")
            for entry in drift_report["differences"]:
                print_plain(
                    f"    {entry['severity'].upper()}: {entry['file']} differs "
                    f"({entry['comparison_path']} vs {entry['source_path']})"
                )
        if drift_report.get("skipped") and drift_report.get("skip_reason") == "different_git_heads":
            print_plain("  Drift check skipped: comparison root and worktree are on different git commits")
        for warning in env_contract_warnings:
            print_plain(f"  Warning: {warning['message']}")
        print_plain("")

    # ── orchestrator ─────────────────────────────────────────────────

    def run_deploy_preflight(
        self,
        branch_name: str,
        strict_deploy_drift: bool = False,
        strict_env_contract: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Run all preflight checks and return context dict, or None on failure.

        The returned dict contains:
          - worktree_path, comparison_root, manifest, drift_report, smoke_path

        On failure, returns None and populates ``errors`` (a list of dicts with
        error_code / stage / root_cause / suggested_actions / failure_category).
        Callers are responsible for forwarding those into the deploy report.

        When ``strict_env_contract`` is True, env-contract issues (e.g.
        ``SITE_DOMAIN`` with an ``http://``/``https://`` prefix) block the
        deploy rather than just warning. This catches the CSRF-breakage class
        documented in STAGING_DROPLET_FIXES.md before a deploy can reach the
        server.
        """
        worktree_path = self.resolve_worktree_path(branch_name)
        if not worktree_path.exists():
            log_error(f"Worktree directory not found: {worktree_path}")
            return None

        comparison_root = self.resolve_comparison_root(worktree_path)
        manifest = self.gather_source_manifest(branch_name, worktree_path, comparison_root)
        drift_report = self.build_drift_report(
            worktree_path,
            comparison_root,
            strict_deploy_drift=strict_deploy_drift,
        )
        smoke_path = self._detect_smoke_path(worktree_path / ".ilha" / "docker-compose.worktree.yml", comparison_root)
        env_contract_warnings = self.detect_env_contract_issues(worktree_path)

        self._print_source_manifest(manifest, drift_report, smoke_path, env_contract_warnings)

        for entry in drift_report.get("differences", []):
            message = (
                f"Critical deploy file drift: {entry['file']} differs between "
                f"{entry['comparison_path']} and {entry['source_path']}"
            )
            if entry["severity"] == "warning":
                log_warning(message)
            else:
                log_error(message)

        for warning in env_contract_warnings:
            if strict_env_contract:
                log_error(warning["message"])
            else:
                log_warning(warning["message"])

        if drift_report.get("status") == "error":
            return None

        if strict_env_contract and env_contract_warnings:
            log_error(
                "Deploy blocked: env-contract issues detected. Fix the values above "
                "(e.g. set SITE_DOMAIN to a bare host, not a URL) or pass "
                "--no-strict-env-contract to bypass. Mis-formed SITE_DOMAIN "
                "breaks CSRF_TRUSTED_ORIGINS on the server."
            )
            return None

        # ── Insecure default credential check ─────────────────────────
        insecure_issues = self._check_insecure_defaults(worktree_path)
        if insecure_issues:
            allow = os.environ.get("ILHA_ALLOW_INSECURE_DEFAULTS", "").lower() in ("1", "true", "yes")
            for issue in insecure_issues:
                if allow:
                    log_warning(f"[insecure-defaults] {issue['message']} (allowed via ILHA_ALLOW_INSECURE_DEFAULTS)")
                else:
                    log_error(f"[insecure-defaults] {issue['message']}")
            if not allow:
                log_error(
                    "Deploy blocked: insecure default credentials detected. "
                    "Set strong values in .env or .ilha/env.ilha, or set "
                    "ILHA_ALLOW_INSECURE_DEFAULTS=1 to bypass (not recommended for production)."
                )
                return None

        return {
            "worktree_path": worktree_path,
            "comparison_root": comparison_root,
            "manifest": manifest,
            "drift_report": drift_report,
            "smoke_path": smoke_path,
            "env_contract_warnings": env_contract_warnings,
        }
