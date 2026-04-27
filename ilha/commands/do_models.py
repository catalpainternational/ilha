"""Pydantic models for DigitalOcean API responses."""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


# Enums for type-safe values

class RegionSlug(str, Enum):
    """DigitalOcean region identifiers."""

    NYC1 = "nyc1"
    NYC3 = "nyc3"
    SFO1 = "sfo1"
    SFO2 = "sfo2"
    SFO3 = "sfo3"
    LON1 = "lon1"
    AMS3 = "ams3"
    TOR1 = "tor1"
    SGP1 = "sgp1"
    BLR1 = "blr1"


class SizeSlug(str, Enum):
    """DigitalOcean size identifiers."""

    S512MB = "512mb"
    S1GB = "1gb"
    S2GB = "2gb"
    S4GB = "4gb"
    S8GB = "8gb"
    S16GB = "16gb"
    S32GB = "32gb"
    S48GB = "48gb"
    S64GB = "64gb"


class DropletStatus(str, Enum):
    """Droplet operational status."""

    NEW = "new"
    ACTIVE = "active"
    ARCHIVED = "archived"
    OFF = "off"


class ImageType(str, Enum):
    """Image type."""

    SNAPSHOT = "snapshot"
    BACKUP = "backup"
    CUSTOM = "custom"


class DNSRecordType(str, Enum):
    """DNS record type."""

    A = "A"
    AAAA = "AAAA"
    CNAME = "CNAME"
    MX = "MX"
    NS = "NS"
    SOA = "SOA"
    SRV = "SRV"
    TXT = "TXT"


class FirewallStatus(str, Enum):
    """Firewall status."""

    NEW = "new"
    ACTIVE = "active"
    ARCHIVED = "archived"


# Core Models


class Region(BaseModel):
    """DigitalOcean region."""

    name: str
    slug: RegionSlug
    available: bool = True
    features: List[str] = Field(default_factory=list)
    sizes: List[str] = Field(default_factory=list)


class Size(BaseModel):
    """DigitalOcean droplet size."""

    slug: SizeSlug
    memory: int = Field(description="Memory in MB")
    vcpus: int = Field(description="Number of vCPUs")
    disk: int = Field(description="Disk space in GB")
    transfer: float = Field(description="Transfer allowance in TB")
    price_monthly: float
    price_hourly: float
    available: bool = True
    description: str = ""


class Image(BaseModel):
    """DigitalOcean image."""

    id: int
    name: str
    type: ImageType
    distribution: str
    slug: Optional[str] = None
    public: bool
    regions: List[str] = Field(default_factory=list)
    size_gigabytes: float
    created_at: Optional[datetime] = None
    min_disk_size: int = 0
    description: str = ""

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_datetime(cls, v: Any) -> Optional[datetime]:
        """Parse ISO 8601 datetime string."""
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


class NetworkInterface(BaseModel):
    """Single network interface."""

    type: str  # "public" or "private"
    ip_address: str
    netmask: Optional[str] = None
    gateway: Optional[str] = None


class Networks(BaseModel):
    """Droplet network interfaces."""

    v4: List[NetworkInterface] = Field(default_factory=list)
    v6: List[Dict[str, Any]] = Field(default_factory=list)


class DODroplet(BaseModel):
    """DigitalOcean droplet (comprehensive model from API)."""

    id: int
    name: str
    memory: int
    vcpus: int
    disk: int
    locked: bool = False
    status: DropletStatus
    kernel: Optional[Dict[str, Any]] = None
    created_at: datetime
    features: List[str] = Field(default_factory=list)
    backup_ids: List[int] = Field(default_factory=list)
    snapshot_ids: List[int] = Field(default_factory=list)
    image: Optional[Image] = None
    size: Optional[Size] = None
    size_slug: Optional[str] = None
    networks: Optional[Networks] = None
    region: Optional[Region] = None
    tags: List[str] = Field(default_factory=list)
    vpc_uuid: Optional[str] = None

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_created_at(cls, v: Any) -> datetime:
        """Parse ISO 8601 datetime string."""
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

    def get_public_ipv4(self) -> Optional[str]:
        """Extract primary public IPv4 address."""
        if not self.networks:
            return None
        for net in self.networks.v4:
            if net.type == "public":
                return net.ip_address
        return None

    def get_private_ipv4(self) -> Optional[str]:
        """Extract primary private IPv4 address."""
        if not self.networks:
            return None
        for net in self.networks.v4:
            if net.type == "private":
                return net.ip_address
        return None

    def is_active(self) -> bool:
        """Check if droplet is in active status."""
        return self.status == DropletStatus.ACTIVE


