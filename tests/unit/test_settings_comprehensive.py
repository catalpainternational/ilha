"""
Comprehensive, deterministic unit tests for ilha/config/settings.py pure functions.

Covers:
- sanitize_project_name / sanitize_hostname (alias) transformations
- generate_secret_key entropy / uniqueness
- INSECURE_DEFAULT_SECRET_KEY constant + DEFAULT_ENV_VARS shape
- resolve_project_name (config.yml + fallbacks)
- resolve_default_branch (mocked git subprocess)
- get_compose_command (mocked docker probes, timeout handling)

No real network / docker / cwd dependence: everything uses tmp_path + monkeypatch.
"""

import subprocess

import pytest

from ilha.config import settings
from ilha.config.settings import (
    sanitize_project_name,
    sanitize_hostname,
    generate_secret_key,
    resolve_project_name,
    resolve_default_branch,
    get_compose_command,
    INSECURE_DEFAULT_SECRET_KEY,
    DEFAULT_ENV_VARS,
)


# ---------------------------------------------------------------------------
# sanitize_project_name
# ---------------------------------------------------------------------------
class TestSanitizeProjectName:
    def test_underscores_become_hyphens(self):
        assert sanitize_project_name("My_Project") == "my-project"

    def test_multiple_consecutive_underscores(self):
        # Each underscore maps to its own hyphen (no collapsing).
        assert sanitize_project_name("a__b") == "a--b"

    def test_non_alnum_becomes_dash(self):
        assert sanitize_project_name("Hello World!") == "hello-world"

    def test_dots_become_dashes(self):
        assert sanitize_project_name("my.project") == "my-project"

    def test_slashes_become_dashes(self):
        assert sanitize_project_name("feature/login") == "feature-login"

    def test_strip_leading_trailing_dashes(self):
        assert sanitize_project_name("--foo--") == "foo"

    def test_strip_dashes_produced_by_replacement(self):
        # "!!!" -> "---" -> stripped -> ""
        assert sanitize_project_name("!!!") == ""

    def test_all_underscores_strip_to_empty(self):
        assert sanitize_project_name("___") == ""

    def test_lowercased(self):
        assert sanitize_project_name("UPPER") == "upper"

    def test_mixed_case_with_hyphen_and_underscore(self):
        assert sanitize_project_name("foo_bar-baz") == "foo-bar-baz"

    def test_unicode_non_ascii_dropped(self):
        # 'é' is not [a-zA-Z0-9-] -> dash -> stripped from the tail.
        assert sanitize_project_name("café") == "caf"

    def test_unicode_interior_becomes_dash(self):
        assert sanitize_project_name("a?b") == "a-b"

    def test_empty_string(self):
        assert sanitize_project_name("") == ""

    def test_whitespace_only(self):
        assert sanitize_project_name("   ") == ""

    def test_digits_preserved(self):
        assert sanitize_project_name("123") == "123"

    def test_already_clean_name_unchanged(self):
        assert sanitize_project_name("business-intelligence") == "business-intelligence"

    def test_leading_underscore_only(self):
        assert sanitize_project_name("_leading") == "leading"

    def test_trailing_underscore_only(self):
        assert sanitize_project_name("trailing_") == "trailing"

    def test_result_is_dns_safe_charset(self):
        result = sanitize_project_name("Weird__Name!!With/Slashes.and.Dots")
        assert all(c.isalnum() or c == "-" for c in result)
        assert not result.startswith("-")
        assert not result.endswith("-")
        assert result == result.lower()


# ---------------------------------------------------------------------------
# sanitize_hostname is an alias of sanitize_project_name
# ---------------------------------------------------------------------------
class TestSanitizeHostnameAlias:
    @pytest.mark.parametrize(
        "name",
        [
            "My_Project",
            "a__b",
            "--foo--",
            "Hello World!",
            "café",
            "123",
            "",
            "   ",
            "a.b.c",
            "UPPER",
            "foo_bar-baz",
            "___",
            "!!!",
            "my.project_name",
            "feature/login",
            "Weird__Name!!With/Slashes",
        ],
    )
    def test_hostname_equals_project_name(self, name):
        assert sanitize_hostname(name) == sanitize_project_name(name)

    def test_alias_is_same_callable_behavior_on_typical_input(self):
        assert sanitize_hostname("Business_Intelligence") == "business-intelligence"


