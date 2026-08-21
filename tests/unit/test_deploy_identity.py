"""Tests for /app/.ilha/deploy.json identity."""

from __future__ import annotations

from pathlib import Path

from ilha.commands.push.deploy_identity import (
    build_deploy_identity,
    parse_deploy_identity,
    write_deploy_identity,
)


def test_build_deploy_identity_copies_manifest_fields():
    identity = build_deploy_identity(
        {
            "git_sha": "abc123",
            "git_branch": "staging",
            "git_dirty": True,
            "branch_name": "staging",
        },
        image_refs={"web": "reg/web@sha256:1"},
        build=False,
        secrets_profile="staging",
        ilha_version="0.9.8",
        deployed_at="2026-08-20T00:00:00Z",
    )
    assert identity["git_sha"] == "abc123"
    assert identity["git_dirty"] is True
    assert identity["image_refs"]["web"].endswith("@sha256:1")
    assert identity["secrets_profile"] == "staging"
    assert "PASSWORD" not in str(identity)


def test_write_and_parse_roundtrip(tmp_path: Path):
    path = tmp_path / ".ilha" / "deploy.json"
    payload = build_deploy_identity(
        {"git_sha": "deadbeef", "branch_name": "demo"},
        deployed_at="2026-08-20T00:00:00Z",
        ilha_version="0.9.8",
    )
    write_deploy_identity(path, payload)
    assert path.exists()
    parsed = parse_deploy_identity(path.read_text())
    assert parsed["git_sha"] == "deadbeef"
    assert parse_deploy_identity("not-json") == {}
