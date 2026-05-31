"""Unit tests for the WorktreeManager orchestrator facade."""

from unittest.mock import Mock, patch

import pytest

from ilha.commands.worktree import WorktreeManager


class TestWorktreeManager:
    @pytest.fixture
    def manager(self, tmp_path):
        project_root = tmp_path / "project"
        project_root.mkdir()

        with patch("ilha.commands.worktree.get_project_root", return_value=project_root), patch(
            "ilha.commands.worktree.WorktreeOrchestrator"
        ) as orchestrator_cls:
            orchestrator = Mock()
            orchestrator.git_manager = Mock()
            orchestrator.docker_manager = Mock()
            orchestrator.env_manager = Mock()
            orchestrator_cls.return_value = orchestrator

            mgr = WorktreeManager(project_root=project_root)
            mgr._ensure_orchestrator()
            return mgr

    def test_create_worktree_success(self, manager):
        manager.orchestrator.create_worktree.return_value = {
            "success": True,
            "data": {"branch": "feature/foo", "worktree_path": str(manager.project_root / "worktrees/feature/foo")},
        }

        ok, payload = manager.create_worktree("feature/foo")

        assert ok is True
        assert payload["data"]["branch"] == "feature/foo"
        manager.orchestrator.create_worktree.assert_called_once_with("feature/foo", force=False)

    def test_create_worktree_already_exists_interactive_confirm(self, manager):
        manager.orchestrator.create_worktree.return_value = {
            "success": True,
            "data": {
                "branch": "feature/foo",
                "worktree_path": str(manager.project_root / "worktrees/feature/foo"),
                "status": "already_exists",
            },
        }
        with patch("ilha.commands.worktree.confirm_use_existing_worktree", return_value=True) as confirm:
            ok, _ = manager.create_worktree("feature/foo", interactive=True)

        assert ok is True
        confirm.assert_called_once_with("feature/foo")

    def test_create_worktree_already_exists_interactive_decline(self, manager):
        manager.orchestrator.create_worktree.return_value = {
            "success": True,
            "data": {
                "branch": "feature/foo",
                "worktree_path": str(manager.project_root / "worktrees/feature/foo"),
                "status": "already_exists",
            },
        }
        with patch("ilha.commands.worktree.confirm_use_existing_worktree", return_value=False) as confirm:
            ok, _ = manager.create_worktree("feature/foo", interactive=True)

        assert ok is False
        confirm.assert_called_once_with("feature/foo")

    def test_create_worktree_failure_bubbles_error(self, manager):
        manager.orchestrator.create_worktree.return_value = {"success": False, "error": "Validation failed"}

        ok, payload = manager.create_worktree("feature/foo")

        assert ok is False
        assert payload["error"] == "Validation failed"

    def test_start_worktree_success(self, manager):
        manager.orchestrator.start_worktree.return_value = {
            "success": True,
            "data": {"domain_name": "feature.foo.localhost", "caddy_configured": True},
        }

        ok = manager.start_worktree("feature/foo")

        assert ok is True
        manager.orchestrator.start_worktree.assert_called_once_with(
            "feature/foo", profile=None, secrets_profile=None
        )

    def test_start_worktree_failure(self, manager):
        manager.orchestrator.start_worktree.return_value = {"success": False, "error": "missing compose file"}

        ok = manager.start_worktree("feature/foo")

        assert ok is False

    def test_stop_worktree_returns_true_even_on_failure(self, manager):
        manager.orchestrator.stop_worktree.return_value = {"success": False, "error": "not running"}

        ok = manager.stop_worktree("feature/foo")

        assert ok is True
        manager.orchestrator.stop_worktree.assert_called_once_with("feature/foo", False)

    def test_remove_worktree_requires_branch_name(self, manager):
        assert manager.remove_worktree("") is False

    def test_remove_worktree_success(self, manager):
        manager.orchestrator.remove_worktree.return_value = {"success": True, "data": {"action": "removed"}}
        with patch("ilha.commands.worktree.ensure_main_repo"):
            ok = manager.remove_worktree("feature/foo", force=True, delete_branch=False)

        assert ok is True
        manager.orchestrator.remove_worktree.assert_called_once_with("feature/foo", True, False)

    def test_remove_worktree_failure(self, manager):
        manager.orchestrator.remove_worktree.return_value = {"success": False, "error": "boom"}
        with patch("ilha.commands.worktree.ensure_main_repo"):
            ok = manager.remove_worktree("feature/foo")

        assert ok is False

    def test_list_worktrees_success(self, manager):
        manager.orchestrator.list_worktrees.return_value = {
            "success": True,
            "data": [{"branch": "feature/foo", "path": "/tmp/foo", "commit": "abc123"}],
        }

        rows = manager.list_worktrees()

        assert rows == [{"branch": "feature/foo", "path": "/tmp/foo", "commit": "abc123"}]

    def test_list_worktrees_failure_returns_empty(self, manager):
        manager.orchestrator.list_worktrees.return_value = {"success": False, "error": "git failure"}

        assert manager.list_worktrees() == []

    def test_get_worktree_info_success(self, manager):
        manager.orchestrator.get_worktree_info.return_value = {
            "success": True,
            "data": {"branch_name": "feature/foo", "exists": True},
        }

        info = manager.get_worktree_info("feature/foo")

        assert info["branch_name"] == "feature/foo"

    def test_get_worktree_info_failure(self, manager):
        manager.orchestrator.get_worktree_info.return_value = {"success": False, "error": "unknown branch"}

        info = manager.get_worktree_info("missing")

        assert info["exists"] is False
        assert info["error"] == "unknown branch"

    def test_remove_all_worktrees_happy_path(self, manager):
        manager.git_manager.list_worktrees.return_value = [
            ("/tmp/wt/feature/foo", "abc123", "feature/foo"),
            ("/tmp/wt/feature/bar", "def456", "feature/bar"),
        ]
        manager.git_manager.get_current_branch.return_value = "main"
        with patch("ilha.commands.worktree.ensure_main_repo"), patch.object(
            WorktreeManager, "remove_worktree", return_value=True
        ) as remover:
            ok = manager.remove_all_worktrees()

        assert ok is True
        assert remover.call_count == 2

    def test_remove_all_worktrees_partial_failure(self, manager):
        manager.git_manager.list_worktrees.return_value = [
            ("/tmp/wt/feature/foo", "abc123", "feature/foo"),
            ("/tmp/wt/feature/bar", "def456", "feature/bar"),
        ]
        manager.git_manager.get_current_branch.return_value = "main"
        with patch("ilha.commands.worktree.ensure_main_repo"), patch.object(
            WorktreeManager, "remove_worktree", side_effect=[True, False]
        ):
            ok = manager.remove_all_worktrees()

        assert ok is False

