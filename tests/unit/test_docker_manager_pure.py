"""
Deterministic unit tests for the isolatable parts of ilha.core.docker_manager.DockerManager.

No real Docker is used: subprocess and validation helpers are monkeypatched.

Regressions covered:
  * CORE-2: ``_compose_project_name`` resolves the project name from
    ``self.project_root`` (via ``resolve_project_name``) — NOT the current
    working directory — so container/volume lookups target the right project.
  * CORE-1: ``backup_volumes`` writes per-volume archives as ``{volume}.tar.gz``
    using ``tar czf`` (gzip), NOT an uncompressed ``{volume}.tar`` / ``tar cf``
    (an uncompressed archive is silently unmatched on restore -> data loss).
  * CORE-10: ``get_worktree_containers`` / ``get_worktree_volumes`` are now
    synchronous (not coroutines); the ``*_sync`` wrappers delegate to them.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ilha.core.docker_manager import DockerManager


# ---------------------------------------------------------------------------
# Construction helper
# ---------------------------------------------------------------------------

def _make_manager(project_root: Path) -> DockerManager:
    """Construct a DockerManager without touching real Docker."""
    with patch("ilha.core.docker_manager.validate_docker_running", return_value=True), \
         patch("ilha.core.docker_manager.get_compose_command", return_value="docker compose"):
        manager = DockerManager(project_root=project_root, validate=False)
    manager.compose_cmd = "docker compose"
    return manager


@pytest.fixture
def project_root(tmp_path):
    root = tmp_path / "myproject"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# _compose_project_name  (CORE-2 regression)
# ---------------------------------------------------------------------------

class TestComposeProjectName:
    def test_uses_config_project_name_from_project_root(self, tmp_path):
        root = tmp_path / "somedir"
        (root / ".ilha").mkdir(parents=True)
        (root / ".ilha" / "config.yml").write_text("project_name: business_intelligence\n")
        manager = _make_manager(root)

        # underscores -> hyphens, lowercased, anchored to project_root config
        assert manager._compose_project_name("ainaro") == "business-intelligence-ainaro"

    def test_falls_back_to_directory_name(self, tmp_path):
        root = tmp_path / "MyProject"
        root.mkdir()
        manager = _make_manager(root)
        # no config.yml -> directory name is used, sanitized
        assert manager._compose_project_name("feature") == "myproject-feature"

    def test_branch_name_is_sanitized(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        manager = _make_manager(root)
        assert manager._compose_project_name("feature/login_v2") == "proj-feature-login-v2"

    def test_uses_project_root_not_cwd(self, tmp_path, monkeypatch):
        """CORE-2: even when cwd differs, the name is derived from project_root."""
        root = tmp_path / "anchored"
        (root / ".ilha").mkdir(parents=True)
        (root / ".ilha" / "config.yml").write_text("project_name: anchored_project\n")
        manager = _make_manager(root)

        # Change cwd to an unrelated directory with a DIFFERENT .ilha config
        other = tmp_path / "elsewhere"
        (other / ".ilha").mkdir(parents=True)
        (other / ".ilha" / "config.yml").write_text("project_name: wrong_project\n")
        monkeypatch.chdir(other)

        # Must reflect project_root, not cwd
        assert manager._compose_project_name("br") == "anchored-project-br"


# ---------------------------------------------------------------------------
# backup_volumes  (CORE-1 regression: per-volume {volume}.tar.gz via `tar czf`)
# ---------------------------------------------------------------------------

class TestBackupVolumes:
    def _install_subprocess_capture(self, monkeypatch, calls):
        """Patch docker_manager.subprocess.run to capture argv and, for the
        alpine per-volume tar step, actually create the expected .tar.gz on the
        host (so backup_volumes' sanity checks pass and it proceeds to return
        the combined archive path)."""

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else cmd)
            # Simulate the alpine per-volume backup producing its output file.
            if isinstance(cmd, (list, tuple)) and "alpine" in cmd and "tar" in cmd:
                # Find the host path bound to /backup
                host_backup = None
                for i, tok in enumerate(cmd):
                    if isinstance(tok, str) and tok.endswith(":/backup"):
                        host_backup = tok[: -len(":/backup")]
                # Find output path following "czf"
                out = None
                if "czf" in cmd:
                    out = cmd[cmd.index("czf") + 1]
                if host_backup and out:
                    filename = Path(out).name  # e.g. myvol.tar.gz
                    (Path(host_backup) / filename).write_bytes(b"fake-archive")
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("ilha.core.docker_manager.subprocess.run", fake_run)

    def test_per_volume_archives_are_tar_gz_via_tar_czf(self, project_root, monkeypatch):
        manager = _make_manager(project_root)
        # Not running -> no orchestrator start/stop
        monkeypatch.setattr(manager, "_is_worktree_running", lambda b: False)
        monkeypatch.setattr("ilha.core.docker_manager.validate_volume_exists", lambda v: True)

        calls = []
        self._install_subprocess_capture(monkeypatch, calls)

        backup_dir = project_root / "backups"
        volume_names = {
            "postgres": "myproject-feature_postgres_data",
            "redis": "myproject-feature_redis_data",
        }
        result = manager.backup_volumes("feature", backup_dir, volume_names_override=volume_names)

        # Should succeed and return the combined backup path
        assert result == backup_dir / "backup_feature.tar"

        # Collect the alpine per-volume tar commands
        alpine_cmds = [c for c in calls if isinstance(c, list) and "alpine" in c and "tar" in c]
        assert len(alpine_cmds) == 2

        produced_outputs = []
        for c in alpine_cmds:
            # CORE-1: must use gzip create (`czf`), NOT plain `cf`
            assert "czf" in c, f"expected gzip tar 'czf' in {c}"
            assert "cf" not in c, f"must not use uncompressed 'cf' in {c}"
            out = c[c.index("czf") + 1]
            produced_outputs.append(out)
            # Output filename must be {volume}.tar.gz, not a bare {volume}.tar
            assert out.endswith(".tar.gz"), f"per-volume archive must be .tar.gz, got {out}"
            assert not out.endswith(".tar") or out.endswith(".tar.gz")

        assert "/backup/myproject-feature_postgres_data.tar.gz" in produced_outputs
        assert "/backup/myproject-feature_redis_data.tar.gz" in produced_outputs

    def test_override_wins_over_legacy_lookup(self, project_root, monkeypatch):
        """When volume_names_override is passed, the legacy get_volume_names_for_project_root
        convention must not be consulted."""
        manager = _make_manager(project_root)
        monkeypatch.setattr(manager, "_is_worktree_running", lambda b: False)
        monkeypatch.setattr("ilha.core.docker_manager.validate_volume_exists", lambda v: True)

        def boom(*a, **k):
            raise AssertionError("legacy get_volume_names_for_project_root should not be called")

        monkeypatch.setattr("ilha.core.docker_manager.get_volume_names_for_project_root", boom)

        calls = []
        self._install_subprocess_capture(monkeypatch, calls)

        result = manager.backup_volumes(
            "feature", project_root / "b",
            volume_names_override={"postgres": "explicit_postgres_data"},
        )
        assert result == project_root / "b" / "backup_feature.tar"
        alpine_cmds = [c for c in calls if isinstance(c, list) and "alpine" in c]
        assert any("/backup/explicit_postgres_data.tar.gz" in c for c in alpine_cmds)

    def test_aborts_when_a_volume_is_missing(self, project_root, monkeypatch):
        """If a per-volume archive is never produced, backup returns None (no
        partial/truncated archive)."""
        manager = _make_manager(project_root)
        monkeypatch.setattr(manager, "_is_worktree_running", lambda b: False)
        # Volume does not exist -> _backup_one_volume returns False -> abort
        monkeypatch.setattr("ilha.core.docker_manager.validate_volume_exists", lambda v: False)

        calls = []
        self._install_subprocess_capture(monkeypatch, calls)

        result = manager.backup_volumes(
            "feature", project_root / "b",
            volume_names_override={"postgres": "myproject-feature_postgres_data"},
        )
        assert result is None


# ---------------------------------------------------------------------------
# CORE-10: get_worktree_containers / get_worktree_volumes are SYNC
# ---------------------------------------------------------------------------

class TestSyncQueryMethods:
    def test_get_worktree_containers_is_not_coroutine(self):
        assert not asyncio.iscoroutinefunction(DockerManager.get_worktree_containers)

    def test_get_worktree_volumes_is_not_coroutine(self):
        assert not asyncio.iscoroutinefunction(DockerManager.get_worktree_volumes)

    def test_sync_wrappers_are_not_coroutines(self):
        assert not asyncio.iscoroutinefunction(DockerManager.get_worktree_containers_sync)
        assert not asyncio.iscoroutinefunction(DockerManager.get_worktree_volumes_sync)

    def test_containers_sync_delegates(self, project_root, monkeypatch):
        manager = _make_manager(project_root)
        sentinel = [{"name": "c1", "state": "running"}]
        monkeypatch.setattr(manager, "get_worktree_containers", lambda b: sentinel)
        assert manager.get_worktree_containers_sync("feature") is sentinel

    def test_volumes_sync_delegates(self, project_root, monkeypatch):
        manager = _make_manager(project_root)
        sentinel = [{"name": "v1", "exists": True}]
        monkeypatch.setattr(manager, "get_worktree_volumes", lambda b: sentinel)
        assert manager.get_worktree_volumes_sync("feature") is sentinel

    def test_get_worktree_containers_parses_docker_ps(self, project_root, monkeypatch):
        manager = _make_manager(project_root)

        def fake_run(cmd, *args, **kwargs):
            return MagicMock(
                returncode=0,
                stdout="proj-feature-web|Up 3 minutes|0.0.0.0:8000->8000/tcp|myimage\n",
                stderr="",
            )

        monkeypatch.setattr("ilha.core.docker_manager.subprocess.run", fake_run)
        containers = manager.get_worktree_containers("feature")
        assert containers == [{
            "name": "proj-feature-web",
            "status": "Up 3 minutes",
            "state": "running",
            "ports": "0.0.0.0:8000->8000/tcp",
            "image": "myimage",
        }]

    def test_get_worktree_containers_empty_when_docker_fails(self, project_root, monkeypatch):
        manager = _make_manager(project_root)
        monkeypatch.setattr(
            "ilha.core.docker_manager.subprocess.run",
            lambda *a, **k: MagicMock(returncode=1, stdout="", stderr="err"),
        )
        assert manager.get_worktree_containers("feature") == []

    def test_get_worktree_volumes_lists_existing(self, project_root, monkeypatch):
        manager = _make_manager(project_root)
        # get_worktree_volumes imports these locally from their source modules
        monkeypatch.setattr(
            "ilha.config.settings.get_volume_names",
            lambda b: {"postgres": "p_data", "redis": "r_data"},
        )
        # only postgres volume exists
        monkeypatch.setattr(
            "ilha.utils.validation.validate_volume_exists",
            lambda v: v == "p_data",
        )
        volumes = manager.get_worktree_volumes("feature")
        assert volumes == [{
            "name": "p_data",
            "type": "postgres",
            "branch": "feature",
            "exists": True,
        }]
