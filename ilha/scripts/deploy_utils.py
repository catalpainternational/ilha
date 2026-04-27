"""Shared utilities for deployment scripts - DRY, reusable functions."""

import subprocess
import json
import time
import sys
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime


def apt_stderr_indicates_lock(stderr: str, stdout: str = "") -> bool:
    """Return True if apt-get output suggests dpkg/apt lock contention (retryable)."""
    blob = f"{stderr or ''}\n{stdout or ''}".lower()
    markers = (
        "could not get lock",
        "unable to lock",
        "waiting for cache lock",
        "dpkg frontend is locked",
        "could not open lock file",
    )
    return any(m in blob for m in markers)


class DeployLogger:
    """Simple logging utility with timestamps."""

    def __init__(self, output_stream=sys.stderr):
        self.stream = output_stream

    def _log(self, level: str, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {level} {message}", file=self.stream, flush=True)

    def info(self, message: str) -> None:
        self._log("INFO ", message)

    def success(self, message: str) -> None:
        self._log("✓    ", message)

    def warning(self, message: str) -> None:
        self._log("⚠    ", message)

    def error(self, message: str) -> None:
        self._log("✗    ", message)


class DockerCommand:
    """Wrapper for docker commands with error handling."""

    def __init__(self, logger: Optional[DeployLogger] = None):
        self.logger = logger or DeployLogger()

    def run(
        self,
        args: List[str],
        capture_output: bool = False,
        check: bool = True,
    ) -> Optional[str]:
        """Run docker command and return output if capture_output=True.

        Args:
            args: List starting with 'docker' or 'docker compose', etc.
            capture_output: If True, return stdout as string
            check: If True, raise on non-zero exit code

        Returns:
            Output string if capture_output=True, empty string on non-capturing
            success, else None when check=False and the command fails.

        Raises:
            subprocess.CalledProcessError if check=True and command fails
        """
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=check,
            )
            if result.returncode != 0:
                return None
            if capture_output:
                return result.stdout.strip()
            return ""
        except FileNotFoundError:
            if check:
                raise
            return None
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Command failed: {' '.join(args)}")
            if e.stderr:
                self.logger.error(f"stderr: {e.stderr}")
            if check:
                raise
            return None

    def compose(
        self,
        subcommand: str,
        args: List[str] = [],
        env_file: str = ".env",
        capture_output: bool = False,
        check: bool = True,
    ) -> Optional[str]:
        """Run docker compose command.

        Args:
            subcommand: 'up', 'down', 'ps', 'logs', etc.
            args: Additional arguments for the subcommand
            env_file: Path to .env file
            capture_output: If True, return stdout
            check: If True, raise on failure

        Returns:
            Output if capture_output=True, else None
        """
        cmd = ["docker", "compose", "--env-file", env_file, subcommand] + args
        return self.run(cmd, capture_output=capture_output, check=check)

    def ps_json(
        self, service: str = "", env_file: str = ".env"
    ) -> List[Dict[str, Any]]:
        """Get docker compose ps output as JSON.

        Args:
            service: Specific service to query, or empty for all
            env_file: Path to .env file

        Returns:
            List of container dicts from JSON output
        """
        args = [service, "--format", "json"] if service else ["--format", "json"]
        output = self.compose("ps", args, env_file=env_file, capture_output=True, check=False)
        if not output:
            return []
        try:
            data = json.loads(output)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            return []

    def exec(
        self,
        container: str,
        cmd: List[str],
        capture_output: bool = False,
        check: bool = True,
    ) -> Optional[str]:
        """Execute command inside container.

        Args:
            container: Container name or ID
            cmd: Command to execute (as list)
            capture_output: If True, return stdout
            check: If True, raise on failure

        Returns:
            Output if capture_output=True, else None
        """
        full_cmd = ["docker", "exec", container] + cmd
        return self.run(full_cmd, capture_output=capture_output, check=check)

    def ps_q(
        self, service: str, env_file: str = ".env"
    ) -> Optional[str]:
        """Get container ID from docker compose ps.

        Args:
            service: Service name
            env_file: Path to .env file

        Returns:
            Container ID or None
        """
        output = self.compose(
            "ps", ["-q", service], env_file=env_file, capture_output=True, check=False
        )
        return output.split('\n')[0] if output else None

    def wait_for_postgres(
        self,
        service: str,
        env_file: str = ".env",
        max_attempts: int = 60,
        interval: int = 2,
    ) -> bool:
        """Wait for PostgreSQL service to be ready.

        Args:
            service: Postgres service name
            env_file: Path to .env file
            max_attempts: Max number of checks
            interval: Seconds between checks

        Returns:
            True if ready, False if timeout
        """
        for attempt in range(1, max_attempts + 1):
            container_id = self.ps_q(service, env_file=env_file)
            if not container_id:
                self.logger.info(f"Postgres container not found yet (attempt {attempt}/{max_attempts})")
                time.sleep(interval)
                continue

            try:
                self.exec(container_id, ["pg_isready", "-q"], check=True)
                self.logger.success(f"Postgres ready (attempt {attempt})")
                return True
            except subprocess.CalledProcessError:
                self.logger.info(f"Postgres not ready yet (attempt {attempt}/{max_attempts})")
                time.sleep(interval)

        self.logger.error(f"Postgres not ready after {max_attempts} attempts")
        return False

    def health_check_http(
        self,
        container: str,
        port: int,
        path: str = "/",
        max_attempts: int = 30,
        interval: int = 2,
        timeout: int = 5,
    ) -> bool:
        """Health check service via HTTP curl inside container.

        Args:
            container: Container name or ID
            port: Port to check (inside container)
            path: HTTP path to check
            max_attempts: Max number of checks
            interval: Seconds between checks
            timeout: Curl timeout in seconds

        Returns:
            True if healthy, False if timeout
        """
        for attempt in range(1, max_attempts + 1):
            try:
                self.exec(
                    container,
                    [
                        "curl",
                        "-fsS",
                        f"--max-time",
                        str(timeout),
                        f"http://127.0.0.1:{port}{path}",
                    ],
                    check=True,
                )
                self.logger.success(f"Health check passed (attempt {attempt})")
                return True
            except subprocess.CalledProcessError:
                self.logger.info(f"Health check attempt {attempt}/{max_attempts} failed")
                if attempt < max_attempts:
                    time.sleep(interval)

        self.logger.error(f"Health check failed after {max_attempts} attempts")
        return False

    def network_connect(self, container: str, network: str) -> bool:
        """Connect container to network.

        Args:
            container: Container name or ID
            network: Network name

        Returns:
            True if successful, False otherwise
        """
        try:
            self.run(["docker", "network", "connect", network, container], check=True)
            self.logger.success(f"Connected {container} to network {network}")
            return True
        except subprocess.CalledProcessError as e:
            err = ((e.stderr or "") + (e.stdout or "")).lower()
            if "already exists" in err or "endpoint with name" in err:
                self.logger.warning(
                    f"{container} already attached to {network} (treating as success)"
                )
                return True
            self.logger.error(f"Failed to connect {container} to network {network}")
            return False

    def container_remove(self, container: str, force: bool = True) -> bool:
        """Remove container.

        Args:
            container: Container name or ID
            force: Use -f flag

        Returns:
            True if successful (or already removed), False on error
        """
        args = ["docker", "rm"]
        if force:
            args.append("-f")
        args.append(container)
        self.run(args, check=False)
        return True

    def container_start(self, container: str) -> bool:
        """Start container.

        Args:
            container: Container name or ID

        Returns:
            True if successful, False otherwise
        """
        try:
            self.run(["docker", "start", container], check=True)
            self.logger.success(f"Started {container}")
            return True
        except subprocess.CalledProcessError:
            self.logger.error(f"Failed to start {container}")
            return False

    def container_stop(self, container: str) -> bool:
        """Stop container.

        Args:
            container: Container name or ID

        Returns:
            True if successful, False otherwise
        """
        try:
            self.run(["docker", "stop", container], check=True)
            self.logger.success(f"Stopped {container}")
            return True
        except subprocess.CalledProcessError:
            self.logger.warning(f"Failed to stop {container}")
            return False


