"""
Environment file loader utility for ilha.

Parses plaintext dotenv files and provides merge/format helpers used by
deploy, worker bundles, and SOPS-backed secret profiles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Union

from .logging import log_warning
from .secret_policy import CONTROL_PLANE_ENV_KEYS, REGISTRY_LOGIN_ENV_KEYS

EnvLayer = Union[Path, Mapping[str, str], None]

SENSITIVE_ENV_SUFFIXES = ("_TOKEN", "_PASSWORD", "_SECRET", "_PRIVATE_KEY", "_API_KEY")
DEFAULT_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "DJANGO_SECRET_KEY",
        "EMAIL_HOST_PASSWORD",
        "SECRET_KEY",
    }
    | CONTROL_PLANE_ENV_KEYS
    | REGISTRY_LOGIN_ENV_KEYS
)


def _strip_inline_quotes(value: str) -> str:
    """Remove a single pair of matching surrounding quotes, if present."""
    if len(value) >= 2 and (
        (value.startswith('"') and value.endswith('"'))
        or (value.startswith("'") and value.endswith("'"))
    ):
        return value[1:-1]
    return value


def parse_env_content(content: str) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines from *content* into a dict.

    Single source of truth for .env parsing. Rules: ``#`` comment lines and
    blank lines are ignored; the split is on the first ``=``; surrounding
    whitespace is stripped; one pair of matching surrounding quotes is removed.
    Lines without ``=`` or with an empty key are ignored.
    """
    env_vars: Dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        env_vars[key] = _strip_inline_quotes(value.strip())
    return env_vars


def parse_env_text(text: str) -> Dict[str, str]:
    """Parse dotenv text into a key/value dict."""
    return parse_env_content(text)


def parse_env_lines(lines: Iterable[str]) -> Dict[str, str]:
    """Parse dotenv lines into a key/value dict."""
    return parse_env_content("\n".join(lines))


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


def sensitive_env_keys(
    env: Mapping[str, str], *, extra_exclude: Optional[Iterable[str]] = None
) -> List[str]:
    """Return env keys that look sensitive."""
    return sorted(key for key in env if is_sensitive_env_key(key, extra_exclude=extra_exclude))


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


def load_env_file(env_path: Path) -> Dict[str, str]:
    """Load and parse .env file.

    An absent file yields an empty dict — that is a legitimate "no
    environment", not a failure.

    Raises:
        EnvFileError: if the file exists but cannot be read (permissions,
            corruption, non-UTF-8).
    """
    if not env_path.exists():
        return {}

    try:
        content = env_path.read_text(encoding="utf-8")
    except (IOError, OSError, UnicodeDecodeError) as e:
        from ..exceptions import EnvFileError

        raise EnvFileError(f"Failed to read env file at {env_path}: {e}") from e

    return parse_env_content(content)


def merge_env_layers(
    *layers: EnvLayer,
    base_defaults: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Merge ordered env layers into one dict — the single precedence contract.

    Later layers win. A layer may be a mapping, a Path (loaded via
    ``load_env_file``), or None/empty (skipped). ``base_defaults`` are folded
    in with ``setdefault`` semantics so they fill only keys that no layer
    provided.
    """
    merged: Dict[str, str] = {}
    for layer in layers:
        if layer is None:
            continue
        if isinstance(layer, Path):
            merged.update(load_env_file(layer))
            continue
        if layer:
            merged.update(dict(layer))
    if base_defaults:
        for key, value in base_defaults.items():
            merged.setdefault(key, value)
    return merged


def upsert_env_vars(
    env_path: Path,
    updates: Mapping[str, str],
    *,
    remove: Optional[Iterable[str]] = None,
) -> None:
    """Set/replace *updates* (and delete *remove*) in an env file in place.

    Preserves comments, blank lines, ordering and any non ``KEY=VALUE`` lines —
    only the affected keys are rewritten. Existing keys are updated where they
    already appear; brand-new keys are appended. Creates the file (and parent
    dirs) if it does not exist.
    """
    remove_set = {str(k) for k in (remove or ())}
    updates = {str(k): str(v) for k, v in updates.items()}

    existing_lines = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    written_keys = set()
    out_lines = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remove_set:
                continue
            if key in updates:
                out_lines.append(f"{key}={updates[key]}")
                written_keys.add(key)
                continue
        out_lines.append(line)

    for key, value in updates.items():
        if key not in written_keys:
            out_lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


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
