#!/usr/bin/env python3
"""
Test runner for ilha setup command tests.

This script runs all setup-related tests including:
- Unit tests for setup command
- Configuration generation tests
- Standalone installation tests
- Integration tests
- Error handling tests
"""

import sys
import subprocess
import pytest
from pathlib import Path


def run_setup_tests():
    """Run all setup-related tests."""
    print("🧪 Running ilha Setup Command Tests")
    print("=" * 50)
    
    # Get the test directory
    test_dir = Path(__file__).parent
    
    # Define test categories
    test_categories = [
        {
            'name': 'Setup Command Unit Tests',
            'path': 'unit/test_setup_command.py',
            'description': 'Basic setup functionality, custom project names, error scenarios'
        },
        {
            'name': 'Configuration Generation Tests',
            'path': 'unit/test_configuration_generation.py',
            'description': 'Config.yml and docker-compose.worktree.yml generation'
        },
        {
            'name': 'Standalone Installation Tests',
            'path': 'unit/test_standalone_installation.py',
            'description': 'Pip installation and entry point functionality'
        },
        {
            'name': 'Setup Integration Tests',
            'path': 'integration/test_setup_integration.py',
            'description': 'Complete workflow from setup to worktree operations'
        },
        {
            'name': 'Error Handling Tests',
            'path': 'unit/test_setup_error_handling.py',
            'description': 'Permission errors, invalid compose files, missing dependencies'
        }
    ]
    
    # Run tests for each category
    total_tests = 0
    passed_tests = 0
    failed_tests = 0
    
    for category in test_categories:
        print(f"\n📋 {category['name']}")
        print(f"   {category['description']}")
        print("-" * 50)
        
        test_file = test_dir / category['path']
        if not test_file.exists():
            print(f"❌ Test file not found: {test_file}")
            continue
        
        # Run pytest for this test file
        try:
            result = subprocess.run([
                sys.executable, '-m', 'pytest', 
                str(test_file),
                '-v',
                '--tb=short',
                '--color=yes'
            ], capture_output=True, text=True, cwd=test_dir.parent)
            
            if result.returncode == 0:
                print(f"✅ {category['name']} - PASSED")
                # Count tests from output
                lines = result.stdout.split('\n')
                for line in lines:
                    if 'PASSED' in line:
                        passed_tests += 1
                        total_tests += 1
            else:
                print(f"❌ {category['name']} - FAILED")
                print("STDOUT:", result.stdout)
                print("STDERR:", result.stderr)
                # Count failed tests
                lines = result.stdout.split('\n')
                for line in lines:
                    if 'FAILED' in line:
                        failed_tests += 1
                        total_tests += 1
                        
        except Exception as e:
            print(f"❌ Error running {category['name']}: {e}")
            failed_tests += 1
            total_tests += 1
    
    # Print summary
    print("\n" + "=" * 50)
    print("📊 Test Summary")
    print("=" * 50)
    print(f"Total Tests: {total_tests}")
    print(f"Passed: {passed_tests}")
    print(f"Failed: {failed_tests}")
    
    if failed_tests == 0:
        print("🎉 All tests passed!")
        return 0
    else:
        print(f"❌ {failed_tests} tests failed")
        return 1


def run_specific_test(test_name):
    """Run a specific test by name."""
    print(f"🧪 Running specific test: {test_name}")
    
    test_dir = Path(__file__).parent
    
    # Map test names to files
    test_files = {
        'setup': 'unit/test_setup_command.py',
        'config': 'unit/test_configuration_generation.py',
        'install': 'unit/test_standalone_installation.py',
        'integration': 'integration/test_setup_integration.py',
        'error': 'unit/test_setup_error_handling.py'
    }
    
    if test_name not in test_files:
        print(f"❌ Unknown test: {test_name}")
        print(f"Available tests: {', '.join(test_files.keys())}")
        return 1
    
    test_file = test_dir / test_files[test_name]
    if not test_file.exists():
        print(f"❌ Test file not found: {test_file}")
        return 1
    
    # Run the specific test
    try:
        result = subprocess.run([
            sys.executable, '-m', 'pytest', 
            str(test_file),
            '-v',
            '--tb=short',
            '--color=yes'
        ], cwd=test_dir.parent)
        
        return result.returncode
        
    except Exception as e:
        print(f"❌ Error running test: {e}")
        return 1


def main():
    """Main entry point."""
    if len(sys.argv) > 1:
        test_name = sys.argv[1]
        return run_specific_test(test_name)
    else:
        return run_setup_tests()


if __name__ == '__main__':
    sys.exit(main())


