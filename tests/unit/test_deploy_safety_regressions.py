"""Regression tests for HIGH-severity deploy bugs fixed in Phase 3.

Each test pins one of the behaviors from the critical analysis so the bug
class cannot silently return via a future refactor. Bug IDs refer to the
analysis in the feature-slim-deploy branch discussion.
"""

from pathlib import Path

from ilha.core.docker_manager import DockerManager


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


# B4 coverage lives in tests/unit/test_phase6_architecture.py::
# test_zd_deploy_script_keeps_b4_rollback_guard, which reads the extracted
# zd_deploy.sh file directly. The previous duplicate here asserted the same
# invariants against the Python-rendered script body; since the rendered
# script is literally the file contents plus an exports header, the
# assertions were identical.


# ── B1: server_prep aborts firewall setup on UFW SSH rule failure ──

def test_server_prep_aborts_on_ufw_ssh_failure():
    """B1: prep must stop (not continue) if the UFW SSH rule cannot be added."""
    script_path = Path(__file__).resolve().parents[2] / "ilha" / "scripts" / "server_prep.py"
    contents = script_path.read_text()

    assert '["ufw", "allow", "22/tcp"]' in contents
    idx = contents.find("Failed to add UFW SSH rule")
    assert idx != -1, "expected firewall lockout warning string"
    tail = contents[idx : idx + 400]
    assert "return False" in tail, (
        "server_prep.py must return False after logging the UFW SSH failure "
        "(do not enable UFW or continue Docker setup)."
    )
