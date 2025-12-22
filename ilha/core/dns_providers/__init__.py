"""
DNS provider implementations for ilha.

This package contains implementations of DNS providers for managing
domain records via various DNS provider APIs.
"""

from .digitalocean import DigitalOceanProvider
from ..dns_manager import DNSManager
from ..droplet_manager import DropletManager

# Register DNS providers
DNSManager.register_provider('digitalocean', DigitalOceanProvider)
DNSManager.register_provider('do', DigitalOceanProvider)  # Alias

# Register droplet providers
DropletManager.register_provider('digitalocean', DigitalOceanProvider)
DropletManager.register_provider('do', DigitalOceanProvider)  # Alias

__all__ = ['DigitalOceanProvider', 'DNSManager', 'DropletManager']

