"""
Comprehensive end-to-end test for ilha workflow.

This test implements the complete workflow as specified:
1. python -m ilha start (Start global Caddy proxy)
2. python -m ilha list (List existing worktrees)
3. python -m ilha create test (Create worktree)
4. cd ilha/worktrees/test (Change directory)
5. python -m ilha up (Start worktree environment)
6. curl -s "http://test.localhost" | grep -q "html" (Verify HTML response)
7. python -m ilha down (Stop worktree environment)
8. cd ../../.. (Navigate back to main repo)
9. python -m ilha delete test (Delete worktree)
10. python -m ilha stop (Stop global Caddy)
"""

import os
import sys
import time
import subprocess
import requests
from pathlib import Path
from typing import Tuple, Optional

import pytest


class TestComprehensiveWorkflow:
    """Comprehensive end-to-end test for ilha workflow."""
    
    @pytest.fixture(scope="class", autouse=True)
    def setup_test_environment(self, request):
        """Setup test environment and ensure clean state."""
        # Get project root
        request.cls.project_root = Path(__file__).parent.parent.parent.parent
        request.cls.original_cwd = os.getcwd()
        
        # Test configuration
        request.cls.test_branch = "test"
        request.cls.worktree_path = request.cls.project_root / "ilha" / "worktrees" / request.cls.test_branch
        
        # Ensure we start from project root
        os.chdir(request.cls.project_root)
        
        yield
        
        # Cleanup after test - inline cleanup code
        try:
            # Stop any running worktree environment
            if request.cls.worktree_path.exists():
                os.chdir(request.cls.worktree_path)
                subprocess.run(
                    ["python", "-m", "ilha", "down"],
                    capture_output=True,
                    cwd=request.cls.worktree_path
                )
                os.chdir(request.cls.project_root)
            
            # Remove worktree if it exists
            if request.cls.worktree_path.exists():
                subprocess.run(
                    ["python", "-m", "ilha", "remove", request.cls.test_branch],
                    capture_output=True,
                    cwd=request.cls.project_root
                )
            
            # Stop global Caddy
            subprocess.run(
                ["python", "-m", "ilha", "stop"],
                capture_output=True,
                cwd=request.cls.project_root
            )
            
        except Exception as e:
            print(f"Warning: Cleanup failed: {e}")
        
        os.chdir(request.cls.original_cwd)
    
    def _run_command(self, command: list, description: str, cwd: Optional[Path] = None) -> Tuple[bool, str]:
        """Run a command and return success status and output."""
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                cwd=cwd or self.project_root
            )
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            return False, f"Command failed: {e.stderr}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"
    
    def _cleanup_test_resources(self):
        """Clean up test resources."""
        try:
            # Stop any running worktree environment
            if self.worktree_path.exists():
                os.chdir(self.worktree_path)
                self._run_command(["python", "-m", "ilha", "down"], "Stop worktree environment")
                os.chdir(self.project_root)
            
            # Remove worktree if it exists
            if self.worktree_path.exists():
                self._run_command(["python", "-m", "ilha", "delete", self.test_branch], "Delete worktree")
            
            # Stop global Caddy
            self._run_command(["python", "-m", "ilha", "stop"], "Stop global Caddy")
            
        except Exception as e:
            print(f"Warning: Cleanup failed: {e}")
    
    def test_complete_ilha_workflow(self):
        """Test the complete ilha workflow as specified."""
        
        # Step 1: Start global Caddy proxy
        print("\n=== Step 1: Starting global Caddy proxy ===")
        success, output = self._run_command(
            ["python", "-m", "ilha", "start"], 
            "Start global Caddy proxy"
        )
        assert success, f"Failed to start global Caddy proxy: {output}"
        print(f"✅ Global Caddy proxy started: {output}")
        
        # Step 2: List existing worktrees
        print("\n=== Step 2: Listing existing worktrees ===")
        success, output = self._run_command(
            ["python", "-m", "ilha", "list"], 
            "List existing worktrees"
        )
        assert success, f"Failed to list worktrees: {output}"
        print(f"✅ Worktrees listed: {output}")
        
        # Step 3: Create worktree
        print("\n=== Step 3: Creating worktree 'test' ===")
        success, output = self._run_command(
            ["python", "-m", "ilha", "create", self.test_branch], 
            "Create worktree"
        )
        assert success, f"Failed to create worktree: {output}"
        print(f"✅ Worktree created: {output}")
        
        # Verify worktree directory exists
        assert self.worktree_path.exists(), f"Worktree directory not found: {self.worktree_path}"
        print(f"✅ Worktree directory exists: {self.worktree_path}")
        
        # Step 4: Change directory to worktree
        print("\n=== Step 4: Changing directory to worktree ===")
        os.chdir(self.worktree_path)
        current_dir = os.getcwd()
        assert str(self.worktree_path) in current_dir, f"Failed to change to worktree directory: {current_dir}"
        print(f"✅ Changed to worktree directory: {current_dir}")
        
        # Step 5: Start worktree environment
        print("\n=== Step 5: Starting worktree environment ===")
        success, output = self._run_command(
            ["python", "-m", "ilha", "up", self.test_branch, "-d"], 
            "Start worktree environment",
            cwd=self.worktree_path
        )
        assert success, f"Failed to start worktree environment: {output}"
        print(f"✅ Worktree environment started: {output}")
        
        # Wait for containers to fully initialize
        print("⏳ Waiting for containers to initialize...")
        time.sleep(10)
        
        # Step 6: Verify HTTP access and HTML response
        print("\n=== Step 6: Verifying HTTP access and HTML response ===")
        
        # Test using 127.0.0.1 with Host header to avoid DNS resolution issues
        # This is the standard approach for testing HTTP routing in automated tests
        test_url = "http://127.0.0.1"
        headers = {"Host": f"{self.test_branch}.localhost"}
        print(f"Testing {self.test_branch}.localhost via {test_url} with Host header")
        
        # Try multiple times with increasing delays
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                print(f"Attempt {attempt + 1}/{max_attempts}: Testing {test_url} (Host: {self.test_branch}.localhost)")
                response = requests.get(test_url, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    # Check if response contains HTML
                    if "html" in response.text.lower():
                        print(f"✅ HTTP request successful: {response.status_code}")
                        print(f"✅ HTML content found in response")
                        break
                    else:
                        print(f"⚠️ HTTP request successful but no HTML found: {response.status_code}")
                        if attempt < max_attempts - 1:
                            print("⏳ Retrying in 5 seconds...")
                            time.sleep(5)
                            continue
                        else:
                            assert False, f"No HTML content found in response from {self.test_branch}.localhost"
                else:
                    print(f"⚠️ HTTP request failed: {response.status_code}")
                    if attempt < max_attempts - 1:
                        print("⏳ Retrying in 5 seconds...")
                        time.sleep(5)
                        continue
                    else:
                        assert False, f"HTTP request failed with status {response.status_code} from {self.test_branch}.localhost"
                        
            except requests.exceptions.RequestException as e:
                print(f"⚠️ HTTP request error: {e}")
                if attempt < max_attempts - 1:
                    print("⏳ Retrying in 5 seconds...")
                    time.sleep(5)
                    continue
                else:
                    assert False, f"HTTP request failed: {e}"
        
        # Step 7: Stop worktree environment
        print("\n=== Step 7: Stopping worktree environment ===")
        success, output = self._run_command(
            ["python", "-m", "ilha", "down", self.test_branch],
            "Stop worktree environment",
            cwd=self.worktree_path
        )
        assert success, f"Failed to stop worktree environment: {output}"
        print(f"✅ Worktree environment stopped: {output}")
        
        # Step 8: Navigate back to main repository
        print("\n=== Step 8: Navigating back to main repository ===")
        os.chdir(self.project_root)
        current_dir = os.getcwd()
        assert str(self.project_root) in current_dir, f"Failed to navigate back to main repo: {current_dir}"
        print(f"✅ Navigated back to main repository: {current_dir}")
        
        # Step 9: Remove worktree
        print("\n=== Step 9: Deleting worktree ===")
        success, output = self._run_command(
            ["python", "-m", "ilha", "delete", self.test_branch], 
            "Delete worktree"
        )
        assert success, f"Failed to delete worktree: {output}"
        print(f"✅ Worktree removed: {output}")
        
        # Verify worktree directory is removed
        assert not self.worktree_path.exists(), f"Worktree directory still exists: {self.worktree_path}"
        print(f"✅ Worktree directory removed: {self.worktree_path}")
        
        # Step 10: Stop global Caddy
        print("\n=== Step 10: Stopping global Caddy ===")
        success, output = self._run_command(
            ["python", "-m", "ilha", "stop"], 
            "Stop global Caddy"
        )
        assert success, f"Failed to stop global Caddy: {output}"
        print(f"✅ Global Caddy stopped: {output}")
        
        print("\n🎉 Complete ilha workflow test PASSED!")
        print("All 10 steps completed successfully:")
        print("  1) ✅ Global Caddy proxy started")
        print("  2) ✅ Listed existing worktrees")
        print("  3) ✅ Created worktree 'test'")
        print("  4) ✅ Changed directory to worktree")
        print("  5) ✅ Started worktree environment")
        print("  6) ✅ Verified HTTP access and HTML response")
        print("  7) ✅ Stopped worktree environment")
        print("  8) ✅ Navigated back to main repository")
        print("  9) ✅ Removed worktree")
        print(" 10) ✅ Stopped global Caddy")
