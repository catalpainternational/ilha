"""Tests for ilha.utils.ssh_utils host key matching."""

from pathlib import Path
from unittest.mock import patch, MagicMock

from ilha.utils.ssh_utils import add_ssh_host_key


class TestHostKeyMatching:
    """Verify host key lookup uses exact matching (S4 fix)."""

    @patch("ilha.utils.ssh_utils.subprocess.run")
    def test_exact_match_uses_ssh_keygen_F(self, mock_run, tmp_path):
        """ssh-keygen -F is used instead of naive substring matching."""
        known_hosts = tmp_path / ".ssh" / "known_hosts"
        known_hosts.parent.mkdir(parents=True)
        known_hosts.write_text("10.0.0.1 ssh-rsa AAAA...\n")

        # ssh-keygen -F returns 0 (found)
        mock_run.return_value = MagicMock(returncode=0, stdout="10.0.0.1 ssh-rsa AAAA...")

        with patch("ilha.utils.ssh_utils.Path.home", return_value=tmp_path):
            result = add_ssh_host_key("10.0.0.1")

        assert result is True
        # Should have called ssh-keygen -F, not just checked "in content"
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ssh-keygen"
        assert "-F" in cmd
        assert "10.0.0.1" in cmd

    @patch("ilha.utils.ssh_utils.subprocess.run")
    def test_no_false_positive_for_ip_prefix(self, mock_run, tmp_path):
        """10.0.0.1 should NOT match 110.0.0.1 (the bug we fixed)."""
        known_hosts = tmp_path / ".ssh" / "known_hosts"
        known_hosts.parent.mkdir(parents=True)
        known_hosts.write_text("110.0.0.1 ssh-rsa AAAA...\n")

        # ssh-keygen -F for 10.0.0.1 returns 1 (not found) because 110.0.0.1 != 10.0.0.1
        keygen_result = MagicMock(returncode=1, stdout="")
        # ssh-keyscan returns the key
        keyscan_result = MagicMock(returncode=0, stdout="10.0.0.1 ssh-rsa BBBB...")

        mock_run.side_effect = [keygen_result, keyscan_result]

        with patch("ilha.utils.ssh_utils.Path.home", return_value=tmp_path):
            result = add_ssh_host_key("10.0.0.1")

        assert result is True
        # Should have called ssh-keygen first, then ssh-keyscan
        assert mock_run.call_count == 2

    @patch("ilha.utils.ssh_utils.subprocess.run")
    def test_adds_key_when_not_found(self, mock_run, tmp_path):
        """When host is not in known_hosts, adds via ssh-keyscan."""
        known_hosts = tmp_path / ".ssh" / "known_hosts"
        known_hosts.parent.mkdir(parents=True)
        known_hosts.write_text("")

        keygen_result = MagicMock(returncode=1, stdout="")
        keyscan_result = MagicMock(returncode=0, stdout="newhost ssh-rsa CCCC...")
        mock_run.side_effect = [keygen_result, keyscan_result]

        with patch("ilha.utils.ssh_utils.Path.home", return_value=tmp_path):
            result = add_ssh_host_key("newhost")

        assert result is True
        content = known_hosts.read_text()
        assert "newhost ssh-rsa CCCC..." in content
