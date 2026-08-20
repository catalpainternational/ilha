"""Tests for Docker build-context secret exclusions."""

from __future__ import annotations

from pathlib import Path

import pytest

from ilha.exceptions import ilhaCommandError
from ilha.utils.dockerignore import (
    assert_build_context_excludes_secrets,
    dockerignore_covers,
    missing_secret_dir_ignores,
)


def test_dockerignore_covers_common_directory_patterns():
    text = ".ilha/\nsecrets\n**/backups/**\n"
    assert dockerignore_covers(text, ".ilha")
    assert dockerignore_covers(text, "secrets")
    assert dockerignore_covers(text, "backups")
    assert not dockerignore_covers(text, "src")


def test_dockerignore_ignores_negation_and_comments():
    text = "# .ilha/\n!.ilha/\n"
    assert not dockerignore_covers(text, ".ilha")


def test_missing_secret_dir_ignores_only_existing_unignored_dirs(tmp_path: Path):
    (tmp_path / ".ilha").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / ".dockerignore").write_text("src/\n")
    assert missing_secret_dir_ignores(tmp_path) == [".ilha"]


def test_assert_build_context_excludes_secrets_raises(tmp_path: Path):
    (tmp_path / "backups").mkdir()
    with pytest.raises(ilhaCommandError, match="backups/"):
        assert_build_context_excludes_secrets(tmp_path)
