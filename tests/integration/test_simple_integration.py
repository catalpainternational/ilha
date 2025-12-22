"""
Simple integration tests for ilha CLI.
"""

import pytest
import subprocess
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

from ilha.core.docker_manager import DockerManager
from ilha.core.environment_manager import EnvironmentManager


class TestSimpleIntegration:
    """Test simple integration functionality."""
    
    @pytest.fixture(scope="class")
    def docker_manager(self):
        """Create Docker manager instance."""
        return DockerManager()
    
    @pytest.fixture(scope="class")
    def env_manager(self):
        """Create Environment manager instance."""
        return EnvironmentManager()
    
    def test_docker_daemon_running(self):
        """Test that Docker daemon is running."""
        result = subprocess.run(["docker", "info"], capture_output=True, check=True)
        assert result.returncode == 0, "Docker daemon is not running"
    
    def test_network_creation(self, docker_manager):
        """Test network creation functionality."""
        network_name = "test_ilha_network_simple"
        
        # Clean up any existing test network
        try:
            subprocess.run(["docker", "network", "rm", network_name], 
                         capture_output=True, check=False)
        except:
            pass
        
        # Test network creation
        result = docker_manager.create_network(network_name)
        assert result, "Failed to create network"
        
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
        test_branch = "test-volume-branch-simple"
        
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
    
    def test_environment_configuration(self, env_manager):
        """Test environment configuration."""
        test_branch = "test-env-branch"
        
        # Test volume names
        volume_names = env_manager.get_worktree_volume_names(test_branch)
        assert volume_names["postgres"] == f"{test_branch}_postgres_data"
        assert volume_names["redis"] == f"{test_branch}_redis_data"
        assert volume_names["media"] == f"{test_branch}_media_files"
        
        # Test environment variables
        env_vars = env_manager.get_environment_variables(test_branch)
        assert env_vars["COMPOSE_PROJECT_NAME"] == test_branch
        assert env_vars["SITE_DOMAIN"] == f"{test_branch}.localhost"
        assert f"{test_branch}.localhost" in env_vars["ALLOWED_HOSTS"]
        
        # Test domain name
        domain = env_manager.get_domain_name(test_branch)
        assert domain == f"{test_branch}.localhost"
        
        # Test database URL
        db_url = env_manager.get_database_url(test_branch)
        assert f"{test_branch}-db" in db_url
        assert "test_project" in db_url
        
        # Test Redis URL
        redis_url = env_manager.get_redis_url(test_branch)
        assert f"{test_branch}-redis" in redis_url

