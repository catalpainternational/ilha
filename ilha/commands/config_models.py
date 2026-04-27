"""Pydantic models for configuration validation."""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Dict, Any
from pathlib import Path
import os


class DropletStoredConfig(BaseModel):
    """Per-droplet stored configuration from ~/.ilha/config.yml."""

    branch: Optional[str] = None
    ip: Optional[str] = None
    domain: Optional[str] = None
    api_token: Optional[str] = None
    remote_user: Optional[str] = Field(default="root")
    ssh_port: int = Field(default=22, ge=1, le=65535)
    docker_compose_file: Optional[str] = None
    environment_variables: Dict[str, str] = Field(default_factory=dict)
    custom_build_args: Dict[str, str] = Field(default_factory=dict)

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, v: Optional[str]) -> Optional[str]:
        """Validate IP address format if provided."""
        if v is None:
            return v
        import ipaddress

        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            raise ValueError(f"Invalid IP address: {v}")

    @field_validator("ssh_port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate SSH port is in valid range."""
        if not (1 <= v <= 65535):
            raise ValueError(f"SSH port {v} out of range [1, 65535]")
        return v


class GlobalIlhaConfig(BaseModel):
    """Top-level ~/.ilha/config.yml structure."""

    version: str = Field(default="1.0")
    default_api_token: Optional[str] = Field(
        default=None,
        description="Default DO API token; can be overridden per-droplet",
    )
    default_region: Optional[str] = Field(default="nyc3")
    default_size: Optional[str] = Field(default="1gb")
    default_remote_user: str = Field(default="root")
    default_ssh_port: int = Field(default=22, ge=1, le=65535)
    droplets: Dict[str, DropletStoredConfig] = Field(default_factory=dict)
    # User preference settings
    auto_backup_before_deploy: bool = Field(default=True)
    health_check_timeout: int = Field(default=300, ge=30, le=3600)
    log_retention_days: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def validate_api_token_available(self) -> "GlobalIlhaConfig":
        """Ensure at least default_api_token or per-droplet tokens exist."""
        if not self.default_api_token:
            # At least one droplet should have its own token
            if not any(d.api_token for d in self.droplets.values()):
                raise ValueError(
                    "No API token configured. Set default_api_token in config "
                    "or api_token in a droplet section."
                )
        return self


class EnvironmentConfig(BaseModel):
    """Runtime environment configuration from env vars."""

    # Required if not in config file
    digitalocean_api_token: Optional[str] = Field(
        default=None, alias="DIGITALOCEAN_API_TOKEN"
    )
    docker_host: Optional[str] = Field(default=None, alias="DOCKER_HOST")
    compose_project_name: Optional[str] = Field(
        default=None, alias="COMPOSE_PROJECT_NAME"
    )
    # Optional
    debug: bool = Field(default=False, alias="DEBUG")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    class Config:
        populate_by_name = True  # allow both alias and field name

    @classmethod
    def from_env(cls) -> "EnvironmentConfig":
        """Load from os.environ."""
        return cls(
            digitalocean_api_token=os.getenv("DIGITALOCEAN_API_TOKEN"),
            docker_host=os.getenv("DOCKER_HOST"),
            compose_project_name=os.getenv("COMPOSE_PROJECT_NAME"),
            debug=os.getenv("DEBUG", "").lower() in ("true", "1"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


class MergedDeployConfig(BaseModel):
    """Fully resolved configuration for a deploy operation."""

    # From environment
    api_token: str  # required
    # From stored config
    branch: str  # required for deploy
    droplet_name: str  # required
    ip: Optional[str] = None
    domain: Optional[str] = None
    remote_user: str = Field(default="root")
    ssh_port: int = Field(default=22)
    # Docker/compose specifics
    docker_compose_file: Optional[Path] = None
    environment_variables: Dict[str, str] = Field(default_factory=dict)
    custom_build_args: Dict[str, str] = Field(default_factory=dict)
    # Deploy settings
    auto_backup: bool = Field(default=True)
    health_check_timeout: int = Field(default=300)
    worker_services: Optional[str] = None
    web_service: Optional[str] = None

    @field_validator("api_token")
    @classmethod
    def api_token_not_empty(cls, v: str) -> str:
        """Ensure api_token is not empty."""
        if not v or not v.strip():
            raise ValueError("api_token is required and cannot be empty")
        return v

    @field_validator("branch")
    @classmethod
    def branch_not_empty(cls, v: str) -> str:
        """Ensure branch is not empty."""
        if not v or not v.strip():
            raise ValueError("branch is required and cannot be empty")
        return v
