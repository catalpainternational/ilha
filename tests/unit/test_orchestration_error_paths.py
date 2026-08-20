"""Failure-path regression tests for worker orchestration.

Each of these pins a defect found in review: an unreadable fleet reported as
an empty one, error envelopes exiting 0, diagnostics corrupting `--json`
stdout, and destroy failures reported as success.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from ilha.cli import cli
from ilha.core.dns_providers.digitalocean import DigitalOceanProvider
from ilha.exceptions import ProviderAPIError, ilhaCommandError


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def noop_prerequisites(monkeypatch):
    monkeypatch.setattr("ilha.cli.helpers.check_setup_or_prompt", lambda: None)
    monkeypatch.setattr("ilha.cli.helpers.check_prerequisites", lambda: None)


def _response(status, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else {}
    if status >= 400:
        import requests

        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    resp.text = json.dumps(payload or {})
    return resp


class TestProviderListingFailures:
    def test_auth_failure_raises_instead_of_returning_empty(self, monkeypatch):
        """The live defect: a 401 produced `droplets: [], success: true`."""
        provider = DigitalOceanProvider("dead-token")
        monkeypatch.setattr(provider.session, "request", lambda *a, **k: _response(401))
        with pytest.raises(ProviderAPIError, match="authentication failed"):
            provider.list_droplets()

    def test_missing_resource_404_is_a_clean_empty(self, monkeypatch):
        provider = DigitalOceanProvider("token")
        monkeypatch.setattr(provider.session, "request", lambda *a, **k: _response(404))
        assert provider.list_droplets() == []

    def test_mid_pagination_failure_raises_not_truncates(self, monkeypatch):
        provider = DigitalOceanProvider("token")
        page_one = _response(
            200,
            {
                "droplets": [{"id": 1, "name": "a", "networks": {}}],
                "links": {"pages": {"next": "..."}},
            },
        )
        responses = iter([page_one, _response(500)])
        monkeypatch.setattr(provider.session, "request", lambda *a, **k: next(responses))
        monkeypatch.setattr(provider, "_MAX_REQUEST_ATTEMPTS", 1)
        with pytest.raises(ProviderAPIError):
            provider.list_droplets()


class TestJsonModeContract:
    def test_error_envelope_exits_nonzero(self, runner, monkeypatch):
        monkeypatch.setattr(
            "ilha.cli_commands.worker.DropletCommands.list_droplets_by_tag",
            lambda self, *a, **k: (_ for _ in ()).throw(
                ilhaCommandError("boom", error_code="do_api_error")
            ),
        )
        result = runner.invoke(cli, ["worker", "list", "--branch", "staging", "--json"])
        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert payload["error"] == "boom"

    def test_diagnostics_go_to_stderr_not_stdout(self, capfd):
        """A stray log line on stdout breaks every `--json` consumer."""
        from ilha.utils.logging import log_error, print_plain

        log_error("diagnostic line")
        print_plain("data line")
        captured = capfd.readouterr()
        assert "diagnostic line" not in captured.out
        assert "diagnostic line" in captured.err
        assert "data line" in captured.out


class TestWorkerDestroySemantics:
    def _droplet(self, id):
        return SimpleNamespace(
            id=id,
            name=f"worker-staging-{id}",
            status="active",
            ip_address="1.2.3.4",
            private_ip_address="10.0.0.9",
            region="syd1",
            size="s-2vcpu-4gb",
            tags=["worker", "branch:staging", "ilha-project:app"],
            created_at=None,
        )

    def test_destroy_failure_is_an_error_envelope(self, runner, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ilha").mkdir()
        (tmp_path / ".ilha" / "config.yml").write_text("worker_pool: {}\n")
        droplet = self._droplet(7)
        monkeypatch.setattr(
            "ilha.cli_commands.worker.DropletCommands.list_droplets_by_tag",
            lambda self, *a, **k: [droplet],
        )
        monkeypatch.setattr(
            "ilha.cli_commands.worker.DropletCommands.destroy_droplet",
            lambda self, **k: False,
        )
        result = runner.invoke(
            cli, ["worker", "destroy", "--all", "--branch", "staging", "--force", "--json"]
        )
        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert payload["details"]["failed"] == [7]

    def test_destroy_all_refuses_without_project_context(self, runner, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)  # no .ilha/config.yml anywhere above tmp
        result = runner.invoke(
            cli, ["worker", "destroy", "--all", "--branch", "staging", "--force", "--json"]
        )
        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert "project" in payload["error"].lower()

    def test_worker_list_rows_carry_created_at(self, runner, monkeypatch, tmp_path):
        from datetime import datetime, timezone

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ilha").mkdir()
        (tmp_path / ".ilha" / "config.yml").write_text("worker_pool: {}\n")
        droplet = self._droplet(3)
        droplet.created_at = datetime(2026, 8, 8, 1, 2, 3, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "ilha.cli_commands.worker.DropletCommands.list_droplets_by_tag",
            lambda self, *a, **k: [droplet],
        )
        result = runner.invoke(cli, ["worker", "list", "--branch", "staging", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["data"]["droplets"][0]["created_at"] == "2026-08-08T01:02:03+00:00"
