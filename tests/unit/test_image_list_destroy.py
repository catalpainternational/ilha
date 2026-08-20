"""Unit tests for ilha image list and destroy."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from ilha.commands.image import ImageManager, parse_docr_image_ref
from ilha.exceptions import ilhaCommandError


def _project(tmp_path: Path, manifests: dict | None = None) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    ilha = root / ".ilha"
    ilha.mkdir()
    (ilha / "config.yml").write_text(
        yaml.safe_dump({"image": {"registry": "reg", "repository": "repo"}}, sort_keys=False)
    )
    images = ilha / "images"
    images.mkdir()
    for branch, data in (manifests or {}).items():
        (images / f"{branch}.json").write_text(json.dumps(data, indent=2))
    return root


def test_parse_docr_image_ref_valid():
    ref = parse_docr_image_ref("registry.digitalocean.com/catalpa/bower/web:staging-abc1234")
    assert ref.registry_name == "catalpa"
    assert ref.repository == "bower/web"
    assert ref.tag == "staging-abc1234"


def test_parse_docr_image_ref_strips_digest_suffix():
    ref = parse_docr_image_ref(
        "registry.digitalocean.com/catalpa/bower/web:staging-abc1234@sha256:deadbeef"
    )
    assert ref.tag == "staging-abc1234"
    assert ref.repository == "bower/web"


def test_parse_docr_image_ref_invalid_host():
    with pytest.raises(ilhaCommandError, match="Not a DigitalOcean registry"):
        parse_docr_image_ref("docker.io/library/nginx:latest")


def test_list_manifests_all_and_one_branch(tmp_path):
    root = _project(
        tmp_path,
        {
            "staging": {
                "branch": "staging",
                "published_at": "2026-01-01T00:00:00Z",
                "services": [{"service": "web", "image": "registry.digitalocean.com/reg/repo/web:t1"}],
            },
            "main": {
                "branch": "main",
                "published_at": "2026-01-02T00:00:00Z",
                "services": [],
            },
        },
    )
    mgr = ImageManager(root)
    assert len(mgr.list_manifests()) == 2
    assert len(mgr.list_manifests("staging")) == 1
    assert mgr.list_manifests("missing") == []


def test_list_manifests_skips_invalid_json(tmp_path, capsys):
    root = _project(tmp_path, {})
    bad = root / ".ilha" / "images" / "bad.json"
    bad.write_text("{not json")
    good = root / ".ilha" / "images" / "good.json"
    good.write_text(json.dumps({"branch": "good", "services": []}))
    mgr = ImageManager(root)
    manifests = mgr.list_manifests()
    assert len(manifests) == 1
    assert manifests[0]["branch"] == "good"


def test_flatten_manifest_rows():
    rows = ImageManager.flatten_manifest_rows(
        [
            {
                "branch": "staging",
                "published_at": "2026-01-01T00:00:00Z",
                "services": [
                    {"service": "web", "image": "img:tag", "digest": "sha256:abc"},
                ],
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0]["service"] == "web"
    assert rows[0]["digest"] == "sha256:abc"


def test_destroy_branch_registry_and_manifest(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        {
            "staging": {
                "branch": "staging",
                "provider": "digitalocean",
                "registry": "reg",
                "services": [
                    {
                        "service": "web",
                        "image": "registry.digitalocean.com/reg/repo/web:staging-abc",
                    }
                ],
            }
        },
    )
    mgr = ImageManager(root)
    deleted = []

    monkeypatch.setattr(mgr, "_delete_docr_tag", lambda ref, registry_override=None: deleted.append(ref.tag))
    monkeypatch.setattr("ilha.commands.image.confirm_action", lambda _msg: True)

    result = mgr.destroy_branch("staging", force=True)
    assert result["manifest_removed"] is True
    assert result["deleted_tags"] == ["registry.digitalocean.com/reg/repo/web:staging-abc"]
    assert deleted == ["staging-abc"]
    assert not mgr.manifest_path("staging").exists()


def test_destroy_branch_manifest_only_skips_registry(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        {
            "staging": {
                "branch": "staging",
                "provider": "digitalocean",
                "services": [
                    {"service": "web", "image": "registry.digitalocean.com/reg/repo/web:t"},
                ],
            }
        },
    )
    mgr = ImageManager(root)
    monkeypatch.setattr(
        mgr,
        "_delete_docr_tag",
        MagicMock(side_effect=AssertionError("should not delete registry")),
    )
    monkeypatch.setattr("ilha.commands.image.confirm_action", lambda _msg: True)

    result = mgr.destroy_branch("staging", manifest_only=True, force=True)
    assert result["deleted_tags"] == []
    assert result["manifest_removed"] is True


def test_destroy_branch_registry_only_keeps_manifest(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        {
            "staging": {
                "branch": "staging",
                "provider": "digitalocean",
                "services": [
                    {"service": "web", "image": "registry.digitalocean.com/reg/repo/web:t"},
                ],
            }
        },
    )
    mgr = ImageManager(root)
    monkeypatch.setattr(mgr, "_delete_docr_tag", lambda ref, registry_override=None: None)
    monkeypatch.setattr("ilha.commands.image.confirm_action", lambda _msg: True)

    result = mgr.destroy_branch("staging", registry_only=True, force=True)
    assert result["manifest_removed"] is False
    assert mgr.manifest_path("staging").exists()


def test_list_remote_tags_uses_registry_client(tmp_path, monkeypatch):
    root = _project(tmp_path, {})
    mgr = ImageManager(root)

    class FakeClient:
        def __init__(self):
            self.list_repo_calls = []
            self.list_tag_calls = []

        def list_repositories(self, registry):
            self.list_repo_calls.append(registry)
            return ["repo/web"]

        def list_tags(self, registry, repository):
            self.list_tag_calls.append((registry, repository))
            return [{"tag": "staging-abc", "updated_at": "2026-01-01T00:00:00Z"}]

    fake = FakeClient()
    monkeypatch.setattr(mgr, "_registry_client", lambda: fake)
    tags = mgr.list_remote_tags("reg")
    assert len(tags) == 1
    assert tags[0]["tag"] == "staging-abc"
    assert fake.list_repo_calls == ["reg"]
