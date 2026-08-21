"""
Configuration settings for ilha CLI.

This module contains all configuration constants and settings used throughout
the ilha CLI application.
"""

import hashlib
import yaml
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# Version information
VERSION = "0.9.10"
AUTHOR = "ilha Contributors"

# Default project configuration (used if .ilha/config.yml doesn't exist)
# PROJECT_NAME is now dynamically determined from get_project_name()
CADDY_NETWORK = "ilha_caddy_proxy"
ILHA_DIR = ".ilha"

# Volume names
POSTGRES_VOLUME_SUFFIX = "postgres_data"
REDIS_VOLUME_SUFFIX = "redis_data"
MEDIA_VOLUME_SUFFIX = "media_files"
SQLITE_VOLUME_SUFFIX = "sqlite_data"


def get_volume_names_for_project_root(project_root: Path, branch_name: str) -> dict[str, str]:
    """Get volume names for a worktree using a specific project root (not cwd).
    Used when restoring volumes so the correct project name is used (e.g. standalone import).
    """
    project_name = None
    config_path = Path(project_root) / ".ilha" / "config.yml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
                project_name = config.get("project_name")
        except Exception:
            pass
    if not project_name:
        project_name = Path(project_root).name
    project_name = sanitize_project_name(project_name)
    prefix = f"{project_name}-{sanitize_project_name(branch_name)}_"
    return {
        "postgres": prefix + POSTGRES_VOLUME_SUFFIX,
        "redis": prefix + REDIS_VOLUME_SUFFIX,
        "media": prefix + MEDIA_VOLUME_SUFFIX,
        "sqlite": prefix + SQLITE_VOLUME_SUFFIX,
    }
CADDY_DATA_VOLUME_SUFFIX = "caddy_data"
CADDY_CONFIG_VOLUME_SUFFIX = "caddy_config"

# Protected branches that cannot be deleted
PROTECTED_BRANCHES = {"main", "master",}

# Branch name validation pattern
BRANCH_NAME_PATTERN = r"^[a-zA-Z0-9_.-]+$"

# Publicly-known placeholder. A deploy that ships this as DJANGO_SECRET_KEY lets
# anyone forge session cookies / CSRF tokens, so it must never reach production —
# generate a unique key per environment with generate_secret_key() instead.
INSECURE_DEFAULT_SECRET_KEY = "django-insecure-secret-key"

# Default environment variables (legacy defaults, overridden by config)
DEFAULT_ENV_VARS = {
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "password",
    "POSTGRES_DB": "database",
    "ALLOWED_HOSTS": "localhost,127.0.0.1,*.localhost,web",
    "DEBUG": "True",
    "DJANGO_SECRET_KEY": INSECURE_DEFAULT_SECRET_KEY,
    "SITE_DOMAIN": "localhost:8000",
    "CADDY_EMAIL": "admin@example.com",
}


def generate_secret_key() -> str:
    """Return a cryptographically strong, unique Django SECRET_KEY.

    Used when generating a new environment so each one gets its own key and no
    deployment ever ships the shared, publicly-known insecure default.
    """
    import secrets
    return secrets.token_urlsafe(64)

# Deployment defaults helpers (Phase 2)
def _get_config_value(path_keys: list[str], default: Optional[str] = None) -> Optional[str]:
    """Safely read a nested value from .ilha/config.yml.

    Args:
        path_keys: List of nested keys to traverse
        default: Default value when not found

    Returns:
        String value or default
    """
    try:
        config_path = get_project_root() / ILHA_DIR / "config.yml"
        if not config_path.exists():
            return default
        import yaml  # local import to avoid top-level dependency at import time
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        node: Any = cfg
        for key in path_keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        if isinstance(node, (str, int, float)):
            return str(node)
        return default
    except Exception:
        return default


def get_deployment_defaults() -> Dict[str, Optional[str]]:
    """Get deployment default values from config if present.

    Keys:
      - default_server
      - default_domain
      - default_ip
      - ssh_key
    """
    return {
        "default_server": _get_config_value(["deployment", "default_server"], None),
        "default_domain": _get_config_value(["deployment", "default_domain"], None),
        "default_ip": _get_config_value(["deployment", "default_ip"], None),
        "ssh_key": _get_config_value(["deployment", "ssh_key"], None),
    }


