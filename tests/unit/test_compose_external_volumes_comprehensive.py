"""Comprehensive, deterministic tests for ilha.utils.compose_external_volumes.

Covers:
- expand_compose_vars: ${VAR}, ${VAR:-default}, undefined-stays-literal,
  nested/iterative expansion, multiple vars per line.
- load_compose: reads a tmp compose file, expands vars from env, returns dict;
  returns {} on missing file / bad yaml / non-mapping top level.
- discover_named_volumes / NamedVolume
- _resolve_volume_docker_name
- list_external_volume_host_names
- compose_web_image_reference
- compose_service_is_buildable
- _load_compose_service

No network / docker access is required for any of these code paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ilha.utils.compose_external_volumes import (
    NamedVolume,
    _load_compose_service,
    _resolve_volume_docker_name,
    compose_service_is_buildable,
    compose_web_image_reference,
    discover_named_volumes,
    expand_compose_vars,
    list_external_volume_host_names,
    load_compose,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# expand_compose_vars
# ---------------------------------------------------------------------------


def test_expand_simple_var_present():
    assert expand_compose_vars("${NAME}", {"NAME": "demo"}) == "demo"


def test_expand_var_embedded_in_text():
    out = expand_compose_vars("pre-${X}-suf", {"X": "mid"})
    assert out == "pre-mid-suf"


def test_expand_undefined_no_default_stays_literal():
    # Undefined var with no default must be preserved verbatim.
    assert expand_compose_vars("${MISSING}", {}) == "${MISSING}"


def test_expand_undefined_with_default_uses_default():
    assert expand_compose_vars("${MISSING:-fallback}", {}) == "fallback"


def test_expand_defined_ignores_default():
    assert expand_compose_vars("${X:-fallback}", {"X": "real"}) == "real"


def test_expand_empty_default():
    # ${VAR:-} with empty default and missing var -> empty string.
    assert expand_compose_vars("[${MISSING:-}]", {}) == "[]"


def test_expand_multiple_vars_same_line():
    out = expand_compose_vars("${A}/${B}", {"A": "one", "B": "two"})
    assert out == "one/two"


def test_expand_mixed_defined_and_undefined():
    out = expand_compose_vars("${A}-${MISSING}", {"A": "x"})
    assert out == "x-${MISSING}"


def test_expand_iterative_nested_reference():
    # A expands to ${B} which then expands to final on a later pass.
    out = expand_compose_vars("${A}", {"A": "${B}", "B": "final"})
    assert out == "final"


def test_expand_iterative_multi_level():
    env = {"A": "${B}", "B": "${C}", "C": "deep"}
    assert expand_compose_vars("${A}", env) == "deep"


def test_expand_value_coerced_to_str():
    # Non-string env values are stringified.
    assert expand_compose_vars("${PORT}", {"PORT": 8080}) == "8080"


def test_expand_no_placeholders_returns_input():
    assert expand_compose_vars("plain text", {"A": "x"}) == "plain text"


def test_expand_template_coerced_to_str():
    # A non-str template is coerced (str(template)) before substitution.
    assert expand_compose_vars(12345, {}) == "12345"


def test_expand_self_referential_does_not_hang():
    # Var referencing itself: with the value literally "${A}" it never stabilizes
    # to something else, but the 20-iteration cap prevents an infinite loop.
    out = expand_compose_vars("${A}", {"A": "${A}"})
    assert out == "${A}"


# ---------------------------------------------------------------------------
# load_compose
# ---------------------------------------------------------------------------


def test_load_compose_basic(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services:\n  web:\n    image: foo\n")
    data = load_compose(c, {})
    assert data == {"services": {"web": {"image": "foo"}}}


def test_load_compose_expands_env(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        "services:\n  web:\n    image: ${REG}/web:latest\n",
    )
    data = load_compose(c, {"REG": "myregistry"})
    assert data["services"]["web"]["image"] == "myregistry/web:latest"


def test_load_compose_missing_file_returns_empty(tmp_path: Path):
    assert load_compose(tmp_path / "nope.yml", {}) == {}


def test_load_compose_bad_yaml_returns_empty(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services: [unclosed\n  : : :\n")
    assert load_compose(c, {}) == {}


def test_load_compose_non_mapping_top_level_returns_empty(tmp_path: Path):
    # A YAML list at the top level is not a dict -> {}.
    c = _write(tmp_path / "docker-compose.yml", "- a\n- b\n")
    assert load_compose(c, {}) == {}


def test_load_compose_empty_file_returns_empty(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "")
    assert load_compose(c, {}) == {}


def test_load_compose_scalar_top_level_returns_empty(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "just-a-string\n")
    assert load_compose(c, {}) == {}


def test_load_compose_undefined_var_kept_literal(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        "services:\n  web:\n    image: ${UNDEFINED_REG}/web\n",
    )
    data = load_compose(c, {})
    assert data["services"]["web"]["image"] == "${UNDEFINED_REG}/web"


# ---------------------------------------------------------------------------
# _resolve_volume_docker_name
# ---------------------------------------------------------------------------


def test_resolve_non_external_gets_project_prefix():
    assert _resolve_volume_docker_name("pgdata", {}, "proj") == "proj_pgdata"


def test_resolve_non_external_no_project_returns_bare_key():
    assert _resolve_volume_docker_name("pgdata", {}, "") == "pgdata"


def test_resolve_external_without_name_returns_bare_key():
    assert _resolve_volume_docker_name("shared", {"external": True}, "proj") == "shared"


def test_resolve_explicit_name_wins_for_external():
    spec = {"external": True, "name": "explicit_vol"}
    assert _resolve_volume_docker_name("k", spec, "proj") == "explicit_vol"


def test_resolve_explicit_name_wins_for_non_external():
    spec = {"name": "explicit_vol"}
    assert _resolve_volume_docker_name("k", spec, "proj") == "explicit_vol"


def test_resolve_non_dict_spec_treated_as_prefixed():
    # A None/str spec (anonymous-ish) falls back to project prefix.
    assert _resolve_volume_docker_name("pgdata", None, "proj") == "proj_pgdata"


def test_resolve_name_coerced_to_str():
    spec = {"name": 12345}
    assert _resolve_volume_docker_name("k", spec, "proj") == "12345"


# ---------------------------------------------------------------------------
# discover_named_volumes / NamedVolume
# ---------------------------------------------------------------------------


def test_discover_named_and_external(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        """
volumes:
  pgdata:
  logs:
    external: true
