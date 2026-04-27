"""
Slim deploy manager for ilha.

Deploys a worktree to a remote server using rsync + remote docker compose,
without requiring Python, git, or ilha on the server.

Flow:
  1. Assemble a flat deploy directory locally (code + compose + env + caddy)
  2. Apply domain/IP overrides locally
  3. Backup Docker volumes locally (if any)
  4. rsync the deploy directory to the server
  5. Restore volumes on the server via SSH + docker
  6. Start Caddy and app services via SSH + docker compose
"""

import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
import fnmatch
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any, Set

from ...config.settings import get_script_dir, get_worktree_dir
from ...core.dns_manager import DNSManager, parse_domain, is_domain
from ...core.droplet_manager import DropletInfo
from ...core.environment_manager import EnvironmentManager
from ...utils.checksum import calculate_file_checksum
from ...utils.compose_external_volumes import (
    compose_web_image_reference,
    list_external_volume_host_names,
)
from ...utils.caddy_config import ensure_caddy_labels_and_network
from ...utils.env_loader import load_env_file
from ...utils.deploy_hosts import build_csrf_trusted_origins, normalize_deploy_host
from ...utils.logging import (
    log_info,
    log_success,
    log_warning,
    log_error,
    log_step,
    print_plain,
    is_verbose,
    StageTracker,
    render_deploy_report,
)
from ...utils.ssh_manager import SSHConnectionManager, SCPTarget
from ...utils.streaming import execute_with_streaming
from ...utils.validation import validate_branch_name
from .deploy_doctor import DeployDoctor
from .deploy_preflight import DeployPreflight
from .deploy_service_config import DeployServiceConfig, load_deploy_service_config
from .remote_compose_probe import remote_compose_service_known, remote_compose_service_logs_tail
from .remote_disk import RemoteDiskManager
from .transfer_manager import TransferManager


