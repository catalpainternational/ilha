"""
Comprehensive, deterministic unit tests for ilha/core/dns_manager.py pure logic.

Covers:
- parse_domain: subdomains, multi-part TLDs, root domains, errors
- get_base_domain: registrable-domain extraction (via tldextract)
- is_domain: IPs / localhost are NOT domains; single vs multi label

No network is used: tldextract works offline against its bundled/cached suffix
list, and these tests assert only on structural parsing, not live lookups.
"""

import pytest

from ilha.core.dns_manager import parse_domain, get_base_domain, is_domain


# ---------------------------------------------------------------------------
# parse_domain
# ---------------------------------------------------------------------------
class TestParseDomain:
    def test_simple_subdomain(self):
        assert parse_domain("app.example.com") == ("app", "example.com")

    def test_root_domain_has_empty_subdomain(self):
        assert parse_domain("example.com") == ("", "example.com")

    def test_two_level_subdomain(self):
        assert parse_domain("h2.h1.example.com") == ("h2.h1", "example.com")

    def test_deep_subdomain(self):
        assert parse_domain("a.b.c.example.com") == ("a.b.c", "example.com")

    def test_multi_part_tld_root(self):
        assert parse_domain("example.co.uk") == ("", "example.co.uk")

    def test_multi_part_tld_with_subdomain(self):
        assert parse_domain("app.example.co.uk") == ("app", "example.co.uk")

    def test_multi_part_tld_with_deep_subdomain(self):
        assert parse_domain("h2.h1.example.co.uk") == ("h2.h1", "example.co.uk")

    def test_org_tld(self):
        assert parse_domain("example.org") == ("", "example.org")

    def test_returns_tuple_of_two_strings(self):
        result = parse_domain("app.example.com")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(x, str) for x in result)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_domain("")

    def test_none_raises(self):
        with pytest.raises(ValueError):
            parse_domain(None)  # type: ignore[arg-type]

    def test_single_label_no_dot_raises(self):
        with pytest.raises(ValueError):
            parse_domain("example")

    def test_localhost_no_dot_raises(self):
        with pytest.raises(ValueError):
            parse_domain("localhost")

    def test_leading_dot_normalizes_to_root(self):
        # tldextract tolerates the empty leading label.
        assert parse_domain(".example.com") == ("", "example.com")

    def test_dev_style_local_tld_preserved(self):
        # Unknown / private-style TLD: whole thing kept as base domain.
        assert parse_domain("test.local") == ("", "test.local")

    def test_foo_localhost_treated_as_base(self):
        # 'localhost' is not a public suffix here, so foo.localhost is the base.
        assert parse_domain("foo.localhost") == ("", "foo.localhost")


# ---------------------------------------------------------------------------
# get_base_domain
# ---------------------------------------------------------------------------
class TestGetBaseDomain:
    def test_simple(self):
        assert get_base_domain("app.example.com") == "example.com"

    def test_root(self):
        assert get_base_domain("example.com") == "example.com"

    def test_deep_subdomain(self):
        assert get_base_domain("a.b.c.example.com") == "example.com"

    def test_multi_part_tld(self):
        assert get_base_domain("app.example.co.uk") == "example.co.uk"

    def test_multi_part_tld_root(self):
        assert get_base_domain("example.co.uk") == "example.co.uk"

    def test_matches_parse_domain_second_element(self):
        for d in ["app.example.com", "example.org", "h2.h1.example.co.uk"]:
            assert get_base_domain(d) == parse_domain(d)[1]

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            get_base_domain("nodot")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            get_base_domain("")


# ---------------------------------------------------------------------------
# is_domain
# ---------------------------------------------------------------------------
class TestIsDomain:
    # --- Positive cases: real domains ---
    def test_simple_domain_is_domain(self):
        assert is_domain("example.com") is True

    def test_subdomain_is_domain(self):
        assert is_domain("app.example.com") is True

    def test_deep_subdomain_is_domain(self):
        assert is_domain("a.b.c.example.com") is True

    def test_multi_part_tld_is_domain(self):
        assert is_domain("example.co.uk") is True

    def test_dev_local_is_domain(self):
        assert is_domain("test.local") is True

    # --- Negative cases: IPs ---
    def test_ipv4_is_not_domain(self):
        assert is_domain("192.168.1.1") is False

    def test_ipv4_loopback_is_not_domain(self):
        assert is_domain("127.0.0.1") is False

    def test_ipv4_ten_range_is_not_domain(self):
        assert is_domain("10.0.0.1") is False

    def test_ipv4_public_is_not_domain(self):
        assert is_domain("8.8.8.8") is False

    # --- Negative cases: localhost ---
    def test_localhost_is_not_domain(self):
        assert is_domain("localhost") is False

    def test_localhost_with_port_is_not_domain(self):
        assert is_domain("localhost:8000") is False

    def test_loopback_with_port_is_not_domain(self):
        assert is_domain("127.0.0.1:8000") is False

    # --- Negative cases: structural ---
    def test_single_label_is_not_domain(self):
        assert is_domain("myhost") is False

    def test_leading_dot_is_not_domain(self):
        assert is_domain(".example.com") is False

    def test_empty_string_is_not_domain(self):
        assert is_domain("") is False

    # --- Boundary / documentation of current behavior ---
    def test_uppercase_localhost_single_label_not_domain(self):
        # No dot present -> not a domain (regardless of case).
        assert is_domain("LOCALHOST") is False

    def test_sub_localhost_is_domain(self):
        # 'sub.localhost' does not start with 'localhost' -> treated as a domain.
        assert is_domain("sub.localhost") is True

    def test_localhost_prefixed_domain_is_still_a_domain(self):
        assert is_domain("localhost.example.com") is True

    def test_ip_prefixed_domain_is_still_a_domain(self):
        assert is_domain("127.0.0.1.nip.io") is True
