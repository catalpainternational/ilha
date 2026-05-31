"""Unit tests for SOPS-backed SecretsManager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from ilha.commands.secrets import SecretsManager, SopsBackend
from ilha.exceptions import ilhaCommandError


def _project_with_secrets(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    ilha = root / ".ilha"
    ilha.mkdir(parents=True)
    (ilha / "config.yml").write_text(
        yaml.safe_dump(
            {
                "secrets": {
                    "provider": "sops",
                    "default_profile": "staging",
                    "profiles": {
                        "staging": {"file": ".ilha/env.staging.sops.env"},
                    },
                }
            },
            sort_keys=False,
        )
    )
    encrypted = ilha / "env.staging.sops.env"
    encrypted.write_text("ENC[staging]\n")
    return root


def test_list_and_resolve_profiles(tmp_path: Path):
    root = _project_with_secrets(tmp_path)
    mgr = SecretsManager(root)
    assert mgr.list_profiles() == ["staging"]
    profile = mgr.resolve_profile("staging")
    assert profile.name == "staging"
    assert profile.file.name == "env.staging.sops.env"


def test_decrypt_profile_uses_sops(tmp_path: Path):
    root = _project_with_secrets(tmp_path)
    mgr = SecretsManager(root)
    profile = mgr.resolve_profile("staging")

    with patch.object(
        SopsBackend,
        "decrypt_file",
        return_value="DJANGO_SECRET_KEY=abc\nDOCR_READ_TOKEN=token\n",
    ) as decrypt_mock:
        env = mgr.decrypt_profile("staging")

    decrypt_mock.assert_called_once_with(profile.file)
    assert env["DJANGO_SECRET_KEY"] == "abc"
    assert env["DOCR_READ_TOKEN"] == "token"


def test_redact_hides_sensitive_values(tmp_path: Path):
    root = _project_with_secrets(tmp_path)
    mgr = SecretsManager(root)
    redacted = mgr.redact(
        {"SITE_DOMAIN": "example.com", "DJANGO_SECRET_KEY": "secret", "FOO": "bar"}
    )
    assert redacted["SITE_DOMAIN"] == "example.com"
    assert redacted["FOO"] == "bar"
    assert redacted["DJANGO_SECRET_KEY"] == "***REDACTED***"


def test_materialize_plaintext_refuses_overwrite(tmp_path: Path):
    root = _project_with_secrets(tmp_path)
    mgr = SecretsManager(root)
    out = root / ".ilha" / "env.plain"
    out.write_text("OLD=1\n")

    with patch.object(SopsBackend, "decrypt_file", return_value="NEW=2\n"):
        with pytest.raises(ilhaCommandError, match="Refusing to overwrite"):
            mgr.materialize_plaintext("staging", out, force=False)

        written = mgr.materialize_plaintext("staging", out, force=True)

    assert written == out
    assert "NEW=2" in out.read_text()
    assert oct(out.stat().st_mode & 0o777) == oct(0o600)
