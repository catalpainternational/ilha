#!/usr/bin/env python3
"""
Comprehensive test runner for ilha CLI.

This script runs all tests including unit tests, integration tests,
and basic functionality tests.
"""

import sys
import subprocess
import os
from pathlib import Path
from typing import List, Tuple

# Add the parent directory to Python path for module imports
current_dir = Path(__file__).parent
parent_dir = current_dir.parent
sys.path.insert(0, str(parent_dir))


def run_command(cmd: List[str], description: str) -> Tuple[bool, str]:
    """Run a command and return success status and output."""
    print(f"\n{'='*60}")
    print(f"Running: {description}")
    print(f"Command: {' '.join(cmd)}")
    print(f"{'='*60}")
    
    try:
        # Set PYTHONPATH to include the parent directory
        env = os.environ.copy()
        env['PYTHONPATH'] = str(parent_dir) + ':' + env.get('PYTHONPATH', '')
        
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            cwd=parent_dir,  # Run from parent directory to access ilha module
            env=env
        )
        
        if result.stdout:
            print("STDOUT:")
            print(result.stdout)
        
        if result.stderr:
            print("STDERR:")
            print(result.stderr)
        
        success = result.returncode == 0
        if success:
            print(f"✅ {description} - PASSED")
        else:
            print(f"❌ {description} - FAILED (exit code: {result.returncode})")
        
        return success, result.stdout + result.stderr
        
    except Exception as e:
        print(f"❌ {description} - ERROR: {e}")
        return False, str(e)


def main():
    """Run all tests."""
    print("🚀 Starting comprehensive test suite for ilha CLI")
    
    tests = [
        # Basic functionality tests
        (["python", "tests/test_ilha.py"], "Basic functionality test"),
        
        # Unit tests
        (["python", "-m", "pytest", "tests/unit/", "-v", "--tb=short"], "Unit tests"),
        
        # Integration tests
        (["python", "-m", "pytest", "tests/integration/", "-v", "--tb=short"], "Integration tests"),
        
        # E2E tests
        (["python", "-m", "pytest", "tests/e2e/", "-v", "--tb=short"], "E2E tests"),
        
        # CLI interface tests
        (["python", "-m", "ilha", "--help"], "CLI help command"),
        (["python", "-m", "ilha", "--version"], "CLI version command"),
        (["python", "-m", "ilha", "volumes", "--help"], "CLI volumes help"),
        
        # Import tests
        (["python", "-c", "import ilha; print('Import successful')"], "Module import test"),
        (["python", "-c", "from ilha.cli import cli; print('CLI import successful')"], "CLI import test"),
        (["python", "-c", "from ilha.core.docker_manager import DockerManager; print('Docker manager import successful')"], "Docker manager import test"),
        (["python", "-c", "from ilha.core.git_manager import GitManager; print('Git manager import successful')"], "Git manager import test"),
        (["python", "-c", "from ilha.core.environment_manager import EnvironmentManager; print('Environment manager import successful')"], "Environment manager import test"),
    ]
    
    passed = 0
    failed = 0
    
    for cmd, description in tests:
        success, output = run_command(cmd, description)
        if success:
            passed += 1
        else:
            failed += 1
    
    print(f"\n{'='*60}")
    print("📊 TEST SUMMARY")
    print(f"{'='*60}")
    print(f"✅ Passed: {passed}")
    print(f"❌ Failed: {failed}")
    print(f"📈 Total:  {passed + failed}")
    
    if failed == 0:
        print("\n🎉 All tests passed! The ilha CLI is ready for use.")
        return 0
    else:
        print(f"\n⚠️  {failed} test(s) failed. Please review the output above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
