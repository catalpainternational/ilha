"""
Environment file loader utility for ilha.

This module provides utilities for loading environment variables from .env files.
"""

from pathlib import Path
from typing import Dict
from .logging import log_warning


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
    env_vars = {}
    
    if not env_path.exists():
        return env_vars
    
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                # Strip whitespace
                line = line.strip()
                
                # Skip empty lines
                if not line:
                    continue
                
                # Skip comment lines
                if line.startswith('#'):
                    continue
                
                # Parse KEY=VALUE format
                if '=' in line:
                    # Split on first = to handle values that might contain =
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # Remove surrounding quotes from value if present
                    # Handle both single and double quotes
                    if len(value) >= 2:
                        if (value.startswith('"') and value.endswith('"')) or \
                           (value.startswith("'") and value.endswith("'")):
                            value = value[1:-1]
                    
                    # Skip if key is empty
                    if key:
                        env_vars[key] = value
                # Lines without = are silently ignored (not an error)
    
    except (IOError, OSError) as e:
        log_warning(f"Failed to read .env file at {env_path}: {e}")
        return {}
    except Exception as e:
        log_warning(f"Error parsing .env file at {env_path}: {e}")
        return {}
    
    return env_vars


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

