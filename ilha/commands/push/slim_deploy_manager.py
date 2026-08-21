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

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
import fnmatch
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional, List, Tuple, Dict, Any, Set

from ...config.settings import get_script_dir, get_worktree_dir
from ...exceptions import VolumeBackupError, ilhaCommandError
from ...core.dns_manager import DNSManager, parse_domain, is_domain
from ...core.droplet_manager import DropletInfo
from ...core.environment_manager import EnvironmentManager
from ...commands.image import ImageManager
from ...commands.secrets import SecretsManager
from ...commands.droplet_remote import (
    RemoteAppTarget,
    compose_ps_health_command,
    dump_remote_postgres,
    read_remote_text_file,
    remote_compose_exec_command,
    set_remote_hostname,
)
from .remote_layout import (
    REMOTE_APP_ROOT as _REMOTE_APP_ROOT,
    REMOTE_CADDY_DIR as _REMOTE_CADDY_DIR,
    REMOTE_DEPLOY_IDENTITY,
    REMOTE_VOLUMES_DIR as _REMOTE_VOLUMES_DIR,
)
from .deploy_identity import build_deploy_identity, parse_deploy_identity, write_deploy_identity
from ...utils.compose_external_volumes import (
    compose_service_is_buildable,
    compose_web_image_reference,
    list_external_volume_host_names,
)
from ...utils.compose_yaml import compose_flags_str
from ...utils.caddy_config import ensure_caddy_labels_and_network
from ...utils.env_loader import load_env_file, merge_env_layers
from ...utils.path_utils import get_env_compose_file_path
from ...utils.published_images import pin_deploy_compose_files
from ...utils.registry_login import docker_login_remote, resolve_registry_read_token
from ...utils.secret_policy import CONTROL_PLANE_ENV_KEYS
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
from .deploy_preflight import DeployPreflight
from .deploy_service_config import DeployServiceConfig, load_deploy_service_config
from ...scripts.http_probe import build_probe_shell_chain, normalize_path as _normalize_http_path
from .remote_compose_probe import remote_compose_service_known, remote_compose_service_logs_tail
from .remote_disk import RemoteDiskManager
from .transfer_manager import TransferManager


