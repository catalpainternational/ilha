"""CLI tests for ilha worker commands."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from ilha.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def noop_prerequisites(monkeypatch):
    monkeypatch.setattr("ilha.cli.helpers.check_setup_or_prompt", lambda: None)
    monkeypatch.setattr("ilha.cli.helpers.check_prerequisites", lambda: None)


def test_worker_group_registered(runner):
    result = runner.invoke(cli, ["worker", "--help"])
    assert result.exit_code == 0
    assert "bundle" in result.output
    assert "deploy" in result.output
    assert "list" in result.output
    assert "destroy" in result.output
    assert "reap" in result.output


def test_worker_bundle_requires_central(runner, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["worker", "bundle", "staging"])
    assert result.exit_code != 0
    assert "central" in result.output.lower()


def test_worker_deploy_refuses_over_max_workers_before_create(runner, monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ilha").mkdir()
    (tmp_path / ".ilha" / "config.yml").write_text(
        "worker_pool:\n  max_workers: 1\n  ssh_keys: [k1]\n"
    )
    created = []
    monkeypatch.setattr(
        "ilha.cli_commands.worker.auto_detect_branch_name",
        lambda branch, json: branch or "staging",
    )
    monkeypatch.setattr(
        "ilha.commands.worker.WorkerManager.resolve_registry_token",
        lambda self, **kwargs: "registry-token",
    )
    monkeypatch.setattr(
        "ilha.cli_commands.worker.DropletCommands.list_droplets_by_tag",
        lambda self, *a, **k: [SimpleNamespace(name="worker-staging-1")],
    )
    monkeypatch.setattr(
        "ilha.cli_commands.worker.DropletCommands.create_droplet",
        lambda self, **kwargs: created.append(kwargs) or True,
    )
    result = runner.invoke(cli, ["worker", "deploy", "staging", "--json"])
    assert result.exit_code != 0
    assert created == []
    assert "max_workers" in result.output


def test_droplet_doctor_is_removed_nonzero(runner):
    result = runner.invoke(cli, ["droplet", "doctor", "staging", "--json"])
    assert result.exit_code != 0
    assert "removed" in result.output.lower()
    assert "ilha doctor" in result.output


def test_auto_detect_branch_name_raises_instead_of_returning_empty(monkeypatch, tmp_path):
    from ilha.commands.droplets import auto_detect_branch_name
    from ilha.exceptions import ilhaCommandError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ilha.commands.droplets.detect_execution_context",
        lambda: (None, None, False),
    )
    monkeypatch.setattr(
        "ilha.commands.droplets.get_project_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "ilha.commands.droplets.get_worktree_branch_name",
        lambda path: None,
    )
    with pytest.raises(ilhaCommandError, match="Could not detect branch name"):
        auto_detect_branch_name(None, json=True)
