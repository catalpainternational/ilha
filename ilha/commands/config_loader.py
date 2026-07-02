"""Configuration loading and merging with Pydantic validation."""

from pathlib import Path
from typing import Optional, Dict, Any
import yaml

from .config_models import (
    GlobalIlhaConfig,
    EnvironmentConfig,
    MergedDeployConfig,
    DropletStoredConfig,
)
from ..exceptions import ilhaCommandError


class ConfigLoader:
    """Load and validate config from ~/.ilha/config.yml + environment."""

    CONFIG_PATH = Path.home() / ".ilha" / "config.yml"

    @classmethod
    def load_global_config(cls) -> GlobalIlhaConfig:
        """Load global config file; create empty if missing.

        Returns:
            GlobalIlhaConfig instance

        Raises:
            ilhaCommandError: If config file is invalid YAML or fails validation
        """
        if not cls.CONFIG_PATH.exists():
            # No config yet — return a minimal, fully-defaulted object WITHOUT
            # running the token-required validator. A token is only needed when
            # actually deploying, which get_merged_deploy_config / get_api_token
            # enforce at the point of use (model_construct skips validators but
            # still applies field defaults).
            return GlobalIlhaConfig.model_construct()

        try:
            with open(cls.CONFIG_PATH) as f:
                data = yaml.safe_load(f) or {}
            return GlobalIlhaConfig(**data)
        except yaml.YAMLError as e:
            raise ilhaCommandError(
                f"Invalid YAML in ~/.ilha/config.yml: {e}"
            )
        except ValueError as e:
            raise ilhaCommandError(f"Config validation failed: {e}")

    @classmethod
    def load_env_config(cls) -> EnvironmentConfig:
        """Load environment configuration.

        Returns:
            EnvironmentConfig instance

        Raises:
            ilhaCommandError: If environment config is invalid
        """
        try:
            return EnvironmentConfig.from_env()
        except ValueError as e:
            raise ilhaCommandError(f"Environment config error: {e}")

    @classmethod
    def get_merged_deploy_config(
        cls,
        droplet_name: str,
        branch: str,
        cli_overrides: Optional[Dict[str, Any]] = None,
    ) -> MergedDeployConfig:
        """Merge stored + environment + CLI config for a deploy.

        Args:
            droplet_name: Name of the droplet to deploy to
            branch: Git branch to deploy
            cli_overrides: CLI argument overrides (optional)

        Returns:
            MergedDeployConfig instance with all settings resolved

        Raises:
            ilhaCommandError: If required config is missing or invalid
        """
        global_config = cls.load_global_config()
        env_config = cls.load_env_config()

        stored_droplet = global_config.droplets.get(droplet_name)
        if not stored_droplet:
            stored_droplet = DropletStoredConfig()

        # Determine API token (CLI > stored > default > env)
        api_token = None
        if cli_overrides and "api_token" in cli_overrides:
            api_token = cli_overrides["api_token"]
        if not api_token:
            api_token = stored_droplet.api_token
        if not api_token:
            api_token = global_config.default_api_token
        if not api_token:
            api_token = env_config.digitalocean_api_token

        if not api_token:
            raise ilhaCommandError(
                "No API token available. Set DIGITALOCEAN_API_TOKEN environment variable, "
                "or configure ~/.ilha/config.yml with default_api_token or per-droplet api_token."
            )

        # Merge all settings. Build the base kwargs from stored/global/env, then
        # let cli_overrides win via dict.update — spreading **cli_overrides on top
        # of explicit keyword args raised "multiple values for keyword argument"
        # whenever an override key (api_token, domain, ip, ssh_port, …) collided.
        merged_kwargs: Dict[str, Any] = dict(
            api_token=api_token,
            branch=branch,
            droplet_name=droplet_name,
            ip=stored_droplet.ip,
            domain=stored_droplet.domain,
            remote_user=stored_droplet.remote_user
            or global_config.default_remote_user,
            ssh_port=stored_droplet.ssh_port
            or global_config.default_ssh_port,
            docker_compose_file=Path(stored_droplet.docker_compose_file)
            if stored_droplet.docker_compose_file
            else None,
            environment_variables=stored_droplet.environment_variables,
            custom_build_args=stored_droplet.custom_build_args,
            auto_backup=global_config.auto_backup_before_deploy,
            health_check_timeout=global_config.health_check_timeout,
        )
        if cli_overrides:
            merged_kwargs.update(cli_overrides)

        try:
            return MergedDeployConfig(**merged_kwargs)
        except ValueError as e:
            raise ilhaCommandError(f"Merged config invalid: {e}")

    @classmethod
    def save_droplet_config(
        cls, droplet_name: str, config: DropletStoredConfig
    ) -> None:
        """Update a droplet's config in ~/.ilha/config.yml.

        Args:
            droplet_name: Name of the droplet
            config: DropletStoredConfig instance to save

        Raises:
            ilhaCommandError: If file write fails
        """
        try:
            global_config = cls.load_global_config()
            global_config.droplets[droplet_name] = config

            cls.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(cls.CONFIG_PATH, "w") as f:
                # Convert Pydantic models to dicts, excluding None values
                data = global_config.model_dump(exclude_none=True)
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            raise ilhaCommandError(
                f"Failed to save config to ~/.ilha/config.yml: {e}"
            )

    @classmethod
    def get_api_token(cls) -> Optional[str]:
        """Get API token from any available source.

        Priority order:
        1. Environment variable DIGITALOCEAN_API_TOKEN
        2. Global config default_api_token
        3. Any droplet-specific api_token

        Returns:
            API token or None if not found
        """
        env_config = cls.load_env_config()
        if env_config.digitalocean_api_token:
            return env_config.digitalocean_api_token

        global_config = cls.load_global_config()
        if global_config.default_api_token:
            return global_config.default_api_token

        # Find first droplet with api_token
        for droplet_config in global_config.droplets.values():
            if droplet_config.api_token:
                return droplet_config.api_token

        return None
