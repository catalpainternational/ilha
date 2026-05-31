"""SOPS-backed secret profile management for ilha."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Union

import yaml

from ilha.config.settings import ILHA_DIR, get_project_root
from ilha.exceptions import ilhaCommandError
from ilha.utils.env_loader import (
    EnvLayer,
    format_env_text,
    is_sensitive_env_key,
    merge_env_layers,
    parse_env_text,
    sensitive_env_keys,
)

DEFAULT_PROVIDER = "sops"
REDACTED_VALUE = "***REDACTED***"


@dataclass(frozen=True)
class SecretProfile:
    """Resolved secret profile metadata."""

    name: str
    file: Path


class SopsBackend:
    """Thin wrapper around the ``sops`` CLI."""

    @staticmethod
    def ensure_available() -> str:
        path = shutil.which("sops")
        if not path:
            raise ilhaCommandError(
                "sops is not installed or not on PATH. "
                "Install from https://github.com/getsops/sops and configure age keys."
            )
        return path

    @classmethod
    def decrypt_file(cls, path: Path) -> str:
        cls.ensure_available()
        if not path.exists():
            raise ilhaCommandError(f"Encrypted secrets file not found: {path}")
        result = subprocess.run(
            ["sops", "-d", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise ilhaCommandError(
                f"Failed to decrypt secrets file {path}: {stderr or 'unknown sops error'}"
            )
        return result.stdout

    @classmethod
    def edit_file(cls, path: Path) -> int:
        cls.ensure_available()
        if not path.exists():
            raise ilhaCommandError(f"Encrypted secrets file not found: {path}")
        return subprocess.call(["sops", str(path)])

    @classmethod
    def exec_with_file(cls, path: Path, command: Sequence[str]) -> int:
        cls.ensure_available()
        if not path.exists():
            raise ilhaCommandError(f"Encrypted secrets file not found: {path}")
        return subprocess.call(["sops", "exec-env", str(path), *command])


class SecretsManager:
    """Load, decrypt, merge, and materialize ilha secret profiles."""

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = Path(project_root or get_project_root()).resolve()

    @property
    def config_path(self) -> Path:
        return self.project_root / ILHA_DIR / "config.yml"

    def load_secrets_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return {}
        loaded = yaml.safe_load(self.config_path.read_text()) or {}
        secrets = loaded.get("secrets") if isinstance(loaded, dict) else None
        return secrets if isinstance(secrets, dict) else {}

    def list_profiles(self) -> List[str]:
        profiles = self.load_secrets_config().get("profiles") or {}
        if not isinstance(profiles, dict):
            return []
        return sorted(str(name) for name in profiles.keys())

    def default_profile_name(self) -> Optional[str]:
        default_profile = self.load_secrets_config().get("default_profile")
        return str(default_profile).strip() if default_profile else None

    def resolve_profile(self, profile_name: Optional[str] = None) -> SecretProfile:
        cfg = self.load_secrets_config()
        provider = str(cfg.get("provider") or DEFAULT_PROVIDER).strip()
        if provider != DEFAULT_PROVIDER:
            raise ilhaCommandError(f"Unsupported secrets provider: {provider}")

        profiles = cfg.get("profiles") or {}
        if not isinstance(profiles, dict) or not profiles:
            raise ilhaCommandError(
                "No secret profiles configured. Add a `secrets:` block to .ilha/config.yml."
            )

        name = (profile_name or self.default_profile_name() or "").strip()
        if not name:
            available = ", ".join(self.list_profiles())
            raise ilhaCommandError(
                f"Secret profile is required. Available profiles: {available or '(none)'}"
            )
        if name not in profiles:
            available = ", ".join(self.list_profiles())
            raise ilhaCommandError(
                f"Unknown secret profile {name!r}. Available profiles: {available or '(none)'}"
            )

        entry = profiles[name]
        if isinstance(entry, str):
            rel_path = entry
        elif isinstance(entry, dict):
            rel_path = entry.get("file")
        else:
            rel_path = None
        if not rel_path:
            raise ilhaCommandError(f"Secret profile {name!r} is missing a file path")

        file_path = (self.project_root / str(rel_path)).resolve()
        return SecretProfile(name=name, file=file_path)

    def decrypt_profile(self, profile_name: Optional[str] = None) -> Dict[str, str]:
        profile = self.resolve_profile(profile_name)
        return parse_env_text(SopsBackend.decrypt_file(profile.file))

    def redact(self, env: Mapping[str, str]) -> Dict[str, str]:
        return {
            key: (REDACTED_VALUE if is_sensitive_env_key(key) else value)
            for key, value in env.items()
        }

    def merge_worktree_layers(
        self,
        worktree_path: Path,
        *,
        profile_name: Optional[str] = None,
        extra_layers: Optional[Sequence[EnvLayer]] = None,
    ) -> Dict[str, str]:
        """Merge project .env, worktree env.ilha, and optional SOPS profile."""
        from ilha.utils.path_utils import get_env_compose_file_path

        layers: List[EnvLayer] = [
            self.project_root / ".env",
            get_env_compose_file_path(worktree_path),
        ]
        if extra_layers:
            layers.extend(extra_layers)
        merged = merge_env_layers(*layers)
        if profile_name:
            merged.update(self.decrypt_profile(profile_name))
        return merged

    @contextmanager
    def materialize_temp_env(
        self,
        env: Mapping[str, str],
        *,
        prefix: str = "ilha-secrets-",
    ) -> Iterator[Path]:
        """Write env to a private temp file and delete it on exit."""
        fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".env")
        path = Path(raw_path)
        try:
            os.close(fd)
            path.write_text(format_env_text(env), encoding="utf-8")
            path.chmod(0o600)
            yield path
        finally:
            path.unlink(missing_ok=True)

    def materialize_plaintext(
        self,
        profile_name: str,
        output_path: Path,
        *,
        force: bool = False,
    ) -> Path:
        """Decrypt a profile and write plaintext env to ``output_path``."""
        if output_path.exists() and not force:
            raise ilhaCommandError(
                f"Refusing to overwrite existing file: {output_path}. Use --force to continue."
            )
        env = self.decrypt_profile(profile_name)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(format_env_text(env), encoding="utf-8")
        output_path.chmod(0o600)
        return output_path

    def warn_plaintext_env_in_paths(self, paths: Sequence[Path]) -> List[str]:
        """Return sensitive keys found in plaintext env files."""
        from ilha.utils.env_loader import load_env_file

        found: List[str] = []
        for path in paths:
            if not path.exists():
                continue
            keys = sensitive_env_keys(load_env_file(path))
            if keys:
                found.extend(keys)
        return sorted(set(found))

    def has_configured_profiles(self) -> bool:
        return bool(self.list_profiles())

    def resolve_registry_token(
        self,
        token_env: str,
        *,
        profile_name: Optional[str] = None,
        profile_env: Optional[Mapping[str, str]] = None,
    ) -> str:
        """Resolve a registry token from shell env or decrypted profile env."""
        token = os.environ.get(token_env, "").strip()
        if token:
            return token
        if profile_env and token_env in profile_env:
            return str(profile_env[token_env]).strip()
        if profile_name:
            decrypted = self.decrypt_profile(profile_name)
            return str(decrypted.get(token_env, "")).strip()
        return ""
