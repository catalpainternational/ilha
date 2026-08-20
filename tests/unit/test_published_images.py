"""Tests for pinning compose services to published image digests."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ilha.commands.image import ImageManager
from ilha.utils.published_images import (
    pin_compose_file,
    pin_deploy_compose_files,
    pin_service_to_image_ref,
)


def test_pin_service_drops_build():
    svc = {"build": ".", "image": "local", "command": "web"}
    pin_service_to_image_ref(svc, "registry.example/app@sha256:abc")
    assert "build" not in svc
    assert svc["image"] == "registry.example/app@sha256:abc"
    assert svc["command"] == "web"


def test_pin_compose_file_rewrites_matching_services(tmp_path: Path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "web": {"build": ".", "image": "app"},
                    "db": {"image": "postgres:16"},
                }
            },
            sort_keys=False,
        )
    )
    pinned = pin_compose_file(compose, {"web": "reg/web@sha256:dead"})
    assert pinned == ["web"]
    data = yaml.safe_load(compose.read_text())
    assert data["services"]["web"]["image"] == "reg/web@sha256:dead"
    assert "build" not in data["services"]["web"]
    assert data["services"]["db"]["image"] == "postgres:16"


def test_pin_deploy_compose_files_uses_manifest(tmp_path: Path):
    root = tmp_path / "proj"
    ilha = root / ".ilha"
    ilha.mkdir(parents=True)
    (ilha / "config.yml").write_text("project_name: demo\n")
    images = ilha / "images"
    images.mkdir()
    (images / "staging.json").write_text(
        json.dumps(
            {
                "branch": "staging",
                "services": [
                    {
                        "service": "web",
                        "image": "registry.digitalocean.com/reg/repo/web:staging-abc",
                        "digest": "sha256:cafef00d",
                    }
                ],
            }
        )
    )
    compose = root / "docker-compose.yml"
    compose.write_text(
        yaml.safe_dump({"services": {"web": {"build": "."}, "db": {"image": "postgres:16"}}})
    )
    refs = pin_deploy_compose_files(
        [compose],
        "staging",
        image_manager=ImageManager(root),
        services=["web"],
    )
    assert refs["web"].endswith("@sha256:cafef00d")
    data = yaml.safe_load(compose.read_text())
    assert data["services"]["web"]["image"] == refs["web"]
    assert "build" not in data["services"]["web"]
