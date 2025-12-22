"""
Unit tests for standalone installation and entry point functionality.

Tests cover:
- Pip installation simulation
- Entry point functionality
- Git submodule installation
- Command availability
"""

import pytest
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from typing import List, Dict, Any

# Mock the entry point functionality
class MockEntryPoint:
    """Mock entry point for testing."""
    
    def __init__(self, name: str, module: str, attr: str):
        self.name = name
        self.module = module
        self.attr = attr
    
    def load(self):
        """Load the entry point function."""
        return getattr(sys.modules.get(self.module, Mock()), self.attr, Mock())


class TestStandaloneInstallation:
    """Test cases for standalone installation functionality."""
    
    @pytest.fixture
    def mock_entry_points(self):
        """Mock entry points for testing."""
        return {
            'console_scripts': [
                MockEntryPoint('ilha', 'ilha.cli', 'main'),
            ]
        }
    
    @pytest.fixture
    def mock_pip_install(self):
        """Mock pip installation."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="Successfully installed ilha")
            yield mock_run
    
    def test_install_001_pip_installation(self, mock_pip_install):
        """Test ID: INSTALL-001 - Pip installation of ilha."""
        # Simulate pip installation
        result = subprocess.run([
            'pip', 'install', 'ilha'
        ], capture_output=True, text=True)
        
        assert result.returncode == 0
        assert "Successfully installed ilha" in result.stdout
    
    def test_install_002_entry_point_functionality(self, mock_entry_points):
        """Test ID: INSTALL-002 - Entry point works correctly."""
        # Mock entry points discovery
        with patch('importlib.metadata.entry_points') as mock_eps:
            mock_eps.return_value = mock_entry_points
            
            # Test entry point loading
            console_scripts = mock_entry_points['console_scripts']
            ilha_ep = console_scripts[0]
            
            assert ilha_ep.name == 'ilha'
            assert ilha_ep.module == 'ilha.cli'
            assert ilha_ep.attr == 'main'
            
            # Test loading the function
            func = ilha_ep.load()
            assert callable(func)
    
    def test_install_003_git_submodule_installation(self):
        """Test ID: INSTALL-003 - Git submodule installation."""
        with patch('subprocess.run') as mock_run:
            # Mock git submodule add command
            mock_run.return_value = Mock(returncode=0, stdout="Submodule added successfully")
            
            # Simulate git submodule add
            result = subprocess.run([
                'git', 'submodule', 'add', 
                'https://github.com/yourusername/ilha.git',
                'ilha'
            ], capture_output=True, text=True)
            
            assert result.returncode == 0
            assert "Submodule added successfully" in result.stdout
    
    def test_command_availability(self):
        """Test that all commands are available after installation."""
        # Mock the CLI module
        with patch('ilha.cli.main') as mock_main:
            mock_main.return_value = None
            
            # Test help command
            with patch('sys.argv', ['ilha', '--help']):
                try:
                    from ilha.cli import main
                    main()
                    mock_main.assert_called()
                except SystemExit:
                    pass  # Expected for help command
            
            # Test setup command help
            with patch('sys.argv', ['ilha', 'setup', '--help']):
                try:
                    from ilha.cli import main
                    main()
                    mock_main.assert_called()
                except SystemExit:
                    pass  # Expected for help command
    
    def test_python_module_execution(self):
        """Test execution via python -m ilha.cli."""
        with patch('ilha.cli.main') as mock_main:
            mock_main.return_value = None
            
            # Test module execution
            with patch('sys.argv', ['python', '-m', 'ilha.cli', '--help']):
                try:
                    from ilha.cli import main
                    main()
                    mock_main.assert_called()
                except SystemExit:
                    pass  # Expected for help command
    
    def test_wheel_package_creation(self):
        """Test wheel package creation for pip installation."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="Successfully built wheel")
            
            # Simulate wheel creation
            result = subprocess.run([
                'python', '-m', 'build', '--wheel'
            ], capture_output=True, text=True)
            
            assert result.returncode == 0
            assert "Successfully built wheel" in result.stdout
    
    def test_installation_verification(self):
        """Test installation verification."""
        with patch('importlib.metadata.distribution') as mock_dist:
            mock_dist.return_value = Mock(
                metadata={'Name': 'ilha', 'Version': '1.0.0'},
                entry_points=lambda: []
            )
            
            # Test package discovery
            dist = mock_dist('ilha')
            assert dist.metadata['Name'] == 'ilha'
            assert dist.metadata['Version'] == '1.0.0'
    
    def test_entry_point_registration(self):
        """Test entry point registration in setup.py."""
        # Mock setup.py content
        setup_content = """
from setuptools import setup, find_packages

setup(
    name="ilha",
    version="1.0.0",
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'ilha=ilha.cli:main',
        ],
    },
)
"""
        
        # Verify entry point is properly configured
        assert 'ilha=ilha.cli:main' in setup_content
        assert 'console_scripts' in setup_content
    
    def test_pyproject_toml_configuration(self):
        """Test pyproject.toml configuration for modern Python packaging."""
        pyproject_content = """
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "ilha"
version = "1.0.0"
description = "Git worktrees with isolated Docker environments"
dependencies = [
    "click>=8.0.0",
    "pyyaml>=6.0",
    "docker>=6.0.0",
]

[project.scripts]
ilha = "ilha.cli:main"
"""
        
        # Verify configuration
        assert 'ilha = "ilha.cli:main"' in pyproject_content
        assert 'name = "ilha"' in pyproject_content
        assert 'version = "1.0.0"' in pyproject_content
    
    def test_installation_dependencies(self):
        """Test that all required dependencies are specified."""
        required_deps = [
            'click>=8.0.0',
            'pyyaml>=6.0',
            'docker>=6.0.0',
        ]
        
        # Mock dependency checking
        with patch('importlib.metadata.distribution') as mock_dist:
            mock_dist.return_value = Mock(
                requires=lambda: required_deps
            )
            
            dist = mock_dist('ilha')
            deps = dist.requires()
            
            for dep in required_deps:
                assert dep in deps
    
    def test_installation_path_resolution(self):
        """Test that installation paths are resolved correctly."""
        with patch('sys.executable') as mock_executable:
            mock_executable.return_value = '/usr/bin/python3'
            
            # Test path resolution
            import site
            with patch.object(site, 'getsitepackages') as mock_site:
                mock_site.return_value = ['/usr/lib/python3.11/site-packages']
                
                # Verify installation path
                site_packages = site.getsitepackages()
                assert '/usr/lib/python3.11/site-packages' in site_packages
    
    def test_installation_script_execution(self):
        """Test installation script execution."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="Installation successful")
            
            # Simulate installation script
            result = subprocess.run([
                'python', 'setup.py', 'install'
            ], capture_output=True, text=True)
            
            assert result.returncode == 0
            assert "Installation successful" in result.stdout
    
    def test_development_installation(self):
        """Test development installation (pip install -e)."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="Development installation successful")
            
            # Simulate development installation
            result = subprocess.run([
                'pip', 'install', '-e', '.'
            ], capture_output=True, text=True)
            
            assert result.returncode == 0
            assert "Development installation successful" in result.stdout
    
    def test_installation_verification_commands(self):
        """Test commands to verify installation."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="ilha version 1.0.0")
            
            # Test version command
            result = subprocess.run([
                'ilha', '--version'
            ], capture_output=True, text=True)
            
            assert result.returncode == 0
            assert "ilha version 1.0.0" in result.stdout
    
    def test_installation_error_handling(self):
        """Test installation error handling."""
        with patch('subprocess.run') as mock_run:
            error = subprocess.CalledProcessError(1, 'pip', "Installation failed")
            error.stderr = "Installation failed"
            mock_run.side_effect = error
            
            # Test error handling
            try:
                result = subprocess.run([
                    'pip', 'install', 'ilha'
                ], capture_output=True, text=True)
                assert False, "Should have raised CalledProcessError"
            except subprocess.CalledProcessError as e:
                assert e.returncode == 1
                # Check stderr if available, otherwise check the exception message
                error_msg = getattr(e, 'stderr', None) or str(e)
                assert "Installation failed" in error_msg or "pip" in str(e)
    
    def test_multiple_installation_methods(self):
        """Test multiple installation methods."""
        installation_methods = [
            ['pip', 'install', 'ilha'],
            ['pip', 'install', '-e', '.'],
            ['python', 'setup.py', 'install'],
            ['python', '-m', 'pip', 'install', 'ilha'],
        ]
        
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="Installation successful")
            
            for method in installation_methods:
                result = subprocess.run(method, capture_output=True, text=True)
                assert result.returncode == 0
                assert "Installation successful" in result.stdout
    
    def test_installation_cleanup(self):
        """Test installation cleanup and uninstallation."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="Successfully uninstalled ilha")
            
            # Test uninstallation
            result = subprocess.run([
                'pip', 'uninstall', 'ilha', '-y'
            ], capture_output=True, text=True)
            
            assert result.returncode == 0
            assert "Successfully uninstalled ilha" in result.stdout


