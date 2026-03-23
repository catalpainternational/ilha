"""
Unit tests for CaddyManager high-level functions.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

from ilha.commands.caddy import CaddyManager


class TestCaddyManager:
    """Test CaddyManager high-level functions."""
    
    @pytest.fixture
    def caddy_manager(self):
        """Create CaddyManager instance with mocked dependencies."""
        with patch('ilha.commands.caddy.DockerManager'), \
             patch('ilha.commands.caddy.get_project_root'), \
             patch('ilha.commands.caddy.get_script_dir'):
            
            manager = CaddyManager()
            manager.docker_manager = Mock()
            manager.project_root = Path("/test/project")
            manager.compose_file = Mock()
            manager.caddyfile = Mock()
            return manager
    
    def test_start_global_caddy_network_creation_fails(self, caddy_manager):
        """Test start_global_caddy when network creation fails."""
        caddy_manager.docker_manager.create_network.return_value = False
        
        result = caddy_manager.start_global_caddy()
        
        assert result is False
        caddy_manager.docker_manager.create_network.assert_called_once()
    
    def test_start_global_caddy_compose_file_not_found(self, caddy_manager):
        """Test start_global_caddy when compose file doesn't exist."""
        # The implementation reads compose_file.read_text() which will raise FileNotFoundError
        # if the file doesn't exist. The exception will propagate.
        caddy_manager.docker_manager.create_network.return_value = True
        caddy_manager._ensure_caddy_volumes = Mock(return_value=True)
        caddy_manager._handle_existing_container = Mock(return_value=True)
        caddy_manager.is_caddy_running = Mock(return_value=False)
        caddy_manager.compose_file.read_text = Mock(side_effect=FileNotFoundError("Compose file not found"))
        
        # The exception will propagate, so we expect it
        with pytest.raises(FileNotFoundError):
            caddy_manager.start_global_caddy()
        
        caddy_manager.docker_manager.create_network.assert_called_once()
    
    def test_start_global_caddy_success(self, caddy_manager):
        """Test successful global Caddy start."""
        caddy_manager.docker_manager.create_network.return_value = True
        caddy_manager.compose_file.exists.return_value = True
        caddy_manager.docker_manager.start_services.return_value = True
        caddy_manager.is_caddy_running = Mock(return_value=False)  # Prevent early return
        caddy_manager._ensure_caddy_volumes = Mock(return_value=True)
        caddy_manager._handle_existing_container = Mock(return_value=True)
        caddy_manager._get_compose_content_with_paths = Mock(return_value="version: '3'\nservices: {}")
        
        result = caddy_manager.start_global_caddy()
        
        assert result is True
        caddy_manager.docker_manager.create_network.assert_called_once()
        # start_services is called with a temp file, not compose_file
        assert caddy_manager.docker_manager.start_services.called
    
    def test_start_global_caddy_services_fail(self, caddy_manager):
        """Test start_global_caddy when services fail to start."""
        caddy_manager.docker_manager.create_network.return_value = True
        caddy_manager.compose_file.exists.return_value = True
        caddy_manager.docker_manager.start_services.return_value = False
        caddy_manager.is_caddy_running = Mock(return_value=False)  # Prevent early return
        caddy_manager._ensure_caddy_volumes = Mock(return_value=True)
        caddy_manager._handle_existing_container = Mock(return_value=True)
        caddy_manager._get_compose_content_with_paths = Mock(return_value="version: '3'\nservices: {}")
        
        result = caddy_manager.start_global_caddy()
        
        assert result is False
        caddy_manager.docker_manager.create_network.assert_called_once()
        assert caddy_manager.docker_manager.start_services.called
    
    def test_stop_global_caddy_compose_file_not_found(self, caddy_manager):
        """Test stop_global_caddy when compose file doesn't exist."""
        # The implementation reads compose_file.read_text() which will raise FileNotFoundError
        # if the file doesn't exist. The exception will propagate.
        caddy_manager.compose_file.read_text = Mock(side_effect=FileNotFoundError("Compose file not found"))
        
        # The exception will propagate, so we expect it
        with pytest.raises(FileNotFoundError):
            caddy_manager.stop_global_caddy()
        
        caddy_manager.docker_manager.stop_services.assert_not_called()
    
    def test_stop_global_caddy_success(self, caddy_manager):
        """Test successful global Caddy stop."""
        caddy_manager.docker_manager.stop_services.return_value = True
        caddy_manager._get_compose_content_with_paths = Mock(return_value="version: '3'\nservices: {}")
        
        result = caddy_manager.stop_global_caddy()
        
        assert result is True
        # stop_services is called with a temp file, not compose_file
        assert caddy_manager.docker_manager.stop_services.called
    
    def test_stop_global_caddy_services_fail(self, caddy_manager):
        """Test stop_global_caddy when services fail to stop."""
        caddy_manager.docker_manager.stop_services.return_value = False
        caddy_manager._get_compose_content_with_paths = Mock(return_value="version: '3'\nservices: {}")
        
        result = caddy_manager.stop_global_caddy()
        
        assert result is False
        assert caddy_manager.docker_manager.stop_services.called
    
    @patch('ilha.commands.caddy.validate_container_running')
    def test_is_caddy_running_true(self, mock_validate, caddy_manager):
        """Test is_caddy_running when Caddy is running."""
        mock_validate.return_value = True
        
        result = caddy_manager.is_caddy_running()
        
        assert result is True
        mock_validate.assert_called_once_with("ilha_caddy_proxy")
    
    @patch('ilha.commands.caddy.validate_container_running')
    def test_is_caddy_running_false(self, mock_validate, caddy_manager):
        """Test is_caddy_running when Caddy is not running."""
        mock_validate.return_value = False
        
        result = caddy_manager.is_caddy_running()
        
        assert result is False
        mock_validate.assert_called_once_with("ilha_caddy_proxy")
    
    @patch('ilha.commands.caddy.validate_container_running')
    def test_get_caddy_status(self, mock_validate, caddy_manager):
        """Test getting Caddy status information."""
        mock_validate.return_value = True
        caddy_manager.compose_file.exists.return_value = True
        caddy_manager.caddyfile.exists.return_value = True
        caddy_manager.docker_manager.create_network.return_value = True
        
        result = caddy_manager.get_caddy_status()
        
        expected = {
            "running": True,
            "on_network": True,
            "compose_file_exists": True,
            "caddyfile_exists": True,
            "network_exists": True
        }
        
        assert result == expected
        mock_validate.assert_called_once_with("ilha_caddy_proxy")
        caddy_manager.compose_file.exists.assert_called_once()
        caddy_manager.caddyfile.exists.assert_called_once()
        caddy_manager.docker_manager.create_network.assert_called_once()
    
    @patch('ilha.commands.caddy.validate_container_exists')
    @patch('ilha.commands.caddy.validate_container_running')
    def test_get_caddy_status_not_running(self, mock_validate, mock_exists, caddy_manager):
        """Test getting Caddy status when not running."""
        mock_validate.return_value = False
        mock_exists.return_value = False  # container not present, so on_network stays False
        caddy_manager.compose_file.exists.return_value = False
        caddy_manager.caddyfile.exists.return_value = False
        caddy_manager.docker_manager.create_network.return_value = False
        
        result = caddy_manager.get_caddy_status()
        
        expected = {
            "running": False,
            "on_network": False,
            "compose_file_exists": False,
            "caddyfile_exists": False,
            "network_exists": False
        }
        
        assert result == expected
        mock_validate.assert_called_once_with("ilha_caddy_proxy")
        caddy_manager.compose_file.exists.assert_called_once()
        caddy_manager.caddyfile.exists.assert_called_once()
        caddy_manager.docker_manager.create_network.assert_called_once()
