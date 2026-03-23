"""
Integration tests for Docker operations.
"""

import pytest
import subprocess
import time
from pathlib import Path
from unittest.mock import patch, Mock

from ilha.core.docker_manager import DockerManager
from ilha.utils.validation import validate_docker_running, validate_network_exists


class TestDockerIntegration:
    """Test Docker integration functionality."""
    
    @pytest.fixture(scope="class")
    def docker_manager(self):
        """Create Docker manager instance."""
        return DockerManager()
    
    def test_docker_daemon_running(self):
        """Test that Docker daemon is running."""
        assert validate_docker_running(), "Docker daemon is not running"
    
    def test_network_creation(self, docker_manager):
        """Test network creation functionality."""
        network_name = "test_ilha_network"
        
        # Clean up any existing test network
        try:
            subprocess.run(["docker", "network", "rm", network_name], 
                         capture_output=True, check=False)
        except:
            pass
        
        # Test network creation
        result = docker_manager.create_network(network_name)
        assert result, "Failed to create network"
        
        # Verify network exists
        assert validate_network_exists(network_name), "Network was not created"
        
        # Test creating existing network (should succeed)
        result = docker_manager.create_network(network_name)
        assert result, "Failed to handle existing network"
        
        # Cleanup
        try:
            subprocess.run(["docker", "network", "rm", network_name], 
                         capture_output=True, check=False)
        except:
            pass
    
    def test_volume_operations(self, docker_manager):
        """Test volume creation and management."""
        test_branch = "test-volume-branch"
        
        # Test volume creation
        result = docker_manager.create_worktree_volumes(test_branch)
        assert result, "Failed to create worktree volumes"
        
        # Verify volumes exist
        volume_names = {
            "postgres": f"{test_branch}_postgres_data",
            "redis": f"{test_branch}_redis_data", 
            "media": f"{test_branch}_media_files"
        }
        
        for volume_type, volume_name in volume_names.items():
            result = subprocess.run(["docker", "volume", "inspect", volume_name],
                                  capture_output=True, check=False)
            assert result.returncode == 0, f"Volume {volume_name} was not created"
        
        # Test volume removal
        result = docker_manager.remove_volumes(test_branch)
        assert result, "Failed to remove volumes"
        
        # Verify volumes are removed
        for volume_type, volume_name in volume_names.items():
            result = subprocess.run(["docker", "volume", "inspect", volume_name],
                                  capture_output=True, check=False)
            assert result.returncode != 0, f"Volume {volume_name} was not removed"
    
    def test_compose_command_execution(self, docker_manager, temp_project_dir):
        """Test Docker Compose command execution."""
        # Create a simple test compose file
        compose_file = temp_project_dir / "docker-compose.test.yml"
        compose_file.write_text("""
version: '3.8'
services:
  test:
    image: alpine:latest
    command: echo "test"
""")
        
        # Test compose command execution
        success, _ = docker_manager.run_compose_command(
            compose_file, 
            ["config"], 
            project_name="test-project"
        )
        assert success, "Failed to execute compose command"
    
    def test_volume_backup_restore(self, docker_manager, temp_project_dir):
        """Test volume backup and restore functionality."""
        test_branch = "test-backup-branch"
        backup_dir = temp_project_dir / "backups"
        backup_dir.mkdir(exist_ok=True)
        
        # Create test volumes
        docker_manager.create_worktree_volumes(test_branch)
        
        # Create some test data in a volume
        volume_name = f"{test_branch}_postgres_data"
        subprocess.run([
            "docker", "run", "--rm", "-v", f"{volume_name}:/data",
            "alpine", "sh", "-c", "echo 'test data' > /data/test.txt"
        ], check=True, capture_output=True)
        
        # Test backup
        backup_file = docker_manager.backup_volumes(test_branch, backup_dir)
        assert backup_file is not None, "Backup failed"
        assert backup_file.exists(), "Backup file was not created"
        
        # Remove original volumes
        docker_manager.remove_volumes(test_branch)
        
        # Test restore
        result = docker_manager.restore_volumes(test_branch, backup_file)
        assert result, "Restore failed"
        
        # Verify data was restored
        result = subprocess.run([
            "docker", "run", "--rm", "-v", f"{volume_name}:/data",
            "alpine", "cat", "/data/test.txt"
        ], capture_output=True, text=True, check=True)
        
        assert "test data" in result.stdout, "Data was not restored correctly"
        
        # Cleanup
        docker_manager.remove_volumes(test_branch)
        backup_file.unlink(missing_ok=True)

