"""
Environment file loader utility for ilha.

This module provides utilities for loading environment variables from .env files.
"""

from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional
from .logging import log_warning


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

    Many modules used to hand-roll this (some stripping quotes, some not),
    which meant the same .env line could parse differently depending on the
    code path. Route all KEY=VALUE parsing through here.
    """
    env_vars: Dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if not key:
            continue
        env_vars[key] = _strip_inline_quotes(value.strip())
    return env_vars


def load_env_file(env_path: Path) -> Dict[str, str]:
    """Load and parse .env file.
    
    Parses a .env file with the following rules:
    - Lines starting with # are comments (ignored)
    - Empty lines are ignored
    - Lines with format KEY=VALUE are parsed
    - Whitespace around keys and values is stripped
    - Lines without = are ignored
    
    Args:
        env_path: Path to .env file
        
    Returns:
        Dictionary of key-value pairs from .env file, or empty dict if file
        doesn't exist or parsing fails
    """
    if not env_path.exists():
        return {}

    try:
        content = env_path.read_text(encoding='utf-8')
    except (IOError, OSError) as e:
        log_warning(f"Failed to read .env file at {env_path}: {e}")
        return {}

    try:
        return parse_env_content(content)
    except Exception as e:
        log_warning(f"Error parsing .env file at {env_path}: {e}")
        return {}


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

    Single source of truth for the "read → strip old keys → append → write"
    dance that was re-implemented across EnvironmentManager (save_push_config,
    save_droplet_config, set_staging_certificate_flag, update_project_root, …) —
    several of which destroyed comments by rewriting from a parsed dict. Route
    env-file mutation through here.
    """
    remove_set = {str(k) for k in (remove or ())}
    updates = {str(k): str(v) for k, v in updates.items()}

    existing_lines = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding='utf-8').splitlines()

    written_keys = set()
    out_lines = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.split('=', 1)[0].strip()
            if key in remove_set:
                continue  # drop this key entirely
            if key in updates:
                out_lines.append(f"{key}={updates[key]}")
                written_keys.add(key)
                continue
        out_lines.append(line)

    # Append any keys that weren't already present.
    for key, value in updates.items():
        if key not in written_keys:
            out_lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(out_lines) + "\n", encoding='utf-8')


def load_env_from_project_root() -> Dict[str, str]:
    """Load .env file from project root directory.
    
    Convenience function that finds the project root and loads the .env file
    from there.
    
    Returns:
        Dictionary of key-value pairs from project root .env file, or empty
        dict if file doesn't exist or project root can't be determined
    """
    try:
        from ..config.settings import get_project_root
        project_root = get_project_root()
        env_path = project_root / ".env"
        return load_env_file(env_path)
    except Exception as e:
        log_warning(f"Failed to load .env file from project root: {e}")
        return {}


def load_env_from_home() -> Dict[str, str]:
    """Load .env file from user's home directory.
    
    Checks ~/.ilha/env.ilha for global ilha configuration.
    This allows users to set global settings that apply across all projects.
    
    Returns:
        Dictionary of key-value pairs from global env file, or empty
        dict if file doesn't exist
    """
    try:
        home_dir = Path.home()
        env_path = home_dir / ".ilha" / "env.ilha"
        return load_env_file(env_path)
    except Exception as e:
        log_warning(f"Failed to load .env file from home directory: {e}")
        return {}

