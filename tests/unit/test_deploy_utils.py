"""Unit tests for ilha.scripts.deploy_utils (server prep / remote deploy helpers)."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from ilha.scripts.deploy_utils import (
    DockerCommand,
    RemoteSystemCommand,
    apt_stderr_indicates_lock,
)


def test_apt_stderr_indicates_lock_detects_lock():
    err = "E: Could not get lock /var/lib/apt/lists/lock"
    assert apt_stderr_indicates_lock(err, "") is True


def test_apt_stderr_indicates_lock_rejects_dns_only():
    err = "Could not resolve 'archive.ubuntu.com'"
    assert apt_stderr_indicates_lock(err, "") is False


def test_remote_system_command_run_success_non_capture_returns_empty_string():
    rsc = RemoteSystemCommand()
    with patch("ilha.scripts.deploy_utils.subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0, stdout="ignored", stderr="")
        out = rsc.run(["true"], capture_output=False, check=False)
        assert out == ""


def test_remote_system_command_run_failure_returns_none():
    rsc = RemoteSystemCommand()
    with patch("ilha.scripts.deploy_utils.subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=1, stdout="", stderr="nope")
        out = rsc.run(["false"], capture_output=False, check=False)
        assert out is None


def test_remote_system_command_command_exists_uses_which(monkeypatch):
    rsc = RemoteSystemCommand()
    monkeypatch.setattr("ilha.scripts.deploy_utils.shutil.which", lambda _: "/bin/sh")
    assert rsc.command_exists("sh") is True
    monkeypatch.setattr("ilha.scripts.deploy_utils.shutil.which", lambda _: None)
    assert rsc.command_exists("nonexistent-xyz") is False


def test_apt_update_fails_fast_on_non_lock_error():
    rsc = RemoteSystemCommand()
    with patch("ilha.scripts.deploy_utils.subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(
            returncode=100,
            stdout="",
            stderr="E: Failed to fetch http://bad.example/Release  Could not resolve 'bad.example'",
        )
        assert rsc.apt_update(timeout=30) is False
        assert run_mock.call_count == 1


def test_apt_update_retries_on_lock_error():
    rsc = RemoteSystemCommand()
    with patch("ilha.scripts.deploy_utils.subprocess.run") as run_mock:
        with patch("ilha.scripts.deploy_utils.time.sleep", lambda _s: None):
            lock_err = MagicMock(
                returncode=100,
                stdout="",
                stderr="E: Could not get lock /var/lib/apt/lists/lock",
            )
            ok = MagicMock(returncode=0, stdout="", stderr="")
            run_mock.side_effect = [lock_err, ok]
            assert rsc.apt_update(timeout=60) is True
            assert run_mock.call_count == 2


def test_dpkg_wait_ignores_unattended_upgrade_shutdown():
    rsc = RemoteSystemCommand()

    def fake_run(args, capture_output=False, check=False):
        assert args != ["pgrep", "-x", "unattended-upgr"]
        return None

    with patch.object(rsc, "run", side_effect=fake_run):
        assert rsc.dpkg_wait(timeout=5) is True


def test_dpkg_wait_waits_for_active_unattended_upgrade():
    rsc = RemoteSystemCommand()
    calls = {"unattended": 0}

    def fake_run(args, capture_output=False, check=False):
        if args == ["pgrep", "-f", "^/usr/bin/python3 /usr/bin/unattended-upgrade"]:
            calls["unattended"] += 1
            return "123" if calls["unattended"] == 1 else None
        return None

    with patch.object(rsc, "run", side_effect=fake_run):
        with patch("ilha.scripts.deploy_utils.time.sleep", lambda _s: None):
            assert rsc.dpkg_wait(timeout=10) is True
    assert calls["unattended"] == 2


def test_docker_command_network_connect_treats_already_exists_as_success():
    dc = DockerCommand()
    err = subprocess.CalledProcessError(
        1,
        ["docker", "network", "connect"],
        stderr=(
            "Error response from daemon: endpoint with name foo "
            "already exists in network bar"
        ),
    )
    with patch("ilha.scripts.deploy_utils.subprocess.run", side_effect=err):
        assert dc.network_connect("cid", "ilha_caddy_proxy") is True


def test_docker_command_network_connect_real_failure():
    dc = DockerCommand()
    err = subprocess.CalledProcessError(
        1,
        ["docker", "network", "connect"],
        stderr="Error response from daemon: network not found",
    )
    with patch("ilha.scripts.deploy_utils.subprocess.run", side_effect=err):
        assert dc.network_connect("cid", "missing_net") is False
