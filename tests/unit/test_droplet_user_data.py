"""Tests for DigitalOcean droplet user_data support."""

from __future__ import annotations

from ilha.core.dns_providers.digitalocean import DigitalOceanProvider


def test_create_droplet_includes_user_data_in_payload(monkeypatch):
    captured = {}

    def fake_request(method, path, json=None, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = json
        response = type("Resp", (), {})()
        response.json = lambda: {
            "droplet": {
                "id": 99,
                "name": "worker-staging-1",
                "status": "new",
                "region": {"slug": "nyc1"},
                "size_slug": "s-1vcpu-1gb",
                "image": {"slug": "ubuntu-22-04-x64"},
                "tags": ["worker"],
                "networks": {"v4": []},
            }
        }
        return response

    provider = DigitalOceanProvider(api_token="token")
    monkeypatch.setattr(provider, "_resolve_vpc_uuid", lambda region, vpc_uuid: None)
    monkeypatch.setattr(provider, "_make_request", fake_request)

    result = provider.create_droplet(
        name="worker-staging-1",
        region="nyc1",
        size="s-1vcpu-1gb",
        image="ubuntu-22-04-x64",
        user_data="#cloud-config\nruncmd: echo hi",
    )

    assert result is not None
    assert captured["json"]["user_data"] == "#cloud-config\nruncmd: echo hi"