def get_default_server() -> Optional[str]:
    return get_deployment_defaults().get("default_server")


def get_default_domain() -> Optional[str]:
    return get_deployment_defaults().get("default_domain")


def get_default_ip() -> Optional[str]:
    return get_deployment_defaults().get("default_ip")


def get_deployment_ssh_key() -> Optional[str]:
    return get_deployment_defaults().get("ssh_key")

# Configuration loading functions
def get_project_config() -> Dict[str, Any]:
    """Load project configuration from .ilha/config.yml"""
    config_path = get_project_root() / ILHA_DIR / "config.yml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return get_default_config()

def get_default_config() -> Dict[str, Any]:
    """Default configuration for projects without setup"""
    return {
        "project_name": get_project_root().name,
        "caddy_network": "ilha_caddy_proxy",
        "worktree_dir": "worktrees",
        "services": {
            "web": {"container_name_template": "${COMPOSE_PROJECT_NAME}-web"},
            "db": {"container_name_template": "${COMPOSE_PROJECT_NAME}-db"},
            "redis": {"container_name_template": "${COMPOSE_PROJECT_NAME}-redis"},
        },
        "volumes": ["postgres_data", "redis_data", "media_files"],
        "environment": {
            "DEBUG": "True",
            "ALLOWED_HOSTS": "localhost,127.0.0.1,*.localhost,web",
        }
    }

def get_project_name() -> str:
    """Get project name from config or fallback to directory name"""
    config = get_project_config()
    return config.get("project_name", get_project_root().name)

def sanitize_project_name(name: str) -> str:
    """Sanitize project name for use in Docker resource names.
    
    Converts underscores to hyphens for consistency with hostname requirements.
    While Docker allows underscores, using hyphens everywhere ensures
    consistency between resource names and hostnames.
    """
    import re
    # Replace underscores with hyphens for consistency
    sanitized = name.replace('_', '-')
    # Replace other non-alphanumeric (except dash) with dash
    sanitized = re.sub(r'[^a-zA-Z0-9-]', '-', sanitized)
    # Remove leading/trailing dashes
    sanitized = sanitized.strip('-')
    # Convert to lowercase for consistency
    return sanitized.lower()

def sanitize_hostname(name: str) -> str:
    """Sanitize a name for use in hostnames (RFC 1034/1035 compliant).

    The transformation is identical to :func:`sanitize_project_name` (both map
    to the same lowercase, dash-only charset), so this is a thin alias — keeping
    one implementation avoids the two silently diverging.
    """
    return sanitize_project_name(name)

def get_worktree_dir() -> str:
    """Get worktree directory from config"""
    config = get_project_config()
    return config.get("worktree_dir", "worktrees")


def resolve_project_name(project_root: Path) -> str:
    """Resolve a project's identity from its `.ilha/config.yml`, then directory name.

    Single source of truth for "what is this project's name?" — used by setup,
    env generation, and worktree orchestration so all sites agree.

    Returns an unsanitized string; callers that need Docker-safe names should
    pipe through ``sanitize_project_name``.
    """
    config_path = Path(project_root) / ILHA_DIR / "config.yml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            name = config.get("project_name")
            if name:
                return str(name)
        except Exception:
            pass
    return Path(project_root).name


