"""Tests for `ilha domains zones` (DigitalOcean DNS zone listing)."""

from __future__ import annotations

from ilha.commands.domains import DomainCommands
from ilha.core.dns_providers.digitalocean import DigitalOceanProvider


def test_list_domain_zones_delegates_to_collect_paginated(monkeypatch):
    fake_domains = [
        {"name": "alpha.example.com", "ttl": 1800},
        {"name": "beta.example.org", "ttl": 3600},
    ]

    def fake_collect(self, endpoint: str, key: str):
        assert endpoint == "/domains"
        assert key == "domains"
        return list(fake_domains)

    monkeypatch.setattr(
        DigitalOceanProvider,
        "_collect_paginated",
        fake_collect,
    )

    provider = DigitalOceanProvider(api_token="fake-token")
    zones = provider.list_domain_zones()
    assert zones == fake_domains

    names = provider.list_all_domains()
    assert names == ["alpha.example.com", "beta.example.org"]


def test_domain_commands_list_zones_json(monkeypatch, capsys):
    def fake_resolve(_token=None):
        return "tok"

    def fake_create(_name, _tok):
        return object()

    class FakeProvider:
        def list_domain_zones(self):
            return [
                {"name": "z1.com", "ttl": 1800},
                {"name": "z2.net", "ttl": 3600},
            ]

    def fake_create_provider(name: str, token: str):
        assert name == "digitalocean"
        return FakeProvider()

    monkeypatch.setattr("ilha.commands.domains.DNSManager.resolve_dns_token", fake_resolve)
    monkeypatch.setattr("ilha.commands.domains.DNSManager.create_provider", fake_create_provider)

    mgr = DomainCommands()
    ok = mgr.list_zones(dns_token=None, json=True, csv=False)
    assert ok is True

    out = capsys.readouterr().out
    assert '"success": true' in out
    assert '"count": 2' in out
    assert "z1.com" in out and "z2.net" in out


def test_domain_commands_list_zones_empty_plain(monkeypatch, capsys):
    monkeypatch.setattr(
        "ilha.commands.domains.DNSManager.resolve_dns_token",
        lambda _t=None: "x",
    )

    class EmptyProvider:
        def list_domain_zones(self):
            return []

    monkeypatch.setattr(
        "ilha.commands.domains.DNSManager.create_provider",
        lambda _n, _t: EmptyProvider(),
    )

    mgr = DomainCommands()
    ok = mgr.list_zones(json=False, csv=False)
    assert ok is True
    assert "No DNS zones found" in capsys.readouterr().out
