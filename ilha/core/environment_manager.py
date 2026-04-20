"""
Environment management for ilha CLI.

This module provides environment file generation, volume naming, and
configuration management for worktree environments.
"""

import os
import socket
from pathlib import Path
from typing import Dict, Optional, Set

from ..config.settings import (
    generate_env_compose_content,
    get_worktree_network_subnets,
    get_volume_names,
    DEFAULT_ENV_VARS,
    get_project_root,
    get_worktree_paths,
    get_worktree_dir,
)
from ..utils.logging import log_info, log_success, log_warning
from ..utils.path_utils import (
    get_env_file_path, 
    get_env_compose_file_path,
    copy_env_file
)
from ..utils.caddy_config import ensure_caddy_labels_and_network, update_allowed_hosts_in_compose, update_vite_allowed_hosts_in_compose
from ..utils.deploy_hosts import build_csrf_trusted_origins, normalize_deploy_host
from ..core.dns_manager import is_domain

HOST_PORT_RANGES: Dict[str, tuple[int, int]] = {
    "ilha_DB_HOST_PORT": (55432, 56431),
    "ilha_REDIS_HOST_PORT": (56379, 57378),
    "ilha_WEB_HOST_PORT": (58000, 58999),
}


class EnvironmentManager:
    """Manages environment configuration for ilha CLI."""
    
    def __init__(self, project_root: Optional[Path] = None):
        """Initialize environment manager.
        
        Args:
            project_root: Project root directory. If None, uses get_project_root().
        """
        # Use the provided project_root directly, don't fall back to get_project_root()
        if project_root is None:
            self.project_root = get_project_root()
        else:
            self.project_root = Path(project_root).resolve()

    def _build_host_port_section(self, branch_name: str) -> str:
        """Build host port assignments for env.ilha."""
        host_ports = self._calculate_host_ports(branch_name)
        if not host_ports:
            return ""
        lines = [f"{var}={value}" for var, value in host_ports.items()]
        return "\n".join(lines) + "\n"

    def _calculate_host_ports(self, branch_name: str) -> Dict[str, int]:
        """Assign deterministic host ports for key services."""
        used_ports = self._collect_used_host_ports()
        existing_ports = self._read_existing_host_ports(branch_name)

        # Reserve existing ports so we don't reassign them
        for var, value in existing_ports.items():
            used_ports.setdefault(var, set()).add(value)

        assigned: Dict[str, int] = {}
        for var, port_range in HOST_PORT_RANGES.items():
            if var in existing_ports:
                assigned[var] = existing_ports[var]
                continue

            allocated = self._allocate_host_port(var, used_ports.get(var, set()), port_range, branch_name)
            assigned[var] = allocated
            if allocated > 0:
                used_ports.setdefault(var, set()).add(allocated)

        return assigned

    def _collect_used_host_ports(self) -> Dict[str, Set[int]]:
        """Scan existing worktrees to understand which host ports are taken."""
        used: Dict[str, Set[int]] = {var: set() for var in HOST_PORT_RANGES}
        directories_to_scan = []

        worktree_root = self.project_root / get_worktree_dir()
        if worktree_root.exists():
            directories_to_scan.append(worktree_root)

        legacy_root = self.project_root.parent
        if legacy_root.exists():
            directories_to_scan.append(legacy_root)

        for root in directories_to_scan:
            for candidate in root.iterdir():
                if not candidate.is_dir():
                    continue
                env_file = candidate / ".ilha" / "env.ilha"
                env_ports = self._extract_host_ports(env_file)
                for var, value in env_ports.items():
                    used.setdefault(var, set()).add(value)

        return used

    def _read_existing_host_ports(self, branch_name: str) -> Dict[str, int]:
        """Read previously assigned host ports for a branch if env.ilha exists."""
        host_ports: Dict[str, int] = {}
        worktree_path = self.project_root / get_worktree_dir() / branch_name
        legacy_path = self.project_root.parent / branch_name

        env_path = get_env_compose_file_path(worktree_path)
        if not env_path.exists() and legacy_path.exists():
            env_path = get_env_compose_file_path(legacy_path)

        host_ports.update(self._extract_host_ports(env_path))
        return host_ports

    def _extract_host_ports(self, env_path: Path) -> Dict[str, int]:
        """Extract host port values from an env.ilha file."""
        from ..utils.env_loader import load_env_file

        if not env_path.exists():
            return {}

        try:
            env_vars = load_env_file(env_path)
        except Exception:
            return {}

        extracted: Dict[str, int] = {}
        for var in HOST_PORT_RANGES.keys():
            raw_value = env_vars.get(var)
            if raw_value is None:
                continue
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            extracted[var] = value
        return extracted

    def _allocate_host_port(
        self,
        var_name: str,
        used_ports: Set[int],
        port_range: tuple[int, int],
        branch_name: str,
    ) -> int:
        """Find the first available host port in the configured range."""
        start, end = port_range
        for port in range(start, end + 1):
            if port in used_ports:
                continue
            if self._is_port_available(port):
                log_info(f"Assigned {var_name}={port} for branch {branch_name}")
                return port

        log_warning(
            f"No free ports available in range {start}-{end} for {var_name}. "
            "Falling back to Docker auto-assignment."
        )
        return 0

    @staticmethod
    def _is_port_available(port: int) -> bool:
        """Check if a host port is available on the current machine."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", port))
            except OSError:
                return False
        return True
    
    def create_worktree_env(self, branch_name: str, worktree_path: Path, 
                           source_env_path: Optional[Path] = None,
                           domain: Optional[str] = None) -> bool:
        """Create worktree environment files.
        
        Args:
            branch_name: Branch name for the worktree
            worktree_path: Path to the worktree directory
            source_env_path: Optional source .env file path
            domain: Optional domain override (subdomain.domain.tld) for production/staging
            
        Returns:
            True if successful, False otherwise
        """
        log_info("Creating worktree environment files")
        
        # Ensure worktree directory exists
        worktree_path.mkdir(parents=True, exist_ok=True)
        
        # Copy .env file if it exists
        env_copied = False
        if source_env_path:
            env_copied = copy_env_file(source_env_path, worktree_path)
        else:
            # Try to find .env in project root
            env_copied = copy_env_file(self.project_root, worktree_path)
        
        if not env_copied:
            log_warning("No .env file found to copy, creating default .env file")
            # Create a default .env file if none exists
            if not self._create_default_env_file(worktree_path, branch_name, domain):
                log_warning("Failed to create default .env file")
        
        # Create env.ilha with worktree-specific settings
        # Ensure .ilha directory exists
        ilha_dir = worktree_path / ".ilha"
        ilha_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate environment content, applying domain overrides if provided
        if domain:
            env_compose_content = self._generate_env_compose_with_domain(branch_name, domain)
        else:
            env_compose_content = self._generate_env_compose_content(branch_name)
            
        env_compose_path = get_env_compose_file_path(worktree_path)
        
        try:
            env_compose_path.write_text(env_compose_content)
            
            # Apply domain overrides to .env file if domain is provided
            if domain:
                self.apply_domain_overrides(worktree_path, domain)
                
            log_success(f"Environment files created for {branch_name}")
            return True
        except Exception as e:
            log_warning(f"Failed to create environment file: {e}")
            return False
    
    def _create_default_env_file(self, worktree_path: Path, branch_name: str, 
                                 domain: Optional[str] = None) -> bool:
        """Create a default .env file if none exists.
        
        Args:
            worktree_path: Path to worktree directory
            branch_name: Branch name
            domain: Optional domain override (subdomain.domain.tld)
        """
        from ..config.settings import (
            ILHA_DIR,
            sanitize_project_name,
            get_allowed_hosts_for_worktree,
            build_allowed_hosts_with_container,
            DEFAULT_ENV_VARS,
        )
        import yaml
        
        env_file = worktree_path / ".env"
        
        # Don't overwrite existing .env file
        if env_file.exists():
            return True
        
        # Get project name from config in self.project_root (not current directory)
        project_name = None
        config_path = self.project_root / ILHA_DIR / "config.yml"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                    project_name = config.get("project_name")
            except Exception:
                pass
        
        # Fallback to project root directory name if config doesn't have project_name
        if not project_name:
            project_name = self.project_root.name
        
        # Create default environment content with project prefix
        project_name = sanitize_project_name(project_name)
        compose_project_name = f"{project_name}-{branch_name}"
        
        # Use domain override if provided, otherwise use localhost
        if domain:
            from ..core.dns_manager import get_base_domain
            normalized_domain = normalize_deploy_host(domain)
            base_domain = get_base_domain(normalized_domain)
            site_domain = normalized_domain
            allowed_hosts = build_allowed_hosts_with_container(
                branch_name,
                [normalized_domain, f"*.{base_domain}"] if base_domain else [normalized_domain],
            )
            csrf_trusted_origins = build_csrf_trusted_origins(normalized_domain, base_domain=base_domain)
        else:
            site_domain = f"{project_name}-{branch_name}.localhost"
            allowed_hosts = get_allowed_hosts_for_worktree(branch_name)
            csrf_trusted_origins = ""
        
        default_env_content = f"""# Default environment configuration for {branch_name}