# ---------------------------------------------------------------------------
# generate_secret_key
# ---------------------------------------------------------------------------
class TestGenerateSecretKey:
    def test_returns_non_empty_string(self):
        key = generate_secret_key()
        assert isinstance(key, str)
        assert len(key) > 0

    def test_high_entropy_length(self):
        # token_urlsafe(64) -> ~86 url-safe chars.
        key = generate_secret_key()
        assert len(key) >= 64

    def test_different_each_call(self):
        keys = {generate_secret_key() for _ in range(50)}
        # Collisions here would indicate a broken RNG.
        assert len(keys) == 50

    def test_not_equal_to_insecure_default(self):
        for _ in range(10):
            assert generate_secret_key() != INSECURE_DEFAULT_SECRET_KEY

    def test_url_safe_charset(self):
        key = generate_secret_key()
        allowed = set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        assert set(key) <= allowed

    def test_does_not_contain_insecure_prefix(self):
        key = generate_secret_key()
        assert not key.startswith("django-insecure")


# ---------------------------------------------------------------------------
# INSECURE_DEFAULT_SECRET_KEY + DEFAULT_ENV_VARS
# ---------------------------------------------------------------------------
class TestConstants:
    def test_insecure_default_value(self):
        assert INSECURE_DEFAULT_SECRET_KEY == "django-insecure-secret-key"

    def test_default_env_vars_is_dict(self):
        assert isinstance(DEFAULT_ENV_VARS, dict)

    def test_default_env_vars_required_keys(self):
        expected = {
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB",
            "ALLOWED_HOSTS",
            "DEBUG",
            "DJANGO_SECRET_KEY",
            "SITE_DOMAIN",
            "CADDY_EMAIL",
        }
        assert expected <= set(DEFAULT_ENV_VARS.keys())

    def test_default_env_vars_values(self):
        assert DEFAULT_ENV_VARS["POSTGRES_USER"] == "user"
        assert DEFAULT_ENV_VARS["POSTGRES_PASSWORD"] == "password"
        assert DEFAULT_ENV_VARS["POSTGRES_DB"] == "database"
        assert DEFAULT_ENV_VARS["DEBUG"] == "True"

    def test_default_env_vars_all_string_values(self):
        assert all(isinstance(v, str) for v in DEFAULT_ENV_VARS.values())

    def test_default_secret_key_points_to_insecure_default(self):
        # The default env deliberately ships the insecure placeholder;
        # generate_secret_key() is what replaces it per-environment.
        assert DEFAULT_ENV_VARS["DJANGO_SECRET_KEY"] == INSECURE_DEFAULT_SECRET_KEY


# ---------------------------------------------------------------------------
# resolve_project_name
# ---------------------------------------------------------------------------
class TestResolveProjectName:
    def _write_config(self, root, body):
        ilha_dir = root / ".ilha"
        ilha_dir.mkdir(parents=True, exist_ok=True)
        (ilha_dir / "config.yml").write_text(body)

    def test_reads_project_name_from_config(self, tmp_path):
        self._write_config(tmp_path, "project_name: my_cool_app\n")
        assert resolve_project_name(tmp_path) == "my_cool_app"

    def test_returns_unsanitized_value(self, tmp_path):
        # resolve_project_name should NOT sanitize; underscores preserved.
        self._write_config(tmp_path, "project_name: Business_Intelligence\n")
        assert resolve_project_name(tmp_path) == "Business_Intelligence"

    def test_falls_back_to_dir_name_when_no_config(self, tmp_path):
        proj = tmp_path / "some_project"
        proj.mkdir()
        assert resolve_project_name(proj) == "some_project"

    def test_falls_back_when_config_missing_key(self, tmp_path):
        proj = tmp_path / "named_dir"
        proj.mkdir()
        self._write_config(proj, "other_key: value\n")
        assert resolve_project_name(proj) == "named_dir"

    def test_falls_back_when_project_name_empty(self, tmp_path):
        proj = tmp_path / "fallback_dir"
        proj.mkdir()
        self._write_config(proj, "project_name:\n")  # -> None
        assert resolve_project_name(proj) == "fallback_dir"

    def test_falls_back_on_invalid_yaml(self, tmp_path):
        proj = tmp_path / "broken_yaml_dir"
        proj.mkdir()
        self._write_config(proj, "project_name: [unclosed\n:::bad")
        assert resolve_project_name(proj) == "broken_yaml_dir"

    def test_falls_back_when_yaml_is_not_a_mapping(self, tmp_path):
        proj = tmp_path / "list_yaml_dir"
        proj.mkdir()
        self._write_config(proj, "- just\n- a\n- list\n")
        # config.get would fail on a list -> exception -> fallback to dir name.
        assert resolve_project_name(proj) == "list_yaml_dir"

    def test_accepts_path_like_string(self, tmp_path):
        proj = tmp_path / "strpath"
        proj.mkdir()
        assert resolve_project_name(str(proj)) == "strpath"

    def test_numeric_project_name_coerced_to_str(self, tmp_path):
        proj = tmp_path / "numname"
        proj.mkdir()
        self._write_config(proj, "project_name: 12345\n")
        result = resolve_project_name(proj)
        assert result == "12345"
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# resolve_default_branch (git mocked)
# ---------------------------------------------------------------------------
class _FakeCompleted:
    def __init__(self, stdout=""):
        self.stdout = stdout


