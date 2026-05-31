"""Unit tests for env_loader parsing and merge helpers."""

from __future__ import annotations

from pathlib import Path

from ilha.utils.env_loader import (
    format_env_text,
    is_sensitive_env_key,
    merge_env_layers,
    parse_env_text,
    sensitive_env_keys,
)


def test_parse_env_text_strips_quotes_and_comments():
    text = """
    # comment
    FOO=bar
    QUOTED="hello world"
    SINGLE='x'
    """
    assert parse_env_text(text) == {
        "FOO": "bar",
        "QUOTED": "hello world",
        "SINGLE": "x",
    }


def test_merge_env_layers_later_wins(tmp_path: Path):
    first = tmp_path / "a.env"
    second = tmp_path / "b.env"
    first.write_text("FOO=1\nBAR=old\n")
    second.write_text("BAR=new\n")
    merged = merge_env_layers(first, second, {"BAZ": "3"})
    assert merged == {"FOO": "1", "BAR": "new", "BAZ": "3"}


def test_sensitive_env_detection():
    env = {
        "SITE_DOMAIN": "localhost",
        "DJANGO_SECRET_KEY": "x",
        "API_TOKEN": "y",
    }
    assert is_sensitive_env_key("DJANGO_SECRET_KEY")
    assert is_sensitive_env_key("API_TOKEN")
    assert not is_sensitive_env_key("SITE_DOMAIN")
    assert sensitive_env_keys(env) == ["API_TOKEN", "DJANGO_SECRET_KEY"]


def test_format_env_text_quote_mode():
    content = format_env_text({"KEY": 'say "hi"'}, quote_values=True)
    assert 'KEY="say \\"hi\\""' in content
