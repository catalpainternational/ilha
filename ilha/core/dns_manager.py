"""
DNS management for ilha CLI.

This module provides DNS provider abstraction for managing domain records
via DNS provider APIs (currently Digital Ocean DNS).
"""

import re
from abc import ABC, abstractmethod
from typing import Optional, Tuple, Dict, Any, List
import os
import tldextract

from ..utils.logging import log_info, log_warning, log_error
from ..utils.env_loader import load_env_from_project_root, load_env_from_home


def parse_domain(full_domain: str) -> Tuple[str, str]:
    """Parse full domain into subdomain and base domain components.
    
    Supports:
    - Multi-level subdomains: 'h2.h1.example.com' → ('h2.h1', 'example.com')
    - Multi-part TLDs: 'example.co.uk' → ('', 'example.co.uk')
    - Combined: 'h2.h1.example.co.uk' → ('h2.h1', 'example.co.uk')
    - Simple subdomain: 'app.example.com' → ('app', 'example.com')
    - Root domain: 'example.com' → ('', 'example.com')
    
    Args:
        full_domain: Full domain name (e.g., 'app.example.com', 'h2.h1.example.co.uk')
        
    Returns:
        Tuple of (subdomain, base_domain) where:
        - subdomain: All subdomain parts joined (e.g., 'h2.h1' or 'app'), empty string for root domains
        - base_domain: Registered domain with TLD (e.g., 'example.com' or 'example.co.uk')
        
    Raises:
        ValueError: If domain format is invalid
    """
    if not full_domain or not isinstance(full_domain, str):
        raise ValueError(f"Invalid domain format: {full_domain}. Expected a non-empty string.")
    
    # Basic validation - must contain at least one dot
    if '.' not in full_domain:
        raise ValueError(f"Invalid domain format: {full_domain}. Expected format: subdomain.domain.tld")
    
    # Use tldextract to properly identify domain and TLD
    extracted = tldextract.extract(full_domain)
    
    # Extract base domain (registered domain with TLD)
    # Use top_domain_under_public_suffix (recommended) with fallback to registered_domain for older versions
    base_domain = getattr(extracted, 'top_domain_under_public_suffix', None) or extracted.registered_domain
    
    # If no registered domain found, fall back to simple parsing
    if not base_domain:
        parts = full_domain.split('.')
        if len(parts) < 2:
            raise ValueError(f"Invalid domain format: {full_domain}. Expected format: subdomain.domain.tld")
        # Fallback: assume last 2 parts are domain.tld
        if len(parts) == 2:
            return ('', full_domain)
        subdomain = '.'.join(parts[:-2])
        domain = '.'.join(parts[-2:])
        return (subdomain, domain) if subdomain else ('', domain)
    
    # Extract subdomain (everything before registered_domain)
    subdomain = extracted.subdomain if extracted.subdomain else ''
    
    return (subdomain, base_domain)


def get_base_domain(full_domain: str) -> str:
    """Extract base domain (registered domain) from full domain.
    
    This is a convenience function that extracts only the base domain portion,
    useful for cases where you only need the base domain and not the subdomain.
    
    Args:
        full_domain: Full domain name (e.g., 'app.example.com', 'h2.h1.example.co.uk')
        
    Returns:
        Base domain with TLD (e.g., 'example.com', 'example.co.uk')
        
    Raises:
        ValueError: If domain format is invalid
    """
    _, base_domain = parse_domain(full_domain)
    return base_domain


def is_domain(host: str) -> bool:
    """Check if host is a domain (not IP or localhost).
    
    Args:
        host: Host string to check
        
    Returns:
        True if host is a domain, False if IP or localhost
    """
    if host.startswith('localhost') or host.startswith('127.0.0.1'):
        return False
    
    # IP pattern: \d+\.\d+\.\d+\.\d+
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', host):
        return False
    
    # Domain: contains dots and valid domain characters
    return '.' in host and not host.startswith('.')


class DNSProvider(ABC):
    """Abstract base class for DNS providers."""
    
    @abstractmethod
    def check_domain_exists(self, subdomain: str, domain: str) -> Tuple[bool, Optional[str]]:
        """Check if subdomain exists and return current IP if any.
        
        Args:
            subdomain: Subdomain name (e.g., 'app')
            domain: Base domain (e.g., 'example.com')
            
        Returns:
            Tuple of (exists, current_ip). current_ip is None if doesn't exist.
        """
        pass
    
    @abstractmethod
    def create_subdomain(self, subdomain: str, domain: str, ip: str) -> bool:
        """Create A record for subdomain pointing to IP.
        
        Args:
            subdomain: Subdomain name (e.g., 'app')
            domain: Base domain (e.g., 'example.com')
            ip: IP address to point to
            
        Returns:
            True if successful, False otherwise
        """
        pass
    
    @abstractmethod
    def list_subdomains(self, domain: str) -> List[str]:
        """List all subdomains for a domain.
        
        Args:
            domain: Base domain (e.g., 'example.com')
            
        Returns:
            List of subdomain names
        """
        pass
    
    @abstractmethod
    def delete_subdomain(self, subdomain: str, domain: str) -> bool:
        """Delete A record for subdomain.
        
        Args:
            subdomain: Subdomain name (e.g., 'app')
            domain: Base domain (e.g., 'example.com')
            
        Returns:
            True if successful, False otherwise
        """
        pass


