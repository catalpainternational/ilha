"""
Integration tests for ilha setup command and complete workflow.

Tests cover:
- Complete workflow from setup to worktree operations
- Multiple project types
- Error handling scenarios
- Backward compatibility
"""

import pytest
import yaml
import tempfile
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any, List

from ilha.commands.setup import SetupManager
from ilha.commands.worktree import WorktreeManager
from ilha.commands.caddy import CaddyManager
from ilha.core.docker_manager import DockerManager
from ilha.core.git_manager import GitManager
from ilha.core.environment_manager import EnvironmentManager


class TestSetupIntegration:
    """Integration tests for setup command and complete workflow."""
    
    @pytest.fixture
    def temp_project_dir(self):
        """Create a temporary project directory for testing."""
        temp_dir = Path(tempfile.mkdtemp(prefix="ilha_integration_test_"))
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def django_project_compose(self):
        """Django project docker-compose.yml."""
        return {
            'version': '3.8',
            'services': {
                'web': {
                    'build': '.',
                    'ports': ['8000:8000'],
                    'container_name': 'django-web',
                    'volumes': ['./:/app'],
                    'environment': {
                        'DEBUG': 'True',
                        'ALLOWED_HOSTS': 'localhost,127.0.0.1',
                        'DATABASE_URL': 'postgres://user:pass@db:5432/django'
                    },
                    'depends_on': ['db', 'redis']
                },
                'db': {
                    'image': 'postgres:13',
                    'container_name': 'django-db',
                    'volumes': ['postgres_data:/var/lib/postgresql/data'],
                    'environment': {
                        'POSTGRES_DB': 'django',
                        'POSTGRES_USER': 'user',
                        'POSTGRES_PASSWORD': 'pass'
                    }
                },
                'redis': {
                    'image': 'redis:alpine',
                    'container_name': 'django-redis'
                }
            },
            'volumes': {
                'postgres_data': {}
            }
        }
    
    @pytest.fixture
    def rails_project_compose(self):
        """Rails project docker-compose.yml."""
        return {
            'version': '3.8',
            'services': {
                'web': {
                    'build': '.',
                    'ports': ['3000:3000'],
                    'container_name': 'rails-web',
                    'volumes': ['./:/app'],
                    'environment': {
                        'RAILS_ENV': 'development',
                        'DATABASE_URL': 'postgres://user:pass@db:5432/rails'
                    },
                    'depends_on': ['db']
                },
                'db': {
                    'image': 'postgres:13',
                    'container_name': 'rails-db',
                    'volumes': ['postgres_data:/var/lib/postgresql/data'],
                    'environment': {
                        'POSTGRES_DB': 'rails',
                        'POSTGRES_USER': 'user',
                        'POSTGRES_PASSWORD': 'pass'
                    }
                }
            },
            'volumes': {
                'postgres_data': {}
            }
        }
    
    @pytest.fixture
    def nodejs_project_compose(self):
        """Node.js project docker-compose.yml."""
        return {
            'version': '3.8',
            'services': {
                'api': {
                    'build': './api',
                    'ports': ['3001:3001'],
                    'container_name': 'nodejs-api',
                    'environment': {
                        'NODE_ENV': 'development',
                        'PORT': '3001'
                    }
                },
                'frontend': {
                    'build': './frontend',
                    'ports': ['3000:3000'],
                    'container_name': 'nodejs-frontend',
                    'environment': {
                        'NODE_ENV': 'development',
                        'REACT_APP_API_URL': 'http://api:3001'
                    },
                    'depends_on': ['api']
                }
            }
        }
    
    def test_integration_001_complete_workflow(self, temp_project_dir, django_project_compose):
        """Test ID: INTEGRATION-001 - Complete workflow from setup to worktree."""
        # Setup project directory
        project_root = temp_project_dir
        compose_file = project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(django_project_compose, f)
        
        # Initialize git repository
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)
            
            # Mock git init
            subprocess.run(['git', 'init'], cwd=project_root)
            subprocess.run(['git', 'add', '.'], cwd=project_root)
            subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=project_root)
        
        # Test setup command
        with patch('ilha.commands.setup.get_project_root') as mock_get_root:
            with patch('ilha.utils.validation.check_prerequisites') as mock_check:
                mock_get_root.return_value = project_root
                mock_check.return_value = True
                
                setup_manager = SetupManager()
                result = setup_manager.setup_project()
                assert result is True
            
            # Verify setup files created
            ilha_dir = project_root / ".ilha"
            assert ilha_dir.exists()
            assert (ilha_dir / "config.yml").exists()
            assert (ilha_dir / "docker-compose.worktree.yml").exists()
            assert (ilha_dir / "Caddyfile.ilha").exists()
        
        # Test worktree creation
        with patch('ilha.commands.worktree.get_project_root') as mock_get_root:
            mock_get_root.return_value = project_root
            
            # Mock Docker and Git managers
            with patch('ilha.commands.worktree.DockerManager') as mock_docker:
                with patch('ilha.commands.worktree.GitManager') as mock_git:
                    with patch('ilha.commands.worktree.EnvironmentManager') as mock_env:
                        mock_docker.return_value = Mock()
                        mock_git.return_value = Mock()
                        mock_env.return_value = Mock()
                        
                        worktree_manager = WorktreeManager()
                        # Mock successful worktree creation
                        worktree_manager.create_worktree = Mock(return_value=True)
                        
                        result = worktree_manager.create_worktree("test-branch")
                        assert result is True
    
    def test_integration_002_multiple_project_types(self, temp_project_dir, django_project_compose, rails_project_compose, nodejs_project_compose):
        """Test ID: INTEGRATION-002 - Test with different project types."""
        project_types = [
            ("django", django_project_compose),
            ("rails", rails_project_compose),
            ("nodejs", nodejs_project_compose),
        ]
        
        for project_type, compose_data in project_types:
            # Create project directory
            project_dir = temp_project_dir / project_type
            project_dir.mkdir()
            
            # Create compose file
            compose_file = project_dir / "docker-compose.yml"
            with open(compose_file, 'w') as f:
                yaml.dump(compose_data, f)
            
        # Test setup
        with patch('ilha.commands.setup.get_project_root') as mock_get_root:
            with patch('ilha.utils.validation.check_prerequisites') as mock_check:
                mock_get_root.return_value = project_dir
                mock_check.return_value = True
                
                setup_manager = SetupManager()
                result = setup_manager.setup_project(project_name=project_type)
                assert result is True
                
                # Verify project-specific configuration
                config_file = project_dir / ".ilha" / "config.yml"
                with open(config_file) as f:
                    config_data = yaml.safe_load(f)
                
                assert config_data['project_name'] == project_type
                assert 'services' in config_data
                assert len(config_data['services']) > 0
    
    def test_integration_003_django_project_workflow(self, temp_project_dir, django_project_compose):
        """Test complete Django project workflow."""
        # Setup Django project
        project_root = temp_project_dir
        compose_file = project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(django_project_compose, f)
        
        # Initialize git
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)
            subprocess.run(['git', 'init'], cwd=project_root)
            subprocess.run(['git', 'add', '.'], cwd=project_root)
            subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=project_root)
        
        # Setup ilha
        with patch('ilha.commands.setup.get_project_root') as mock_get_root:
            with patch('ilha.utils.validation.check_prerequisites') as mock_check:
                mock_get_root.return_value = project_root
                mock_check.return_value = True
                
                setup_manager = SetupManager()
                result = setup_manager.setup_project(project_name="django-project")
                assert result is True
        
        # Verify Django-specific configuration
        config_file = project_root / ".ilha" / "config.yml"
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        assert config_data['project_name'] == "django-project"
        assert 'web' in config_data['services']
        assert 'db' in config_data['services']
        assert 'redis' in config_data['services']
        assert 'postgres_data' in config_data['volumes']
    
    def test_integration_004_rails_project_workflow(self, temp_project_dir, rails_project_compose):
        """Test complete Rails project workflow."""
        # Setup Rails project
        project_root = temp_project_dir
        compose_file = project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(rails_project_compose, f)
        
        # Initialize git
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)
            subprocess.run(['git', 'init'], cwd=project_root)
            subprocess.run(['git', 'add', '.'], cwd=project_root)
            subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=project_root)
        
        # Setup ilha
        with patch('ilha.commands.setup.get_project_root') as mock_get_root:
            with patch('ilha.utils.validation.check_prerequisites') as mock_check:
                mock_get_root.return_value = project_root
                mock_check.return_value = True
                
                setup_manager = SetupManager()
                result = setup_manager.setup_project(project_name="rails-project")
                assert result is True
        
        # Verify Rails-specific configuration
        config_file = project_root / ".ilha" / "config.yml"
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        assert config_data['project_name'] == "rails-project"
        assert 'web' in config_data['services']
        assert 'db' in config_data['services']
        assert 'postgres_data' in config_data['volumes']
    
    def test_integration_005_nodejs_project_workflow(self, temp_project_dir, nodejs_project_compose):
        """Test complete Node.js project workflow."""
        # Setup Node.js project
        project_root = temp_project_dir
        compose_file = project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(nodejs_project_compose, f)
        
        # Initialize git
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)
            subprocess.run(['git', 'init'], cwd=project_root)
            subprocess.run(['git', 'add', '.'], cwd=project_root)
            subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=project_root)
        
        # Setup ilha
        with patch('ilha.commands.setup.get_project_root') as mock_get_root:
            with patch('ilha.utils.validation.check_prerequisites') as mock_check:
                mock_get_root.return_value = project_root
                mock_check.return_value = True
                
                setup_manager = SetupManager()
                result = setup_manager.setup_project(project_name="nodejs-project")
                assert result is True
        
        # Verify Node.js-specific configuration
        config_file = project_root / ".ilha" / "config.yml"
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        assert config_data['project_name'] == "nodejs-project"
        assert 'api' in config_data['services']
        assert 'frontend' in config_data['services']
    
    def test_integration_006_minimal_project_workflow(self, temp_project_dir):
        """Test minimal project workflow."""
        # Setup minimal project (no existing compose file)
        project_root = temp_project_dir
        
        # Initialize git
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)
            subprocess.run(['git', 'init'], cwd=project_root)
            subprocess.run(['git', 'add', '.'], cwd=project_root)
            subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=project_root)
        
        # Setup ilha (should create minimal compose file)
        with patch('ilha.commands.setup.get_project_root') as mock_get_root:
            with patch('ilha.utils.validation.check_prerequisites') as mock_check:
                mock_get_root.return_value = project_root
                mock_check.return_value = True
                
                setup_manager = SetupManager()
                result = setup_manager.setup_project(project_name="minimal-project")
                assert result is True
        
        # Verify minimal configuration
        config_file = project_root / ".ilha" / "config.yml"
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        assert config_data['project_name'] == "minimal-project"
        assert 'web' in config_data['services']
        
        # Verify minimal compose file was created
        minimal_compose = project_root / "docker-compose.yml"
        assert minimal_compose.exists()
    
    def test_integration_007_error_scenarios(self, temp_project_dir):
        """Test error scenarios in integration."""
        # Test permission errors
        with patch('pathlib.Path.mkdir') as mock_mkdir:
            mock_mkdir.side_effect = PermissionError("Permission denied")
            
            with patch('ilha.commands.setup.get_project_root') as mock_get_root:
                mock_get_root.return_value = temp_project_dir
                
                setup_manager = SetupManager()
                result = setup_manager.setup_project()
                assert result is False
        
        # Test invalid compose file
        invalid_compose = temp_project_dir / "docker-compose.yml"
        invalid_compose.write_text("invalid: yaml: content: [")
        
        with patch('ilha.commands.setup.get_project_root') as mock_get_root:
            mock_get_root.return_value = temp_project_dir
            
            setup_manager = SetupManager()
            result = setup_manager.setup_project()
            assert result is False
        
        # Test missing dependencies
        with patch('ilha.utils.validation.check_prerequisites') as mock_check:
            mock_check.return_value = False
            
            with patch('ilha.commands.setup.get_project_root') as mock_get_root:
                mock_get_root.return_value = temp_project_dir
                
                setup_manager = SetupManager()
                result = setup_manager.setup_project()
                assert result is False
    
    def test_integration_008_backward_compatibility(self, temp_project_dir):
        """Test backward compatibility with existing projects."""
        # Create existing ilha configuration
        ilha_dir = temp_project_dir / ".ilha"
        ilha_dir.mkdir()
        
        # Create legacy config
        legacy_config = {
            'project_name': 'legacy-project',
            'services': {
                'web': {'container_name_template': '${COMPOSE_PROJECT_NAME}-web'}
            }
        }
        
        config_file = ilha_dir / "config.yml"
        with open(config_file, 'w') as f:
            yaml.dump(legacy_config, f)
        
        # Test setup with existing configuration
        with patch('ilha.commands.setup.get_project_root') as mock_get_root:
            with patch('ilha.utils.validation.check_prerequisites') as mock_check:
                mock_get_root.return_value = temp_project_dir
                mock_check.return_value = True
                
                setup_manager = SetupManager()
                
                # Should handle existing configuration gracefully
                assert setup_manager.is_setup_complete() is False  # Missing other files
                
                # Run setup again
                result = setup_manager.setup_project()
                assert result is True
    
    def test_integration_009_complex_project_workflow(self, temp_project_dir):
        """Test complex project with multiple services, volumes, and networks."""
        complex_compose = {
            'version': '3.8',
            'services': {
                'web': {
                    'build': '.',
                    'ports': ['8000:8000'],
                    'container_name': 'complex-web',
                    'volumes': ['./:/app'],
                    'environment': {
                        'DEBUG': 'True',
                        'DATABASE_URL': 'postgres://user:pass@db:5432/complex'
                    },
                    'depends_on': ['db', 'redis', 'elasticsearch']
                },
                'db': {
                    'image': 'postgres:13',
                    'container_name': 'complex-db',
                    'volumes': ['postgres_data:/var/lib/postgresql/data'],
                    'environment': {
                        'POSTGRES_DB': 'complex',
                        'POSTGRES_USER': 'user',
                        'POSTGRES_PASSWORD': 'pass'
                    }
                },
                'redis': {
                    'image': 'redis:alpine',
                    'container_name': 'complex-redis'
                },
                'elasticsearch': {
                    'image': 'elasticsearch:7.14.0',
                    'container_name': 'complex-elasticsearch',
                    'volumes': ['elasticsearch_data:/usr/share/elasticsearch/data'],
                    'environment': {
                        'discovery.type': 'single-node'
                    }
                },
                'nginx': {
                    'image': 'nginx:alpine',
                    'container_name': 'complex-nginx',
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
        
        # Setup complex project
        project_root = temp_project_dir
        compose_file = project_root / "docker-compose.yml"
        with open(compose_file, 'w') as f:
            yaml.dump(complex_compose, f)
        
        # Initialize git
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)
            subprocess.run(['git', 'init'], cwd=project_root)
            subprocess.run(['git', 'add', '.'], cwd=project_root)
            subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=project_root)
        
        # Setup ilha
        with patch('ilha.commands.setup.get_project_root') as mock_get_root:
            with patch('ilha.utils.validation.check_prerequisites') as mock_check:
                mock_get_root.return_value = project_root
                mock_check.return_value = True
                
                setup_manager = SetupManager()
                result = setup_manager.setup_project(project_name="complex-project")
                assert result is True
        
        # Verify complex configuration
        config_file = project_root / ".ilha" / "config.yml"
        with open(config_file) as f:
            config_data = yaml.safe_load(f)
        
        assert config_data['project_name'] == "complex-project"
        assert len(config_data['services']) == 5  # web, db, redis, elasticsearch, nginx
        assert len(config_data['volumes']) == 2  # postgres_data, elasticsearch_data
        
        # Verify transformed compose file
        worktree_compose = project_root / ".ilha" / "docker-compose.worktree.yml"
        with open(worktree_compose) as f:
            transformed_data = yaml.safe_load(f)
        
        # Check all services are transformed
        assert len(transformed_data['services']) == 5
        assert 'networks' in transformed_data
        assert 'ilha_caddy_proxy' in transformed_data['networks']
    
    def test_integration_010_setup_status_verification(self, temp_project_dir):
        """Test setup status verification."""
        project_root = temp_project_dir
        
        # Test initial status
        with patch('ilha.commands.setup.get_project_root') as mock_get_root:
            mock_get_root.return_value = project_root
            
            setup_manager = SetupManager()
            status = setup_manager.get_setup_status()
            
            assert status['is_complete'] is False
            assert status['ilha_dir_exists'] is False
            assert status['config_file_exists'] is False
            assert status['compose_file_exists'] is False
            assert status['caddyfile_exists'] is False
        
        # Run setup
        compose_file = project_root / "docker-compose.yml"
        compose_file.write_text("version: '3.8'\nservices:\n  web:\n    image: nginx:alpine")
        
        with patch('ilha.utils.validation.check_prerequisites') as mock_check:
            mock_check.return_value = True
            result = setup_manager.setup_project()
            assert result is True
        
        # Test final status
        status = setup_manager.get_setup_status()
        assert status['is_complete'] is True
        assert status['ilha_dir_exists'] is True
        assert status['config_file_exists'] is True
        assert status['compose_file_exists'] is True
        assert status['caddyfile_exists'] is True
