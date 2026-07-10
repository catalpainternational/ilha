"""Comprehensive unit tests for ilha/utils/env_loader.py.

Covers parse_env_content, _strip_inline_quotes, load_env_file,
upsert_env_vars, load_env_from_project_root, load_env_from_home.

All filesystem interaction is confined to tmp_path; home dir and project
root discovery are monkeypatched so nothing touches the real environment.
"""

from pathlib import Path

import pytest

from ilha.utils import env_loader
from ilha.utils.env_loader import (
    _strip_inline_quotes,
    load_env_file,
    load_env_from_home,
    load_env_from_project_root,
    merge_env_layers,
    parse_env_content,
    upsert_env_vars,
)


# ---------------------------------------------------------------------------
# merge_env_layers — the single precedence contract
# ---------------------------------------------------------------------------

def test_merge_later_layer_wins():
    assert merge_env_layers({"A": "1", "B": "1"}, {"B": "2"}) == {"A": "1", "B": "2"}


def test_merge_base_defaults_fill_gaps_only():
    # base_defaults must NOT override an explicit value from any layer.
    merged = merge_env_layers(
        {"POSTGRES_USER": "explicit"},
        base_defaults={"POSTGRES_USER": "compose-default", "POSTGRES_DB": "app"},
    )
    assert merged["POSTGRES_USER"] == "explicit"   # explicit wins
    assert merged["POSTGRES_DB"] == "app"          # gap filled by default


def test_merge_skips_none_and_empty_layers():
    assert merge_env_layers(None, {}, {"A": "1"}, None) == {"A": "1"}


def test_merge_no_args_is_empty():
    assert merge_env_layers() == {}


def test_merge_base_defaults_applied_after_all_layers():
    # A key absent from every layer but present in base_defaults appears;
    # a key present in a layer is untouched even if base_defaults also has it.
    merged = merge_env_layers(
        {"A": "layer"},
        {"B": "layer"},
        base_defaults={"A": "def", "C": "def"},
    )
    assert merged == {"A": "layer", "B": "layer", "C": "def"}


# ---------------------------------------------------------------------------
# _strip_inline_quotes
# ---------------------------------------------------------------------------

def test_strip_double_quotes():
    assert _strip_inline_quotes('"hello"') == "hello"


def test_strip_single_quotes():
    assert _strip_inline_quotes("'hello'") == "hello"


def test_strip_quotes_preserves_inner_spaces():
    assert _strip_inline_quotes('"hello world"') == "hello world"


def test_strip_quotes_mismatched_not_stripped():
    assert _strip_inline_quotes("\"hello'") == "\"hello'"


def test_strip_quotes_only_leading_quote_kept():
    assert _strip_inline_quotes('"hello') == '"hello'


def test_strip_quotes_single_char_not_stripped():
    # A lone quote is length 1, below the >= 2 threshold.
    assert _strip_inline_quotes('"') == '"'
    assert _strip_inline_quotes("'") == "'"


def test_strip_quotes_empty_string():
    assert _strip_inline_quotes("") == ""


def test_strip_quotes_pair_of_quotes_becomes_empty():
    assert _strip_inline_quotes('""') == ""
    assert _strip_inline_quotes("''") == ""


def test_strip_quotes_unquoted_unchanged():
    assert _strip_inline_quotes("plain") == "plain"


def test_strip_quotes_inner_quotes_untouched():
    assert _strip_inline_quotes('a"b') == 'a"b'


# ---------------------------------------------------------------------------
# parse_env_content
# ---------------------------------------------------------------------------

def test_parse_basic_pair():
    assert parse_env_content("KEY=value") == {"KEY": "value"}


def test_parse_comment_lines_ignored():
    content = "# this is a comment\nKEY=value\n# another"
    assert parse_env_content(content) == {"KEY": "value"}


def test_parse_blank_lines_ignored():
    content = "\n\nKEY=value\n\n"
    assert parse_env_content(content) == {"KEY": "value"}


def test_parse_double_quoted_value_stripped():
    assert parse_env_content('KEY="quoted value"') == {"KEY": "quoted value"}


def test_parse_single_quoted_value_stripped():
    assert parse_env_content("KEY='quoted value'") == {"KEY": "quoted value"}


def test_parse_line_without_equals_ignored():
    content = "JUST_A_WORD\nKEY=value"
    assert parse_env_content(content) == {"KEY": "value"}


def test_parse_empty_key_ignored():
    content = "=orphan_value\nKEY=value"
    assert parse_env_content(content) == {"KEY": "value"}


def test_parse_equals_inside_value_preserved():
    assert parse_env_content("KEY=a=b=c") == {"KEY": "a=b=c"}


def test_parse_whitespace_around_key_and_value_stripped():
    assert parse_env_content("  KEY  =  value  ") == {"KEY": "value"}


def test_parse_empty_value():
    assert parse_env_content("KEY=") == {"KEY": ""}


def test_parse_multiple_pairs_last_wins_on_duplicate():
    content = "KEY=first\nKEY=second"
    assert parse_env_content(content) == {"KEY": "second"}


def test_parse_empty_content():
    assert parse_env_content("") == {}


def test_parse_comment_with_leading_whitespace_ignored():
    assert parse_env_content("   # indented comment") == {}


def test_parse_url_value_with_equals_and_colons():
    content = "DATABASE_URL=postgres://u:p@host:5432/db?sslmode=require"
    assert parse_env_content(content) == {
        "DATABASE_URL": "postgres://u:p@host:5432/db?sslmode=require"
    }


# ---------------------------------------------------------------------------
# load_env_file
# ---------------------------------------------------------------------------

def test_load_env_file_missing_returns_empty(tmp_path):
    assert load_env_file(tmp_path / "does_not_exist.env") == {}


