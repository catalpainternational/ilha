#!/usr/bin/env python3
"""
Test runner for the comprehensive ilha workflow test.

This script runs the specific comprehensive test that validates the complete
ilha workflow from start to finish.
"""

import sys
import subprocess
import os
from pathlib import Path

# Add the parent directory to Python path for module imports
current_directory = Path(__file__).parent
parent_directory = current_directory.parent
sys.path.insert(0, str(parent_directory))


def run_comprehensive_test():
    """Run the comprehensive ilha workflow test."""
    print("🚀 Starting comprehensive ilha workflow test")
    print("=" * 70)

    # Set PYTHONPATH to include the parent directory
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(parent_directory) + ':' + environment.get('PYTHONPATH', '')

    try:
        # Run the specific comprehensive test
        result = subprocess.run([
            sys.executable, "-m", "pytest",
            "tests/e2e/test_comprehensive_workflow.py::TestComprehensiveWorkflow::test_complete_ilha_workflow",
            "-v", "-s", "--tb=short"
        ], capture_output=True, text=True, cwd=parent_directory, env=environment)

        print("STDOUT:")
        print(result.stdout)

        if result.stderr:
            print("STDERR:")
            print(result.stderr)

        if result.returncode == 0:
            print("✅ Comprehensive ilha workflow test PASSED!")
            print("\n🎉 All workflow steps completed successfully:")
            print("   1) ✅ Global Caddy proxy started")
            print("   2) ✅ Listed existing worktrees")
            print("   3) ✅ Created worktree 'test'")
            print("   4) ✅ Changed directory to worktree")
            print("   5) ✅ Started worktree environment")
            print("   6) ✅ Verified HTTP access and HTML response")
            print("   7) ✅ Stopped worktree environment")
            print("   8) ✅ Navigated back to main repository")
            print("   9) ✅ Removed worktree")
            print("  10) ✅ Stopped global Caddy")
            return 0
        else:
            print("❌ Comprehensive ilha workflow test FAILED!")
            print(f"Exit code: {result.returncode}")
            return 1

    except Exception as exception:
        print(f"❌ Error running test: {exception}")
        return 1


if __name__ == "__main__":
    sys.exit(run_comprehensive_test())
