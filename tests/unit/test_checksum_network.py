"""Unit tests for ``ilha/utils/checksum.py`` and ``ilha/utils/network.py``.

Checksum tests exercise real files under ``tmp_path`` (SHA256 is deterministic,
so no mocking is needed there). Network tests monkeypatch ``socket.gethostbyname``
so no real DNS lookups occur.

BUGS FOUND: none.
"""

import hashlib
import socket

import pytest

from ilha.utils import network
from ilha.utils.checksum import (
    calculate_directory_checksum,
    calculate_file_checksum,
    verify_directory_checksum,
    verify_file_checksum,
)
from ilha.utils.network import resolve_server_ip


# ---------------------------------------------------------------------------
# calculate_file_checksum
# ---------------------------------------------------------------------------


def test_calculate_file_checksum_matches_hashlib(tmp_path):
    data = b"hello ilha checksum"
    f = tmp_path / "file.bin"
    f.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert calculate_file_checksum(f) == expected


def test_calculate_file_checksum_empty_file(tmp_path):
    f = tmp_path / "empty"
    f.write_bytes(b"")
    assert calculate_file_checksum(f) == hashlib.sha256(b"").hexdigest()


def test_calculate_file_checksum_large_multiblock(tmp_path):
    # Larger than the 4096-byte read block to exercise the streaming loop.
    data = b"a" * (4096 * 3 + 17)
    f = tmp_path / "big.bin"
    f.write_bytes(data)
    assert calculate_file_checksum(f) == hashlib.sha256(data).hexdigest()


def test_calculate_file_checksum_is_deterministic(tmp_path):
    f = tmp_path / "d.bin"
    f.write_bytes(b"repeatable")
    assert calculate_file_checksum(f) == calculate_file_checksum(f)


def test_calculate_file_checksum_differs_by_content(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"content-A")
    b.write_bytes(b"content-B")
    assert calculate_file_checksum(a) != calculate_file_checksum(b)


def test_calculate_file_checksum_missing_raises(tmp_path):
    missing = tmp_path / "nope.bin"
    with pytest.raises(FileNotFoundError):
        calculate_file_checksum(missing)


# ---------------------------------------------------------------------------
# verify_file_checksum
# ---------------------------------------------------------------------------


def test_verify_file_checksum_match(tmp_path):
    f = tmp_path / "v.bin"
    f.write_bytes(b"verify me")
    digest = hashlib.sha256(b"verify me").hexdigest()
    assert verify_file_checksum(f, digest) is True


def test_verify_file_checksum_mismatch(tmp_path):
    f = tmp_path / "v.bin"
    f.write_bytes(b"verify me")
    assert verify_file_checksum(f, "0" * 64) is False


def test_verify_file_checksum_missing_file_returns_false(tmp_path):
    missing = tmp_path / "gone.bin"
    # Missing file is swallowed and reported as a non-match, not raised.
    assert verify_file_checksum(missing, "0" * 64) is False


def test_verify_file_checksum_empty_expected(tmp_path):
    f = tmp_path / "v.bin"
    f.write_bytes(b"data")
    assert verify_file_checksum(f, "") is False


def test_verify_file_checksum_case_sensitive(tmp_path):
    f = tmp_path / "v.bin"
    f.write_bytes(b"data")
    digest = hashlib.sha256(b"data").hexdigest()
    # hexdigest is lowercase; uppercase should NOT match (string comparison).
    assert verify_file_checksum(f, digest.upper()) is False


# ---------------------------------------------------------------------------
# calculate_directory_checksum / verify_directory_checksum
# ---------------------------------------------------------------------------


def test_calculate_directory_checksum_deterministic(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"aaa")
    (tmp_path / "b.txt").write_bytes(b"bbb")
    first = calculate_directory_checksum(tmp_path)
    second = calculate_directory_checksum(tmp_path)
    assert first == second


def test_calculate_directory_checksum_changes_with_content(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"aaa")
    before = calculate_directory_checksum(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"changed")
    after = calculate_directory_checksum(tmp_path)
    assert before != after


def test_calculate_directory_checksum_respects_exclude(tmp_path):
    (tmp_path / "keep.txt").write_bytes(b"keep")
    with_excluded = tmp_path / "skip.log"
    with_excluded.write_bytes(b"noise")

    excluded = calculate_directory_checksum(tmp_path, exclude_patterns=["*.log"])

    # Removing the excluded file should not change the checksum.
    with_excluded.unlink()
    without_file = calculate_directory_checksum(tmp_path, exclude_patterns=["*.log"])
    assert excluded == without_file


def test_verify_directory_checksum_match(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"aaa")
    digest = calculate_directory_checksum(tmp_path)
    assert verify_directory_checksum(tmp_path, digest) is True


def test_verify_directory_checksum_mismatch(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"aaa")
    assert verify_directory_checksum(tmp_path, "0" * 64) is False


# ---------------------------------------------------------------------------
# resolve_server_ip
# ---------------------------------------------------------------------------


def test_resolve_server_ip_returns_dotted_quad_unchanged(monkeypatch):
    # Must not perform DNS resolution when given a dotted-quad.
    def boom(_host):
        raise AssertionError("gethostbyname should not be called for an IP")

    monkeypatch.setattr(network.socket, "gethostbyname", boom)
    assert resolve_server_ip("192.168.1.10") == "192.168.1.10"


def test_resolve_server_ip_resolves_hostname(monkeypatch):
    monkeypatch.setattr(network.socket, "gethostbyname", lambda host: "203.0.113.7")
    assert resolve_server_ip("example.com") == "203.0.113.7"


def test_resolve_server_ip_returns_input_on_dns_failure(monkeypatch):
    def fail(_host):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(network.socket, "gethostbyname", fail)
    assert resolve_server_ip("does-not-resolve.invalid") == "does-not-resolve.invalid"


def test_resolve_server_ip_passes_hostname_to_lookup(monkeypatch):
    seen = {}

    def record(host):
        seen["host"] = host
        return "198.51.100.2"

    monkeypatch.setattr(network.socket, "gethostbyname", record)
    resolve_server_ip("myhost.local")
    assert seen["host"] == "myhost.local"


@pytest.mark.parametrize(
    "value",
    ["0.0.0.0", "127.0.0.1", "255.255.255.255", "10.0.0.1"],
)
def test_resolve_server_ip_recognizes_various_ipv4(monkeypatch, value):
    monkeypatch.setattr(
        network.socket,
        "gethostbyname",
        lambda _h: (_ for _ in ()).throw(AssertionError("should not resolve an IP")),
    )
    assert resolve_server_ip(value) == value
