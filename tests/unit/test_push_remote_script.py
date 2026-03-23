"""Tests for PushManager._run_remote_import command construction.

_compose_remote_script was removed in favour of _run_remote_import, which
builds an ``ilha server-import`` command, wraps it in ``nohup``, and
executes it over SSH.  These tests verify the command string that is
composed for the initial SSH call (the "start" call), without actually
running any subprocess or SSH session.
"""

import re
from pathlib import Path
from unittest.mock import patch, MagicMock

from ilha.commands.push import PushManager


def _capture_nohup_command(pm, **kwargs):
    """Call _run_remote_import with mocked SSH/subprocess and return the
    nohup command string that would be sent to the server.

    The first ``ssh.build_ssh_command`` call receives the nohup wrapper as
    its *command* argument.  We capture that and immediately make
    ``subprocess.run`` pretend the process started successfully (returning
    a fake PID) and then finished ("FINISHED" + "SUCCESS").
    """
    captured = {}

    original_build = pm.ssh.build_ssh_command

    def fake_build(username, server, command, **kw):
        if "nohup" not in captured:
            captured["nohup"] = command
        return ["echo", "fake"]

    call_count = {"n": 0}

    def fake_run(cmd, **kw):
        call_count["n"] += 1
        mock_result = MagicMock()
        mock_result.returncode = 0
        # First call: return a fake PID
        if call_count["n"] == 1:
            mock_result.stdout = "12345\n"
        # Second call: process status check -> FINISHED
        elif call_count["n"] == 2:
            mock_result.stdout = "FINISHED\n"
        # Third call: line count
        elif call_count["n"] == 3:
            mock_result.stdout = "0\n"
        # Fourth call: success check
        elif call_count["n"] == 4:
            mock_result.stdout = "SUCCESS\n"
        else:
            mock_result.stdout = ""
        return mock_result

    with patch.object(pm.ssh, "build_ssh_command", side_effect=fake_build), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("time.sleep"):
        pm._run_remote_import(
            username=kwargs.get("username", "root"),
            server=kwargs.get("server", "10.0.0.1"),
            remote_file=kwargs.get("remote_file", "/var/ilha/packages/pkg.tar.gz"),
            branch_name=kwargs.get("branch_name", "feature-auth"),
            domain=kwargs.get("domain", None),
            ip=kwargs.get("ip", None),
            build=kwargs.get("build", False),
            debug=kwargs.get("debug", False),
            use_staging_certificates=kwargs.get("use_staging_certificates", False),
        )

    return captured.get("nohup", "")


def test_run_remote_import_basic(tmp_path: Path):
    """Basic invocation must contain the server-import sub-command, the
    remote file path, and the branch name."""
    pm = PushManager(project_root=tmp_path)
    cmd = _capture_nohup_command(
        pm,
        remote_file="/var/ilha/packages/pkg.tar.gz",
        branch_name="feature-auth",
    )

    # Must use nohup wrapper
    assert cmd.startswith("nohup ")
    # Must invoke ilha server-import with the package path
    assert "ilha" in cmd
    assert "server-import" in cmd
    assert "/var/ilha/packages/pkg.tar.gz" in cmd
    # Must pass branch name
    assert "--branch-name" in cmd
    assert "feature-auth" in cmd
    # Must redirect output and background the process
    assert ">" in cmd
    assert "echo $!" in cmd


def test_run_remote_import_with_domain_and_ip(tmp_path: Path):
    """--domain and --ip flags are mutually exclusive; each must appear in
    the generated command when supplied."""
    pm = PushManager(project_root=tmp_path)

    # Domain case
    cmd_domain = _capture_nohup_command(
        pm,
        branch_name="feature-auth",
        domain="app.example.com",
        ip=None,
    )
    assert "--domain" in cmd_domain
    assert "app.example.com" in cmd_domain
    # --ip must NOT appear when domain is set
    assert "--ip" not in cmd_domain

    # IP case
    cmd_ip = _capture_nohup_command(
        pm,
        branch_name="feature-auth",
        domain=None,
        ip="203.0.113.10",
    )
    assert "--ip" in cmd_ip
    assert "203.0.113.10" in cmd_ip
    # --domain must NOT appear when ip is set
    assert "--domain" not in cmd_ip


def test_run_remote_import_optional_flags(tmp_path: Path):
    """Optional flags (--build, --debug, --use-staging-certificates) must
    be forwarded when set, and absent when not."""
    pm = PushManager(project_root=tmp_path)

    # Without optional flags
    cmd_plain = _capture_nohup_command(pm, branch_name="test-branch")
    assert "--build" not in cmd_plain
    assert "--debug" not in cmd_plain
    assert "--use-staging-certificates" not in cmd_plain

    # With all optional flags
    cmd_full = _capture_nohup_command(
        pm,
        branch_name="test-branch",
        build=True,
        debug=True,
        use_staging_certificates=True,
    )
    assert "--build" in cmd_full
    assert "--debug" in cmd_full
    assert "--use-staging-certificates" in cmd_full
