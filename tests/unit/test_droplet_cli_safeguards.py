import json

import pytest
from click.testing import CliRunner

from ilha.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


def _extract_json(output: str) -> dict:
    start = output.find("{")
    end = output.rfind("}")
    assert start != -1 and end != -1 and end > start
    return json.loads(output[start : end + 1])


def test_droplet_put_json_includes_safeguard_report(monkeypatch, runner):
    class FakeDeployManager:
        def put(self, **kwargs):
            return True

        def get_last_deploy_report(self):
            return {
                "source_manifest": {"source_path": "/tmp/worktrees/demo", "branch_name": "demo"},
                "drift_report": {"status": "warning", "differences": []},
                "smoke_checks": {"success": True, "path": "/api/health/", "checks": []},
                "stage_timings": {"smoke_checks": 1.2},
                "recovery_attempted": False,
                "recovery_succeeded": False,
            }

        def cleanup(self):
            pass

    monkeypatch.setattr("ilha.cli_commands.droplets.auto_detect_branch_name", lambda branch, json: branch or "demo")
    monkeypatch.setattr("ilha.cli_commands.droplets.check_prerequisites_no_git", lambda: None)
    monkeypatch.setattr(
        "ilha.cli_commands.droplets.resolve_existing_remote_target",
        lambda **kwargs: ("root@1.2.3.4:/root", "demo.example.com", None, None, {}),
    )
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.SlimDeployManager", FakeDeployManager)

    result = runner.invoke(cli, ["droplet", "put", "demo", "--json"])
    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["success"] is True
    assert payload["data"]["url"] == "https://demo.example.com"
    assert payload["data"]["source_manifest"]["branch_name"] == "demo"
    assert payload["data"]["drift_report"]["status"] == "warning"
    assert payload["data"]["smoke_checks"]["success"] is True


def test_droplet_put_defaults_code_only_not_restore_volumes(monkeypatch, runner):
    put_kwargs = {}

    class FakeDeployManager:
        def put(self, **kwargs):
            put_kwargs.update(kwargs)
            return True

        def get_last_deploy_report(self):
            return {
                "source_manifest": {"source_path": "/tmp/worktrees/demo", "branch_name": "demo"},
                "drift_report": {"status": "ok", "differences": []},
                "smoke_checks": {"success": True, "path": "/", "checks": []},
                "stage_timings": {},
                "recovery_attempted": False,
                "recovery_succeeded": False,
            }

        def cleanup(self):
            pass

    monkeypatch.setattr("ilha.cli_commands.droplets.auto_detect_branch_name", lambda branch, json: branch or "demo")
    monkeypatch.setattr("ilha.cli_commands.droplets.check_prerequisites_no_git", lambda: None)
    monkeypatch.setattr(
        "ilha.cli_commands.droplets.resolve_existing_remote_target",
        lambda **kwargs: ("root@1.2.3.4:/root", "demo.example.com", None, None, {}),
    )
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.SlimDeployManager", FakeDeployManager)
    monkeypatch.setattr("ilha.cli_commands.droplets.resolve_cli_secrets_profile", lambda *a, **k: None)

    result = runner.invoke(cli, ["droplet", "put", "demo", "--json"])
    assert result.exit_code == 0, result.output
    assert put_kwargs.get("restore_volumes") is False
    assert put_kwargs.get("dump_db_first") is False


def test_droplet_put_restore_volumes_json_requires_i_mean_it(monkeypatch, runner):
    monkeypatch.setattr("ilha.cli_commands.droplets.auto_detect_branch_name", lambda branch, json: branch or "demo")
    monkeypatch.setattr("ilha.cli_commands.droplets.check_prerequisites_no_git", lambda: None)
    monkeypatch.setattr(
        "ilha.cli_commands.droplets.resolve_existing_remote_target",
        lambda **kwargs: ("root@1.2.3.4:/root", "demo.example.com", None, None, {}),
    )
    monkeypatch.setattr("ilha.cli_commands.droplets.resolve_cli_secrets_profile", lambda *a, **k: None)
    result = runner.invoke(
        cli,
        ["droplet", "put", "demo", "--domain", "demo.example.com", "--restore-volumes", "--json"],
    )
    assert result.exit_code != 0
    assert "i-mean-it" in result.output


def test_droplet_help_lists_new_remote_commands(runner):
    result = runner.invoke(cli, ["droplet", "--help"])
    assert result.exit_code == 0
    for name in ("put", "ssh", "dump-db", "status", "exec", "rename", "logs"):
        assert name in result.output