services:
  web:
    build: .
""",
    )
    vols = discover_named_volumes([c], env={"COMPOSE_PROJECT_NAME": "proj"})
    by_key = {v.key: v for v in vols}
    assert set(by_key) == {"pgdata", "logs"}
    assert by_key["pgdata"].docker_name == "proj_pgdata"
    assert by_key["pgdata"].is_external is False
    assert by_key["logs"].docker_name == "logs"
    assert by_key["logs"].is_external is True
    assert by_key["pgdata"].declared_in == str(c)


def test_discover_external_with_name_override(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        """
volumes:
  shared:
    external: true
    name: company_shared_vol
""",
    )
    vols = discover_named_volumes([c], env={"COMPOSE_PROJECT_NAME": "proj"})
    assert vols[0].docker_name == "company_shared_vol"
    assert vols[0].is_external is True


def test_discover_expands_project_name_in_volume_name(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        """
volumes:
  logs:
    external: true
    name: ${COMPOSE_PROJECT_NAME}_logs
""",
    )
    vols = discover_named_volumes([c], env={"COMPOSE_PROJECT_NAME": "myproj"})
    assert vols[0].docker_name == "myproj_logs"


def test_discover_last_wins_across_files(tmp_path: Path):
    base = _write(
        tmp_path / "docker-compose.yml",
        "volumes:\n  data:\n",
    )
    overlay = _write(
        tmp_path / "overlay.yml",
        "volumes:\n  data:\n    external: true\n    name: overlaid\n",
    )
    vols = discover_named_volumes([base, overlay], env={"COMPOSE_PROJECT_NAME": "p"})
    assert len(vols) == 1
    assert vols[0].docker_name == "overlaid"
    assert vols[0].is_external is True
    assert vols[0].declared_in == str(overlay)


def test_discover_skips_missing_files(tmp_path: Path):
    real = _write(tmp_path / "docker-compose.yml", "volumes:\n  data:\n")
    vols = discover_named_volumes(
        [tmp_path / "ghost.yml", real], env={"COMPOSE_PROJECT_NAME": "p"}
    )
    assert [v.key for v in vols] == ["data"]


def test_discover_no_volumes_block(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services:\n  web:\n    build: .\n")
    assert discover_named_volumes([c], env={}) == []


def test_discover_volumes_not_a_mapping_skipped(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "volumes:\n  - a\n  - b\n")
    assert discover_named_volumes([c], env={}) == []


def test_discover_bad_yaml_skipped(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "volumes: [unclosed\n : :\n")
    assert discover_named_volumes([c], env={}) == []


def test_discover_env_none_defaults_to_bare_key(tmp_path: Path):
    # env=None -> no COMPOSE_PROJECT_NAME -> non-external volume keeps bare key.
    c = _write(tmp_path / "docker-compose.yml", "volumes:\n  data:\n")
    vols = discover_named_volumes([c])
    assert vols[0].docker_name == "data"


def test_discover_empty_iterable():
    assert discover_named_volumes([]) == []


def test_named_volume_is_frozen():
    v = NamedVolume(key="k", docker_name="d", is_external=False, declared_in="f")
    with pytest.raises(Exception):
        v.key = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# list_external_volume_host_names
# ---------------------------------------------------------------------------


def test_list_external_with_explicit_name(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        """
volumes:
  logs_volume:
    external: true
    name: ${COMPOSE_PROJECT_NAME}_logs_volume
  other:
    external: false
