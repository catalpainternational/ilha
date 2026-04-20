"""Regression tests for HIGH-severity deploy bugs fixed in Phase 3.

Each test pins one of the behaviors from the critical analysis so the bug
class cannot silently return via a future refactor. Bug IDs refer to the
analysis in the feature-slim-deploy branch discussion.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from ilha.commands.push.deploy_preflight import DeployPreflight
from ilha.commands.push.slim_deploy_manager import SlimDeployManager
from ilha.core.docker_manager import DockerManager


# ── G5: SITE_DOMAIN drift blocks deploy under strict_env_contract=True ────

def _write_env(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _make_worktree(tmp_path: Path, env_body: str) -> Path:
    """Create a minimal worktree the preflight can resolve.

    Env body must supply any insecure-default keys (DJANGO_SECRET_KEY,
    POSTGRES_PASSWORD) that ``_check_insecure_defaults`` enforces.
    """
    worktrees_dir = tmp_path / "worktrees"
    worktree = worktrees_dir / "feat"
    compose_dir = worktree / ".ilha"
    compose_dir.mkdir(parents=True)
    (compose_dir / "docker-compose.worktree.yml").write_text(
        "services:\n  web:\n    image: x\n"
    )
    baseline = (
        "DJANGO_SECRET_KEY=safe-test-key-123\n"
        "POSTGRES_PASSWORD=safe-test-password\n"
    )
    _write_env(worktree / ".env", baseline + env_body)
    return worktree


def test_preflight_blocks_deploy_when_site_domain_has_scheme(tmp_path: Path):
    """G5: SITE_DOMAIN with http(s):// prefix must block deploy under strict mode.

    This is the CSRF-breakage class documented in STAGING_DROPLET_FIXES.md.
    """
    worktree = _make_worktree(tmp_path, "SITE_DOMAIN=https://app.example.com\n")
    preflight = DeployPreflight(project_root=tmp_path)

    # Strict mode: must return None (block)
    result_strict = preflight.run_deploy_preflight(
        branch_name="feat",
        strict_env_contract=True,
    )
    assert result_strict is None, "strict_env_contract=True must block when SITE_DOMAIN has a scheme"

    # Non-strict mode: must pass through (warn only)
    result_lenient = preflight.run_deploy_preflight(
        branch_name="feat",
        strict_env_contract=False,
    )
    assert result_lenient is not None, "strict_env_contract=False must allow drift (warn only)"
    warnings = result_lenient["env_contract_warnings"]
    assert warnings and warnings[0]["variable"] == "SITE_DOMAIN"


def test_preflight_allows_bare_site_domain_under_strict(tmp_path: Path):
    """A correctly-formatted SITE_DOMAIN must not trigger the strict gate."""
    worktree = _make_worktree(tmp_path, "SITE_DOMAIN=app.example.com\n")
    preflight = DeployPreflight(project_root=tmp_path)
    result = preflight.run_deploy_preflight(
        branch_name="feat",
        strict_env_contract=True,
    )
    assert result is not None
    assert result["env_contract_warnings"] == []


def test_slim_deploy_manager_sets_env_contract_failure_code(tmp_path: Path):
    """SlimDeployManager surfaces env_contract_violation in _last_report on strict fail."""
    worktree = _make_worktree(tmp_path, "SITE_DOMAIN=https://app.example.com\n")
    mgr = SlimDeployManager(project_root=tmp_path)
    result = mgr._run_deploy_preflight(
        branch_name="feat",
        strict_env_contract=True,
    )
    assert result is None
    error = mgr._last_report.get("error") or {}
    assert error.get("error_code") == "env_contract_violation"
    assert mgr._last_report["failure_category"] == "validation_error"


# ── B5: backup_volumes aborts on partial failure (no silent partial archive) ──

def test_backup_volumes_aborts_when_a_volume_fails(tmp_path: Path, monkeypatch):
    """B5: if any per-volume tar fails, no combined archive is produced.

    Prior behavior: the outer `tar czf` ran regardless, producing a partial
    archive that restored silently with missing volumes on the server.
    """
    from ilha.core import docker_manager as dm_module

    mgr = DockerManager(project_root=tmp_path)
    monkeypatch.setattr(mgr, "_is_worktree_running", lambda branch: False)
    monkeypatch.setattr(
        dm_module,
        "get_volume_names_for_project_root",
        lambda root, branch: {"postgres": "v_pg", "redis": "v_redis"},
    )
    # Both volumes "exist" but the first backup fails; second succeeds.
    monkeypatch.setattr(dm_module, "validate_volume_exists", lambda name: True)

    call_log = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        call_log.append(tuple(cmd))
        # Fail the tar for v_pg; succeed otherwise.
        if "tar" in cmd and any(
            isinstance(c, str) and c.endswith("v_pg.tar") for c in cmd
        ):
            import subprocess as sp
            raise sp.CalledProcessError(1, cmd, stderr="tar: oops")
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(dm_module.subprocess, "run", fake_subprocess_run)

    backup_dir = tmp_path / "deploy" / "volumes"
    backup_dir.mkdir(parents=True)

    result = mgr.backup_volumes("feat", backup_dir)

    assert result is None, "backup must abort when any volume fails"
    archive = backup_dir / "backup_feat.tar"
    assert not archive.exists(), "no partial archive should be emitted"
    # Outer `tar czf` must NOT have been called after a per-volume failure
    outer_tar_calls = [
        c for c in call_log
        if c and c[0] == "tar" and "czf" in c
    ]
    assert not outer_tar_calls, "outer tar czf must be skipped on partial failure"


# ── B4: zero-downtime start-app script gates Phase 8 on NEW_HEALTHY ──

def test_zd_start_app_script_rolls_back_on_unhealthy_new_web(tmp_path: Path):
    """B4: Phase 8 must refuse to swap Caddy if NEW_HEALTHY != 1.

    The script should leave the canary in place (not execute the trap cleanup
    that removes it) and exit non-zero so the deploy fails loudly instead of
    swapping traffic onto a broken container.
    """
    mgr = SlimDeployManager(project_root=tmp_path)
    rendered = {}

    def capture(username, server, script, **kwargs):
        rendered["script"] = script
        return True

    # Bind on the instance so `self` isn't shifted into our positional args.
    mgr._run_nohup_remote = capture  # type: ignore[method-assign]
    mgr.ssh.ensure_host_key = lambda *a, **kw: True  # type: ignore[method-assign]
    mgr.ssh.build_ssh_command = lambda *a, **kw: ["echo"]  # type: ignore[method-assign]

    with patch("ilha.commands.push.slim_deploy_manager.subprocess.run") as run_mock:
        run_mock.return_value.stdout = ""
        run_mock.return_value.returncode = 0
        try:
            mgr._start_app_remote("root", "1.2.3.4", build=False, debug=False)
        except Exception:
            pass  # post-nohup verification steps may fail without a real server

    script = rendered.get("script", "")
    assert script, "start-app script should have been rendered"

    # The guard that must exist:
    assert 'NEW_HEALTHY" != "1"' in script, (
        "Phase 8 must check NEW_HEALTHY before swapping Caddy upstream"
    )
    # The rollback branch must (a) announce itself, (b) disarm the trap so the
    # canary keeps serving, and (c) exit non-zero — all before Phase 9 runs.
    guard_idx = script.find('NEW_HEALTHY" != "1"')
    phase9_idx = script.find('Phase 9:')
    assert guard_idx != -1 and phase9_idx != -1 and guard_idx < phase9_idx
    rollback_region = script[guard_idx:phase9_idx]
    assert 'ROLLBACK: new compose web failed health check' in rollback_region, (
        "Rollback branch should announce itself in logs"
    )
    assert "trap - EXIT" in rollback_region, (
        "Rollback must disarm the cleanup trap so the canary continues serving"
    )
    assert "exit 1" in rollback_region, "Rollback must exit non-zero"


# ── B1: server_prep.sh aborts firewall setup on UFW SSH rule failure ──

def test_server_prep_aborts_on_ufw_ssh_failure():
    """B1: the prep script must `exit 1` (not continue) if the UFW SSH rule
    cannot be added, otherwise users can lock themselves out silently."""
    script_path = Path(__file__).resolve().parents[2] / "ilha" / "scripts" / "server_prep.sh"
    contents = script_path.read_text()

    # Find the SSH-rule guard and confirm it exits.
    assert "ufw allow 22/tcp" in contents
    # Split around the key line and check the block that follows contains exit 1
    # before the next non-nested statement.
    idx = contents.find('Failed to add UFW SSH rule')
    assert idx != -1, "expected firewall lockout warning string"
    tail = contents[idx:idx + 400]
    assert "exit 1" in tail, (
        "server_prep.sh must `exit 1` after logging the UFW SSH failure; "
        "otherwise the script continues and installs Docker while claiming the "
        "firewall was aborted (silent lockout risk)."
    )