# This file was automatically created by ilha

# Database configuration
# IMPORTANT: Set these values to match your PostgreSQL container configuration
# These values must match POSTGRES_USER, POSTGRES_PASSWORD, and POSTGRES_DB
# in your docker-compose.yml file
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_DB=

# Django configuration
DEBUG=True
DJANGO_SECRET_KEY=django-insecure-secret-key
ALLOWED_HOSTS={allowed_hosts}
SITE_DOMAIN={site_domain}
"""
        if csrf_trusted_origins:
            default_env_content += (
                "USE_X_FORWARDED_HOST=True\n"
                "SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https\n"
                f"CSRF_TRUSTED_ORIGINS={csrf_trusted_origins}\n"
            )
        default_env_content += f"""

# Redis configuration
REDIS_HOST={compose_project_name}-redis
REDIS_PORT=6379
REDIS_DB=0

# Caddy configuration
CADDY_EMAIL={DEFAULT_ENV_VARS['CADDY_EMAIL']}
"""
        
        try:
            env_file.write_text(default_env_content)
            log_info(f"Created default .env file for {branch_name}")
            return True
        except Exception as e:
            log_warning(f"Failed to create default .env file: {e}")
            return False
    
    def _generate_env_compose_content(self, branch_name: str) -> str:
        """Generate env.ilha content for a worktree using self.project_root."""
        from ..config.settings import ILHA_DIR, sanitize_project_name, get_allowed_hosts_for_worktree
        import yaml
        
        # Get project name from config in self.project_root
        project_name = None
        config_path = self.project_root / ILHA_DIR / "config.yml"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                    project_name = config.get("project_name")
            except Exception:
                pass
        
        # Fallback to project root directory name if config doesn't have project_name
        if not project_name:
            project_name = self.project_root.name
        
        project_name = sanitize_project_name(project_name)
        compose_project_name = f"{project_name}-{branch_name}"
        site_domain = f"http://{compose_project_name}.localhost"  # RFC-compliant hostname with protocol
        allowed_hosts = get_allowed_hosts_for_worktree(branch_name)
        
        internal_subnet, web_subnet = get_worktree_network_subnets(branch_name)
        base_content = f"""# ilha environment configuration for {branch_name}
COMPOSE_PROJECT_NAME={compose_project_name}
PROJECT_ROOT={self.project_root}
SITE_DOMAIN={site_domain}
ALLOWED_HOSTS={allowed_hosts}
DEBUG=True
USE_X_FORWARDED_HOST=True
CSRF_TRUSTED_ORIGINS={site_domain}

