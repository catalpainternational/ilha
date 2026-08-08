"""
Environment file loader utility for ilha.

Parses plaintext dotenv files and provides merge/format helpers used by
deploy, worker bundles, and SOPS-backed secret profiles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Union

from .logging import log_warning

EnvLayer = Union[Path, Mapping[str, str], None]

SENSITIVE_ENV_SUFFIXES = ("_TOKEN", "_PASSWORD", "_SECRET", "_PRIVATE_KEY", "_API_KEY")
DEFAULT_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "DIGITALOCEAN_API_TOKEN",
        "DOCR_READ_TOKEN",
        "DJANGO_SECRET_KEY",
        "EMAIL_HOST_PASSWORD",
        "SECRET_KEY",
    }
)


def is_sensitive_env_key(key: str, *, extra_exclude: Optional[Iterable[str]] = None) -> bool:
    """Return True if an env key looks like a secret."""
    upper = key.upper()
    if upper in DEFAULT_SENSITIVE_EXACT_KEYS:
        return True
    if extra_exclude:
        excluded = {item.upper() for item in extra_exclude}
        if upper in excluded:
            return True
    return any(upper.endswith(suffix) for suffix in SENSITIVE_ENV_SUFFIXES)


def sensitive_env_keys(env: Mapping[str, str], *, extra_exclude: Optional[Iterable[str]] = None) -> List[str]:
    """Return env keys that look sensitive."""
    return sorted(key for key in env if is_sensitive_env_key(key, extra_exclude=extra_exclude))


def parse_env_text(text: str) -> Dict[str, str]:
    """Parse dotenv text into a key/value dict."""
    return parse_env_lines(text.splitlines())


def parse_env_lines(lines: Iterable[str]) -> Dict[str, str]:
    """Parse dotenv lines into a key/value dict."""
    env_vars: Dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2:
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
        if key:
            env_vars[key] = value
    return env_vars


def format_env_text(
    env: Mapping[str, str],
    *,
    quote_values: bool = False,
) -> str:
    """Serialize env vars to dotenv text."""
    lines: List[str] = []
    for key in sorted(env.keys()):
        value = str(env[key])
        if quote_values:
            safe = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{safe}"')
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + ("\n" if lines else "")


def merge_env_layers(*layers: EnvLayer) -> Dict[str, str]:
    """Merge env layers left-to-right; later layers override earlier ones."""
    merged: Dict[str, str] = {}
    for layer in layers:
        if layer is None:
            continue
        if isinstance(layer, Path):
            merged.update(load_env_file(layer))
        else:
            merged.update(dict(layer))
    return merged


def load_env_file(env_path: Path) -> Dict[str, str]:
    """Load and parse a plaintext .env file.

    Args:
        env_path: Path to .env file

    Returns:
        Dictionary of key-value pairs, or empty dict if missing/unreadable.
    """
    if not env_path.exists():
        return {}

    try:
        return parse_env_text(env_path.read_text(encoding="utf-8"))
    except (OSError, IOError) as exc:
        log_warning(f"Failed to read .env file at {env_path}: {exc}")
        return {}
    except Exception as exc:
        log_warning(f"Error parsing .env file at {env_path}: {exc}")
        return {}


def load_env_from_project_root() -> Dict[str, str]:
    """Load .env from project root."""
    try:
        from ..config.settings import get_project_root

        project_root = get_project_root()
        return load_env_file(project_root / ".env")
    except Exception as exc:
        log_warning(f"Failed to load .env file from project root: {exc}")
        return {}


def load_env_from_home() -> Dict[str, str]:
    """Load ~/.ilha/env.ilha for global ilha configuration."""
    try:
        env_path = Path.home() / ".ilha" / "env.ilha"
        return load_env_file(env_path)
    except Exception as exc:
        log_warning(f"Failed to load .env file from home directory: {exc}")
        return {}