class DNSManager:
    """Manages DNS operations via provider abstraction."""
    
    _providers: Dict[str, type] = {}
    
    @classmethod
    def register_provider(cls, name: str, provider_class: type):
        """Register a DNS provider.
        
        Args:
            name: Provider name (e.g., 'digitalocean', 'do')
            provider_class: Provider class implementing DNSProvider
        """
        cls._providers[name.lower()] = provider_class
    
    @classmethod
    def create_provider(cls, provider_name: str, api_token: str) -> Optional[DNSProvider]:
        """Create a DNS provider instance.
        
        Args:
            provider_name: Provider name (e.g., 'digitalocean', 'do')
            api_token: API token for authentication
            
        Returns:
            DNSProvider instance or None if provider not found
        """
        provider_name = provider_name.lower()
        if provider_name not in cls._providers:
            log_error(f"Unknown DNS provider: {provider_name}")
            log_info(f"Available providers: {', '.join(cls._providers.keys())}")
            return None
        
        provider_class = cls._providers[provider_name]
        try:
            return provider_class(api_token)
        except Exception as e:
            log_error(f"Failed to create DNS provider {provider_name}: {e}")
            return None
    
    @classmethod
    def get_available_providers(cls) -> list[str]:
        """Get list of available DNS providers.
        
        Returns:
            List of provider names
        """
        return list(cls._providers.keys())
    
    @staticmethod
    def resolve_dns_token(token: Optional[str] = None) -> Optional[str]:
        """Resolve DNS API token from various sources.
        
        Priority order (highest to lowest):
        1. Explicit token from CLI flag
        2. Shell environment variable (DIGITALOCEAN_API_TOKEN)
        3. .env file in current directory (worktree or project root)
        4. .ilha/env.ilha in current directory (worktree or project root)
        5. .env file in parent project root (if in worktree)
        6. .ilha/env.ilha in parent project root (if in worktree)
        7. ~/.ilha/env.ilha file (global)
        
        Args:
            token: Explicit token from CLI flag
            
        Returns:
            API token or None if not found
        """
        # Explicit token takes precedence
        if token:
            return token
        
        # Try Digital Ocean API token from shell environment
        token = os.getenv("DIGITALOCEAN_API_TOKEN")
        if token:
            return token
        
        # Try loading from .env file in project root
        try:
            env_vars = load_env_from_project_root()
            token = env_vars.get("DIGITALOCEAN_API_TOKEN")
            if token:
                return token
        except Exception:
            # Silently continue if .env loading fails
            pass
        
        # Try loading from .ilha/env.ilha in project root
        try:
            from ..config.settings import get_project_root
            from ..utils.env_loader import load_env_file
            project_root = get_project_root()
            ilha_env = project_root / ".ilha" / "env.ilha"
            if ilha_env.exists():
                env_vars = load_env_file(ilha_env)
                token = env_vars.get("DIGITALOCEAN_API_TOKEN")
                if token:
                    return token
        except Exception:
            # Silently continue if project .ilha/env.ilha loading fails
            pass
        
        # If token still not found, check parent project root if we're in a worktree
        try:
            from ..utils.path_utils import get_parent_project_root
            from ..utils.env_loader import load_env_file
            parent_root = get_parent_project_root()
            if parent_root:
                # Try loading from .env file in parent project root
                parent_env = parent_root / ".env"
                if parent_env.exists():
                    env_vars = load_env_file(parent_env)
                    token = env_vars.get("DIGITALOCEAN_API_TOKEN")
                    if token:
                        return token
                
                # Try loading from .ilha/env.ilha in parent project root
                parent_ilha_env = parent_root / ".ilha" / "env.ilha"
                if parent_ilha_env.exists():
                    env_vars = load_env_file(parent_ilha_env)
                    token = env_vars.get("DIGITALOCEAN_API_TOKEN")
                    if token:
                        return token
        except Exception:
            # Silently continue if parent project root loading fails
            pass
        
        # Try loading from global ilha config file in home directory
        try:
            env_vars = load_env_from_home()
            token = env_vars.get("DIGITALOCEAN_API_TOKEN")
            if token:
                return token
        except Exception:
            # Silently continue if global config loading fails
            pass
        
        return None

