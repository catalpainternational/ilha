#!/usr/bin/env python3
"""
Simple test script for ilha CLI.

This script tests basic functionality of the ilha CLI without
requiring a full Docker environment.
"""

import sys
from pathlib import Path

# Add the ilha module to the path
sys.path.insert(0, str(Path(__file__).parent))

from ilha.cli import cli
from ilha.utils.logging import log_info, log_success, log_error


def test_help():
    """Test help command."""
    log_info("Testing help command...")
    try:
        # This would normally be called via click, but we'll test the function directly
        from ilha.commands.utility import UtilityManager
        utility_manager = UtilityManager()
        utility_manager.show_help_info()
        log_success("Help command works")
    except Exception as e:
        log_error(f"Help command failed: {e}")
        raise


def test_version():
    """Test version command."""
    log_info("Testing version command...")
    try:
        from ilha.commands.utility import UtilityManager
        utility_manager = UtilityManager()
        utility_manager.show_version_info()
        log_success("Version command works")
    except Exception as e:
        log_error(f"Version command failed: {e}")
        raise


def test_config():
    """Test configuration loading."""
    log_info("Testing configuration...")
    try:
        from ilha.config.settings import (
            VERSION, AUTHOR, PROJECT_NAME, CADDY_NETWORK,
            get_project_root, get_worktree_paths
        )
        
        log_info(f"Version: {VERSION}")
        log_info(f"Author: {AUTHOR}")
        log_info(f"Project: {PROJECT_NAME}")
        log_info(f"Network: {CADDY_NETWORK}")
        log_info(f"Project root: {get_project_root()}")
        
        # Test worktree paths
        new_path, legacy_path = get_worktree_paths("test-branch")
        log_info(f"New path: {new_path}")
        log_info(f"Legacy path: {legacy_path}")
        
        log_success("Configuration loading works")
    except Exception as e:
        log_error(f"Configuration test failed: {e}")
        raise


def test_validation():
    """Test validation functions."""
    log_info("Testing validation functions...")
    try:
        from ilha.utils.validation import (
            validate_branch_name, validate_branch_protection,
            PROTECTED_BRANCHES
        )
        
        # Test branch name validation
        assert validate_branch_name("test-branch") == True
        assert validate_branch_name("test@branch") == False
        assert validate_branch_name("") == False
        
        # Test protected branches
        assert validate_branch_protection("main") == True
        assert validate_branch_protection("test-branch") == False
        
        log_info(f"Protected branches: {PROTECTED_BRANCHES}")
        
        log_success("Validation functions work")
    except Exception as e:
        log_error(f"Validation test failed: {e}")
        raise


def test_environment_manager():
    """Test environment manager."""
    log_info("Testing environment manager...")
    try:
        from ilha.core.environment_manager import EnvironmentManager
        
        env_manager = EnvironmentManager()
        
        # Test volume names
        volume_names = env_manager.get_worktree_volume_names("test-branch")
        log_info(f"Volume names: {volume_names}")
        
        # Test environment variables
        env_vars = env_manager.get_environment_variables("test-branch")
        log_info(f"Environment variables: {env_vars}")
        
        # Test domain name
        domain = env_manager.get_domain_name("test-branch")
        assert domain == "test-branch.localhost"
        
        log_success("Environment manager works")
    except Exception as e:
        log_error(f"Environment manager test failed: {e}")
        raise


def main():
    """Run all tests."""
    log_info("Starting ilha CLI tests...")
    
    tests = [
        test_config,
        test_validation,
        test_environment_manager,
        test_help,
        test_version,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        if test():
            passed += 1
        else:
            failed += 1
    
    log_info(f"Tests completed: {passed} passed, {failed} failed")
    
    if failed == 0:
        log_success("All tests passed!")
        return 0
    else:
        log_error(f"{failed} tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