def _make_git_fake(*, head=None, current=None, head_exc=None, current_exc=None):
    """Build a fake subprocess.run for git that records timeout usage."""
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs))
        # cmd is like: ["git", "-C", root, "symbolic-ref", "refs/remotes/origin/HEAD"]
        if "symbolic-ref" in cmd:
            if head_exc is not None:
                raise head_exc
            return _FakeCompleted(head or "")
        if "branch" in cmd:
            if current_exc is not None:
                raise current_exc
            return _FakeCompleted(current or "")
        raise AssertionError(f"unexpected git command: {cmd}")

    fake_run.calls = calls
    return fake_run


class TestResolveDefaultBranch:
    def test_uses_origin_head_symbolic_ref(self, tmp_path, monkeypatch):
        fake = _make_git_fake(head="refs/remotes/origin/main")
        monkeypatch.setattr(subprocess, "run", fake)
        assert resolve_default_branch(tmp_path) == "main"

    def test_origin_head_with_nonstandard_name(self, tmp_path, monkeypatch):
        fake = _make_git_fake(head="refs/remotes/origin/develop")
        monkeypatch.setattr(subprocess, "run", fake)
        assert resolve_default_branch(tmp_path) == "develop"

    def test_falls_back_to_current_branch(self, tmp_path, monkeypatch):
        fake = _make_git_fake(
            head_exc=subprocess.CalledProcessError(1, "git"),
            current="feature-x",
        )
        monkeypatch.setattr(subprocess, "run", fake)
        assert resolve_default_branch(tmp_path) == "feature-x"

    def test_falls_back_to_master_when_both_fail(self, tmp_path, monkeypatch):
        fake = _make_git_fake(
            head_exc=subprocess.CalledProcessError(1, "git"),
            current_exc=subprocess.CalledProcessError(1, "git"),
        )
        monkeypatch.setattr(subprocess, "run", fake)
        assert resolve_default_branch(tmp_path) == "master"

    def test_falls_back_when_git_not_installed(self, tmp_path, monkeypatch):
        fake = _make_git_fake(
            head_exc=FileNotFoundError("git"),
            current_exc=FileNotFoundError("git"),
        )
        monkeypatch.setattr(subprocess, "run", fake)
        assert resolve_default_branch(tmp_path) == "master"

    def test_falls_back_on_timeout(self, tmp_path, monkeypatch):
        fake = _make_git_fake(
            head_exc=subprocess.TimeoutExpired("git", 5),
            current_exc=subprocess.TimeoutExpired("git", 5),
        )
        monkeypatch.setattr(subprocess, "run", fake)
        assert resolve_default_branch(tmp_path) == "master"

    def test_ignores_unexpected_ref_prefix(self, tmp_path, monkeypatch):
        # symbolic-ref returns something that isn't under origin/ -> fall through.
        fake = _make_git_fake(head="refs/heads/weird", current="localbranch")
        monkeypatch.setattr(subprocess, "run", fake)
        assert resolve_default_branch(tmp_path) == "localbranch"

    def test_empty_current_branch_falls_back_to_master(self, tmp_path, monkeypatch):
        fake = _make_git_fake(
            head_exc=subprocess.CalledProcessError(1, "git"),
            current="",  # detached HEAD -> empty
        )
        monkeypatch.setattr(subprocess, "run", fake)
        assert resolve_default_branch(tmp_path) == "master"

    def test_passes_timeout_kwarg(self, tmp_path, monkeypatch):
        fake = _make_git_fake(head="refs/remotes/origin/main")
        monkeypatch.setattr(subprocess, "run", fake)
        resolve_default_branch(tmp_path)
        assert fake.calls, "expected at least one git invocation"
        for _cmd, kwargs in fake.calls:
            assert "timeout" in kwargs
            assert isinstance(kwargs["timeout"], (int, float))

    def test_real_non_git_dir_returns_master(self, tmp_path):
        # No git repo here; the real git will error and we fall back to master.
        # (Deterministic: an empty tmp dir is never a repo.)
        assert resolve_default_branch(tmp_path) == "master"