class SlimDeployManager:
    """Deploys a worktree via rsync + remote docker compose (no ilha on server)."""

    # Remote directory structure
    REMOTE_APP_ROOT = "/app"
    REMOTE_CADDY_DIR = "/app/caddy"
    REMOTE_VOLUMES_DIR = "/app/volumes"

    # Timeouts (seconds) for parallel futures
    _FUTURE_TIMEOUTS = {
        "dns": 300,
        "prepare": 600,
        "assemble": 1800,
        "build": 1800,
        "restore": 600,
        "caddy": 300,
    }

    # Upper bound for the remote start-app script (nohup poll). Steps without an
    # embedded `compose build web` are: bring up deps, wait for db,
    # `compose up -d` web/workers, health-check (which waits up to 180s for the
    # entrypoint's own migrations to finish) — normally a few minutes.
    _APP_START_MAX_WAIT_SEC = 600
    # When deploy passes build=True, the script also runs `docker compose build web` on the server.
    _APP_START_MAX_WAIT_EMBEDDED_BUILD_SEC = 1200

    # Upper bound on rsync wall-clock time. Prevents a stalled SSH connection
    # from hanging the deploy indefinitely (Popen.wait has no built-in timeout).
    _RSYNC_MAX_WAIT_SEC = 1800
    # Minimum bytes of HTML body for domain smoke (avoids empty Caddy-only 200s)
    _SMOKE_HTML_MIN_BYTES = 256

    # Exclude heavy/non-runtime paths from deploy context by default
    _DEFAULT_DEPLOY_IGNORE = [
        ".git",
        "__pycache__",
        "*.pyc",
        ".tox",
        "node_modules",
        ".venv",
        "venv",
        ".pytest_cache",
        "packages",
        "*.ilha-package.tar.gz",
        "test-results",
        "*.log",
    ]

    _ENOSPC_MARKERS = (
        "no space left on device",
        "failed to extract layer",
        "failed to solve: failed to extract",
    )

    _STAGE_DISPLAY_NAMES = {
        "preflight": "Preflight",
        "assemble_prepare_dns": "Server prep + assembly",
        "disk_check": "Disk check",
        "volume_backup": "Volume backup",
        "transfer": "Code sync",
        "remote_prepare": "Remote services",
        "app_start": "App start",
        "smoke_checks": "Smoke checks",
    }

    def __init__(self, project_root: Optional[Path] = None):
        if project_root is None:
            from ...config.settings import get_project_root
            self.project_root = get_project_root()
        else:
            self.project_root = Path(project_root).resolve()

        self.ssh = SSHConnectionManager()
        self.transfer = TransferManager()
        self.disk = RemoteDiskManager(self.ssh, self.REMOTE_APP_ROOT)
        self.doctor_mgr = DeployDoctor(self.ssh, self.REMOTE_APP_ROOT)
        self.preflight = DeployPreflight(self.project_root)
        self.services = load_deploy_service_config(self.project_root)
        self._session_id = uuid.uuid4().hex[:8]
        self._tracker: Optional[StageTracker] = None
        self._last_report: Dict[str, Any] = {
            "success": False,
            "stage_timings": {},
            "failure_category": None,
            "error": None,
            "disk_check": None,
            "source_manifest": None,
            "drift_report": None,
            "smoke_checks": None,
            "recovery_attempted": False,
            "recovery_succeeded": False,
            "notes": [],
            "volume_backup_path": None,
        }
        self._last_failure_category: Optional[str] = None

    def _remote_tmp(self, name):
        """Session-namespaced /tmp path for remote scripts/logs."""
        return f"/tmp/ilha-{name}-{self._session_id}"

    def get_last_deploy_report(self) -> Dict[str, Any]:
        """Return the latest deploy report for JSON output and diagnostics."""
        return dict(self._last_report)

    def _reset_report(self) -> None:
        self._last_failure_category = None
        self._last_report = {
            "success": False,
            "stage_timings": {},
            "failure_category": None,
            "error": None,
            "disk_check": None,
            "source_manifest": None,
            "drift_report": None,
            "smoke_checks": None,
            "recovery_attempted": False,
            "recovery_succeeded": False,
            "notes": [],
            "volume_backup_path": None,
        }

    def _mark_stage_timing(self, stage: str, started_at: float) -> None:
        elapsed = round(time.time() - started_at, 3)
        self._last_report["stage_timings"][stage] = elapsed
        if self._tracker:
            name = self._STAGE_DISPLAY_NAMES.get(stage, stage)
            self._tracker.record(name, elapsed)

    def _set_failure(
        self,
        *,
        error_code: str,
        stage: str,
        root_cause: str,
        suggested_actions: Optional[List[str]] = None,
        failure_category: Optional[str] = None,
    ) -> None:
        category = failure_category or error_code
        self._last_failure_category = category
        self._last_report["failure_category"] = category
        self._last_report["error"] = {
            "error_code": error_code,
            "stage": stage,
            "root_cause": root_cause,
            "suggested_actions": suggested_actions or [],
        }

    @classmethod
    def _is_enospc_line(cls, line: str) -> bool:
        lower = line.strip().lower()
        return any(marker in lower for marker in cls._ENOSPC_MARKERS)

    def _note(self, message: str) -> None:
        """Append a deploy report note and log in verbose mode."""
        notes = self._last_report.setdefault("notes", [])
        if isinstance(notes, list) and message not in notes:
            notes.append(message)
        log_info(message)

    def _ensure_remote_external_volumes(
        self, deploy_dir: Path, username: str, server: str
    ) -> bool:
        """Create declared ``external: true`` volumes on the remote host (idempotent)."""
        compose = deploy_dir / "docker-compose.yml"
        env_path = deploy_dir / ".env"
        if not compose.exists() or not env_path.exists():
            return True
        env = load_env_file(env_path)
        names = list_external_volume_host_names(compose, env)
        if not names:
            return True
        log_step(f"Ensuring {len(names)} external Docker volume(s) on server")
        self.ssh.ensure_host_key(server)
        for vol_name in names:
            vq = shlex.quote(vol_name)
            cmd = self.ssh.build_ssh_command(
                username, server, f"docker volume inspect {vq} >/dev/null 2>&1 || docker volume create {vq}"
            )
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            if result.returncode != 0:
                log_error(
                    f"Failed to ensure external volume {vol_name!r}: "
                    f"{(result.stderr or result.stdout or '').strip()}"
                )
                self._set_failure(
                    error_code="external_volume_create_failed",
                    stage="remote_prepare",
                    root_cause=f"Could not create external Docker volume {vol_name!r} on the server.",
                    suggested_actions=[
                        "SSH to the server and run: docker volume create <name>",
                        "Check Docker permissions and disk space.",
                    ],
                    failure_category="remote_setup_error",
                )
                return False
        log_success("External Docker volumes are present on server")
        return True

    def _remote_docker_image_exists(
        self, username: str, server: str, image: str
    ) -> bool:
        """Return True if *image* exists on the remote Docker host."""
        if not (image or "").strip():
            return True
        iq = shlex.quote(image.strip())
        cmd = self.ssh.build_ssh_command(
            username, server, f"docker image inspect {iq} >/dev/null 2>&1"
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        return result.returncode == 0

    def _ssh_reachable(self, username: str, server: str) -> bool:
        cmd = self.ssh.build_ssh_command(username, server, "true")
        cmd.insert(1, "-o")
        cmd.insert(2, "BatchMode=yes")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0

    def _validate_restored_postgres_password(
        self,
        username: str,
        server: str,
        *,
        force_password_reset: bool,
    ) -> bool:
        """Verify Postgres accepts POSTGRES_PASSWORD from ``/app/.env`` after volume restore."""
        if force_password_reset:
            self._note("Skipping Postgres password validation (--force-password-reset).")
            return True
        script = r"""set -euo pipefail
cd /app
set -a
[ -f .env ] && source .env
set +a
docker compose --env-file .env up -d db
for i in $(seq 1 90); do
  if docker compose --env-file .env exec -T db pg_isready -U "${POSTGRES_USER:-postgres}" -q 2>/dev/null; then
    break
  fi
  sleep 1
done
if ! docker compose --env-file .env exec -T db pg_isready -U "${POSTGRES_USER:-postgres}" -q 2>/dev/null; then
  echo "PG_NOT_READY" >&2
  exit 2
fi
if ! docker compose --env-file .env exec -T -e PGPASSWORD="${POSTGRES_PASSWORD:-}" db \
  psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" -h 127.0.0.1 -c "select 1" >/dev/null 2>&1; then
  echo "PG_AUTH_OR_CONN_FAILED" >&2
  exit 3
fi
echo OK
"""
        code, out, err = self._run_remote_text(username, server, script, timeout=180)
        if code == 0:
            log_success("Postgres password matches deployed .env after volume restore")
            return True
        log_error(
            "Postgres credential check failed after volume restore. "
            "Restored data may not match POSTGRES_PASSWORD in the merged .env."
        )
        if err.strip():
            log_error(f"  Remote stderr: {err.strip()[:800]}")
        self._set_failure(
            error_code="postgres_password_mismatch",
            stage="restore",
            root_cause=(
                "Restored Postgres volume does not accept POSTGRES_PASSWORD from the deployed .env."
            ),
            suggested_actions=[
                "Re-align the DB role password with .env on the server, or deploy without "
                "`--restore-volumes` so Postgres initializes fresh.",
                "If you intentionally reset DB passwords, re-run with --force-password-reset.",
            ],
            failure_category="remote_data_error",
        )
        return False

    @staticmethod
    def _safe_int(value: str, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _load_deployignore_patterns(
        self,
        worktree_path: Path,
        deployignore: Optional[str],
    ) -> List[str]:
        patterns: List[str] = list(self._DEFAULT_DEPLOY_IGNORE)
        ignore_file: Optional[Path] = None
        if deployignore:
            ignore_file = Path(deployignore)
        else:
            candidate = worktree_path / ".ilha" / "deployignore"
            if candidate.exists():
                ignore_file = candidate
        if ignore_file and ignore_file.exists():
            try:
                for raw in ignore_file.read_text().splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    patterns.append(line)
                log_info(f"Loaded deploy ignore patterns from {ignore_file}")
            except Exception as exc:
                log_warning(f"Failed to read deploy ignore file {ignore_file}: {exc}")
        return patterns

    @staticmethod
    def _pattern_matches(rel_path: str, name: str, pattern: str) -> bool:
        normalized = rel_path
        if normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.replace("\\", "/")
        normalized_name = name.replace("\\", "/")
        rule = pattern.strip().replace("\\", "/")
        if not rule:
            return False
        if rule.endswith("/"):
            prefix = rule.rstrip("/")
            return normalized == prefix or normalized.startswith(prefix + "/")
        if "/" in rule and not any(ch in rule for ch in "*?[]"):
            return normalized == rule or normalized.startswith(rule + "/")
        if "/" not in rule and not any(ch in rule for ch in "*?[]"):
            parts = normalized.split("/")
            if rule in parts:
                return True
        return fnmatch.fnmatch(normalized, rule) or fnmatch.fnmatch(normalized_name, rule)

    def _build_copy_ignore(
        self,
        worktree_path: Path,
        patterns: List[str],
        excluded: Set[str],
    ):
        def _ignore(current_dir: str, names: List[str]):
            ignored = set()
            current_path = Path(current_dir)
            try:
                rel_base = current_path.relative_to(worktree_path)
            except ValueError:
                rel_base = Path(".")
            for name in names:
                rel_path = (rel_base / name) if str(rel_base) != "." else Path(name)
                rel_posix = rel_path.as_posix()
                if any(self._pattern_matches(rel_posix, name, p) for p in patterns):
                    ignored.add(name)
                    excluded.add(rel_posix)
            return ignored
        return _ignore

    @staticmethod
    def _dir_size_bytes(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    def _run_local_text(
        self,
        command: List[str],
        timeout: int = 30,
    ) -> Tuple[int, str, str]:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            return result.returncode, result.stdout or "", result.stderr or ""
        except subprocess.TimeoutExpired:
            return 1, "", f"Local command timed out after {timeout}s"
        except Exception as exc:
            return 1, "", str(exc)

    def _run_deploy_preflight(
        self,
        branch_name: str,
        strict_deploy_drift: bool = False,
        strict_env_contract: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Delegate to DeployPreflight and wire results into the deploy report."""
        result = self.preflight.run_deploy_preflight(
            branch_name,
            strict_deploy_drift=strict_deploy_drift,
            strict_env_contract=strict_env_contract,
        )
        if result is None:
            # Determine failure reason and populate report even on failure
            worktree_path = self.preflight.resolve_worktree_path(branch_name)
            if not worktree_path.exists():
                self._set_failure(
                    error_code="worktree_not_found",
                    stage="preflight",
                    root_cause=f"Worktree directory not found: {worktree_path}",
                    suggested_actions=["Create the worktree or run the deploy from the correct repository."],
                    failure_category="validation_error",
                )
            else:
                # Gather manifest/drift for the report even though we're failing
                comparison_root = self.preflight.resolve_comparison_root(worktree_path)
                manifest = self.preflight.gather_source_manifest(branch_name, worktree_path, comparison_root)
                drift_report = self.preflight.build_drift_report(
                    worktree_path, comparison_root, strict_deploy_drift=strict_deploy_drift,
                )
                env_contract_warnings = self.preflight.detect_env_contract_issues(worktree_path)
                self._last_report["source_manifest"] = manifest
                self._last_report["drift_report"] = drift_report
                if strict_env_contract and env_contract_warnings:
                    self._set_failure(
                        error_code="env_contract_violation",
                        stage="preflight",
                        root_cause=(
                            "Env-contract issues detected (e.g. SITE_DOMAIN with "
                            "http(s):// prefix). Mis-formed values break "
                            "CSRF_TRUSTED_ORIGINS on the server."
                        ),
                        suggested_actions=[
                            "Fix the offending values in .env or .ilha/env.ilha "
                            "(bare host, not a URL).",
                            "Or re-run with --no-strict-env-contract to bypass.",
                        ],
                        failure_category="validation_error",
                    )
                else:
                    self._set_failure(
                        error_code="critical_file_drift",
                        stage="preflight",
                        root_cause=(
                            "Critical deploy files differ between root and worktree. "
                            "Deploy the worktree copy or sync the changes first."
                        ),
                        suggested_actions=[
                            "Deploy the worktree copy or sync the changes first.",
                        ],
                        failure_category="validation_error",
                    )
            return None

        self._last_report["source_manifest"] = result["manifest"]
        self._last_report["drift_report"] = result["drift_report"]
        return result

    def _probe_external_domain_html(self, domain: str) -> Tuple[bool, Optional[str]]:
        """GET ``https://<domain>/`` and require ``text/html`` plus a non-trivial body."""
        url = f"https://{domain}/"
        code, out, err = self._run_local_text(
            [
                "curl",
                "-kfsS",
                "--max-time",
                "15",
                "-D",
                "-",
                "-o",
                "-",
                url,
            ],
            timeout=25,
        )
        if code != 0:
            return False, (err or out or "curl failed").strip()[:800] or None
        sep = "\r\n\r\n"
        if sep not in out:
            sep = "\n\n"
        if sep not in out:
            return False, "malformed HTTP response (no header/body separator)"
        headers, body = out.split(sep, 1)
        lower = headers.lower()
        if "content-type:" not in lower or "text/html" not in lower:
            return False, f"expected Content-Type text/html; got headers excerpt: {headers[:400]!r}"
        if len(body.strip()) < self._SMOKE_HTML_MIN_BYTES:
            return False, (
                f"HTML body too small ({len(body)} bytes, minimum {self._SMOKE_HTML_MIN_BYTES})"
            )
        return True, None

    def _probe_http_redirects_to_https(self, domain: str) -> Tuple[bool, Optional[str]]:
        """Verify plain HTTP for a domain redirects to HTTPS."""
        url = f"http://{domain}/accounts/login/"
        code, out, err = self._run_local_text(
            [
                "curl",
                "-fsSI",
                "--max-time",
                "10",
                "--max-redirs",
                "0",
                url,
            ],
            timeout=15,
        )
        if code not in (0, 22):
            return False, (err or out or "curl failed").strip()[:800] or None

        status_line = next(
            (line.strip() for line in out.splitlines() if line.startswith("HTTP/")),
            "",
        )
        location = next(
            (
                line.split(":", 1)[1].strip()
                for line in out.splitlines()
                if line.lower().startswith("location:")
            ),
            "",
        )
        redirect_statuses = ("301", "302", "307", "308")
        if not any(f" {status} " in f"{status_line} " for status in redirect_statuses):
            return False, f"expected HTTP redirect to HTTPS, got {status_line or 'no HTTP status'}"
        expected_prefix = f"https://{domain}/"
        if not location.startswith(expected_prefix):
            return False, f"expected Location to start with {expected_prefix}, got {location!r}"
        return True, None

    def _run_smoke_checks(
        self,
        *,
        username: str,
        server: str,
        smoke_path: str,
        domain: Optional[str],
        ip: Optional[str],
        timeout_seconds: int = 60,
    ) -> Tuple[bool, Dict[str, Any]]:
        started_at = time.time()
        delays = [0, 1, 2, 4, 8, 15]
        checks: List[Dict[str, Any]] = []
        via_web = self._remote_service_exists(username, server, self.services.web_service)
        local_url = f"http://127.0.0.1:{self.services.web_port}{self._normalized_smoke_path(smoke_path)}"
        probe_target = "web_container" if via_web else "host_local"
        external_url = f"https://{domain}/" if domain else None

        for attempt, delay in enumerate(delays, start=1):
            if delay:
                time.sleep(delay)
            probe_cmd = self._build_smoke_probe_command(
                smoke_path, via_compose_web=via_web, quiet=True
            )
            local_code, _, local_err = self._run_remote_text(
                username,
                server,
                probe_cmd,
                timeout=10,
            )
            local_ok = local_code == 0
            local_result = {
                "name": "local_app",
                "target": local_url,
                "probe": probe_target,
                "attempt": attempt,
                "success": local_ok,
                "error": local_err.strip() or None,
            }

            external_result = None
            redirect_result = None
            external_html_result = None
            external_ok = True
            redirect_ok = True
            external_html_ok = True
            if local_ok and external_url:
                redirect_ok, redirect_err = self._probe_http_redirects_to_https(domain)
                redirect_result = {
                    "name": "external_http_to_https_redirect",
                    "target": f"http://{domain}/accounts/login/",
                    "attempt": attempt,
                    "success": redirect_ok,
                    "error": redirect_err,
                }
                external_code, _, external_err = self._run_local_text(
                    ["curl", "-kfsSI", "--max-time", "5", external_url],
                    timeout=10,
                )
                external_ok = external_code == 0
                external_result = {
                    "name": "external_domain_head",
                    "target": external_url,
                    "attempt": attempt,
                    "success": external_ok,
                    "error": external_err.strip() or None,
                }
                if external_ok and domain:
                    html_ok, html_err = self._probe_external_domain_html(domain)
                    external_html_ok = html_ok
                    external_html_result = {
                        "name": "external_domain_html",
                        "target": external_url,
                        "attempt": attempt,
                        "success": html_ok,
                        "error": html_err,
                    }

            checks = [local_result]
            if redirect_result is not None:
                checks.append(redirect_result)
            if external_result is not None:
                checks.append(external_result)
            if external_html_result is not None:
                checks.append(external_html_result)
            if local_ok and redirect_ok and external_ok and external_html_ok:
                report = {
                    "success": True,
                    "path": smoke_path,
                    "checks": checks,
                    "duration_seconds": round(time.time() - started_at, 3),
                }
                self._last_report["smoke_checks"] = report
                return True, report

            if time.time() - started_at >= timeout_seconds:
                break

        report: Dict[str, Any] = {
            "success": False,
            "path": smoke_path,
            "checks": checks,
            "duration_seconds": round(time.time() - started_at, 3),
        }
        if via_web:
            tail = remote_compose_service_logs_tail(
                self._run_remote_text,
                username=username,
                server=server,
                remote_app_root=self.REMOTE_APP_ROOT,
                service_name=self.services.web_service,
                lines=100,
                timeout=45,
            )
            if tail:
                report["web_logs_tail"] = tail[:12000]
                log_error("Smoke checks failed — recent web container logs:")
                for line in tail.splitlines()[-40:]:
                    print_plain(f"  {line}")
        self._last_report["smoke_checks"] = report
        return False, report

    def _remote_service_exists(self, username: str, server: str, service_name: str) -> bool:
        return remote_compose_service_known(
            self._run_remote_text,
            username=username,
            server=server,
            remote_app_root=self.REMOTE_APP_ROOT,
            service_name=service_name,
            timeout=15,
        )

    @staticmethod
    def _normalized_smoke_path(smoke_path: str) -> str:
        p = (smoke_path or "/").strip()
        if not p.startswith("/"):
            p = "/" + p
        return p or "/"

    def _build_smoke_probe_command(
        self,
        smoke_path: str,
        *,
        via_compose_web: bool,
        quiet: bool = True,
    ) -> str:
        """Shell command to verify the app responds on the configured web port.

        Caddy-only setups often do not publish the app port on the host; the
        listener lives inside the web container. When ``via_compose_web`` is
        true, run curl inside that service via ``docker compose exec``.
        """
        norm = self._normalized_smoke_path(smoke_path)
        url = f"http://127.0.0.1:{self.services.web_port}{norm}"
        tail = " >/dev/null" if quiet else " 2>&1"
        inner_curl = f"curl -fsS --max-time 5 {shlex.quote(url)}{tail}"
        if via_compose_web:
            root = shlex.quote(self.REMOTE_APP_ROOT)
            service = shlex.quote(self.services.web_service)
            return (
                f"cd {root} && docker compose --env-file .env exec -T {service} sh -c {shlex.quote(inner_curl)}"
            )
        return f"curl -fsS --max-time 5 {shlex.quote(url)}{tail}"

    def _doctor_collect_command(
        self,
        title: str,
        command: str,
        *,
        username: str,
        server: str,
        local: bool = False,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        if local:
            code, stdout, stderr = self._run_local_text(["zsh", "-lc", command], timeout=timeout)
        else:
            code, stdout, stderr = self._run_remote_text(username, server, command, timeout=timeout)
        return {
            "title": title,
            "command": command,
            "success": code == 0,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
        }

    def doctor(
        self,
        *,
        branch_name: str,
        scp_target: str,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        target = SCPTarget(scp_target)
        preflight = self._run_deploy_preflight(branch_name)
        smoke_path = preflight["smoke_path"] if preflight else "/"

        report = self.doctor_mgr.doctor(
            username=target.username,
            server=target.server,
            smoke_path=smoke_path,
            source_manifest=self._last_report.get("source_manifest"),
            drift_report=self._last_report.get("drift_report"),
            domain=domain,
        )
        report["branch"] = branch_name
        report["target"] = scp_target
        return report

    # ──────────────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────────────
    def deploy(
        self,
        branch_name: str,
        scp_target: str,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
        prepare_server: bool = True,
        dns_token: Optional[str] = None,
        skip_dns_check: bool = False,
        build: bool = False,
        debug: bool = False,
        droplet_info: Optional[DropletInfo] = None,
        use_staging_certificates: bool = False,
        disk_recovery: str = "safe",
        retry_on_disk_reclaim: bool = False,
        deployignore: Optional[str] = None,
        no_disk_check: bool = False,
        skip_volume_backup: bool = False,
        strict_deploy_drift: bool = False,
        strict_env_contract: bool = True,
        smoke_timeout: int = 60,
        expected_ssh_fingerprint: Optional[str] = None,
        restore_volumes: bool = False,
        no_build: bool = False,
        force_password_reset: bool = False,
    ) -> bool:
        """Run the full slim deploy pipeline.

        Returns True on success, False on failure.
        """
        self.ssh.set_expected_fingerprint(expected_ssh_fingerprint)
        self._reset_report()
        try:
            target = SCPTarget(scp_target)
        except ValueError:
            log_error(f"Invalid SCP target: {scp_target}  (expected user@host:path)")
            self._set_failure(
                error_code="invalid_scp_target",
                stage="preflight",
                root_cause=f"Invalid target: {scp_target}",
                suggested_actions=["Use format user@host:path."],
                failure_category="validation_error",
            )
            return False

        username = target.username
        server = target.server
        preflight = self._run_deploy_preflight(
            branch_name,
            strict_deploy_drift=strict_deploy_drift,
            strict_env_contract=strict_env_contract,
        )
        if preflight is None:
            render_deploy_report(self._last_report)
            return False

        log_info("Starting slim deploy (no ilha required on server)...")
        log_info(f"  Branch:  {branch_name}")
        log_info(f"  Server:  {username}@{server}")
        if domain:
            log_info(f"  Domain:  {domain}")
        elif ip:
            log_info(f"  IP:      {ip}")

        target_label = domain or ip or f"{username}@{server}"
        self._tracker = StageTracker(f"Deploying {branch_name} → {target_label}")

        with self._tracker:
            attempts = 2 if retry_on_disk_reclaim else 1
            for attempt in range(1, attempts + 1):
                if attempt > 1:
                    print_plain(f"Retrying slim deploy after disk recovery (attempt {attempt}/{attempts})...")
                success = self._deploy_once(
                    branch_name=branch_name,
                    username=username,
                    server=server,
                    domain=domain,
                    ip=ip,
                    prepare_server=prepare_server,
                    dns_token=dns_token,
                    skip_dns_check=skip_dns_check,
                    build=build,
                    debug=debug,
                    droplet_info=droplet_info,
                    use_staging_certificates=use_staging_certificates,
                    deployignore=deployignore,
                    no_disk_check=no_disk_check,
                    skip_volume_backup=skip_volume_backup,
                    smoke_path=preflight["smoke_path"],
                    smoke_timeout=smoke_timeout,
                    restore_volumes=restore_volumes,
                    no_build=no_build,
                    force_password_reset=force_password_reset,
                )
                if success:
                    self._last_report["success"] = True
                    break
                if (
                    self._last_failure_category != "disk_full"
                    or attempt >= attempts
                    or disk_recovery == "off"
                ):
                    break
                self._last_report["recovery_attempted"] = True
                self.disk.log_remote_disk_diagnostics(username, server)
                recovered = self.disk.attempt_remote_disk_recovery(username, server, disk_recovery)
                self._last_report["recovery_succeeded"] = recovered
                if not recovered:
                    break

        render_deploy_report(self._last_report)
        self._tracker = None
        return self._last_report["success"]

    def _deploy_once(
        self,
        *,
        branch_name: str,
        username: str,
        server: str,
        domain: Optional[str],
        ip: Optional[str],
        prepare_server: bool,
        dns_token: Optional[str],
        skip_dns_check: bool,
        build: bool,
        debug: bool,
        droplet_info: Optional[DropletInfo],
        use_staging_certificates: bool,
        deployignore: Optional[str],
        no_disk_check: bool,
        skip_volume_backup: bool,
        smoke_path: str,
        smoke_timeout: int = 60,
        restore_volumes: bool = False,
        no_build: bool = False,
        force_password_reset: bool = False,
    ) -> bool:
        deploy_dir: Optional[Path] = None
        try:
            if ip and not domain:
                print_plain(
                    "[ilha] Warning: deploy with --ip and no --domain configures Caddy for IP-only access; "
                    "HTTPS and hostname routing for your app domain will not be set up. "
                    "Pass --domain <fqdn> for domain routing and TLS."
                )
            stage_started = time.time()
            log_step("Preparing server, DNS, and assembly")
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {}
                if domain and not skip_dns_check:
                    futures["dns"] = executor.submit(
                        self._handle_dns, domain, server, dns_token, droplet_info
                    )
                if prepare_server:
                    futures["prepare"] = executor.submit(
                        self._prepare_server_slim, username, server
                    )
                futures["assemble"] = executor.submit(
                    self._assemble_deploy_dir,
                    branch_name,
                    domain=domain,
                    ip=ip,
                    debug=debug,
                    use_staging_certificates=use_staging_certificates,
                    deployignore=deployignore,
                )

                try:
                    dns_ok = futures["dns"].result(timeout=self._FUTURE_TIMEOUTS["dns"]) if "dns" in futures else True
                except TimeoutError:
                    log_error(f"DNS management timed out after {self._FUTURE_TIMEOUTS['dns']}s")
                    dns_ok = False
                try:
                    prepare_ok = futures["prepare"].result(timeout=self._FUTURE_TIMEOUTS["prepare"]) if "prepare" in futures else True
                except TimeoutError:
                    log_error(f"Server preparation timed out after {self._FUTURE_TIMEOUTS['prepare']}s")
                    prepare_ok = False
                try:
                    deploy_dir = futures["assemble"].result(timeout=self._FUTURE_TIMEOUTS["assemble"])
                except TimeoutError:
                    log_error(f"Deploy assembly timed out after {self._FUTURE_TIMEOUTS['assemble']}s")
                    deploy_dir = None
            self._mark_stage_timing("assemble_prepare_dns", stage_started)

            if "dns" in futures and not dns_ok:
                log_warning("DNS management failed, continuing anyway...")
            if prepare_server and not prepare_ok:
                self._set_failure(
                    error_code="server_prepare_failed",
                    stage="prepare",
                    root_cause="Slim server preparation failed.",
                    suggested_actions=["Re-run with --no-prepare if server is already configured."],
                    failure_category="remote_setup_error",
                )
                return False
            if deploy_dir is None:
                self._set_failure(
                    error_code="assemble_failed",
                    stage="assemble",
                    root_cause="Failed to assemble deploy directory.",
                    suggested_actions=["Check worktree paths and compose files."],
                    failure_category="local_build_error",
                )
                return False

            deploy_size = self._dir_size_bytes(deploy_dir)
            if not no_disk_check:
                log_step("Checking remote disk space")
                stage_started = time.time()
                disk_ok, disk_report = self.disk.check_remote_disk_capacity(
                    username=username,
                    server=server,
                    deploy_size_bytes=deploy_size,
                )
                self._last_report["disk_check"] = disk_report
                self._mark_stage_timing("disk_check", stage_started)
                if not disk_ok:
                    self._set_failure(
                        error_code="insufficient_remote_disk",
                        stage="disk_check",
                        root_cause="Insufficient disk space on remote server for deploy/build.",
                        suggested_actions=[
                            "Free disk space on /app and /var/lib/docker.",
                            "Re-run with --retry-on-disk-reclaim and --disk-recovery safe.",
                        ],
                        failure_category="disk_full",
                    )
                    self.disk.log_remote_disk_diagnostics(username, server)
                    self.disk.print_disk_recovery_guidance(username, server)
                    return False

            if not self._ensure_remote_external_volumes(deploy_dir, username, server):
                return False

            stage_started = time.time()
            volume_tar = None
            if restore_volumes and not skip_volume_backup:
                log_step("Backing up volumes")
                volume_tar = self._backup_volumes_locally(branch_name, deploy_dir)
                if volume_tar:
                    self._last_report["volume_backup_path"] = str(volume_tar)
            elif restore_volumes and skip_volume_backup:
                print_plain(
                    "[ilha] Warning: --restore-volumes with --skip-volume-backup skips local backup; "
                    "remote restore only runs if a backup archive already exists under /app/volumes/."
                )
            elif skip_volume_backup:
                log_info("Skipping local volume backup (--skip-volume-backup)")
            else:
                log_info(
                    "Skipping local volume backup (default for `droplet deploy`). "
                    "Pass --restore-volumes to copy worktree Docker volumes to the server."
                )
            self._mark_stage_timing("volume_backup", stage_started)

            log_step("Syncing code to server")
            stage_started = time.time()
            self.ssh.ensure_host_key(server)
            if not self._rsync_to_server(deploy_dir, username, server):
                self._set_failure(
                    error_code="rsync_failed",
                    stage="transfer",
                    root_cause="Failed to transfer deploy directory to remote server.",
                    suggested_actions=["Check SSH connectivity and remote permissions."],
                    failure_category="transfer_error",
                )
                return False
            self._mark_stage_timing("transfer", stage_started)

            compose_env = load_env_file(deploy_dir / ".env")
            web_image = compose_web_image_reference(
                deploy_dir / "docker-compose.yml",
                compose_env,
                self.services.web_service,
            )
            image_exists = (
                self._remote_docker_image_exists(username, server, web_image)
                if web_image
                else True
            )
            if web_image and not image_exists and no_build:
                self._set_failure(
                    error_code="missing_remote_image",
                    stage="transfer",
                    root_cause=(
                        f"Docker image {web_image!r} is not present on the server and "
                        "`--no-build` was set."
                    ),
                    suggested_actions=[
                        "Run a deploy with `--build`, or remove `--no-build` to allow an automatic first-time build.",
                        "Or build/push the image to the server registry before deploying.",
                    ],
                    failure_category="remote_setup_error",
                )
                return False
            auto_build_missing = bool(web_image) and not image_exists and not no_build and not build
            if auto_build_missing:
                msg = (
                    f"Remote image {web_image!r} not found; will run `docker compose build` "
                    "before starting the app."
                )
                self._note(msg)

            stage_started = time.time()
            restore_ok = True
            build_ok = True

            # Phase 1: restore volumes + start caddy (in parallel)
            log_step("Starting remote services")
            with ThreadPoolExecutor(max_workers=2) as executor:
                phase1_futures = {}
                if volume_tar:
                    phase1_futures["restore"] = executor.submit(
                        self._restore_volumes_remote, username, server, branch_name
                    )
                phase1_futures["caddy"] = executor.submit(
                    self._start_caddy_remote, username, server
                )
                for key in phase1_futures:
                    try:
                        ok = phase1_futures[key].result(timeout=self._FUTURE_TIMEOUTS.get(key, 600))
                    except TimeoutError:
                        log_error(f"{key} timed out after {self._FUTURE_TIMEOUTS.get(key, 600)}s")
                        ok = False
                    if key == "restore":
                        restore_ok = ok
                    elif key == "caddy" and not ok:
                        log_warning("Caddy start had issues, but continuing with app services...")

            if not restore_ok:
                self._set_failure(
                    error_code="volume_restore_failed",
                    stage="restore",
                    root_cause="Remote volume restore failed.",
                    suggested_actions=["Check remote volume backup file and docker volume permissions."],
                    failure_category="remote_data_error",
                )
                return False

            if volume_tar and not self._validate_restored_postgres_password(
                username, server, force_password_reset=force_password_reset
            ):
                return False

            should_compose_build = restore_ok and (bool(build) or auto_build_missing)
            if should_compose_build:
                log_step("Running Docker build")
                try:
                    build_ok = self._build_remote(username, server, debug)
                except Exception as exc:
                    log_error(f"Docker build failed: {exc}")
                    build_ok = False
                if not build_ok:
                    self._set_failure(
                        error_code="compose_build_failed",
                        stage="remote_prepare",
                        root_cause="Remote `docker compose build` did not complete successfully.",
                        suggested_actions=[
                            "Inspect the remote build log under /tmp/ilha-*.log",
                            "Free disk space if ENOSPC appears, then retry.",
                        ],
                        failure_category=self._last_failure_category or "remote_setup_error",
                    )
                    return False

            self._mark_stage_timing("remote_prepare", stage_started)

            log_step("Starting application")
            stage_started = time.time()
            if not self._start_app_remote(username, server, build=False, debug=debug):
                if self._last_failure_category != "disk_full":
                    self._set_failure(
                        error_code="app_start_failed",
                        stage="start_app",
                        root_cause="Failed to start application services on the server.",
                        suggested_actions=[
                            "Inspect remote logs under /tmp/ilha-*.log and docker compose ps output.",
                            "Check /tmp/ilha-diagnostics/ on the server for captured logs from the failed step.",
                        ],
                        failure_category="remote_runtime_error",
                    )
                return False
            self._mark_stage_timing("app_start", stage_started)

            log_step("Running smoke checks")
            stage_started = time.time()
            smoke_ok, smoke_report = self._run_smoke_checks(
                username=username,
                server=server,
                smoke_path=smoke_path,
                domain=domain,
                ip=ip,
                timeout_seconds=smoke_timeout,
            )
            self._mark_stage_timing("smoke_checks", stage_started)
            if not smoke_ok:
                self._set_failure(
                    error_code="smoke_checks_failed",
                    stage="smoke_checks",
                    root_cause="Post-deploy smoke checks did not pass before timeout.",
                    suggested_actions=[
                        "Run `ilha droplet doctor` for this target.",
                        "Inspect remote compose ps output and service logs.",
                    ],
                    failure_category="remote_runtime_error",
                )
                for check in smoke_report.get("checks", []):
                    if not check.get("success"):
                        probe = check.get("probe")
                        via = f" [{probe}]" if probe else ""
                        log_error(
                            f"Smoke check failed: {check['name']}{via} -> {check['target']} "
                            f"({check.get('error') or 'unknown error'})"
                        )
                return False

            url = None
            if domain:
                url = f"https://{domain}"
            elif ip:
                url = f"http://{ip}"
            elif droplet_info and droplet_info.ip_address:
                url = f"http://{droplet_info.ip_address}"
            if url:
                self._last_report["url"] = url

            log_success("Slim deploy completed successfully!")
            return True
        finally:
            if deploy_dir is not None:
                try:
                    shutil.rmtree(deploy_dir, ignore_errors=True)
                except Exception:
                    pass

    def put(
        self,
        branch_name: str,
        scp_target: str,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
        build: bool = False,
        debug: bool = False,
        use_staging_certificates: bool = False,
        code_only: bool = False,
        db_only: bool = False,
        smoke_timeout: int = 60,
    ) -> bool:
        """Update an existing slim-deployed server.

        Default: backup volumes, sync code + config + volumes, restore volumes, restart Caddy + app.
        code_only: sync code + config only, restart Caddy + app (no volume operations).
        db_only: backup volumes, sync only volumes to server, stop app, restore volumes, start app.
        """
        try:
            self._reset_report()
            target = SCPTarget(scp_target)
        except ValueError:
            log_error(f"Invalid SCP target: {scp_target}  (expected user@host:path)")
            self._set_failure(
                error_code="invalid_scp_target",
                stage="preflight",
                root_cause=f"Invalid target: {scp_target}",
                suggested_actions=["Use format user@host:path."],
                failure_category="validation_error",
            )
            return False

        username = target.username
        server = target.server
        preflight = None
        if not db_only:
            preflight = self._run_deploy_preflight(branch_name)
            if preflight is None:
                return False

        log_info("Starting slim put...")
        log_info(f"  Branch:  {branch_name}")
        log_info(f"  Server:  {username}@{server}")
        if domain:
            log_info(f"  Domain:  {domain}")
        elif ip:
            log_info(f"  IP:      {ip}")
        if code_only:
            log_info("  Mode:    code only (no volumes)")
        elif db_only:
            log_info("  Mode:    db/volumes only (no code sync)")

        mode = " (code-only)" if code_only else (" (db-only)" if db_only else "")
        target_label = domain or ip or f"{username}@{server}"
        self._tracker = StageTracker(f"Updating {branch_name} → {target_label}{mode}")

        def _cleanup_deploy_dir(d: Optional[Path]) -> None:
            if d is not None:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass

        # ── db_only: volumes only ─────────────────────────────────────
        if db_only:
            deploy_dir = Path(tempfile.mkdtemp(prefix=f"ilha-put-volumes-{branch_name}-"))
            with self._tracker:
                try:
                    (deploy_dir / "volumes").mkdir()
                    log_step("Backing up volumes")
                    volume_tar = self._backup_volumes_locally(branch_name, deploy_dir)
                    if not volume_tar:
                        log_warning("No volumes to sync; nothing to do for --db-only")
                        _cleanup_deploy_dir(deploy_dir)
                        return True
                    log_step("Syncing volumes to server")
                    self.ssh.ensure_host_key(server)
                    if not self._rsync_volumes_to_server(deploy_dir, username, server):
                        _cleanup_deploy_dir(deploy_dir)
                        return False
                    log_step("Stopping application")
                    if not self._stop_app_remote(username, server):
                        log_warning("App stop had issues, but continuing with volume restore...")
                    log_step("Restoring volumes")
                    if not self._restore_volumes_remote(username, server, branch_name):
                        _cleanup_deploy_dir(deploy_dir)
                        return False
                    log_step("Starting application")
                    if not self._start_app_remote(username, server, build=False, debug=False):
                        _cleanup_deploy_dir(deploy_dir)
                        log_error("Failed to start application services on the server.")
                        self._set_failure(
                            error_code="app_start_failed",
                            stage="start_app",
                            root_cause="Failed to start application services on the server.",
                            suggested_actions=["Inspect remote compose ps output and service logs."],
                            failure_category="remote_runtime_error",
                        )
                        return False
                    self._last_report["success"] = True
                    log_success("Slim put (db-only) completed successfully!")
                    return True
                finally:
                    _cleanup_deploy_dir(deploy_dir)

        # ── code_only: code + config, no volumes ───────────────────────
        if code_only:
          with self._tracker:
            log_step("Assembling deploy directory")
            deploy_dir = self._assemble_deploy_dir(
                branch_name,
                domain=domain,
                ip=ip,
                debug=debug,
                use_staging_certificates=use_staging_certificates,
            )
            if deploy_dir is None:
                render_deploy_report(self._last_report)
                return False
            log_step("Syncing code to server")
            self.ssh.ensure_host_key(server)
            if not self._rsync_to_server(deploy_dir, username, server):
                _cleanup_deploy_dir(deploy_dir)
                render_deploy_report(self._last_report)
                return False
            log_step("Starting remote services")
            if not self._start_caddy_remote(username, server):
                log_warning("Caddy start had issues, but continuing with app services...")
            log_step("Starting application")
            if not self._start_app_remote(username, server, build=build, debug=debug):
                _cleanup_deploy_dir(deploy_dir)
                log_error("Failed to start application services on the server.")
                self._set_failure(
                    error_code="app_start_failed",
                    stage="start_app",
                    root_cause="Failed to start application services on the server.",
                    suggested_actions=["Inspect remote compose ps output and service logs."],
                    failure_category="remote_runtime_error",
                )
                render_deploy_report(self._last_report)
                return False
            log_step("Running smoke checks")
            smoke_ok, smoke_report = self._run_smoke_checks(
                username=username,
                server=server,
                smoke_path=(preflight or {}).get("smoke_path", "/"),
                domain=domain,
                ip=ip,
                timeout_seconds=smoke_timeout,
            )
            if not smoke_ok:
                _cleanup_deploy_dir(deploy_dir)
                self._set_failure(
                    error_code="smoke_checks_failed",
                    stage="smoke_checks",
                    root_cause="Post-deploy smoke checks did not pass before timeout.",
                    suggested_actions=[
                        "Run `ilha droplet doctor` for this target.",
                        "Inspect remote compose ps output and service logs.",
                    ],
                    failure_category="remote_runtime_error",
                )
                render_deploy_report(self._last_report)
                return False
            url = f"https://{domain}" if domain else (f"http://{ip}" if ip else None)
            if url:
                self._last_report["url"] = url
            self._last_report["success"] = True
            log_success("Slim put (code-only) completed successfully!")
            render_deploy_report(self._last_report)
            _cleanup_deploy_dir(deploy_dir)
            self._tracker = None
            return True

        # ── Default: full sync (volumes + code + config) ────────────────
        with self._tracker:
            log_step("Assembling deploy directory")
            deploy_dir = self._assemble_deploy_dir(
                branch_name,
                domain=domain,
                ip=ip,
                debug=debug,
                use_staging_certificates=use_staging_certificates,
            )
            if deploy_dir is None:
                render_deploy_report(self._last_report)
                return False
            log_step("Backing up volumes")
            volume_tar = self._backup_volumes_locally(branch_name, deploy_dir)
            log_step("Syncing to server")
            self.ssh.ensure_host_key(server)
            if not self._rsync_to_server(deploy_dir, username, server):
                _cleanup_deploy_dir(deploy_dir)
                render_deploy_report(self._last_report)
                return False
            log_step("Starting remote services")
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {}
                if volume_tar:
                    futures["restore"] = executor.submit(
                        self._restore_volumes_remote, username, server, branch_name
                    )
                futures["caddy"] = executor.submit(self._start_caddy_remote, username, server)
                for key in futures:
                    try:
                        ok = futures[key].result(timeout=self._FUTURE_TIMEOUTS.get(key, 600))
                    except TimeoutError:
                        log_error(f"{key} timed out after {self._FUTURE_TIMEOUTS.get(key, 600)}s")
                        ok = False
                    if key == "caddy" and not ok:
                        log_warning("Caddy start had issues, but continuing with app services...")
            log_step("Starting application")
            if not self._start_app_remote(username, server, build=build, debug=debug):
                _cleanup_deploy_dir(deploy_dir)
                log_error("Failed to start application services on the server.")
                self._set_failure(
                    error_code="app_start_failed",
                    stage="start_app",
                    root_cause="Failed to start application services on the server.",
                    suggested_actions=["Inspect remote compose ps output and service logs."],
                    failure_category="remote_runtime_error",
                )
                render_deploy_report(self._last_report)
                return False
            log_step("Running smoke checks")
            smoke_ok, smoke_report = self._run_smoke_checks(
                username=username,
                server=server,
                smoke_path=(preflight or {}).get("smoke_path", "/"),
                domain=domain,
                ip=ip,
                timeout_seconds=smoke_timeout,
            )
            if not smoke_ok:
                _cleanup_deploy_dir(deploy_dir)
                self._set_failure(
                    error_code="smoke_checks_failed",
                    stage="smoke_checks",
                    root_cause="Post-deploy smoke checks did not pass before timeout.",
                    suggested_actions=[
                        "Run `ilha droplet doctor` for this target.",
                        "Inspect remote compose ps output and service logs.",
                    ],
                    failure_category="remote_runtime_error",
                )
                render_deploy_report(self._last_report)
                return False
            url = f"https://{domain}" if domain else (f"http://{ip}" if ip else None)
            if url:
                self._last_report["url"] = url
            self._last_report["success"] = True
            log_success("Slim put completed successfully!")
            render_deploy_report(self._last_report)
            _cleanup_deploy_dir(deploy_dir)
            self._tracker = None
            return True

    # ──────────────────────────────────────────────────────────────────
    # Internal: assemble deploy directory
    # ──────────────────────────────────────────────────────────────────
    def _assemble_deploy_dir(
        self,
        branch_name: str,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
        debug: bool = False,
        use_staging_certificates: bool = False,
        deployignore: Optional[str] = None,
    ) -> Optional[Path]:
        """Build a flat deploy directory ready to rsync.

        Layout:
          deploy_dir/
          ├── code/              # worktree source code
          ├── caddy/
          │   ├── docker-compose.yml   # global caddy compose
          │   ├── Caddyfile
          │   ├── caddy-docker-monitor.py
          │   └── caddy-dynamic-config.py  (if exists)
          ├── docker-compose.yml # worktree compose (renamed from worktree yml)
          ├── .env               # merged env with overrides applied
          └── volumes/           # (populated later if needed)
        """
        log_info("Assembling deploy directory...")
        if not validate_branch_name(branch_name):
            log_error(f"Invalid branch name: {branch_name!r}")
            return None
        deploy_dir = Path(tempfile.mkdtemp(prefix=f"ilha-deploy-{branch_name}-"))

        try:
            # Locate worktree using config (respects fractal worktree_dir setting)
            worktree_path = self.preflight.resolve_worktree_path(branch_name)
            if worktree_path == self.project_root.resolve():
                log_info("Using current worktree as deploy source")
            if not worktree_path.exists():
                log_error(f"Worktree directory not found: {worktree_path}")
                return None

            worktree_ilha = worktree_path / ".ilha"
            worktree_compose = worktree_ilha / "docker-compose.worktree.yml"
            if not worktree_compose.exists():
                log_error(f"Worktree compose file not found: {worktree_compose}")
                return None

            # ── code/ ────────────────────────────────────────────────
            code_dir = deploy_dir / "code"
            log_info(f"Copying worktree code from {worktree_path}...")
            ignore_patterns = self._load_deployignore_patterns(worktree_path, deployignore)
            excluded_paths: Set[str] = set()
            shutil.copytree(
                worktree_path, code_dir,
                ignore=self._build_copy_ignore(worktree_path, ignore_patterns, excluded_paths),
            )
            if excluded_paths:
                important = sorted(
                    p for p in excluded_paths
                    if p.startswith("packages") or p.endswith(".ilha-package.tar.gz")
                )
                if important:
                    preview = ", ".join(important[:3])
                    print_plain(
                        f"Excluded deploy artifacts from build context ({len(excluded_paths)} paths). "
                        f"Examples: {preview}"
                    )
            log_success(f"Code copied ({_dir_size_mb(code_dir):.1f} MB)")

            # ── docker-compose.yml (app) ─────────────────────────────
            shutil.copy2(worktree_compose, deploy_dir / "docker-compose.yml")
            log_info("Copied worktree compose file")

            # Apply domain/IP to Caddy labels in compose
            if domain or ip:
                self._apply_compose_overrides(
                    deploy_dir / "docker-compose.yml", domain=domain, ip=ip
                )

            # ── .env (merged) ────────────────────────────────────────
            self._build_merged_env(
                deploy_dir, worktree_path, branch_name,
                domain=domain, ip=ip, debug=debug,
                use_staging_certificates=use_staging_certificates,
            )

            # ── caddy/ ──────────────────────────────────────────────
            caddy_dir = deploy_dir / "caddy"
            caddy_dir.mkdir()
            self._assemble_caddy_files(caddy_dir, deploy_dir / ".env")

            # ── volumes/ (placeholder) ───────────────────────────────
            (deploy_dir / "volumes").mkdir()

            log_success(f"Deploy directory assembled: {deploy_dir}")
            return deploy_dir

        except Exception as exc:
            log_error(f"Failed to assemble deploy directory: {exc}")
            import traceback
            log_error(traceback.format_exc())
            shutil.rmtree(deploy_dir, ignore_errors=True)
            return None

    # ──────────────────────────────────────────────────────────────────
    # Internal: compose overrides (Caddy labels + network)
    # ──────────────────────────────────────────────────────────────────
    def _apply_compose_overrides(
        self, compose_path: Path,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> None:
        """Update Caddy labels and network config in the compose file."""
        try:
            import yaml

            data = yaml.safe_load(compose_path.read_text())
            if not data:
                return
            normalized_domain = normalize_deploy_host(domain) if domain else None
            normalized_ip = normalize_deploy_host(ip) if ip else None
            ensure_caddy_labels_and_network(data, domain=normalized_domain, ip=normalized_ip)
            # Production deploy: ensure web service restarts on exit (e.g. OOM 137)
            if normalized_domain or normalized_ip:
                services = data.get("services") or {}
                web_config = services.get("web")
                if web_config is not None and "restart" not in web_config:
                    web_config["restart"] = "unless-stopped"

            # Strip elevated container privileges from services that don't need them.
            # Only services matching _PRIVILEGED_SERVICE_PATTERNS (Playwright/browser
            # services) keep SYS_ADMIN, seccomp:unconfined, ipc:host, etc.
            self._strip_unnecessary_privileges(data)

            compose_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
            log_info(f"Applied {'domain' if normalized_domain else 'IP'} override to compose labels")
        except ImportError:
            log_warning("PyYAML not available — skipping compose label override")
        except Exception as exc:
            log_warning(f"Could not update compose labels: {exc}")

    # Services matching these patterns keep elevated privileges (Playwright/Chromium).
    _PRIVILEGED_SERVICE_PATTERNS = ("web", "rq-worker")

    # Keys removed from non-privileged services during deploy assembly.
    _PRIVILEGE_KEYS = ("security_opt", "cap_add", "ipc", "shm_size")

    def _strip_unnecessary_privileges(self, compose_data: dict) -> None:
        """Remove elevated Docker privileges from services that don't need them.

        Services whose name starts with one of ``_PRIVILEGED_SERVICE_PATTERNS``
        (e.g. ``web``, ``rq-worker-1``) keep their privileges because they run
        Playwright/Chromium.  All other services (``db``, ``redis``, ``caddy``,
        etc.) have ``security_opt``, ``cap_add``, ``ipc``, and ``shm_size``
        stripped for defense in depth.
        """
        services = compose_data.get("services")
        if not services:
            return
        for svc_name, svc_config in services.items():
            if not isinstance(svc_config, dict):
                continue
            if any(svc_name.startswith(prefix) for prefix in self._PRIVILEGED_SERVICE_PATTERNS):
                continue
            removed = []
            for key in self._PRIVILEGE_KEYS:
                if key in svc_config:
                    del svc_config[key]
                    removed.append(key)
            if removed:
                log_info(f"Stripped privileges from service '{svc_name}': {', '.join(removed)}")

    # ──────────────────────────────────────────────────────────────────
    # Internal: merged .env
    # ──────────────────────────────────────────────────────────────────
    def _build_merged_env(
        self,
        deploy_dir: Path,
        worktree_path: Path,
        branch_name: str,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
        debug: bool = False,
        use_staging_certificates: bool = False,
    ) -> None:
        """Build a single .env file by merging project .env + worktree env.ilha,
        then applying domain/IP overrides.

        Critical for fractal compatibility:
        - Sets PROJECT_ROOT to the server-side code path so that
          ``${PROJECT_ROOT}:/app`` bind mounts and ``${PROJECT_ROOT}/.ilha/env.ilha``
          env_file references in the worktree compose file resolve correctly.
        - Also writes the same overrides into the code copy's env.ilha, because
          the compose file loads ``${PROJECT_ROOT}/.ilha/env.ilha`` *after*
          ``${PROJECT_ROOT}/.env``, and the env.ilha values would otherwise
          overwrite the production overrides with localhost defaults.
        """
        from ...utils.env_loader import load_env_file
        from ...utils.path_utils import get_env_compose_file_path

        REMOTE_CODE_DIR = f"{self.REMOTE_APP_ROOT}/code"

        merged: dict = {}

        # Layer 1: project root .env
        root_env = self.project_root / ".env"
        if root_env.exists():
            merged.update(load_env_file(root_env))

        # Layer 2: worktree .ilha/env.ilha (overrides root .env)
        env_ilha = get_env_compose_file_path(worktree_path)
        if env_ilha.exists():
            merged.update(load_env_file(env_ilha))

        # Layer 3: deployment overrides
        # PROJECT_ROOT must point to the code directory on the server so that
        # ${PROJECT_ROOT}:/app bind mounts and ${PROJECT_ROOT}/.env env_file
        # references in docker-compose.worktree.yml resolve correctly.
        merged["PROJECT_ROOT"] = REMOTE_CODE_DIR

        from ...config.settings import build_allowed_hosts_with_container

        if domain:
            from ...core.dns_manager import get_base_domain
            normalized_domain = normalize_deploy_host(domain)
            base_domain = get_base_domain(normalized_domain)
            additional_hosts = [normalized_domain]
            if base_domain:
                additional_hosts.append(f"*.{base_domain}")
            # Bare hostname only — Django/allauth often build URLs from SITE_DOMAIN;
            # a scheme here produces invalid CSRF/origin values (e.g. https://https://...).
            merged["SITE_DOMAIN"] = normalized_domain
            merged["ALLOWED_HOSTS"] = build_allowed_hosts_with_container(branch_name, additional_hosts)
            merged["USE_X_FORWARDED_HOST"] = "True"
            merged["SECURE_PROXY_SSL_HEADER"] = "HTTP_X_FORWARDED_PROTO,https"
            # Django settings split CSRF_TRUSTED_ORIGINS by comma, not space
            merged["CSRF_TRUSTED_ORIGINS"] = build_csrf_trusted_origins(
                normalized_domain,
                base_domain=base_domain,
            )
            # Vite allowedHosts
            vite_allowed_hosts = [normalized_domain]
            if base_domain:
                vite_allowed_hosts.append(f"*.{base_domain}")
            vite_allowed_hosts.extend(["localhost", "127.0.0.1"])
            merged["VITE_ALLOWED_HOSTS"] = ",".join(vite_allowed_hosts)
            if "CADDY_EMAIL" not in merged:
                merged["CADDY_EMAIL"] = f"admin@{normalized_domain}"
        elif ip:
            normalized_ip = normalize_deploy_host(ip)
            merged["SITE_DOMAIN"] = normalized_ip
            merged["ALLOWED_HOSTS"] = build_allowed_hosts_with_container(branch_name, [normalized_ip])
            merged["USE_X_FORWARDED_HOST"] = "True"
            # Comma-separated for Django's split(',')
            merged["CSRF_TRUSTED_ORIGINS"] = build_csrf_trusted_origins(
                normalized_ip,
                include_https=False,
                include_wildcard_https=False,
            )

        merged["DEBUG"] = "True" if debug else "False"
        merged["BUILD_MODE"] = "prod"

        if use_staging_certificates:
            merged["USE_STAGING_CERTIFICATES"] = "1"

        # Fallback: worktree compose often expects DATABASE_URL; synthesize from POSTGRES_* if missing
        if "DATABASE_URL" not in merged or not merged["DATABASE_URL"]:
            user = merged.get("POSTGRES_USER", "biuser")
            password = merged.get("POSTGRES_PASSWORD", "bipassword")
            db = merged.get("POSTGRES_DB", "business_intelligence")
            merged["DATABASE_URL"] = f"postgres://{user}:{password}@db:5432/{db}"
            if password == "bipassword":
                log_warning(
                    "DATABASE_URL synthesized with default password 'bipassword'. "
                    "Set POSTGRES_PASSWORD in your .env for production."
                )

        # ── Write top-level .env (used by ``docker compose --env-file`` and sourced by scripts) ──
        # Quote values so shell `source .env` does not treat space-separated parts as commands
        def _env_line(key: str, value: str) -> str:
            safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
            return f'{key}="{safe}"'

        env_path = deploy_dir / ".env"
        lines = [_env_line(k, v) for k, v in merged.items()]
        env_path.write_text("\n".join(lines) + "\n")
        log_info(f"Merged .env written ({len(merged)} vars)")

        # ── Also write .env inside code/ (referenced by compose env_file) ──
        code_env = deploy_dir / "code" / ".env"
        code_env.write_text("\n".join(lines) + "\n")

        # ── Update the env.ilha inside code/.ilha/ (loaded *second* by compose,
        #    so it must not revert production overrides to localhost defaults) ──
        code_env_ilha = deploy_dir / "code" / ".ilha" / "env.ilha"
        if code_env_ilha.exists():
            code_env_ilha.write_text("\n".join(lines) + "\n")
            log_info("Updated code/.ilha/env.ilha with production overrides")

    # ──────────────────────────────────────────────────────────────────
    # Internal: Caddy files
    # ──────────────────────────────────────────────────────────────────
    def _assemble_caddy_files(self, caddy_dir: Path, env_path: Path) -> None:
        """Copy Caddy configuration files and generate a resolved compose file."""
        script_dir = get_script_dir()
        config_dir = script_dir / "config"

        # Caddyfile
        caddyfile_src = config_dir / "Caddyfile.ilha"
        if caddyfile_src.exists():
            shutil.copy2(caddyfile_src, caddy_dir / "Caddyfile")

        # Monitor script
        monitor_src = script_dir / "scripts" / "caddy-docker-monitor.py"
        if monitor_src.exists():
            shutil.copy2(monitor_src, caddy_dir / "caddy-docker-monitor.py")

        # Dynamic config script (optional)
        dynamic_src = script_dir / "scripts" / "caddy-dynamic-config.py"
        if dynamic_src.exists():
            shutil.copy2(dynamic_src, caddy_dir / "caddy-dynamic-config.py")

        helper_src = script_dir / "scripts" / "caddy_admin_utils.py"
        if helper_src.exists():
            shutil.copy2(helper_src, caddy_dir / "caddy_admin_utils.py")

        # Build a concrete caddy compose (resolve path placeholders)
        caddy_compose_template = config_dir / "docker-compose.global-caddy.yml"
        if caddy_compose_template.exists():
            content = caddy_compose_template.read_text()
            # Replace path placeholders with absolute server-side paths
            content = content.replace(
                "{CADDYFILE_PATH}",
                f"{self.REMOTE_CADDY_DIR}/Caddyfile",
            )
            content = content.replace(
                "{MONITOR_SCRIPT_PATH}",
                f"{self.REMOTE_CADDY_DIR}/caddy-docker-monitor.py",
            )
            content = content.replace(
                "{HELPER_UTILS_PATH}",
                f"{self.REMOTE_CADDY_DIR}/caddy_admin_utils.py",
            )
            (caddy_dir / "docker-compose.yml").write_text(content)
            log_info("Caddy compose file assembled with server-side paths")

    # ──────────────────────────────────────────────────────────────────
    # Internal: volume backup / restore
    # ──────────────────────────────────────────────────────────────────
    def _backup_volumes_locally(self, branch_name: str, deploy_dir: Path) -> Optional[Path]:
        """Backup worktree Docker volumes into deploy_dir/volumes/.

        Returns path to the tar file, or None if no volumes backed up.
        """
        from ...core.docker_manager import DockerManager

        docker_mgr = DockerManager(project_root=self.project_root)
        env_mgr = EnvironmentManager(project_root=self.project_root)
        volume_names = env_mgr.get_worktree_volume_names(branch_name)

        if not volume_names:
            log_info("No volumes to backup")
            return None

        volumes_dir = deploy_dir / "volumes"
        volumes_dir.mkdir(exist_ok=True)
        tar_path = volumes_dir / f"backup_{branch_name}.tar"

        log_info(f"Backing up {len(volume_names)} volume(s)...")
        try:
            backup_path = docker_mgr.backup_volumes(branch_name, volumes_dir)
            if backup_path is not None and backup_path.exists():
                size_mb = backup_path.stat().st_size / 1024 / 1024
                log_success(f"Volumes backed up ({size_mb:.1f} MB)")
                return backup_path
            else:
                log_warning("Volume backup returned no data")
                return None
        except Exception as exc:
            log_warning(f"Volume backup failed: {exc}")
            return None

    def _restore_volumes_remote(self, username: str, server: str, branch_name: str) -> bool:
        """Restore volumes on the remote server using docker run + alpine."""
        if not validate_branch_name(branch_name):
            log_error(f"Invalid branch name for volume restore: {branch_name!r}")
            return False
        log_info("Restoring volumes on remote server...")

        # Backup format: gzipped tar (tar czf) containing volume_name.tar members.
        # Each member is an uncompressed tar of that volume's contents (single
        # compression pass on the outer archive to avoid redundant gzip layers).
        # Also supports legacy .tar.gz members for backwards compatibility.
        script = f"""#!/bin/bash
set -euo pipefail
BACKUP_TAR="{self.REMOTE_VOLUMES_DIR}/backup_{branch_name}.tar"
if [ ! -f "$BACKUP_TAR" ]; then
    echo "No volume backup found at $BACKUP_TAR, skipping restore"
    exit 0
fi
echo "Restoring volumes from $BACKUP_TAR..."

# Stop application containers before restoring volumes to prevent data corruption.
# Running containers (e.g. Postgres) crash if their data volume is overwritten underneath them.
cd {self.REMOTE_APP_ROOT}/code 2>/dev/null && docker compose --env-file ../.env down 2>/dev/null || true

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
tar xzf "$BACKUP_TAR" -C "$TMPDIR"
for f in "$TMPDIR"/*.tar "$TMPDIR"/*.tar.gz; do
    [ -f "$f" ] || continue
    base=$(basename "$f")
    vol="${{base%.tar.gz}}"
    vol="${{vol%.tar}}"
    echo "  Restoring volume: $vol"
    docker volume create "$vol" >/dev/null 2>&1 || true
    docker run --rm -v "$vol":/data -v "$TMPDIR":/backup alpine \\
        sh -c "rm -rf /data/* /data/..?* /data/.[!.]* 2>/dev/null; tar xf /backup/$base -C /data"
done
echo "Volume restore complete"
"""
        cmd = self.ssh.build_ssh_command(username, server, f"bash -s")
        try:
            result = subprocess.run(
                cmd, input=script, capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                log_success("Volumes restored on server")
                for line in result.stdout.splitlines():
                    if line.strip():
                        log_info(f"  {line}")
                return True
            else:
                log_warning(f"Volume restore returned exit code {result.returncode}")
                if result.stderr:
                    for line in result.stderr.splitlines():
                        log_warning(f"  {line}")
                return False
        except Exception as exc:
            log_warning(f"Volume restore failed: {exc}")
            return False

    # ──────────────────────────────────────────────────────────────────
    # Internal: server prep (slim)
    # ──────────────────────────────────────────────────────────────────
    def _prepare_server_slim(self, username: str, server: str) -> bool:
        """Run the slim server preparation script (Docker-only) via Python."""
        log_info("Running slim server preparation (Docker + firewall only)...")
        self.ssh.ensure_host_key(server)

        script_dir = get_script_dir() / "scripts"
        server_prep_path = script_dir / "server_prep.py"
        deploy_utils_path = script_dir / "deploy_utils.py"

        if not server_prep_path.exists():
            log_error(f"Slim prep script not found: {server_prep_path}")
            return False
        if not deploy_utils_path.exists():
            log_error(f"Deploy utilities not found: {deploy_utils_path}")
            return False

        # Upload both files to remote /tmp directory
        remote_tmp_dir = f"/tmp/ilha-{self._session_id}"
        remote_prep_path = f"{remote_tmp_dir}/server_prep.py"
        remote_utils_path = f"{remote_tmp_dir}/deploy_utils.py"

        # Create remote tmp directory
        mkdir_cmd = self.ssh.build_ssh_command(
            username, server, f"mkdir -p {remote_tmp_dir}"
        )
        result = subprocess.run(mkdir_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log_error(f"Failed to create remote temp directory: {result.stderr}")
            return False

        # Upload deploy_utils.py first (it's a dependency)
        utils_content = deploy_utils_path.read_text()
        upload_utils_cmd = self.ssh.build_ssh_command(
            username, server, f"cat > {shlex.quote(remote_utils_path)}"
        )
        result = subprocess.run(
            upload_utils_cmd, input=utils_content, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log_error(f"Failed to upload deploy_utils.py: {result.stderr}")
            return False

        # Upload server_prep.py
        prep_content = server_prep_path.read_text()
        upload_prep_cmd = self.ssh.build_ssh_command(
            username, server, f"cat > {shlex.quote(remote_prep_path)}"
        )
        result = subprocess.run(
            upload_prep_cmd, input=prep_content, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log_error(f"Failed to upload server_prep.py: {result.stderr}")
            return False

        # Execute server_prep.py with PYTHONPATH set to find deploy_utils
        exec_cmd = f"cd {remote_tmp_dir} && PYTHONPATH={remote_tmp_dir} python3 {shlex.quote(remote_prep_path)}"
        ssh_cmd = self.ssh.build_ssh_command(username, server, exec_cmd)

        success, stdout_lines, stderr_lines = execute_with_streaming(
            ssh_cmd, timeout=None, progress_interval=15, prefix="  "
        )

        if not success:
            log_error("Slim server preparation failed")
            if stderr_lines:
                for line in stderr_lines:
                    log_error(f"  {line}")
            return False

        log_success("Slim server preparation complete")
        return True

    def _run_remote_text(
        self,
        username: str,
        server: str,
        command: str,
        timeout: int = 30,
    ) -> Tuple[int, str, str]:
        ssh_cmd = self.ssh.build_ssh_command(username, server, command)
        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode, result.stdout or "", result.stderr or ""
        except subprocess.TimeoutExpired:
            return 1, "", f"Remote command timed out after {timeout}s"
        except Exception as exc:
            return 1, "", str(exc)

    # ──────────────────────────────────────────────────────────────────
    # Internal: rsync
    # ──────────────────────────────────────────────────────────────────
    def _rsync_streaming_once(
        self, cmd: List[str]
    ) -> Tuple[bool, int, List[str]]:
        """Run rsync with progress streaming. Returns (timed_out, returncode, out_lines)."""
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        out_lines: List[str] = []
        deadline = time.time() + self._RSYNC_MAX_WAIT_SEC
        timed_out = False
        try:
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                if time.time() > deadline:
                    timed_out = True
                    break
                line = line.rstrip()
                out_lines.append(line)
                if "%" in line or "speedup" in line.lower():
                    log_info(f"  {line}")
        finally:
            if timed_out or time.time() > deadline:
                timed_out = True
                process.kill()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
            else:
                try:
                    process.wait(timeout=max(1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.kill()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
        return timed_out, int(process.returncode or 0), out_lines

    def _rsync_to_server(self, deploy_dir: Path, username: str, server: str) -> bool:
        """rsync the deploy directory to the server."""
        log_info(f"Transferring deploy directory to {server}:{self.REMOTE_APP_ROOT}/ ...")

        # Ensure remote directory exists
        mkdir_cmd = self.ssh.build_ssh_command(
            username, server, f"mkdir -p {self.REMOTE_APP_ROOT}"
        )
        mkdir_result = subprocess.run(mkdir_cmd, capture_output=True, text=True, timeout=45)
        if mkdir_result.returncode != 0:
            log_error(f"Failed to create remote directory {self.REMOTE_APP_ROOT}: {mkdir_result.stderr.strip()}")
            return False

        cmd = [
            "rsync",
            "-avz",
            "--progress",
            "--delete-delay",
            "--partial",
            "--inplace",
            "-e", self.ssh.get_rsync_ssh_command(username, server),
            f"{deploy_dir}/",
            f"{username}@{server}:{self.REMOTE_APP_ROOT}/",
        ]
        if is_verbose():
            log_info(f"Running rsync ({_dir_size_mb(deploy_dir):.1f} MB)...")
        else:
            log_info("Running rsync...")

        for attempt in (1, 2):
            if attempt > 1:
                log_info("Retrying rsync after exit 255 (unexpected end of file)...")
            timed_out, code, out_lines = self._rsync_streaming_once(cmd)
            if timed_out:
                log_error(f"rsync exceeded {self._RSYNC_MAX_WAIT_SEC}s deadline — killed")
                for line in out_lines[-20:]:
                    if line.strip():
                        log_error(f"  {line}")
                return False
            if code == 0:
                log_success("Transfer complete")
                return True
            if code == 255 and attempt == 1 and self._ssh_reachable(username, server):
                log_warning("rsync exited 255; SSH still reachable — retrying once.")
                continue
            log_error(f"rsync failed (exit {code})")
            for line in out_lines[-20:]:
                if line.strip() and ("%" not in line and "speedup" not in line.lower()):
                    log_error(f"  {line}")
            return False
        return False

    def _rsync_volumes_to_server(self, deploy_dir: Path, username: str, server: str) -> bool:
        """Rsync only deploy_dir/volumes/ to REMOTE_VOLUMES_DIR on the server."""
        volumes_src = deploy_dir / "volumes"
        if not volumes_src.exists():
            log_warning("No volumes directory to sync")
            return True
        log_info(f"Transferring volumes to {server}:{self.REMOTE_VOLUMES_DIR}/ ...")
        mkdir_cmd = self.ssh.build_ssh_command(
            username, server, f"mkdir -p {self.REMOTE_VOLUMES_DIR}"
        )
        subprocess.run(mkdir_cmd, capture_output=True, timeout=45)
        # No -z: volume archives are already gzip-compressed
        cmd = [
            "rsync", "-av", "--progress",
            "-e", self.ssh.get_rsync_ssh_command(username, server),
            f"{volumes_src}/",
            f"{username}@{server}:{self.REMOTE_VOLUMES_DIR}/",
        ]
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if process.returncode != 0:
            log_error(f"rsync volumes failed (exit {process.returncode})")
            if process.stderr:
                for line in process.stderr.splitlines():
                    if line.strip():
                        log_warning(f"  {line}")
            return False
        log_success("Volumes transfer complete")
        return True

    # ──────────────────────────────────────────────────────────────────
    # Internal: remote start commands
    # ──────────────────────────────────────────────────────────────────
    def _stop_app_remote(self, username: str, server: str) -> bool:
        """Stop application services on the server so volumes can be safely restored."""
        log_info("Stopping application services on server...")
        self.ssh.ensure_host_key(server)
        script = f"""#!/bin/bash
set -euo pipefail
cd {self.REMOTE_APP_ROOT}
docker compose --env-file .env down --remove-orphans 2>&1 || true
echo "App services stopped"
"""
        tmp_script = self._remote_tmp("stop-app.sh")
        q = shlex.quote(tmp_script)
        exec_cmd = f"cat > {q} && chmod +x {q} && {q} && rm -f {q}"
        ssh_cmd = self.ssh.build_ssh_command(username, server, exec_cmd)
        success, _, stderr_lines = execute_with_streaming(
            ssh_cmd, script=script, timeout=120, progress_interval=15, prefix="  "
        )
        if success:
            log_success("App services stopped on server")
            return True
        log_warning("App stop had issues")
        for line in (stderr_lines or [])[-10:]:
            if line.strip():
                log_warning(f"  {line}")
        return False

    def _start_caddy_remote(self, username: str, server: str) -> bool:
        """Start global Caddy proxy on the server."""
        log_info("Starting Caddy proxy on server...")
        self.ssh.ensure_host_key(server)
        script = f"""#!/bin/bash
set -euo pipefail
cd {self.REMOTE_CADDY_DIR}

# Pass env vars from app .env for CADDY_EMAIL, USE_STAGING_CERTIFICATES
set -a
[ -f {self.REMOTE_APP_ROOT}/.env ] && source {self.REMOTE_APP_ROOT}/.env
set +a

docker compose -p ilha-proxy up -d --force-recreate 2>&1
echo "Caddy proxy started"
"""
        tmp_script = self._remote_tmp("start-caddy.sh")
        q = shlex.quote(tmp_script)
        exec_cmd = f"cat > {q} && chmod +x {q} && {q} && rm -f {q}"
        ssh_cmd = self.ssh.build_ssh_command(username, server, exec_cmd)
        success, stdout_lines, stderr_lines = execute_with_streaming(
            ssh_cmd, script=script, timeout=120, progress_interval=15, prefix="  "
        )
        if success:
            log_success("Caddy proxy started on server")
            return True
        else:
            log_warning("Caddy start failed")
            for line in stderr_lines[-20:]:
                if line.strip():
                    log_warning(f"  {line}")
            return False

    def _run_nohup_remote(
        self,
        username: str,
        server: str,
        script: str,
        script_path: str,
        log_file: str,
        label: str,
        success_markers: List[str],
        max_wait: int = 1800,
        debug: bool = False,
    ) -> bool:
        """Upload script, launch via nohup, poll until done. Two-step pattern avoids stdin+nohup failure."""
        stage_name = "build" if "build" in label.lower() else "start_app"
        # Step 1: upload script (synchronous, no nohup — reliable)
        upload_cmd = self.ssh.build_ssh_command(
            username, server,
            f"cat > {shlex.quote(script_path)} && chmod +x {shlex.quote(script_path)}",
        )
        if debug:
            log_info(f"  [debug] {label} upload SSH: {' '.join(upload_cmd)}")
        try:
            up = subprocess.run(
                upload_cmd,
                input=script,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            log_error(f"Failed to upload {label} script (SSH timeout)")
            return False
        if up.returncode != 0:
            log_error(f"Failed to upload {label} script: {up.stderr}")
            return False
        # Step 2: launch via nohup (no stdin)
        launch_cmd = self.ssh.build_ssh_command(
            username, server,
            f"nohup bash -c {shlex.quote(script_path + '; echo EXIT_CODE=$?')} >> {shlex.quote(log_file)} 2>&1 & echo $!",
        )
        if debug:
            log_info(f"  [debug] {label} launch SSH: {' '.join(launch_cmd)}")
        try:
            launch = subprocess.run(
                launch_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            log_error(f"Failed to start remote {label} (SSH timeout)")
            return False
        if launch.returncode != 0:
            log_error(f"Failed to start remote {label}: {launch.stderr}")
            return False
        pid = launch.stdout.strip()
        if not pid.isdigit():
            log_error(f"Failed to get PID from remote command: {launch.stdout}")
            return False
        log_info(f"{label} running on server (PID {pid}), log: {log_file}")
        log_info("Monitoring progress (this may take several minutes)...")
        # Step 3: poll
        poll_interval = 5
        elapsed = 0
        last_line_count = 0
        grep_parts = " || ".join(
            f"grep -q {shlex.quote(m)} {shlex.quote(log_file)}"
            for m in success_markers
        )
        success_check = f"({grep_parts}) && echo SUCCESS || echo FAILED"
        is_build = "Build" in label
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval
            # pid validated as digit above
            check_cmd = self.ssh.build_ssh_command(
                username, server,
                f"ps -p {pid} > /dev/null 2>&1 && echo RUNNING || echo FINISHED",
            )
            check_result = subprocess.run(
                check_cmd, capture_output=True, text=True, timeout=10,
            )
            process_status = (check_result.stdout or "").strip()
            wc_cmd = self.ssh.build_ssh_command(
                username, server,
                f"wc -l {shlex.quote(log_file)} 2>/dev/null | awk '{{print $1}}' || echo 0",
            )
            wc_result = subprocess.run(wc_cmd, capture_output=True, text=True, timeout=10)
            try:
                current_line_count = int((wc_result.stdout or "0").strip().split()[0])
            except (ValueError, IndexError):
                current_line_count = 0
            if current_line_count > last_line_count:
                lines_to_show = current_line_count - last_line_count
                tail_cmd = self.ssh.build_ssh_command(
                    username, server, f"tail -n {lines_to_show} {shlex.quote(log_file)}",
                )
                tail_result = subprocess.run(tail_cmd, capture_output=True, text=True, timeout=10)
                disk_full_detected = False
                for line in (tail_result.stdout or "").splitlines():
                    s = line.strip()
                    if s == "EXIT_CODE=0":
                        log_info(f"  {label} script completed successfully.")
                    elif s.startswith("EXIT_CODE="):
                        log_info(f"  {label} script exited with code {s.split('=', 1)[1]}.")
                    elif s:
                        log_info(f"  {line}")
                    if self._is_enospc_line(s):
                        disk_full_detected = True
                last_line_count = current_line_count
                if disk_full_detected:
                    self._set_failure(
                        error_code="disk_full",
                        stage=stage_name,
                        root_cause="Remote build/runtime failed due to disk exhaustion (ENOSPC).",
                        suggested_actions=[
                            "Free space on /app and /var/lib/docker.",
                            "Prune old images/build cache and retry.",
                        ],
                        failure_category="disk_full",
                    )
                    self.disk.log_remote_disk_diagnostics(username, server)
                    self.disk.print_disk_recovery_guidance(username, server)
                    kill_cmd = self.ssh.build_ssh_command(username, server, f"kill -9 {pid} >/dev/null 2>&1 || true")
                    subprocess.run(kill_cmd, capture_output=True, timeout=10)
                    return False
            if process_status == "FINISHED":
                log_info(f"{label} process finished")
                success_cmd = self.ssh.build_ssh_command(username, server, success_check)
                success_result = subprocess.run(
                    success_cmd, capture_output=True, text=True, timeout=10,
                )
                if (success_result.stdout or "").strip() == "SUCCESS":
                    if is_build:
                        log_success("Docker images built on server")
                    return True
                if is_build:
                    log_warning(f"{label} failed - check log for details")
                else:
                    log_error(f"{label} failed - check log for details")
                # One more scan for ENOSPC markers after process exit.
                scan_cmd = self.ssh.build_ssh_command(
                    username,
                    server,
                    f"grep -Ei 'no space left on device|failed to extract layer|failed to solve: failed to extract' {shlex.quote(log_file)} >/dev/null && echo ENOSPC || echo OTHER",
                )
                scan_result = subprocess.run(scan_cmd, capture_output=True, text=True, timeout=10)
                if (scan_result.stdout or "").strip() == "ENOSPC":
                    self._set_failure(
                        error_code="disk_full",
                        stage=stage_name,
                        root_cause="Remote build/runtime failed due to disk exhaustion (ENOSPC).",
                        suggested_actions=[
                            "Free space on /app and /var/lib/docker.",
                            "Prune old images/build cache and retry.",
                        ],
                        failure_category="disk_full",
                    )
                    self.disk.log_remote_disk_diagnostics(username, server)
                    self.disk.print_disk_recovery_guidance(username, server)
                self._log_tail_and_return_false(username, server, log_file, label)
                return False
            if elapsed % 60 == 0:
                log_info(f"Still running... ({elapsed}s elapsed)")
        if is_build:
            log_warning(f"{label} timed out after {max_wait}s")
        else:
            log_error(f"{label} timed out after {max_wait}s")
        log_error(f"Check progress: ssh {username}@{server} 'tail -f {log_file}'")
        self._log_tail_and_return_false(username, server, log_file, label)
        return False

    def _build_remote(self, username: str, server: str, debug: bool = False) -> bool:
        """Build Docker images on the server via nohup + poll (avoids SSH timeout on long builds)."""
        log_info("Building Docker images on server (this may take a while)...")
        self.ssh.ensure_host_key(server)
        script = f"""#!/bin/bash
set -euo pipefail
cd {self.REMOTE_APP_ROOT}

# Load env vars
set -a
[ -f .env ] && source .env
set +a

docker compose --env-file .env build 2>&1
echo "Build complete"
"""
        return self._run_nohup_remote(
            username, server, script,
            script_path=self._remote_tmp("build.sh"),
            log_file=self._remote_tmp("build.log"),
            label="Build",
            success_markers=["Build complete", "EXIT_CODE=0"],
            max_wait=1800,
            debug=debug,
        )

    def _compose_remote_deploy_wrapper_script(
        self,
        *,
        app_root: str,
        remote_tmp_dir: str,
        remote_deploy_file: str,
        build: bool,
    ) -> str:
        """Shell wrapper that exports service config and runs ``remote_deploy.py`` on the server."""
        web_service = self.services.web_service
        web_port = self.services.web_port
        health_path = self.services.health_path
        db_service = self.services.db_service
        deps_args = " ".join(shlex.quote(s) for s in self.services.dependency_services)
        worker_args = " ".join(shlex.quote(s) for s in self.services.worker_services)
        return "\n".join(
            [
                "#!/bin/bash",
                "set -euo pipefail",
                f"export APP_ROOT={shlex.quote(app_root)}",
                f"export WEB_SERVICE={shlex.quote(web_service)}",
                f"export WEB_PORT={shlex.quote(str(web_port))}",
                f"export HEALTH_PATH={shlex.quote(health_path)}",
                f"export DB_SERVICE={shlex.quote(db_service)}",
                f"export DEPS_SERVICES={shlex.quote(deps_args)}",
                f"export WORKER_SERVICES={shlex.quote(worker_args)}",
                f"export BUILD_WEB={'1' if build else '0'}",
                f"export PYTHONPATH={remote_tmp_dir}:${{PYTHONPATH:-}}",
                f"cd {remote_tmp_dir}",
                f"python3 {shlex.quote(remote_deploy_file)}",
            ]
        )

    def _start_app_remote(self, username: str, server: str, build: bool = False, debug: bool = False) -> bool:
        """Start application services on the server via nohup + poll.

        Strategy (brief downtime accepted; no canary/swap):
        1. Bring up dependency services and wait for the database.
        2. (Optional) Build the web image.
        3. ``compose up -d`` for web + workers. The container's entrypoint
           is responsible for running migrations before serving traffic.
        4. Health-check the running web container.
        """
        log_info("Starting application services on server...")
        self.ssh.ensure_host_key(server)
        app_root = self.REMOTE_APP_ROOT
        start_max_wait = (
            self._APP_START_MAX_WAIT_EMBEDDED_BUILD_SEC
            if build
            else self._APP_START_MAX_WAIT_SEC
        )
        log_info(
            f"App start watchdog: {start_max_wait}s "
            f"({'includes embedded compose build web' if build else 'no embedded image build'})"
        )
        # Upload remote_deploy.py and deploy_utils.py to remote /tmp
        script_dir = get_script_dir() / "scripts"
        remote_deploy_path = script_dir / "remote_deploy.py"
        deploy_utils_path = script_dir / "deploy_utils.py"

        if not remote_deploy_path.exists():
            log_error(f"Remote deploy script not found: {remote_deploy_path}")
            return False
        if not deploy_utils_path.exists():
            log_error(f"Deploy utilities not found: {deploy_utils_path}")
            return False

        remote_tmp_dir = f"/tmp/ilha-{self._session_id}"
        remote_deploy_file = f"{remote_tmp_dir}/remote_deploy.py"
        remote_utils_file = f"{remote_tmp_dir}/deploy_utils.py"

        # Create remote tmp directory
        mkdir_cmd = self.ssh.build_ssh_command(
            username, server, f"mkdir -p {remote_tmp_dir}"
        )
        result = subprocess.run(mkdir_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log_error(f"Failed to create remote temp directory: {result.stderr}")
            return False

        # Upload deploy_utils.py first (it's a dependency)
        utils_content = deploy_utils_path.read_text()
        upload_utils_cmd = self.ssh.build_ssh_command(
            username, server, f"cat > {shlex.quote(remote_utils_file)}"
        )
        result = subprocess.run(
            upload_utils_cmd, input=utils_content, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log_error(f"Failed to upload deploy_utils.py: {result.stderr}")
            return False

        # Upload remote_deploy.py
        deploy_content = remote_deploy_path.read_text()
        upload_deploy_cmd = self.ssh.build_ssh_command(
            username, server, f"cat > {shlex.quote(remote_deploy_file)}"
        )
        result = subprocess.run(
            upload_deploy_cmd, input=deploy_content, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log_error(f"Failed to upload remote_deploy.py: {result.stderr}")
            return False

        wrapper_script = self._compose_remote_deploy_wrapper_script(
            app_root=app_root,
            remote_tmp_dir=remote_tmp_dir,
            remote_deploy_file=remote_deploy_file,
            build=build,
        )

        log_file = self._remote_tmp("start-app.log")
        ok = self._run_nohup_remote(
            username, server, wrapper_script,
            script_path=self._remote_tmp("start-app.sh"),
            log_file=log_file,
            label="App start",
            success_markers=["EXIT_CODE=0"],
            max_wait=start_max_wait,
            debug=debug,
        )
        if not ok:
            return False
        # Verify web service container exists and is running
        web_service = self.services.web_service
        web_cid_cmd = self.ssh.build_ssh_command(
            username, server,
            f"cd {app_root} && docker compose --env-file .env ps -q {shlex.quote(web_service)} 2>/dev/null | head -1",
        )
        web_cid_result = subprocess.run(web_cid_cmd, capture_output=True, text=True, timeout=10)
        web_cid = (web_cid_result.stdout or "").strip()
        if not web_cid:
            log_error(f"{web_service} service container not found after start")
            self._log_tail_and_return_false(username, server, log_file, "App start")
            return False
        running_cmd = self.ssh.build_ssh_command(
            username, server,
            f"docker inspect --format '{{{{.State.Running}}}}' {shlex.quote(web_cid)} 2>/dev/null",
        )
        running_result = subprocess.run(running_cmd, capture_output=True, text=True, timeout=10)
        if (running_result.stdout or "").strip().lower() != "true":
            log_error(f"{web_service} container is not running after start")
            logs_cmd = self.ssh.build_ssh_command(
                username, server,
                f"docker logs --tail 20 {shlex.quote(web_cid)} 2>&1",
            )
            logs_result = subprocess.run(logs_cmd, capture_output=True, text=True, timeout=10)
            for line in (logs_result.stdout or "").splitlines():
                if line.strip():
                    log_error(f"  {line}")
            self._log_tail_and_return_false(username, server, log_file, "App start")
            return False
        log_success("Application services started on server")
        return True

    def _log_tail_and_return_false(
        self, username: str, server: str, log_file: str, label: str
    ) -> None:
        """Log last 30 lines of remote log file (DRY for app start / build failure paths)."""
        final_cmd = self.ssh.build_ssh_command(username, server, f"tail -n 30 {shlex.quote(log_file)}")
        final_result = subprocess.run(final_cmd, capture_output=True, text=True, timeout=10)
        for line in (final_result.stdout or "").splitlines():
            s = line.strip()
            if s == "EXIT_CODE=0":
                log_error(f"  {label} script completed successfully (see log for details).")
            elif s.startswith("EXIT_CODE="):
                log_error(f"  {label} script exited with code {s.split('=', 1)[1]}.")
            elif s:
                log_error(f"  {line}")

    # ──────────────────────────────────────────────────────────────────
    # Internal: DNS
    # ──────────────────────────────────────────────────────────────────
    def _handle_dns(
        self, domain: str, server: str,
        dns_token: Optional[str] = None,
        droplet_info: Optional[DropletInfo] = None,
    ) -> bool:
        """Create / update DNS record for the domain."""
        try:
            if not is_domain(domain):
                log_error(f"Invalid domain: {domain}")
                return False

            subdomain, base_domain = parse_domain(domain)
            api_token = DNSManager.resolve_dns_token(dns_token)
            if not api_token:
                log_error("DNS API token not found")
                return False

            provider = DNSManager.create_provider("digitalocean", api_token)
            if not provider:
                return False

            # Resolve target IP
            import re, socket
            target_ip = server
            if droplet_info and droplet_info.ip_address:
                target_ip = droplet_info.ip_address
            elif not re.match(r"^\d+\.\d+\.\d+\.\d+$", server):
                try:
                    target_ip = socket.gethostbyname(server)
                except socket.gaierror:
                    pass

            exists, current_ip = provider.check_domain_exists(subdomain, base_domain)
            if exists and current_ip == target_ip:
                log_success(f"DNS record already correct: {domain} -> {target_ip}")
                return True
            elif exists:
                log_info(f"Updating DNS: {domain} -> {target_ip}")
                return provider.update_subdomain(subdomain, base_domain, target_ip)
            else:
                log_info(f"Creating DNS: {domain} -> {target_ip}")
                return provider.create_subdomain(subdomain, base_domain, target_ip)
        except Exception as exc:
            log_error(f"DNS management error: {exc}")
            return False

    # ──────────────────────────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────────────────────────
    def cleanup(self):
        """Release SSH connections."""
        self.ssh.cleanup()
        self.transfer.cleanup()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _dir_size_mb(path: Path) -> float:
    """Rough size of a directory in megabytes."""
    return SlimDeployManager._dir_size_bytes(path) / 1024 / 1024
