"""Unit tests for DigitalOcean API Pydantic models."""

import pytest
from datetime import datetime

from ilha.commands.do_models import (
    DODroplet,
    DODomain,
    DomainRecord,
    DNSRecordType,
    DropletStatus,
    Region,
    Size,
    Image,
    Networks,
    NetworkInterface,
    parse_droplet_response,
    parse_droplets_response,
    parse_domain_response,
    parse_domain_record_response,
)


class TestNetworkInterface:
    """Test NetworkInterface model."""

    def test_create_public_ipv4(self):
        """Test creating public IPv4 interface."""
        iface = NetworkInterface(type="public", ip_address="192.168.1.1")
        assert iface.type == "public"
        assert iface.ip_address == "192.168.1.1"

    def test_create_private_ipv4(self):
        """Test creating private IPv4 interface."""
        iface = NetworkInterface(type="private", ip_address="10.0.0.1")
        assert iface.type == "private"
        assert iface.ip_address == "10.0.0.1"


class TestNetworks:
    """Test Networks model."""

    def test_create_empty(self):
        """Test creating empty networks."""
        nets = Networks()
        assert len(nets.v4) == 0
        assert len(nets.v6) == 0

    def test_create_with_interfaces(self):
        """Test creating networks with interfaces."""
        iface1 = NetworkInterface(type="public", ip_address="192.168.1.1")
        iface2 = NetworkInterface(type="private", ip_address="10.0.0.1")
        nets = Networks(v4=[iface1, iface2])
        assert len(nets.v4) == 2


class TestDODroplet:
    """Test DODroplet model."""

    def test_minimal_droplet(self):
        """Test creating minimal droplet."""
        droplet = DODroplet(
            id=1,
            name="test",
            memory=1024,
            vcpus=1,
            disk=25,
            status=DropletStatus.ACTIVE,
            created_at=datetime.now(),
        )
        assert droplet.id == 1
        assert droplet.name == "test"
        assert droplet.is_active()

    def test_droplet_from_dict(self):
        """Test creating droplet from API response dict."""
        data = {
            "id": 123,
            "name": "my-droplet",
            "memory": 2048,
            "vcpus": 2,
            "disk": 50,
            "status": "active",
            "created_at": "2024-01-15T10:30:00Z",
            "features": ["ipv6", "install_agent"],
            "tags": ["production"],
        }
        droplet = DODroplet(**data)
        assert droplet.id == 123
        assert droplet.name == "my-droplet"
        assert droplet.status == DropletStatus.ACTIVE
        assert "ipv6" in droplet.features
        assert "production" in droplet.tags

    def test_droplet_datetime_parsing(self):
        """Test datetime parsing from ISO 8601 string."""
        droplet = DODroplet(
            id=1,
            name="test",
            memory=1024,
            vcpus=1,
            disk=25,
            status=DropletStatus.ACTIVE,
            created_at="2024-01-15T10:30:00Z",
        )
        assert isinstance(droplet.created_at, datetime)
        assert droplet.created_at.year == 2024

    def test_get_public_ipv4(self):
        """Test extracting public IPv4 address."""
        iface_public = NetworkInterface(type="public", ip_address="192.168.1.1")
        iface_private = NetworkInterface(type="private", ip_address="10.0.0.1")
        nets = Networks(v4=[iface_public, iface_private])

        droplet = DODroplet(
            id=1,
            name="test",
            memory=1024,
            vcpus=1,
            disk=25,
            status=DropletStatus.ACTIVE,
            created_at=datetime.now(),
            networks=nets,
        )
        assert droplet.get_public_ipv4() == "192.168.1.1"

    def test_get_private_ipv4(self):
        """Test extracting private IPv4 address."""
        iface_public = NetworkInterface(type="public", ip_address="192.168.1.1")
        iface_private = NetworkInterface(type="private", ip_address="10.0.0.1")
        nets = Networks(v4=[iface_public, iface_private])

        droplet = DODroplet(
            id=1,
            name="test",
            memory=1024,
            vcpus=1,
            disk=25,
            status=DropletStatus.ACTIVE,
            created_at=datetime.now(),
            networks=nets,
        )
        assert droplet.get_private_ipv4() == "10.0.0.1"

    def test_get_ipv4_when_no_networks(self):
        """Test IP extraction when networks is None."""
        droplet = DODroplet(
            id=1,
            name="test",
            memory=1024,
            vcpus=1,
            disk=25,
            status=DropletStatus.ACTIVE,
            created_at=datetime.now(),
            networks=None,
        )
        assert droplet.get_public_ipv4() is None
        assert droplet.get_private_ipv4() is None

    def test_is_active(self):
        """Test is_active method."""
        active_droplet = DODroplet(
            id=1,
            name="active",
            memory=1024,
            vcpus=1,
            disk=25,
            status=DropletStatus.ACTIVE,
            created_at=datetime.now(),
        )
        assert active_droplet.is_active()

        inactive_droplet = DODroplet(
            id=2,
            name="off",
            memory=1024,
            vcpus=1,
            disk=25,
            status=DropletStatus.OFF,
            created_at=datetime.now(),
        )
        assert not inactive_droplet.is_active()