# Per-worktree network subnets (avoid pool overlap; use in docker-compose.worktree.yml)
ILHA_SUBNET_INTERNAL={internal_subnet}
ILHA_SUBNET_WEB={web_subnet}
"""
        host_port_section = self._build_host_port_section(branch_name)
        if host_port_section:
            base_content = f"{base_content}\n{host_port_section}"
        return base_content

    def ensure_worktree_env_has_subnet_vars(self, worktree_path: Path, branch_name: str) -> None:
        """Append ILHA_SUBNET_INTERNAL and ILHA_SUBNET_WEB to env.ilha if missing.

        Allows existing worktrees created before per-worktree subnets to get the vars on next up.
        """
        env_file = worktree_path / ".ilha" / "env.ilha"
        if not env_file.exists():
            return
        try:
            content = env_file.read_text()
            if "ILHA_SUBNET_INTERNAL" in content:
                return
            internal_subnet, web_subnet = get_worktree_network_subnets(branch_name)
            append = f"\n# Per-worktree network subnets (avoid pool overlap)\nILHA_SUBNET_INTERNAL={internal_subnet}\nILHA_SUBNET_WEB={web_subnet}\n"
            env_file.write_text(content.rstrip() + append)
        except Exception:
            pass

    def get_worktree_volume_names(self, branch_name: str) -> Dict[str, str]:
        """Get worktree-specific volume names."""
        return get_volume_names(branch_name)
    
    def get_environment_variables(self, branch_name: str) -> Dict[str, str]:
        """Get environment variables for a worktree."""
        from ..config.settings import get_project_name, sanitize_project_name, get_allowed_hosts_for_worktree
        
        project_name = sanitize_project_name(get_project_name())
        compose_project_name = f"{project_name}-{branch_name}"
        site_domain = f"{project_name}-{branch_name}.localhost"
        
        env_vars = DEFAULT_ENV_VARS.copy()
        env_vars.update({
            "COMPOSE_PROJECT_NAME": compose_project_name,
            "SITE_DOMAIN": site_domain,
            "ALLOWED_HOSTS": get_allowed_hosts_for_worktree(branch_name),
        })
        return env_vars
    
    def validate_environment_file(self, env_file_path: Path) -> bool:
        """Validate that an environment file exists and is readable."""
        if not env_file_path.exists():
            return False
        
        try:
            content = env_file_path.read_text()
            return len(content.strip()) > 0
        except Exception:
            return False
    
    def _should_use_secure_cookies(self, site_domain: str) -> bool:
        """Determine if secure cookies should be used based on SITE_DOMAIN.
        
        Args:
            site_domain: SITE_DOMAIN value (may include http:// or https:// prefix)
            
        Returns:
            True if secure cookies should be used (HTTPS/production),
            False for HTTP/localhost/IP deployments
        """
        # Strip protocol prefix if present
        domain = site_domain
        if domain.startswith('https://'):
            return True
        elif domain.startswith('http://'):
            return False
        
        # Check if it's a localhost domain
        if domain.endswith('.localhost') or domain == 'localhost' or domain.startswith('127.0.0.1'):
            return False
        
        # Check if it's an IP address
        import re
        if re.match(r'^\d+\.\d+\.\d+\.\d+', domain):
            return False
        
        # For production domains (not localhost, not IP), use secure cookies
        # This handles cases where SITE_DOMAIN is just the domain without protocol
        return is_domain(domain)
    
    def _production_domain_env_values(self, domain: str) -> Dict[str, str]:
        """Return SITE_DOMAIN and CSRF_TRUSTED_ORIGINS for production/staging domain.
        
        DRY: Django expects SITE_DOMAIN as bare host (no protocol) and
        CSRF_TRUSTED_ORIGINS as comma-separated (settings use .split(',')).
        Used by _generate_env_compose_with_domain and apply_domain_overrides.
        """
        from ..core.dns_manager import get_base_domain
        normalized_domain = normalize_deploy_host(domain)
        base_domain = get_base_domain(normalized_domain)
        csrf_origins = build_csrf_trusted_origins(normalized_domain, base_domain=base_domain)
        return {"site_domain": normalized_domain, "csrf_trusted_origins": csrf_origins}
    
    def _update_env_var_in_content(self, content: str, var_name: str, value: str) -> str:
        """Update or add an environment variable in content string.

        Args:
            content: Environment file content
            var_name: Variable name (e.g., 'SITE_DOMAIN')
            value: Variable value

        Returns:
            Updated content string
        """
        import re
        pattern = rf'^{var_name}=.*$'
        if re.search(pattern, content, flags=re.MULTILINE):
            content = re.sub(pattern, f'{var_name}={value}', content, flags=re.MULTILINE)
        else:
            content += f"\n{var_name}={value}\n"
        return content

    def _resolve_branch_name_from_worktree(self, worktree_path: Path) -> Optional[str]:
        """Extract branch name from worktree path or COMPOSE_PROJECT_NAME fallback."""
        from ..utils.path_utils import get_worktree_branch_name, get_env_compose_file_path

        branch_name = get_worktree_branch_name(worktree_path)
        if not branch_name:
            env_ilha = get_env_compose_file_path(worktree_path)
            if env_ilha.exists():
                for line in env_ilha.read_text().splitlines():
                    if line.startswith('COMPOSE_PROJECT_NAME='):
                        compose_project_name = line.split('=', 1)[1].strip().strip('"\'')
                        parts = compose_project_name.rsplit('-', 1)
                        if len(parts) == 2:
                            branch_name = parts[1]
                            break
        if not branch_name:
            log_warning(f"Could not determine branch name for worktree at {worktree_path}, using existing ALLOWED_HOSTS format without container name")
        return branch_name

    def _apply_common_env_overrides(
        self,
        content: str,
        *,
        site_domain: str,
        allowed_hosts: str,
        csrf_trusted_origins: str,
        debug: bool,
    ) -> str:
        """Apply the shared env variable overrides used by both domain and IP deployments."""
        import re
        content = self._update_env_var_in_content(content, 'SITE_DOMAIN', site_domain)
        content = self._update_env_var_in_content(content, 'ALLOWED_HOSTS', allowed_hosts)
        debug_value = 'True' if debug else 'False'
        # DEBUG uses case-insensitive matching for legacy compat
        pattern = r'^DEBUG=.*$'
        if re.search(pattern, content, flags=re.MULTILINE | re.IGNORECASE):
            content = re.sub(pattern, f'DEBUG={debug_value}', content, flags=re.MULTILINE | re.IGNORECASE)
        else:
            content += f"\nDEBUG={debug_value}\n"
        use_secure_cookies = self._should_use_secure_cookies(site_domain)
        content = self._update_env_var_in_content(content, 'USE_SECURE_COOKIES', str(use_secure_cookies))
        content = self._update_env_var_in_content(content, 'USE_X_FORWARDED_HOST', 'True')
        content = self._update_env_var_in_content(content, 'CSRF_TRUSTED_ORIGINS', csrf_trusted_origins)
        content = self._update_env_var_in_content(content, 'BUILD_MODE', 'prod')
        return content
    
    def get_domain_name(self, branch_name: str) -> str:
        """Get the domain name for a worktree.
        
        Reads SITE_DOMAIN from env.ilha if available, otherwise
        constructs the default localhost domain pattern.
        
        Args:
            branch_name: Branch name for the worktree
            
        Returns:
            Domain name (without http:// or https:// prefix)
        """
        try:
            # Attempt to read SITE_DOMAIN from worktree env.ilha
            from ..config.settings import get_worktree_paths
            new_path, legacy_path = get_worktree_paths(branch_name)
            env_path = get_env_compose_file_path(new_path)
            if not env_path.exists() and legacy_path.exists():
                env_path = get_env_compose_file_path(legacy_path)
            site_domain_value = None
            if env_path.exists():
                content = env_path.read_text()
                for line in content.splitlines():
                    if line.startswith('SITE_DOMAIN='):
                        site_domain_value = line.split('=', 1)[1].strip()
                        break
            if site_domain_value:
                # Strip http:// or https:// prefix if present
                domain = site_domain_value
                if domain.startswith('https://'):
                    domain = domain[8:]
                elif domain.startswith('http://'):
                    domain = domain[7:]
                # Remove trailing slash if present
                domain = domain.rstrip('/')
                # Strip surrounding quotes if present (single or double)
                domain = domain.strip("'\"")
                return domain
        except Exception:
            pass
        # Fallback: build from computed domain
        from ..config.settings import get_project_name, sanitize_project_name
        project_name = sanitize_project_name(get_project_name())
        return f"{project_name}-{branch_name}.localhost"

    def get_access_url(self, branch_name: str) -> str:
        """Get the preferred access URL for a worktree.

        If env.ilha defines SITE_DOMAIN starting with http(s):// use it directly.
        Otherwise, construct http://{domain}/ where domain is either SITE_DOMAIN or
        the default {project}-{branch}.localhost fallback.
        """
        try:
            # Attempt to read SITE_DOMAIN from worktree env.ilha
            from ..config.settings import get_worktree_paths
            new_path, legacy_path = get_worktree_paths(branch_name)
            env_path = get_env_compose_file_path(new_path)
            if not env_path.exists() and legacy_path.exists():
                env_path = get_env_compose_file_path(legacy_path)
            site_domain_value = None
            if env_path.exists():
                content = env_path.read_text()
                for line in content.splitlines():
                    if line.startswith('SITE_DOMAIN='):
                        site_domain_value = line.split('=', 1)[1].strip()
                        break
            if site_domain_value:
                # If already a full URL, return as-is
                if site_domain_value.startswith('http://') or site_domain_value.startswith('https://'):
                    return site_domain_value.rstrip('/')
                # Else treat as hostname
                return f"http://{site_domain_value}"
        except Exception:
            pass
        # Fallback: build from computed domain
        return f"http://{self.get_domain_name(branch_name)}"
    
    def get_allowed_hosts(self, branch_name: str) -> str:
        """Get the allowed hosts for a worktree."""
        from ..config.settings import get_allowed_hosts_for_worktree
        return get_allowed_hosts_for_worktree(branch_name)
    
    def get_database_url(self, branch_name: str) -> str:
        """Get the database URL for a worktree.
        
        Reads POSTGRES_USER, POSTGRES_PASSWORD, and POSTGRES_DB from environment variables.
        These should be set in .ilha/env.ilha or .env files.
        
        Args:
            branch_name: Branch name for the worktree
            
        Returns:
            Database URL in format: postgres://user:password@host:port/database
        """
        from ..config.settings import get_project_name, sanitize_project_name
        from ..utils.path_utils import get_env_compose_file_path
        from ..utils.env_loader import load_env_file
        
        # Get environment variables from worktree's env.ilha file
        worktree_path, _ = get_worktree_paths(branch_name)
        env_file = get_env_compose_file_path(worktree_path)
        env_vars = load_env_file(env_file) if env_file.exists() else {}
        
        # Fallback to project root env.ilha if worktree doesn't have one
        if not env_vars.get('POSTGRES_USER'):
            project_root = get_project_root()
            root_env_file = project_root / ".ilha" / "env.ilha"
            if root_env_file.exists():
                root_env = load_env_file(root_env_file)
                env_vars.update(root_env)
        
        # Get PostgreSQL credentials from environment variables
        postgres_user = env_vars.get('POSTGRES_USER') or os.getenv('POSTGRES_USER')
        postgres_password = env_vars.get('POSTGRES_PASSWORD') or os.getenv('POSTGRES_PASSWORD')
        postgres_db = env_vars.get('POSTGRES_DB') or os.getenv('POSTGRES_DB')
        
        if not postgres_user or not postgres_password or not postgres_db:
            raise ValueError(
                "PostgreSQL credentials not found in environment variables. "
                "Please set POSTGRES_USER, POSTGRES_PASSWORD, and POSTGRES_DB in "
                ".ilha/env.ilha or .env file."
            )
        
        project_name = sanitize_project_name(get_project_name())
        compose_project_name = f"{project_name}-{branch_name}"
        return f"postgres://{postgres_user}:{postgres_password}@{compose_project_name}-db:5432/{postgres_db}"
    
    def get_redis_url(self, branch_name: str, redis_port: int = 6379, redis_db: int = 0) -> str:
        """Get the Redis URL for a worktree."""
        from ..config.settings import get_project_name, sanitize_project_name
        project_name = sanitize_project_name(get_project_name())
        compose_project_name = f"{project_name}-{branch_name}"
        return f"redis://{compose_project_name}-redis:{redis_port}/{redis_db}"
    
    def generate_compose_environment(self, branch_name: str) -> Dict[str, str]:
        """Generate environment variables for docker compose."""
        from ..config.settings import get_project_name, sanitize_project_name
        
        env_vars = self.get_environment_variables(branch_name)
        
        # Add database and Redis URLs with project prefix
        project_name = sanitize_project_name(get_project_name())
        compose_project_name = f"{project_name}-{branch_name}"
        
        env_vars["DATABASE_URL"] = self.get_database_url(branch_name)
        env_vars["REDIS_HOST"] = f"{compose_project_name}-redis"
        env_vars["REDIS_PORT"] = "6379"
        env_vars["REDIS_DB"] = "0"
        
        return env_vars
    
    def create_env_file_from_template(self, template_path: Path, 
                                    target_path: Path, 
                                    branch_name: str) -> bool:
        """Create an environment file from a template."""
        if not template_path.exists():
            log_warning(f"Template file {template_path} not found")
            return False
        
        try:
            content = template_path.read_text()
            
            # Replace placeholders
            content = content.replace("{{BRANCH_NAME}}", branch_name)
            content = content.replace("{{DOMAIN_NAME}}", self.get_domain_name(branch_name))
            content = content.replace("{{ALLOWED_HOSTS}}", self.get_allowed_hosts(branch_name))
            content = content.replace("{{DATABASE_URL}}", self.get_database_url(branch_name))
            content = content.replace("{{REDIS_URL}}", self.get_redis_url(branch_name))
            
            target_path.write_text(content)
            log_success(f"Environment file created from template: {target_path}")
            return True
            
        except Exception as e:
            log_warning(f"Failed to create environment file from template: {e}")
            return False
    
    def cleanup_environment_files(self, worktree_path: Path) -> bool:
        """Clean up environment files for a worktree."""
        env_files = [
            get_env_file_path(worktree_path),
            get_env_compose_file_path(worktree_path)
        ]
        
        success = True
        for env_file in env_files:
            if env_file.exists():
                try:
                    env_file.unlink()
                    log_info(f"Removed environment file: {env_file}")
                except Exception as e:
                    log_warning(f"Failed to remove environment file {env_file}: {e}")
                    success = False
        
        return success
    
    def get_worktree_config(self, branch_name: str) -> Dict[str, any]:
        """Get complete worktree configuration."""
        return {
            "branch_name": branch_name,
            "domain_name": self.get_domain_name(branch_name),
            "allowed_hosts": self.get_allowed_hosts(branch_name),
            "database_url": self.get_database_url(branch_name),
            "redis_url": self.get_redis_url(branch_name),
            "volume_names": self.get_worktree_volume_names(branch_name),
            "environment_variables": self.get_environment_variables(branch_name),
            "compose_environment": self.generate_compose_environment(branch_name)
        }
    
    def _generate_env_compose_with_domain(self, branch_name: str, domain: str) -> str:
        """Generate env.ilha content with domain override.
        
        Args:
            branch_name: Branch name
            domain: Domain override (subdomain.domain.tld)
            
        Returns:
            Environment file content with domain overrides
        """
        from ..config.settings import ILHA_DIR, sanitize_project_name
        from ..utils.logging import log_warning
        import yaml
        
        project_root = self.project_root
        
        # Get project name from config in self.project_root (not current directory)
        project_name = None
        config_path = self.project_root / ILHA_DIR / "config.yml"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                    project_name = config.get("project_name")
            except Exception:
                pass
        
        # Fallback to project root directory name if config doesn't have project_name
        if not project_name:
            project_name = self.project_root.name
        
        project_name = sanitize_project_name(project_name)
        compose_project_name = f"{project_name}-{branch_name}"
        
        # Use DRY production domain values (bare SITE_DOMAIN, comma-separated CSRF)
        domain_vars = self._production_domain_env_values(domain)
        site_domain = domain_vars["site_domain"]
        csrf_trusted_origins = domain_vars["csrf_trusted_origins"]
        from ..core.dns_manager import get_base_domain
        base_domain = get_base_domain(site_domain)
        allowed_hosts = f"localhost,127.0.0.1,{site_domain},*.{base_domain},web" if base_domain else f"localhost,127.0.0.1,{site_domain},web"
        
        # Determine CADDY_EMAIL: check if already set in env.ilha, otherwise use default
        default_caddy_email = f"admin@{site_domain}"
        caddy_email = default_caddy_email
        
        # Check if env.ilha exists and has CADDY_EMAIL
        worktree_path = Path(project_root) / "worktrees" / branch_name
        env_compose_path = get_env_compose_file_path(worktree_path)
        if env_compose_path.exists():
            from ..utils.env_loader import load_env_file
            existing_env = load_env_file(env_compose_path)
            if 'CADDY_EMAIL' in existing_env:
                caddy_email = existing_env['CADDY_EMAIL']
            else:
                log_warning(f"CADDY_EMAIL not set in env.ilha. Using default: {default_caddy_email}")
                log_warning("This email is used for Let's Encrypt certificate notifications.")
                log_warning("To customize, add CADDY_EMAIL=your-email@example.com to .ilha/env.ilha")
        else:
            log_warning(f"CADDY_EMAIL not set. Using default: {default_caddy_email}")
            log_warning("This email is used for Let's Encrypt certificate notifications.")
            log_warning("To customize, add CADDY_EMAIL=your-email@example.com to .ilha/env.ilha")
        
        # Determine secure cookie setting (bare domain is fine)
        use_secure_cookies = self._should_use_secure_cookies(site_domain)
        
        base_content = f"""# ilha environment configuration for {branch_name}
# Domain override: {site_domain}
COMPOSE_PROJECT_NAME={compose_project_name}
PROJECT_ROOT={project_root}
SITE_DOMAIN={site_domain}
ALLOWED_HOSTS={allowed_hosts}
DEBUG=False
USE_X_FORWARDED_HOST=True
SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https
CSRF_TRUSTED_ORIGINS={csrf_trusted_origins}
USE_SECURE_COOKIES={str(use_secure_cookies)}
BUILD_MODE=prod
CADDY_EMAIL={caddy_email}
"""
        host_port_section = self._build_host_port_section(branch_name)
        if host_port_section:
            base_content = f"{base_content}\n{host_port_section}"
        return base_content
    def _update_volumes_for_production(self, compose_data: Dict, worktree_path: Path) -> bool:
        """Update volumes in compose data for production mode.
        
        In production mode (BUILD_MODE=prod), frontend services should use built files
        from the Docker image rather than bind-mounted source code. This method removes
        source code bind mounts for frontend services while preserving named volumes.
        
        Args:
            compose_data: Docker Compose data structure
            worktree_path: Path to worktree directory
            
        Returns:
            True if volumes were updated, False otherwise
        """
        updated = False
        
        if 'services' not in compose_data:
            return False
        
        # Common frontend service names
        frontend_service_names = ['frontend', 'web', 'client', 'app']
        
        for service_name, service_config in compose_data['services'].items():
            # Check if this is a frontend service (by name or by build context)
            is_frontend = False
            if service_name.lower() in frontend_service_names:
                is_frontend = True
            elif 'build' in service_config:
                build_config = service_config['build']
                if isinstance(build_config, dict) and 'context' in build_config:
                    context = build_config['context']
                    if isinstance(context, str) and any(frontend_name in context.lower() 
                                                       for frontend_name in ['frontend', 'client', 'web']):
                        is_frontend = True
            
            if is_frontend and 'volumes' in service_config:
                volumes = service_config['volumes']
                new_volumes = []
                
                for volume in volumes:
                    if isinstance(volume, str):
                        # Keep named volumes (e.g., /app/node_modules) and volume references
                        # Remove bind mounts to source code directories
                        if volume.startswith('/') and ':' in volume:
                            # This is a bind mount (host:container or /path:container)
                            # Check if it's a source code mount (contains common source paths)
                            source_path = volume.split(':')[0]
                            if any(path in source_path.lower() for path in ['frontend', 'client', 'web', 'src']):
                                # This is a source code bind mount - remove it in production
                                log_info(f"Removing source code bind mount for {service_name}: {volume}")
                                updated = True
                                continue
                        # Keep named volumes and other volume types
                        new_volumes.append(volume)
                    elif isinstance(volume, dict):
                        # Handle dict-style volume definitions
                        if 'type' in volume and volume['type'] == 'bind':
                            target = volume.get('target', '')
                            if any(path in target.lower() for path in ['/app', '/code', '/src']):
                                # This is a source code bind mount - remove it in production
                                log_info(f"Removing source code bind mount for {service_name}: {volume}")
                                updated = True
                                continue
                        # Keep other volume types (named volumes, tmpfs, etc.)
                        new_volumes.append(volume)
                    else:
                        # Keep other volume formats
                        new_volumes.append(volume)
                
                if updated:
                    service_config['volumes'] = new_volumes
                    if not new_volumes:
                        # Remove volumes key if empty
                        del service_config['volumes']
        
        return updated
    
    def apply_domain_overrides(self, worktree_path: Path, domain: str, debug: bool = False) -> bool:
        """Apply domain overrides to existing environment files.

        This method modifies .env files to replace localhost references
        with production/staging domain values.
        """
        try:
            from ..config.settings import build_allowed_hosts_with_container
            from ..utils.path_utils import get_env_compose_file_path

            branch_name = self._resolve_branch_name_from_worktree(worktree_path)

            # Use DRY production domain values (bare SITE_DOMAIN, comma-separated CSRF)
            domain_vars = self._production_domain_env_values(domain)
            site_domain_bare = domain_vars["site_domain"]
            csrf_trusted_origins = domain_vars["csrf_trusted_origins"]

            # Extract base domain for ALLOWED_HOSTS
            from ..core.dns_manager import get_base_domain
            base_domain = get_base_domain(site_domain_bare)

            if branch_name:
                additional_hosts = [site_domain_bare]
                if base_domain:
                    additional_hosts.append(f"*.{base_domain}")
                allowed_hosts = build_allowed_hosts_with_container(branch_name, additional_hosts)
            else:
                allowed_hosts = f"localhost,127.0.0.1,{site_domain_bare},*.{base_domain},web" if base_domain else f"localhost,127.0.0.1,{site_domain_bare},web"

            # Update env.ilha file
            env_ilha = get_env_compose_file_path(worktree_path)
            if env_ilha.exists():
                from ..utils.env_loader import load_env_file
                existing_env = load_env_file(env_ilha)
                content = env_ilha.read_text()

                # CADDY_EMAIL default (domain-only)
                if 'CADDY_EMAIL' not in existing_env:
                    default_caddy_email = f"admin@{site_domain_bare}"
                    log_warning(f"CADDY_EMAIL not set in env.ilha. Using default: {default_caddy_email}")
                    log_warning("This email is used for Let's Encrypt certificate notifications.")
                    log_warning("To customize, add CADDY_EMAIL=your-email@example.com to .ilha/env.ilha")
                    if 'CADDY_EMAIL=' not in content:
                        content += f"\nCADDY_EMAIL={default_caddy_email}\n"

                # Shared env var updates
                content = self._apply_common_env_overrides(
                    content,
                    site_domain=site_domain_bare,
                    allowed_hosts=allowed_hosts,
                    csrf_trusted_origins=csrf_trusted_origins,
                    debug=debug,
                )

                # Domain-specific: SECURE_PROXY_SSL_HEADER (HTTPS only)
                content = self._update_env_var_in_content(content, 'SECURE_PROXY_SSL_HEADER', 'HTTP_X_FORWARDED_PROTO,https')

                env_ilha.write_text(content)
                log_info(f"Applied domain overrides to env.ilha: {site_domain_bare}")
            
            # Update docker-compose override file to add/update Caddy labels and ALLOWED_HOSTS
            # IMPORTANT: Only modify the .ilha/docker-compose.worktree.yml override file,
            # never modify the base docker-compose.yml file
            from ..utils.path_utils import get_compose_override_path
            import yaml
            from ..utils.file_utils import clean_compose_version_field
            
            # Only modify the override file in .ilha directory
            compose_file = get_compose_override_path(worktree_path)
            
            if not compose_file or not compose_file.exists():
                log_warning(f"Could not find docker-compose override file for worktree at {worktree_path}")
                log_warning(f"Expected: {worktree_path / '.ilha' / 'docker-compose.worktree.yml'}")
            else:
                # Only process the override file, never the base compose file
                try:
                    compose_content = compose_file.read_text()
                    compose_data = yaml.safe_load(compose_content)
                    
                    if not compose_data:
                        log_warning(f"Docker Compose override file is empty or invalid: {compose_file}")
                    elif 'services' not in compose_data:
                        log_warning(f"Docker Compose override file has no 'services' section: {compose_file}")
                    else:
                        # Use shared utility to ensure Caddy labels and network are configured
                        # This will ADD labels if missing, or update them if they exist
                        caddy_updated = ensure_caddy_labels_and_network(
                            compose_data, 
                            domain=site_domain_bare, 
                            ip=None, 
                            use_localhost_pattern=False  # Don't use localhost pattern, use domain
                        )
                        
                        # Also update ALLOWED_HOSTS in environment section for all services
                        allowed_hosts_updated = False
                        vite_allowed_hosts_updated = False
                        volumes_updated = False
                        for service_name, service_config in compose_data['services'].items():
                            if update_allowed_hosts_in_compose(service_config, site_domain_bare):
                                allowed_hosts_updated = True
                                log_info(f"Updated ALLOWED_HOSTS for {service_name} in override file")
                            # Update VITE_ALLOWED_HOSTS for frontend services (Vite dev server)
                            if update_vite_allowed_hosts_in_compose(service_config, site_domain_bare, service_name):
                                vite_allowed_hosts_updated = True
                                log_info(f"Updated VITE_ALLOWED_HOSTS for {service_name} in override file")
                        
                        # Update volumes for production mode (remove source code bind mounts for frontend)
                        # Check BUILD_MODE from env.ilha
                        from ..utils.path_utils import get_env_compose_file_path
                        env_ilha = get_env_compose_file_path(worktree_path)
                        build_mode = None
                        if env_ilha.exists():
                            env_content = env_ilha.read_text()
                            for line in env_content.splitlines():
                                if line.startswith('BUILD_MODE='):
                                    build_mode = line.split('=', 1)[1].strip()
                                    break
                        
                        if build_mode == 'prod':
                            volumes_updated = self._update_volumes_for_production(compose_data, worktree_path)
                            if volumes_updated:
                                log_info("Updated volumes for production mode (removed source code bind mounts)")
                        
                        if caddy_updated or allowed_hosts_updated or vite_allowed_hosts_updated or volumes_updated:
                            # Remove version field if it's null (Docker Compose v2 doesn't require it)
                            clean_compose_version_field(compose_data)
                            
                            # Write YAML with proper formatting
                            yaml_content = yaml.dump(compose_data, default_flow_style=False, sort_keys=False, allow_unicode=True)
                            compose_file.write_text(yaml_content)
                            log_info(f"Updated override file {compose_file.name} with domain: {site_domain_bare}")
                            log_info(f"Caddy will route {domain} to containers when they start")
                        else:
                            log_info(f"No updates needed for override file (labels and ALLOWED_HOSTS already configured)")
                        
                except Exception as e:
                    log_warning(f"Failed to update override file {compose_file.name}: {e}")
                    import traceback
                    log_warning(f"Traceback: {traceback.format_exc()}")
                    # Don't return False here - env files may have been updated successfully
                    # Compose file update failure is a warning, not a complete failure
            
            # Return True if we successfully updated env files (compose file update is optional)
            # Env files are the critical part for domain override
            return True
            
        except Exception as e:
            log_warning(f"Failed to apply domain overrides: {e}")
            return False

    def update_project_root(self, worktree_path: Path, project_root: Path) -> bool:
        """Update PROJECT_ROOT in env.ilha file.
        
        This method is used during standalone imports to fix PROJECT_ROOT
        from source machine path to target server path.
        
        Reuses the same pattern as apply_domain_overrides(): read file, regex replace, write file.
        
        Args:
            worktree_path: Path to worktree directory
            project_root: New PROJECT_ROOT value (target server path)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            from ..utils.path_utils import get_env_compose_file_path
            import re
            
            env_ilha = get_env_compose_file_path(worktree_path)
            if not env_ilha.exists():
                log_warning(f"env.ilha not found: {env_ilha}")
                return False
            
            content = env_ilha.read_text()
            # Replace PROJECT_ROOT line
            content = re.sub(
                r'^PROJECT_ROOT=.*$',
                f'PROJECT_ROOT={project_root}',
                content,
                flags=re.MULTILINE
            )
            env_ilha.write_text(content)
            log_info(f"Updated PROJECT_ROOT in env.ilha: {project_root}")
            return True
        except Exception as e:
            log_warning(f"Failed to update PROJECT_ROOT: {e}")
            return False

    def fix_standalone_paths(self, worktree_path: Path, project_root: Path) -> bool:
        """Fix build context and volume paths in compose file for standalone deployments.
        
        For standalone deployments, build context and code volume mounts should point
        to the worktree directory, not PROJECT_ROOT.
        
        Reuses the same compose file update pattern as apply_domain_overrides():
        read YAML, update services, write YAML.
        
        Args:
            worktree_path: Path to worktree directory
            project_root: Project root path (for reference, but worktree_path is used for builds)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            from ..utils.path_utils import get_compose_override_path
            import yaml
            from ..utils.file_utils import clean_compose_version_field
            
            compose_file = get_compose_override_path(worktree_path)
            if not compose_file or not compose_file.exists():
                log_warning(f"Compose file not found: {compose_file}")
                return False
            
            compose_content = compose_file.read_text()
            compose_data = yaml.safe_load(compose_content)
            
            if not compose_data or 'services' not in compose_data:
                log_warning(f"Invalid compose file: {compose_file}")
                return False
            
            updated = False
            
            # Fix build context and volume mounts for all services
            for service_name, service_config in compose_data['services'].items():
                # Fix build context
                if 'build' in service_config:
                    if isinstance(service_config['build'], str):
                        if '${PROJECT_ROOT}' in service_config['build']:
                            service_config['build'] = service_config['build'].replace('${PROJECT_ROOT}', str(worktree_path))
                            updated = True
                    elif isinstance(service_config['build'], dict):
                        if 'context' in service_config['build']:
                            context = service_config['build']['context']
                            if isinstance(context, str) and '${PROJECT_ROOT}' in context:
                                service_config['build']['context'] = context.replace('${PROJECT_ROOT}', str(worktree_path))
                                updated = True
                
                # Fix volume mounts for code directories (e.g., /app, /code, /src)
                if 'volumes' in service_config:
                    volumes = service_config['volumes']
                    for i, volume in enumerate(volumes):
                        if isinstance(volume, str) and '${PROJECT_ROOT}' in volume:
                            # Only replace for code mounts (contains /app, /code, /src, etc.)
                            if any(path in volume for path in ['/app', '/code', '/src']):
                                volumes[i] = volume.replace('${PROJECT_ROOT}', str(worktree_path))
                                updated = True
            
            if updated:
                clean_compose_version_field(compose_data)
                yaml_content = yaml.dump(compose_data, default_flow_style=False, sort_keys=False, allow_unicode=True)
                compose_file.write_text(yaml_content)
                log_info(f"Fixed standalone paths in {compose_file.name}")
            
            return True
        except Exception as e:
            log_warning(f"Failed to fix standalone paths: {e}")
            return False

    def verify_domain_configuration(self, worktree_path: Path, domain: str) -> Dict[str, bool]:
        """Verify domain configuration is correct after deployment.
        
        Args:
            worktree_path: Path to worktree directory
            domain: Expected domain
            
        Returns:
            Dictionary with verification results for each check
        """
        results = {
            "compose_labels": False,
            "env_variables": False,
        }
        
        try:
            # Check compose file labels
            from ..utils.path_utils import get_compose_override_path
            compose_file = get_compose_override_path(worktree_path)
            if compose_file and compose_file.exists():
                import yaml
                compose_content = compose_file.read_text()
                compose_data = yaml.safe_load(compose_content)
                if compose_data and 'services' in compose_data:
                    for svc_config in compose_data['services'].values():
                        if 'labels' in svc_config:
                            labels = svc_config['labels']
                            if isinstance(labels, list):
                                for label in labels:
                                    if isinstance(label, str) and 'caddy.proxy=' in label and domain in label:
                                        results["compose_labels"] = True
                                        break
                            elif isinstance(labels, dict):
                                if labels.get('caddy.proxy') == domain:
                                    results["compose_labels"] = True
                                    break
                        if results["compose_labels"]:
                            break
            
            # Check env.ilha variables
            env_ilha = get_env_compose_file_path(worktree_path)
            if env_ilha.exists():
                content = env_ilha.read_text()
                # Check if domain is in ALLOWED_HOSTS
                if 'ALLOWED_HOSTS=' in content:
                    for line in content.splitlines():
                        if line.startswith('ALLOWED_HOSTS='):
                            allowed_hosts = line.split('=', 1)[1].strip().strip('"\'')
                            if domain in allowed_hosts:
                                results["env_variables"] = True
                                break
                # Check if SITE_DOMAIN includes domain
                if 'SITE_DOMAIN=' in content:
                    for line in content.splitlines():
                        if line.startswith('SITE_DOMAIN='):
                            site_domain = line.split('=', 1)[1].strip().strip('"\'')
                            if domain in site_domain:
                                results["env_variables"] = True
                                break
            
            return results
            
        except Exception as e:
            log_warning(f"Domain configuration verification failed: {e}")
            return results
    
    def apply_ip_overrides(self, worktree_path: Path, ip: str, debug: bool = False) -> bool:
        """Apply IP overrides to existing environment files (HTTP-only).

        Sets SITE_DOMAIN to http://{ip}, updates ALLOWED_HOSTS to include
        the IP, and sets DEBUG based on debug parameter (default: False for production).
        """
        try:
            from ..config.settings import build_allowed_hosts_with_container
            from ..utils.path_utils import get_env_compose_file_path

            branch_name = self._resolve_branch_name_from_worktree(worktree_path)

            http_url = f"http://{ip}"

            if branch_name:
                allowed_hosts = build_allowed_hosts_with_container(branch_name, [ip])
            else:
                allowed_hosts = f"localhost,127.0.0.1,{ip},web"

            # Update env.ilha
            env_ilha = get_env_compose_file_path(worktree_path)
            if env_ilha.exists():
                content = env_ilha.read_text()
                content = self._apply_common_env_overrides(
                    content,
                    site_domain=http_url,
                    allowed_hosts=allowed_hosts,
                    csrf_trusted_origins=f"http://{ip}",
                    debug=debug,
                )
                env_ilha.write_text(content)
                log_info(f"Applied IP overrides to env.ilha: {ip}")
            
            # Update docker-compose.worktree.yml to replace localhost patterns with IP
            from ..utils.path_utils import get_compose_override_path
            compose_file = get_compose_override_path(worktree_path)
            if compose_file and compose_file.exists():
                try:
                    import yaml
                    compose_content = compose_file.read_text()
                    compose_data = yaml.safe_load(compose_content)
                    
                    if compose_data and 'services' in compose_data:
                        updated = False
                        for service_name, service_config in compose_data['services'].items():
                            if 'labels' in service_config:
                                labels = service_config['labels']
                                # Handle both list and dict formats
                                if isinstance(labels, list):
                                    for i, label in enumerate(labels):
                                        if isinstance(label, str) and 'caddy.proxy=' in label:
                                            # Replace localhost pattern with IP
                                            if '${COMPOSE_PROJECT_NAME}.localhost' in label or '.localhost' in label:
                                                labels[i] = f"caddy.proxy={ip}"
                                                updated = True
                                                log_info(f"Updated Caddy label for {service_name}: {ip}")
                                elif isinstance(labels, dict):
                                    if 'caddy.proxy' in labels:
                                        old_value = labels['caddy.proxy']
                                        if '${COMPOSE_PROJECT_NAME}.localhost' in str(old_value) or '.localhost' in str(old_value):
                                            labels['caddy.proxy'] = ip
                                            updated = True
                                            log_info(f"Updated Caddy label for {service_name}: {ip}")
                        
                        # Update volumes for production mode (remove source code bind mounts for frontend)
                        volumes_updated = self._update_volumes_for_production(compose_data, worktree_path)
                        if volumes_updated:
                            log_info("Updated volumes for production mode (removed source code bind mounts)")
                        
                        if updated or volumes_updated:
                            # Remove version field if it's null (Docker Compose v2 doesn't require it)
                            from ..utils.file_utils import clean_compose_version_field
                            clean_compose_version_field(compose_data)
                            
                            compose_file.write_text(yaml.dump(compose_data, default_flow_style=False, sort_keys=False))
                            log_info(f"Updated docker-compose.worktree.yml with IP: {ip}")
                except Exception as e:
                    log_warning(f"Failed to update docker-compose.worktree.yml: {e}")
            
            return True
        except Exception as e:
            log_warning(f"Failed to apply IP overrides: {e}")
            return False
    
    def get_push_config(self, branch_name: str) -> Dict[str, Optional[str]]:
        """Get push configuration from worktree's env.ilha file.
        
        Reads PUSH_SCP_TARGET, PUSH_BRANCH_NAME, PUSH_DOMAIN, PUSH_IP from
        the worktree's env.ilha file. Also checks DROPLET_* variables
        for backward compatibility.
        
        Args:
            branch_name: Branch name for the worktree
            
        Returns:
            Dictionary with push configuration keys (scp_target, branch_name, domain, ip)
            Values are None if not found
        """
        from ..config.settings import get_worktree_paths
        from ..utils.env_loader import load_env_file
        
        try:
            worktree_path, legacy_path = get_worktree_paths(branch_name)
            env_path = get_env_compose_file_path(worktree_path)
            if not env_path.exists() and legacy_path.exists():
                env_path = get_env_compose_file_path(legacy_path)
            
            if not env_path.exists():
                # Try DROPLET_* config as fallback
                droplet_config = self.get_droplet_config(branch_name)
                return {
                    'scp_target': droplet_config.get('scp_target'),
                    'branch_name': None,
                    'domain': droplet_config.get('domain'),
                    'ip': droplet_config.get('ip')
                }
            
            env_vars = load_env_file(env_path)
            
            # Check PUSH_* first (legacy), then DROPLET_* (new format)
            scp_target = env_vars.get('PUSH_SCP_TARGET') or env_vars.get('DROPLET_SCP_TARGET')
            domain = env_vars.get('PUSH_DOMAIN') or env_vars.get('DROPLET_DOMAIN')
            ip = env_vars.get('PUSH_IP') or env_vars.get('DROPLET_IP')
            
            return {
                'scp_target': scp_target,
                'branch_name': env_vars.get('PUSH_BRANCH_NAME'),
                'domain': domain,
                'ip': ip
            }
        except Exception:
            # Try DROPLET_* config as fallback
            droplet_config = self.get_droplet_config(branch_name)
            return {
                'scp_target': droplet_config.get('scp_target'),
                'branch_name': None,
                'domain': droplet_config.get('domain'),
                'ip': droplet_config.get('ip')
            }
    
    def save_push_config(self, branch_name: str, scp_target: str, domain: Optional[str] = None, 
                        ip: Optional[str] = None) -> bool:
        """Save push configuration to worktree's env.ilha file.
        
        Appends or updates PUSH_SCP_TARGET, PUSH_BRANCH_NAME, PUSH_DOMAIN, PUSH_IP
        in the worktree's env.ilha file. Also saves to DROPLET_* format.
        
        Args:
            branch_name: Branch name for the worktree
            scp_target: SCP target in format username@server:path
            domain: Optional domain override
            ip: Optional IP override (mutually exclusive with domain)
            
        Returns:
            True if successful, False otherwise
        """
        from ..config.settings import get_worktree_paths
        import re
        
        try:
            worktree_path, legacy_path = get_worktree_paths(branch_name)
            env_path = get_env_compose_file_path(worktree_path)
            if not env_path.exists() and legacy_path.exists():
                env_path = get_env_compose_file_path(legacy_path)
            
            # Ensure .ilha directory exists
            env_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Read existing content or create new
            if env_path.exists():
                content = env_path.read_text()
            else:
                content = f"# ilha environment configuration for {branch_name}\n"
            
            # Update or add push configuration variables (legacy PUSH_* format)
            # Remove existing push config lines
            content = re.sub(r'^PUSH_SCP_TARGET=.*$', '', content, flags=re.MULTILINE)
            content = re.sub(r'^PUSH_BRANCH_NAME=.*$', '', content, flags=re.MULTILINE)
            content = re.sub(r'^PUSH_DOMAIN=.*$', '', content, flags=re.MULTILINE)
            content = re.sub(r'^PUSH_IP=.*$', '', content, flags=re.MULTILINE)
            
            # Remove multiple blank lines
            content = re.sub(r'\n\n\n+', '\n\n', content)
            
            # Add push configuration at the end (legacy format for backward compatibility)
            content += "\n# Push configuration (auto-saved after successful push)\n"
            content += f"PUSH_SCP_TARGET={scp_target}\n"
            content += f"PUSH_BRANCH_NAME={branch_name}\n"
            if domain:
                content += f"PUSH_DOMAIN={domain}\n"
            if ip:
                content += f"PUSH_IP={ip}\n"
            
            env_path.write_text(content)
            
            # Also save to DROPLET_* format using the new method
            droplet_config = {
                'scp_target': scp_target,
                'domain': domain,
                'ip': ip,
            }
            self.save_droplet_config(branch_name, droplet_config)
            
            log_info(f"Saved push configuration to {env_path}")
            return True
        except Exception as e:
            log_warning(f"Failed to save push configuration: {e}")
            return False
    
    def get_droplet_config(self, branch_name: str) -> Dict[str, Optional[str]]:
        """Get droplet configuration from worktree's env.ilha file.
        
        Reads all DROPLET_* variables from the worktree's env.ilha file.
        
        Args:
            branch_name: Branch name for the worktree
            
        Returns:
            Dictionary with droplet configuration keys. Values are None if not found.
            Keys: region, size, image, ssh_keys, tags, vpc_uuid, central_droplet_name,
                  scp_target, domain, ip, dns_token, output_dir, keep_package,
                  prepare_server, resume, build, debug, containers, exclude_deps
        """
        from ..config.settings import get_worktree_paths
        from ..utils.env_loader import load_env_file
        
        default_config = {
            'region': None,
            'size': None,
            'image': None,
            'ssh_keys': None,
            'tags': None,
            'vpc_uuid': None,
            'central_droplet_name': None,
            'scp_target': None,
            'domain': None,
            'ip': None,
            'dns_token': None,
            'output_dir': None,
            'keep_package': None,
            'prepare_server': None,
            'resume': None,
            'build': None,
            'debug': None,
            'containers': None,
            'exclude_deps': None,
        }
        
        try:
            worktree_path, legacy_path = get_worktree_paths(branch_name)
            env_path = get_env_compose_file_path(worktree_path)
            if not env_path.exists() and legacy_path.exists():
                env_path = get_env_compose_file_path(legacy_path)
            
            if not env_path.exists():
                return default_config
            
            env_vars = load_env_file(env_path)
            
            # Map DROPLET_* env vars to config dict keys
            config = default_config.copy()
            config['region'] = env_vars.get('DROPLET_REGION')
            config['size'] = env_vars.get('DROPLET_SIZE')
            config['image'] = env_vars.get('DROPLET_IMAGE')
            config['ssh_keys'] = env_vars.get('DROPLET_SSH_KEYS')
            config['tags'] = env_vars.get('DROPLET_TAGS')
            config['vpc_uuid'] = env_vars.get('DROPLET_VPC_UUID')
            config['central_droplet_name'] = env_vars.get('DROPLET_CENTRAL_DROPLET_NAME')
            config['scp_target'] = env_vars.get('DROPLET_SCP_TARGET')
            config['domain'] = env_vars.get('DROPLET_DOMAIN')
            config['ip'] = env_vars.get('DROPLET_IP')
            config['dns_token'] = env_vars.get('DROPLET_DNS_TOKEN')
            config['output_dir'] = env_vars.get('DROPLET_OUTPUT_DIR')
            config['keep_package'] = env_vars.get('DROPLET_KEEP_PACKAGE')
            config['prepare_server'] = env_vars.get('DROPLET_PREPARE_SERVER')
            config['resume'] = env_vars.get('DROPLET_RESUME')
            config['build'] = env_vars.get('DROPLET_BUILD')
            config['debug'] = env_vars.get('DROPLET_DEBUG')
            config['containers'] = env_vars.get('DROPLET_CONTAINERS')
            config['exclude_deps'] = env_vars.get('DROPLET_EXCLUDE_DEPS')
            
            return config
        except Exception:
            return default_config
    
    def save_droplet_config(self, branch_name: str, config: Dict[str, Optional[str]]) -> bool:
        """Save droplet configuration to worktree's env.ilha file.
        
        Writes or updates DROPLET_* variables in the worktree's env.ilha file.
        
        Args:
            branch_name: Branch name for the worktree
            config: Dictionary with droplet configuration keys. Only non-None values are saved.
                   Keys: region, size, image, ssh_keys, tags, vpc_uuid, central_droplet_name,
                         scp_target, domain, ip, dns_token, output_dir, keep_package,
                         prepare_server, resume, build, debug, containers, exclude_deps
            
        Returns:
            True if successful, False otherwise
        """
        from ..config.settings import get_worktree_paths
        import re
        
        try:
            worktree_path, legacy_path = get_worktree_paths(branch_name)
            env_path = get_env_compose_file_path(worktree_path)
            if not env_path.exists() and legacy_path.exists():
                env_path = get_env_compose_file_path(legacy_path)
            
            # Ensure .ilha directory exists
            env_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Read existing content or create new
            if env_path.exists():
                content = env_path.read_text()
            else:
                content = f"# ilha environment configuration for {branch_name}\n"
            
            # Remove existing DROPLET_* configuration lines
            droplet_vars = [
                'DROPLET_REGION', 'DROPLET_SIZE', 'DROPLET_IMAGE', 'DROPLET_SSH_KEYS',
                'DROPLET_TAGS', 'DROPLET_VPC_UUID', 'DROPLET_CENTRAL_DROPLET_NAME',
                'DROPLET_SCP_TARGET', 'DROPLET_DOMAIN', 'DROPLET_IP', 'DROPLET_DNS_TOKEN',
                'DROPLET_OUTPUT_DIR', 'DROPLET_KEEP_PACKAGE', 'DROPLET_PREPARE_SERVER',
                'DROPLET_RESUME', 'DROPLET_BUILD', 'DROPLET_DEBUG', 'DROPLET_CONTAINERS', 'DROPLET_EXCLUDE_DEPS'
            ]
            for var in droplet_vars:
                content = re.sub(rf'^{re.escape(var)}=.*$', '', content, flags=re.MULTILINE)
            
            # Remove multiple blank lines
            content = re.sub(r'\n\n\n+', '\n\n', content)
            
            # Add droplet configuration at the end
            content += "\n# Droplet command preferences (auto-saved after successful operations)\n"
            
            # Map config dict keys to DROPLET_* env vars
            mapping = {
                'region': 'DROPLET_REGION',
                'size': 'DROPLET_SIZE',
                'image': 'DROPLET_IMAGE',
                'ssh_keys': 'DROPLET_SSH_KEYS',
                'tags': 'DROPLET_TAGS',
                'vpc_uuid': 'DROPLET_VPC_UUID',
                'central_droplet_name': 'DROPLET_CENTRAL_DROPLET_NAME',
                'scp_target': 'DROPLET_SCP_TARGET',
                'domain': 'DROPLET_DOMAIN',
                'ip': 'DROPLET_IP',
                'dns_token': 'DROPLET_DNS_TOKEN',
                'output_dir': 'DROPLET_OUTPUT_DIR',
                'keep_package': 'DROPLET_KEEP_PACKAGE',
                'prepare_server': 'DROPLET_PREPARE_SERVER',
                'resume': 'DROPLET_RESUME',
                'build': 'DROPLET_BUILD',
                'debug': 'DROPLET_DEBUG',
                'containers': 'DROPLET_CONTAINERS',
                'exclude_deps': 'DROPLET_EXCLUDE_DEPS',
            }
            
            for key, env_var in mapping.items():
                value = config.get(key)
                if value is not None:
                    # Convert boolean to string
                    if isinstance(value, bool):
                        content += f"{env_var}={str(value).lower()}\n"
                    else:
                        content += f"{env_var}={value}\n"
            
            env_path.write_text(content)
            log_info(f"Saved droplet configuration to {env_path}")
            return True
        except Exception as e:
            log_warning(f"Failed to save droplet configuration: {e}")
            return False
    
    def set_staging_certificate_flag(self, branch_name: str, value: bool = True) -> bool:
        """Set USE_STAGING_CERTIFICATES flag in worktree's env.ilha file.
        
        Args:
            branch_name: Branch name for the worktree
            value: If True, sets USE_STAGING_CERTIFICATES=1. If False, removes it.
            
        Returns:
            True if successful, False otherwise
        """
        from ..config.settings import get_worktree_paths
        import re
        
        try:
            worktree_path, legacy_path = get_worktree_paths(branch_name)
            env_path = get_env_compose_file_path(worktree_path)
            if not env_path.exists() and legacy_path.exists():
                env_path = get_env_compose_file_path(legacy_path)
            
            # Ensure .ilha directory exists
            env_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Read existing content or create new
            if env_path.exists():
                content = env_path.read_text()
            else:
                content = f"# ilha environment configuration for {branch_name}\n"
            
            # Remove existing USE_STAGING_CERTIFICATES line if present
            content = re.sub(r'^USE_STAGING_CERTIFICATES=.*$', '', content, flags=re.MULTILINE)
            
            # Remove multiple blank lines
            content = re.sub(r'\n\n\n+', '\n\n', content)
            
            # Add the flag if value is True
            if value:
                # Check if we need to add a newline before adding the flag
                if content and not content.endswith('\n'):
                    content += '\n'
                content += "USE_STAGING_CERTIFICATES=1\n"
                log_info(f"Set USE_STAGING_CERTIFICATES=1 in {env_path}")
            else:
                log_info(f"Removed USE_STAGING_CERTIFICATES from {env_path}")
            
            env_path.write_text(content)
            return True
        except Exception as e:
            log_warning(f"Failed to set staging certificate flag: {e}")
            return False