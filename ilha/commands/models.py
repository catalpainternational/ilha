"""Pydantic models for commands."""

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class RemoteTarget(BaseModel):
    """Resolved remote target for rsync/SSH operations."""

    scp_target: str = Field(
        description="Resolved rsync/SSH target (user@ip:path format)"
    )
    domain: Optional[str] = Field(
        default=None, description="Domain name if applicable"
    )
    ip: Optional[str] = Field(
        default=None, description="IP address of the target"
    )
    token: Optional[str] = Field(
        default=None, description="API token for DNS operations"
    )
    stored_config: Dict[str, Any] = Field(
        default_factory=dict, description="Stored droplet configuration"
    )


class MergedConfig(BaseModel):
    """Merged CLI and stored configuration."""

    config: Dict[str, Any] = Field(
        description="Merged configuration dictionary"
    )
    env_manager: Any = Field(
        description="EnvironmentManager instance (not validated by Pydantic)"
    )
    stored_config: Dict[str, Any] = Field(
        description="Original stored configuration"
    )

    class Config:
        arbitrary_types_allowed = True
