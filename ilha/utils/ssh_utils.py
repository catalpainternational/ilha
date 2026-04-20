"""
SSH utilities for ilha CLI.

This module provides utilities for managing SSH connections, including
automatic host key management.
"""

import hashlib
import base64
import subprocess
import os
from pathlib import Path
from typing import Optional

from .logging import log_info, log_warning, log_error


def _compute_key_fingerprints(keyscan_output: str) -> list[str]:
    """Compute SHA256 fingerprints from ssh-keyscan output lines.

    Each non-comment, non-empty line is: ``host keytype base64_key``
    Returns a list of ``SHA256:<digest>`` strings (one per key line).
    """
    fingerprints = []
    for line in keyscan_output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            raw_key = base64.b64decode(parts[2])
            digest = hashlib.sha256(raw_key).digest()
            fp = "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode()
            fingerprints.append(fp)
        except Exception:
            continue
    return fingerprints


def add_ssh_host_key(
    host: str,
    port: int = 22,
    expected_fingerprint: Optional[str] = None,
) -> bool:
    """Add SSH host key to known_hosts file.

    Uses ssh-keyscan to fetch the host key and appends it to ~/.ssh/known_hosts.
    If the host key already exists, this is a no-op.

    When *expected_fingerprint* is supplied (e.g. ``SHA256:…`` from a cloud
    provider API), the scanned key is verified against it before being
    trusted.  A mismatch aborts the operation and returns False.

    For hosts where no expected fingerprint is available the key is still
    accepted on first use (TOFU) but a warning is logged.

    Args:
        host: Hostname or IP address
        port: SSH port (default: 22)
        expected_fingerprint: Optional SHA256 fingerprint to verify against

    Returns:
        True if successful, False otherwise
    """
    try:
        # Get home directory
        home_dir = Path.home()
        ssh_dir = home_dir / '.ssh'
        known_hosts = ssh_dir / 'known_hosts'

        # Create .ssh directory if it doesn't exist
        ssh_dir.mkdir(mode=0o700, exist_ok=True)

        # Check if host key already exists using ssh-keygen -F (exact match).
        # When the caller pinned an expected fingerprint, we must still verify
        # the stored key against it — otherwise a stale TOFU entry from a
        # previous session would silently bypass fingerprint verification.
        if known_hosts.exists():
            try:
                lookup = subprocess.run(
                    ['ssh-keygen', '-F', host, '-f', str(known_hosts)],
                    capture_output=True, text=True, timeout=5, check=False
                )
                if lookup.returncode == 0 and lookup.stdout.strip():
                    if expected_fingerprint:
                        stored_fps = _compute_key_fingerprints(lookup.stdout)
                        if expected_fingerprint not in stored_fps:
                            log_error(
                                f"SSH host key for {host} in known_hosts does NOT match "
                                f"pinned fingerprint {expected_fingerprint}. "
                                f"Stored: {stored_fps}. Refusing to connect."
                            )
                            return False
                        log_info(f"SSH host key for {host} already in known_hosts and fingerprint verified")
                    else:
                        log_info(f"SSH host key for {host} already in known_hosts")
                    return True
            except Exception as exc:
                # If ssh-keygen lookup fails, continue to add the key
                log_warning(f"ssh-keygen -F lookup failed for {host}: {exc}; continuing to fetch")

        # Use ssh-keyscan to fetch host key
        log_info(f"Adding SSH host key for {host}:{port}...")
        cmd = ['ssh-keyscan', '-p', str(port), '-H', host]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False
        )

        if result.returncode != 0:
            log_warning(f"Failed to fetch SSH host key for {host}: {result.stderr}")
            return False

        host_key = result.stdout.strip()
        if not host_key:
            log_warning(f"No host key returned for {host}")
            return False

        # --- Fingerprint verification ---
        scanned_fps = _compute_key_fingerprints(host_key)
        if expected_fingerprint:
            if expected_fingerprint not in scanned_fps:
                log_error(
                    f"SSH host key fingerprint mismatch for {host}! "
                    f"Expected {expected_fingerprint}, got {scanned_fps}. "
                    "Possible MITM attack — aborting."
                )
                return False
            log_info(f"SSH host key fingerprint verified for {host}: {expected_fingerprint}")
        else:
            # TOFU — warn the user. Surface the fingerprint in a machine-readable
            # form so operators can copy it and pin the expected value on the
            # next deploy via --expected-ssh-fingerprint.
            if scanned_fps:
                log_warning(
                    f"Trusting SSH host key for {host} on first use (TOFU). "
                    f"Fingerprint(s): {', '.join(scanned_fps)}"
                )
                log_warning(
                    "To pin this key on future deploys, pass "
                    f"--expected-ssh-fingerprint={scanned_fps[0]} "
                    "to `ilha droplet deploy`/`put`."
                )
            else:
                log_warning(f"Could not compute fingerprint for {host} host key")

        # Append to known_hosts
        try:
            with open(known_hosts, 'a') as f:
                f.write(host_key + '\n')

            # Set appropriate permissions
            known_hosts.chmod(0o600)

            log_info(f"Successfully added SSH host key for {host}")
            return True
        except Exception as e:
            log_warning(f"Failed to write to known_hosts: {e}")
            return False

    except Exception as e:
        log_warning(f"Error adding SSH host key for {host}: {e}")
        return False
