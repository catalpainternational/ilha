"""CLI tests for `ilha image` commands."""

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


def test_image_group_is_registered_and_reserved(runner):
    result = runner.invoke(cli, ["image", "--help"])

    assert result.exit_code == 0
    assert "build" in result.output
    assert "publish" in result.output
    assert "list" in result.output
    assert "destroy" in result.output


def test_image_build_cli_invokes_manager(monkeypatch, runner):
    captured = {}

    class FakeImageManager:
        def build(self, branch, options):
            captured["branch"] = branch
            captured["options"] = options
            return {"built_images": ["example/web:tag"]}

    monkeypatch.setattr("ilha.cli_commands.image.ImageManager", lambda: FakeImageManager())

    result = runner.invoke(
        cli,
        [
            "image",
            "build",
            "staging",
            "--registry",
            "reg",
            "--repository",
            "repo",
            "--service",
            "web",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["branch"] == "staging"
    assert captured["options"].registry == "reg"
    assert captured["options"].repository == "repo"
    assert captured["options"].services == ("web",)
    payload = _extract_json(result.output)
    assert payload["success"] is True
    assert payload["data"]["built_images"] == ["example/web:tag"]


def test_image_publish_cli_invokes_manager(monkeypatch, runner):
    captured = {}

    class FakeImageManager:
        def publish(self, branch, options):
            captured["branch"] = branch
            captured["options"] = options
            return {"manifest": ".ilha/images/staging.json"}

    monkeypatch.setattr("ilha.cli_commands.image.ImageManager", lambda: FakeImageManager())

    result = runner.invoke(cli, ["image", "publish", "staging", "--registry", "reg", "--save", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["branch"] == "staging"
    assert captured["options"].save is True
    payload = _extract_json(result.output)
    assert payload["data"]["manifest"] == ".ilha/images/staging.json"


def test_image_list_cli_invokes_manager(monkeypatch, runner):
    captured = {}

    class FakeImageManager:
        def list_manifests(self, branch=None):
            captured["branch"] = branch
            return [{"branch": "staging", "services": []}]

        def flatten_manifest_rows(self, manifests):
            return []

        def _load_image_config(self):
            return {}

    monkeypatch.setattr("ilha.cli_commands.image.ImageManager", lambda: FakeImageManager())

    result = runner.invoke(cli, ["image", "list", "staging", "--json"])
    assert result.exit_code == 0, result.output
    assert captured["branch"] == "staging"


def test_image_destroy_cli_invokes_manager(monkeypatch, runner):
    captured = {}

    class FakeImageManager:
        def destroy_branches(self, branches, **kwargs):
            captured["branches"] = list(branches)
            captured["kwargs"] = kwargs
            return {"count": 1, "results": []}

    monkeypatch.setattr("ilha.cli_commands.image.ImageManager", lambda: FakeImageManager())

    result = runner.invoke(cli, ["image", "destroy", "staging,main", "--force", "--json"])
    assert result.exit_code == 0, result.output
    assert captured["branches"] == ["staging", "main"]
    assert captured["kwargs"]["force"] is True
