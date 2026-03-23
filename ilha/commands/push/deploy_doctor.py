"""
Deploy doctor diagnostics for ilha.

Collects health-check information from a remote deployment:
docker compose status, service logs, Caddy logs, smoke-test curl,
external domain reachability, and disk usage.
"""

import shlex
import subprocess
from typing import Dict, Any, List, Optional, Tuple

from ...utils.ssh_manager import SSHConnectionManager
from .remote_compose_probe import remote_compose_service_known


class DeployDoctor:
    """Runs diagnostic checks against a remote slim deployment."""

    def __init__(self, ssh: SSHConnectionManager, remote_app_root: str) -> None:
        self.ssh = ssh
        self.remote_app_root = remote_app_root
        self.remote_caddy_dir = f"{remote_app_root}/caddy"

    # ── small SSH / process helpers ──────────────────────────────────

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

    def _remote_service_exists(self, username: str, server: str, service_name: str) -> bool:
        return remote_compose_service_known(
            self._run_remote_text,
            username=username,
            server=server,
            remote_app_root=self.remote_app_root,
            service_name=service_name,
            timeout=15,
        )

    # ── smoke-probe helpers ──────────────────────────────────────────

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
        """Shell command to verify the app responds on port 8000."""
        norm = self._normalized_smoke_path(smoke_path)
        url = f"http://127.0.0.1:8000{norm}"
        tail = " >/dev/null" if quiet else " 2>&1"
        inner_curl = f"curl -fsS --max-time 5 {shlex.quote(url)}{tail}"
        if via_compose_web:
            root = shlex.quote(self.remote_app_root)
            return (
                f"cd {root} && docker compose --env-file .env exec -T web sh -c {shlex.quote(inner_curl)}"
            )
        return f"curl -fsS --max-time 5 {shlex.quote(url)}{tail}"

    # ── command collector ────────────────────────────────────────────

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

    # ── public API ───────────────────────────────────────────────────

    def doctor(
        self,
        *,
        username: str,
        server: str,
        smoke_path: str = "/",
        source_manifest: Optional[Any] = None,
        drift_report: Optional[Any] = None,
        domain: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run all diagnostic checks and return a structured report."""
        report: Dict[str, Any] = {
            "target": f"{username}@{server}",
            "source_manifest": source_manifest,
            "drift_report": drift_report,
            "smoke_path": smoke_path,
            "checks": [],
        }

        report["checks"].append(
            self._doctor_collect_command(
                "docker_compose_ps",
                f"cd {self.remote_app_root} && docker compose --env-file .env ps 2>&1",
                username=username,
                server=server,
                timeout=20,
            )
        )

        for service in ("web", "rq-worker-1"):
            if self._remote_service_exists(username, server, service):
                report["checks"].append(
                    self._doctor_collect_command(
                        f"{service}_logs",
                        f"cd {self.remote_app_root} && docker compose --env-file .env logs --tail 100 {shlex.quote(service)} 2>&1",
                        username=username,
                        server=server,
                        timeout=20,
                    )
                )
            else:
                report["checks"].append(
                    {
                        "title": f"{service}_logs",
                        "command": "",
                        "success": True,
                        "stdout": "",
                        "stderr": "",
                        "skipped": True,
                        "reason": f"Service {service} not present in compose config",
                    }
                )

        report["checks"].append(
            self._doctor_collect_command(
                "caddy_logs",
                f"cd {self.remote_caddy_dir} && docker compose -p ilha-proxy logs --tail 100 2>&1",
                username=username,
                server=server,
                timeout=20,
            )
        )
        use_web_exec = self._remote_service_exists(username, server, "web")
        report["checks"].append(
            self._doctor_collect_command(
                "local_app_curl",
                self._build_smoke_probe_command(
                    smoke_path, via_compose_web=use_web_exec, quiet=False
                ),
                username=username,
                server=server,
                timeout=15,
            )
        )
        if domain:
            report["checks"].append(
                self._doctor_collect_command(
                    "external_domain_curl",
                    f"curl -kfsSI --max-time 5 {shlex.quote(f'https://{domain}/')} 2>&1",
                    username=username,
                    server=server,
                    local=True,
                    timeout=15,
                )
            )
        report["checks"].append(
            self._doctor_collect_command(
                "disk_usage",
                "df -h /app /var/lib/docker 2>&1 || true",
                username=username,
                server=server,
                timeout=15,
            )
        )

        report["success"] = all(
            check.get("success", False) for check in report["checks"] if not check.get("skipped")
        )
        return report