def test_load_env_file_valid(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# header\nFOO=bar\nBAZ=\"q\"\n", encoding="utf-8")
    assert load_env_file(p) == {"FOO": "bar", "BAZ": "q"}


def test_load_env_file_unreadable_raises(tmp_path, monkeypatch):
    """An existing-but-unreadable env file must surface, not silently yield {}.

    Swallowing a read failure to {} ran the worktree with an empty environment
    and failed confusingly far downstream — so we raise EnvFileError instead.
    """
    from ilha.exceptions import EnvFileError

    p = tmp_path / ".env"
    p.write_text("FOO=bar\n", encoding="utf-8")

    def boom(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(EnvFileError):
        load_env_file(p)


def test_load_env_file_empty_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("", encoding="utf-8")
    assert load_env_file(p) == {}


# ---------------------------------------------------------------------------
# upsert_env_vars
# ---------------------------------------------------------------------------

def test_upsert_updates_existing_key_in_place(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\nB=2\nC=3\n", encoding="utf-8")
    upsert_env_vars(p, {"B": "22"})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines == ["A=1", "B=22", "C=3"]


def test_upsert_appends_new_key(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n", encoding="utf-8")
    upsert_env_vars(p, {"NEW": "x"})
    assert p.read_text(encoding="utf-8").splitlines() == ["A=1", "NEW=x"]


def test_upsert_removes_keys(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\nB=2\nC=3\n", encoding="utf-8")
    upsert_env_vars(p, {}, remove=["B"])
    assert p.read_text(encoding="utf-8").splitlines() == ["A=1", "C=3"]


def test_upsert_preserves_comments_blanks_and_order(tmp_path):
    p = tmp_path / ".env"
    original = "# top comment\nA=1\n\n# section\nB=2\n"
    p.write_text(original, encoding="utf-8")
    upsert_env_vars(p, {"B": "changed"})
    assert p.read_text(encoding="utf-8").splitlines() == [
        "# top comment",
        "A=1",
        "",
        "# section",
        "B=changed",
    ]


def test_upsert_creates_file_and_parent_dirs(tmp_path):
    p = tmp_path / "nested" / "deeper" / ".env"
    assert not p.parent.exists()
    upsert_env_vars(p, {"K": "v"})
    assert p.exists()
    assert p.read_text(encoding="utf-8") == "K=v\n"


def test_upsert_value_with_equals(tmp_path):
    p = tmp_path / ".env"
    upsert_env_vars(p, {"URL": "a=b=c"})
    assert load_env_file(p) == {"URL": "a=b=c"}


def test_upsert_value_with_spaces(tmp_path):
    p = tmp_path / ".env"
    upsert_env_vars(p, {"MSG": "hello world"})
    # Round trips: internal spaces preserved.
    assert load_env_file(p) == {"MSG": "hello world"}


def test_upsert_update_and_append_combined(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\nB=2\n", encoding="utf-8")
    upsert_env_vars(p, {"A": "10", "C": "3"})
    assert p.read_text(encoding="utf-8").splitlines() == ["A=10", "B=2", "C=3"]


def test_upsert_remove_and_update_together(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\nB=2\nC=3\n", encoding="utf-8")
    upsert_env_vars(p, {"C": "30"}, remove=["A"])
    assert p.read_text(encoding="utf-8").splitlines() == ["B=2", "C=30"]


def test_upsert_coerces_non_string_values(tmp_path):
    p = tmp_path / ".env"
    upsert_env_vars(p, {"NUM": 42, "FLAG": True})
    assert load_env_file(p) == {"NUM": "42", "FLAG": "True"}


def test_upsert_trailing_newline_present(tmp_path):
    p = tmp_path / ".env"
    upsert_env_vars(p, {"K": "v"})
    assert p.read_text(encoding="utf-8").endswith("\n")


def test_upsert_preserves_non_keyvalue_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text("export SOMETHING\nA=1\n", encoding="utf-8")
    upsert_env_vars(p, {"A": "2"})
    assert p.read_text(encoding="utf-8").splitlines() == ["export SOMETHING", "A=2"]


# ---------------------------------------------------------------------------
# load_env_from_project_root
# ---------------------------------------------------------------------------

def test_load_env_from_project_root_valid(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PROOT=yes\n", encoding="utf-8")
    monkeypatch.setattr(
        "ilha.config.settings.get_project_root", lambda: tmp_path
    )
    assert load_env_from_project_root() == {"PROOT": "yes"}


def test_load_env_from_project_root_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ilha.config.settings.get_project_root", lambda: tmp_path
    )
    assert load_env_from_project_root() == {}


def test_load_env_from_project_root_error_returns_empty(monkeypatch):
    def boom():
        raise RuntimeError("no project root")

    monkeypatch.setattr("ilha.config.settings.get_project_root", boom)
    assert load_env_from_project_root() == {}


# ---------------------------------------------------------------------------
# load_env_from_home
# ---------------------------------------------------------------------------

def test_load_env_from_home_valid(tmp_path, monkeypatch):
    ilha_dir = tmp_path / ".ilha"
    ilha_dir.mkdir()
    (ilha_dir / "env.ilha").write_text("GLOBAL=1\n", encoding="utf-8")
    monkeypatch.setattr(env_loader.Path, "home", lambda: tmp_path)
    assert load_env_from_home() == {"GLOBAL": "1"}


def test_load_env_from_home_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(env_loader.Path, "home", lambda: tmp_path)
    assert load_env_from_home() == {}


def test_load_env_from_home_error_returns_empty(monkeypatch):
    def boom():
        raise RuntimeError("no home")

    monkeypatch.setattr(env_loader.Path, "home", boom)
    assert load_env_from_home() == {}
