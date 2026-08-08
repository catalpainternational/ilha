"""CLI tests for `ilha secrets` commands."""

from __future__ import annotations

import json

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


def _extract_json(output: str) -> dict:
    start = output.find("{")
    end = output.rfind("}")
    assert start != -1 and end != -1
    return json.loads(output[start : end + 1])


def test_secrets_group_registered(runner):
    result = runner.invoke(cli, ["secrets", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "edit" in result.output
    assert "materialize" in result.output


def test_secrets_list_json(monkeypatch, runner):
    class FakeSecretsManager:
        def list_profiles(self):
            return ["staging", "production"]

        def default_profile_name(self):
            return "staging"

        def load_secrets_config(self):
            return {"provider": "sops"}

    monkeypatch.setattr("ilha.cli_commands.secrets.SecretsManager", FakeSecretsManager)

    result = runner.invoke(cli, ["secrets", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["success"] is True
    assert payload["data"]["profiles"] == ["staging", "production"]


def test_secrets_print_redacts(monkeypatch, runner):
    class FakeSecretsManager:
        def decrypt_profile(self, profile_name=None):
            return {"DJANGO_SECRET_KEY": "secret", "SITE_DOMAIN": "x"}

        def redact(self, env):
            return {k: ("***" if "SECRET" in k else v) for k, v in env.items()}

    monkeypatch.setattr("ilha.cli_commands.secrets.SecretsManager", FakeSecretsManager)

    result = runner.invoke(cli, ["secrets", "print", "staging", "--redact", "--json"])
    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["data"]["env"]["DJANGO_SECRET_KEY"] == "***"


def test_secrets_print_preserves_long_and_bracketed_values(runner, monkeypatch, capfd):
    """Data fidelity: rich's console hard-wraps long lines at terminal width
    and eats [bracketed] text as markup — either corrupts `secrets print`
    output consumed by pipes (a 164-char API key came back 65 chars)."""
    long_value = "sk-" + "x" * 200
    bracketed = "prefix[red]suffix"
    monkeypatch.setattr(
        "ilha.cli_commands.secrets.SecretsManager.decrypt_profile",
        lambda self, name=None: {"LONG_KEY": long_value, "BRACKET_KEY": bracketed},
    )
    result = runner.invoke(cli, ["secrets", "print", "staging"])
    assert result.exit_code == 0
    out = capfd.readouterr().out + result.output
    assert f"LONG_KEY={long_value}" in out
    assert f"BRACKET_KEY={bracketed}" in out
