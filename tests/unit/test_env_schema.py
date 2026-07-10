"""Unit tests for the declarative env schema (ilha.config.env_schema).

Covers the two consumers: generation (generate_default_app_env) and
validation (validate_env), plus required_keys. Footprint is duck-typed, so a
SimpleNamespace with has_django / has_postgres stands in for ProjectFootprint.
"""

from types import SimpleNamespace

from ilha.config.env_schema import (
    ENV_SCHEMA,
    EnvIssue,
    generate_default_app_env,
    required_keys,
    validate_env,
)
from ilha.config.settings import INSECURE_DEFAULT_SECRET_KEY


def fp(*, has_django=False, has_postgres=False):
    return SimpleNamespace(has_django=has_django, has_postgres=has_postgres)


# --- validate_env -----------------------------------------------------------

def test_validate_missing_required_postgres_creds_is_error():
    issues = validate_env({}, fp(has_postgres=True))
    keys = {i.key for i in issues if i.severity == "error"}
    assert {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"} <= keys


def test_validate_blank_required_is_error():
    env = {"POSTGRES_USER": "", "POSTGRES_PASSWORD": "", "POSTGRES_DB": ""}
    issues = validate_env(env, fp(has_postgres=True))
    assert {i.key for i in issues} == {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
    assert all(i.severity == "error" for i in issues)


def test_validate_present_postgres_creds_ok():
    env = {"POSTGRES_USER": "u", "POSTGRES_PASSWORD": "p", "POSTGRES_DB": "d"}
    assert validate_env(env, fp(has_postgres=True)) == []


def test_validate_insecure_secret_key_is_error():
    env = {"DJANGO_SECRET_KEY": INSECURE_DEFAULT_SECRET_KEY}
    issues = validate_env(env, fp(has_django=True))
    assert any(i.key == "DJANGO_SECRET_KEY" and i.severity == "error" for i in issues)


def test_validate_blank_secret_key_is_error():
    # Empty string is both "required and blank" and a forbidden value; either way
    # it must surface as an error, exactly once per the missing-branch.
    issues = validate_env({"DJANGO_SECRET_KEY": ""}, fp(has_django=True))
    assert any(i.key == "DJANGO_SECRET_KEY" and i.severity == "error" for i in issues)


def test_validate_strong_secret_key_ok():
    env = {"DJANGO_SECRET_KEY": "a-very-strong-unique-key"}
    assert [i for i in validate_env(env, fp(has_django=True)) if i.key == "DJANGO_SECRET_KEY"] == []


def test_validate_no_footprint_has_no_required_issues():
    # A plain (non-Django/non-Postgres) project requires none of these.
    assert validate_env({}, fp()) == []


def test_validate_secret_not_required_when_not_django():
    # Absent secret is fine when Django isn't in play.
    assert validate_env({}, fp(has_postgres=True, has_django=False)) != []  # postgres missing
    django_issues = [i for i in validate_env({}, fp(has_postgres=True)) if i.key == "DJANGO_SECRET_KEY"]
    assert django_issues == []


# --- generate_default_app_env ----------------------------------------------

def test_generate_postgres_creds_are_populated_and_valid():
    env = generate_default_app_env(fp(has_postgres=True))
    assert env["POSTGRES_USER"]
    assert env["POSTGRES_PASSWORD"]
    assert env["POSTGRES_DB"]
    # The generated env must pass its own validation — no blanks.
    assert validate_env(env, fp(has_postgres=True)) == []


def test_generated_password_is_not_trivial():
    env = generate_default_app_env(fp(has_postgres=True))
    assert len(env["POSTGRES_PASSWORD"]) >= 16
    assert env["POSTGRES_PASSWORD"] != env["POSTGRES_USER"]


def test_generate_secret_key_is_unique_and_safe():
    a = generate_default_app_env(fp(has_django=True))
    b = generate_default_app_env(fp(has_django=True))
    assert a["DJANGO_SECRET_KEY"] != b["DJANGO_SECRET_KEY"]  # fresh each call
    assert a["DJANGO_SECRET_KEY"] != INSECURE_DEFAULT_SECRET_KEY


def test_debug_always_emitted():
    assert generate_default_app_env(fp())["DEBUG"] == "True"


def test_no_postgres_footprint_omits_postgres():
    env = generate_default_app_env(fp(has_django=True))
    assert "POSTGRES_USER" not in env
    assert "DJANGO_SECRET_KEY" in env


def test_no_django_footprint_omits_secret_key():
    env = generate_default_app_env(fp(has_postgres=True))
    assert "DJANGO_SECRET_KEY" not in env
    assert "POSTGRES_USER" in env


# --- required_keys ----------------------------------------------------------

def test_required_keys_tracks_footprint():
    assert required_keys(fp()) == []
    assert set(required_keys(fp(has_postgres=True))) == {
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    }
    assert required_keys(fp(has_django=True)) == ["DJANGO_SECRET_KEY"]


def test_schema_is_nonempty_and_app_owned_entries_exist():
    assert ENV_SCHEMA
    assert any(s.owner == "app" for s in ENV_SCHEMA)
    assert all(isinstance(EnvIssue(k, "error", "m"), EnvIssue) for k in ["x"])
