"""
File utilities for ilha CLI.

This module provides utilities for file operations including .gitignore management.
"""

import os
import stat
import yaml
from pathlib import Path
from typing import Optional, Dict, Any, List

from .logging import log_info, log_success, log_warning, log_error


def prompt_user_input(prompt: str, default: Optional[str] = None) -> str:
    """Prompt user for input with optional default value."""
    if default:
        full_prompt = f"{prompt} [{default}]: "
    else:
        full_prompt = f"{prompt}: "
    
    try:
        response = input(full_prompt).strip()
        return response if response else (default or "")
    except (KeyboardInterrupt, EOFError):
        log_warning("Input cancelled by user")
        return ""


def prompt_yes_no(prompt: str, default: bool = True) -> bool:
    """Prompt user for yes/no input with default value."""
    default_str = "Y/n" if default else "y/N"
    full_prompt = f"{prompt} [{default_str}]: "
    
    try:
        response = input(full_prompt).strip().lower()
        if not response:
            return default
        return response in ['y', 'yes', '1', 'true']
    except (KeyboardInterrupt, EOFError):
        log_warning("Input cancelled by user")
        return default


def read_gitignore_file(project_root: Path) -> Optional[list]:
    """Read .gitignore file and return lines as list."""
    gitignore_path = project_root / ".gitignore"
    
    if not gitignore_path.exists():
        return None
    
    try:
        with open(gitignore_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return lines
    except Exception as e:
        log_error(f"Failed to read .gitignore file: {e}")
        return None


def write_gitignore_file(project_root: Path, lines: list) -> bool:
    """Write lines to .gitignore file."""
    gitignore_path = project_root / ".gitignore"
    
    try:
        with open(gitignore_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        return True
    except Exception as e:
        log_error(f"Failed to write .gitignore file: {e}")
        return False


def add_to_gitignore(project_root: Path, entry: str) -> bool:
    """Add entry to .gitignore file if not already present."""
    lines = read_gitignore_file(project_root)
    
    if lines is None:
        # Create new .gitignore file
        lines = []
    
    # Check if entry already exists
    entry_stripped = entry.strip()
    for line in lines:
        if line.strip() == entry_stripped:
            log_info(f"Entry '{entry_stripped}' already exists in .gitignore")
            return True
    
    # Add ilha comment header if not already present anywhere in file
    ilha_comment_exists = any(line.strip() == '# ilha' for line in lines)
    if not ilha_comment_exists:
        lines.append('# ilha\n')
    
    # Add entry
    if not entry_stripped.endswith('\n'):
        entry_stripped += '\n'
    
    lines.append(entry_stripped)
    
    # Write back to file
    if write_gitignore_file(project_root, lines):
        log_success(f"Added '{entry_stripped.strip()}' to .gitignore")
        return True
    else:
        log_error(f"Failed to add '{entry_stripped.strip()}' to .gitignore")
        return False


def check_gitignore_entry(project_root: Path, entry: str) -> bool:
    """Check if entry exists in .gitignore file."""
    lines = read_gitignore_file(project_root)
    
    if lines is None:
        return False
    
    entry_stripped = entry.strip()
    for line in lines:
        if line.strip() == entry_stripped:
            return True
    
    return False


def replace_gitignore_entry(project_root: Path, old_entry: str, new_entry: str) -> bool:
    """Replace an entry in .gitignore file with a new entry."""
    lines = read_gitignore_file(project_root)
    
    if lines is None:
        # No .gitignore file, just add new entry
        return add_to_gitignore(project_root, new_entry)
    
    old_entry_stripped = old_entry.strip()
    new_entry_stripped = new_entry.strip()
    replaced = False
    
    # Replace old entry with new entry
    for i, line in enumerate(lines):
        if line.strip() == old_entry_stripped:
            lines[i] = new_entry_stripped + '\n'
            replaced = True
            log_info(f"Replaced '{old_entry_stripped}' with '{new_entry_stripped}' in .gitignore")
            break
    
    # If old entry wasn't found, add new entry
    if not replaced:
        if new_entry_stripped not in [line.strip() for line in lines]:
            lines.append(new_entry_stripped + '\n')
            log_info(f"Added '{new_entry_stripped}' to .gitignore")
        else:
            log_info(f"Entry '{new_entry_stripped}' already exists in .gitignore")
            return True
    
    # Write back to file
    if write_gitignore_file(project_root, lines):
        log_success("Updated .gitignore successfully")
        return True
    else:
        log_error("Failed to update .gitignore")
        return False


def remove_gitignore_entry(project_root: Path, entry: str) -> bool:
    """Remove entry from .gitignore file."""
    lines = read_gitignore_file(project_root)
    
    if lines is None:
        return True  # No .gitignore file, nothing to remove
    
    entry_stripped = entry.strip()
    original_count = len(lines)
    
    # Filter out matching lines
    lines = [line for line in lines if line.strip() != entry_stripped]
    
    if len(lines) == original_count:
        # Entry wasn't found
        return True
    
    # Write back to file
    if write_gitignore_file(project_root, lines):
        log_info(f"Removed '{entry_stripped}' from .gitignore")
        return True
    else:
        log_error(f"Failed to remove '{entry_stripped}' from .gitignore")
        return False


def _add_to_ignore_file(filepath: Path, entry: str, file_label: str) -> bool:
    """Add entry to an ignore file if not already present.

    Shared implementation for .gitignore, .cursorignore, .cursorindexignore, etc.
    """
    try:
        if filepath.exists():
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        else:
            lines = []
    except Exception as e:
        log_error(f"Failed to read {file_label} file: {e}")
        return False

    entry_stripped = entry.strip()
    for line in lines:
        if line.strip() == entry_stripped:
            log_info(f"Entry '{entry_stripped}' already exists in {file_label}")
            return True

    if not any(line.strip() == '# ilha' for line in lines):
        lines.append('# ilha\n')

    lines.append(entry_stripped + '\n' if not entry_stripped.endswith('\n') else entry_stripped)

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        log_success(f"Added '{entry_stripped.strip()}' to {file_label}")
        return True
    except Exception as e:
        log_error(f"Failed to add '{entry_stripped.strip()}' to {file_label}: {e}")
        return False


def add_to_cursorignore(project_root: Path, entry: str) -> bool:
    """Add entry to .cursorignore file if not already present."""
    return _add_to_ignore_file(project_root / ".cursorignore", entry, ".cursorignore")


def add_to_cursorindexignore(project_root: Path, entry: str) -> bool:
    """Add entry to .cursorindexignore file if not already present."""
    return _add_to_ignore_file(project_root / ".cursorindexignore", entry, ".cursorindexignore")


def find_compose_files(project_root: Path) -> list[Path]:
    """Find all compose files in priority order."""
    # Check in order: compose.yml, compose.yaml, docker-compose.yml, docker-compose.yaml
    candidates = [
        'compose.yml',
        'compose.yaml', 
        'docker-compose.yml',
        'docker-compose.yaml'
    ]
    return [project_root / name for name in candidates if (project_root / name).exists()]


def prompt_compose_file_choice(compose_files: list[Path]) -> Optional[Path]:
    """Prompt user to choose from multiple compose files."""
    if not compose_files:
        return None
    
    if len(compose_files) == 1:
        return compose_files[0]
    
    log_info("Found multiple Docker Compose files:")
    for i, compose_file in enumerate(compose_files, 1):
        log_info(f"  {i}) {compose_file.name}")
    
    # Default to first file (highest priority)
    default_choice = "1"
    choice = prompt_user_input("Which file would you like to use?", default_choice)
    
    try:
        choice_index = int(choice) - 1
        if 0 <= choice_index < len(compose_files):
            selected_file = compose_files[choice_index]
            log_info(f"Selected: {selected_file.name}")
            return selected_file
        else:
            log_error(f"Invalid choice: {choice}")
            return None
    except ValueError:
        log_error(f"Invalid choice: {choice}")
        return None


def clean_compose_version_field(compose_data: Dict[str, Any]) -> None:
    """Remove version field from compose data.
    
    Docker Compose v2 doesn't require the version field, so it should be
    removed to prevent validation warnings and confusion.
    
    Args:
        compose_data: Docker Compose data dictionary (modified in place)
    """
    if 'version' in compose_data:
        compose_data.pop('version', None)


# Script names under /app that are commonly used as container entrypoints
_ENTRYPOINT_SCRIPT_NAMES = ("entrypoint.sh", "endpoint.sh")


def _volumes_mount_app(volumes: Any) -> List[str]:
    """Return relative paths (under PROJECT_ROOT) for volumes that mount to /app.
    
    Handles list of strings like '${PROJECT_ROOT}:/app' or '${PROJECT_ROOT}/ambulancia/ambulancia:/app'.
    Returns the source path suffix after ${PROJECT_ROOT}/, or '' for ${PROJECT_ROOT}:/app.
    """
    if not volumes:
        return []
    paths: List[str] = []
    for vol in volumes if isinstance(volumes, list) else []:
        if not isinstance(vol, str) or ":/app" not in vol:
            continue
        # Format is "source:target"; target must be exactly /app (not /app/...)
        parts = vol.split(":", 1)
        if len(parts) != 2 or parts[1].rstrip("/") != "/app":
            continue
        source = parts[0].strip()
        if not source or "${PROJECT_ROOT}" not in source:
            continue
        # Normalize: ${PROJECT_ROOT} or ${PROJECT_ROOT}/path
        suffix = source.replace("${PROJECT_ROOT}", "").strip("/")
        if suffix not in paths:
            paths.append(suffix)
    return paths


def ensure_entrypoint_executable(compose_path: Path, project_root: Path) -> None:
    """Ensure entrypoint scripts under paths mounted to /app are executable on the host.
    
    Parses the compose file, finds services with volume mounts to /app, and runs
    chmod +x on entrypoint.sh (and endpoint.sh) under those paths so container
    entrypoints work when the host directory is mounted over /app.
    
    Args:
        compose_path: Path to docker-compose.overlay.yml (or similar).
        project_root: Resolved directory used as PROJECT_ROOT when running compose.
    """
    if not compose_path.exists():
        return
    project_root = Path(project_root).resolve()
    try:
        with open(compose_path, encoding="utf-8") as f:
            compose_data = yaml.safe_load(f)
    except Exception:
        return
    if not compose_data or "services" not in compose_data:
        return
    seen: set = set()
    for service_config in compose_data.get("services", {}).values():
        if not isinstance(service_config, dict):
            continue
        volumes = service_config.get("volumes") or []
        for rel in _volumes_mount_app(volumes):
            dir_path = project_root / rel if rel else project_root
            for name in _ENTRYPOINT_SCRIPT_NAMES:
                script = dir_path / name
                if script in seen or not script.is_file():
                    continue
                seen.add(script)
                try:
                    mode = script.stat().st_mode
                    if not (mode & stat.S_IXUSR):
                        script.chmod(mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
                        log_info(f"Set executable bit on {script} (mounted at /app)")
                except OSError:
                    pass
