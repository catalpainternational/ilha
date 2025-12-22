"""
Direct Python API for ilha MCP server.

This module provides direct Python API access to ilha functionality,
bypassing the CLI wrapper for richer responses and better integration.
"""

from .ilha_api import ilhaAPI

__all__ = ["ilhaAPI"]