def resolve_default_branch(project_root: Path) -> str:
    """Resolve the default branch for a project root.

    Order: ``origin/HEAD`` symbolic ref → current local branch → ``"master"``.
    Used to pick a sensible default for env.ilha generation at the project root,
    where there is no checked-out worktree to read a branch from.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        ref = result.stdout.strip()
        if ref.startswith("refs/remotes/origin/"):
            return ref[len("refs/remotes/origin/"):]
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "branch", "--show-current"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        branch = result.stdout.strip()
        if branch:
            return branch
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return "master"

# Docker Compose command detection
def get_compose_command() -> str:
    """Get the appropriate docker compose command.

    Probes are bounded by a timeout so a wedged Docker daemon cannot hang the
    CLI indefinitely (an unbounded ``docker compose version`` can block).
    """
    import subprocess
    try:
        subprocess.run(["docker", "compose", "version"],
                      capture_output=True, check=True, timeout=10)
        return "docker compose"
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        try:
            subprocess.run(["docker-compose", "version"],
                          capture_output=True, check=True, timeout=10)
            return "docker-compose"
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            # Fallback to docker-compose if both fail
            return "docker-compose"

# Path resolution
def get_project_root() -> Path:
    """Get the project root directory, supporting fractal worktree execution."""
    current = Path.cwd()
    
    # First: Check if we're IN a worktree with its own .ilha/config.yml (fractal mode)
    if (current / ".ilha" / "config.yml").exists():
        return current
    
    # Second: Search upward to find parent project root
    for parent in current.parents:
        if (parent / ".ilha" / "config.yml").exists():
            return parent
    
    # Fallback: check for .ilha directory without config.yml (legacy)
    if (current / ".ilha").exists() and (current / ".ilha").is_dir():
        return current
        
    for parent in current.parents:
        if (parent / ".ilha").exists() and (parent / ".ilha").is_dir():
            return parent
    
    # Final fallback
    return current

def get_script_dir() -> Path:
    """Get the ilha-cli script directory."""
    return Path(__file__).parent.parent.absolute()

def get_package_caddyfile_path() -> Path:
    """Get the path to the package Caddyfile template."""
    return get_script_dir() / "config" / "Caddyfile.ilha"

def get_package_caddy_compose_path() -> Path:
    """Get the path to the package Caddy compose template."""
    return get_script_dir() / "config" / "docker-compose.global-caddy.yml"

def get_worktree_paths(branch_name: str) -> tuple[Path, Path]:
    """Get worktree paths for a branch (new and legacy)."""
    repo_root = get_project_root()
    worktree_dir = get_worktree_dir()
    new_path = repo_root / worktree_dir / branch_name
    legacy_path = repo_root.parent / branch_name
    return new_path, legacy_path

def get_container_name_for_worktree(branch_name: str) -> str:
    """Get the container name for a worktree's web service.
    
    Container name format: {project-name}-{branch}-web
    
    Args:
        branch_name: Branch name for the worktree
        
    Returns:
        Container name string
        
    Example:
        For project 'business_intelligence' and branch 'test':
        Returns: 'business-intelligence-test-web'
    """
    project_name = sanitize_project_name(get_project_name())
    return f"{project_name}-{branch_name}-web"

def build_allowed_hosts_with_container(branch_name: str, additional_hosts: Optional[List[str]] = None) -> str:
    """Build ALLOWED_HOSTS string with container name included.
    
    This is a DRY helper function that ensures container names are always
    included in ALLOWED_HOSTS for Caddy proxy routing.
    
    Args:
        branch_name: Branch name for the worktree
        additional_hosts: Optional list of additional hostnames to include
        
    Returns:
        Comma-separated ALLOWED_HOSTS string with format:
        localhost,127.0.0.1,[additional_hosts...],{container-name},web
        
    Example:
        build_allowed_hosts_with_container('test', ['example.com', '*.example.com'])
        Returns: 'localhost,127.0.0.1,example.com,*.example.com,business-intelligence-test-web,web'
    """
    container_name = get_container_name_for_worktree(branch_name)
    
    # Base hosts always included
    hosts = ["localhost", "127.0.0.1"]
    
    # Add additional hosts if provided
    if additional_hosts:
        hosts.extend(additional_hosts)
    
    # Always append container name and 'web' for inter-container communication
    hosts.extend([container_name, "web"])
    
    return ",".join(hosts)

def get_allowed_hosts_for_worktree(branch_name: str) -> str:
    """Generate ALLOWED_HOSTS string for a worktree with explicit subdomain.
    
    Returns a comma-separated string with:
    - localhost, 127.0.0.1 for local access
    - {project-name}-{branch}.localhost for the specific worktree subdomain
    - *.localhost as a wildcard fallback
    - {container-name} for Caddy proxy routing
    - web for inter-container communication
    
    Args:
        branch_name: Branch name for the worktree
        
    Returns:
        Comma-separated ALLOWED_HOSTS string
        
    Example:
        For project 'business_intelligence' and branch 'beta':
        Returns: 'localhost,127.0.0.1,business-intelligence-beta.localhost,*.localhost,business-intelligence-beta-web,web'
    """
    project_name = sanitize_project_name(get_project_name())
    subdomain = f"{project_name}-{branch_name}.localhost"
    return build_allowed_hosts_with_container(branch_name, [subdomain, "*.localhost"])

# Helper function to extract domain from SITE_DOMAIN (DRY)
def extract_domain_from_site_domain(site_domain: str) -> str:
    """Extract domain from SITE_DOMAIN by removing protocol.
    
    Args:
        site_domain: SITE_DOMAIN value (e.g., 'http://boards-test-local.localhost' or 'https://app.example.com')
        
    Returns:
        Domain without protocol (e.g., 'boards-test-local.localhost' or 'app.example.com')
    """
    domain = site_domain
    # Remove protocol if present
    if domain.startswith('https://'):
        domain = domain[8:]
    elif domain.startswith('http://'):
        domain = domain[7:]
    # Remove trailing slash if present
    domain = domain.rstrip('/')
    return domain


def get_worktree_network_subnets(branch_name: str) -> Tuple[str, str]:
    """Return unique /24 subnets for a worktree (internal, web) to avoid pool overlap.

    Uses a deterministic hash of the compose project name so each worktree
    gets distinct subnets. Overlay compose can reference
    ``${ILHA_SUBNET_INTERNAL}`` / ``${ILHA_SUBNET_WEB}``.
    """
    project_name = sanitize_project_name(get_project_name())
    compose_project_name = f"{project_name}-{sanitize_project_name(branch_name)}"
    seed = int(hashlib.sha256(compose_project_name.encode()).hexdigest()[:8], 16)
    internal_octet = (seed % 127) * 2 + 1
    web_octet = internal_octet + 1
    return f"10.201.{internal_octet}.0/24", f"10.201.{web_octet}.0/24"


# Environment file generation
def generate_env_compose_content(
    branch_name: str,
    *,
    project_root: Optional[Path] = None,
    has_django: Optional[bool] = None,
) -> str:
    """Generate env.ilha content for a worktree.

    Single source of truth for non-domain env.ilha generation — both the
    project-root ``ilha setup`` path and per-worktree creation route here
    (EnvironmentManager delegates to this) so the two can't drift.

    Always emits ilha-owned plumbing: COMPOSE_PROJECT_NAME, PROJECT_ROOT and
    SITE_DOMAIN (bare host, used by Caddy labels). When Django is detected (or
    ``has_django`` is forced True), also emits Django-specific siblings
    (ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, USE_X_FORWARDED_HOST) derived from
    SITE_DOMAIN.

    Args:
        project_root: Root to resolve the project name / footprint against;
            defaults to ``get_project_root()``.
        has_django: Force the Django-sibling emission; when None, detect it
            against ``project_root``.
    """
    if project_root is None:
        project_root = get_project_root()
    project_name = sanitize_project_name(resolve_project_name(project_root))  # underscores -> hyphens
    compose_project_name = f"{project_name}-{sanitize_project_name(branch_name)}"
    site_domain = f"{compose_project_name}.localhost"  # bare host
    internal_subnet, web_subnet = get_worktree_network_subnets(branch_name)
    if has_django is None:
        from ..utils.project_footprint import detect_project_footprint
        has_django = detect_project_footprint(project_root).has_django

    content = f"""# ilha environment configuration for {branch_name}
