"""
Unit tests for environment manager.
"""

import pytest
from unittest.mock import Mock, patch, mock_open
from pathlib import Path
from ilha.core.environment_manager import EnvironmentManager, HOST_PORT_RANGES


class TestEnvironmentManager:
    """Test environment manager functionality."""
    
    def test_init(self):
        """Test environment manager initialization."""
        env_manager = EnvironmentManager()
        assert env_manager is not None
    
    @patch('ilha.config.settings.get_project_name')
    def test_get_worktree_volume_names(self, mock_project_name):
        """Test worktree volume name generation.
        
        Note: Caddy volumes are not included as they are shared globally,
        not worktree-specific.
        """
        mock_project_name.return_value = "test_project"
        env_manager = EnvironmentManager()
        volume_names = env_manager.get_worktree_volume_names("test-branch")
        
        expected = {
            "postgres": "test-project-test-branch_postgres_data",
            "redis": "test-project-test-branch_redis_data",
            "media": "test-project-test-branch_media_files",
            "sqlite": "test-project-test-branch_sqlite_data",
        }
        assert volume_names == expected
    
    @patch('ilha.config.settings.get_project_name')
    def test_get_environment_variables(self, mock_project_name):
        """Test environment variable generation."""
        mock_project_name.return_value = "test_project"
        env_manager = EnvironmentManager()
        env_vars = env_manager.get_environment_variables("test-branch")
        
        assert env_vars["COMPOSE_PROJECT_NAME"] == "test-project-test-branch"
        assert env_vars["SITE_DOMAIN"] == "test-project-test-branch.localhost"
        assert "test-project-test-branch.localhost" in env_vars["ALLOWED_HOSTS"]
        assert "localhost" in env_vars["ALLOWED_HOSTS"]
        assert "127.0.0.1" in env_vars["ALLOWED_HOSTS"]
    
    @patch('ilha.config.settings.get_project_name')
    def test_get_domain_name(self, mock_project_name):
        """Test domain name generation."""
        mock_project_name.return_value = "test_project"
        env_manager = EnvironmentManager()
        domain = env_manager.get_domain_name("test-branch")
        assert domain == "test-project-test-branch.localhost"
    
    @patch('ilha.config.settings.get_project_name')
    def test_get_allowed_hosts(self, mock_project_name):
        """Test allowed hosts generation."""
        mock_project_name.return_value = "test_project"
        env_manager = EnvironmentManager()
        hosts = env_manager.get_allowed_hosts("test-branch")
        # The implementation now includes container names
        assert "localhost" in hosts
        assert "test-project-test-branch.localhost" in hosts
        assert "*.localhost" in hosts
    
    @patch('ilha.config.settings.get_project_name')
    @patch('ilha.utils.env_loader.load_env_file')
    @patch('os.getenv')
    @patch('pathlib.Path.exists')
    def test_get_database_url(self, mock_exists, mock_getenv, mock_load_env, mock_project_name):
        """Test database URL generation."""
        mock_project_name.return_value = "test_project"
        # Mock environment variables
        mock_load_env.return_value = {
            'POSTGRES_USER': 'biuser',
            'POSTGRES_PASSWORD': 'bipassword',
            'POSTGRES_DB': 'database'
        }
        mock_getenv.side_effect = lambda key, default=None: {
            'POSTGRES_USER': 'biuser',
            'POSTGRES_PASSWORD': 'bipassword',
            'POSTGRES_DB': 'database'
        }.get(key, default)
        mock_exists.return_value = True
        
        env_manager = EnvironmentManager()
        db_url = env_manager.get_database_url("test-branch")
        expected = "postgres://biuser:bipassword@test-project-test-branch-db:5432/database"
        assert db_url == expected
    
    @patch('ilha.config.settings.get_project_name')
    @patch('ilha.utils.env_loader.load_env_file')
    @patch('os.getenv')
    @patch('pathlib.Path.exists')
    def test_get_database_url_custom_params(self, mock_exists, mock_getenv, mock_load_env, mock_project_name):
        """Test database URL generation with custom parameters via environment."""
        mock_project_name.return_value = "test_project"
        # Mock environment variables with custom values
        mock_load_env.return_value = {
            'POSTGRES_USER': 'custom_user',
            'POSTGRES_PASSWORD': 'custom_pass',
            'POSTGRES_DB': 'custom_db'
        }
        mock_getenv.side_effect = lambda key, default=None: {
            'POSTGRES_USER': 'custom_user',
            'POSTGRES_PASSWORD': 'custom_pass',
            'POSTGRES_DB': 'custom_db'
        }.get(key, default)
        mock_exists.return_value = True
        
        env_manager = EnvironmentManager()
        # The method no longer accepts custom params, it reads from env
        db_url = env_manager.get_database_url("test-branch")
        expected = "postgres://custom_user:custom_pass@test-project-test-branch-db:5432/custom_db"
        assert db_url == expected
    
    @patch('ilha.config.settings.get_project_name')
    def test_get_redis_url(self, mock_project_name):
        """Test Redis URL generation."""
        mock_project_name.return_value = "test_project"
        env_manager = EnvironmentManager()
        redis_url = env_manager.get_redis_url("test-branch")
        expected = "redis://test-project-test-branch-redis:6379/0"
        assert redis_url == expected
    
    @patch('ilha.config.settings.get_project_name')
    def test_get_redis_url_custom_params(self, mock_project_name):
        """Test Redis URL generation with custom parameters."""
        mock_project_name.return_value = "test_project"
        env_manager = EnvironmentManager()
        redis_url = env_manager.get_redis_url("test-branch", redis_port=6380, redis_db=1)
        expected = "redis://test-project-test-branch-redis:6380/1"
        assert redis_url == expected
    
    @patch('ilha.config.settings.get_project_name')
    @patch('ilha.utils.env_loader.load_env_file')
    @patch('os.getenv')
    @patch('pathlib.Path.exists')
    def test_generate_compose_environment(self, mock_exists, mock_getenv, mock_load_env, mock_project_name):
        """Test compose environment generation."""
        mock_project_name.return_value = "test_project"
        # Mock environment variables for database URL
        mock_load_env.return_value = {
            'POSTGRES_USER': 'biuser',
            'POSTGRES_PASSWORD': 'bipassword',
            'POSTGRES_DB': 'database'
        }
        mock_getenv.side_effect = lambda key, default=None: {
            'POSTGRES_USER': 'biuser',
            'POSTGRES_PASSWORD': 'bipassword',
            'POSTGRES_DB': 'database'
        }.get(key, default)
        mock_exists.return_value = True
        
        env_manager = EnvironmentManager()
        compose_env = env_manager.generate_compose_environment("test-branch")
        
        assert compose_env["COMPOSE_PROJECT_NAME"] == "test-project-test-branch"
        assert compose_env["DATABASE_URL"] == "postgres://biuser:bipassword@test-project-test-branch-db:5432/database"
        assert compose_env["REDIS_HOST"] == "test-project-test-branch-redis"
        assert compose_env["REDIS_PORT"] == "6379"
        assert compose_env["REDIS_DB"] == "0"
    
    @patch('pathlib.Path.exists')
    @patch('pathlib.Path.write_text')
    def test_validate_environment_file_success(self, mock_write, mock_exists):
        """Test environment file validation success."""
        mock_exists.return_value = True
        
        env_manager = EnvironmentManager()
        env_file_path = Path("/test/.env")
        
        with patch('pathlib.Path.read_text', return_value="TEST_VAR=value"):
            assert env_manager.validate_environment_file(env_file_path) == True
    
    @patch('pathlib.Path.exists')
    def test_validate_environment_file_not_exists(self, mock_exists):
        """Test environment file validation when file doesn't exist."""
        mock_exists.return_value = False
        
        env_manager = EnvironmentManager()
        env_file_path = Path("/test/.env")
        
        assert env_manager.validate_environment_file(env_file_path) == False
    
    @patch('pathlib.Path.exists')
    def test_validate_environment_file_empty(self, mock_exists):
        """Test environment file validation when file is empty."""
        mock_exists.return_value = True
        
        env_manager = EnvironmentManager()
        env_file_path = Path("/test/.env")
        
        with patch('pathlib.Path.read_text', return_value=""):
            assert env_manager.validate_environment_file(env_file_path) == False
    
    @patch('pathlib.Path.exists')
    def test_validate_environment_file_read_error(self, mock_exists):
        """Test environment file validation when read fails."""
        mock_exists.return_value = True
        
        env_manager = EnvironmentManager()
        env_file_path = Path("/test/.env")
        
        with patch('pathlib.Path.read_text', side_effect=Exception("Read error")):
            assert env_manager.validate_environment_file(env_file_path) == False
    
    @patch('ilha.config.settings.get_project_name')
    @patch('ilha.utils.env_loader.load_env_file')
    @patch('os.getenv')
    @patch('pathlib.Path.exists')
    def test_get_worktree_config(self, mock_exists, mock_getenv, mock_load_env, mock_project_name):
        """Test complete worktree configuration generation."""
        mock_project_name.return_value = "test_project"
        # Mock environment variables for database URL (matching the expected assertion)
        mock_load_env.return_value = {
            'POSTGRES_USER': 'biuser',
            'POSTGRES_PASSWORD': 'bipassword',
            'POSTGRES_DB': 'database'
        }
        mock_getenv.side_effect = lambda key, default=None: {
            'POSTGRES_USER': 'biuser',
            'POSTGRES_PASSWORD': 'bipassword',
            'POSTGRES_DB': 'database'
        }.get(key, default)
        mock_exists.return_value = True  # Mock env file exists
        
        env_manager = EnvironmentManager()
        config = env_manager.get_worktree_config("test-branch")
        
        assert config["branch_name"] == "test-branch"
        assert config["domain_name"] == "test-project-test-branch.localhost"
        # allowed_hosts now includes container name
        assert "localhost" in config["allowed_hosts"]
        assert "test-project-test-branch.localhost" in config["allowed_hosts"]
        assert "*.localhost" in config["allowed_hosts"]
        assert "postgres://biuser:bipassword@test-project-test-branch-db:5432/database" in config["database_url"]
        assert "redis://test-project-test-branch-redis:6379/0" in config["redis_url"]
        assert "postgres" in config["volume_names"]
        assert "redis" in config["volume_names"]
        assert "media" in config["volume_names"]
        assert "COMPOSE_PROJECT_NAME" in config["environment_variables"]
        assert "DATABASE_URL" in config["compose_environment"]
    
    @patch('ilha.config.settings.get_project_name')
    @patch('ilha.utils.env_loader.load_env_file')
    @patch('os.getenv')
    def test_create_env_file_from_template_success(self, mock_getenv, mock_load_env, mock_project_name):
        """Test environment file creation from template success."""
        mock_project_name.return_value = "test_project"
        # Mock environment variables for database URL
        mock_load_env.return_value = {
            'POSTGRES_USER': 'biuser',
            'POSTGRES_PASSWORD': 'bipassword',
            'POSTGRES_DB': 'database'
        }
        mock_getenv.side_effect = lambda key, default=None: {
            'POSTGRES_USER': 'biuser',
            'POSTGRES_PASSWORD': 'bipassword',
            'POSTGRES_DB': 'database'
        }.get(key, default)
        template_content = "COMPOSE_PROJECT_NAME={{BRANCH_NAME}}\nSITE_DOMAIN={{DOMAIN_NAME}}"
        
        env_manager = EnvironmentManager()
        # Create mock Path objects
        mock_template_path = Mock(spec=Path)
        mock_template_path.exists.return_value = True
        mock_template_path.read_text.return_value = template_content
        mock_target_path = Mock(spec=Path)
        mock_write = mock_target_path.write_text
        
        result = env_manager.create_env_file_from_template(mock_template_path, mock_target_path, "test-branch")
        
        assert result is True
        mock_write.assert_called_once()
        # Check that placeholders were replaced
        call_args = mock_write.call_args[0][0]
        # The content should have placeholders replaced
        assert "COMPOSE_PROJECT_NAME=test-branch" in call_args
        # Check that DOMAIN_NAME was replaced (not still {{DOMAIN_NAME}})
        assert "{{DOMAIN_NAME}}" not in call_args
        assert "test-project-test-branch.localhost" in call_args
    
    @patch('pathlib.Path.exists')
    def test_create_env_file_from_template_not_exists(self, mock_exists):
        """Test environment file creation from template when template doesn't exist."""
        mock_exists.return_value = False
        
        env_manager = EnvironmentManager()
        template_path = Path("/test/template.env")
        target_path = Path("/test/.env")
        
        result = env_manager.create_env_file_from_template(template_path, target_path, "test-branch")
        assert result == False
    
    @patch('pathlib.Path.exists')
    @patch('pathlib.Path.read_text')
    def test_create_env_file_from_template_read_error(self, mock_read, mock_exists):
        """Test environment file creation from template when read fails."""
        mock_exists.return_value = True
        mock_read.side_effect = Exception("Read error")
        
        env_manager = EnvironmentManager()
        template_path = Path("/test/template.env")
        target_path = Path("/test/.env")
        
        result = env_manager.create_env_file_from_template(template_path, target_path, "test-branch")
        assert result == False
    
    @patch('pathlib.Path.exists')
    @patch('pathlib.Path.unlink')
    def test_cleanup_environment_files_success(self, mock_unlink, mock_exists):
        """Test environment file cleanup success."""
        mock_exists.return_value = True
        
        env_manager = EnvironmentManager()
        worktree_path = Path("/test/worktree")
        
        result = env_manager.cleanup_environment_files(worktree_path)
        
        assert result == True
        assert mock_unlink.call_count == 2  # .env and env.ilha
    
    @patch('pathlib.Path.exists')
    @patch('pathlib.Path.unlink')
    def test_cleanup_environment_files_partial_failure(self, mock_unlink, mock_exists):
        """Test environment file cleanup with partial failure."""
        mock_exists.return_value = True
        mock_unlink.side_effect = [None, Exception("Delete error")]
        
        env_manager = EnvironmentManager()
        worktree_path = Path("/test/worktree")
        
        result = env_manager.cleanup_environment_files(worktree_path)
        
        assert result == False
        assert mock_unlink.call_count == 2

    def test_host_port_assignment_generates_unique_values(self, tmp_path, monkeypatch):
        """Host ports should be assigned within the configured ranges."""
        project_root = tmp_path
        (project_root / "worktrees").mkdir(parents=True, exist_ok=True)
        env_manager = EnvironmentManager(project_root=project_root)

        host_ports = env_manager._calculate_host_ports("alpha")

        assert set(host_ports.keys()) == set(HOST_PORT_RANGES.keys())
        for var, port in host_ports.items():
            start, end = HOST_PORT_RANGES[var]
            assert start <= port <= end

    def test_host_port_assignment_preserves_existing_values(self, tmp_path):
        """Existing host port assignments should be reused when regenerating env files."""
        project_root = tmp_path
        branch_dir = project_root / "worktrees" / "alpha"
        ilha_dir = branch_dir / ".ilha"
        ilha_dir.mkdir(parents=True, exist_ok=True)
        env_file = ilha_dir / "env.ilha"
        env_file.write_text(
            "ilha_DB_HOST_PORT=16000\n"
            "ilha_REDIS_HOST_PORT=27000\n"
            "ilha_WEB_HOST_PORT=38500\n"
        )

        env_manager = EnvironmentManager(project_root=project_root)
        host_ports = env_manager._calculate_host_ports("alpha")

        assert host_ports["ilha_DB_HOST_PORT"] == 16000
        assert host_ports["ilha_REDIS_HOST_PORT"] == 27000
        assert host_ports["ilha_WEB_HOST_PORT"] == 38500