class SlimDeployManager:
    """Deploys a worktree via rsync + remote docker compose (no ilha on server)."""

    # Remote directory structure
    REMOTE_APP_ROOT = _REMOTE_APP_ROOT
    REMOTE_CADDY_DIR = _REMOTE_CADDY_DIR
    REMOTE_VOLUMES_DIR = _REMOTE_VOLUMES_DIR

    # Compose flags for every remote `docker compose` invocation: ship both the
    # user's compose (base) and ilha's overlay so server merges them at runtime.
    COMPOSE_FLAGS = compose_flags_str()

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
        "backups",
        "backups/",
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
        self.services = load_deploy_service_config(self.project_root)
        self.preflight = DeployPreflight(self.project_root)
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
        # Names of host volumes the deploy is about to ship; populated by
        # _backup_volumes_locally for the restore-warning step.
        self._last_backup_volume_names: List[str] = []
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
# Read only the Postgres vars this script needs WITHOUT `source`-ing .env.
# Sourcing evaluates the file, so a value containing $(...) or `...` (e.g. from
# a restored volume or a compose default) would execute as a command. Capturing
# a value via $(...) assigns it literally and never re-evaluates its contents.
_read_env() {
  [ -f .env ] || return 0
  line=$(grep -E "^$1=" .env | tail -n1) || return 0
  val=${line#*=}
  case "$val" in
    \"*\") val=${val#\"}; val=${val%\"} ;;
    \'*\') val=${val#\'}; val=${val%\'} ;;
  esac
  printf '%s' "$val"
}
POSTGRES_USER=$(_read_env POSTGRES_USER)
POSTGRES_PASSWORD=$(_read_env POSTGRES_PASSWORD)
POSTGRES_DB=$(_read_env POSTGRES_DB)
docker compose --env-file .env __COMPOSE_FLAGS__ up -d db
for i in $(seq 1 90); do
  if docker compose --env-file .env __COMPOSE_FLAGS__ exec -T db pg_isready -U "${POSTGRES_USER:-postgres}" -q 2>/dev/null; then
    break
  fi
  sleep 1
done
if ! docker compose --env-file .env __COMPOSE_FLAGS__ exec -T db pg_isready -U "${POSTGRES_USER:-postgres}" -q 2>/dev/null; then
  echo "PG_NOT_READY" >&2
  exit 2
fi
if ! docker compose --env-file .env __COMPOSE_FLAGS__ exec -T -e PGPASSWORD="${POSTGRES_PASSWORD:-}" db \
  psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" -h 127.0.0.1 -c "select 1" >/dev/null 2>&1; then
  echo "PG_AUTH_OR_CONN_FAILED" >&2
  exit 3
fi
echo OK
""".replace("__COMPOSE_FLAGS__", self.COMPOSE_FLAGS)
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
        return self.ssh.run_local_text(command, timeout=timeout)

    def _run_deploy_preflight(
        self,
        branch_name: str,
        strict_deploy_drift: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Delegate to DeployPreflight and wire results into the deploy report."""
        result = self.preflight.run_deploy_preflight(
            branch_name,
            strict_deploy_drift=strict_deploy_drift,
        )
        if result is None:
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
                self._last_report["source_manifest"] = manifest
                self._last_report["drift_report"] = drift_report
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

        # Smoke path override precedence (highest first):
        #   1. ILHA_SMOKE_PATH env var (per-invocation override)
        #   2. ``deploy.smoke_path`` in .ilha/config.yml (per-project override)
        #   3. preflight detection (compose label / README hint / ``/``)
        env_override = os.environ.get("ILHA_SMOKE_PATH", "").strip()
        if env_override:
            result["smoke_path"] = env_override
        elif self.services.smoke_path is not None:
            result["smoke_path"] = self.services.smoke_path
        return result

    def _probe_external_path_content_type(
        self, domain: str, path: str, expected_type_substring: str,
    ) -> Tuple[bool, Optional[str]]:
        """HEAD ``https://<domain><path>`` and verify the ``Content-Type``
        header contains the expected substring.

        Used to detect the SPA-fallback trap: if Caddy isn't routing the
        path to the backend, the frontend's catch-all serves ``index.html``
        and ``Content-Type: text/html`` — even though the app appears
        "alive". Comparing content-type catches that without false-positives
        from auth redirects or other 3xx behaviour.
        """
        url = f"https://{domain}{self._normalized_smoke_path(path)}"
        # ``-kfsSI`` = ignore TLS issues, fail on 4xx/5xx, silent, head only.
        code, out, err = self._run_local_text(
            ["curl", "-kfsSI", "--max-time", "5", url],
            timeout=10,
        )
        if code != 0:
            return False, (err or out or "curl failed").strip()[:400] or None
        lower = out.lower()
        if "content-type:" not in lower:
            return False, "response has no Content-Type header"
        if expected_type_substring.lower() not in lower:
            # Trim to the content-type line for a focused error message.
            for line in out.splitlines():
                if line.lower().startswith("content-type:"):
                    return False, (
                        f"expected Content-Type containing "
                        f"'{expected_type_substring}', got: {line.strip()}"
                    )
            return False, f"expected Content-Type containing '{expected_type_substring}'"
        return True, None

    def _probe_external_domain_html(
        self, domain: str, smoke_path: str = "/"
    ) -> Tuple[bool, Optional[str]]:
        """GET ``https://<domain><smoke_path>`` and require ``text/html`` plus a non-trivial body.

        Follows redirects: a login-required app routinely 302s the path to a login
        URL, so ``-L`` lets the probe land on the rendered HTML before measuring
        the body. The header dump (``-D -``) reflects the *final* response after
        redirects, which is what we want for content-type and body checks.
        """
        url = f"https://{domain}{self._normalized_smoke_path(smoke_path)}"
        code, out, err = self._run_local_text(
            [
                "curl",
                "-kfsSL",
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

    @staticmethod
    def _describe_smoke_failure(smoke_report: Dict[str, Any]) -> str:
        """Build a one-line root_cause that names the failing check + its error.

        The previous hardcoded ``"did not pass before timeout"`` lied whenever
        the failure was a 502, an auth error, or content-length too small —
        which is most of the time. Use the structured smoke_report instead.
        """
        checks = (smoke_report or {}).get("checks") or []
        for check in checks:
            if not check.get("success"):
                name = check.get("name", "unknown")
                target = check.get("target", "")
                err = (check.get("error") or "").strip()
                err_excerpt = err.splitlines()[0][:240] if err else "no error message"
                target_str = f" ({target})" if target else ""
                return f"Smoke check '{name}' failed{target_str}: {err_excerpt}"
        # No individual check reported failure but smoke_ok was False — likely
        # the entire window of probe attempts timed out before convergence.
        return "Post-deploy smoke checks did not converge before timeout."

    def _wait_for_dns_resolution(
        self,
        domain: str,
        expected_ip: Optional[str],
        timeout_seconds: int = 120,
    ) -> bool:
        """Block until *domain* resolves via the system resolver.

        For a fresh A record on a brand-new zone, the local resolver may have a
        negative cache or the record may not have propagated yet. We poll
        ``socket.gethostbyname`` with backoff. When *expected_ip* is provided
        we also require a matching answer; otherwise any answer is accepted.

        Returns True on resolution, False on timeout.
        """
        import socket

        deadline = time.time() + max(0, timeout_seconds)
        attempt = 0
        last_error: Optional[str] = None
        while time.time() < deadline:
            attempt += 1
            try:
                resolved = socket.gethostbyname(domain)
            except socket.gaierror as exc:
                last_error = str(exc)
                resolved = None
            if resolved and (expected_ip is None or resolved == expected_ip):
                if attempt > 1:
                    log_info(f"DNS resolved {domain} -> {resolved}")
                return True
            if resolved and expected_ip and resolved != expected_ip:
                last_error = f"resolved {resolved}, expected {expected_ip}"
            time.sleep(min(8, 1 + attempt))
        log_warning(
            f"DNS for {domain} did not resolve within {timeout_seconds}s "
            f"({last_error or 'no answer'}); external smoke checks may fail."
        )
        return False

    def _run_smoke_checks(
        self,
        *,
        username: str,
        server: str,
        smoke_path: str,
        domain: Optional[str],
        ip: Optional[str],
        timeout_seconds: int = 60,
        allow_dirty: bool = False,
    ) -> Tuple[bool, Dict[str, Any]]:
        started_at = time.time()
        dns_ready = True
        if domain:
            dns_ready = self._wait_for_dns_resolution(
                domain, expected_ip=ip, timeout_seconds=120
            )
            if not dns_ready:
                log_warning(
                    "Skipping external smoke checks until this machine resolves the "
                    f"deploy domain ({domain}). Local checks still validate the server."
                )
        delays = [0, 1, 2, 4, 8, 15]
        checks: List[Dict[str, Any]] = []
        via_web = self._remote_service_exists(username, server, self.services.web_service)
        normalized = self._normalized_smoke_path(smoke_path)
        local_url = f"http://127.0.0.1:{self.services.web_port}{normalized}"
        probe_target = "web_container" if via_web else "host_local"
        external_url = f"https://{domain}{normalized}" if domain else None

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
            if local_ok and external_url and dns_ready:
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
                    html_ok, html_err = self._probe_external_domain_html(domain, smoke_path)
                    external_html_ok = html_ok
                    external_html_result = {
                        "name": "external_domain_html",
                        "target": external_url,
                        "attempt": attempt,
                        "success": html_ok,
                        "error": html_err,
                    }

            # Footprint-driven advisory probes: catch SPA-fallback traps that
            # the generic root-path smoke can't see (a broken Caddy route or
            # missing collectstatic both produce 200 text/html on the SPA's
            # index page). Logged as warnings so a project without
            # django.contrib.admin doesn't fail-block the deploy.
            advisory_results: List[Dict[str, Any]] = []
            if local_ok and external_url and dns_ready:
                from ...utils.project_footprint import detect_project_footprint
                fp = detect_project_footprint(self.project_root)
                if fp.has_django:
                    css_ok, css_err = self._probe_external_path_content_type(
                        domain, "/static/admin/css/base.css", "text/css"
                    )
                    advisory_results.append({
                        "name": "django_static_admin_css",
                        "target": f"https://{domain}/static/admin/css/base.css",
                        "attempt": attempt,
                        "success": css_ok,
                        "error": css_err,
                        "advisory": True,
                    })
                    if not css_ok:
                        log_warning(
                            "Django static admin assets aren't being served as "
                            f"text/css (probed /static/admin/css/base.css): {css_err}. "
                            "Either collectstatic didn't run (add it to your compose "
                            "command) or path routing isn't directing /static/* to "
                            "the backend (check deploy.api_paths)."
                        )

            checks = [local_result]
            if redirect_result is not None:
                checks.append(redirect_result)
            if external_result is not None:
                checks.append(external_result)
            if external_html_result is not None:
                checks.append(external_html_result)
            checks.extend(advisory_results)
            if local_ok and redirect_ok and external_ok and external_html_ok:
                identity_ok, identity_check = self._smoke_deploy_identity(
                    username, server, allow_dirty=allow_dirty
                )
                health_ok, health_check = self._smoke_compose_health(username, server)
                if identity_check:
                    checks.append(identity_check)
                if health_check:
                    checks.append(health_check)
                if identity_ok and health_ok:
                    report = {
                        "success": True,
                        "path": smoke_path,
                        "checks": checks,
                        "duration_seconds": round(time.time() - started_at, 3),
                    }
                    if domain and not dns_ready:
                        report["external_checks_skipped"] = "dns_not_ready_locally"
                    self._last_report["smoke_checks"] = report
                    return True, report
                report = {
                    "success": False,
                    "path": smoke_path,
                    "checks": checks,
                    "duration_seconds": round(time.time() - started_at, 3),
                }
                self._last_report["smoke_checks"] = report
                return False, report

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

    def _smoke_deploy_identity(
        self,
        username: str,
        server: str,
        *,
        allow_dirty: bool,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Compare remote deploy.json to the local source manifest."""
        local = self._last_report.get("source_manifest") or {}
        local_sha = local.get("git_sha")
        if not local_sha:
            return True, None
        target = RemoteAppTarget(
            username=username,
            server=server,
            scp_target=f"{username}@{server}:/root",
            domain=None,
            ip=None,
            branch_name=str(local.get("branch_name") or ""),
        )
        raw = read_remote_text_file(
            target, REMOTE_DEPLOY_IDENTITY, ssh=self.ssh
        )
        identity = parse_deploy_identity(raw)
        remote_sha = identity.get("git_sha")
        dirty = bool(local.get("git_dirty"))
        sha_ok = remote_sha == local_sha
        dirty_ok = allow_dirty or not dirty
        ok = sha_ok and dirty_ok
        error = None
        if not sha_ok:
            error = (
                f"Remote deploy.json git_sha {remote_sha!r} does not match local HEAD {local_sha!r}"
            )
        elif not dirty_ok:
            error = "Worktree has uncommitted changes; pass --allow-dirty to accept"
        return ok, {
            "name": "deploy_identity",
            "target": REMOTE_DEPLOY_IDENTITY,
            "success": ok,
            "error": error,
            "local_sha": local_sha,
            "remote_sha": remote_sha,
            "git_dirty": dirty,
        }

    def _smoke_compose_health(
        self, username: str, server: str
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Require web (and db if configured) compose services to be running."""
        services = [self.services.web_service]
        if self.services.db_service:
            services.append(self.services.db_service)
        cmd = compose_ps_health_command(services, app_root=self.REMOTE_APP_ROOT)
        code, out, err = self._run_remote_text(username, server, cmd, timeout=30)
        ok = code == 0
        return ok, {
            "name": "compose_health",
            "target": ",".join(services),
            "success": ok,
            "error": None if ok else (err.strip() or out.strip() or f"exit {code}"),
        }

    def _remote_service_exists(self, username: str, server: str, service_name: str) -> bool:
        return remote_compose_service_known(
            self._run_remote_text,
            username=username,
            server=server,
            remote_app_root=self.REMOTE_APP_ROOT,
            service_name=service_name,
            timeout=15,
        )

    # Thin instance-method shim over the shared normalizer in ``http_probe``;
    # kept so existing call sites (``self._normalized_smoke_path(...)``)
    # don't need to be touched. Marked staticmethod for the same callability.
    _normalized_smoke_path = staticmethod(_normalize_http_path)

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
        true, run the probe inside that service via ``docker compose exec``.

        Strategy chain (curl → wget → python → ``/dev/tcp``) lives in
        ``http_probe.build_probe_shell_chain`` so the in-deploy smoke and
        the in-remote-deploy health check share one source of truth.
        """
        norm = self._normalized_smoke_path(smoke_path)
        url = f"http://127.0.0.1:{self.services.web_port}{norm}"
        tail = " >/dev/null" if quiet else " 2>&1"
        inner_probe = build_probe_shell_chain(
            port=self.services.web_port, path=norm, timeout=5, tail=tail,
        )

        if via_compose_web:
            root = shlex.quote(self.REMOTE_APP_ROOT)
            service = shlex.quote(self.services.web_service)
            return (
                f"cd {root} && docker compose --env-file .env {self.COMPOSE_FLAGS} exec -T {service} sh -c {shlex.quote(inner_probe)}"
            )
        return f"curl -fsS --max-time 5 {shlex.quote(url)}{tail}"

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
        smoke_timeout: int = 60,
        expected_ssh_fingerprint: Optional[str] = None,
        restore_volumes: bool = False,
        no_build: bool = False,
        force_password_reset: bool = False,
        secrets_profile: Optional[str] = None,
        allow_dirty: bool = False,
        set_hostname: Optional[str] = None,
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
                    secrets_profile=secrets_profile,
                    allow_dirty=allow_dirty,
                    set_hostname=set_hostname,
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
        secrets_profile: Optional[str] = None,
        allow_dirty: bool = False,
        set_hostname: Optional[str] = None,
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
                    secrets_profile=secrets_profile,
                    pin_published_images=not build,
                    source_manifest=self._last_report.get("source_manifest"),
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
                try:
                    volume_tar = self._backup_volumes_locally(branch_name, deploy_dir)
                except VolumeBackupError as exc:
                    # A requested restore cannot proceed safely without a backup;
                    # aborting here prevents the app coming up on a fresh/empty DB.
                    log_error(str(exc))
                    self._set_failure(
                        error_code="volume_backup_error",
                        stage="volume_backup",
                        root_cause=str(exc),
                        suggested_actions=[
                            "Re-run with --skip-volume-backup only if you intend "
                            "to start from existing remote volumes.",
                            "Check Docker is running and the named volumes exist locally.",
                        ],
                    )
                    return False
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
                    "Skipping local volume backup (--no-restore-volumes); "
                    "the server will initialize empty volumes."
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
            web_is_buildable = compose_service_is_buildable(
                deploy_dir / "docker-compose.yml",
                compose_env,
                self.services.web_service,
            )
            auto_build_missing = (
                bool(web_image)
                and not image_exists
                and not no_build
                and not build
                and web_is_buildable
            )
            if auto_build_missing:
                msg = (
                    f"Remote image {web_image!r} not found; will run `docker compose build` "
                    "before starting the app."
                )
                self._note(msg)
            elif bool(web_image) and not image_exists and not web_is_buildable:
                self._note(
                    f"Remote image {web_image!r} will be pulled by `docker compose up` "
                    "(no `build:` directive in compose; nothing to build)."
                )

            stage_started = time.time()
            restore_ok = True
            build_ok = True

            # Phase 1: restore volumes + start caddy (in parallel)
            log_step("Starting remote services")
            if volume_tar and self._last_backup_volume_names:
                self._warn_about_existing_remote_volumes(
                    username, server, self._last_backup_volume_names
                )
            # Detect orphan stacks from a previous deploy that used a
            # different COMPOSE_PROJECT_NAME (typically a BYO migration).
            from ...config.settings import get_project_name as _get_project_name
            from ...config.settings import sanitize_project_name as _sanitize
            self._warn_about_orphan_compose_project(
                username, server,
                active_compose_project=self.services_resolved_compose_project_name(branch_name),
                sanitized_project_name=_sanitize(_get_project_name()),
                branch_name=branch_name,
            )
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
            pull_images = bool(self._last_report.get("image_refs"))
            if pull_images and not self._registry_login_remote(
                username, server, secrets_profile=secrets_profile
            ):
                return False
            if not self._start_app_remote(
                username, server, build=False, debug=debug, pull_images=pull_images
            ):
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
            if not self._run_post_up_remote(username, server):
                return False
            if set_hostname:
                try:
                    set_remote_hostname(
                        RemoteAppTarget(
                            username=username,
                            server=server,
                            scp_target=f"{username}@{server}:/root",
                            domain=domain,
                            ip=ip,
                            branch_name=branch_name,
                        ),
                        set_hostname,
                        ssh=self.ssh,
                    )
                except ilhaCommandError as exc:
                    log_error(str(exc))
                    self._set_failure(
                        error_code="hostname_failed",
                        stage="start_app",
                        root_cause=str(exc),
                        suggested_actions=["Set the hostname manually with hostnamectl."],
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
                allow_dirty=allow_dirty,
            )
            self._mark_stage_timing("smoke_checks", stage_started)
            if not smoke_ok:
                self._set_failure(
                    error_code="smoke_checks_failed",
                    stage="smoke_checks",
                    root_cause=self._describe_smoke_failure(smoke_report),
                    suggested_actions=[
                        "Inspect remote `docker compose ps` and service logs on this target.",
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
        restore_volumes: bool = False,
        force_password_reset: bool = False,
        smoke_timeout: int = 60,
        secrets_profile: Optional[str] = None,
        dump_db_first: bool = False,
        allow_dirty: bool = False,
        set_hostname: Optional[str] = None,
    ) -> bool:
        """Update an existing slim-deployed server.

        Default: sync code + config only (no volume restore).
        restore_volumes: backup local volumes, sync, restore on the server.
        db_only: volumes only (no code sync).
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
        restore_volumes = bool(restore_volumes) or bool(db_only)
        code_only = not restore_volumes
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
        if db_only:
            log_info("  Mode:    db/volumes only (no code sync)")
        elif restore_volumes:
            log_info("  Mode:    restore local volumes onto the server")
        else:
            log_info("  Mode:    code only (no volumes)")

        mode = " (code-only)" if (code_only and not restore_volumes) else (" (db-only)" if db_only else "")
        target_label = domain or ip or f"{username}@{server}"
        self._tracker = StageTracker(f"Updating {branch_name} → {target_label}{mode}")

        def _cleanup_deploy_dir(d: Optional[Path]) -> None:
            if d is not None:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass

        if dump_db_first and self.services.db_service:
            try:
                dump_remote_postgres(
                    RemoteAppTarget(
                        username=username,
                        server=server,
                        scp_target=scp_target,
                        domain=domain,
                        ip=ip,
                        branch_name=branch_name,
                    ),
                    db_service=self.services.db_service,
                    project_root=self.project_root,
                    ssh=self.ssh,
                )
            except ilhaCommandError as exc:
                log_error(str(exc))
                self._set_failure(
                    error_code="remote_dump_failed",
                    stage="preflight",
                    root_cause=str(exc),
                    suggested_actions=["Fix SSH/db access or omit --dump-db-first."],
                    failure_category="remote_runtime_error",
                )
                return False

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
                    if not self._validate_restored_postgres_password(
                        username, server, force_password_reset=force_password_reset
                    ):
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
                except VolumeBackupError as exc:
                    log_error(str(exc))
                    self._set_failure(
                        error_code="volume_backup_error",
                        stage="volume_backup",
                        root_cause=str(exc),
                        suggested_actions=["Check Docker is running and the named volumes exist locally."],
                    )
                    return False
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
                secrets_profile=secrets_profile,
                pin_published_images=not build,
                source_manifest=(preflight or {}).get("manifest") or self._last_report.get("source_manifest"),
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
            if not self._start_app_then_smoke(
                username,
                server,
                build=build,
                debug=debug,
                domain=domain,
                ip=ip,
                smoke_path=(preflight or {}).get("smoke_path", "/"),
                smoke_timeout=smoke_timeout,
                allow_dirty=allow_dirty,
                set_hostname=set_hostname,
                secrets_profile=secrets_profile,
                branch_name=branch_name,
            ):
                _cleanup_deploy_dir(deploy_dir)
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
                secrets_profile=secrets_profile,
                pin_published_images=not build,
                source_manifest=(preflight or {}).get("manifest") or self._last_report.get("source_manifest"),
            )
            if deploy_dir is None:
                render_deploy_report(self._last_report)
                return False
            log_step("Backing up volumes")
            try:
                volume_tar = self._backup_volumes_locally(branch_name, deploy_dir)
            except VolumeBackupError as exc:
                log_error(str(exc))
                self._set_failure(
                    error_code="volume_backup_error",
                    stage="volume_backup",
                    root_cause=str(exc),
                    suggested_actions=["Check Docker is running and the named volumes exist locally."],
                )
                _cleanup_deploy_dir(deploy_dir)
                render_deploy_report(self._last_report)
                return False
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
                restore_ok = True
                for key in futures:
                    try:
                        ok = futures[key].result(timeout=self._FUTURE_TIMEOUTS.get(key, 600))
                    except TimeoutError:
                        log_error(f"{key} timed out after {self._FUTURE_TIMEOUTS.get(key, 600)}s")
                        ok = False
                    if key == "restore":
                        restore_ok = ok
                    elif key == "caddy" and not ok:
                        log_warning("Caddy start had issues, but continuing with app services...")
            if volume_tar and restore_ok and not self._validate_restored_postgres_password(
                username, server, force_password_reset=force_password_reset
            ):
                _cleanup_deploy_dir(deploy_dir)
                render_deploy_report(self._last_report)
                return False
            log_step("Starting application")
            if not self._start_app_then_smoke(
                username,
                server,
                build=build,
                debug=debug,
                domain=domain,
                ip=ip,
                smoke_path=(preflight or {}).get("smoke_path", "/"),
                smoke_timeout=smoke_timeout,
                allow_dirty=allow_dirty,
                set_hostname=set_hostname,
                secrets_profile=secrets_profile,
                branch_name=branch_name,
            ):
                _cleanup_deploy_dir(deploy_dir)
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

    def _service_is_buildable(self, deploy_dir: Path, env: Dict[str, str], service: str) -> bool:
        """True if *service* has ``build:`` in the base or overlay compose file."""
        if not service:
            return False
        return compose_service_is_buildable(
            deploy_dir / "docker-compose.yml", env, service
        ) or compose_service_is_buildable(
            deploy_dir / "docker-compose.overlay.yml", env, service
        )

    def _maybe_pin_published_images(
        self,
        deploy_dir: Path,
        branch_name: str,
        *,
        pin_published_images: bool,
    ) -> Optional[Dict[str, str]]:
        """Pin app services to digest refs when a manifest exists.

        Returns ``{}`` when pinning is skipped, a service→ref map when pinned,
        or ``None`` when assembly must abort.
        """
        if not pin_published_images:
            return {}
        compose_env = load_env_file(deploy_dir / ".env") if (deploy_dir / ".env").exists() else {}
        app_services = self.services.app_service_names()
        buildable = [s for s in app_services if self._service_is_buildable(deploy_dir, compose_env, s)]
        image_mgr = ImageManager(self.project_root)
        manifest_path = image_mgr.manifest_path(branch_name)
        if buildable and not manifest_path.exists():
            log_error(
                f"No published image manifest at {manifest_path}. "
                f"Run `ilha image publish {branch_name}` or pass `--build` to compile on the server."
            )
            return None
        if not manifest_path.exists() or not buildable:
            return {}
        try:
            image_refs = pin_deploy_compose_files(
                [
                    deploy_dir / "docker-compose.yml",
                    deploy_dir / "docker-compose.overlay.yml",
                ],
                branch_name,
                image_manager=image_mgr,
                services=buildable,
                prefer_digest=True,
            )
        except ilhaCommandError as exc:
            log_error(str(exc))
            return None
        log_info(
            "Pinned compose services to published image digests: "
            + ", ".join(f"{name}={ref}" for name, ref in image_refs.items())
        )
        return image_refs

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
        secrets_profile: Optional[str] = None,
        pin_published_images: bool = False,
        source_manifest: Optional[Dict[str, Any]] = None,
    ) -> Optional[Path]:
        """Build a flat deploy directory ready to rsync.

        Layout:
          deploy_dir/
          ├── code/                            # worktree source code
          ├── caddy/
          │   ├── docker-compose.yml           # global caddy compose
          │   ├── Caddyfile
          │   ├── caddy-docker-monitor.py
          │   └── caddy-dynamic-config.py      (if exists)
          ├── docker-compose.yml               # the user's compose (base, copied verbatim)
          ├── docker-compose.overlay.yml       # ilha's overlay deltas
          ├── .env                             # merged env with overrides applied
          └── volumes/                         # (populated later if needed)
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

            worktree_compose = worktree_path / "docker-compose.yml"
            worktree_overlay = worktree_path / ".ilha" / "docker-compose.overlay.yml"
            if not worktree_compose.exists():
                log_error(f"docker-compose.yml not found in worktree: {worktree_compose}")
                return None
            if not worktree_overlay.exists():
                log_error(
                    f"ilha overlay not found at {worktree_overlay}. "
                    f"Re-run `ilha setup` to regenerate."
                )
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

            # ── compose files (base + overlay) ───────────────────────
            shutil.copy2(worktree_compose, deploy_dir / "docker-compose.yml")
            shutil.copy2(worktree_overlay, deploy_dir / "docker-compose.overlay.yml")
            log_info("Copied base + overlay compose files")

            # Apply domain/IP to Caddy labels in the OVERLAY (ilha owns those).
            if domain or ip:
                self._apply_compose_overrides(
                    deploy_dir / "docker-compose.overlay.yml", domain=domain, ip=ip
                )

            # Strip elevated privileges from the BASE COPY (privileges are user-
            # defined; the overlay never carries them). This mutates the deploy
            # copy, never the user's source.
            self._strip_privileges_in_compose(deploy_dir / "docker-compose.yml")

            # ── .env (merged) ────────────────────────────────────────
            self._build_merged_env(
                deploy_dir, worktree_path, branch_name,
                domain=domain, ip=ip, debug=debug,
                use_staging_certificates=use_staging_certificates,
                secrets_profile=secrets_profile,
            )

            image_refs = self._maybe_pin_published_images(
                deploy_dir,
                branch_name,
                pin_published_images=pin_published_images,
            )
            if image_refs is None:
                return None
            self._last_report["image_refs"] = image_refs

            identity = build_deploy_identity(
                source_manifest or self._last_report.get("source_manifest") or {},
                image_refs=image_refs,
                build=not pin_published_images,
                secrets_profile=secrets_profile,
            )
            write_deploy_identity(deploy_dir / ".ilha" / "deploy.json", identity)

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
        """Update Caddy labels and ``restart`` policy in the OVERLAY compose."""
        try:
            import yaml

            data = yaml.safe_load(compose_path.read_text())
            if not data:
                return
            normalized_domain = normalize_deploy_host(domain) if domain else None
            normalized_ip = normalize_deploy_host(ip) if ip else None

            # Django+SPA: route API paths to the backend, everything else to
            # the frontend. Skip when only one tier exists or the user hasn't
            # provided api_paths (None means "no path constraints").
            path_routes: Dict[str, List[str]] = {}
            if (
                self.services.frontend_service
                and self.services.api_paths
                and self.services.frontend_service in (data.get("services") or {})
                and self.services.web_service in (data.get("services") or {})
            ):
                path_routes[self.services.web_service] = list(self.services.api_paths)
                # Frontend explicitly has no path constraint (catch-all).
                path_routes[self.services.frontend_service] = []
                log_info(
                    f"Django+SPA routing: {self.services.web_service} handles "
                    f"{', '.join(self.services.api_paths)}; "
                    f"{self.services.frontend_service} handles everything else."
                )

            ensure_caddy_labels_and_network(
                data,
                domain=normalized_domain,
                ip=normalized_ip,
                path_routes=path_routes or None,
            )
            # Production deploy: ensure web service restarts on exit (e.g. OOM 137)
            if normalized_domain or normalized_ip:
                services = data.get("services") or {}
                web_config = services.get("web")
                if web_config is not None and "restart" not in web_config:
                    web_config["restart"] = "unless-stopped"

            compose_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
            log_info(f"Applied {'domain' if normalized_domain else 'IP'} override to compose labels")
        except ImportError:
            log_warning("PyYAML not available — skipping compose label override")
        except Exception as exc:
            log_warning(f"Could not update compose labels: {exc}")

    # The deploy-time compose transforms are pure functions in deploy_compose.py.
    # These methods stay as thin delegators so the manager's surface and existing
    # call sites/tests are unchanged.
    def _strip_host_port_publishes(self, compose_data: dict) -> None:
        from .deploy_compose import strip_host_port_publishes

        strip_host_port_publishes(compose_data)

    def _strip_unnecessary_privileges(self, compose_data: dict) -> None:
        from .deploy_compose import strip_unnecessary_privileges

        strip_unnecessary_privileges(compose_data)

    def _retarget_relative_path(self, value: str) -> str:
        from .deploy_compose import retarget_relative_path

        return retarget_relative_path(value)

    def _retarget_paths_to_code_subdir(self, compose_data: dict) -> None:
        from .deploy_compose import retarget_paths_to_code_subdir

        retarget_paths_to_code_subdir(compose_data)

    def _strip_privileges_in_compose(self, compose_path: Path) -> None:
        """Load the base compose, apply deploy transforms, and rewrite it in place.

        Transforms applied:
        - strip elevated privileges from non-privileged services
        - retarget user-written relative paths into the ``./code/`` subdir
        """
        try:
            import yaml
            data = yaml.safe_load(compose_path.read_text())
            if not data:
                return
            self._strip_unnecessary_privileges(data)
            self._retarget_paths_to_code_subdir(data)
            self._strip_host_port_publishes(data)
            compose_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        except ImportError:
            log_warning("PyYAML not available — skipping base compose finalization")
        except Exception as exc:
            log_warning(f"Could not finalize base compose {compose_path}: {exc}")

    # ──────────────────────────────────────────────────────────────────
    # Internal: merged .env
    # ──────────────────────────────────────────────────────────────────
    def _extract_compose_db_env_defaults(self, worktree_path: Path) -> Dict[str, str]:
        """Read ``services.db.environment`` from the worktree compose and
        return the ``${VAR:-default}`` defaults (or literal values).

        Returns a ``{POSTGRES_USER: ..., POSTGRES_DB: ..., POSTGRES_PASSWORD: ...}``
        mapping containing only the keys the compose explicitly defaults. The
        caller layers these in *under* the ``.env``/``env.ilha`` values so an
        explicit user-set value still wins.
        """
        try:
            import yaml
        except ImportError:
            return {}
        compose_path = worktree_path / "docker-compose.yml"
        if not compose_path.exists():
            return {}
        try:
            data = yaml.safe_load(compose_path.read_text()) or {}
        except yaml.YAMLError:
            return {}

        services = data.get("services") or {}
        # The DB service's compose key may follow user naming; ``self.services``
        # already resolved it via footprint detection and config.
        db_key = self.services.db_service or "db"
        db_svc = services.get(db_key)
        if not isinstance(db_svc, dict):
            return {}
        environment = db_svc.get("environment") or {}
        # Compose accepts both dict and list forms.
        if isinstance(environment, list):
            env_dict: Dict[str, str] = {}
            for entry in environment:
                if isinstance(entry, str) and "=" in entry:
                    k, v = entry.split("=", 1)
                    env_dict[k.strip()] = v.strip()
            environment = env_dict

        out: Dict[str, str] = {}
        # Match `${VAR:-default}` / `${VAR-default}` / literal value.
        var_default_re = re.compile(r"^\s*\$\{([^}:?-]+)(?::?[-?])([^}]*)\}\s*$")
        for postgres_key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD"):
            if postgres_key not in environment:
                continue
            raw = str(environment[postgres_key])
            m = var_default_re.match(raw)
            if m and m.group(1) == postgres_key:
                out[postgres_key] = m.group(2)
            elif "${" not in raw:
                out[postgres_key] = raw
            # else: a non-default ${OTHER_VAR} reference — caller's env layers handle it.
        return out

    def _build_merged_env(
        self,
        deploy_dir: Path,
        worktree_path: Path,
        branch_name: str,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
        debug: bool = False,
        use_staging_certificates: bool = False,
        secrets_profile: Optional[str] = None,
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
        REMOTE_CODE_DIR = f"{self.REMOTE_APP_ROOT}/code"

        root_env = self.project_root / ".env"
        env_ilha = get_env_compose_file_path(worktree_path)

        secrets_layer = None
        if secrets_profile:
            secrets_layer = SecretsManager(self.project_root).decrypt_profile(secrets_profile)
            log_info(f"Merged SOPS secret profile: {secrets_profile}")

        # Layers (later wins): root .env < worktree env.ilha < SOPS profile.
        # Compose ``db`` ``${VAR:-default}`` values fill only the gaps.
        merged: dict = merge_env_layers(
            load_env_file(root_env) if root_env.exists() else {},
            load_env_file(env_ilha) if env_ilha.exists() else {},
            secrets_layer,
            base_defaults=self._extract_compose_db_env_defaults(worktree_path),
        )

        # Layer 3: deployment overrides
        # PROJECT_ROOT must point to the code directory on the server so that
        # ${PROJECT_ROOT}:/app bind mounts and ${PROJECT_ROOT}/.env env_file
        # references in docker-compose.overlay.yml resolve correctly.
        merged["PROJECT_ROOT"] = REMOTE_CODE_DIR

        # COMPOSE_PROJECT_NAME: for BYO-main projects (the project pre-dated
        # ilha and uses the bare-project namespace locally), unify the prod
        # namespace with local so the backed-up tar restores into volumes
        # that the prod compose actually mounts. Cheap host-volume probe;
        # only triggers for main/master.
        from ...utils.compose_external_volumes import resolve_compose_project_name
        from ...config.settings import get_project_name, sanitize_project_name
        sanitized_project = sanitize_project_name(get_project_name())
        resolved_project_name = resolve_compose_project_name(
            self.project_root,
            branch_name,
            sanitized_project_name=sanitized_project,
        )
        if resolved_project_name != merged.get("COMPOSE_PROJECT_NAME"):
            log_info(
                f"COMPOSE_PROJECT_NAME resolved to '{resolved_project_name}' "
                f"(was '{merged.get('COMPOSE_PROJECT_NAME')}' from env layers); "
                f"matches the bare-project namespace where local volumes live."
            )
        merged["COMPOSE_PROJECT_NAME"] = resolved_project_name

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

        from ...utils.project_footprint import detect_project_footprint

        footprint = detect_project_footprint(worktree_path)
        # Worktree compose often expects DATABASE_URL; synthesize from POSTGRES_*
        # only when this project actually ships a Postgres tier. Static/Caddy-only
        # projects must not get a phantom DATABASE_URL with bipassword leaked into
        # their .env.
        if footprint.has_postgres and (
            "DATABASE_URL" not in merged or not merged["DATABASE_URL"]
        ):
            # Host is the configured db service name (compose network hostname),
            # not a hardcoded ``db`` — a project whose service is e.g. ``postgres``
            # would otherwise get an unreachable DATABASE_URL. Credentials come
            # from the project's own POSTGRES_* (pulled from compose defaults in
            # Layer 2.5); no project-specific literals are baked in.
            db_host = self.services.db_service or "db"
            user = merged.get("POSTGRES_USER", "postgres")
            password = merged.get("POSTGRES_PASSWORD", "")
            db = merged.get("POSTGRES_DB", user)
            merged["DATABASE_URL"] = f"postgres://{user}:{password}@{db_host}:5432/{db}"
            if not password:
                log_warning(
                    "DATABASE_URL synthesized without a POSTGRES_PASSWORD. "
                    "Set POSTGRES_PASSWORD in your .env for production."
                )

        # ── Strip deploy-only / provider control-plane secrets ──
        # The DigitalOcean API token grants full account control (create/destroy
        # droplets, edit DNS). It legitimately lives in the operator's project
        # .env for DNS/droplet commands, but must NEVER be shipped to a workload
        # server: an app RCE or any local read there would escalate to full
        # cloud-account takeover. Keep such secrets on the operator machine / CI.
        stripped = [k for k in list(merged) if k in CONTROL_PLANE_ENV_KEYS]
        for k in stripped:
            merged.pop(k, None)
        if stripped:
            log_info(f"Stripped deploy-only secret(s) from server .env: {', '.join(stripped)}")

        # Warn if the environment still carries the publicly-known insecure
        # SECRET_KEY (legacy worktrees created before per-env key generation).
        from ...config.settings import INSECURE_DEFAULT_SECRET_KEY
        if merged.get("DJANGO_SECRET_KEY") == INSECURE_DEFAULT_SECRET_KEY:
            log_warning(
                "DJANGO_SECRET_KEY is the insecure shared default — anyone can forge "
                "sessions/CSRF tokens. Set a unique DJANGO_SECRET_KEY in this "
                "worktree's .env before deploying to production."
            )

        # ── Write top-level .env (used by ``docker compose --env-file``) ──
        from ...utils.env_loader import format_env_text

        env_content = format_env_text(merged, quote_values=True)

        # Secret-bearing files must not be world-readable. write_text() honours
        # umask (typically 0644) and rsync -a preserves the mode onto the server,
        # so tighten to 0600 (owner-only) on every copy.
        def _write_secret_file(path: Path, content: str) -> None:
            path.write_text(content)
            path.chmod(0o600)

        env_path = deploy_dir / ".env"
        _write_secret_file(env_path, env_content)
        log_info(f"Merged .env written ({len(merged)} vars)")

        # ── Also write .env inside code/ (referenced by compose env_file) ──
        code_env = deploy_dir / "code" / ".env"
        _write_secret_file(code_env, env_content)

        # ── Update the env.ilha inside code/.ilha/ (loaded *second* by compose,
        #    so it must not revert production overrides to localhost defaults) ──
        code_env_ilha = deploy_dir / "code" / ".ilha" / "env.ilha"
        if code_env_ilha.exists():
            _write_secret_file(code_env_ilha, env_content)
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

        # Shared routing module: both monitor and dynamic-config import this.
        routing_src = script_dir / "scripts" / "caddy_routing.py"
        if routing_src.exists():
            shutil.copy2(routing_src, caddy_dir / "caddy_routing.py")

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
            content = content.replace(
                "{ROUTING_MODULE_PATH}",
                f"{self.REMOTE_CADDY_DIR}/caddy_routing.py",
            )
            (caddy_dir / "docker-compose.yml").write_text(content)
            log_info("Caddy compose file assembled with server-side paths")

    # ──────────────────────────────────────────────────────────────────
    # Internal: volume backup / restore
    # ──────────────────────────────────────────────────────────────────
    def _backup_volumes_locally(self, branch_name: str, deploy_dir: Path) -> Optional[Path]:
        """Backup the project's named volumes into deploy_dir/volumes/.

        Discovers volumes by parsing the project's ``docker-compose.yml`` +
        overlay (the only source of truth) instead of guessing from a
        hardcoded suffix list. This handles projects that name their volumes
        differently from ilha's convention (e.g. ``pgdata`` vs
        ``postgres_data``) and respects ``external: true`` ownership.

        Returns path to the tar file, or None if there's nothing to back up.
        """
        from ...core.docker_manager import DockerManager
        from ...utils.compose_external_volumes import discover_named_volumes
        from ...utils.validation import validate_volume_exists

        compose_files = [
            self.project_root / "docker-compose.yml",
            self.project_root / ".ilha" / "docker-compose.overlay.yml",
        ]
        env_mgr = EnvironmentManager(project_root=self.project_root)
        env_vars = env_mgr.get_environment_variables(branch_name)

        discovered = discover_named_volumes(compose_files, env=env_vars)
        if not discovered:
            log_info("No named volumes declared in compose; skipping volume backup")
            return None

        # Skip externally-managed volumes — the project owns their lifecycle
        # (could be shared across projects, mounted from host paths, etc.).
        # Backup what ilha-deployed compose actually creates: the non-external
        # named volumes, plus their host-resolved docker names.
        candidates = {v.key: v.docker_name for v in discovered if not v.is_external}
        external_keys = [v.key for v in discovered if v.is_external]
        if external_keys:
            log_info(
                f"Skipping external volume(s) (project-owned): {', '.join(external_keys)}"
            )

        if not candidates:
            log_info("No backup-able named volumes (all external); skipping")
            return None

        existing = {
            key: name
            for key, name in candidates.items()
            if validate_volume_exists(name)
        }
        if not existing:
            log_info(
                "No declared volumes present on host; skipping volume backup "
                f"(candidates: {', '.join(candidates.values())})"
            )
            return None

        volumes_dir = deploy_dir / "volumes"
        volumes_dir.mkdir(exist_ok=True)

        log_info(
            f"Backing up {len(existing)} named volume(s) "
            f"discovered from compose: {', '.join(existing.values())}"
        )
        docker_mgr = DockerManager(project_root=self.project_root)
        try:
            backup_path = docker_mgr.backup_volumes(
                branch_name, volumes_dir, volume_names_override=existing
            )
            if backup_path is not None and backup_path.exists():
                size_mb = backup_path.stat().st_size / 1024 / 1024
                log_success(f"Volumes backed up ({size_mb:.1f} MB)")
                # Stash the actual volume names so the restore-warning step
                # can probe the server for which already exist (so we can
                # tell the operator exactly which volumes will be overwritten).
                self._last_backup_volume_names = list(existing.values())
                return backup_path
            else:
                # We KNOW there are volumes present on the host (``existing`` is
                # non-empty), so a null/absent archive here is a real failure,
                # not "nothing to back up". Raise so the caller aborts instead
                # of restoring nothing and booting on a fresh/empty volume.
                raise VolumeBackupError(
                    "Volume backup produced no archive for volumes present on host: "
                    f"{', '.join(existing.values())}"
                )
        except VolumeBackupError:
            raise
        except Exception as exc:
            raise VolumeBackupError(f"Volume backup failed: {exc}") from exc

    def services_resolved_compose_project_name(self, branch_name: str) -> str:
        """Return the effective ``COMPOSE_PROJECT_NAME`` for this deploy.

        Resolves through the same path as ``_build_merged_env`` so callers
        (orphan detection, future doctor checks) see the same value the
        deploy will write to ``.env`` on the server.
        """
        from ...utils.compose_external_volumes import resolve_compose_project_name
        from ...config.settings import get_project_name, sanitize_project_name
        sanitized = sanitize_project_name(get_project_name())
        return resolve_compose_project_name(
            self.project_root, branch_name, sanitized_project_name=sanitized
        )

    def _probe_remote_existing_volumes(
        self, username: str, server: str, volume_names: Iterable[str]
    ) -> List[str]:
        """Return the subset of ``volume_names`` that already exist on the
        server. Cheap: one ``docker volume inspect`` invocation, exits 0 only
        for volumes that do exist (silently skips missing).
        """
        names = [n for n in volume_names if n]
        if not names:
            return []
        # ``docker volume inspect`` accepts multiple names; ``-f`` per-volume
        # name output, with one line per volume that exists. Missing volumes
        # surface as stderr (we ignore) and a non-zero exit (also ignored —
        # we just consume the existing-volume lines from stdout).
        quoted = " ".join(shlex.quote(n) for n in names)
        cmd = f"docker volume inspect -f '{{{{.Name}}}}' {quoted} 2>/dev/null || true"
        code, out, _ = self._run_remote_text(username, server, cmd, timeout=15)
        existing = [line.strip() for line in (out or "").splitlines() if line.strip()]
        # Preserve caller order, only return those actually requested.
        wanted = set(names)
        return [n for n in existing if n in wanted]

    def _warn_about_orphan_compose_project(
        self,
        username: str,
        server: str,
        *,
        active_compose_project: str,
        sanitized_project_name: str,
        branch_name: str,
    ) -> None:
        """Surface a cleanup hint when a previous deploy used a different
        ``COMPOSE_PROJECT_NAME`` and left orphan containers/volumes on the
        droplet. The most common case is a BYO project where ilha previously
        deployed under ``<project>-<branch>`` and now resolves to bare
        ``<project>`` after BYO-main detection.
        """
        convention = f"{sanitized_project_name}-{branch_name}"
        if convention == active_compose_project:
            return  # already aligned, nothing to check
        cmd = (
            f"docker ps -a --filter label=com.docker.compose.project={shlex.quote(convention)} "
            "--format '{{.Names}}'"
        )
        code, out, _ = self._run_remote_text(username, server, cmd, timeout=15)
        if code != 0:
            return
        names = [n.strip() for n in (out or "").splitlines() if n.strip()]
        if not names:
            return
        log_warning(
            f"Found {len(names)} orphan container(s) on the server from a "
            f"previous deploy under '{convention}': {', '.join(names)}. "
            f"This deploy uses '{active_compose_project}' so the old stack "
            f"will be left running until cleaned up. Run on the server: "
            f"`cd /app && docker compose --env-file .env "
            f"-f docker-compose.yml -f docker-compose.overlay.yml "
            f"-p {shlex.quote(convention)} down -v`"
        )

    def _warn_about_existing_remote_volumes(
        self,
        username: str,
        server: str,
        backup_volume_names: Iterable[str],
    ) -> None:
        """Surface a clear warning before ``_restore_volumes_remote`` overwrites
        existing server-side volumes. Doesn't block — slim deploy is often
        run unattended and the user has ``--no-restore-volumes`` as the
        explicit opt-out — but the message lands in the deploy log so an
        operator sees what's about to be overwritten.
        """
        existing = self._probe_remote_existing_volumes(username, server, backup_volume_names)
        if not existing:
            return
        log_warning(
            f"Volume restore will OVERWRITE existing server-side volumes: "
            f"{', '.join(existing)}. Pass --no-restore-volumes to keep "
            f"server data and let the app fail-fast if its compose can't "
            f"start without local data."
        )

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
cd {self.REMOTE_APP_ROOT} 2>/dev/null && docker compose --env-file .env {self.COMPOSE_FLAGS} down 2>/dev/null || true

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
    # Internal: helper for uploading a single file via stdin → ssh cat
    # ──────────────────────────────────────────────────────────────────
    def _upload_remote_file(
        self,
        username: str,
        server: str,
        local_path: Path,
        remote_path: str,
        *,
        label: Optional[str] = None,
        timeout: int = 30,
    ) -> bool:
        """Pipe a local file's contents to the remote via ``ssh ... cat >``.

        Used for the small Python helpers shipped to ``/tmp/ilha-<session>/``
        for server prep and remote deploy. Returns True on success.
        """
        content = local_path.read_text()
        cat_cmd = self.ssh.build_ssh_command(
            username, server, f"cat > {shlex.quote(remote_path)}"
        )
        result = subprocess.run(
            cat_cmd, input=content, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            log_error(f"Failed to upload {label or local_path.name}: {result.stderr}")
            return False
        return True

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
        http_probe_path = script_dir / "http_probe.py"

        if not server_prep_path.exists():
            log_error(f"Slim prep script not found: {server_prep_path}")
            return False
        if not deploy_utils_path.exists():
            log_error(f"Deploy utilities not found: {deploy_utils_path}")
            return False
        if not http_probe_path.exists():
            log_error(f"HTTP probe helpers not found: {http_probe_path}")
            return False

        # Upload all files to remote /tmp directory
        remote_tmp_dir = f"/tmp/ilha-{self._session_id}"
        remote_prep_path = f"{remote_tmp_dir}/server_prep.py"
        remote_utils_path = f"{remote_tmp_dir}/deploy_utils.py"
        remote_probe_path = f"{remote_tmp_dir}/http_probe.py"

        # Create remote tmp directory
        mkdir_cmd = self.ssh.build_ssh_command(
            username, server, f"mkdir -p {remote_tmp_dir}"
        )
        result = subprocess.run(mkdir_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log_error(f"Failed to create remote temp directory: {result.stderr}")
            return False

        # Upload http_probe.py and deploy_utils.py first (dependencies of server_prep.py)
        if not self._upload_remote_file(
            username, server, http_probe_path, remote_probe_path, label="http_probe.py"
        ):
            return False
        if not self._upload_remote_file(
            username, server, deploy_utils_path, remote_utils_path, label="deploy_utils.py"
        ):
            return False
        if not self._upload_remote_file(
            username, server, server_prep_path, remote_prep_path, label="server_prep.py"
        ):
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
        return self.ssh.run_remote_text(username, server, command, timeout=timeout)

    # ──────────────────────────────────────────────────────────────────
    # Internal: rsync
    # ──────────────────────────────────────────────────────────────────
    def _rsync_streaming_once(
        self, cmd: List[str]
    ) -> Tuple[bool, int, List[str]]:
        """Run rsync with progress streaming. Returns (timed_out, returncode, out_lines).

        Output is drained on a background reader thread and consumed through a
        queue with a bounded ``get(timeout=…)``. This enforces the wall-clock
        deadline even when rsync produces NO output at all — a blocking
        ``readline()`` loop would sit inside readline forever on a stalled
        SSH/TCP connection and never re-check the deadline (the exact hang this
        watchdog exists to prevent).
        """
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
        line_q: "queue.Queue[Optional[str]]" = queue.Queue()

        def _drain(stream, q: "queue.Queue") -> None:
            try:
                for raw in iter(stream.readline, ""):
                    q.put(raw)
            finally:
                q.put(None)  # sentinel: stdout closed / EOF

        assert process.stdout is not None
        reader = threading.Thread(target=_drain, args=(process.stdout, line_q), daemon=True)
        reader.start()

        deadline = time.time() + self._RSYNC_MAX_WAIT_SEC
        timed_out = False
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                raw = line_q.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                # No output this slice — loop and re-check the wall-clock
                # deadline. This is what lets a silent stall time out.
                continue
            if raw is None:
                break  # EOF
            line = raw.rstrip()
            out_lines.append(line)
            if "%" in line or "speedup" in line.lower():
                log_info(f"  {line}")

        if timed_out:
            process.kill()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        return timed_out, int(process.returncode or 0), out_lines

    @staticmethod
    def build_deploy_rsync_args(app_uid: int, app_gid: int) -> List[str]:
        """rsync flags for the deploy tree.

        ``-a`` implies ``-o -g``: with root receiving, rsync stamps the
        *laptop's* uid onto the server (macOS uid 501/502), leaving 0600
        secrets (.env, .ilha/age.key) unreadable by the container user — this
        took staging down on 2026-08-21. ``--chown`` overrides that mapping so
        every transferred file lands owned by the app user. Modes are still
        preserved: a 0600 secret owned by the app uid is readable by the app.
        """
        return [
            "-avz",
            f"--chown={int(app_uid)}:{int(app_gid)}",
            "--progress",
            "--delete-delay",
            "--partial",
            "--inplace",
        ]

    @classmethod
    def build_ownership_heal_command(cls, app_uid: int, app_gid: int) -> str:
        """Remote command fixing ownership of the deployed tree in place.

        ``--chown`` only touches files rsync transfers; content-unchanged
        files are skipped and keep whatever ownership an earlier (buggy)
        deploy left them with. This heals those. ``volumes/`` is deliberately
        excluded — database data has its own uid contract (e.g. postgres 999).
        """
        root = cls.REMOTE_APP_ROOT
        # Every target exists after a successful transfer, so a chown failure
        # here is a real signal (e.g. read-only fs) — let it reach the caller.
        targets = f"{root}/code {root}/.env {root}/docker-compose.yml {root}/docker-compose.overlay.yml"
        return f"chown -R {int(app_uid)}:{int(app_gid)} {targets}"

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
            *self.build_deploy_rsync_args(self.services.app_uid, self.services.app_gid),
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
                # Heal ownership rsync could not: --chown covers transferred
                # files only, and content-unchanged files are skipped, so a
                # tree damaged by an earlier deploy stays damaged without this.
                heal_cmd = self.ssh.build_ssh_command(
                    username,
                    server,
                    self.build_ownership_heal_command(
                        self.services.app_uid, self.services.app_gid
                    ),
                )
                heal = subprocess.run(
                    heal_cmd, capture_output=True, text=True, timeout=120
                )
                if heal.returncode != 0:
                    log_warning(
                        "Ownership normalization failed (continuing): "
                        f"{heal.stderr.strip() or heal.returncode}"
                    )
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
docker compose --env-file .env {self.COMPOSE_FLAGS} down --remove-orphans 2>&1 || true
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
        """Start global Caddy proxy on the server.

        ``--force-recreate`` is intentional: it tears down ``caddy_monitor`` so
        any updates to its bind-mounted scripts (``caddy-docker-monitor.py``,
        ``caddy_routing.py``, ``caddy_admin_utils.py``) are re-imported by a
        fresh Python process. Without this, label-schema or routing-logic
        fixes would silently sit on disk while the running daemon kept its
        old in-memory copy.
        """
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

# NOTE: intentionally do NOT `source .env` here — `docker compose --env-file`
# performs variable interpolation from the file directly, and sourcing an
# untrusted .env would execute $(...) / `...` embedded in a value.
docker compose --env-file .env {self.COMPOSE_FLAGS} build 2>&1
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
        pull_images: bool = False,
    ) -> str:
        """Shell wrapper that exports service config and runs ``remote_deploy.py`` on the server."""
        web_service = self.services.web_service
        web_port = self.services.web_port
        health_path = self.services.health_path
        db_service = self.services.db_service
        deps_args = " ".join(shlex.quote(s) for s in self.services.dependency_services)
        worker_args = " ".join(shlex.quote(s) for s in self.services.worker_services)
        frontend_service = self.services.frontend_service or ""
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
                f"export FRONTEND_SERVICE={shlex.quote(frontend_service)}",
                f"export BUILD_WEB={'1' if build else '0'}",
                f"export PULL_IMAGES={'1' if pull_images else '0'}",
                f"export PYTHONPATH={remote_tmp_dir}:${{PYTHONPATH:-}}",
                f"cd {remote_tmp_dir}",
                f"python3 {shlex.quote(remote_deploy_file)}",
            ]
        )

    def _registry_login_remote(
        self,
        username: str,
        server: str,
        *,
        secrets_profile: Optional[str] = None,
    ) -> bool:
        """Log the remote Docker daemon into DOCR so compose can pull digests."""
        try:
            token = resolve_registry_read_token(
                self.project_root, secrets_profile=secrets_profile
            )
            docker_login_remote(self.ssh, username, server, token)
        except ilhaCommandError as exc:
            log_error(str(exc))
            self._set_failure(
                error_code="registry_login_failed",
                stage="start_app",
                root_cause=str(exc),
                suggested_actions=[
                    "Set DOCR_READ_TOKEN in the environment or a SOPS profile.",
                    "Or pass `--build` to compile images on the server instead of pulling.",
                ],
                failure_category="remote_setup_error",
            )
            return False
        log_info("Remote docker login to registry.digitalocean.com succeeded")
        return True

    def _run_post_up_remote(self, username: str, server: str) -> bool:
        """Run migrate + configured post_up commands after compose is up."""
        migrate = self.services.resolved_migrate_command(self.project_root)
        commands: List[Tuple[str, str]] = []
        if migrate:
            commands.append(("migrate", migrate))
        for extra in self.services.post_up:
            text = str(extra).strip()
            if text:
                commands.append(("post_up", text))
        for label, raw in commands:
            argv = shlex.split(raw)
            if not argv:
                continue
            log_step(f"Running remote {label}: {raw}")
            cmd = remote_compose_exec_command(
                self.services.web_service, argv, app_root=self.REMOTE_APP_ROOT
            )
            result = self.ssh.execute_remote(
                username, server, cmd, timeout=600, check=False
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                log_error(f"Remote {label} failed: {err}")
                self._set_failure(
                    error_code="post_up_failed",
                    stage="start_app",
                    root_cause=f"Remote {label} failed: {err or raw}",
                    suggested_actions=[
                        f"Run `ilha droplet exec {self.services.web_service} {raw}` and inspect logs.",
                    ],
                    failure_category="remote_runtime_error",
                )
                return False
            log_success(f"Remote {label} succeeded")
        return True

    def _start_app_then_smoke(
        self,
        username: str,
        server: str,
        *,
        build: bool,
        debug: bool,
        domain: Optional[str],
        ip: Optional[str],
        smoke_path: str,
        smoke_timeout: int,
        allow_dirty: bool,
        set_hostname: Optional[str],
        secrets_profile: Optional[str],
        branch_name: str,
    ) -> bool:
        """Login/pull/start/migrate/hostname/smoke shared by put modes."""
        pull_images = bool(self._last_report.get("image_refs"))
        if pull_images and not self._registry_login_remote(
            username, server, secrets_profile=secrets_profile
        ):
            return False
        if not self._start_app_remote(
            username, server, build=build, debug=debug, pull_images=pull_images
        ):
            log_error("Failed to start application services on the server.")
            self._set_failure(
                error_code="app_start_failed",
                stage="start_app",
                root_cause="Failed to start application services on the server.",
                suggested_actions=["Inspect remote compose ps output and service logs."],
                failure_category="remote_runtime_error",
            )
            return False
        if not self._run_post_up_remote(username, server):
            return False
        if set_hostname:
            try:
                set_remote_hostname(
                    RemoteAppTarget(
                        username=username,
                        server=server,
                        scp_target=f"{username}@{server}:/root",
                        domain=domain,
                        ip=ip,
                        branch_name=branch_name,
                    ),
                    set_hostname,
                    ssh=self.ssh,
                )
            except ilhaCommandError as exc:
                log_error(str(exc))
                self._set_failure(
                    error_code="hostname_failed",
                    stage="start_app",
                    root_cause=str(exc),
                    suggested_actions=["Set the hostname manually with hostnamectl."],
                    failure_category="remote_runtime_error",
                )
                return False
        log_step("Running smoke checks")
        smoke_ok, smoke_report = self._run_smoke_checks(
            username=username,
            server=server,
            smoke_path=smoke_path,
            domain=domain,
            ip=ip,
            timeout_seconds=smoke_timeout,
            allow_dirty=allow_dirty,
        )
        if not smoke_ok:
            self._set_failure(
                error_code="smoke_checks_failed",
                stage="smoke_checks",
                root_cause=self._describe_smoke_failure(smoke_report),
                suggested_actions=[
                    "Inspect remote `docker compose ps` and service logs on this target.",
                    "Inspect remote compose ps output and service logs.",
                ],
                failure_category="remote_runtime_error",
            )
            return False
        return True

    def _start_app_remote(
        self,
        username: str,
        server: str,
        build: bool = False,
        debug: bool = False,
        pull_images: bool = False,
    ) -> bool:
        """Start application services on the server via nohup + poll.

        Strategy (brief downtime accepted; no canary/swap):
        1. Bring up dependency services and wait for the database.
        2. (Optional) Pull published images or build the web image.
        3. ``compose up -d`` for web + workers.
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
        # Upload remote_deploy.py + its imports to remote /tmp
        script_dir = get_script_dir() / "scripts"
        remote_deploy_path = script_dir / "remote_deploy.py"
        deploy_utils_path = script_dir / "deploy_utils.py"
        http_probe_path = script_dir / "http_probe.py"

        for required in (remote_deploy_path, deploy_utils_path, http_probe_path):
            if not required.exists():
                log_error(f"Required deploy script missing: {required}")
                return False

        remote_tmp_dir = f"/tmp/ilha-{self._session_id}"
        remote_deploy_file = f"{remote_tmp_dir}/remote_deploy.py"
        remote_utils_file = f"{remote_tmp_dir}/deploy_utils.py"
        remote_probe_file = f"{remote_tmp_dir}/http_probe.py"

        # Create remote tmp directory
        mkdir_cmd = self.ssh.build_ssh_command(
            username, server, f"mkdir -p {remote_tmp_dir}"
        )
        result = subprocess.run(mkdir_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log_error(f"Failed to create remote temp directory: {result.stderr}")
            return False

        # Upload dependencies before the entry script.
        if not self._upload_remote_file(
            username, server, http_probe_path, remote_probe_file, label="http_probe.py"
        ):
            return False
        if not self._upload_remote_file(
            username, server, deploy_utils_path, remote_utils_file, label="deploy_utils.py"
        ):
            return False
        if not self._upload_remote_file(
            username, server, remote_deploy_path, remote_deploy_file, label="remote_deploy.py"
        ):
            return False

        wrapper_script = self._compose_remote_deploy_wrapper_script(
            app_root=app_root,
            remote_tmp_dir=remote_tmp_dir,
            remote_deploy_file=remote_deploy_file,
            build=build,
            pull_images=pull_images,
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
            f"cd {app_root} && docker compose --env-file .env {self.COMPOSE_FLAGS} ps -q {shlex.quote(web_service)} 2>/dev/null | head -1",
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
