"""Unit tests for Pydantic config models."""

import pytest
from pathlib import Path

from ilha.commands.config_models import (
    GlobalIlhaConfig,
    DropletStoredConfig,
    EnvironmentConfig,
    MergedDeployConfig,
)


class TestDropletStoredConfig:
    """Test DropletStoredConfig model."""

    def test_create_minimal(self):
        """Test creating minimal droplet config."""
        config = DropletStoredConfig()
        assert config.branch is None
        assert config.ip is None
        # Unset remote_user/ssh_port default to None so they inherit the global
        # default_remote_user / default_ssh_port at merge time (the effective
        # "root"/22 fallback lives in MergedDeployConfig).
        assert config.remote_user is None
        assert config.ssh_port is None

    def test_create_full(self):
        """Test creating droplet config with all fields."""
        config = DropletStoredConfig(
            branch="main",
            ip="192.168.1.1",
            domain="example.com",
            api_token="token-123",
            remote_user="ubuntu",
            ssh_port=2222,
            docker_compose_file="docker-compose.prod.yml",
        )
        assert config.branch == "main"
        assert config.ip == "192.168.1.1"
        assert config.domain == "example.com"
        assert config.api_token == "token-123"
        assert config.remote_user == "ubuntu"
        assert config.ssh_port == 2222

    def test_valid_ip_validation(self):
        """Test that valid IPs are accepted."""
        config = DropletStoredConfig(ip="10.0.0.1")
        assert config.ip == "10.0.0.1"

        config = DropletStoredConfig(ip="2001:db8::1")
        assert config.ip == "2001:db8::1"

    def test_invalid_ip_validation(self):
        """Test that invalid IPs are rejected."""
        with pytest.raises(ValueError, match="Invalid IP address"):
            DropletStoredConfig(ip="not-an-ip")

        with pytest.raises(ValueError, match="Invalid IP address"):
            DropletStoredConfig(ip="999.999.999.999")

    def test_ssh_port_validation(self):
        """Test SSH port bounds validation."""
        # Valid ports
        config = DropletStoredConfig(ssh_port=22)
        assert config.ssh_port == 22

        config = DropletStoredConfig(ssh_port=2222)
        assert config.ssh_port == 2222

        # Invalid ports
        with pytest.raises(ValueError):
            DropletStoredConfig(ssh_port=0)

        with pytest.raises(ValueError):
            DropletStoredConfig(ssh_port=99999)


class TestGlobalIlhaConfig:
    """Test GlobalIlhaConfig model."""

    def test_create_minimal(self):
        """Test creating minimal global config."""
        # A token (default or per-droplet) is required by validate_api_token_available.
        config = GlobalIlhaConfig(default_api_token="token")
        assert config.version == "1.0"
        assert config.default_region == "nyc3"
        assert len(config.droplets) == 0

    def test_create_with_default_token(self):
        """Test creating config with default API token."""
        config = GlobalIlhaConfig(default_api_token="global-token")
        assert config.default_api_token == "global-token"

    def test_api_token_validation_passes_with_default(self):
        """Test validation passes when default token exists."""
        config = GlobalIlhaConfig(
            default_api_token="token",
            droplets={},
        )
        # Should not raise
        assert config.default_api_token == "token"

    def test_api_token_validation_passes_with_droplet_token(self):
        """Test validation passes when droplet token exists."""
        config = GlobalIlhaConfig(
            droplets={
                "staging": DropletStoredConfig(api_token="token"),
            }
        )
        # Should not raise
        assert config.droplets["staging"].api_token == "token"

    def test_api_token_validation_fails_without_any_token(self):
        """Test validation fails when no token available."""
        with pytest.raises(ValueError, match="No API token configured"):
            GlobalIlhaConfig(
                default_api_token=None,
                droplets={},
            )

    def test_health_check_timeout_validation(self):
        """Test health check timeout bounds."""
        config = GlobalIlhaConfig(default_api_token="token", health_check_timeout=300)
        assert config.health_check_timeout == 300

        with pytest.raises(ValueError):
            GlobalIlhaConfig(default_api_token="token", health_check_timeout=10)  # Too low

        with pytest.raises(ValueError):
            GlobalIlhaConfig(default_api_token="token", health_check_timeout=10000)  # Too high


