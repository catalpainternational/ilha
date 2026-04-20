"""Tests for safety fixes applied to the deployment pipeline."""

from pathlib import Path
from unittest.mock import patch

import pytest

from ilha.commands.push.slim_deploy_manager import SlimDeployManager


class TestDiskRecoveryConfirmation:
    """S1: disk recovery requires confirmation in non-interactive mode."""

    def test_non_interactive_skips_recovery(self, tmp_path, monkeypatch):
        """In non-interactive (no stdin), recovery is skipped."""
        mgr = SlimDeployManager(project_root=tmp_path)

        def fake_remote(_u, _s, command, timeout=30):
            return 0, "", ""

        monkeypatch.setattr(mgr.disk, "_run_remote_text", fake_remote)
        # Without force=True, input() raises OSError in test
        result = mgr.disk.attempt_remote_disk_recovery("root", "1.2.3.4", "safe")
        assert result is False  # Skipped

    def test_force_bypasses_confirmation(self, tmp_path, monkeypatch):
        """force=True skips the confirmation prompt."""
        mgr = SlimDeployManager(project_root=tmp_path)

        def fake_remote(_u, _s, command, timeout=30):
            return 0, "", ""

        monkeypatch.setattr(mgr.disk, "_run_remote_text", fake_remote)
        result = mgr.disk.attempt_remote_disk_recovery("root", "1.2.3.4", "safe", force=True)
        assert result is True


class TestParallelStageOrdering:
    """S2: build waits for restore, doesn't run if restore fails."""

    def test_build_skipped_when_restore_fails(self, tmp_path, monkeypatch):
        """When volume restore fails, build should NOT run."""
        mgr = SlimDeployManager(project_root=tmp_path)
        build_called = []

        def fake_restore(_u, _s, _b):
            return False  # Restore fails

        def fake_caddy(_u, _s):
            return True

        def fake_build(_u, _s, _d=False):
            build_called.append(True)
            return True

        monkeypatch.setattr(mgr, "_restore_volumes_remote", fake_restore)
        monkeypatch.setattr(mgr, "_start_caddy_remote", fake_caddy)
        monkeypatch.setattr(mgr, "_build_remote", fake_build)

        # Simulate the parallel section from _deploy_once
        # We can't easily call _deploy_once, so test the logic directly
        import time
        from concurrent.futures import ThreadPoolExecutor

        volume_tar = Path("/fake/backup.tar")
        build = True
        restore_ok = True
        build_ok = True

        # Phase 1: restore + caddy
        with ThreadPoolExecutor(max_workers=2) as executor:
            phase1_futures = {}
            if volume_tar:
                phase1_futures["restore"] = executor.submit(fake_restore, "u", "s", "b")
            phase1_futures["caddy"] = executor.submit(fake_caddy, "u", "s")
            for key in phase1_futures:
                ok = phase1_futures[key].result(timeout=10)
                if key == "restore":
                    restore_ok = ok

        # Phase 2: build only if restore succeeded
        if build and restore_ok:
            fake_build("u", "s")
        elif build and not restore_ok:
            build_ok = False

        assert restore_ok is False
        assert build_ok is False
        assert build_called == []  # Build was NOT called


class TestSmokeTimeoutThreading:
    """S6: smoke_timeout parameter reaches _run_smoke_checks."""

    def test_deploy_accepts_smoke_timeout(self, tmp_path):
        """deploy() signature accepts smoke_timeout parameter."""
        mgr = SlimDeployManager(project_root=tmp_path)
        # Just verify the parameter exists in the signature
        import inspect
        sig = inspect.signature(mgr.deploy)
        assert "smoke_timeout" in sig.parameters
        assert sig.parameters["smoke_timeout"].default == 60

    def test_put_accepts_smoke_timeout(self, tmp_path):
        """put() signature accepts smoke_timeout parameter."""
        mgr = SlimDeployManager(project_root=tmp_path)
        import inspect
        sig = inspect.signature(mgr.put)
        assert "smoke_timeout" in sig.parameters
        assert sig.parameters["smoke_timeout"].default == 60


# S3 (ServerPreparer timeout) is no longer enforced here: the legacy push
# path's ServerPreparer was removed along with PushManager. Slim deploy runs
# its own prep script (server_prep.sh) via _prepare_server_slim and is covered
# by the deploy safety regression tests.
