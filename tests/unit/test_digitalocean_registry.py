"""Unit tests for DigitalOcean registry API client."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ilha.core.digitalocean_registry import DigitalOceanRegistryClient
from ilha.exceptions import ilhaCommandError


def test_docker_credentials_parses_auths_entry():
    client = DigitalOceanRegistryClient("token")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "auths": {
            "registry.digitalocean.com": {
                "username": "user",
                "password": "pass",
            }
        }
    }
    client._request = MagicMock(return_value=response)  # type: ignore[method-assign]

    creds = client.docker_credentials("catalpa")
    assert creds == {"username": "user", "password": "pass"}


def test_delete_tag_requires_204():
    client = DigitalOceanRegistryClient("token")
    response = MagicMock()
    response.status_code = 204
    client._request = MagicMock(return_value=response)  # type: ignore[method-assign]

    client.delete_tag("catalpa", "bower/web", "staging-abc")


def test_list_repositories_returns_names():
    client = DigitalOceanRegistryClient("token")
    client._collect_paginated = MagicMock(  # type: ignore[method-assign]
        return_value=[{"name": "bower/web"}, {"name": ""}]
    )
    assert client.list_repositories("catalpa") == ["bower/web"]


def test_docker_credentials_parses_auth_blob():
    import base64

    client = DigitalOceanRegistryClient("token")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "auths": {
            "registry.digitalocean.com": {
                "auth": base64.b64encode(b"user:secret-token").decode("ascii"),
            }
        }
    }
    client._request = MagicMock(return_value=response)  # type: ignore[method-assign]

    creds = client.docker_credentials("catalpa")
    assert creds == {"username": "user", "password": "secret-token"}


def test_docker_credentials_missing_password_raises():
    client = DigitalOceanRegistryClient("token")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"auths": {"registry.digitalocean.com": {"username": "user"}}}
    client._request = MagicMock(return_value=response)  # type: ignore[method-assign]

    with pytest.raises(ilhaCommandError, match="missing username or password"):
        client.docker_credentials("catalpa")