# ---------------------------------------------------------------------------
# get_compose_command (docker probes mocked)
# ---------------------------------------------------------------------------
def _make_compose_fake(behaviors):
    """behaviors: list of ('return' | Exception) applied per subprocess.run call.

    A 'return' entry means the probe succeeded.
    """
    calls = []
    seq = list(behaviors)

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs))
        behavior = seq.pop(0) if seq else "return"
        if isinstance(behavior, Exception):
            raise behavior
        return _FakeCompleted("Docker Compose version v2.0.0")

    fake_run.calls = calls
    return fake_run


class TestGetComposeCommand:
    def test_docker_compose_v2_present(self, monkeypatch):
        fake = _make_compose_fake(["return"])
        monkeypatch.setattr(subprocess, "run", fake)
        assert get_compose_command() == "docker compose"
        # First probe is the v2 plugin.
        assert fake.calls[0][0] == ["docker", "compose", "version"]

    def test_falls_back_to_legacy_docker_compose(self, monkeypatch):
        fake = _make_compose_fake(
            [subprocess.CalledProcessError(1, "docker"), "return"]
        )
        monkeypatch.setattr(subprocess, "run", fake)
        assert get_compose_command() == "docker-compose"
        assert fake.calls[1][0] == ["docker-compose", "version"]

    def test_docker_missing_falls_back_to_legacy(self, monkeypatch):
        fake = _make_compose_fake(
            [FileNotFoundError("docker"), "return"]
        )
        monkeypatch.setattr(subprocess, "run", fake)
        assert get_compose_command() == "docker-compose"

    def test_both_missing_returns_legacy_default(self, monkeypatch):
        fake = _make_compose_fake(
            [FileNotFoundError("docker"), FileNotFoundError("docker-compose")]
        )
        monkeypatch.setattr(subprocess, "run", fake)
        assert get_compose_command() == "docker-compose"

    def test_v2_timeout_falls_back_to_legacy(self, monkeypatch):
        fake = _make_compose_fake(
            [subprocess.TimeoutExpired("docker compose", 10), "return"]
        )
        monkeypatch.setattr(subprocess, "run", fake)
        assert get_compose_command() == "docker-compose"

    def test_both_timeout_returns_legacy_default(self, monkeypatch):
        fake = _make_compose_fake(
            [
                subprocess.TimeoutExpired("docker compose", 10),
                subprocess.TimeoutExpired("docker-compose", 10),
            ]
        )
        monkeypatch.setattr(subprocess, "run", fake)
        assert get_compose_command() == "docker-compose"

    def test_passes_timeout_kwarg_on_probes(self, monkeypatch):
        fake = _make_compose_fake(["return"])
        monkeypatch.setattr(subprocess, "run", fake)
        get_compose_command()
        assert fake.calls, "expected at least one docker probe"
        for _cmd, kwargs in fake.calls:
            assert "timeout" in kwargs
            assert isinstance(kwargs["timeout"], (int, float))
            assert kwargs["timeout"] > 0

    def test_timeout_kwarg_present_on_both_probes(self, monkeypatch):
        fake = _make_compose_fake(
            [subprocess.CalledProcessError(1, "docker"), "return"]
        )
        monkeypatch.setattr(subprocess, "run", fake)
        get_compose_command()
        assert len(fake.calls) == 2
        for _cmd, kwargs in fake.calls:
            assert "timeout" in kwargs
