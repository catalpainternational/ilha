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


