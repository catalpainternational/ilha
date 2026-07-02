"""Security regression tests for ilha/commands/push/transfer_manager.py.

These pin three fixed injection / consistency bugs. No real SSH runs:
``self.ssh`` methods and subprocess are mocked, and we assert on the exact
argv / command strings that would be handed to ssh / rsync.

Regressions covered:
- SEC-4  ensure_remote_dir must shlex-quote the remote dir in `mkdir -p`.
- SEC-5  check_remote_file_exists must quote the path and NOT nest a second
         `bash -lc` inside the remote command.
- UTIL-13 transfer_package must build the rsync `-e` transport from
          self.ssh.get_rsync_ssh_command(...), not a hardcoded ssh string.
"""

import shlex
import subprocess
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ilha.commands.push.transfer_manager import TransferManager
from ilha.utils.ssh_manager import SCPTarget


def _completed(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture(autouse=True)
def _no_hostkey(monkeypatch):
    """Never touch the real known_hosts / network during these tests."""
    monkeypatch.setattr(
        "ilha.commands.push.transfer_manager.add_ssh_host_key",
        lambda *a, **k: True,
    )


@pytest.fixture
def tm():
    mgr = TransferManager()
    # Replace the SSH manager with a spy that records build_ssh_command args
    # and returns a harmless argv.
    mgr.ssh = MagicMock()
    mgr.ssh.build_ssh_command.return_value = ["ssh", "deploy@example.com", "REMOTE"]
    return mgr


# --------------------------------------------------------------------------- #
# SEC-4: ensure_remote_dir quotes the mkdir -p path
# --------------------------------------------------------------------------- #

def test_ensure_remote_dir_quotes_path(tm, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(0))
    target = SCPTarget("deploy@example.com:/srv/app")

    assert tm.ensure_remote_dir(target) is True

    # Inspect the remote command passed to build_ssh_command.
    args, kwargs = tm.ssh.build_ssh_command.call_args
    command = args[2]  # (username, server, command, ...)
    assert command == "mkdir -p " + shlex.quote("/srv/app")


def test_ensure_remote_dir_injection_path_is_quoted_not_executed(tm, monkeypatch):
    """SEC-4 regression: a path containing shell metacharacters must be a
    single quoted argument to mkdir, never a separate command."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(0))
    evil = "/tmp/x;touch pwned"
    target = SCPTarget(f"deploy@example.com:{evil}")

    tm.ensure_remote_dir(target)

    command = tm.ssh.build_ssh_command.call_args.args[2]
    # The whole evil path is a single shlex-quoted token.
    assert command == "mkdir -p " + shlex.quote(evil)
    # The `;touch pwned` lives inside quotes, so it cannot break out.
    assert command.startswith("mkdir -p '")
    assert command.endswith("'")
    # Sanity: an UNquoted injection would look like this, and must NOT appear.
    assert "mkdir -p /tmp/x;touch pwned" not in command


def test_ensure_remote_dir_uses_control_master(tm, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(0))
    tm.ensure_remote_dir(SCPTarget("deploy@example.com:/srv/app"))
    kwargs = tm.ssh.build_ssh_command.call_args.kwargs
    assert kwargs.get("use_control_master") is True


def test_ensure_remote_dir_returns_false_on_nonzero(tm, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(1, "", "boom"))
    assert tm.ensure_remote_dir(SCPTarget("deploy@example.com:/srv/app")) is False


def test_ensure_remote_dir_skips_current_dir(tm, monkeypatch):
    called = {"run": False}

    def fake_run(*a, **k):
        called["run"] = True
        return _completed(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    # remote_path '.' infers to '.', which is skipped (no mkdir).
    target = SCPTarget("deploy@example.com:.")
    assert tm.ensure_remote_dir(target) is True
    assert called["run"] is False
    tm.ssh.build_ssh_command.assert_not_called()


# --------------------------------------------------------------------------- #
# SEC-5: check_remote_file_exists quotes path, no nested bash -lc
# --------------------------------------------------------------------------- #

def test_check_remote_file_exists_quotes_path(tm, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "EXISTS\n")
    )
    target = SCPTarget("deploy@example.com:/srv/app")
    path = "/srv/app/pkg.tar.gz"

    assert tm.check_remote_file_exists(target, path) is True

    command = tm.ssh.build_ssh_command.call_args.args[2]
    assert command == f"test -f {shlex.quote(path)} && echo EXISTS || echo NOT_FOUND"


def test_check_remote_file_exists_no_nested_bash_lc(tm, monkeypatch):
    """SEC-5 regression: the inner remote command must NOT itself contain a
    second `bash -lc`; build_ssh_command supplies the single outer shell."""
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "EXISTS\n")
    )
    target = SCPTarget("deploy@example.com:/srv/app")
    tm.check_remote_file_exists(target, "/srv/app/pkg.tar.gz")

    command = tm.ssh.build_ssh_command.call_args.args[2]
    assert "bash -lc" not in command
    assert "bash -c" not in command


def test_check_remote_file_exists_injection_path_quoted(tm, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "NOT_FOUND\n")
    )
    target = SCPTarget("deploy@example.com:/srv/app")
    evil = "/srv/app/pkg.tar.gz; rm -rf /"
    tm.check_remote_file_exists(target, evil)

    command = tm.ssh.build_ssh_command.call_args.args[2]
    assert shlex.quote(evil) in command
    # Unquoted injection form must not appear.
    assert "test -f /srv/app/pkg.tar.gz; rm -rf /" not in command


def test_check_remote_file_exists_true_when_exists(tm, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "EXISTS\n")
    )
    assert tm.check_remote_file_exists(
        SCPTarget("deploy@example.com:/srv/app"), "/srv/app/x"
    ) is True


def test_check_remote_file_exists_false_when_not_found(tm, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "NOT_FOUND\n")
    )
    assert tm.check_remote_file_exists(
        SCPTarget("deploy@example.com:/srv/app"), "/srv/app/x"
    ) is False


def test_check_remote_file_exists_false_on_ssh_failure(tm, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(255, "", "no route"))
    assert tm.check_remote_file_exists(
        SCPTarget("deploy@example.com:/srv/app"), "/srv/app/x"
    ) is False


def test_check_remote_file_exists_false_on_exception(tm, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ssh exploded")

    monkeypatch.setattr(subprocess, "run", boom)
    # Must swallow and return False, never raise.
    assert tm.check_remote_file_exists(
        SCPTarget("deploy@example.com:/srv/app"), "/srv/app/x"
    ) is False


# --------------------------------------------------------------------------- #
# SEC-4 companion: find_existing_package quotes path AND branch
# --------------------------------------------------------------------------- #

def test_find_existing_package_quotes_path_and_branch(tm, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _completed(0, "/srv/app/feature.ilha-package.tar.gz\n"),
    )
    target = SCPTarget("deploy@example.com:/srv/app")
    result = tm.find_existing_package(target, "feat;evil")

    command = tm.ssh.build_ssh_command.call_args.args[2]
    assert shlex.quote("/srv/app") in command
    assert shlex.quote("feat;evil") in command
    assert result == "/srv/app/feature.ilha-package.tar.gz"


def test_find_existing_package_none_when_empty(tm, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(0, "\n"))
    assert tm.find_existing_package(
        SCPTarget("deploy@example.com:/srv/app"), "feat"
    ) is None


# --------------------------------------------------------------------------- #
# UTIL-13: transfer_package uses get_rsync_ssh_command for -e
# --------------------------------------------------------------------------- #

class _FakeProc:
    """Minimal Popen stand-in with a streamable stdout and returncode."""

    def __init__(self, returncode=0, lines=("sending...\n",)):
        self.returncode = returncode
        self._lines = list(lines) + [""]
        self.stdout = types.SimpleNamespace(
            readline=MagicMock(side_effect=self._lines)
        )

    def wait(self):
        return self.returncode


@pytest.fixture
def package_file(tmp_path):
    p = tmp_path / "app.ilha-package.tar.gz"
    p.write_bytes(b"x" * 2048)
    return p


def test_transfer_package_rsync_uses_get_rsync_ssh_command(tm, monkeypatch, package_file):
    sentinel = "ssh -o ControlPath=/tmp/ilha-XXXX/control-example SENTINEL"
    tm.ssh.get_rsync_ssh_command.return_value = sentinel

    captured = {}

    # `which rsync` -> rc 0 so the rsync branch is taken.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(0))

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    target = SCPTarget("deploy@example.com:/srv/app")
    assert tm.transfer_package(package_file, target) is True

    tm.ssh.get_rsync_ssh_command.assert_called_once_with("deploy", "example.com")

    cmd = captured["cmd"]
    assert cmd[0] == "rsync"
    assert "-e" in cmd
    e_value = cmd[cmd.index("-e") + 1]
    # The transport must be exactly what the SSH manager produced.
    assert e_value == sentinel
    # UTIL-13 regression: the old hardcoded string must not be used.
    assert e_value != "ssh -o StrictHostKeyChecking=accept-new"


def test_transfer_package_rsync_e_is_not_hardcoded_anywhere(tm, monkeypatch, package_file):
    tm.ssh.get_rsync_ssh_command.return_value = "SSH_TRANSPORT"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(0))

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    tm.transfer_package(package_file, SCPTarget("deploy@example.com:/srv/app"))

    cmd = captured["cmd"]
    # No element of the rsync argv is the legacy hardcoded accept-new ssh string.
    assert "ssh -o StrictHostKeyChecking=accept-new" not in cmd


def test_transfer_package_rsync_target_path_correct(tm, monkeypatch, package_file):
    tm.ssh.get_rsync_ssh_command.return_value = "SSH_TRANSPORT"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(0))

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    tm.transfer_package(package_file, SCPTarget("deploy@example.com:/srv/app"))

    cmd = captured["cmd"]
    # local source then remote destination as last two args.
    assert cmd[-2] == str(package_file)
    assert cmd[-1] == f"/srv/app/{package_file.name}"


def test_transfer_package_falls_back_to_scp_when_no_rsync(tm, monkeypatch, package_file):
    """When `which rsync` fails, the SCP fallback path runs (no Popen)."""
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["which", "rsync"]:
            return _completed(1)  # rsync not available
        # This is the scp invocation.
        return _completed(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    popen_called = {"n": 0}

    def fake_popen(*a, **k):
        popen_called["n"] += 1
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert tm.transfer_package(package_file, SCPTarget("deploy@example.com:/srv/app")) is True
    assert popen_called["n"] == 0
    tm.ssh.get_rsync_ssh_command.assert_not_called()


def test_transfer_package_returns_false_on_scp_failure(tm, monkeypatch, package_file):
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["which", "rsync"]:
            return _completed(1)
        return _completed(1, "", "Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert tm.transfer_package(package_file, SCPTarget("deploy@example.com:/srv/app")) is False