class TestEnvironmentConfig:
    """Test EnvironmentConfig model."""

    def test_create_from_env_minimal(self, monkeypatch):
        """Test creating environment config with minimal env vars."""
        monkeypatch.delenv("DIGITALOCEAN_API_TOKEN", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)

        config = EnvironmentConfig.from_env()
        assert config.digitalocean_api_token is None
        assert config.debug is False
        assert config.log_level == "INFO"

    def test_create_from_env_with_token(self, monkeypatch):
        """Test creating environment config with API token."""
        monkeypatch.setenv("DIGITALOCEAN_API_TOKEN", "env-token")
        config = EnvironmentConfig.from_env()
        assert config.digitalocean_api_token == "env-token"

    def test_create_from_env_debug_flag(self, monkeypatch):
        """Test debug flag parsing."""
        monkeypatch.setenv("DEBUG", "true")
        config = EnvironmentConfig.from_env()
        assert config.debug is True

        monkeypatch.setenv("DEBUG", "1")
        config = EnvironmentConfig.from_env()
        assert config.debug is True

        monkeypatch.setenv("DEBUG", "false")
        config = EnvironmentConfig.from_env()
        assert config.debug is False


class TestMergedDeployConfig:
    """Test MergedDeployConfig model."""

    def test_create_minimal(self):
        """Test creating minimal deploy config."""
        config = MergedDeployConfig(
            api_token="token",
            branch="main",
            droplet_name="staging",
        )
        assert config.api_token == "token"
        assert config.branch == "main"
        assert config.droplet_name == "staging"
        assert config.remote_user == "root"
        assert config.ssh_port == 22

    def test_api_token_validation(self):
        """Test API token is required."""
        with pytest.raises(ValueError, match="api_token is required"):
            MergedDeployConfig(api_token="", branch="main", droplet_name="staging")

    def test_branch_validation(self):
        """Test branch is required."""
        with pytest.raises(ValueError, match="branch is required"):
            MergedDeployConfig(api_token="token", branch="", droplet_name="staging")

    def test_with_all_fields(self):
        """Test creating deploy config with all fields."""
        config = MergedDeployConfig(
            api_token="token",
            branch="feature/x",
            droplet_name="prod",
            ip="10.0.0.1",
            domain="example.com",
            remote_user="deploy",
            ssh_port=2222,
            docker_compose_file=Path("/tmp/docker-compose.yml"),
            auto_backup=False,
            health_check_timeout=600,
        )
        assert config.branch == "feature/x"
        assert config.ip == "10.0.0.1"
        assert config.remote_user == "deploy"
        assert config.ssh_port == 2222
        assert config.auto_backup is False
        assert config.health_check_timeout == 600

    def test_health_check_timeout_validation(self):
        """Test health check timeout bounds."""
        config = MergedDeployConfig(
            api_token="token",
            branch="main",
            droplet_name="staging",
            health_check_timeout=300,
        )
        assert config.health_check_timeout == 300

        with pytest.raises(ValueError):
            MergedDeployConfig(
                api_token="token",
                branch="main",
                droplet_name="staging",
                health_check_timeout=10,  # Too low
            )


class TestConfigPydanticFeatures:
    """Test Pydantic-specific features."""

    def test_model_dump(self):
        """Test model serialization."""
        config = DropletStoredConfig(branch="main", ip="10.0.0.1")
        data = config.model_dump()
        assert data["branch"] == "main"
        assert data["ip"] == "10.0.0.1"
        # Unset remote_user is None (inherits global default_remote_user at merge).
        assert data["remote_user"] is None

    def test_model_dump_exclude_none(self):
        """Test model serialization excluding None values."""
        config = DropletStoredConfig(branch="main")
        data = config.model_dump(exclude_none=True)
        assert data["branch"] == "main"
        assert "ip" not in data  # None values excluded
        assert "domain" not in data

    def test_model_json_schema(self):
        """Test JSON schema generation."""
        schema = DropletStoredConfig.model_json_schema()
        assert "properties" in schema
        assert "branch" in schema["properties"]
        assert "ssh_port" in schema["properties"]
