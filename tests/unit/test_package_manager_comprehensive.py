"""
Comprehensive, deterministic unit tests for ilha.core.package_manager.PackageManager.

These tests exercise the *pure / isolatable* parts of PackageManager without any
real Docker: environment file packaging/restore, metadata generation, checksum
verification, and the public ``export_package`` signature.

Context / regressions covered here:
  * CORE-3: env packaging now actually happens (``_copy_environment_files`` copies
    .env / .ilha / docker-compose.ilha.yml into the package's ``environment/`` dir).
  * worker-droplet / hub-and-spoke support was REMOVED. Metadata must NOT contain
    'vpc_deployment' or 'container_filter'. ``export_package`` / ``_generate_metadata``
    / ``_copy_environment_files`` must NOT accept container_filter / exclude_deps /
    droplet_info / central_droplet_info.

All Docker/git dependencies are replaced with fakes at construction time.
"""

import inspect
import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

from ilha.core.package_manager import PackageManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pm(tmp_path):
    """A PackageManager whose heavy managers are replaced with fakes.

    None of the methods under test touch docker/git/orchestrator, but the
    ``__init__`` constructs them eagerly (which would hit real Docker), so we
    patch the classes for the duration of construction.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    with patch("ilha.core.package_manager.DockerManager", MagicMock()), \
         patch("ilha.core.package_manager.GitManager", MagicMock()), \
         patch("ilha.core.package_manager.EnvironmentManager", MagicMock()), \
         patch("ilha.core.package_manager.WorktreeOrchestrator", MagicMock()):
        manager = PackageManager(project_root=project_root)
    return manager


def _make_worktree(base: Path) -> Path:
    """Create a realistic worktree directory with env files."""
    wt = base / "worktrees" / "feature-x"
    wt.mkdir(parents=True)

    (wt / ".env").write_text("FOO=bar\nSECRET=1\n")

    ilha_dir = wt / ".ilha"
    ilha_dir.mkdir()
    (ilha_dir / "env.ilha").write_text("COMPOSE_PROJECT_NAME=project-feature-x\n")
    (ilha_dir / "config.yml").write_text("project_name: project\n")
    (ilha_dir / "docker-compose.overlay.yml").write_text(
        "services:\n  web:\n    labels:\n      - caddy.proxy=web.localhost\n"
    )

    (wt / "docker-compose.ilha.yml").write_text("services:\n  web:\n    image: x\n")
    return wt


# ---------------------------------------------------------------------------
# _copy_environment_files  (CORE-3 regression)
# ---------------------------------------------------------------------------

class TestCopyEnvironmentFiles:
    def test_copies_env_ilha_and_compose_into_package(self, pm, tmp_path):
        wt = _make_worktree(tmp_path)
        package_dir = tmp_path / "pkg.ilha-package"
        package_dir.mkdir()

        result = pm._copy_environment_files(wt, package_dir)

        assert result is True
        env_dir = package_dir / "environment"
        assert env_dir.is_dir()
        # .env packaged (this is the core of the CORE-3 regression)
        assert (env_dir / ".env").is_file()
        assert (env_dir / ".env").read_text() == "FOO=bar\nSECRET=1\n"
        # .ilha directory copied recursively with contents
        assert (env_dir / ".ilha").is_dir()
        assert (env_dir / ".ilha" / "env.ilha").is_file()
        assert (env_dir / ".ilha" / "config.yml").is_file()
        assert (env_dir / ".ilha" / "docker-compose.overlay.yml").is_file()
        # docker-compose.ilha.yml copied
        assert (env_dir / "docker-compose.ilha.yml").is_file()

    def test_creates_environment_dir_even_when_files_missing(self, pm, tmp_path):
        """A worktree with no env files still yields an environment/ dir and True."""
        wt = tmp_path / "worktrees" / "bare"
        wt.mkdir(parents=True)
        package_dir = tmp_path / "pkg.ilha-package"
        package_dir.mkdir()

        result = pm._copy_environment_files(wt, package_dir)

        assert result is True
        env_dir = package_dir / "environment"
        assert env_dir.is_dir()
        assert not (env_dir / ".env").exists()
        assert not (env_dir / ".ilha").exists()

    def test_only_env_file_present(self, pm, tmp_path):
        wt = tmp_path / "worktrees" / "onlyenv"
        wt.mkdir(parents=True)
        (wt / ".env").write_text("A=1\n")
        package_dir = tmp_path / "pkg.ilha-package"
        package_dir.mkdir()

        assert pm._copy_environment_files(wt, package_dir) is True
        assert (package_dir / "environment" / ".env").read_text() == "A=1\n"
        assert not (package_dir / "environment" / ".ilha").exists()

    def test_signature_has_no_removed_worker_params(self):
        sig = inspect.signature(PackageManager._copy_environment_files)
        params = set(sig.parameters)
        assert params == {"self", "worktree_path", "package_dir"}
        for removed in ("container_filter", "exclude_deps", "droplet_info", "central_droplet_info"):
            assert removed not in params


# ---------------------------------------------------------------------------
# _restore_environment_files  (round-trip)
# ---------------------------------------------------------------------------

class TestRestoreEnvironmentFiles:
    def test_round_trip_restores_files(self, pm, tmp_path):
        # First package a worktree
        src_wt = _make_worktree(tmp_path)
        package_dir = tmp_path / "pkg.ilha-package"
        package_dir.mkdir()
        assert pm._copy_environment_files(src_wt, package_dir) is True

        # Now restore into a fresh, empty worktree
        dst_wt = tmp_path / "worktrees" / "restored"
        dst_wt.mkdir(parents=True)

        result = pm._restore_environment_files(package_dir, dst_wt)

        assert result is True
        assert (dst_wt / ".env").read_text() == "FOO=bar\nSECRET=1\n"
        assert (dst_wt / ".ilha" / "env.ilha").is_file()
        assert (dst_wt / ".ilha" / "config.yml").is_file()
        assert (dst_wt / ".ilha" / "docker-compose.overlay.yml").is_file()
        assert (dst_wt / "docker-compose.ilha.yml").is_file()

    def test_returns_false_when_environment_missing(self, pm, tmp_path):
        package_dir = tmp_path / "pkg.ilha-package"
        package_dir.mkdir()  # no environment/ subdir
        dst_wt = tmp_path / "worktrees" / "restored"
        dst_wt.mkdir(parents=True)

        assert pm._restore_environment_files(package_dir, dst_wt) is False

    def test_restore_overwrites_existing_ilha(self, pm, tmp_path):
        src_wt = _make_worktree(tmp_path)
        package_dir = tmp_path / "pkg.ilha-package"
        package_dir.mkdir()
        pm._copy_environment_files(src_wt, package_dir)

        dst_wt = tmp_path / "worktrees" / "restored"
        dst_wt.mkdir(parents=True)
        stale = dst_wt / ".ilha"
        stale.mkdir()
        (stale / "stale.txt").write_text("old")

        assert pm._restore_environment_files(package_dir, dst_wt) is True
        # stale file must be gone (dir was replaced), packaged files present
        assert not (dst_wt / ".ilha" / "stale.txt").exists()
        assert (dst_wt / ".ilha" / "env.ilha").is_file()

    def test_signature_has_no_removed_worker_params(self):
        sig = inspect.signature(PackageManager._restore_environment_files)
        params = set(sig.parameters)
        assert params == {"self", "package_dir", "worktree_path", "preserve_domain_settings"}
        for removed in ("container_filter", "droplet_info", "central_droplet_info"):
            assert removed not in params


# ---------------------------------------------------------------------------
# _generate_metadata
# ---------------------------------------------------------------------------

class TestGenerateMetadata:
    def _make_package_with_files(self, tmp_path) -> Path:
        package_dir = tmp_path / "pkg.ilha-package"
        (package_dir / "environment").mkdir(parents=True)
        (package_dir / "environment" / ".env").write_text("A=1\n")
        (package_dir / "volumes").mkdir()
        (package_dir / "volumes" / "backup_feature-x.tar").write_bytes(b"\x00\x01\x02")
        (package_dir / "code").mkdir()
        (package_dir / "code" / "feature-x.tar.gz").write_bytes(b"gzdata")
        return package_dir

    def test_metadata_has_expected_keys(self, pm, tmp_path):
        package_dir = self._make_package_with_files(tmp_path)
        with patch("ilha.core.package_manager.get_project_name", return_value="testproj"):
            metadata = pm._generate_metadata("feature-x", package_dir, include_code=True, skip_volumes=False)

        for key in ("package_version", "branch_name", "include_code", "skip_volumes", "checksums"):
            assert key in metadata
        assert metadata["branch_name"] == "feature-x"
        assert metadata["include_code"] is True
        assert metadata["skip_volumes"] is False
        assert metadata["project_name"] == "testproj"

    def test_checksums_cover_all_files(self, pm, tmp_path):
        package_dir = self._make_package_with_files(tmp_path)
        # Count files BEFORE metadata.json is written (rglob happens before write)
        expected_files = {
            str(p.relative_to(package_dir))
            for p in package_dir.rglob("*")
            if p.is_file()
        }
        with patch("ilha.core.package_manager.get_project_name", return_value="testproj"):
            metadata = pm._generate_metadata("feature-x", package_dir, include_code=True)

        assert set(metadata["checksums"].keys()) == expected_files
        # metadata.json is written after checksums are computed, so it is NOT self-included
        assert "metadata.json" not in metadata["checksums"]
        # every checksum is a non-empty string
        assert all(isinstance(v, str) and v for v in metadata["checksums"].values())

    def test_metadata_json_written_and_reloadable(self, pm, tmp_path):
        package_dir = self._make_package_with_files(tmp_path)
        with patch("ilha.core.package_manager.get_project_name", return_value="testproj"):
            metadata = pm._generate_metadata("feature-x", package_dir, include_code=False, skip_volumes=True)

        metadata_path = package_dir / "metadata.json"
        assert metadata_path.is_file()
        reloaded = json.loads(metadata_path.read_text())
        assert reloaded["branch_name"] == "feature-x"
        assert reloaded["skip_volumes"] is True
        assert reloaded["checksums"] == metadata["checksums"]

    def test_metadata_has_no_vpc_deployment_or_container_filter(self, pm, tmp_path):
        """Worker-droplet / hub-and-spoke removal regression."""
        package_dir = self._make_package_with_files(tmp_path)
        with patch("ilha.core.package_manager.get_project_name", return_value="testproj"):
            metadata = pm._generate_metadata("feature-x", package_dir, include_code=True)

        assert "vpc_deployment" not in metadata
        assert "container_filter" not in metadata
        assert "central_droplet_info" not in metadata
        assert "droplet_info" not in metadata
        assert "exclude_deps" not in metadata

    def test_signature_has_no_removed_worker_params(self):
        sig = inspect.signature(PackageManager._generate_metadata)
        params = set(sig.parameters)
        assert params == {"self", "branch_name", "package_dir", "include_code", "skip_volumes"}
        for removed in ("container_filter", "exclude_deps", "droplet_info", "central_droplet_info"):
            assert removed not in params


# ---------------------------------------------------------------------------
# _verify_package_checksums
# ---------------------------------------------------------------------------

class TestVerifyPackageChecksums:
    def test_matching_checksums_pass(self, pm, tmp_path):
        package_dir = tmp_path / "pkg.ilha-package"
        (package_dir / "environment").mkdir(parents=True)
        (package_dir / "environment" / ".env").write_text("A=1\n")
        (package_dir / "data.bin").write_bytes(b"hello world")

        with patch("ilha.core.package_manager.get_project_name", return_value="testproj"):
            metadata = pm._generate_metadata("feature-x", package_dir, include_code=False)

        assert pm._verify_package_checksums(package_dir, metadata) is True

    def test_tampered_file_fails(self, pm, tmp_path):
        package_dir = tmp_path / "pkg.ilha-package"
        (package_dir / "environment").mkdir(parents=True)
        target = package_dir / "environment" / ".env"
        target.write_text("A=1\n")

        with patch("ilha.core.package_manager.get_project_name", return_value="testproj"):
            metadata = pm._generate_metadata("feature-x", package_dir, include_code=False)

        # Tamper AFTER metadata generation
        target.write_text("A=2  # TAMPERED\n")

        assert pm._verify_package_checksums(package_dir, metadata) is False

    def test_missing_file_fails(self, pm, tmp_path):
        package_dir = tmp_path / "pkg.ilha-package"
        (package_dir / "environment").mkdir(parents=True)
        target = package_dir / "environment" / ".env"
        target.write_text("A=1\n")

        with patch("ilha.core.package_manager.get_project_name", return_value="testproj"):
            metadata = pm._generate_metadata("feature-x", package_dir, include_code=False)

        target.unlink()  # remove the file entirely

        assert pm._verify_package_checksums(package_dir, metadata) is False

    def test_empty_checksums_passes(self, pm, tmp_path):
        package_dir = tmp_path / "pkg.ilha-package"
        package_dir.mkdir()
        assert pm._verify_package_checksums(package_dir, {"checksums": {}}) is True
        assert pm._verify_package_checksums(package_dir, {}) is True


# ---------------------------------------------------------------------------
# export_package signature  (worker-droplet removal)
# ---------------------------------------------------------------------------

class TestExportPackageSignature:
    def test_accepts_expected_params(self):
        sig = inspect.signature(PackageManager.export_package)
        params = set(sig.parameters)
        assert params == {
            "self", "branch_name", "output_dir",
            "include_code", "compressed", "skip_volumes",
        }

    def test_rejects_removed_worker_params(self):
        sig = inspect.signature(PackageManager.export_package)
        params = set(sig.parameters)
        for removed in (
            "container_filter", "exclude_deps",
            "droplet_info", "central_droplet_info",
        ):
            assert removed not in params

    def test_defaults_are_stable(self):
        sig = inspect.signature(PackageManager.export_package)
        assert sig.parameters["include_code"].default is False
        assert sig.parameters["compressed"].default is True
        assert sig.parameters["skip_volumes"].default is False