class TestDomainRecord:
    """Test DomainRecord model."""

    def test_a_record(self):
        """Test creating A record."""
        record = DomainRecord(
            id=1,
            type=DNSRecordType.A,
            name="www",
            data="192.168.1.1",
        )
        assert record.type == DNSRecordType.A
        assert record.name == "www"
        assert record.data == "192.168.1.1"
        assert record.ttl == 3600

    def test_cname_record(self):
        """Test creating CNAME record."""
        record = DomainRecord(
            id=2,
            type=DNSRecordType.CNAME,
            name="alias",
            data="www.example.com.",
        )
        assert record.type == DNSRecordType.CNAME

    def test_mx_record(self):
        """Test creating MX record."""
        record = DomainRecord(
            id=3,
            type=DNSRecordType.MX,
            name="@",
            data="mail.example.com.",
            priority=10,
        )
        assert record.type == DNSRecordType.MX
        assert record.priority == 10

    def test_custom_ttl(self):
        """Test custom TTL value."""
        record = DomainRecord(
            id=1,
            type=DNSRecordType.A,
            name="test",
            data="1.2.3.4",
            ttl=7200,
        )
        assert record.ttl == 7200

    def test_ttl_validation(self):
        """Test TTL validation."""
        with pytest.raises(ValueError, match="TTL must be non-negative"):
            DomainRecord(
                id=1,
                type=DNSRecordType.A,
                name="test",
                data="1.2.3.4",
                ttl=-1,
            )


class TestDODomain:
    """Test DODomain model."""

    def test_create_domain(self):
        """Test creating domain."""
        domain = DODomain(name="example.com")
        assert domain.name == "example.com"
        assert domain.ttl == 3600
        assert len(domain.records) == 0

    def test_domain_with_records(self):
        """Test creating domain with records."""
        records = [
            DomainRecord(id=1, type=DNSRecordType.A, name="www", data="1.2.3.4"),
            DomainRecord(
                id=2, type=DNSRecordType.MX, name="@", data="mail.example.com.", priority=10
            ),
        ]
        domain = DODomain(name="example.com", records=records)
        assert len(domain.records) == 2
        assert domain.records[0].type == DNSRecordType.A


class TestParsingFunctions:
    """Test parsing utility functions."""

    def test_parse_droplet_response_valid(self):
        """Test parsing valid droplet response."""
        data = {
            "id": 123,
            "name": "test-droplet",
            "memory": 1024,
            "vcpus": 1,
            "disk": 25,
            "status": "active",
            "created_at": "2024-01-15T10:00:00Z",
        }
        droplet = parse_droplet_response(data)
        assert droplet is not None
        assert droplet.id == 123
        assert droplet.name == "test-droplet"

    def test_parse_droplet_response_invalid(self):
        """Test parsing invalid droplet response returns None."""
        data = {"id": "not-an-int"}  # Invalid - id should be int
        droplet = parse_droplet_response(data)
        assert droplet is None

    def test_parse_droplets_response(self):
        """Test parsing multiple droplets."""
        data = [
            {
                "id": 1,
                "name": "droplet1",
                "memory": 1024,
                "vcpus": 1,
                "disk": 25,
                "status": "active",
                "created_at": "2024-01-15T10:00:00Z",
            },
            {
                "id": 2,
                "name": "droplet2",
                "memory": 2048,
                "vcpus": 2,
                "disk": 50,
                "status": "active",
                "created_at": "2024-01-15T10:00:00Z",
            },
        ]
        droplets = parse_droplets_response(data)
        assert len(droplets) == 2
        assert droplets[0].id == 1
        assert droplets[1].id == 2

    def test_parse_droplets_response_with_invalid(self):
        """Test parsing droplets skips invalid entries."""
        data = [
            {
                "id": 1,
                "name": "valid",
                "memory": 1024,
                "vcpus": 1,
                "disk": 25,
                "status": "active",
                "created_at": "2024-01-15T10:00:00Z",
            },
            {"id": "invalid"},  # Invalid
        ]
        droplets = parse_droplets_response(data)
        assert len(droplets) == 1  # Only valid entry included

    def test_parse_domain_record_response(self):
        """Test parsing domain record response."""
        data = {
            "id": 1,
            "type": "A",
            "name": "www",
            "data": "192.168.1.1",
        }
        record = parse_domain_record_response(data)
        assert record is not None
        assert record.type == DNSRecordType.A
        assert record.name == "www"

    def test_parse_domain_record_response_invalid(self):
        """Test parsing invalid domain record returns None."""
        data = {"id": "not-an-int"}  # Invalid
        record = parse_domain_record_response(data)
        assert record is None