COMPOSE_PROJECT_NAME={compose_project_name}
PROJECT_ROOT={project_root}
SITE_DOMAIN={site_domain}

# Per-worktree network subnets (avoid pool overlap; use in docker-compose overlay)
ILHA_SUBNET_INTERNAL={internal_subnet}
ILHA_SUBNET_WEB={web_subnet}
"""

    if has_django:
        allowed_hosts = get_allowed_hosts_for_worktree(branch_name)
        csrf_trusted_origins = f"http://{site_domain},https://{site_domain}"
        content += f"""
# Django-specific (emitted because Django was detected in this project)
ALLOWED_HOSTS={allowed_hosts}
CSRF_TRUSTED_ORIGINS={csrf_trusted_origins}
USE_X_FORWARDED_HOST=True
"""
    return content

# Volume naming
def get_volume_name(branch_name: str, volume_type: str) -> str:
    """Get worktree-specific volume name.
    Uses sanitized branch name so the volume name matches COMPOSE_PROJECT_NAME
    (e.g. business-intelligence-testdbcopy_postgres_data) and the worktree DB uses the copied data.
    """
    project_name = sanitize_project_name(get_project_name())
    return f"{project_name}-{sanitize_project_name(branch_name)}_{volume_type}"

def get_volume_names(branch_name: str) -> dict[str, str]:
    """Get all volume names for a worktree.
    
    Note: Caddy volumes (caddy_data, caddy_config) are intentionally excluded 
    as they are shared globally across all worktrees, not worktree-specific.
    """
    return {
        "postgres": get_volume_name(branch_name, POSTGRES_VOLUME_SUFFIX),
        "redis": get_volume_name(branch_name, REDIS_VOLUME_SUFFIX),
        "media": get_volume_name(branch_name, MEDIA_VOLUME_SUFFIX),
        "sqlite": get_volume_name(branch_name, SQLITE_VOLUME_SUFFIX),
    }

def get_source_volume_name(volume_type: str) -> str:
    """Get master/project root volume name using original (unsanitized) project name.
    
    These are the source volumes that worktree volumes are copied from.
    They use the original project name format (with underscores) as defined
    in docker-compose.yml.
    
    Args:
        volume_type: Volume type suffix (e.g., POSTGRES_VOLUME_SUFFIX)
        
    Returns:
        Volume name in format: {original_project_name}_{volume_type}
    """
    original_project_name = get_project_name()  # Unsanitized
    return f"{original_project_name}_{volume_type}"

def get_source_volume_names() -> dict[str, str]:
    """Get all master/project root volume names.
    
    Returns volume names for the project root (master branch) volumes
    that are used as sources when copying to new worktrees.
    Uses get_project_root() (cwd-based); for a specific path use get_source_volume_names_for_project_root().
    
    Returns:
        Dictionary mapping volume types to volume names:
        {
            "postgres": "{original_project_name}_postgres_data",
            "redis": "{original_project_name}_redis_data",
            "media": "{original_project_name}_media_files",
            "sqlite": "{original_project_name}_sqlite_data"
        }
    """
    return {
        "postgres": get_source_volume_name(POSTGRES_VOLUME_SUFFIX),
        "redis": get_source_volume_name(REDIS_VOLUME_SUFFIX),
        "media": get_source_volume_name(MEDIA_VOLUME_SUFFIX),
        "sqlite": get_source_volume_name(SQLITE_VOLUME_SUFFIX),
    }


def get_source_volume_names_for_project_root(project_root: Path) -> dict[str, str]:
    """Get master/project root volume names using a specific project root (not cwd).
    
    Use this when creating worktree volumes so the source is resolved from the
    same project root as the orchestrator (avoids wrong source when cwd differs).
    
    Args:
        project_root: Path to the project root (must contain .ilha/config.yml).
        
    Returns:
        Same structure as get_source_volume_names().
    """
    project_name = None
    config_path = Path(project_root) / ".ilha" / "config.yml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
                project_name = config.get("project_name")
        except Exception:
            pass
    if not project_name:
        project_name = Path(project_root).name
    prefix = f"{project_name}_"
    return {
        "postgres": prefix + POSTGRES_VOLUME_SUFFIX,
        "redis": prefix + REDIS_VOLUME_SUFFIX,
        "media": prefix + MEDIA_VOLUME_SUFFIX,
        "sqlite": prefix + SQLITE_VOLUME_SUFFIX,
    }
