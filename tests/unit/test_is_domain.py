"""
Regression tests for CaddyDynamicConfig._is_domain().

The key invariant: .localhost domains must NOT be treated as real domains,
otherwise Caddy enables HTTPS and browsers reject the self-signed cert.
"""

import importlib
import pytest

# The script uses hyphens in its filename, so we need importlib.
_mod = importlib.import_module("ilha.scripts.caddy-dynamic-config")
CaddyDynamicConfig = _mod.CaddyDynamicConfig


@pytest.fixture
def config():
    """CaddyDynamicConfig with docker/caddy deps stubbed out."""
    obj = object.__new__(CaddyDynamicConfig)
    return obj


class TestIsDomain:
    """_is_domain must distinguish real domains from local/dev hosts."""

    # --- Real domains: should return True ---

    @pytest.mark.parametrize("host", [
        "example.com",
        "app.example.com",
        "staging.mysite.io",
        "deep.sub.domain.co.uk",
    ])
    def test_real_domains(self, config, host):
        assert config._is_domain(host) is True

    # --- Localhost variants: should return False ---

    @pytest.mark.parametrize("host", [
        "localhost",
        "business-intelligence-staging.localhost",
        "myproject-feature.localhost",
        "a.b.c.localhost",
    ])
    def test_localhost_variants(self, config, host):
        assert config._is_domain(host) is False

    # --- IPs: should return False ---

    @pytest.mark.parametrize("host", [
        "127.0.0.1",
        "192.168.1.100",
        "10.0.0.1",
    ])
    def test_ip_addresses(self, config, host):
        assert config._is_domain(host) is False

    # --- Edge cases ---

    def test_dotless_host(self, config):
        """A single-label host (no dots) is not a domain."""
        assert config._is_domain("myhost") is False

    def test_leading_dot(self, config):
        assert config._is_domain(".example.com") is False
