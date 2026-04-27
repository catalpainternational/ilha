#!/usr/bin/env python3
"""Zero-downtime application deployment script.

Runs on the TARGET server. Configuration comes from environment variables:
- APP_ROOT: Remote app directory (e.g. /app)
- WEB_SERVICE: Compose service name for primary web app
- WEB_PORT: Port web app listens on inside container
- HEALTH_PATH: HTTP path for health checks
- DB_SERVICE: Compose service for database (used for readiness check)
- DEPS_SERVICES: Space-separated list of dependency services to bring up
- WORKER_SERVICES: Space-separated worker services to recreate (optional)
- BUILD_WEB: "1" to build image in phase 1, "" to skip

10-Phase Strategy:
1. Build new image while old container serves traffic
1b. Bring up dependency services; wait for DB readiness
2. Start canary container from new image
3. Health-check canary
4. Pause Caddy monitor
5. Swap Caddy upstream to canary
6. Recreate compose-managed web service
7. Health-check recreated service
8. Swap Caddy upstream to recreated service
9. Cleanup and resume monitor
10. Recreate worker services (if any)
"""

import sys
import os
import atexit
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from deploy_utils import DeployLogger, DockerCommand, CaddyManager


class RemoteDeployer:
    """Orchestrates zero-downtime deployment phases."""

    def __init__(self):
        self.logger = DeployLogger()
        self.docker = DockerCommand(self.logger)
        self.caddy = CaddyManager(self.logger)

        # Load and validate environment
        self.app_root = self._require_env("APP_ROOT")
        self.web_service = self._require_env("WEB_SERVICE")
        self.web_port = self._require_env("WEB_PORT")
        self.health_path = self._require_env("HEALTH_PATH")
        self.db_service = self._require_env("DB_SERVICE")
        self.deps_services = self._require_env("DEPS_SERVICES").split()
        self.worker_services = os.getenv("WORKER_SERVICES", "").split()
        self.build_web = os.getenv("BUILD_WEB", "0") == "1"
        self.env_file = ".env"

        # Change to app root
        os.chdir(self.app_root)

        # Register cleanup on exit
        atexit.register(self._cleanup)

    def _persist_canary_diagnostics(self) -> str:
        """Write canary failure artifacts under ``/tmp/ilha-diagnostics/<timestamp>/``."""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        diag_dir = Path(f"/tmp/ilha-diagnostics/{ts}")
        diag_dir.mkdir(parents=True, exist_ok=True)

        def _write_cmd(label: str, argv: list) -> None:
            out_path = diag_dir / f"{label}.txt"
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                out_path.write_text(
                    (proc.stdout or "")
                    + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                out_path.write_text(f"collection failed: {exc}", encoding="utf-8")

        self.logger.info(f"Writing canary diagnostics to {diag_dir}")
        _write_cmd(
            "docker_logs_web_zd_canary",
            ["docker", "logs", "--tail", "500", "web_zd_canary"],
        )
        _write_cmd(
            "docker_inspect_web_zd_canary",
            ["docker", "inspect", "web_zd_canary"],
        )
        _write_cmd(
            "docker_compose_ps",
            [
                "docker",
                "compose",
                "--env-file",
                self.env_file,
                "ps",
                "-a",
            ],
        )
        (diag_dir / "README.txt").write_text(
            "Artifacts from ilha canary health failure (remote_deploy).\n",
            encoding="utf-8",
        )
        return str(diag_dir)

    def _require_env(self, name: str) -> str:
        """Get required environment variable.

        Args:
            name: Variable name

        Returns:
            Variable value

        Raises:
            SystemExit if not set
        """
        value = os.getenv(name)
        if not value:
            self.logger.error(f"{name} not set")
            sys.exit(1)
        return value

    def _cleanup(self) -> None:
        """Cleanup on exit - remove canary, restart monitor."""
        self.docker.container_remove("web_zd_canary")
        self.docker.container_start("caddy_monitor")
        for path in ["/tmp/ilha_canary.env", "/tmp/ilha_caddy_swap.py"]:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

    def _get_service_image(self, service: str) -> Optional[str]:
        """Get Docker image name for compose service.

        Args:
            service: Service name

        Returns:
            Image name or None if not found
        """
        # Try docker compose images command first
        output = self.docker.compose(
            "images",
            [service, "--format", "json"],
            env_file=self.env_file,
            capture_output=True,
            check=False,
        )
        if output:
            try:
                import json
                data = json.loads(output)
                if isinstance(data, list) and data:
                    return f"{data[0]['Repository']}:{data[0]['Tag']}"
            except Exception:
                pass

        # Fallback: parse from docker-compose config
        output = self.docker.compose(
            "config",
            ["--format", "json"],
            env_file=self.env_file,
            capture_output=True,
            check=False,
        )
        if output:
            try:
                import json
                config = json.loads(output)
                return config.get("services", {}).get(service, {}).get("image")
            except Exception:
                pass

        return None

    def _prepare_canary_env(self) -> bool:
        """Prepare environment file for canary container.

        Copies .env and cleans up quotes, rewrites internal DNS names.

        Returns:
            True if successful
        """
        try:
            # Read .env
            env_file = Path(self.env_file)
            env_content = env_file.read_text()

            # Strip quotes from values
            cleaned = []
            for line in env_content.split('\n'):
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    value = value.strip('\'"')
                    cleaned.append(f"{key}={value}")
                else:
                    cleaned.append(line)

            # Rewrite internal DNS names for canary (not in compose network)
            content = '\n'.join(cleaned)

            # Get compose project name
            import re
            match = re.search(r'^COMPOSE_PROJECT_NAME=(.+)$', content, re.MULTILINE)
            if match:
                project_name = match.group(1).strip()
                content = content.replace("@db:", f"@{project_name}-db:")
                content = content.replace("@redis:", f"@{project_name}-redis:")
                content = re.sub(r'^DB_HOST=db$', f"DB_HOST={project_name}-db", content, flags=re.MULTILINE)
                content = re.sub(r'^REDIS_HOST=redis$', f"REDIS_HOST={project_name}-redis", content, flags=re.MULTILINE)

            # Write canary env file
            Path("/tmp/ilha_canary.env").write_text(content)
            self.logger.success("Canary .env prepared")
            return True
        except Exception as e:
            self.logger.error(f"Failed to prepare canary .env: {e}")
            return False

    def _get_service_container_name(self, service: str) -> Optional[str]:
        """Get running container name for service.

        Args:
            service: Service name

        Returns:
            Container name or None
        """
        containers = self.docker.ps_json(service, env_file=self.env_file)
        if containers:
            return containers[0].get("Names") or containers[0].get("Name")
        return None

    def _get_app_network(self, service: str) -> Optional[str]:
        """Get compose app network from service.

        Args:
            service: Service name

        Returns:
            Network name or None
        """
        container_id = self.docker.ps_q(service, env_file=self.env_file)
        if not container_id:
            return None

        output = self.docker.run(
            [
                "docker",
                "inspect",
                container_id,
                "--format",
                "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}",
            ],
            capture_output=True,
            check=False,
        )
        if output:
            for network in output.split():
                if "caddy" not in network:
                    return network
        return None

    def run(self) -> bool:
        """Execute full deployment.

        Returns:
            True if successful, False on failure
        """
        self.logger.info("=== Zero-Downtime Deployment ===")

        # Phase 1: Build image
        self.logger.info("Phase 1: Building image...")
        if self.build_web:
            self.docker.compose("build", [self.web_service], env_file=self.env_file)
        else:
            self.logger.info("Skipping build (using existing image)")

        # Phase 1b: Bring up dependencies and wait for DB
        self.logger.info(f"Phase 1b: Ensuring dependency services: {self.deps_services}")
        self.docker.compose("up", ["-d"] + self.deps_services, env_file=self.env_file, check=False)

        self.logger.info(f"Phase 1b: Waiting for {self.db_service} readiness...")
        if not self.docker.wait_for_postgres(self.db_service, env_file=self.env_file):
            self.logger.error("Database not ready")
            return False

        # Phase 2: Get image and start canary
        self.logger.info(f"Phase 2: Starting canary container for {self.web_service}...")
        image = self._get_service_image(self.web_service)
        if not image:
            self.logger.error(f"Could not determine image for {self.web_service}")
            return False
        self.logger.info(f"Using image: {image}")

        if not self._prepare_canary_env():
            return False

        self.docker.container_remove("web_zd_canary")
        self.docker.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                "web_zd_canary",
                "--env-file",
                "/tmp/ilha_canary.env",
                "-e",
                "SKIP_MIGRATIONS=true",
                "-e",
                "STARTUP_PERFORMANCE_MODE=true",
                "--network",
                "ilha_caddy_proxy",
                image,
            ]
        )

        # Connect canary to app network
        app_network = self._get_app_network(self.db_service)
        if not app_network:
            self.logger.error(f"Could not determine app network from {self.db_service}")
            return False

        if not self.docker.network_connect("web_zd_canary", app_network):
            self.logger.error(f"Could not connect canary to {app_network}")
            return False

        # Phase 3: Health check canary
        self.logger.info(f"Phase 3: Health checking canary :{ self.web_port}{self.health_path}...")
        if not self.docker.health_check_http(
            "web_zd_canary", int(self.web_port), self.health_path
        ):
            diag_path = self._persist_canary_diagnostics()
            self.logger.error(
                f"Canary health check failed, rolling back (diagnostics: {diag_path})"
            )
            self.docker.run(["docker", "logs", "--tail", "30", "web_zd_canary"], check=False)
            return False

        # Phase 4: Pause Caddy monitor
        self.logger.info("Phase 4: Pausing Caddy monitor...")
        self.docker.container_stop("caddy_monitor")

        # Phase 5: Swap to canary
        self.logger.info("Phase 5: Swapping traffic to canary...")
        old_web_name = self._get_service_container_name(self.web_service)
        if old_web_name:
            success = self.caddy.swap_upstream(
                f"{old_web_name}:{self.web_port}",
                f"web_zd_canary:{self.web_port}",
            )
            if not success:
                self.logger.warning("Caddy swap to canary failed, continuing...")

        # Phase 6: Recreate web service
        self.logger.info(f"Phase 6: Recreating {self.web_service} service...")
        self.docker.compose(
            "up",
            ["-d", "--no-deps", "--force-recreate", self.web_service],
            env_file=self.env_file,
        )

        # Connect new web to Caddy network
        for attempt in range(1, 4):
            container_id = self.docker.ps_q(self.web_service, env_file=self.env_file)
            if container_id:
                self.docker.network_connect(container_id, "ilha_caddy_proxy")
                break
            if attempt < 3:
                import time
                time.sleep(5)

        # Phase 7: Health check new web
        self.logger.info(f"Phase 7: Health checking new {self.web_service}...")
        container_id = self.docker.ps_q(self.web_service, env_file=self.env_file)
        if not container_id or not self.docker.health_check_http(
            container_id, int(self.web_port), self.health_path
        ):
            self.logger.error(
                f"New {self.web_service} failed health check, keeping canary as fallback"
            )
            if container_id:
                self.docker.run(["docker", "logs", "--tail", "40", container_id], check=False)
            # Keep canary alive and restart monitor
            atexit.cancel(self._cleanup)
            self.docker.container_start("caddy_monitor")
            return False

        # Phase 8: Swap to new web
        self.logger.info("Phase 8: Swapping traffic to new web...")
        new_web_name = self._get_service_container_name(self.web_service)
        if new_web_name:
            success = self.caddy.swap_upstream(
                f"web_zd_canary:{self.web_port}",
                f"{new_web_name}:{self.web_port}",
            )
            if not success:
                self.logger.warning("Caddy swap to new web failed, continuing...")

        # Phase 9: Cleanup
        self.logger.info("Phase 9: Cleanup...")
        self.docker.container_remove("web_zd_canary")
        self.docker.container_start("caddy_monitor")

        # Phase 10: Restart workers
        if self.worker_services:
            self.logger.info(f"Phase 10: Restarting workers: {self.worker_services}...")
            self.docker.compose(
                "up",
                ["-d", "--no-deps", "--force-recreate"] + self.worker_services,
                env_file=self.env_file,
                check=False,
            )
        else:
            self.logger.info("Phase 10: No workers configured")

        self.logger.success("=== Deployment Complete ===")
        self.docker.compose("ps", [], env_file=self.env_file)
        return True


def main() -> int:
    """Main entry point."""
    try:
        deployer = RemoteDeployer()
        success = deployer.run()
        return 0 if success else 1
    except Exception as e:
        DeployLogger().error(f"Deployment failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
