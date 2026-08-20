"""Unit tests for registry-backed image planning and execution."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ilha.commands.image import ImageManager, ImageOptions
from ilha.exceptions import ilhaCommandError


def _project(tmp_path: Path, config: dict | None = None, compose: dict | None = None) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    ilha = root / ".ilha"
    ilha.mkdir()
    (ilha / "config.yml").write_text(
        yaml.safe_dump({"project_name": "demo", **(config or {})}, sort_keys=False)
    )
    worktree = root / "worktrees" / "staging"
    worktree.mkdir(parents=True)
    (worktree / ".ilha").mkdir()
    (worktree / ".ilha" / "env.ilha").write_text("COMPOSE_PROJECT_NAME=demo-staging\n")
    (worktree / ".env").write_text("")
    (worktree / "Dockerfile").write_text("FROM scratch\n")
    (worktree / "worker.Dockerfile").write_text("FROM scratch\n")
    (worktree / ".dockerignore").write_text(".ilha/\nsecrets/\nbackups/\n")
    compose_data = compose or {
        "services": {
            "web": {"build": "."},
            "rq-worker-1": {"build": "."},
            "redis": {"image": "redis:7"},
        }
    }
    (worktree / "docker-compose.yml").write_text(yaml.safe_dump(compose_data, sort_keys=False))
    return root


def _completed(code: int = 0, stdout: str = ""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


def test_resolve_plan_merges_cli_config_and_defaults(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        {
            "image": {
                "provider": "digitalocean",
                "registry": "saved-registry",
                "repository": "saved-repo",
                "platform": "linux/arm64",
                "tag_template": "{branch}-{sha7}",
            }
        },
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _completed(stdout="abcdef1234567890\n"))

    plan = ImageManager(root).resolve_plan(
        "staging",
        ImageOptions(registry="cli-registry", repository="cli-repo"),
    )

    assert plan.registry == "cli-registry"
    assert plan.repository == "cli-repo"
    assert plan.platform == "linux/arm64"
    assert plan.tag == "staging-abcdef1"
    assert [svc.service for svc in plan.services] == ["web", "rq-worker-1"]
    assert plan.services[0].image_ref == "registry.digitalocean.com/cli-registry/cli-repo/web:staging-abcdef1"


def test_save_preferences_updates_only_image_block(tmp_path):
    root = _project(tmp_path, {"deployment": {"default_domain": "app.example.com"}})

    ImageManager(root).save_preferences(
        ImageOptions(registry="catalpa", repository="bower", platform="linux/amd64")
    )

    config = yaml.safe_load((root / ".ilha" / "config.yml").read_text())
    assert config["deployment"] == {"default_domain": "app.example.com"}
    assert config["image"]["registry"] == "catalpa"
    assert config["image"]["repository"] == "bower"
    assert config["image"]["platform"] == "linux/amd64"


def test_requested_non_buildable_service_fails(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _completed(stdout="abcdef1234567890\n"))

    with pytest.raises(ilhaCommandError, match="no build directive"):
        ImageManager(root).resolve_plan("staging", ImageOptions(services=("redis",)))


def test_default_services_do_not_include_extra_buildable_services(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        compose={
            "services": {
                "web": {"build": "."},
                "rq-worker-1": {"build": "."},
                "debug-shell": {"build": "."},
            }
        },
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _completed(stdout="abcdef1234567890\n"))

    plan = ImageManager(root).resolve_plan("staging", ImageOptions())

    assert [svc.service for svc in plan.services] == ["web", "rq-worker-1"]


def test_unsupported_build_keys_fail_with_clear_error(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        compose={
            "services": {
                "web": {
                    "build": {
                        "context": ".",
                        "additional_contexts": {"assets": "./assets"},
                    }
                }
            }
        },
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _completed(stdout="abcdef1234567890\n"))

    with pytest.raises(ilhaCommandError, match="Unsupported compose build key"):
        ImageManager(root).resolve_plan("staging", ImageOptions())


def test_unknown_tag_template_field_fails_clearly(tmp_path, monkeypatch):
    root = _project(tmp_path, {"image": {"tag_template": "{branch}-{sha8}"}})
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _completed(stdout="abcdef1234567890\n"))

    with pytest.raises(ilhaCommandError, match="Unknown image tag template field"):
        ImageManager(root).resolve_plan("staging", ImageOptions())


def test_tag_template_expands_per_service(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        {"image": {"registry": "reg", "repository": "repo", "tag_template": "{branch}-{service}-{sha7}"}},
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _completed(stdout="abcdef1234567890\n"))

    plan = ImageManager(root).resolve_plan("staging", ImageOptions())

    tags = {svc.service: svc.tag for svc in plan.services}
    assert tags["web"] == "staging-web-abcdef1"
    assert tags["rq-worker-1"] == "staging-rq-worker-1-abcdef1"


def test_build_deduplicates_shared_build_context_and_tags_worker(tmp_path, monkeypatch):
    root = _project(tmp_path, {"image": {"registry": "reg", "repository": "repo"}})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return _completed(stdout="abcdef1234567890\n")
        return _completed()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = ImageManager(root).build("staging", ImageOptions())

    docker_builds = [c for c in calls if c[:2] == ["docker", "build"]]
    docker_tags = [c for c in calls if c[:2] == ["docker", "tag"]]
    assert len(docker_builds) == 1
    assert len(docker_tags) == 1
    assert sorted(result["built_images"]) == [
        "registry.digitalocean.com/reg/repo/rq-worker-1:staging-abcdef1",
        "registry.digitalocean.com/reg/repo/web:staging-abcdef1",
    ]


def test_push_requires_registry(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _completed(stdout="abcdef1234567890\n"))

    with pytest.raises(ilhaCommandError, match="registry is required"):
        ImageManager(root).push("staging", ImageOptions())


def test_publish_writes_manifest_after_push(tmp_path, monkeypatch):
    root = _project(tmp_path, {"image": {"registry": "reg", "repository": "repo"}})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return _completed(stdout="abcdef1234567890\n")
        if cmd[:2] == ["docker", "login"]:
            return _completed()
        if cmd[:3] == ["docker", "image", "inspect"] and "--format" not in cmd:
            return _completed()
        if cmd[:4] == ["docker", "image", "inspect", "--format"]:
            return _completed(stdout=f"{cmd[-1]}@sha256:deadbeef\n")
        return _completed()

    class FakeRegistryClient:
        def docker_login(self, registry, **kwargs):
            calls.append(["registry_login", registry])

    monkeypatch.setenv("DIGITALOCEAN_API_TOKEN", "token")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        ImageManager,
        "_registry_client",
        lambda self: FakeRegistryClient(),
    )

    result = ImageManager(root).publish("staging", ImageOptions())

    manifest_path = Path(result["manifest"])
    manifest = json.loads(manifest_path.read_text())
    assert manifest["branch"] == "staging"
    assert manifest["services"][0]["digest"] == "sha256:deadbeef"
    assert any(c[:2] == ["docker", "push"] for c in calls)
    assert not any(c[:3] == ["docker", "login", "registry.digitalocean.com"] for c in calls)


def test_push_requires_registry_api_token(tmp_path, monkeypatch):
    root = _project(tmp_path, {"image": {"registry": "reg", "repository": "repo"}})

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return _completed(stdout="abcdef1234567890\n")
        return _completed()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("ilha.commands.image.DropletManager.resolve_droplet_token", lambda token: None)

    with pytest.raises(ilhaCommandError, match="DigitalOcean API token not found"):
        ImageManager(root).push("staging", ImageOptions())


def test_build_rejects_sensitive_build_args(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        compose={
            "services": {
                "web": {
                    "build": {
                        "context": ".",
                        "args": {"DJANGO_SECRET_KEY": "nope"},
                    }
                }
            }
        },
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: _completed(stdout="abcdef1234567890\n"),
    )

    with pytest.raises(ilhaCommandError, match="Sensitive values must not be passed"):
        ImageManager(root).build("staging", ImageOptions(services=("web",)))


def test_build_refuses_unignored_ilha_dir_in_context(tmp_path, monkeypatch):
    root = _project(tmp_path)
    worktree = root / "worktrees" / "staging"
    (worktree / ".dockerignore").write_text("*.pyc\n")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: _completed(stdout="abcdef1234567890\n"),
    )

    with pytest.raises(ilhaCommandError, match=r"\.ilha/"):
        ImageManager(root).build("staging", ImageOptions(services=("web",)))


def test_resolve_plan_fails_when_git_sha_unavailable(tmp_path, monkeypatch):
    root = _project(tmp_path, {"image": {"registry": "reg", "repository": "repo"}})
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: _completed(code=128, stderr="fatal: not a git repository\n"),
    )

    with pytest.raises(ilhaCommandError, match="Failed to resolve git SHA"):
        ImageManager(root).resolve_plan("staging", ImageOptions())
