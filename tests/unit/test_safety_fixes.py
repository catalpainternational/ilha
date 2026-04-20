"""Tests for safety fixes applied to the deployment pipeline.

Scope after test-suite cleanup: only the S1 confirmation behavior remains
here. S2 (parallel stage ordering) was deleted because the test re-
implemented the parallel section inline and asserted against its own
re-implementation, not _deploy_once itself. S6 (signature introspection)
was deleted as tautological. S3 (ServerPreparer timeout) was obsoleted
by the Phase 2 removal of the legacy push path.
"""

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