class CaddyManager:
    """Manage Caddy reverse proxy configuration."""

    def __init__(self, logger: Optional[DeployLogger] = None, admin_addr: str = "http://localhost:2019"):
        self.logger = logger or DeployLogger()
        self.admin_addr = admin_addr

    def get_config(self) -> Optional[Dict[str, Any]]:
        """Get current Caddy configuration.

        Returns:
            Config dict or None if failed
        """
        try:
            result = subprocess.run(
                ["curl", "-sf", f"{self.admin_addr}/config/"],
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            self.logger.warning("Failed to get Caddy config")
            return None

    def swap_upstream(self, old_target: str, new_target: str) -> bool:
        """Swap Caddy reverse proxy upstream target.

        Args:
            old_target: Current upstream (e.g., "web:8000")
            new_target: New upstream (e.g., "web_canary:8000")

        Returns:
            True if successful, False otherwise
        """
        config = self.get_config()
        if not config:
            self.logger.warning("Cannot swap upstream without config")
            return False

        # Traverse config structure and replace upstreams
        swapped = False
        routes = (
            config.get("apps", {})
            .get("http", {})
            .get("servers", {})
            .get("srv0", {})
            .get("routes", [])
        )

        for route in routes:
            for handler in route.get("handle", []):
                if handler.get("handler") == "reverse_proxy":
                    for upstream in handler.get("upstreams", []):
                        if upstream.get("dial") == old_target:
                            upstream["dial"] = new_target
                            swapped = True

                for subroute in handler.get("routes", []):
                    for sh in subroute.get("handle", []):
                        if sh.get("handler") == "reverse_proxy":
                            for upstream in sh.get("upstreams", []):
                                if upstream.get("dial") == old_target:
                                    upstream["dial"] = new_target
                                    swapped = True

        if not swapped:
            self.logger.warning(f"No upstream {old_target} found in Caddy config")
            return False

        # POST updated config
        try:
            subprocess.run(
                [
                    "curl",
                    "-sf",
                    "-X",
                    "POST",
                    f"{self.admin_addr}/load",
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    json.dumps(config),
                ],
                capture_output=True,
                check=True,
            )
            self.logger.success(f"Swapped {old_target} → {new_target}")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to update Caddy config: {e.stderr}")
            return False


class RemoteSystemCommand:
    """Utilities for remote system commands (apt, firewall, etc.)."""

    def __init__(self, logger: Optional[DeployLogger] = None):
        self.logger = logger or DeployLogger()

    def run(
        self,
        args: List[str],
        capture_output: bool = False,
        check: bool = False,
    ) -> Optional[str]:
        """Run system command.

        Args:
            args: Command as list
            capture_output: If True, return stdout
            check: If True, raise on failure

        Returns:
            Output if capture_output=True, empty string on non-capturing
            success, else None when check=False and the command fails.
        """
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=check,
            )
            if result.returncode != 0:
                return None
            if capture_output:
                return result.stdout.strip()
            return ""
        except FileNotFoundError:
            if check:
                raise
            return None
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Command failed: {' '.join(args)}")
            if e.stderr:
                self.logger.error(f"stderr: {e.stderr}")
            if check:
                raise
            return None

    def command_exists(self, cmd: str) -> bool:
        """Check if command exists in PATH.

        Args:
            cmd: Command name

        Returns:
            True if command exists, False otherwise
        """
        return shutil.which(cmd) is not None

    def apt_update(self, timeout: int = 300) -> bool:
        """Wait for apt lock and update.

        Retries only when stderr/stdout suggests lock contention; other errors
        fail fast with diagnostics.

        Args:
            timeout: Max seconds to wait for lock

        Returns:
            True if successful, False if timeout or unrecoverable apt error
        """
        waited = 0
        while waited < timeout:
            try:
                result = subprocess.run(
                    ["apt-get", "-qq", "update"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except FileNotFoundError:
                self.logger.error("apt-get not found on PATH")
                return False
            if result.returncode == 0:
                self.logger.success(f"apt ready (waited {waited}s)")
                return True
            stderr = result.stderr or ""
            stdout = result.stdout or ""
            if apt_stderr_indicates_lock(stderr, stdout):
                self.logger.info("apt/dpkg lock held, waiting 10s...")
                time.sleep(10)
                waited += 10
                continue
            self.logger.error("apt-get update failed (non-lock error); not retrying.")
            if stderr.strip():
                self.logger.error(f"stderr: {stderr.strip()}")
            if stdout.strip():
                self.logger.error(f"stdout: {stdout.strip()}")
            return False

        self.logger.error(f"Timeout waiting for apt lock after {timeout}s")
        return False

    def dpkg_wait(self, timeout: int = 120) -> bool:
        """Wait for dpkg/apt processes to finish.

        Args:
            timeout: Max seconds to wait

        Returns:
            True if processes finished, False if timeout
        """
        waited = 0
        while waited < timeout:
            # Lock files are normally present on Ubuntu; only wait while a
            # package manager process is actively running.
            apt_check = self.run(["pgrep", "-x", "apt"], check=False)
            apt_get_check = self.run(["pgrep", "-x", "apt-get"], check=False)
            dpkg_check = self.run(["pgrep", "-x", "dpkg"], check=False)
            unattended_check = self.run(
                ["pgrep", "-f", "^/usr/bin/python3 /usr/bin/unattended-upgrade"],
                check=False,
            )

            if (
                apt_check is None
                and apt_get_check is None
                and dpkg_check is None
                and unattended_check is None
            ):
                if waited > 0:
                    self.logger.success(f"dpkg idle (waited {waited}s)")
                return True

            self.logger.info("dpkg/apt still in use, waiting 5s...")
            time.sleep(5)
            waited += 5

        self.logger.error(f"Timeout waiting for dpkg/apt after {timeout}s")
        return False
