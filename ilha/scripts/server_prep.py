#!/usr/bin/env python3
"""Slim server preparation script for ilha deploy.

Installs ONLY Docker + Docker Compose v2 on a fresh Ubuntu server.
No Python, git, or ilha required on the target.

Steps:
1. Mask apt-daily timers (so they can't fire mid-prep and race with our apt calls)
2. Wait for apt/dpkg lock (any in-flight unattended-upgrades on first boot)
3. Configure firewall (UFW or iptables)
4. Install Docker Engine and Compose v2
5. Create ilha Docker network
6. Create Caddy volumes
7. Create swap file
"""

import sys
import subprocess
import time
from typing import Optional

from deploy_utils import DeployLogger, RemoteSystemCommand


class ServerPrep:
    """Prepares a fresh Ubuntu server for ilha deployment."""

    def __init__(self):
        self.logger = DeployLogger()
        self.sys = RemoteSystemCommand(self.logger)

    # systemd units that auto-run apt and race with our prep on first boot.
    _APT_TIMER_UNITS = ("apt-daily.timer", "apt-daily-upgrade.timer")
    _APT_SERVICE_UNITS = ("apt-daily.service", "apt-daily-upgrade.service")

    def run(self) -> bool:
        """Execute full preparation.

        Returns:
            True if successful, False on failure
        """
        self.logger.info("=== ilha Slim Server Preparation ===")
        self.logger.info("Installing: Docker Engine + Compose v2 + firewall")

        self._disable_apt_timers()
        try:
            return self._run_steps()
        finally:
            self._restore_apt_timers()

    def _run_steps(self) -> bool:
        # Wait for any currently-running unattended-upgrade to finish.
        # The mask above prevents new ones from starting mid-prep.
        if not self.sys.apt_update():
            return False
        if not self.sys.dpkg_wait():
            return False

        if not self._setup_firewall():
            return False

        if not self._install_docker():
            return False

        if not self._create_docker_network():
            return False

        if not self._create_caddy_volumes():
            return False

        if not self._setup_swap():
            return False

        self.logger.success("=== Slim server preparation complete ===")
        return True

    def _disable_apt_timers(self) -> None:
        """Mask apt-daily timers and services so they can't fire mid-prep.

        Masking (not just stopping) is required: systemd will otherwise re-fire
        the timer the moment we release control. A currently-running invocation
        is left alone — it'll be drained by apt_update/dpkg_wait below.
        """
        self.logger.info("--- Masking apt-daily timers ---")
        units = self._APT_TIMER_UNITS + self._APT_SERVICE_UNITS
        self.sys.run(["systemctl", "stop", *units], check=False)
        self.sys.run(["systemctl", "mask", *units], check=False)

    def _restore_apt_timers(self) -> None:
        """Unmask and re-enable apt-daily timers.

        Best-effort: a failure here shouldn't override the run's outcome, so we
        log and move on. Worst case the operator runs `systemctl unmask` later.
        """
        units = self._APT_TIMER_UNITS + self._APT_SERVICE_UNITS
        result = self.sys.run(["systemctl", "unmask", *units], check=False)
        if result is None:
            self.logger.warning(
                "Could not unmask apt-daily units — run "
                "`systemctl unmask apt-daily.timer apt-daily-upgrade.timer "
                "apt-daily.service apt-daily-upgrade.service` manually."
            )
            return
        self.sys.run(["systemctl", "start", *self._APT_TIMER_UNITS], check=False)

    def _setup_firewall(self) -> bool:
        """Configure firewall (UFW or iptables).

        Returns:
            True if successful
        """
        self.logger.info("--- Configuring firewall ---")

        if self.sys.command_exists("ufw"):
            return self._setup_ufw()
        elif self.sys.command_exists("iptables"):
            return self._setup_iptables()
        else:
            self.logger.warning("No firewall tool found, skipping")
            return True

    def _setup_ufw(self) -> bool:
        """Setup UFW firewall.

        Returns:
            True if successful
        """
        try:
            # Set defaults
            self.sys.run(["ufw", "default", "deny", "incoming"], check=False)
            self.sys.run(["ufw", "default", "allow", "outgoing"], check=False)

            # CRITICAL: SSH rule must succeed before enabling
            result = self.sys.run(["ufw", "allow", "22/tcp"], check=False)
            if result is None:
                self.logger.error("Failed to add UFW SSH rule — aborting")
                return False

            # Add HTTP/HTTPS
            self.sys.run(["ufw", "allow", "80/tcp"], check=False)
            self.sys.run(["ufw", "allow", "443/tcp"], check=False)

            # Enable
            self.sys.run(["ufw", "--force", "enable"], check=False)

            # Verify SSH rule still exists
            output = self.sys.run(["ufw", "status"], capture_output=True, check=False)
            if output and "22/tcp" in output and "ALLOW" in output:
                self.logger.success("UFW configured (22, 80, 443 open)")
                return True
            else:
                self.logger.error("UFW enabled but SSH rule missing — disabling to prevent lockout")
                self.sys.run(["ufw", "disable"], check=False)
                return False
        except Exception as e:
            self.logger.error(f"UFW setup failed: {e}")
            return False

    def _setup_iptables(self) -> bool:
        """Setup iptables firewall.

        Returns:
            True if successful
        """
        try:
            for port in [22, 80, 443]:
                self.sys.run(
                    ["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"],
                    check=False,
                )
            self.logger.success("iptables rules added (22, 80, 443 open)")
            return True
        except Exception as e:
            self.logger.error(f"iptables setup failed: {e}")
            return False

    def _install_docker(self) -> bool:
        """Install Docker Engine and Compose v2.

        Returns:
            True if successful
        """
        self.logger.info("--- Installing Docker ---")

        # Check if already installed
        docker_check = self.sys.run(["docker", "--version"], capture_output=True, check=False)
        compose_check = self.sys.run(["docker", "compose", "version"], capture_output=True, check=False)

        if docker_check and compose_check:
            self.logger.success("Docker and Docker Compose already installed")
            if docker_check:
                self.logger.info(docker_check)
            if compose_check:
                self.logger.info(compose_check)
            return True

        self.logger.info("Installing Docker Engine and Compose v2...")

        try:
            # Download and run get.docker.com
            result = subprocess.run(
                "curl -fsSL https://get.docker.com | sh",
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                self.logger.error(f"Docker installation failed: {result.stderr}")
                return False

            # Enable and start Docker daemon
            self.sys.run(["systemctl", "enable", "docker"], check=False)
            self.sys.run(["systemctl", "start", "docker"], check=False)

            # Verify installation
            docker_ver = self.sys.run(["docker", "--version"], capture_output=True, check=False)
            compose_ver = self.sys.run(["docker", "compose", "version"], capture_output=True, check=False)

            if docker_ver and compose_ver:
                self.logger.success("Docker installed")
                self.logger.info(docker_ver)
                self.logger.info(compose_ver)
                return True
            else:
                self.logger.error("Docker installation verification failed")
                return False
        except Exception as e:
            self.logger.error(f"Docker installation failed: {e}")
            return False

    def _create_docker_network(self) -> bool:
        """Create Docker network for Caddy.

        Returns:
            True if successful
        """
        self.logger.info("--- Creating Docker network ---")

        try:
            # Check if network exists
            result = self.sys.run(
                ["docker", "network", "inspect", "ilha_caddy_proxy"],
                check=False,
            )
            if result is not None:
                self.logger.success("Docker network 'ilha_caddy_proxy' already exists")
                return True

            # Create network
            self.sys.run(
                ["docker", "network", "create", "ilha_caddy_proxy"],
                check=True,
            )
            self.logger.success("Docker network 'ilha_caddy_proxy' created")
            return True
        except Exception as e:
            self.logger.error(f"Network creation failed: {e}")
            return False

    def _create_caddy_volumes(self) -> bool:
        """Create Docker volumes for Caddy.

        Returns:
            True if successful
        """
        self.logger.info("--- Creating Caddy volumes ---")

        try:
            for volume in ["ilha_caddy_data", "ilha_caddy_config"]:
                result = self.sys.run(
                    ["docker", "volume", "inspect", volume],
                    check=False,
                )
                if result is not None:
                    self.logger.success(f"Docker volume '{volume}' already exists")
                    continue

                self.sys.run(
                    ["docker", "volume", "create", volume],
                    check=True,
                )
                self.logger.success(f"Docker volume '{volume}' created")

            return True
        except Exception as e:
            self.logger.error(f"Volume creation failed: {e}")
            return False

    def _setup_swap(self) -> bool:
        """Create swap file for memory-constrained servers.

        Returns:
            True if successful
        """
        self.logger.info("--- Setting up swap ---")

        try:
            # Check if swap already exists
            result = self.sys.run(
                ["swapon", "--show"],
                capture_output=True,
                check=False,
            )
            if result and "/swapfile" in result:
                self.logger.success("Swap already configured")
                return True

            self.logger.info("Creating 512MB swap...")

            # Create swapfile
            self.sys.run(
                ["fallocate", "-l", "512M", "/swapfile"],
                check=True,
            )

            # Set permissions
            self.sys.run(
                ["chmod", "600", "/swapfile"],
                check=True,
            )

            # Format as swap
            self.sys.run(
                ["mkswap", "/swapfile"],
                check=True,
            )

            # Enable swap
            self.sys.run(
                ["swapon", "/swapfile"],
                check=True,
            )

            # Add to fstab for persistence
            with open("/etc/fstab", "a") as f:
                f.write("\n/swapfile none swap sw 0 0\n")

            self.logger.success("512MB swap enabled")
            return True
        except Exception as e:
            self.logger.error(f"Swap setup failed: {e}")
            return False


def main() -> int:
    """Main entry point."""
    try:
        prep = ServerPrep()
        success = prep.run()
        return 0 if success else 1
    except Exception as e:
        DeployLogger().error(f"Server preparation failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
