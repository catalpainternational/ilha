"""Tests for compose-driven named-volume discovery.

The discovery module is the new source of truth for backup/restore — it
replaces the hardcoded ``[postgres_data, redis_data, ...]`` suffix list,
which only matched ilha-native projects. Compose-driven discovery handles
both lifecycle A (ilha-native, project-prefixed) and lifecycle B (BYO
projects with native volume names like ``pgdata``).
"""

from __future__ import annotations

from pathlib import Path

from ilha.utils.compose_external_volumes import (
    NamedVolume,
    discover_named_volumes,
    resolve_compose_project_name,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ── BYO-project case (the time project) ───────────────────────────────────


def test_byo_project_discovers_pgdata_with_project_prefix(tmp_path: Path):
    """time project: compose declares ``pgdata`` and runs as
    ``COMPOSE_PROJECT_NAME=time``. Docker volume → ``time_pgdata``."""
    compose = tmp_path / "docker-compose.yml"
    _write(compose, "services: {}\nvolumes:\n  pgdata: null\n  frontend_node_modules: null\n")

    vols = discover_named_volumes([compose], env={"COMPOSE_PROJECT_NAME": "time"})
    by_key = {v.key: v for v in vols}
    assert by_key["pgdata"].docker_name == "time_pgdata"
    assert by_key["pgdata"].is_external is False
    assert by_key["frontend_node_modules"].docker_name == "time_frontend_node_modules"


# ── ilha-native case (worktrees) ──────────────────────────────────────────


def test_ilha_worktree_namespace_prefixes_correctly(tmp_path: Path):
    """ilha-managed worktree: COMPOSE_PROJECT_NAME=time-feature_x.
    Same compose, different prefix — same discovery code."""
    compose = tmp_path / "docker-compose.yml"
    _write(compose, "services: {}\nvolumes:\n  pgdata:\n")

    vols = discover_named_volumes(
        [compose], env={"COMPOSE_PROJECT_NAME": "time-feature_x"}
    )
    assert vols[0].docker_name == "time-feature_x_pgdata"


# ── external volumes ──────────────────────────────────────────────────────


def test_external_volume_without_name_uses_bare_key(tmp_path: Path):
    """``external: true`` without ``name:`` field: docker resolves the
    volume by its bare key, no project prefix."""
    compose = tmp_path / "docker-compose.yml"
    _write(
        compose,
        "services: {}\nvolumes:\n  shared_media:\n    external: true\n",
    )
    vols = discover_named_volumes([compose], env={"COMPOSE_PROJECT_NAME": "time"})
    v = vols[0]
    assert v.is_external is True
    assert v.docker_name == "shared_media"


def test_external_volume_with_explicit_name(tmp_path: Path):
    compose = tmp_path / "docker-compose.yml"
    _write(
        compose,
        "services: {}\nvolumes:\n  app_data:\n    external: true\n    name: prod_app_data_v2\n",
    )
    vols = discover_named_volumes([compose], env={"COMPOSE_PROJECT_NAME": "any"})
    assert vols[0].docker_name == "prod_app_data_v2"
    assert vols[0].is_external is True


def test_explicit_name_field_on_non_external_still_wins(tmp_path: Path):
    """Compose lets a project pin a non-external volume's name. ``name:``
    overrides the project-prefix convention."""
    compose = tmp_path / "docker-compose.yml"
    _write(
        compose,
        "services: {}\nvolumes:\n  pgdata:\n    name: custom_pg_volume\n",
    )
    vols = discover_named_volumes([compose], env={"COMPOSE_PROJECT_NAME": "time"})
    assert vols[0].docker_name == "custom_pg_volume"
    assert vols[0].is_external is False


# ── env-var expansion ─────────────────────────────────────────────────────


def test_env_vars_in_compose_volume_block_expanded(tmp_path: Path):
    """Compose interpolation must run before YAML parsing so dynamic
    volume names (``${VAR}``) resolve correctly."""
    compose = tmp_path / "docker-compose.yml"
    _write(
        compose,
        "services: {}\nvolumes:\n  ${VOL_NAME:-pgdata}:\n    name: ${VOL_FULL_NAME:-time_pgdata_v2}\n",
    )
    vols = discover_named_volumes(
        [compose], env={"COMPOSE_PROJECT_NAME": "time"}
    )
    assert vols[0].key == "pgdata"
    assert vols[0].docker_name == "time_pgdata_v2"


# ── multiple compose files (base + overlay) ───────────────────────────────


def test_overlay_redeclaration_is_last_wins(tmp_path: Path):
    """``docker compose -f base -f overlay`` merges with last-wins on
    volumes; discovery must match that semantic."""
    base = tmp_path / "docker-compose.yml"
    overlay = tmp_path / "docker-compose.overlay.yml"
    _write(base, "services: {}\nvolumes:\n  pgdata:\n")
    _write(
        overlay,
        "services: {}\nvolumes:\n  pgdata:\n    external: true\n    name: shared_pg\n",
    )
    vols = discover_named_volumes(
        [base, overlay], env={"COMPOSE_PROJECT_NAME": "time"}
    )
    assert len(vols) == 1
    assert vols[0].docker_name == "shared_pg"
    assert vols[0].is_external is True
    assert overlay.name in vols[0].declared_in


# ── degenerate inputs ─────────────────────────────────────────────────────


def test_no_volumes_block_returns_empty(tmp_path: Path):
    compose = tmp_path / "docker-compose.yml"
    _write(compose, "services:\n  web:\n    image: nginx\n")
    assert discover_named_volumes([compose], env={"COMPOSE_PROJECT_NAME": "x"}) == []


def test_missing_compose_file_skipped_silently(tmp_path: Path):
    real = tmp_path / "docker-compose.yml"
    _write(real, "services: {}\nvolumes:\n  data:\n")
    missing = tmp_path / "does-not-exist.yml"

    vols = discover_named_volumes([missing, real], env={"COMPOSE_PROJECT_NAME": "x"})
    assert [v.key for v in vols] == ["data"]


def test_no_project_name_returns_bare_key(tmp_path: Path):
    """A blank ``COMPOSE_PROJECT_NAME`` falls back to the bare key —
    matches docker compose's behavior when COMPOSE_PROJECT_NAME is unset."""
    compose = tmp_path / "docker-compose.yml"
    _write(compose, "services: {}\nvolumes:\n  pgdata:\n")
    vols = discover_named_volumes([compose], env={})
    assert vols[0].docker_name == "pgdata"


# ── resolve_compose_project_name ──────────────────────────────────────────


def test_resolve_returns_convention_for_non_main_branches(tmp_path: Path):
    """Worktree branches always get ``<project>-<branch>``; no probe runs."""
    name = resolve_compose_project_name(
        tmp_path, "feature-x", sanitized_project_name="time"
    )
    assert name == "time-feature-x"


def test_resolve_returns_bare_project_when_local_volume_matches(tmp_path: Path, monkeypatch):
    """BYO-main case: host has ``time_pgdata`` matching the bare-namespace
    resolution → ilha must align with that, not its own ``time-main`` convention."""
    compose = tmp_path / "docker-compose.yml"
    _write(compose, "services: {}\nvolumes:\n  pgdata:\n")

    # Stub the volume validator so the test doesn't depend on docker.
    from ilha.utils import compose_external_volumes
    monkeypatch.setattr(
        compose_external_volumes, "validate_volume_exists",
        lambda name: name == "time_pgdata",
    )

    name = resolve_compose_project_name(
        tmp_path, "main", sanitized_project_name="time"
    )
    assert name == "time"


def test_resolve_falls_back_to_convention_when_no_bare_volumes(tmp_path: Path, monkeypatch):
    """Fresh project (no pre-existing local data) → use ilha's convention."""
    compose = tmp_path / "docker-compose.yml"
    _write(compose, "services: {}\nvolumes:\n  pgdata:\n")

    from ilha.utils import compose_external_volumes
    monkeypatch.setattr(
        compose_external_volumes, "validate_volume_exists",
        lambda name: False,
    )

    name = resolve_compose_project_name(
        tmp_path, "main", sanitized_project_name="time"
    )
    assert name == "time-main"


def test_resolve_skips_external_volumes_when_probing(tmp_path: Path, monkeypatch):
    """An external volume's existence shouldn't trigger BYO-main detection
    (the project owns its lifecycle; not a signal of pre-ilha state)."""
    compose = tmp_path / "docker-compose.yml"
    _write(
        compose,
        "services: {}\nvolumes:\n  shared_secrets:\n    external: true\n",
    )

    from ilha.utils import compose_external_volumes
    monkeypatch.setattr(
        compose_external_volumes, "validate_volume_exists",
        lambda name: True,  # would match if we considered externals
    )

    name = resolve_compose_project_name(
        tmp_path, "main", sanitized_project_name="time"
    )
    assert name == "time-main"


def test_resolve_master_branch_treated_like_main(tmp_path: Path, monkeypatch):
    compose = tmp_path / "docker-compose.yml"
    _write(compose, "services: {}\nvolumes:\n  data:\n")

    from ilha.utils import compose_external_volumes
    monkeypatch.setattr(
        compose_external_volumes, "validate_volume_exists",
        lambda name: name == "legacyproj_data",
    )

    name = resolve_compose_project_name(
        tmp_path, "master", sanitized_project_name="legacyproj"
    )
    assert name == "legacyproj"
