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


# ---------------------------------------------------------------------------
# worker deploy --count N: droplets launch in parallel, each fails on its own
# ---------------------------------------------------------------------------

import json as _json
import threading
from types import SimpleNamespace

from ilha.exceptions import ilhaCommandError


@pytest.fixture
def deploy_env(monkeypatch, tmp_path):
    """worker deploy with every external effect faked; returns the recorder."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ilha").mkdir()
    (tmp_path / ".ilha" / "config.yml").write_text(
        "worker_pool:\n  max_workers: 4\n  ssh_keys: [k1]\n  central_droplet_name: central\n"
    )
    rec = SimpleNamespace(created=[], bootstrapped=[], destroyed=[], fail_bootstrap=set(),
                          fail_create=set(), destroy_ok=True, barrier=None, lock=threading.Lock())
    W = "ilha.cli_commands.worker"
    monkeypatch.setattr(f"{W}.auto_detect_branch_name", lambda branch, json: branch or "staging")
    monkeypatch.setattr(f"{W}._resolve_central_private_ip", lambda **k: "10.0.0.2")
    monkeypatch.setattr(f"{W}._resolve_vpc_uuid", lambda **k: "vpc-1")
    monkeypatch.setattr(f"{W}.DropletCommands.list_droplets_by_tag", lambda self, *a, **k: [])
    monkeypatch.setattr("ilha.commands.worker.WorkerManager.resolve_registry_token",
                        lambda self, **k: "registry-token")
    monkeypatch.setattr("ilha.commands.worker.WorkerManager.build_bundle",
                        lambda self, *a, **k: SimpleNamespace())
    monkeypatch.setattr("ilha.commands.worker.WorkerManager.render_cloud_init",
                        lambda self, bundle: "#cloud-config")

    def create_droplet(self, **kwargs):
        name = kwargs["name"]
        if name in rec.fail_create:
            return None
        with rec.lock:
            rec.created.append(name)
            droplet_id = 100 + len(rec.created)
        return SimpleNamespace(id=droplet_id, name=name, ip_address=f"203.0.113.{droplet_id - 100}",
                               private_ip_address="", status="active")

    def bootstrap_worker(self, bundle, host, **kwargs):
        if rec.barrier is not None:
            # Every bootstrap must be running at once for all to pass the
            # barrier; a sequential loop times out here.
            rec.barrier.wait(timeout=5)
        if host in rec.fail_bootstrap:
            raise ilhaCommandError("docker not ready")
        with rec.lock:
            rec.bootstrapped.append(host)

    def destroy_droplet(self, droplet_id, **kwargs):
        assert kwargs.get("only_droplet") is True and kwargs.get("force") is True
        rec.destroyed.append(droplet_id)
        return rec.destroy_ok

    monkeypatch.setattr(f"{W}.DropletCommands.create_droplet", create_droplet)
    monkeypatch.setattr("ilha.commands.worker.WorkerManager.bootstrap_worker", bootstrap_worker)
    monkeypatch.setattr(f"{W}.DropletCommands.destroy_droplet", destroy_droplet)
    return rec


def _envelope(result):
    return _json.loads(result.output)


def test_worker_deploy_bootstraps_all_droplets_concurrently(runner, deploy_env):
    deploy_env.barrier = threading.Barrier(4)
    result = runner.invoke(cli, ["worker", "deploy", "staging", "--count", "4", "--json"])
    assert result.exit_code == 0, result.output
    payload = _envelope(result)
    assert payload["success"] is True
    assert payload["data"]["count"] == 4
    assert [d["name"] for d in payload["data"]["droplets"]] == [
        "worker-staging-1", "worker-staging-2", "worker-staging-3", "worker-staging-4"]
    assert len(deploy_env.bootstrapped) == 4
    assert deploy_env.destroyed == []


def test_one_failed_bootstrap_does_not_stop_the_others_and_is_destroyed(runner, deploy_env):
    deploy_env.fail_bootstrap = {"203.0.113.2"}
    result = runner.invoke(cli, ["worker", "deploy", "staging", "--count", "4", "--json"])
    assert result.exit_code != 0
    payload = _envelope(result)
    assert payload["success"] is False
    assert len(deploy_env.bootstrapped) == 3
    details = payload["details"]
    assert len(details["created"]) == 3
    [failed] = details["failed"]
    assert failed["destroyed"] is True
    assert "docker not ready" in failed["error"]
    # Only the droplet whose worker failed to start is destroyed.
    assert deploy_env.destroyed == [failed["id"]]
    assert failed["id"] not in {d["id"] for d in details["created"]}


def test_a_failed_destroy_is_reported_so_the_droplet_is_not_lost(runner, deploy_env):
    deploy_env.fail_bootstrap = {"203.0.113.1"}
    deploy_env.destroy_ok = False
    result = runner.invoke(cli, ["worker", "deploy", "staging", "--count", "1", "--json"])
    assert result.exit_code != 0
    [failed] = _envelope(result)["details"]["failed"]
    assert failed["destroyed"] is False
    assert failed["id"] == 101


def test_a_failed_create_leaves_the_rest_running(runner, deploy_env):
    deploy_env.fail_create = {"worker-staging-3"}
    result = runner.invoke(cli, ["worker", "deploy", "staging", "--count", "3", "--json"])
    assert result.exit_code != 0
    details = _envelope(result)["details"]
    assert [d["name"] for d in details["created"]] == ["worker-staging-1", "worker-staging-2"]
    assert details["failed"][0]["name"] == "worker-staging-3"
    assert deploy_env.destroyed == []


def test_worker_names_continue_after_the_existing_fleet(runner, deploy_env, monkeypatch):
    monkeypatch.setattr(
        "ilha.cli_commands.worker.DropletCommands.list_droplets_by_tag",
        lambda self, *a, **k: [SimpleNamespace(name="worker-staging-1"),
                               SimpleNamespace(name="worker-staging-2")],
    )
    result = runner.invoke(cli, ["worker", "deploy", "staging", "--count", "2", "--json"])
    assert result.exit_code == 0, result.output
    assert sorted(deploy_env.created) == ["worker-staging-3", "worker-staging-4"]
