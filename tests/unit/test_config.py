"""
Unit tests for configuration management.
"""

import pytest
from unittest.mock import patch
from ilha.config.settings import (
    VERSION, AUTHOR, CADDY_NETWORK,
    get_project_root, get_worktree_paths, get_volume_names,
    generate_env_compose_content, PROTECTED_BRANCHES, DEFAULT_ENV_VARS,
    get_project_name, sanitize_project_name
)
from ilha.utils.validation import validate_branch_name


class TestConfiguration:
    """Test configuration constants and functions."""
    
    def test_version_constants(self):
        """Test version and author constants."""
        # Version is dynamic, just check it's a valid version string
        assert isinstance(VERSION, str)
        assert len(VERSION) > 0
        assert AUTHOR == "ilha Contributors"
        assert CADDY_NETWORK == "ilha_caddy_proxy"
    
    def test_project_name_dynamic(self):
        """Test that project name is determined dynamically."""
        project_name = get_project_name()
        assert isinstance(project_name, str)
        assert len(project_name) > 0
    
    def test_protected_branches(self):
        """Test protected branches list."""
        expected_branches = {"main", "master", "develop", "production", "staging"}
        assert PROTECTED_BRANCHES == expected_branches
    
    def test_default_env_vars(self):
        """Test default environment variables."""
        assert "POSTGRES_USER" in DEFAULT_ENV_VARS
        assert "POSTGRES_PASSWORD" in DEFAULT_ENV_VARS
        assert "POSTGRES_DB" in DEFAULT_ENV_VARS
        assert DEFAULT_ENV_VARS["POSTGRES_USER"] == "user"
        assert DEFAULT_ENV_VARS["POSTGRES_PASSWORD"] == "password"
        assert DEFAULT_ENV_VARS["POSTGRES_DB"] == "database"
    
    def test_get_project_root(self):
        """Test project root path resolution."""
        project_root = get_project_root()
        assert isinstance(project_root, type(project_root))
        # Project root should be a valid Path object
        assert hasattr(project_root, 'name')
    
    def test_get_worktree_paths(self):
        """Test worktree path resolution."""
        new_path, legacy_path = get_worktree_paths("test-branch")
        
        assert "test-branch" in str(new_path)
        assert "worktrees" in str(new_path)
        assert "test-branch" in str(legacy_path)
        assert new_path != legacy_path
    
    @patch('ilha.config.settings.get_project_name')
    def test_get_volume_names(self, mock_project_name):
        """Test volume name generation."""
        mock_project_name.return_value = "test_project"
        volume_names = get_volume_names("test-branch")
        
        assert volume_names["postgres"] == "test-project-test-branch_postgres_data"
        assert volume_names["redis"] == "test-project-test-branch_redis_data"
        assert volume_names["media"] == "test-project-test-branch_media_files"
    
    @patch('ilha.config.settings.get_project_name')
    def test_generate_env_compose_content(self, mock_project_name):
        """Test environment compose file content generation."""
        mock_project_name.return_value = "test_project"
        content = generate_env_compose_content("test-branch")
        
        assert "COMPOSE_PROJECT_NAME=test-project-test-branch" in content
        assert "SITE_DOMAIN=test-project-test-branch.localhost" in content
        assert "ALLOWED_HOSTS=localhost,127.0.0.1,test-project-test-branch.localhost,*.localhost" in content
        assert "# ilha environment configuration for test-branch" in content
    
    def test_sanitize_project_name(self):
        """Test project name sanitization."""
        assert sanitize_project_name("test_project") == "test-project"
        assert sanitize_project_name("Test Project") == "test-project"
        assert sanitize_project_name("test@project!") == "test-project"
        assert sanitize_project_name("my-cool_project") == "my-cool-project"
        assert sanitize_project_name("---test---") == "test"
    
    def test_sanitize_hostname(self):
        """Test hostname sanitization for RFC 1034/1035 compliance."""
        from ilha.config.settings import sanitize_hostname
        
        # Test underscore replacement
        assert sanitize_hostname("business_intelligence") == "business-intelligence"
        
        # Test mixed characters
        assert sanitize_hostname("my_project-v2") == "my-project-v2"
        
        # Test leading/trailing special chars
        assert sanitize_hostname("_test_project_") == "test-project"
        
        # Test case conversion
        assert sanitize_hostname("MyProject_Name") == "myproject-name"
        
        # Test multiple underscores
        assert sanitize_hostname("my__project") == "my--project"
    
    def test_validate_branch_name(self):
        """Test branch name validation."""
        # Valid branch names
        assert validate_branch_name("test-branch") == True
        assert validate_branch_name("feature_auth") == True
        assert validate_branch_name("bugfix123") == True
        assert validate_branch_name("release-v1.0") == True
        
        # Invalid branch names
        assert validate_branch_name("test@branch") == False
        assert validate_branch_name("test#branch") == False
        assert validate_branch_name("test branch") == False
        assert validate_branch_name("") == False
        assert validate_branch_name(None) == False
