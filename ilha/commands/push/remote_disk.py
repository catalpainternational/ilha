"""
Remote disk management for slim deploys.

Checks remote disk capacity, logs diagnostics, and attempts recovery
when the server runs out of space.
"""

import random
import subprocess
import time
from typing import Any, Dict, Tuple

from ...utils.logging import (
    log_error,
    log_info,
    log_success,
    log_warning,
    print_plain,
)
from ...utils.ssh_manager import SSHConnectionManager


class RemoteDiskManager:
    """Disk-capacity helpers that run over SSH on the deploy target."""

    def __init__(self, ssh: SSHConnectionManager, remote_app_root: str) -> None:
        self.ssh = ssh
        self.remote_app_root = remote_app_root

    # ── SSH helper ───────────────────────────────────────────────────

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

    # ── Public API ───────────────────────────────────────────────────

    def _estimate_remote_disk_requirements(self, deploy_size_bytes: int) -> Dict[str, int]:
        app_required = max(int(deploy_size_bytes * 1.25), deploy_size_bytes + 512 * 1024 * 1024)
        docker_required = max(int(deploy_size_bytes * 2.5), deploy_size_bytes + 1024 * 1024 * 1024)
        return {
            "/app": app_required,
            "/var/lib/docker": docker_required,
        }

    def _check_remote_disk_capacity(
        self,
        *,
        username: str,
        server: str,
        deploy_size_bytes: int,
    ) -> Tuple[bool, Dict[str, Any]]:
        required_by_path = self._estimate_remote_disk_requirements(deploy_size_bytes)
        script = (
            "for p in /app /var/lib/docker; do "
            "probe=\"$p\"; "
            "while [ ! -e \"$probe\" ] && [ \"$probe\" != \"/\" ]; do probe=$(dirname \"$probe\"); done; "
            "a=$(df -B1 --output=avail \"$probe\" | tail -1 | tr -d ' '); "
            "echo \"$p:$a:$probe\"; "
            "done"
        )
        code, stdout, stderr = self._run_remote_text(username, server, script, timeout=30)
        report: Dict[str, Any] = {
            "required_bytes": required_by_path,
            "available_bytes": {},
            "probe_paths": {},
            "paths_checked": ["/app", "/var/lib/docker"],
            "deploy_size_bytes": deploy_size_bytes,
            "ok": False,
        }
        if code != 0:
            report["error"] = stderr.strip() or "Failed to check remote disk availability."
            return False, report

        for line in stdout.splitlines():
            if ":" not in line:
                continue
            parts = line.split(":", 2)
            path = parts[0].strip()
            value = parts[1].strip() if len(parts) > 1 else "0"
            probe_path = parts[2].strip() if len(parts) > 2 else path
            report["available_bytes"][path] = _safe_int(value)
            report["probe_paths"][path] = probe_path

        ok = True
        for path, required in required_by_path.items():
            available = _safe_int(report["available_bytes"].get(path, 0))
            if available < required:
                ok = False
        report["ok"] = ok
        if not ok:
            for path in ["/app", "/var/lib/docker"]:
                required = required_by_path[path]
                available = _safe_int(report["available_bytes"].get(path, 0))
                probe_path = report["probe_paths"].get(path, path)
                log_error(
                    f"Insufficient disk on {path}: required ~{required // (1024 * 1024)} MB, "
                    f"available {available // (1024 * 1024)} MB (probe: {probe_path})"
                )
        return ok, report

    def _log_remote_disk_diagnostics(self, username: str, server: str) -> None:
        commands = [
            "df -h /app /var/lib/docker 2>/dev/null || true",
            "docker system df 2>/dev/null || true",
        ]
        for cmd in commands:
            _, stdout, _ = self._run_remote_text(username, server, cmd, timeout=30)
            for line in stdout.splitlines():
                if line.strip():
                    log_error(f"  {line}")

    def _attempt_remote_disk_recovery(self, username: str, server: str, mode: str, force: bool = False) -> bool:
        safe_script = f"""
set -euo pipefail
find {self.remote_app_root} -type f -name '*.ilha-package.tar.gz' -delete 2>/dev/null || true
find {self.remote_app_root}/packages -type f -name '*.tar.gz' -delete 2>/dev/null || true
docker builder prune -f >/dev/null 2>&1 || true
docker image prune -f >/dev/null 2>&1 || true
docker container prune -f >/dev/null 2>&1 || true
"""
        aggressive_script = safe_script + "\ndocker system prune -af --volumes >/dev/null 2>&1 || true\n"
        selected = aggressive_script if mode == "aggressive" else safe_script

        if not force:
            actions = "prune Docker images/containers" if mode == "safe" else "prune ALL Docker data including volumes"
            log_warning(f"Disk recovery ({mode}) will {actions} on {server}")
            try:
                answer = input(f"Proceed with {mode} disk recovery? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    log_info("Disk recovery skipped by user")
                    return False
            except (EOFError, KeyboardInterrupt, OSError):
                log_info("Disk recovery skipped (non-interactive)")
                return False

        print_plain(f"Attempting remote disk recovery ({mode})...")
        code, _, stderr = self._run_remote_text(
            username, server, selected, timeout=180
        )
        if code != 0:
            log_error(f"Disk recovery failed: {stderr.strip()}")
            return False
        # Let overlayfs reclaim settle before retrying build
        time.sleep(1 + random.random())
        log_success("Remote disk recovery completed")
        return True

    def _print_disk_recovery_guidance(self, username: str, server: str) -> None:
        log_error("Disk exhaustion detected. Suggested recovery commands:")
        log_error(f"  ssh {username}@{server} \"df -h /app /var/lib/docker\"")
        log_error(f"  ssh {username}@{server} \"find {self.remote_app_root} -type f -name '*.ilha-package.tar.gz' -delete\"")
        log_error(f"  ssh {username}@{server} \"docker builder prune -f && docker image prune -f\"")


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
