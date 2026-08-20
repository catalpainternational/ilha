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


def test_worker_bundle_requires_central(runner, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["worker", "bundle", "staging"])
    assert result.exit_code != 0
    assert "central" in result.output.lower()
