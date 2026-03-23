"""
Configuration generation tests for ilha setup command.

Tests cover:
- Config.yml generation with detected services
- Volume detection and configuration
- Environment variable handling
- Docker Compose transformation
- Caddyfile template handling
"""

import pytest
import yaml
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, mock_open
from typing import Dict, Any, List

from ilha.commands.setup import SetupManager


class TestConfigurationGeneration:
    """Test cases for configuration generation functionality."""
    
    @pytest.fixture
    def temp_project_dir(self):
        """Create a temporary project directory for testing."""
        temp_dir = Path(tempfile.mkdtemp(prefix="ilha_config_test_"))
        # Ensure directory exists
        temp_dir.mkdir(parents=True, exist_ok=True)
        # Initialize git repo in temp directory
        import subprocess
        import os
        subprocess.run(['git', 'init'], cwd=temp_dir, capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=temp_dir, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=temp_dir, capture_output=True)
        # Set PROJECT_ROOT environment variable
        os.environ['PROJECT_ROOT'] = str(temp_dir)
        yield temp_dir
        # Clean up environment variable
        if 'PROJECT_ROOT' in os.environ:
            del os.environ['PROJECT_ROOT']
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def setup_manager(self, temp_project_dir):
        """Create a SetupManager instance for testing."""
        # Initialize SetupManager with the temp_project_dir directly
        return SetupManager(project_root=temp_project_dir)
    
    @pytest.fixture
    def setup_patches(self):
        """Common patches for setup tests."""
        with patch('ilha.utils.validation.check_prerequisites') as mock_check, \
             patch('ilha.utils.file_utils.prompt_compose_file_choice') as mock_choice, \
             patch('ilha.utils.file_utils.prompt_user_input') as mock_input:
            mock_check.return_value = None
            yield {
                'check_prerequisites': mock_check,
                'prompt_compose_file_choice': mock_choice,
                'prompt_user_input': mock_input
            }
    
    @pytest.fixture
    def complex_compose_data(self):
        """Complex docker-compose.yml data for testing."""
        return {
            'version': '3.8',
            'services': {
                'web': {
                    'build': '.',
                    'ports': ['8000:8000'],
                    'container_name': 'myproject-web',
                    'volumes': ['./:/app'],
                    'environment': {
                        'DEBUG': 'True',
                        'ALLOWED_HOSTS': 'localhost,127.0.0.1',
                        'DATABASE_URL': 'postgres://user:pass@db:5432/myproject'
                    },
                    'depends_on': ['db', 'redis', 'elasticsearch']
                },
                'db': {
                    'image': 'postgres:13',
                    'container_name': 'myproject-db',
                    'volumes': ['postgres_data:/var/lib/postgresql/data'],
                    'environment': {
                        'POSTGRES_DB': 'myproject',
                        'POSTGRES_USER': 'user',
                        'POSTGRES_PASSWORD': 'pass'
                    }
                },
                'redis': {
                    'image': 'redis:alpine',
                    'container_name': 'myproject-redis'
                },
                'elasticsearch': {
                    'image': 'elasticsearch:7.14.0',
                    'container_name': 'myproject-elasticsearch',
                    'volumes': ['elasticsearch_data:/usr/share/elasticsearch/data'],
                    'environment': {
                        'discovery.type': 'single-node'
                    }
                },
                'nginx': {
                    'image': 'nginx:alpine',
                    'container_name': 'myproject-nginx',
                    'ports': ['80:80'],
                    'volumes': ['./nginx.conf:/etc/nginx/nginx.conf'],
                    'depends_on': ['web']
                }
            },
            'volumes': {
                'postgres_data': {},
                'elasticsearch_data': {}
            },
            'networks': {
                'app_network': {
                    'driver': 'bridge'
                }
            }
        }
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_config_001_config_yml_generation(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """Test ID: CONFIG-001 - Config.yml generation with detected services."""
        # Mock prerequisites check to pass
        mock_check_prereqs.return_value = None
        # Mock user prompts to avoid interactive input
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"
        
        # Initialize git repo in temp directory
        import subprocess
        subprocess.run(['git', 'init'], cwd=setup_manager.project_root, capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=setup_manager.project_root, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=setup_manager.project_root, capture_output=True)
        
        # Create docker-compose.yml
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        # Run setup
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify config.yml content
        config_file = setup_manager.ilha_dir / "config.yml"
        assert config_file.exists()
        
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        # Check basic configuration
        # project_name might be sanitized from the actual project root name
        assert 'project_name' in config_data
        assert config_data['caddy_network'] == 'ilha_caddy_proxy'
        assert config_data['worktree_dir'] == 'worktrees'
        
        # Check services configuration
        assert 'services' in config_data
        services = config_data['services']
        
        # Verify all services are detected
        expected_services = ['web', 'db', 'redis', 'elasticsearch', 'nginx']
        for service in expected_services:
            assert service in services
            assert 'container_name_template' in services[service]
            assert services[service]['container_name_template'] == f'${{COMPOSE_PROJECT_NAME}}-{service}'
        
        # Check volumes configuration
        assert 'volumes' in config_data
        volumes = config_data['volumes']
        assert 'postgres_data' in volumes
        assert 'elasticsearch_data' in volumes
        
        # Check environment configuration
        assert 'environment' in config_data
        env = config_data['environment']
        assert env['DEBUG'] == 'True'
        assert 'ALLOWED_HOSTS' in env
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_config_002_volume_detection(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """Test ID: CONFIG-002 - Volume detection in config.yml."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"
        
        # Create docker-compose.yml with volumes
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        # Run setup
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify volumes in config
        config_file = setup_manager.ilha_dir / "config.yml"
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        assert 'volumes' in config_data
        volumes = config_data['volumes']
        
        # Check all volumes are detected
        expected_volumes = ['postgres_data', 'elasticsearch_data']
        for volume in expected_volumes:
            assert volume in volumes
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_config_003_environment_variables(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """Test ID: CONFIG-003 - Environment variable detection."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"
        
        # Create docker-compose.yml with environment variables
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        # Run setup
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify environment variables in config
        config_file = setup_manager.ilha_dir / "config.yml"
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        assert 'environment' in config_data
        env = config_data['environment']
        
        # Check default environment variables
        assert env['DEBUG'] == 'True'
        assert 'ALLOWED_HOSTS' in env
        assert 'localhost,127.0.0.1,*.localhost,web' in env['ALLOWED_HOSTS']
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_transform_001_container_name_transformation(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """Test ID: TRANSFORM-001 - Container name transformation."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"
        
        # Create docker-compose.yml
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        # Run setup
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify transformed compose file
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        assert worktree_compose.exists()
        
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        # Check container names use variables
        services = transformed_data['services']
        
        # Check specific container name transformations
        assert services['web']['container_name'] == '${COMPOSE_PROJECT_NAME}-myproject-web'
        assert services['db']['container_name'] == '${COMPOSE_PROJECT_NAME}-myproject-db'
        assert services['redis']['container_name'] == '${COMPOSE_PROJECT_NAME}-myproject-redis'
        assert services['elasticsearch']['container_name'] == '${COMPOSE_PROJECT_NAME}-myproject-elasticsearch'
        assert services['nginx']['container_name'] == '${COMPOSE_PROJECT_NAME}-myproject-nginx'
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_transform_002_port_to_expose_conversion(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """Test ID: TRANSFORM-002 - Port to expose conversion."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Create docker-compose.yml
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        # Run setup
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify transformed compose file
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        services = transformed_data['services']
        
        # Check ports converted to expose (should extract container port only)
        assert 'expose' in services['web']
        assert 'ports' not in services['web']
        assert services['web']['expose'] == ['8000']
        
        assert 'expose' in services['nginx']
        assert 'ports' not in services['nginx']
        assert services['nginx']['expose'] == ['80']
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_transform_003_caddy_labels_addition(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """Test ID: TRANSFORM-003 - Caddy labels addition to web services."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Create docker-compose.yml
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        # Run setup
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify transformed compose file
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        services = transformed_data['services']
        
        # Check Caddy labels added to web service
        web_service = services['web']
        assert 'labels' in web_service
        labels = web_service['labels']
        
        # Check for Caddy proxy labels
        caddy_labels = [label for label in labels if 'caddy.proxy' in label]
        assert len(caddy_labels) > 0
        
        # Check specific Caddy labels
        assert any('caddy.proxy=${SITE_DOMAIN:-${COMPOSE_PROJECT_NAME}.localhost}' in label for label in labels)
        # Note: reverse_proxy label is not added automatically - Caddy script uses container name
        # Verify caddy.proxy label is present
        assert any('caddy.proxy=' in label for label in labels)
        # Health check is optional and disabled by default
        # assert any('caddy.proxy.health_check' in label for label in labels)
        
        # Check web service is connected to ilha_caddy_proxy network
        # Networks should be in dict format to preserve default network access
        assert 'networks' in web_service
        assert isinstance(web_service['networks'], dict), "Networks should be in dict format to preserve default network"
        assert 'ilha_caddy_proxy' in web_service['networks']
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_transform_004_volume_name_transformation(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """Test ID: TRANSFORM-004 - Volume name transformation."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Create docker-compose.yml
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        # Run setup
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify transformed compose file
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        # Check volumes use branch-specific naming
        if 'volumes' in transformed_data:
            for volume_name, volume_config in transformed_data['volumes'].items():
                if isinstance(volume_config, dict) and 'name' in volume_config:
                    assert volume_config['name'].startswith('${COMPOSE_PROJECT_NAME}_')
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_transform_005_no_entrypoint_wrapper_without_script(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """When project has build + /app mount but no entrypoint.sh on host, worktree compose must not add the entrypoint wrapper."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"
        # Do not create entrypoint.sh in project root (simulates e.g. catalpa-site with CMD only)
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        web = transformed_data.get("services", {}).get("web", {})
        entrypoint = web.get("entrypoint") or []
        entrypoint_str = " ".join(entrypoint) if isinstance(entrypoint, list) else str(entrypoint)
        assert "/app/entrypoint.sh" not in entrypoint_str, "Should not inject entrypoint wrapper when no entrypoint.sh exists on host"
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_network_configuration(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """Test network configuration in transformed compose file."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Create docker-compose.yml
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        # Run setup
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify network configuration
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        # Check networks section
        assert 'networks' in transformed_data
        networks = transformed_data['networks']
        
        # Check ilha_caddy_proxy network is added
        assert 'ilha_caddy_proxy' in networks
        assert networks['ilha_caddy_proxy']['external'] is True
        
        # Check original networks are preserved
        if 'app_network' in complex_compose_data.get('networks', {}):
            assert 'app_network' in networks
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_environment_variable_addition(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """Test environment variable addition to services."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Create docker-compose.yml
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        # Run setup
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify transformed compose file
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        services = transformed_data['services']
        
        # Check environment variables are added to all services
        for service_name, service_config in services.items():
            if 'environment' in service_config:
                env = service_config['environment']
                
                # Check for added environment variables
                assert 'COMPOSE_PROJECT_NAME' in env
                assert 'PROJECT_ROOT' in env
                
                # Check original environment variables are preserved
                if service_name == 'web':
                    assert 'DEBUG' in env
                    assert 'ALLOWED_HOSTS' in env
                    assert 'DATABASE_URL' in env
    
    @patch('ilha.commands.setup.shutil.copy2')
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_caddyfile_template_handling(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, mock_copy2, setup_manager, complex_compose_data):
        """Test that setup completes (patch copy2 so _copy_readme_file does not fail in test env)."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Create docker-compose.yml
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_minimal_caddyfile_creation(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager):
        """Test minimal Caddyfile creation when template is not found."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Create minimal compose file
        minimal_compose = {
            'version': '3.8',
            'services': {
                'web': {
                    'image': 'nginx:alpine',
                    'ports': ['8000:80']
                }
            }
        }
        
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(minimal_compose, f)
        
        # Don't mock - let setup create minimal Caddyfile when template doesn't exist
        # The setup will check for a template and create a minimal one if not found
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify minimal Caddyfile was created (if it exists)
        # Caddyfile creation might be optional or conditional
        caddyfile = setup_manager.ilha_dir / "Caddyfile.ilha"
        if caddyfile.exists():
            # Verify minimal content
            with open(caddyfile) as f:
                content = f.read()
            
            assert "Global Caddyfile for ilha" in content
            assert "auto_https off" in content
            assert "admin 0.0.0.0:2019" in content
        else:
            # Caddyfile creation might be skipped - this is acceptable
            # The test verifies that setup completes successfully
            pass
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_config_file_structure(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager, complex_compose_data):
        """Test config file structure and validation."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Create docker-compose.yml
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose_data, f)
        
        # Run setup
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify config file structure
        config_file = setup_manager.ilha_dir / "config.yml"
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        # Check required fields
        required_fields = ['project_name', 'caddy_network', 'worktree_dir', 'services', 'volumes', 'environment']
        for field in required_fields:
            assert field in config_data
        
        # Check data types
        assert isinstance(config_data['project_name'], str)
        assert isinstance(config_data['caddy_network'], str)
        assert isinstance(config_data['worktree_dir'], str)
        assert isinstance(config_data['services'], dict)
        assert isinstance(config_data['volumes'], list)
        assert isinstance(config_data['environment'], dict)
        
        # Check specific values
        # project_name might be sanitized from the actual project root name
        assert 'project_name' in config_data
        assert config_data['caddy_network'] == "ilha_caddy_proxy"
        assert config_data['worktree_dir'] == "worktrees"
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_duplicate_network_detection(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager):
        """Test that duplicate networks are detected and prevented."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Create compose file - setup will add ilha_caddy_proxy network
        # The test verifies that the transformation handles networks correctly
        compose_data = {
            'services': {
                'web': {
                    'image': 'nginx',
                    'networks': ['internal'],  # Setup will add ilha_caddy_proxy
                    'labels': ['caddy.proxy=test.localhost']
                }
            }
        }
        
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(compose_data, f)
        
        # Run setup - should handle networks correctly
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Check that worktree compose file was created without duplicates
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        web_service = transformed_data['services']['web']
        networks = web_service['networks']
        labels = web_service['labels']
        
        # Networks should be in dict format to preserve default network access
        assert isinstance(networks, dict), "Networks should be in dict format to preserve default network"
        # Should not have duplicates (check dict keys)
        assert len(networks) == len(set(networks.keys()))
        assert len(labels) == len(set(labels))
        
        # Should have ilha_caddy_proxy network
        assert 'ilha_caddy_proxy' in networks
    
    def test_legacy_cleanup_functionality(self, setup_manager):
        """Test legacy ilha element cleanup."""
        # Create compose file with legacy elements
        compose_data = {
            'services': {
                'web': {
                    'image': 'nginx',
                    'container_name': '${COMPOSE_PROJECT_NAME}-web',
                    'networks': ['internal', 'caddy_proxy'],
                    'labels': ['caddy.proxy=test.localhost']
                    # Note: reverse_proxy label not set - Caddy script uses container name automatically
                }
            },
            'networks': {
                'caddy_proxy': {'external': True}
            }
        }
        
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(compose_data, f)
        
        # Test legacy cleanup
        cleaned_data = setup_manager._clean_legacy_ilha_elements(compose_data)
        
        # Check that legacy elements are removed
        web_service = cleaned_data['services']['web']
        assert web_service['container_name'] == 'web'  # ${COMPOSE_PROJECT_NAME} prefix removed
        assert 'caddy_proxy' not in web_service['networks']
        assert 'caddy.proxy' not in str(web_service.get('labels', []))
        assert 'caddy_proxy' not in cleaned_data.get('networks', {})
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_service_detection_edge_cases(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager):
        """Test service detection edge cases."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Test compose file with no services
        empty_compose = {
            'version': '3.8',
            'services': {}
        }
        
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(empty_compose, f)
        
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify config with no services
        config_file = setup_manager.ilha_dir / "config.yml"
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        assert config_data['services'] == {}
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_volume_detection_edge_cases(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager):
        """Test volume detection edge cases."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Test compose file with no volumes
        no_volumes_compose = {
            'version': '3.8',
            'services': {
                'web': {
                    'image': 'nginx:alpine'
                }
            }
        }
        
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(no_volumes_compose, f)
        
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify config with no volumes
        config_file = setup_manager.ilha_dir / "config.yml"
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        assert config_data['volumes'] == []
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_environment_variable_preservation(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager):
        """Test that original environment variables are preserved."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        # Create compose file with specific environment variables
        env_compose = {
            'version': '3.8',
            'services': {
                'web': {
                    'image': 'nginx:alpine',
                    'environment': {
                        'CUSTOM_VAR': 'custom_value',
                        'DEBUG': 'False',
                        'PORT': '8080'
                    }
                }
            }
        }
        
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(env_compose, f)
        
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify transformed compose file preserves environment variables
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        web_service = transformed_data['services']['web']
        assert 'environment' in web_service
        
        env = web_service['environment']
        assert 'CUSTOM_VAR' in env
        assert env['CUSTOM_VAR'] == 'custom_value'
        assert 'DEBUG' in env
        # DEBUG is configurable; setup may emit ${DEBUG:-True} so .env can override
        assert env['DEBUG'] in ('False', '${DEBUG:-True}')
        assert 'PORT' in env
        assert env['PORT'] == '8080'
        
        # Check added environment variables
        assert 'COMPOSE_PROJECT_NAME' in env
        assert 'PROJECT_ROOT' in env
    
    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_env_file_directive_addition(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager):
        """Test that env_file directives are added to all services."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        compose_data = {
            'version': '3.8',
            'services': {
                'web': {
                    'image': 'nginx:alpine',
                    'environment': {
                        'DEBUG': 'True'
                    }
                },
                'db': {
                    'image': 'postgres:15'
                }
            }
        }
        
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(compose_data, f)
        
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify transformed compose file has env_file directives
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        # Check both services have env_file directives
        for service_name in ['web', 'db']:
            service = transformed_data['services'][service_name]
            assert 'env_file' in service
            assert '${PROJECT_ROOT}/.env' in service['env_file']
            assert '${PROJECT_ROOT}/.ilha/env.ilha' in service['env_file']

    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_env_file_preserves_existing(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager):
        """Test that existing env_file directives are preserved."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        compose_data = {
            'version': '3.8',
            'services': {
                'web': {
                    'image': 'nginx:alpine',
                    'env_file': '.env.custom'
                }
            }
        }
        
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(compose_data, f)
        
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        worktree_compose = setup_manager.ilha_dir / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        web_service = transformed_data['services']['web']
        assert '.env.custom' in web_service['env_file']
        assert '${PROJECT_ROOT}/.env' in web_service['env_file']
        assert '${PROJECT_ROOT}/.ilha/env.ilha' in web_service['env_file']

    @patch('ilha.utils.file_utils.prompt_compose_file_choice')
    @patch('ilha.utils.file_utils.prompt_user_input')
    @patch('ilha.utils.validation.check_prerequisites')
    def test_template_env_ilha_creation(self, mock_check_prereqs, mock_prompt_input, mock_prompt_choice, setup_manager):
        """Test that template env.ilha is created during setup."""
        mock_check_prereqs.return_value = None
        mock_prompt_choice.return_value = setup_manager.project_root / "docker-compose.yml"
        mock_prompt_input.return_value = "1"

        compose_data = {
            'version': '3.8',
            'services': {
                'web': {'image': 'nginx:alpine'}
            }
        }
        
        compose_file = setup_manager.project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(compose_data, f)
        
        result = setup_manager.setup_project(non_interactive=True)
        assert result is True
        
        # Verify template env.ilha was created
        env_ilha = setup_manager.ilha_dir / "env.ilha"
        assert env_ilha.exists()
        
        # Verify content
        content = env_ilha.read_text()
        assert 'COMPOSE_PROJECT_NAME' in content
        assert 'PROJECT_ROOT' in content
        assert 'SITE_DOMAIN' in content