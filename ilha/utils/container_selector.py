"""
Container selection utility for ilha CLI.

This module provides functionality to parse and validate container selection
syntax like 'worktree.container' for selective push operations.
"""

from typing import List, Dict, Optional, Set
from pathlib import Path
import yaml

from ..core.git_manager import GitManager
from ..utils.logging import log_error
from ..config.settings import get_project_root


def parse_container_selection(
    selection_string: str,
    project_root: Optional[Path] = None
) -> List[Dict[str, str]]:
    """Parse container selection string into structured data.
    
    Args:
        selection_string: Comma-separated list of 'worktree.container' patterns
                         Example: "feature-auth.db,feature-auth.redis"
        project_root: Project root directory. If None, uses get_project_root().
        
    Returns:
        List of dictionaries with 'worktree' and 'container' keys
        
    Raises:
        ValueError: If syntax is invalid or worktree/container doesn't exist
    """
    if project_root is None:
        project_root = get_project_root()
    
    if not selection_string or not selection_string.strip():
        raise ValueError("Container selection string cannot be empty")
    
    # Split by comma and parse each selection
    selections = []
    parts = [p.strip() for p in selection_string.split(',')]
    
    for part in parts:
        if not part:
            continue
            
        # Validate format: worktree.container
        if '.' not in part:
            raise ValueError(
                f"Invalid container selection format: '{part}'. "
                f"Expected format: 'worktree.container'"
            )
        
        # Split into worktree and container
        split_parts = part.split('.', 1)
        if len(split_parts) != 2:
            raise ValueError(
                f"Invalid container selection format: '{part}'. "
                f"Expected format: 'worktree.container'"
            )
        
        worktree_name = split_parts[0].strip()
        container_name = split_parts[1].strip()
        
        if not worktree_name:
            raise ValueError(f"Worktree name cannot be empty in selection: '{part}'")
        if not container_name:
            raise ValueError(f"Container name cannot be empty in selection: '{part}'")
        
        # Validate worktree exists
        git_manager = GitManager(project_root=project_root, validate=False)
        if not git_manager.validate_worktree_exists(worktree_name):
            raise ValueError(
                f"Worktree '{worktree_name}' does not exist. "
                f"Available worktrees: {', '.join(git_manager.list_worktrees())}"
            )
        
        # Validate container/service exists in docker-compose.yml
        worktree_path = git_manager.find_worktree_path(worktree_name)
        if not worktree_path:
            raise ValueError(f"Could not find worktree path for '{worktree_name}'")
        
        # Check docker-compose.yml for the service
        compose_file = worktree_path / "docker-compose.yml"
        if not compose_file.exists():
            # Try worktree-specific compose file
            compose_file = worktree_path / ".ilha" / "docker-compose.worktree.yml"
        
        if not compose_file.exists():
            raise ValueError(
                f"No docker-compose.yml found for worktree '{worktree_name}'"
            )
        
        # Load compose file and check if service exists
        try:
            with open(compose_file) as f:
                compose_data = yaml.safe_load(f) or {}
        except Exception as e:
            raise ValueError(
                f"Failed to parse docker-compose.yml for worktree '{worktree_name}': {e}"
            )
        
        services = compose_data.get('services', {})
        if container_name not in services:
            available_services = ', '.join(services.keys()) if services else 'none'
            raise ValueError(
                f"Service '{container_name}' not found in worktree '{worktree_name}'. "
                f"Available services: {available_services}"
            )
        
        selections.append({
            'worktree': worktree_name,
            'container': container_name
        })
    
    if not selections:
        raise ValueError("No valid container selections found")
    
    return selections


def validate_container_selections(
    selections: List[Dict[str, str]],
    project_root: Optional[Path] = None
) -> bool:
    """Validate a list of container selections.
    
    Args:
        selections: List of dictionaries with 'worktree' and 'container' keys
        project_root: Project root directory. If None, uses get_project_root().
        
    Returns:
        True if all selections are valid
        
    Raises:
        ValueError: If any selection is invalid
    """
    if project_root is None:
        project_root = get_project_root()
    
    git_manager = GitManager(project_root=project_root, validate=False)
    
    for selection in selections:
        worktree_name = selection.get('worktree')
        container_name = selection.get('container')
        
        if not worktree_name or not container_name:
            raise ValueError("Each selection must have 'worktree' and 'container' keys")
        
        # Validate worktree exists
        if not git_manager.validate_worktree_exists(worktree_name):
            raise ValueError(f"Worktree '{worktree_name}' does not exist")
        
        # Validate container exists
        worktree_path = git_manager.find_worktree_path(worktree_name)
        if not worktree_path:
            raise ValueError(f"Could not find worktree path for '{worktree_name}'")
        
        compose_file = worktree_path / "docker-compose.yml"
        if not compose_file.exists():
            compose_file = worktree_path / ".ilha" / "docker-compose.worktree.yml"
        
        if not compose_file.exists():
            raise ValueError(f"No docker-compose.yml found for worktree '{worktree_name}'")
        
        try:
            with open(compose_file) as f:
                compose_data = yaml.safe_load(f) or {}
        except Exception as e:
            raise ValueError(
                f"Failed to parse docker-compose.yml for worktree '{worktree_name}': {e}"
            )
        
        services = compose_data.get('services', {})
        if container_name not in services:
            raise ValueError(
                f"Service '{container_name}' not found in worktree '{worktree_name}'"
            )
    
    return True


def resolve_service_dependencies(
    compose_data: Dict,
    service_names: List[str],
    exclude_services: Optional[List[str]] = None,
    project_root: Optional[Path] = None
) -> List[str]:
    """Resolve service dependencies for selected services.
    
    Args:
        compose_data: Parsed docker-compose.yml data
        service_names: List of service names to resolve dependencies for
        exclude_services: Optional list of service names to exclude from dependency resolution
        project_root: Project root directory (unused, kept for API consistency)
        
    Returns:
        List of service names including dependencies (excluding specified services)
    """
    services = compose_data.get('services', {})
    if not services:
        return service_names
    
    exclude_set = set(exclude_services or [])
    
    # Track services we need to include
    services_to_include = set(service_names)
    # Track services we've already processed
    processed = set()
    
    def resolve_deps(service_name: str):
        """Recursively resolve dependencies for a service."""
        if service_name in processed or service_name not in services:
            return
        
        processed.add(service_name)
        service_config = services[service_name]
        
        # Check depends_on
        depends_on = service_config.get('depends_on', [])
        if isinstance(depends_on, list):
            for dep in depends_on:
                if isinstance(dep, str):
                    dep_name = dep
                elif isinstance(dep, dict):
                    dep_name = dep.get('service') or list(dep.keys())[0] if dep else None
                else:
                    continue
                
                # Skip excluded services
                if dep_name and dep_name in exclude_set:
                    continue
                
                if dep_name and dep_name in services:
                    services_to_include.add(dep_name)
                    resolve_deps(dep_name)
        elif isinstance(depends_on, dict):
            for dep_name in depends_on.keys():
                # Skip excluded services
                if dep_name in exclude_set:
                    continue
                    
                if dep_name in services:
                    services_to_include.add(dep_name)
                    resolve_deps(dep_name)
    
    # Resolve dependencies for all selected services
    for service_name in service_names:
        resolve_deps(service_name)
    
    return sorted(list(services_to_include))

