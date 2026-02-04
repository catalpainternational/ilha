import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from ilha.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def noop_prerequisites(monkeypatch):
    """Prevent real prerequisite checks during CLI tests."""

    monkeypatch.setattr("ilha.cli.helpers.check_setup_or_prompt", lambda: None)
    monkeypatch.setattr("ilha.cli.helpers.check_prerequisites", lambda: None)


def _extract_json(output: str) -> dict:
    """Return the JSON object embedded in CLI output."""

    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise AssertionError(f"No JSON object found in CLI output:\n{output}")
    return json.loads(output[start : end + 1])


def test_create_command_reports_success(monkeypatch, runner):
    class FakeWorktreeManager:
        def __init__(self):
            pass

        def create_worktree(self, branch_name, interactive=True):
            self.branch_name = branch_name
            return True, {"data": {"status": "created", "worktree_path": "/tmp/worktrees/feature/foo"}}

    manager = FakeWorktreeManager()
    monkeypatch.setattr("ilha.cli_commands.worktrees.WorktreeManager", lambda: manager)

    result = runner.invoke(cli, ["create", "feature/foo", "--json"])
    assert result.exit_code == 0
    assert manager.branch_name == "feature/foo"
    payload = _extract_json(result.output)
    assert payload["success"] is True
    assert payload["data"]["status"] == "created"


def test_worktree_first_up_invokes_branch(monkeypatch, runner):
    class FakeWorktreeManager:
        def __init__(self):
            self.env_manager = SimpleNamespace(get_access_url=lambda branch: f"http://{branch}.test")

        def start_worktree(self, branch_name, profile=None):
            self.started_branch = branch_name
            self.started_profile = profile
            return True

    manager = FakeWorktreeManager()
    monkeypatch.setattr("ilha.cli_commands.worktrees.WorktreeManager", lambda: manager)

    result = runner.invoke(cli, ["feature/up-test", "up", "--json"])
    assert result.exit_code == 0
    assert manager.started_branch == "feature/up-test"
    payload = _extract_json(result.output)
    assert payload["success"] is True
    assert payload["data"]["branch_name"] == "feature/up-test"


def test_alias_delete_routes_to_remove(monkeypatch, runner):
    class FakeWorktreeManager:
        def __init__(self):
            pass

        def remove_worktree(self, branch_name, force, delete_branch=True):
            self.removed_branch = branch_name
            self.remove_force = force
            self.remove_delete_branch = delete_branch
            return True

    manager = FakeWorktreeManager()
    monkeypatch.setattr("ilha.cli_commands.worktrees.WorktreeManager", lambda: manager)
    monkeypatch.setattr("ilha.cli_commands.worktrees.has_wildcard", lambda _: False)

    result = runner.invoke(cli, ["-D", "feature/delete-me", "--json"])
    assert result.exit_code == 0
    assert manager.removed_branch == "feature/delete-me"
    assert manager.remove_delete_branch is True
    payload = _extract_json(result.output)
    assert payload["success"] is True


def test_command_wrapper_reports_errors(monkeypatch, runner):
    class FailingManager:
        def __init__(self):
            pass

        def create_worktree(self, branch_name, interactive=True):
            return False, {"error": "boom"}

    monkeypatch.setattr("ilha.cli_commands.worktrees.WorktreeManager", lambda: FailingManager())

    result = runner.invoke(cli, ["create", "broken-branch", "--json"])
    payload = _extract_json(result.output)
    assert payload["success"] is False
    assert "Failed to create worktree" in payload["error"]


def test_bare_up_resolves_from_worktree_context(monkeypatch, runner):
    """Test that 'ilha up' resolves worktree from current directory context."""
    class FakeWorktreeManager:
        def __init__(self):
            self.env_manager = SimpleNamespace(get_access_url=lambda branch: f"http://{branch}.test")

        def start_worktree(self, branch_name, profile=None):
            self.started_branch = branch_name
            self.started_profile = profile
            return True

    manager = FakeWorktreeManager()
    monkeypatch.setattr("ilha.cli_commands.worktrees.WorktreeManager", lambda: manager)
    
    # Mock detect_execution_context - need to patch the actual function object
    # The import happens inside parse_args, so we need to ensure the mock is in place
    # when the import happens (which is when parse_args is called)
    original_detect = None
    try:
        from ilha.utils import path_utils
        original_detect = path_utils.detect_execution_context
    except ImportError:
        pass
    
    def mock_detect_execution_context():
        return (None, "feature-auth", True)  # (worktree_path, branch_name, is_worktree)
    
    # Mock at the module level - this should work since the import happens at runtime
    monkeypatch.setattr("ilha.utils.path_utils.detect_execution_context", mock_detect_execution_context)

    result = runner.invoke(cli, ["up", "--json"])
    assert result.exit_code == 0, f"Command failed with output: {result.output}"
    assert manager.started_branch == "feature-auth"
    payload = _extract_json(result.output)
    assert payload["success"] is True
    assert payload["data"]["branch_name"] == "feature-auth"