def test_droplet_deploy_json_includes_safeguard_report(monkeypatch, runner):
    deploy_kwargs = {}

    class FakeDeployManager:
        def deploy(self, **kwargs):
            deploy_kwargs.update(kwargs)
            return True

        def get_last_deploy_report(self):
            return {
                "source_manifest": {"source_path": "/tmp/worktrees/demo", "branch_name": "demo"},
                "drift_report": {"status": "ok", "differences": []},
                "smoke_checks": {"success": True, "path": "/", "checks": []},
                "stage_timings": {"transfer": 2.1},
                "failure_category": None,
                "recovery_attempted": False,
                "recovery_succeeded": False,
            }

        def cleanup(self):
            pass

    monkeypatch.setattr("ilha.cli_commands.droplets.auto_detect_branch_name", lambda branch, json: branch or "demo")
    monkeypatch.setattr("ilha.cli_commands.droplets.check_prerequisites_no_git", lambda: None)
    monkeypatch.setattr("ilha.cli_commands.droplets.check_setup_or_prompt", lambda: None)
    monkeypatch.setattr("ilha.cli_commands.droplets.check_prerequisites", lambda: None)
    monkeypatch.setattr("ilha.cli_commands.droplets.parse_rsync_target_or_droplet", lambda target, token=None: target)
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.SlimDeployManager", FakeDeployManager)

    result = runner.invoke(
        cli,
        ["droplet", "deploy", "demo", "--no-create", "--scp-target", "root@1.2.3.4:/root", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["success"] is True
    assert payload["data"]["branch"] == "demo"
    assert payload["data"]["source_manifest"]["branch_name"] == "demo"
    assert payload["data"]["smoke_checks"]["success"] is True
    assert deploy_kwargs["strict_deploy_drift"] is False
    # `--restore-volumes` defaults to True (flipped 2026-04-27): a fresh-droplet
    # deploy should land as a working staging by default; pass
    # --no-restore-volumes for code-only.
    assert deploy_kwargs.get("restore_volumes") is True
    assert deploy_kwargs.get("no_build") is False


def test_droplet_deploy_passes_strict_deploy_drift_flag(monkeypatch, runner):
    deploy_kwargs = {}

    class FakeDeployManager:
        def deploy(self, **kwargs):
            deploy_kwargs.update(kwargs)
            return True

        def get_last_deploy_report(self):
            return {
                "source_manifest": {"source_path": "/tmp/worktrees/demo", "branch_name": "demo"},
                "drift_report": {"status": "ok", "differences": []},
                "smoke_checks": {"success": True, "path": "/", "checks": []},
                "stage_timings": {},
                "failure_category": None,
                "recovery_attempted": False,
                "recovery_succeeded": False,
            }

        def cleanup(self):
            pass

    monkeypatch.setattr("ilha.cli_commands.droplets.auto_detect_branch_name", lambda branch, json: branch or "demo")
    monkeypatch.setattr("ilha.cli_commands.droplets.check_prerequisites_no_git", lambda: None)
    monkeypatch.setattr("ilha.cli_commands.droplets.check_setup_or_prompt", lambda: None)
    monkeypatch.setattr("ilha.cli_commands.droplets.check_prerequisites", lambda: None)
    monkeypatch.setattr("ilha.cli_commands.droplets.parse_rsync_target_or_droplet", lambda target, token=None: target)
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.SlimDeployManager", FakeDeployManager)

    result = runner.invoke(
        cli,
        [
            "droplet",
            "deploy",
            "demo",
            "--no-create",
            "--scp-target",
            "root@1.2.3.4:/root",
            "--strict-deploy-drift",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert deploy_kwargs["strict_deploy_drift"] is True


def test_droplet_list_skips_docker_prerequisite(monkeypatch, runner):
    """Cloud-only droplet list must not block on `docker info` (see ilha.utils.validation)."""

    def _docker_prereq_must_not_run():
        raise AssertionError("check_prerequisites_no_git must not run for ilha droplet list")

    monkeypatch.setattr("ilha.cli_commands.droplets.check_prerequisites_no_git", _docker_prereq_must_not_run)

    calls = {"n": 0}

    def fake_list_droplets(self, api_token=None, json=False, csv=False):
        calls["n"] += 1
        return True

    monkeypatch.setattr("ilha.commands.droplets.DropletCommands.list_droplets", fake_list_droplets)

    result = runner.invoke(cli, ["droplet", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert calls["n"] == 1
