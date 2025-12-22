"""
Unit tests for DNS manager domain parsing functions.

Tests cover:
- parse_domain() with various domain formats
- get_base_domain() helper function
- Sub-subdomains and multi-part TLDs
- Edge cases and error handling
"""

import pytest
from ilha.core.dns_manager import parse_domain, get_base_domain


class TestParseDomain:
    """Test parse_domain() function."""

    def test_simple_subdomain(self):
        """Test parsing simple subdomain."""
        subdomain, base_domain = parse_domain("app.example.com")
        assert subdomain == "app"
        assert base_domain == "example.com"

    def test_sub_subdomain(self):
        """Test parsing sub-subdomain."""
        subdomain, base_domain = parse_domain("h2.h1.example.com")
        assert subdomain == "h2.h1"
        assert base_domain == "example.com"

    def test_multi_level_subdomain(self):
        """Test parsing multi-level subdomain."""
        subdomain, base_domain = parse_domain("level3.level2.level1.example.com")
        assert subdomain == "level3.level2.level1"
        assert base_domain == "example.com"

    def test_root_domain(self):
        """Test parsing root domain (no subdomain)."""
        subdomain, base_domain = parse_domain("example.com")
        assert subdomain == ""
        assert base_domain == "example.com"

    def test_multi_part_tld(self):
        """Test parsing domain with multi-part TLD."""
        subdomain, base_domain = parse_domain("example.co.uk")
        assert subdomain == ""
        assert base_domain == "example.co.uk"

    def test_subdomain_with_multi_part_tld(self):
        """Test parsing subdomain with multi-part TLD."""
        subdomain, base_domain = parse_domain("app.example.co.uk")
        assert subdomain == "app"
        assert base_domain == "example.co.uk"

    def test_sub_subdomain_with_multi_part_tld(self):
        """Test parsing sub-subdomain with multi-part TLD."""
        subdomain, base_domain = parse_domain("h2.h1.example.co.uk")
        assert subdomain == "h2.h1"
        assert base_domain == "example.co.uk"

    def test_other_multi_part_tlds(self):
        """Test parsing other common multi-part TLDs."""
        # .com.au
        subdomain, base_domain = parse_domain("app.example.com.au")
        assert subdomain == "app"
        assert base_domain == "example.com.au"

        # .co.nz
        subdomain, base_domain = parse_domain("site.example.co.nz")
        assert subdomain == "site"
        assert base_domain == "example.co.nz"

        # .co.za
        subdomain, base_domain = parse_domain("www.example.co.za")
        assert subdomain == "www"
        assert base_domain == "example.co.za"

    def test_complex_subdomain_with_multi_part_tld(self):
        """Test parsing complex multi-level subdomain with multi-part TLD."""
        subdomain, base_domain = parse_domain("api.v2.staging.example.co.uk")
        assert subdomain == "api.v2.staging"
        assert base_domain == "example.co.uk"

    def test_invalid_empty_string(self):
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid domain format"):
            parse_domain("")

    def test_invalid_no_dots(self):
        """Test that domain without dots raises ValueError."""
        with pytest.raises(ValueError, match="Invalid domain format"):
            parse_domain("example")

    def test_invalid_none(self):
        """Test that None raises ValueError."""
        with pytest.raises(ValueError):
            parse_domain(None)

    def test_backward_compatibility_simple(self):
        """Test backward compatibility with existing simple domains."""
        # These should work exactly as before
        subdomain, base_domain = parse_domain("app.example.com")
        assert subdomain == "app"
        assert base_domain == "example.com"

        subdomain, base_domain = parse_domain("staging.example.com")
        assert subdomain == "staging"
        assert base_domain == "example.com"


class TestGetBaseDomain:
    """Test get_base_domain() helper function."""

    def test_simple_subdomain(self):
        """Test extracting base domain from simple subdomain."""
        base_domain = get_base_domain("app.example.com")
        assert base_domain == "example.com"

    def test_sub_subdomain(self):
        """Test extracting base domain from sub-subdomain."""
        base_domain = get_base_domain("h2.h1.example.com")
        assert base_domain == "example.com"

    def test_root_domain(self):
        """Test extracting base domain from root domain."""
        base_domain = get_base_domain("example.com")
        assert base_domain == "example.com"

    def test_multi_part_tld(self):
        """Test extracting base domain with multi-part TLD."""
        base_domain = get_base_domain("example.co.uk")
        assert base_domain == "example.co.uk"

    def test_subdomain_with_multi_part_tld(self):
        """Test extracting base domain from subdomain with multi-part TLD."""
        base_domain = get_base_domain("app.example.co.uk")
        assert base_domain == "example.co.uk"

    def test_sub_subdomain_with_multi_part_tld(self):
        """Test extracting base domain from sub-subdomain with multi-part TLD."""
        base_domain = get_base_domain("h2.h1.example.co.uk")
        assert base_domain == "example.co.uk"

    def test_complex_subdomain(self):
        """Test extracting base domain from complex multi-level subdomain."""
        base_domain = get_base_domain("api.v2.staging.example.com")
        assert base_domain == "example.com"

    def test_invalid_empty_string(self):
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError):
            get_base_domain("")

    def test_invalid_no_dots(self):
        """Test that domain without dots raises ValueError."""
        with pytest.raises(ValueError):
            get_base_domain("example")

