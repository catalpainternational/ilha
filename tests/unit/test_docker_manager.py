"""
Unit tests for DockerManager high-level functions.
"""

import pytest
import subprocess
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

from ilha.core.docker_manager import DockerManager


class TestDockerManager:
    """Test DockerManager high-level functions."""
    
    @pytest.fixture
    def docker_manager(self):
        """Create DockerManager instance with mocked dependencies."""
        with patch('ilha.core.docker_manager.validate_docker_running'), \
             patch('ilha.core.docker_manager.get_compose_command'):
            
            manager = DockerManager()
            manager.compose_cmd = "docker compose"
            return manager
    
    @patch('ilha.core.docker_manager.validate_volume_exists')
    @patch('subprocess.run')
    def test_copy_volume_source_not_exists(self, mock_run, mock_validate, docker_manager):
        """Test copy_volume when source volume doesn't exist."""
        source_volume = "source-volume"
        target_volume = "target-volume"
        
        mock_validate.return_value = False
        mock_run.return_value = Mock(returncode=0)
        
        result = docker_manager.copy_volume(source_volume, target_volume)
        
        assert result == True
        mock_validate.assert_called_once_with(source_volume)
        mock_run.assert_called_once_with(
            ["docker", "volume", "create", target_volume],
            check=True,
            capture_output=True
        )
    
    @patch('ilha.core.docker_manager.validate_volume_exists')
    @patch('subprocess.run')
    def test_copy_volume_success(self, mock_run, mock_validate, docker_manager):
        """Test successful volume copy."""
        source_volume = "source-volume"
        target_volume = "target-volume"
        
        mock_validate.return_value = True
        mock_run.return_value = Mock(returncode=0)
        
        result = docker_manager.copy_volume(source_volume, target_volume)
        
        assert result == True
        mock_validate.assert_called_once_with(source_volume)
        assert mock_run.call_count == 2  # create target volume + copy data
    
    @patch('ilha.core.docker_manager.validate_volume_exists')
    @patch('subprocess.run')
    def test_copy_volume_copy_failure(self, mock_run, mock_validate, docker_manager):
        """Test volume copy with copy operation failure."""
        source_volume = "source-volume"
        target_volume = "target-volume"
        
        mock_validate.return_value = True
        # First call (create volume) succeeds, second call (copy) fails
        mock_run.side_effect = [Mock(returncode=0), Exception("Copy failed")]
        
        with pytest.raises(Exception, match="Copy failed"):
            docker_manager.copy_volume(source_volume, target_volume)
        
        mock_validate.assert_called_once_with(source_volume)
    
    @patch('ilha.core.docker_manager.get_volume_names')
    @patch.object(DockerManager, 'copy_volume')
    def test_create_worktree_volumes_success(self, mock_copy_volume, mock_get_volume_names, docker_manager):
        """Test successful worktree volume creation.
        
        Note: Creates postgres, redis, media, and sqlite volumes.
        Caddy volumes are shared globally and not per-worktree.
        """
        branch_name = "test-branch"
        project_name = "test-project"
        
        mock_get_volume_names.return_value = {
            "postgres": "test-branch_postgres_data",
            "redis": "test-branch_redis_data",
            "media": "test-branch_media_files",
            "sqlite": "test-branch_sqlite_data",
        }
        mock_copy_volume.return_value = True
        
        result = docker_manager.create_worktree_volumes(branch_name, project_name)
        
        assert result == True
        mock_get_volume_names.assert_called_once_with(branch_name)
        assert mock_copy_volume.call_count == 4  # One for each volume type (postgres, redis, media, sqlite)
    
    @patch('ilha.core.docker_manager.get_volume_names')
    @patch.object(DockerManager, 'copy_volume')
    def test_create_worktree_volumes_partial_failure(self, mock_copy_volume, mock_get_volume_names, docker_manager):
        """Test worktree volume creation with partial failure."""
        branch_name = "test-branch"
        project_name = "test-project"
        
        mock_get_volume_names.return_value = {
            "postgres": "test-branch_postgres_data",
            "redis": "test-branch_redis_data",
            "media": "test-branch_media_files",
            "sqlite": "test-branch_sqlite_data",
        }
        # First three succeed, fourth fails
        mock_copy_volume.side_effect = [True, True, True, False]
        
        result = docker_manager.create_worktree_volumes(branch_name, project_name)
        
        assert result == False
        mock_get_volume_names.assert_called_once_with(branch_name)
        assert mock_copy_volume.call_count == 4
    
    @patch('ilha.core.docker_manager.get_volume_names')
    @patch('ilha.core.docker_manager.validate_volume_exists')
    @patch('subprocess.run')
    def test_remove_volumes_success(self, mock_run, mock_validate, mock_get_volume_names, docker_manager):
        """Test successful volume removal.
        
        Note: Removes postgres, redis, media, and sqlite volumes.
        Caddy volumes are shared globally and not removed per-worktree.
        """
        branch_name = "test-branch"
        
        mock_get_volume_names.return_value = {
            "postgres": "test-branch_postgres_data",
            "redis": "test-branch_redis_data",
            "media": "test-branch_media_files",
            "sqlite": "test-branch_sqlite_data",
        }
        mock_validate.return_value = True
        mock_run.return_value = Mock(returncode=0)
        
        result = docker_manager.remove_volumes(branch_name)
        
        assert result == True
        mock_get_volume_names.assert_called_once_with(branch_name)
        assert mock_validate.call_count == 4
        assert mock_run.call_count == 4
    
    @patch('ilha.core.docker_manager.get_volume_names')
    @patch('ilha.core.docker_manager.validate_volume_exists')
    @patch('subprocess.run')
    def test_remove_volumes_partial_failure(self, mock_run, mock_validate, mock_get_volume_names, docker_manager):
        """Test volume removal with partial failure."""
        branch_name = "test-branch"
        
        mock_get_volume_names.return_value = {
            "postgres": "test-branch_postgres_data",
            "redis": "test-branch_redis_data",
            "media": "test-branch_media_files",
        }
        mock_validate.return_value = True
        # First two succeed, third fails with CalledProcessError
        mock_run.side_effect = [Mock(returncode=0), Mock(returncode=0), subprocess.CalledProcessError(1, "docker", "Remove failed")]
        
        result = docker_manager.remove_volumes(branch_name)
        
        assert result == False
        mock_get_volume_names.assert_called_once_with(branch_name)
        assert mock_validate.call_count == 3
        assert mock_run.call_count == 3
    
    @patch('ilha.core.docker_manager.get_volume_names_for_project_root')
    @patch('subprocess.run')
    def test_backup_volumes_success(self, mock_run, mock_get_volume_names, docker_manager):
        """Test successful volume backup."""
        branch_name = "test-branch"
        backup_dir = Path("/test/backups")

        mock_get_volume_names.return_value = {
            "postgres": "test-branch_postgres_data",
            "redis": "test-branch_redis_data",
            "media": "test-branch_media_files",
        }
        mock_run.return_value = Mock(returncode=0)

        # Mock the backup file creation
        with patch('pathlib.Path.mkdir'), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('pathlib.Path.unlink'), \
             patch('shutil.rmtree'):

            result = docker_manager.backup_volumes(branch_name, backup_dir)

        assert result is not None
        assert result.name.startswith("backup_test-branch")
        assert result.name.endswith(".tar")
        assert result.suffix == ".tar"
    
    @patch('ilha.core.docker_manager.get_volume_names_for_project_root')
    @patch('subprocess.run')
    def test_restore_volumes_success(self, mock_run, mock_get_volume_names_for_project_root, docker_manager):
        """Test successful volume restore."""
        branch_name = "test-branch"
        backup_file = Path("/test/backup.tar")
        
        mock_get_volume_names_for_project_root.return_value = {
            "postgres": "test-branch_postgres_data",
            "redis": "test-branch_redis_data",
            "media": "test-branch_media_files",
        }
        mock_run.return_value = Mock(returncode=0)
        
        # Mock the restore process - need to mock _is_worktree_running and Path operations
        # Create a proper mock for restore_temp_dir that supports / operator
        from unittest.mock import MagicMock
        mock_restore_dir = MagicMock(spec=Path)
        mock_restore_dir.glob.return_value = []  # No nested backups found
        mock_restore_dir.absolute.return_value = Path("/test/restore_temp")
        mock_restore_dir.stat.return_value = Mock(st_size=1024 * 1024)
        mock_restore_dir.mkdir.return_value = None
        # Make restore_dir support / operator for volume backup paths
        def mock_restore_truediv(self, other):
            # Return a mock path for volume backup files
            mock_vol_backup = Mock(spec=Path)
            mock_vol_backup.exists.return_value = False  # No backup files found
            return mock_vol_backup
        mock_restore_dir.__truediv__ = mock_restore_truediv
        
        # Mock backup_file.parent to support the / operator
        mock_backup_parent = MagicMock()
        # When parent / "restore_temp" is called, return our mock_restore_dir
        def mock_truediv(self, other):
            if other == "restore_temp":
                return mock_restore_dir
            return Path(str(self) + "/" + str(other))
        mock_backup_parent.__truediv__ = mock_truediv
        
        # Mock backup_file as a Mock so we can control its parent
        mock_backup_file = Mock(spec=Path)
        mock_backup_file.exists.return_value = True
        mock_backup_file.stat.return_value = Mock(st_size=1024 * 1024)
        mock_backup_file.name = 'backup.tar'
        mock_backup_file.parent = mock_backup_parent
        
        with patch.object(docker_manager, '_is_worktree_running', return_value=False), \
             patch('pathlib.Path.unlink'), \
             patch('shutil.rmtree'), \
             patch('subprocess.run', return_value=Mock(returncode=0, stderr='', stdout='')):
            
            result = docker_manager.restore_volumes(branch_name, mock_backup_file)
        
        assert result == True
        mock_get_volume_names_for_project_root.assert_called_once_with(docker_manager.project_root, branch_name)
    
    @patch('subprocess.run')
    def test_start_services_success(self, mock_run, docker_manager):
        """Test successful service start."""
        compose_file = Path("/test/docker-compose.yml")
        env_file = Path("/test/.ilha/env.ilha")
        project_name = "test-project"
        
        mock_run.return_value = Mock(returncode=0)
        
        # Mock the env_file.exists() to return True
        with patch.object(Path, 'exists', return_value=True):
            result = docker_manager.start_services(compose_file, env_file, project_name)
        
        assert result == True
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "docker" in call_args
        assert "compose" in call_args
        assert "--env-file" in call_args
        assert str(env_file) in call_args
        assert "-f" in call_args
        assert str(compose_file) in call_args
        assert "up" in call_args
        assert "-d" in call_args
    
    @patch('subprocess.run')
    def test_stop_services_success(self, mock_run, docker_manager):
        """Test successful service stop."""
        compose_file = Path("/test/docker-compose.yml")
        env_file = Path("/test/.ilha/env.ilha")
        project_name = "test-project"
        
        mock_run.return_value = Mock(returncode=0)
        
        # Mock the env_file.exists() to return True
        with patch.object(Path, 'exists', return_value=True):
            result = docker_manager.stop_services(compose_file, env_file, project_name)
        
        assert result == True
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "docker" in call_args
        assert "compose" in call_args
        assert "--env-file" in call_args
        assert str(env_file) in call_args
        assert "-f" in call_args
        assert str(compose_file) in call_args
        assert "down" in call_args
    
    @patch('subprocess.run')
    def test_run_compose_command_success(self, mock_run, docker_manager):
        """Test successful compose command execution."""
        compose_file = Path("/test/docker-compose.yml")
        command = ["config"]
        project_name = "test-project"
        
        mock_run.return_value = Mock(returncode=0)
        
        success, stderr = docker_manager.run_compose_command(compose_file, command, None, project_name)
        
        assert success is True
        assert stderr is None
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "docker" in call_args
        assert "compose" in call_args
        assert "-f" in call_args
        assert str(compose_file) in call_args
        assert "config" in call_args
    
    @patch('subprocess.run')
    def test_run_compose_command_failure(self, mock_run, docker_manager):
        """Test compose command execution failure."""
        compose_file = Path("/test/docker-compose.yml")
        command = ["config"]
        project_name = "test-project"
        
        mock_run.side_effect = subprocess.CalledProcessError(1, "docker", stderr="Compose command failed")
        
        success, stderr = docker_manager.run_compose_command(compose_file, command, None, project_name)
        
        assert success is False
        assert stderr == "Compose command failed"
        mock_run.assert_called_once()
    
    @patch('subprocess.run')
    def test_list_volumes_success(self, mock_run, docker_manager):
        """Test successful volume listing."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="test-branch_postgres_data\ntest-branch_redis_data\n"
        )
        
        result = docker_manager.list_volumes()
        
        assert result == ["test-branch_postgres_data", "test-branch_redis_data"]
        mock_run.assert_called_once_with(
            ["docker", "volume", "ls", "-q"],
            capture_output=True,
            text=True,
            check=True
        )
    
    @patch('subprocess.run')
    def test_get_volume_sizes_success(self, mock_run, docker_manager):
        """Test successful volume size retrieval."""
        # First call returns volume list, subsequent calls return size for each volume
        mock_run.side_effect = [
            Mock(returncode=0, stdout="test-volume_postgres_data\ntest-volume_redis_data"),
            Mock(returncode=0, stdout="1.2GB\t/data"),
            Mock(returncode=0, stdout="800MB\t/data")
        ]
        
        result = docker_manager.get_volume_sizes()
        
        assert "test-volume_postgres_data" in result
        assert result["test-volume_postgres_data"] == "1.2GB"
        assert mock_run.call_count == 3
    
