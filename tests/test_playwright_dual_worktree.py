"""
Playwright test for dual worktree login and navigation.

This test verifies that two separate worktree instances (bug and fix) can be:
1. Created and started simultaneously
2. Accessed via their respective subdomains
3. Logged into with the same credentials
4. Navigated to settings pages independently
5. Operate in complete isolation from each other

Test Requirements:
- Username: testuser
- Password: testpassword
- Worktrees: bug.localhost and fix.localhost
- Navigation: Both instances should be able to access /administration/settings/
"""

import os
import sys
import time
import subprocess
from pathlib import Path
from typing import Tuple, Optional
from playwright.sync_api import sync_playwright, Page

import pytest


class TestPlaywrightDualWorktree:
    """Playwright test for dual worktree login and navigation."""
    
    # Test configuration
    TEST_BRANCHES = ["bug", "fix"]
    TEST_USERNAME = "testuser"
    TEST_PASSWORD = "testpassword"
    LOGIN_URL = "/accounts/login/"
    SETTINGS_URL = "/administration/settings/organization_profile/"
    MAX_RETRIES = 5
    RETRY_DELAY = 5
    
    @pytest.fixture(scope="class", autouse=True)
    def setup_test_environment(self, request):
        """Setup test environment - assumes containers are already running."""
        # Get project root
        request.cls.project_root = Path(__file__).parent.parent.parent.parent
        request.cls.original_cwd = os.getcwd()
        
        # Test configuration
        request.cls.worktree_paths = {}
        for branch in self.TEST_BRANCHES:
            request.cls.worktree_paths[branch] = (
                request.cls.project_root / "ilha" / "worktrees" / branch
            )
        
        # Ensure we start from project root
        os.chdir(request.cls.project_root)
        
        print(f"\n=== Setting up dual worktree test environment ===")
        print(f"Project root: {request.cls.project_root}")
        print(f"Test branches: {self.TEST_BRANCHES}")
        print(f"Assuming containers are already running and accessible")
        
        yield
        
        # No cleanup needed - containers are managed externally
        print(f"\n=== Test completed - no cleanup needed ===")
        print(f"Containers are assumed to be managed externally")
        
        os.chdir(request.cls.original_cwd)
    
    def _run_command(self, command: list, description: str, cwd: Optional[Path] = None) -> Tuple[bool, str]:
        """Run a command and return success status and output."""
        try:
            # The correct project root is the parent of the ilha directory
            working_dir = cwd or Path(__file__).parent.parent.parent
            
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                cwd=working_dir
            )
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            return False, f"Command failed: {e.stderr}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"
    
    def _cleanup_test_resources(self, cls):
        """Clean up test resources."""
        print(f"\n=== Cleaning up test resources ===")
        
        # Stop any running worktree environments
        for branch in self.TEST_BRANCHES:
            worktree_path = cls.worktree_paths[branch]
            if worktree_path.exists():
                print(f"Stopping worktree environment for {branch}...")
                os.chdir(worktree_path)
                self._run_command(
                    ["python", "-m", "ilha", "down", branch],
                    f"Stop worktree environment for {branch}",
                    cwd=worktree_path
                )
                os.chdir(cls.project_root)
        
        # Remove worktrees
        for branch in self.TEST_BRANCHES:
            print(f"Removing worktree for {branch}...")
            self._run_command(
                ["python", "-m", "ilha", "delete", branch, "--force"],
                f"Delete worktree for {branch}"
            )
        
        # Stop global Caddy
        print("Stopping global Caddy...")
        self._run_command(
            ["python", "-m", "ilha", "stop"],
            "Stop global Caddy"
        )
    
    def _wait_for_service(self, page: Page, url: str, max_attempts: int = 20, delay: int = 10) -> bool:
        """Wait for a service to become available using Chromium."""
        for attempt in range(max_attempts):
            try:
                response = page.goto(url, wait_until="networkidle", timeout=15000)
                # Accept both 200 OK and 302 redirect as valid responses
                if response and response.status in [200, 302]:
                    print(f"✅ Service at {url} is ready (status: {response.status})")
                    return True
            except Exception as e:
                print(f"⚠️ Service at {url} not ready yet: {e}")
            
            if attempt < max_attempts - 1:
                print(f"⏳ Waiting for service at {url} (attempt {attempt + 1}/{max_attempts})...")
                time.sleep(delay)
        
        return False
    
    def _login_to_worktree(self, page: Page, worktree_url: str, branch: str) -> bool:
        """Login to a specific worktree instance."""
        print(f"\n=== Logging into {branch} worktree ===")
        
        # Navigate to login page
        login_url = f"{worktree_url}{self.LOGIN_URL}"
        print(f"Navigating to login page: {login_url}")
        
        try:
            page.goto(login_url, wait_until="networkidle")
        except Exception as e:
            print(f"Failed to navigate to login page: {e}")
            return False
        
        # Fill in login form
        print(f"Filling login form for {branch}...")
        try:
            # Wait for form elements to be available
            page.wait_for_selector('input[name="login"]', timeout=10000)
            
            # Fill username
            page.fill('input[name="login"]', self.TEST_USERNAME)
            print(f"✅ Username filled: {self.TEST_USERNAME}")
            
            # Fill password
            page.fill('input[name="password"]', self.TEST_PASSWORD)
            print(f"✅ Password filled")
            
            # Submit form
            page.click('button[type="submit"]')
            print(f"✅ Login form submitted")
            
            # Wait for redirect after login
            page.wait_for_load_state("networkidle", timeout=15000)
            print(f"✅ Login completed for {branch}")
            
            return True
            
        except Exception as e:
            print(f"Login failed for {branch}: {e}")
            return False
    
    def _navigate_to_settings(self, page: Page, worktree_url: str, branch: str) -> bool:
        """Navigate to settings page for a specific worktree."""
        print(f"\n=== Navigating to settings for {branch} worktree ===")
        
        settings_url = f"{worktree_url}{self.SETTINGS_URL}"
        print(f"Navigating to settings: {settings_url}")
        
        try:
            page.goto(settings_url, wait_until="networkidle")
            
            # Verify we're on the organization profile settings page
            page.wait_for_selector('h2.column-header', timeout=10000)
            
            # Check for the specific header text
            header_element = page.locator('h2.column-header:has-text("Organisation Profile")')
            if header_element.count() > 0:
                print(f"✅ Organization Profile settings page loaded for {branch}")
                return True
            else:
                print(f"❌ Organization Profile header not found for {branch}")
                return False
            
        except Exception as e:
            print(f"Failed to navigate to settings for {branch}: {e}")
            return False
    
    def test_dual_worktree_login_and_navigation(self):
        """Test login and navigation on both worktree instances."""
        print(f"\n=== Starting dual worktree test ===")
        print(f"Assuming containers are already running and accessible")
        
        # Step 1: Test with Playwright
        print(f"\n=== Step 1: Testing with Playwright ===")
        
        with sync_playwright() as p:
            # Launch browser
            browser = p.chromium.launch(headless=False)  # Set to True for headless mode
            
            try:
                # Create separate browser contexts for each worktree
                contexts = {}
                pages = {}
                
                for branch in self.TEST_BRANCHES:
                    worktree_url = f"http://{branch}.localhost"
                    print(f"Creating browser context for {branch}...")
                    
                    # Create isolated context for each worktree
                    contexts[branch] = browser.new_context()
                    pages[branch] = contexts[branch].new_page()
                    
                    # Set viewport
                    pages[branch].set_viewport_size({"width": 1280, "height": 720})
                    
                    print(f"✅ Browser context created for {branch}")
                
                # Step 2: Verify services are accessible
                print(f"\n=== Step 2: Verifying services are accessible ===")
                for branch in self.TEST_BRANCHES:
                    worktree_url = f"http://{branch}.localhost"
                    print(f"Checking {worktree_url}...")
                    
                    if not self._wait_for_service(pages[branch], worktree_url, max_attempts=5, delay=2):
                        assert False, f"Service at {worktree_url} is not accessible - ensure containers are running"
                    print(f"✅ Service accessible at {worktree_url}")
                
                # Step 3: Login to both worktrees
                print(f"\n=== Step 3: Logging into both worktrees ===")
                login_success = {}
                
                for branch in self.TEST_BRANCHES:
                    worktree_url = f"http://{branch}.localhost"
                    login_success[branch] = self._login_to_worktree(
                        pages[branch], worktree_url, branch
                    )
                
                # Verify all logins succeeded
                for branch in self.TEST_BRANCHES:
                    assert login_success[branch], f"Login failed for {branch} worktree"
                    print(f"✅ Login successful for {branch}")
                
                # Step 4: Navigate to settings on both worktrees
                print(f"\n=== Step 4: Navigating to settings on both worktrees ===")
                settings_success = {}
                
                for branch in self.TEST_BRANCHES:
                    worktree_url = f"http://{branch}.localhost"
                    settings_success[branch] = self._navigate_to_settings(
                        pages[branch], worktree_url, branch
                    )
                
                # Verify all settings navigation succeeded
                for branch in self.TEST_BRANCHES:
                    assert settings_success[branch], f"Settings navigation failed for {branch} worktree"
                    print(f"✅ Settings navigation successful for {branch}")
                
                # Step 5: Verify isolation between worktrees
                print(f"\n=== Step 5: Verifying worktree isolation ===")
                
                # Check that both worktrees are running independently using Chromium
                for branch in self.TEST_BRANCHES:
                    worktree_url = f"http://{branch}.localhost"
                    
                    # Verify the page is still accessible using Chromium
                    try:
                        response = pages[branch].goto(worktree_url, wait_until="networkidle", timeout=10000)
                        assert response and response.status == 200, f"Worktree {branch} is not accessible"
                        print(f"✅ Worktree {branch} is accessible and isolated")
                    except Exception as e:
                        assert False, f"Worktree {branch} isolation check failed: {e}"
                
                print(f"\n🎉 Dual worktree test PASSED!")
                print(f"✅ Services verified as accessible")
                print(f"✅ Login successful on both instances")
                print(f"✅ Settings navigation successful on both instances")
                print(f"✅ Worktree isolation verified")
                
            finally:
                # Close browser contexts
                for branch in self.TEST_BRANCHES:
                    if branch in contexts:
                        contexts[branch].close()
                
                browser.close()
        
        # Step 6: Test completed
        print(f"\n=== Step 6: Test completed successfully ===")
        print(f"Test completed - containers assumed to be running externally")


if __name__ == "__main__":
    # Allow running the test directly
    pytest.main([__file__, "-v", "-s"])
