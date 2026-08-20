#!/usr/bin/env python3
"""Application deployment script (slim, brief-downtime).

Runs on the TARGET server. Configuration comes from environment variables:
- APP_ROOT: Remote app directory (e.g. /app)
- WEB_SERVICE: Compose service name for primary web app
- WEB_PORT: Port web app listens on inside container
- HEALTH_PATH: HTTP path for health checks
- DB_SERVICE: Compose service for database (empty = no database tier)
- DEPS_SERVICES: Space-separated list of dependency services (empty = none)
- WORKER_SERVICES: Space-separated worker services (empty = none)
- FRONTEND_SERVICE: Compose service for the SPA frontend (empty = none)
- BUILD_WEB: "1" to build the web image before deploy, "" to skip

Strategy (brief downtime accepted):
1. (Optional) Bring up dependency services and wait for the database. Static
   sites and other db-less projects skip this step entirely.
2. (Optional) Build the web image.
3. ``compose up -d`` for the web service and any workers — compose recreates
   anything whose image or config changed. Apps with their own entrypoint
   (e.g. Django containers running migrations) are responsible for being
   ready before answering health checks.
4. Health-check the resulting web container.
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from deploy_utils import DeployLogger, DockerCommand


class RemoteDeployer:
    """Bring up dependencies (if any), run migrations, then start web/workers."""

    def __init__(self):
        self.logger = DeployLogger()
        self.docker = DockerCommand(self.logger)

        self.app_root = self._require_env("APP_ROOT")
        self.web_service = self._require_env("WEB_SERVICE")
        self.web_port = self._require_env("WEB_PORT")
        self.health_path = self._require_env("HEALTH_PATH")
        # db / deps / workers are now optional. An empty string here is the
        # convention for "no database tier" / "no dependencies".
        self.db_service = (os.getenv("DB_SERVICE") or "").strip()
        self.deps_services = [s for s in (os.getenv("DEPS_SERVICES") or "").split() if s]
        self.worker_services = [s for s in (os.getenv("WORKER_SERVICES") or "").split() if s]
        self.frontend_service = (os.getenv("FRONTEND_SERVICE") or "").strip()
        self.build_web = os.getenv("BUILD_WEB", "0") == "1"
        self.pull_images = os.getenv("PULL_IMAGES", "0") == "1"
        self.env_file = ".env"

        os.chdir(self.app_root)

    def _require_env(self, name: str) -> str:
        value = os.getenv(name)
        if not value:
            self.logger.error(f"{name} not set")
            sys.exit(1)
        return value

    def _persist_failure_diagnostics(self, container_id: Optional[str]) -> str:
        """Dump compose ps + recent web logs to ``/tmp/ilha-diagnostics/<ts>/``."""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        diag_dir = Path(f"/tmp/ilha-diagnostics/{ts}")
        diag_dir.mkdir(parents=True, exist_ok=True)

        def _write_cmd(label: str, argv: List[str]) -> None:
            out_path = diag_dir / f"{label}.txt"
            try:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, timeout=120
                )
                out_path.write_text(
                    (proc.stdout or "")
                    + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                out_path.write_text(f"collection failed: {exc}", encoding="utf-8")

        self.logger.info(f"Writing failure diagnostics to {diag_dir}")
        _write_cmd(
            "docker_compose_ps",
            [
                "docker", "compose", "--env-file", self.env_file,
                *DockerCommand.COMPOSE_FLAGS,
                "ps", "-a",
            ],
        )
        if container_id:
            _write_cmd(
                "docker_logs_web",
                ["docker", "logs", "--tail", "500", container_id],
            )
        (diag_dir / "README.txt").write_text(
            "Artifacts from ilha deploy failure (remote_deploy).\n",
            encoding="utf-8",
        )
        return str(diag_dir)

    def run(self) -> bool:
        self.logger.info("=== Deployment ===")

        if self.deps_services:
            self.logger.info(f"Step 1: Bringing up dependencies: {self.deps_services}")
            self.docker.compose(
                "up", ["-d"] + self.deps_services, env_file=self.env_file, check=False
            )
        else:
            self.logger.info("Step 1: No dependency services configured; skipping")

        if self.db_service:
            self.logger.info(f"Step 1: Waiting for {self.db_service} readiness...")
            if not self.docker.wait_for_postgres(self.db_service, env_file=self.env_file):
                self.logger.error("Database not ready")
                return False
        else:
            self.logger.info("Step 1: No database service configured; skipping readiness wait")

        if self.build_web:
            self.logger.info(f"Step 2: Building {self.web_service} image...")
            if (
                self.docker.compose(
                    "build", [self.web_service], env_file=self.env_file, check=False
                )
                is None
            ):
                self.logger.error(f"docker compose build {self.web_service} failed")
                return False
        else:
            self.logger.info("Step 2: Skipping image build (using existing image)")

        if self.pull_images:
            self.logger.info("Step 2b: Pulling compose images...")
            if (
                self.docker.compose(
                    "pull", [], env_file=self.env_file, check=False
                )
                is None
            ):
                self.logger.error("docker compose pull failed")
                return False

        services = [self.web_service] + self.worker_services
        if self.frontend_service:
            services.append(self.frontend_service)
        self.logger.info(f"Step 3: Bringing up {services}...")
        if (
            self.docker.compose(
                "up", ["-d"] + services, env_file=self.env_file, check=False
            )
            is None
        ):
            self.logger.error(f"docker compose up {services} failed")
            return False

        if not self.worker_services:
            self.logger.info("No workers configured")
        if not self.frontend_service:
            self.logger.info("No frontend service configured")

        # The container's entrypoint may run migrations + collectstatic before
        # uvicorn answers /health-check/. Allow up to 180s (90 × 2s) before
        # giving up — enough for a non-trivial first-deploy migration.
        self.logger.info(
            f"Step 4: Health checking {self.web_service} at "
            f":{self.web_port}{self.health_path}"
        )
        container_id = self.docker.ps_q(self.web_service, env_file=self.env_file)
        if not container_id:
            self.logger.error(f"{self.web_service} container not found after up")
            self._persist_failure_diagnostics(None)
            return False
        if not self.docker.health_check_http(
            container_id,
            int(self.web_port),
            self.health_path,
            max_attempts=90,
        ):
            diag_path = self._persist_failure_diagnostics(container_id)
            self.logger.error(f"Health check failed (diagnostics: {diag_path})")
            # `DockerCommand.run` captures stdout/stderr into a buffer that nobody
            # reads on the failure path — swallowing exactly the diagnostic we
            # need. Run docker logs directly and emit each line so it lands in
            # the deploy log streamed back to the operator.
            self.logger.error("--- web container logs (last 100 lines) ---")
            try:
                proc = subprocess.run(
                    ["docker", "logs", "--tail", "100", container_id],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                for stream in (proc.stdout or "", proc.stderr or ""):
                    for line in stream.splitlines():
                        if line.strip():
                            self.logger.error(f"  {line}")
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"  (failed to capture container logs: {exc})")
            self.logger.error("--- end web container logs ---")
            return False

        self.logger.success("=== Deployment Complete ===")
        self.docker.compose("ps", [], env_file=self.env_file)
        return True


def main() -> int:
    try:
        deployer = RemoteDeployer()
        success = deployer.run()
        return 0 if success else 1
    except Exception as e:  # noqa: BLE001
        DeployLogger().error(f"Deployment failed: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
