"""
Unit tests for VolumeManager high-level functions.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

from ilha.commands.volumes import VolumeManager


class TestVolumeManager:
    """Test VolumeManager high-level functions."""
    
    @pytest.fixture
    def volume_manager(self):
        """Create VolumeManager instance with mocked dependencies."""
        with patch('ilha.commands.volumes.DockerManager'), \
             patch('ilha.commands.volumes.EnvironmentManager'):
            
            manager = VolumeManager()
            manager.docker_manager = Mock()
            manager.env_manager = Mock()
            return manager
    
    def test_list_volumes_empty(self, volume_manager):
        """Test listing volumes when no volumes exist."""
        volume_manager.docker_manager.list_volumes.return_value = []
        
        # Should not raise an exception
        volume_manager.list_volumes()
        
        volume_manager.docker_manager.list_volumes.assert_called_once()
    
    def test_list_volumes_with_volumes(self, volume_manager):
        """Test listing volumes when volumes exist."""
        volumes = ["test-branch_postgres_data", "test-branch_redis_data"]
        volume_manager.docker_manager.list_volumes.return_value = volumes
        
        # Should not raise an exception
        volume_manager.list_volumes()
        
        volume_manager.docker_manager.list_volumes.assert_called_once()
    
    def test_show_volume_sizes_empty(self, volume_manager):
        """Test showing volume sizes when no volumes exist."""
        volume_manager.docker_manager.get_volume_sizes.return_value = {}
        
        # Should not raise an exception
        volume_manager.show_volume_sizes()
        
        volume_manager.docker_manager.get_volume_sizes.assert_called_once()
    
    def test_show_volume_sizes_with_volumes(self, volume_manager):
        """Test showing volume sizes when volumes exist."""
        sizes = {
            "test-branch_postgres_data": "1.2GB",
            "test-branch_redis_data": "50MB"
        }
        volume_manager.docker_manager.get_volume_sizes.return_value = sizes
        
        # Should not raise an exception
        volume_manager.show_volume_sizes()
        
        volume_manager.docker_manager.get_volume_sizes.assert_called_once()
    
    def test_backup_volumes_empty_branch_name(self, volume_manager):
        """Test backup_volumes with empty branch name."""
        result = volume_manager.backup_volumes("")
        assert result == False
    
    def test_backup_volumes_none_branch_name(self, volume_manager):
        """Test backup_volumes with None branch name."""
        result = volume_manager.backup_volumes(None)
        assert result == False
    
    @patch('pathlib.Path.cwd')
    def test_backup_volumes_default_backup_dir(self, mock_cwd, volume_manager):
        """Test backup_volumes with default backup directory."""
        branch_name = "test-branch"
        backup_file = Path("/test/backups/backup_test-branch_20240101_120000.tar")
        mock_cwd.return_value = Path("/test")
        
        volume_manager.docker_manager.backup_volumes.return_value = backup_file
        
        result = volume_manager.backup_volumes(branch_name)
        
        assert result == True
        volume_manager.docker_manager.backup_volumes.assert_called_once_with(
            branch_name, Path("/test/backups")
        )
    
    def test_backup_volumes_custom_backup_dir(self, volume_manager):
        """Test backup_volumes with custom backup directory."""
        branch_name = "test-branch"
        backup_dir = Path("/custom/backups")
        backup_file = Path("/custom/backups/backup_test-branch_20240101_120000.tar")
        
        volume_manager.docker_manager.backup_volumes.return_value = backup_file
        
        result = volume_manager.backup_volumes(branch_name, backup_dir)
        
        assert result == True
        volume_manager.docker_manager.backup_volumes.assert_called_once_with(branch_name, backup_dir)
    
    def test_backup_volumes_failure(self, volume_manager):
        """Test backup_volumes when backup fails."""
        branch_name = "test-branch"
        backup_dir = Path("/test/backups")
        
        volume_manager.docker_manager.backup_volumes.return_value = None
        
        result = volume_manager.backup_volumes(branch_name, backup_dir)
        
        assert result == False
        volume_manager.docker_manager.backup_volumes.assert_called_once_with(branch_name, backup_dir)
    
    def test_restore_volumes_empty_branch_name(self, volume_manager):
        """Test restore_volumes with empty branch name."""
        backup_file = Path("/test/backup.tar")
        result = volume_manager.restore_volumes("", backup_file)
        assert result == False
    
    def test_restore_volumes_none_branch_name(self, volume_manager):
        """Test restore_volumes with None branch name."""
        backup_file = Path("/test/backup.tar")
        result = volume_manager.restore_volumes(None, backup_file)
        assert result == False
    
    @patch('pathlib.Path.exists')
    def test_restore_volumes_backup_file_not_found(self, mock_exists, volume_manager):
        """Test restore_volumes when backup file doesn't exist."""
        branch_name = "test-branch"
        backup_file = Path("/test/nonexistent.tar")
        mock_exists.return_value = False
        
        result = volume_manager.restore_volumes(branch_name, backup_file)
        
        assert result == False
        volume_manager.docker_manager.restore_volumes.assert_not_called()
    
    @patch('pathlib.Path.exists')
    def test_restore_volumes_success(self, mock_exists, volume_manager):
        """Test successful volume restore."""
        branch_name = "test-branch"
        backup_file = Path("/test/backup.tar")
        mock_exists.return_value = True
        
        volume_manager.docker_manager.restore_volumes.return_value = True
        
        result = volume_manager.restore_volumes(branch_name, backup_file)
        
        assert result == True
        volume_manager.docker_manager.restore_volumes.assert_called_once_with(branch_name, backup_file)
    
    @patch('pathlib.Path.exists')
    def test_restore_volumes_failure(self, mock_exists, volume_manager):
        """Test restore_volumes when restore fails."""
        branch_name = "test-branch"
        backup_file = Path("/test/backup.tar")
        mock_exists.return_value = True
        
        volume_manager.docker_manager.restore_volumes.return_value = False
        
        result = volume_manager.restore_volumes(branch_name, backup_file)
        
        assert result == False
        volume_manager.docker_manager.restore_volumes.assert_called_once_with(branch_name, backup_file)
    
    def test_clean_volumes_empty_branch_name(self, volume_manager):
        """Test clean_volumes with empty branch name."""
        result = volume_manager.clean_volumes("")
        assert result == False
    
    def test_clean_volumes_none_branch_name(self, volume_manager):
        """Test clean_volumes with None branch name."""
        result = volume_manager.clean_volumes(None)
        assert result == False
    
    def test_clean_volumes_success(self, volume_manager):
        """Test successful volume cleanup."""
        branch_name = "test-branch"
        
        volume_manager.docker_manager.remove_volumes.return_value = True
        
        result = volume_manager.clean_volumes(branch_name)
        
        assert result == True
        volume_manager.docker_manager.remove_volumes.assert_called_once_with(branch_name)
    
    def test_clean_volumes_partial_failure(self, volume_manager):
        """Test clean_volumes with partial failure."""
        branch_name = "test-branch"
        
        volume_manager.docker_manager.remove_volumes.return_value = False
        
        result = volume_manager.clean_volumes(branch_name)
        
        assert result == False
        volume_manager.docker_manager.remove_volumes.assert_called_once_with(branch_name)
    
    def test_get_volume_info(self, volume_manager):
        """Test getting volume information."""
        branch_name = "test-branch"
        volume_names = {
            "postgres": "test-branch_postgres_data",
            "redis": "test-branch_redis_data",
            "media": "test-branch_media_files"
        }
        volume_sizes = {
            "test-branch_postgres_data": "1.2GB",
            "test-branch_redis_data": "50MB",
            "test-branch_media_files": "200MB"
        }
        all_volumes = ["test-branch_postgres_data", "test-branch_redis_data", "test-branch_media_files"]
        
        volume_manager.env_manager.get_worktree_volume_names.return_value = volume_names
        volume_manager.docker_manager.get_volume_sizes.return_value = volume_sizes
        volume_manager.docker_manager.list_volumes.return_value = all_volumes
        
        result = volume_manager.get_volume_info(branch_name)
        
        expected = {
            "branch_name": branch_name,
            "volume_names": volume_names,
            "volume_sizes": volume_sizes,
            "total_volumes": len(volume_names),
            "volumes_exist": True
        }
        
        assert result == expected
        volume_manager.env_manager.get_worktree_volume_names.assert_called_once_with(branch_name)
        volume_manager.docker_manager.get_volume_sizes.assert_called_once()
    
    def test_get_volume_info_no_volumes(self, volume_manager):
        """Test getting volume information when no volumes exist."""
        branch_name = "test-branch"
        volume_names = {}
        volume_sizes = {}
        
        volume_manager.env_manager.get_worktree_volume_names.return_value = volume_names
        volume_manager.docker_manager.get_volume_sizes.return_value = volume_sizes
        
        result = volume_manager.get_volume_info(branch_name)
        
        expected = {
            "branch_name": branch_name,
            "volume_names": volume_names,
            "volume_sizes": volume_sizes,
            "total_volumes": 0,
            "volumes_exist": False
        }
        
        assert result == expected
        volume_manager.env_manager.get_worktree_volume_names.assert_called_once_with(branch_name)
        volume_manager.docker_manager.get_volume_sizes.assert_called_once()