class DomainRecord(BaseModel):
    """DNS record in a DigitalOcean domain."""

    id: int
    type: DNSRecordType
    name: str
    data: str
    priority: Optional[int] = None
    port: Optional[int] = None
    ttl: int = 3600
    weight: Optional[int] = None
    flags: Optional[int] = None
    tag: Optional[str] = None

    @field_validator("ttl")
    @classmethod
    def validate_ttl(cls, v: int) -> int:
        """Validate TTL is within reasonable range."""
        if v < 0:
            raise ValueError(f"TTL must be non-negative, got {v}")
        return v


class DODomain(BaseModel):
    """DigitalOcean domain (comprehensive model from API)."""

    name: str
    ttl: int = 3600
    zone_file: Optional[str] = None
    records: List[DomainRecord] = Field(default_factory=list)

    @field_validator("ttl")
    @classmethod
    def validate_ttl(cls, v: int) -> int:
        """Validate TTL is within reasonable range."""
        if v < 0:
            raise ValueError(f"TTL must be non-negative, got {v}")
        return v


class SSHKey(BaseModel):
    """DigitalOcean SSH key."""

    id: int
    name: str
    public_key: str
    fingerprint: str


class FirewallRule(BaseModel):
    """Single firewall rule."""

    protocol: str  # "tcp", "udp", "icmp"
    ports: Optional[str] = None  # "80", "8000-9000", None for all
    sources: Optional[Dict[str, Any]] = None
    destinations: Optional[Dict[str, Any]] = None


class Firewall(BaseModel):
    """DigitalOcean firewall."""

    id: str
    name: str
    status: FirewallStatus
    inbound_rules: List[FirewallRule] = Field(default_factory=list)
    outbound_rules: List[FirewallRule] = Field(default_factory=list)
    droplet_ids: List[int] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_datetime(cls, v: Any) -> Optional[datetime]:
        """Parse ISO 8601 datetime string."""
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


# Response wrappers


class SingleResourceResponse(BaseModel):
    """Generic single-resource API response."""

    droplet: Optional[DODroplet] = None
    domain: Optional[DODomain] = None
    domain_record: Optional[DomainRecord] = None
    ssh_key: Optional[SSHKey] = None
    firewall: Optional[Firewall] = None
    image: Optional[Image] = None


class ListResourcesResponse(BaseModel):
    """Generic list API response."""

    droplets: List[DODroplet] = Field(default_factory=list)
    domains: List[DODomain] = Field(default_factory=list)
    domain_records: List[DomainRecord] = Field(default_factory=list)
    ssh_keys: List[SSHKey] = Field(default_factory=list)
    firewalls: List[Firewall] = Field(default_factory=list)
    images: List[Image] = Field(default_factory=list)
    regions: List[Region] = Field(default_factory=list)
    sizes: List[Size] = Field(default_factory=list)
    links: Optional[Dict[str, str]] = None
    meta: Optional[Dict[str, int]] = None


class DOAPIError(BaseModel):
    """DigitalOcean API error response."""

    id: str = Field(description="Error ID")
    message: str = Field(description="Error message")
    request_id: Optional[str] = None
    errors: Optional[List[Dict[str, str]]] = None

    def __str__(self) -> str:
        """Human-readable error message."""
        return f"API Error {self.id}: {self.message}"


# Parsing utilities

def parse_droplet_response(data: Dict[str, Any]) -> Optional[DODroplet]:
    """Parse a single droplet from API response data.

    Args:
        data: Droplet data dict from API

    Returns:
        DODroplet instance or None if parsing fails
    """
    try:
        return DODroplet(**data)
    except ValueError as e:
        # Validation error - return None rather than raise
        return None


def parse_droplets_response(data: List[Dict[str, Any]]) -> List[DODroplet]:
    """Parse multiple droplets from API response data.

    Args:
        data: List of droplet data dicts from API

    Returns:
        List of DODroplet instances (invalid entries are skipped)
    """
    droplets = []
    for item in data:
        parsed = parse_droplet_response(item)
        if parsed:
            droplets.append(parsed)
    return droplets


def parse_domain_response(data: Dict[str, Any]) -> Optional[DODomain]:
    """Parse a single domain from API response data.

    Args:
        data: Domain data dict from API

    Returns:
        DODomain instance or None if parsing fails
    """
    try:
        return DODomain(**data)
    except ValueError:
        return None


def parse_domain_record_response(data: Dict[str, Any]) -> Optional[DomainRecord]:
    """Parse a single domain record from API response data.

    Args:
        data: Domain record data dict from API

    Returns:
        DomainRecord instance or None if parsing fails
    """
    try:
        return DomainRecord(**data)
    except ValueError:
        return None
