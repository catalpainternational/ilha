"""Tests for CLI secrets profile resolution (explicit, default, --no-secrets)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ilha.commands.secrets import SecretsManager, resolve_cli_secrets_profile
from ilha.exceptions import ilhaCommandError


def _project(tmp_path: Path, *, with_default: bool = True) -> Path:
    root = tmp_path / "proj"
    ilha = root / ".ilha"
    ilha.mkdir(parents=True)
    secrets = {
        "provider": "sops",
        "profiles": {"staging": {"file": ".ilha/env.staging.sops.env"}},
    }
    if with_default:
        secrets["default_profile"] = "staging"
    (ilha / "config.yml").write_text(yaml.safe_dump({"secrets": secrets}, sort_keys=False))
    (ilha / "env.staging.sops.env").write_text("ENC[]\n")
    return root


def test_resolve_optional_uses_default(tmp_path: Path):
    root = _project(tmp_path)
    mgr = SecretsManager(root)
    assert mgr.resolve_optional_profile_name(None) == "staging"
    assert mgr.resolve_optional_profile_name("staging") == "staging"


def test_resolve_optional_none_without_default(tmp_path: Path):
    root = _project(tmp_path, with_default=False)
    mgr = SecretsManager(root)
    assert mgr.resolve_optional_profile_name(None) is None


def test_resolve_optional_none_without_secrets_block(tmp_path: Path):
    root = tmp_path / "plain"
    (root / ".ilha").mkdir(parents=True)
    (root / ".ilha" / "config.yml").write_text("project_name: demo\n")
    mgr = SecretsManager(root)
    assert mgr.resolve_optional_profile_name(None) is None


def test_cli_resolver_prefers_flag_over_default(tmp_path: Path):
    root = _project(tmp_path)
    assert resolve_cli_secrets_profile("staging", project_root=root) == "staging"


def test_cli_resolver_uses_default_when_flag_omitted(tmp_path: Path):
    root = _project(tmp_path)
    assert resolve_cli_secrets_profile(None, project_root=root) == "staging"


def test_cli_resolver_no_secrets_wins(tmp_path: Path):
    root = _project(tmp_path)
    assert resolve_cli_secrets_profile(None, no_secrets=True, project_root=root) is None


def test_cli_resolver_rejects_secrets_and_no_secrets(tmp_path: Path):
    root = _project(tmp_path)
    with pytest.raises(ilhaCommandError, match="mutually exclusive"):
        resolve_cli_secrets_profile("staging", no_secrets=True, project_root=root)