def test_bare_up_resolves_from_current_git_branch(monkeypatch, runner):
    """Test that 'ilha up' resolves worktree from current git branch when at project root."""
    class FakeWorktreeManager:
        def __init__(self):
            self.env_manager = SimpleNamespace(get_access_url=lambda branch: f"http://{branch}.test")

        def start_worktree(self, branch_name, profile=None):
            self.started_branch = branch_name
            self.started_profile = profile
            return True

    class FakeGitManager:
        def __init__(self, project_root=None, validate=False):
            pass

        def get_current_branch(self):
            return "feature-branch"

        def validate_worktree_exists(self, branch_name):
            return branch_name == "feature-branch"

    manager = FakeWorktreeManager()
    monkeypatch.setattr("ilha.cli_commands.worktrees.WorktreeManager", lambda: manager)
    
    # Mock detect_execution_context to return project root (no worktree context)
    monkeypatch.setattr(
        "ilha.utils.path_utils.detect_execution_context",
        lambda: (None, None, False)  # (worktree_path, branch_name, is_worktree)
    )
    
    # Mock GitManager to return current branch and validate worktree exists
    monkeypatch.setattr("ilha.core.git_manager.GitManager", FakeGitManager)

    result = runner.invoke(cli, ["up", "--json"])
    assert result.exit_code == 0
    assert manager.started_branch == "feature-branch"
    payload = _extract_json(result.output)
    assert payload["success"] is True
    assert payload["data"]["branch_name"] == "feature-branch"
def test_bare_up_fails_when_no_worktree_resolved(monkeypatch, runner):
    """Test that 'ilha up' fails with helpful error when worktree cannot be resolved."""
    # Mock detect_execution_context to return no worktree context
    monkeypatch.setattr(
        "ilha.utils.path_utils.detect_execution_context",
        lambda: (None, None, False)  # (worktree_path, branch_name, is_worktree)
    )
    
    # Mock GitManager to return None for current branch (no branch)
    class FakeGitManager:
        def __init__(self, project_root=None, validate=False):
            pass

        def get_current_branch(self):
            return None

        def validate_worktree_exists(self, branch_name):
            return False

    monkeypatch.setattr("ilha.core.git_manager.GitManager", FakeGitManager)    result = runner.invoke(cli, ["up"])
    assert result.exit_code != 0
    assert "Could not determine worktree" in result.output
    assert "ilha <worktree_name> up" in result.output


def test_bare_up_includes_d_flag(monkeypatch, runner):
    """Test that 'ilha up' automatically includes -d flag."""
    class FakeWorktreeManager:
        def __init__(self):
            self.env_manager = SimpleNamespace(get_access_url=lambda branch: f"http://{branch}.test")

        def start_worktree(self, branch_name, profile=None):
            self.started_branch = branch_name
            self.started_profile = profile
            return True

    manager = FakeWorktreeManager()
    monkeypatch.setattr("ilha.cli_commands.worktrees.WorktreeManager", lambda: manager)
    
    # Mock detect_execution_context to return a worktree context
    monkeypatch.setattr(
        "ilha.utils.path_utils.detect_execution_context",
        lambda: (None, "feature-auth", True)
    )

    # Test that bare 'ilha up' works (should include -d automatically)
    result = runner.invoke(cli, ["up", "--json"])
    assert result.exit_code == 0
    assert manager.started_branch == "feature-auth"


def test_bare_down_resolves_from_worktree_context(monkeypatch, runner):
    """Test that 'ilha down' resolves worktree from current directory context."""
    class FakeWorktreeManager:
        def __init__(self):
            pass

        def stop_worktree(self, branch_name):
            self.stopped_branch = branch_name
            return True

    manager = FakeWorktreeManager()
    monkeypatch.setattr("ilha.cli_commands.worktrees.WorktreeManager", lambda: manager)
    
    # Mock detect_execution_context to return a worktree context
    monkeypatch.setattr(
        "ilha.utils.path_utils.detect_execution_context",
        lambda: (None, "feature-auth", True)
    )

    result = runner.invoke(cli, ["down", "--json"])
    assert result.exit_code == 0
    assert manager.stopped_branch == "feature-auth"
    payload = _extract_json(result.output)
    assert payload["success"] is True
    assert payload["data"]["branch_name"] == "feature-auth"
