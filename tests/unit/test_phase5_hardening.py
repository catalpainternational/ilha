"""Regression tests for Phase 5 deploy hardening.

Covers:
- B2: SSH execute_remote_script always cleans up /tmp file, even on failure.
- B6: DeployServiceConfig defaults match historical hardcoded values and flow
      into the remote start-app script and the smoke-probe command.
- B8: SSH control paths include the port, so simultaneous connections to the
      same host on different ports do not collide.
- G4: SSH host-key pinning refuses a stored known_hosts entry whose
      fingerprint does not match the caller's expected fingerprint.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from ilha.commands.push.deploy_service_config import (
    DeployServiceConfig,
    load_deploy_service_config,
)
from ilha.commands.push.slim_deploy_manager import SlimDeployManager
from ilha.utils.ssh_manager import SSHConnectionManager


# ── B2: /tmp cleanup survives script failure ─────────────────────────────

def test_execute_remote_script_always_cleans_up_tmp():
    """B2: the shell we hand to SSH must `rm` the /tmp script unconditionally
    (under the same rc), even when the uploaded script exits non-zero.

    The previous chain `cat > … && chmod … && /tmp/… && rm -f /tmp/…` skipped
    the `rm` whenever the script returned non-zero, accumulating stale files.
    """
    mgr = SSHConnectionManager()
    mgr.ensure_host_key = lambda *a, **kw: True  # type: ignore[method-assign]
    mgr.build_ssh_command = lambda *a, **kw: ["echo", a[2] if len(a) > 2 else ""]  # type: ignore[method-assign]

    captured = {}

    def fake_run(cmd, **kwargs):
        # Last argument is the remote command string the SSH wrapper passes.
        captured["remote_cmd"] = cmd[-1] if cmd else ""
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    with patch("ilha.utils.ssh_manager.subprocess.run", side_effect=fake_run):
        mgr.execute_remote_script("root", "1.2.3.4", "echo hi", script_name="job.sh")

    remote = captured["remote_cmd"]
    assert "cat > /tmp/job.sh" in remote
    # Key assertions: not a short-circuiting && chain that skips rm.
    assert "&& rm -f /tmp/job.sh" not in remote, (
        "Old pattern: `&& rm` only runs when the script succeeds. "
        "Must capture rc and rm unconditionally instead."
    )
    assert "rc=$?" in remote, "Must capture the script's exit code"
    assert "rm -f /tmp/job.sh" in remote, "Must always attempt the cleanup"
    assert "exit $rc" in remote, "Must propagate the original exit code"


# ── B8: SSH control paths disambiguate by port ───────────────────────────

def test_ssh_control_path_includes_port():
    """B8: the control-master socket filename must encode the port so the
    manager does not confuse two simultaneous sessions to the same host on
    different SSH ports (e.g. :22 vs :2222 through a jumphost)."""
    mgr = SSHConnectionManager()
    path_22 = mgr._get_control_path("root", "1.2.3.4", 22)
    path_2222 = mgr._get_control_path("root", "1.2.3.4", 2222)

    assert path_22 != path_2222, (
        "Control paths must differ across ports on the same host"
    )
    assert path_22.endswith("-22"), f"Port 22 path should end with -22, got {path_22}"
    assert path_2222.endswith("-2222"), f"Port 2222 path should end with -2222, got {path_2222}"

    # Repeat calls must be stable (cached).
    assert mgr._get_control_path("root", "1.2.3.4", 22) == path_22


# ── G4: pinned fingerprint rejects a mismatched known_hosts entry ────────

def test_add_ssh_host_key_rejects_mismatched_stored_key(tmp_path: Path, monkeypatch):
    """G4: a stale TOFU entry in known_hosts must NOT be silently trusted when
    the caller pins a specific fingerprint. Previously the early-out at the
    top of ``add_ssh_host_key`` returned True on any ssh-keygen -F hit,
    bypassing verification."""
    from ilha.utils import ssh_utils

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(ssh_utils.Path, "home", lambda: home)

    ssh_dir = home / ".ssh"
    ssh_dir.mkdir()
    known_hosts = ssh_dir / "known_hosts"
    # Any non-empty content; we stub the ssh-keygen -F subprocess anyway.
    known_hosts.write_text("placeholder\n")

    # Fake ssh-keygen -F hit with a key whose computed fingerprint we control.
    stored_fps = ["SHA256:aaaabbbbccccdddd"]
    monkeypatch.setattr(
        ssh_utils, "_compute_key_fingerprints", lambda _out: stored_fps
    )

    class KnownHostsHit:
        returncode = 0
        stdout = "host,1.2.3.4 ssh-ed25519 AAAAfakekey"
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ssh-keygen" and "-F" in cmd:
            return KnownHostsHit()
        raise AssertionError(
            f"Unexpected subprocess.run call during lookup phase: {cmd!r}"
        )

    monkeypatch.setattr(ssh_utils.subprocess, "run", fake_run)

    # Expected pin does NOT match the stored fingerprint — must be rejected.
    ok = ssh_utils.add_ssh_host_key(
        "1.2.3.4", expected_fingerprint="SHA256:zzzzzzzzzzzzzzzz"
    )
    assert ok is False, (
        "A stored host-key entry whose fingerprint does not match the pinned "
        "fingerprint must be rejected; silent acceptance would defeat the pin."
    )

    # Control: matching pin accepts the stored entry without keyscan.
    ok_match = ssh_utils.add_ssh_host_key("1.2.3.4", expected_fingerprint=stored_fps[0])
    assert ok_match is True


# ── B6: DeployServiceConfig ──────────────────────────────────────────────

def test_deploy_service_config_defaults_match_historical():
    cfg = DeployServiceConfig()
    assert cfg.web_service == "web"
    assert cfg.web_port == 8000
    assert cfg.health_path == "/health-check/"
    assert cfg.dependency_services == ["db", "redis"]
    assert cfg.db_service == "db"
    assert cfg.worker_services == ["rq-worker-1"]


def test_deploy_service_config_from_mapping_overrides():
    cfg = DeployServiceConfig.from_mapping({
        "web_service": "api",
        "web_port": 3000,
        "health_path": "healthz",  # missing leading slash on purpose
        "dependency_services": "postgres, cache",  # comma-string convenience
        "worker_services": [],
    })
    assert cfg.web_service == "api"
    assert cfg.web_port == 3000
    assert cfg.health_path == "/healthz"
    assert cfg.dependency_services == ["postgres", "cache"]
    assert cfg.worker_services == []


def test_deploy_service_config_invalid_port_falls_back(caplog):
    cfg = DeployServiceConfig.from_mapping({"web_port": "not-a-number"})
    assert cfg.web_port == 8000  # default preserved


def test_load_deploy_service_config_missing_file_returns_defaults(tmp_path: Path):
    cfg = load_deploy_service_config(tmp_path)
    assert cfg == DeployServiceConfig()


def test_load_deploy_service_config_reads_yaml(tmp_path: Path):
    (tmp_path / ".ilha").mkdir()
    (tmp_path / ".ilha" / "config.yml").write_text(
        "deploy:\n"
        "  web_service: api\n"
        "  web_port: 3000\n"
        "  health_path: /healthz\n"
        "  worker_services: []\n"
    )
    cfg = load_deploy_service_config(tmp_path)
    assert cfg.web_service == "api"
    assert cfg.web_port == 3000
    assert cfg.health_path == "/healthz"
    assert cfg.worker_services == []


# ── B6: config flows into the remote start-app script ───────────────────

def _render_start_app_script(mgr: SlimDeployManager) -> str:
    """Return the remote deploy wrapper script (exports + ``python3 remote_deploy.py``)."""
    return mgr._compose_remote_deploy_wrapper_script(
        app_root=mgr.REMOTE_APP_ROOT,
        remote_tmp_dir="/tmp/ilha-testsession",
        remote_deploy_file="/tmp/ilha-testsession/remote_deploy.py",
        build=False,
    )


def test_start_app_script_honors_custom_service_config(tmp_path: Path):
    """A project that configures `deploy:` in its config.yml must see those
    values exported to the start-app script — not the hardcoded defaults.

    Post Phase 6: service config is passed as shell env vars to the wrapper that
    runs ``remote_deploy.py`` on the server.
    """
    (tmp_path / ".ilha").mkdir()
    (tmp_path / ".ilha" / "config.yml").write_text(
        "deploy:\n"
        "  web_service: api\n"
        "  web_port: 3000\n"
        "  health_path: /healthz\n"
        "  dependency_services: [postgres, cache]\n"
        "  db_service: postgres\n"
        "  worker_services: [celery-worker]\n"
    )
    mgr = SlimDeployManager(project_root=tmp_path)
    script = _render_start_app_script(mgr)

    # Config-driven exports appear in the header.
    assert "export WEB_SERVICE=api" in script
    assert "export WEB_PORT=3000" in script
    assert "export HEALTH_PATH=/healthz" in script
    assert "export DB_SERVICE=postgres" in script
    assert "export DEPS_SERVICES='postgres cache'" in script
    assert "export WORKER_SERVICES=celery-worker" in script

    assert "python3" in script and "remote_deploy.py" in script

    # Historical hardcoded values must NOT appear in functional positions.
    assert ":8000/" not in script
    assert "rq-worker-1" not in script


def test_start_app_script_default_config_preserves_historical_behavior(tmp_path: Path):
    """Phase 5 must not regress deploys for projects that have not adopted the
    new config block — defaults are web/8000//health-check/ and db/redis."""
    mgr = SlimDeployManager(project_root=tmp_path)  # no .ilha/config.yml
    script = _render_start_app_script(mgr)
    assert "export WEB_SERVICE=web" in script
    assert "export WEB_PORT=8000" in script
    assert "export HEALTH_PATH=/health-check/" in script
    assert "export DB_SERVICE=db" in script
    assert "export DEPS_SERVICES='db redis'" in script
    assert "export WORKER_SERVICES=rq-worker-1" in script


def test_start_app_script_no_worker_services_skips_phase10(tmp_path: Path):
    (tmp_path / ".ilha").mkdir()
    (tmp_path / ".ilha" / "config.yml").write_text(
        "deploy:\n"
        "  worker_services: []\n"
    )
    mgr = SlimDeployManager(project_root=tmp_path)
    script = _render_start_app_script(mgr)
    # Empty worker list is exported; Phase 10 in remote_deploy.py logs "No workers configured".
    assert "export WORKER_SERVICES=" in script
    remote_body = (Path(__file__).resolve().parents[2] / "ilha" / "scripts" / "remote_deploy.py").read_text()
    assert "No workers configured" in remote_body
