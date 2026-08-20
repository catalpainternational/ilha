"""
Droplet management for ilha CLI.

This module provides droplet provider abstraction for managing cloud droplets
via provider APIs (currently Digital Ocean).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from datetime import datetime

from ..utils.logging import log_info, log_warning, log_error
from .dns_manager import DNSManager


@dataclass
class DropletInfo:
    """Information about a droplet."""
    id: int
    name: str
    ip_address: Optional[str]  # Public IP
    status: str
    region: str
    size: str
    image: str
    created_at: Optional[datetime] = None
    tags: List[str] = None
    private_ip_address: Optional[str] = None  # VPC private IP
    vpc_uuid: Optional[str] = None  # VPC UUID
    
    def __post_init__(self):
        """Initialize default values."""
        if self.tags is None:
            self.tags = []


class DropletProvider(ABC):
    """Abstract base class for droplet providers."""
    
    @abstractmethod
    def create_droplet(self, name: str, region: str, size: str, image: str,
                      ssh_keys: Optional[List[str]] = None,
                      tags: Optional[List[str]] = None,
                      vpc_uuid: Optional[str] = None,
                      user_data: Optional[str] = None) -> Optional[DropletInfo]:
        """Create a new droplet.
        
        Args:
            name: Droplet name
            region: Droplet region (e.g., 'nyc1')
            size: Droplet size (e.g., 's-1vcpu-1gb')
            image: Droplet image (e.g., 'ubuntu-22-04-x64')
            ssh_keys: List of SSH key IDs or fingerprints
            tags: List of tags for the droplet
            vpc_uuid: Optional VPC UUID for private networking
            user_data: Optional cloud-init user data script
            
        Returns:
            DropletInfo if successful, None otherwise
        """
        pass
    
    @abstractmethod
    def list_droplets(self) -> List[DropletInfo]:
        """List all droplets.
        
        Returns:
            List of DropletInfo objects
        """
        pass
    
    @abstractmethod
    def get_droplet(self, droplet_id: int) -> Optional[DropletInfo]:
        """Get droplet information by ID.
        
        Args:
            droplet_id: Droplet ID
            
        Returns:
            DropletInfo if found, None otherwise
        """
        pass
    
    @abstractmethod
    def destroy_droplet(self, droplet_id: int) -> bool:
        """Destroy a droplet.
        
        Args:
            droplet_id: Droplet ID to destroy
            
        Returns:
            True if successful, False otherwise
        """
        pass
    
    @abstractmethod
    def wait_for_droplet_ready(self, droplet_id: int, timeout: int = 300, check_ssh: bool = True) -> bool:
        """Wait for droplet to be ready (active status and optionally SSH).
        
        Args:
            droplet_id: Droplet ID
            timeout: Maximum time to wait in seconds (default: 300)
            check_ssh: Also wait for SSH to be accessible (default: True)
            
        Returns:
            True if droplet is ready, False if timeout
        """
        pass
    
    def list_sizes(self) -> List[Dict[str, Any]]:
        """List available droplet sizes.
        
        Returns:
            List of dictionaries with size information (slug, memory, vcpus, disk, price_monthly, price_hourly)
        """
        # Default implementation returns empty list
        # Providers should override this method
        return []
    
    def list_regions(self) -> List[Dict[str, Any]]:
        """List available droplet regions.
        
        Returns:
            List of dictionaries with region information (slug, name, available, features, sizes)
        """
        return []


class DropletManager:
    """Manages droplet operations via provider abstraction."""
    
    _providers: Dict[str, type] = {}
    
    @classmethod
    def register_provider(cls, name: str, provider_class: type):
        """Register a droplet provider.
        
        Args:
            name: Provider name (e.g., 'digitalocean', 'do')
            provider_class: Provider class implementing DropletProvider
        """
        cls._providers[name.lower()] = provider_class
    
    @classmethod
    def create_provider(cls, provider_name: str, api_token: str) -> Optional[DropletProvider]:
        """Create a droplet provider instance.
        
        Args:
            provider_name: Provider name (e.g., 'digitalocean', 'do')
            api_token: API token for authentication
            
        Returns:
            DropletProvider instance or None if provider not found
        """
        provider_name = provider_name.lower()
        if provider_name not in cls._providers:
            log_error(f"Unknown droplet provider: {provider_name}")
            log_info(f"Available providers: {', '.join(cls._providers.keys())}")
            return None
        
        provider_class = cls._providers[provider_name]
        try:
            return provider_class(api_token)
        except Exception as e:
            log_error(f"Failed to create droplet provider {provider_name}: {e}")
            return None
    
    @classmethod
    def get_available_providers(cls) -> List[str]:
        """Get list of available droplet providers.
        
        Returns:
            List of provider names
        """
        return list(cls._providers.keys())
    
    @staticmethod
    def resolve_droplet_token(token: Optional[str] = None) -> Optional[str]:
        """Resolve droplet API token from various sources.
        
        Reuses DNS token resolution since both use the same Digital Ocean API token.
        
        Priority order (highest to lowest):
        1. Explicit token from CLI flag
        2. Shell environment variable (DIGITALOCEAN_API_TOKEN)
        3. .env file in project root (DIGITALOCEAN_API_TOKEN)
        4. .ilha/env.ilha in project root (DIGITALOCEAN_API_TOKEN)
        5. ~/.ilha/env.ilha file (DIGITALOCEAN_API_TOKEN)
        
        Args:
            token: Explicit token from CLI flag
            
        Returns:
            API token or None if not found
        """
        return DNSManager.resolve_dns_token(token)
    
    @staticmethod
    def get_droplet_defaults() -> Dict[str, Any]:
        """Get droplet default values from environment files.
        
        Checks .env file in project root and .ilha/env.ilha.
        
        Returns:
            Dictionary with default values:
            - region: Default region (default: 'nyc1')
            - size: Default size (default: 's-1vcpu-1gb')
            - image: Default image (default: 'ubuntu-22-04-x64')
            - ssh_keys: Default SSH keys (default: [])
        """
        from ..utils.env_loader import load_env_from_project_root
        from ..config.settings import get_project_root
        
        defaults = {
            'region': 'nyc1',
            'size': 's-1vcpu-1gb',
            'image': 'ubuntu-22-04-x64',
            'ssh_keys': []
        }
        
        # Load from project root .env
        env_vars = load_env_from_project_root()
        
        # Load from .ilha/env.ilha if exists
        try:
            project_root = get_project_root()
            ilha_env = project_root / '.ilha' / 'env.ilha'
            if ilha_env.exists():
                from ..utils.env_loader import load_env_file
                ilha_vars = load_env_file(ilha_env)
                env_vars.update(ilha_vars)
        except Exception:
            pass
        
        # Override defaults with environment variables
        if 'DROPLET_DEFAULT_REGION' in env_vars:
            defaults['region'] = env_vars['DROPLET_DEFAULT_REGION']
        
        if 'DROPLET_DEFAULT_SIZE' in env_vars:
            defaults['size'] = env_vars['DROPLET_DEFAULT_SIZE']
        
        if 'DROPLET_DEFAULT_IMAGE' in env_vars:
            defaults['image'] = env_vars['DROPLET_DEFAULT_IMAGE']
        
        if 'DROPLET_DEFAULT_SSH_KEYS' in env_vars:
            ssh_keys_str = env_vars['DROPLET_DEFAULT_SSH_KEYS']
            if ssh_keys_str:
                defaults['ssh_keys'] = [k.strip() for k in ssh_keys_str.split(',') if k.strip()]
        
        return defaults

