"""Tests for shared remote-droplet helpers."""

from __future__ import annotations

from pathlib import Path

from ilha.commands.droplet_remote import (
    dump_postgres_command,
    resolve_dump_output_path,
    sanitize_backup_label,
)


def test_dump_postgres_command_uses_container_env():
    cmd = dump_postgres_command("db")
    assert "cd '/app'" in cmd or "cd /app" in cmd
    assert "exec -T db" in cmd
    assert "POSTGRES_PASSWORD" in cmd
    assert "pg_dump -Fc" in cmd
    assert "PGPASSWORD" in cmd


def test_resolve_dump_output_path_defaults_under_backups(tmp_path: Path):
    dest = resolve_dump_output_path(tmp_path, output=None, label="staging.example.com")
    assert dest.parent == (tmp_path / "backups").resolve()
    assert dest.name.startswith("staging.example.com-")
    assert dest.suffix == ".pgdump"


def test_resolve_dump_output_path_honors_explicit_output(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dest = resolve_dump_output_path(tmp_path, output=Path("custom.dump"), label="x")
    assert dest == (tmp_path / "custom.dump").resolve()


def test_sanitize_backup_label_strips_unsafe_chars():
    assert sanitize_backup_label("staging.bower.bid") == "staging.bower.bid"
    assert "/" not in sanitize_backup_label("a/b")
