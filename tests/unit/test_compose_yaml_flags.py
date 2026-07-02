"""Unit tests for ilha/utils/compose_yaml.py.

Covers COMPOSE_FILES, compose_flags_list/str, compose_reset/ComposeReset,
and the PyYAML !reset tag round-trip registered by register_compose_yaml_tags.
"""

import yaml

from ilha.utils.compose_yaml import (
    COMPOSE_FILES,
    ComposeReset,
    compose_flags_list,
    compose_flags_str,
    compose_reset,
    register_compose_yaml_tags,
)


# ---------------------------------------------------------------------------
# COMPOSE_FILES constant
# ---------------------------------------------------------------------------

def test_compose_files_value():
    assert COMPOSE_FILES == ("docker-compose.yml", "docker-compose.overlay.yml")


def test_compose_files_is_tuple():
    assert isinstance(COMPOSE_FILES, tuple)


# ---------------------------------------------------------------------------
# compose_flags_list / compose_flags_str
# ---------------------------------------------------------------------------

def test_compose_flags_list():
    assert compose_flags_list() == [
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.overlay.yml",
    ]


def test_compose_flags_list_returns_new_list_each_call():
    a = compose_flags_list()
    b = compose_flags_list()
    assert a == b
    assert a is not b


def test_compose_flags_str():
    assert (
        compose_flags_str()
        == "-f docker-compose.yml -f docker-compose.overlay.yml"
    )


def test_compose_flags_str_matches_joined_list():
    assert compose_flags_str() == " ".join(compose_flags_list())


# ---------------------------------------------------------------------------
# compose_reset / ComposeReset
# ---------------------------------------------------------------------------

def test_compose_reset_default_empty():
    r = compose_reset()
    assert isinstance(r, ComposeReset)
    assert list(r) == []


def test_compose_reset_with_values():
    r = compose_reset([1, 2, 3])
    assert isinstance(r, ComposeReset)
    assert list(r) == [1, 2, 3]


def test_compose_reset_none_yields_empty():
    assert list(compose_reset(None)) == []


def test_compose_reset_is_list_subclass():
    assert isinstance(compose_reset([]), list)


# ---------------------------------------------------------------------------
# !reset tag dump / load round-trips
# ---------------------------------------------------------------------------

def test_reset_dump_emits_tag():
    data = {"ports": compose_reset([])}
    dumped = yaml.safe_dump(data)
    assert "!reset" in dumped


def test_reset_dump_flow_style_empty():
    dumped = yaml.safe_dump({"ports": compose_reset([])})
    # Empty reset renders as a flow sequence with the tag.
    assert "!reset []" in dumped


def test_reset_dump_with_values_flow_style():
    dumped = yaml.safe_dump({"ports": compose_reset(["80:80", "443:443"])})
    assert "!reset" in dumped
    # flow_style=True => bracketed inline sequence
    assert "[" in dumped and "]" in dumped


def test_reset_load_sequence_via_safeloader():
    doc = "ports: !reset [80, 443]"
    loaded = yaml.safe_load(doc)
    assert loaded == {"ports": [80, 443]}


def test_reset_load_empty_sequence():
    loaded = yaml.safe_load("ports: !reset []")
    assert loaded == {"ports": []}


def test_reset_load_mapping():
    loaded = yaml.safe_load("environment: !reset {A: 1, B: 2}")
    assert loaded == {"environment": {"A": 1, "B": 2}}


def test_reset_load_scalar():
    loaded = yaml.safe_load("value: !reset hello")
    assert loaded == {"value": "hello"}


def test_reset_round_trip_sequence():
    original = {"ports": compose_reset(["8080:80"])}
    dumped = yaml.safe_dump(original)
    reloaded = yaml.safe_load(dumped)
    # On reload the tag becomes a plain list.
    assert reloaded == {"ports": ["8080:80"]}


def test_register_is_idempotent():
    # Calling again should not raise and round-trip must still work.
    register_compose_yaml_tags()
    dumped = yaml.safe_dump({"x": compose_reset([1])})
    assert yaml.safe_load(dumped) == {"x": [1]}


def test_reset_tag_registered_on_full_loader():
    loaded = yaml.load("ports: !reset [1, 2]", Loader=yaml.FullLoader)
    assert loaded == {"ports": [1, 2]}


def test_reset_tag_registered_on_plain_loader():
    loaded = yaml.load("ports: !reset [1, 2]", Loader=yaml.Loader)
    assert loaded == {"ports": [1, 2]}


def test_reset_dump_via_plain_dumper():
    dumped = yaml.dump({"ports": compose_reset([1])}, Dumper=yaml.Dumper)
    assert "!reset" in dumped
