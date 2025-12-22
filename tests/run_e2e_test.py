#!/usr/bin/env python3
"""
End-to-end test runner for ilha comprehensive workflow test.

This script runs the specific comprehensive e2e test that validates the complete
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


def run_e2e_test():
    """Run the comprehensive end-to-end test."""
    print("🚀 Starting comprehensive end-to-end test for ilha")
    print("=" * 70)

    # Set PYTHONPATH to include the parent directory
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(parent_directory) + ':' + environment.get('PYTHONPATH', '')

    try:
        # Run the specific e2e test
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
            print("✅ Comprehensive end-to-end test PASSED!")
            print("\n🎉 All workflow steps completed successfully:")
            print("   0) Global Caddy proxy started")
            print("   1) Worktree created")
            print("   2) Directory changed to worktree")
            print("   3) Worktree environment started")
            print("   4) HTTP access to subdomain verified")
            print("   5) Full HTML webpage response confirmed")
            print("   6) Worktree environment stopped")
            print("   7) Containers verified as down")
            print("   8) Navigation back to main repository")
            print("   9) Worktree completely removed")
            print("  10) Global Caddy proxy stopped")
            return 0
        else:
            print("❌ Comprehensive end-to-end test FAILED!")
            print(f"Exit code: {result.returncode}")
            return 1

    except Exception as exception:
        print(f"❌ Error running test: {exception}")
        return 1


if __name__ == "__main__":
    sys.exit(run_e2e_test())