""",
    )
    names = list_external_volume_host_names(c, {"COMPOSE_PROJECT_NAME": "myproj-branch"})
    assert names == ["myproj-branch_logs_volume"]


def test_list_external_without_name_derives_from_project(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        "volumes:\n  cache:\n    external: true\n",
    )
    names = list_external_volume_host_names(c, {"COMPOSE_PROJECT_NAME": "proj"})
    assert names == ["proj_cache"]


def test_list_external_without_name_no_project_uses_key(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        "volumes:\n  cache:\n    external: true\n",
    )
    names = list_external_volume_host_names(c, {})
    assert names == ["cache"]


def test_list_external_ignores_non_external(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        """
volumes:
  pgdata:
  ext:
    external: true
""",
    )
    names = list_external_volume_host_names(c, {"COMPOSE_PROJECT_NAME": "p"})
    assert names == ["p_ext"]


def test_list_external_dedupes(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        """
volumes:
  a:
    external: true
    name: shared
  b:
    external: true
    name: shared
""",
    )
    names = list_external_volume_host_names(c, {})
    assert names == ["shared"]


def test_list_external_skips_non_dict_spec(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "volumes:\n  anon:\n")
    assert list_external_volume_host_names(c, {}) == []


def test_list_external_no_volumes_block(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services:\n  web:\n    build: .\n")
    assert list_external_volume_host_names(c, {}) == []


def test_list_external_missing_file(tmp_path: Path):
    assert list_external_volume_host_names(tmp_path / "nope.yml", {}) == []


# ---------------------------------------------------------------------------
# _load_compose_service
# ---------------------------------------------------------------------------


def test_load_compose_service_returns_mapping(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        "services:\n  web:\n    image: foo\n    build: .\n",
    )
    svc = _load_compose_service(c, {}, "web")
    assert svc == {"image": "foo", "build": "."}


def test_load_compose_service_missing_returns_empty(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services:\n  web:\n    image: foo\n")
    assert _load_compose_service(c, {}, "caddy") == {}


def test_load_compose_service_no_services_block(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "volumes:\n  data:\n")
    assert _load_compose_service(c, {}, "web") == {}


def test_load_compose_service_non_dict_service(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services:\n  web: null\n")
    assert _load_compose_service(c, {}, "web") == {}


# ---------------------------------------------------------------------------
# compose_web_image_reference
# ---------------------------------------------------------------------------


def test_web_image_explicit(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        "services:\n  web:\n    image: myregistry/web:1.2.3\n",
    )
    assert compose_web_image_reference(c, {}, "web") == "myregistry/web:1.2.3"


def test_web_image_explicit_with_env(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        "services:\n  web:\n    image: ${REG}/web:${TAG}\n",
    )
    ref = compose_web_image_reference(c, {"REG": "reg", "TAG": "v9"}, "web")
    assert ref == "reg/web:v9"


def test_web_image_build_only_defaults_from_project(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services:\n  web:\n    build: .\n")
    ref = compose_web_image_reference(c, {"COMPOSE_PROJECT_NAME": "bi-staging"}, "web")
    assert ref == "bi-staging_web:latest"


def test_web_image_build_only_no_project(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services:\n  web:\n    build: .\n")
    assert compose_web_image_reference(c, {}, "web") == "web:latest"


def test_web_image_missing_service_returns_empty(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services: {}\n")
    ref = compose_web_image_reference(c, {"COMPOSE_PROJECT_NAME": "p"}, "caddy")
    assert ref == ""


def test_web_image_non_dict_service_returns_empty(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services:\n  web: null\n")
    assert compose_web_image_reference(c, {}, "web") == ""


def test_web_image_strips_whitespace(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        'services:\n  web:\n    image: "  spaced/img:1  "\n',
    )
    assert compose_web_image_reference(c, {}, "web") == "spaced/img:1"


def test_web_image_missing_file_returns_empty(tmp_path: Path):
    assert compose_web_image_reference(tmp_path / "nope.yml", {}, "web") == ""


# ---------------------------------------------------------------------------
# compose_service_is_buildable
# ---------------------------------------------------------------------------


def test_buildable_true_with_build_directive(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        "services:\n  web:\n    build: .\n    image: my-app:dev\n",
    )
    assert compose_service_is_buildable(c, {}, "web") is True


def test_buildable_true_with_build_mapping(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        "services:\n  web:\n    build:\n      context: .\n      dockerfile: Dockerfile\n",
    )
    assert compose_service_is_buildable(c, {}, "web") is True


def test_buildable_false_for_pull_only_image(tmp_path: Path):
    c = _write(
        tmp_path / "docker-compose.yml",
        "services:\n  caddy:\n    image: caddy:2-alpine\n",
    )
    assert compose_service_is_buildable(c, {}, "caddy") is False


def test_buildable_false_for_missing_service(tmp_path: Path):
    c = _write(tmp_path / "docker-compose.yml", "services: {}\n")
    assert compose_service_is_buildable(c, {}, "web") is False


def test_buildable_false_for_missing_file(tmp_path: Path):
    assert compose_service_is_buildable(tmp_path / "nope.yml", {}, "web") is False
