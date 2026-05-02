from pathlib import Path

from ilha.core.environment_manager import EnvironmentManager


def _mark_django(*paths: Path) -> None:
    """Plant a manage.py marker so detect_project_footprint reports has_django=True."""
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)
        (p / "manage.py").write_text("# django marker\n")


def test_env_contains_csrf_and_proxy_defaults(tmp_path: Path):
    _mark_django(tmp_path)
    em = EnvironmentManager(project_root=tmp_path)
    worktree = tmp_path / "worktrees" / "feature-x"
    ok = em.create_worktree_env("feature-x", worktree)
    assert ok
    env_dt = worktree / ".ilha" / "env.ilha"
    text = env_dt.read_text()
    assert "USE_X_FORWARDED_HOST=True" in text
    assert "CSRF_TRUSTED_ORIGINS=http://" in text


def test_domain_overrides_add_csrf_and_proxy(tmp_path: Path):
    _mark_django(tmp_path)
    em = EnvironmentManager(project_root=tmp_path)
    worktree = tmp_path / "worktrees" / "feature-y"
    ok = em.create_worktree_env("feature-y", worktree, domain="app.example.com")
    assert ok
    _mark_django(worktree)
    em.apply_domain_overrides(worktree, "app.example.com")
    env_dt = worktree / ".ilha" / "env.ilha"
    text = env_dt.read_text()
    assert "USE_X_FORWARDED_HOST=True" in text
    assert "SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https" in text
    assert "CSRF_TRUSTED_ORIGINS=https://app.example.com,http://app.example.com,https://*.example.com" in text


def test_production_domain_values_strip_scheme_and_path(tmp_path: Path):
    em = EnvironmentManager(project_root=tmp_path)
    values = em._production_domain_env_values("https://app.example.com/login/")

    assert values["site_domain"] == "app.example.com"
    assert values["csrf_trusted_origins"] == (
        "https://app.example.com,http://app.example.com,https://*.example.com"
    )


def test_ip_overrides_add_csrf_and_proxy(tmp_path: Path):
    _mark_django(tmp_path)
    em = EnvironmentManager(project_root=tmp_path)
    worktree = tmp_path / "worktrees" / "feature-z"
    ok = em.create_worktree_env("feature-z", worktree)
    assert ok
    _mark_django(worktree)
    assert em.apply_ip_overrides(worktree, "203.0.113.10")
    env_dt = worktree / ".ilha" / "env.ilha"
    text = env_dt.read_text()
    assert "USE_X_FORWARDED_HOST=True" in text
    assert "SECURE_PROXY_SSL_HEADER" not in text  # no https on IP
    assert "CSRF_TRUSTED_ORIGINS=http://203.0.113.10" in text


def test_non_django_project_omits_csrf_and_proxy(tmp_path: Path):
    """Static-only / non-Django projects must not get Django-only env vars."""
    em = EnvironmentManager(project_root=tmp_path)
    worktree = tmp_path / "worktrees" / "static"
    ok = em.create_worktree_env("static", worktree)
    assert ok
    text = (worktree / ".ilha" / "env.ilha").read_text()
    assert "USE_X_FORWARDED_HOST" not in text
    assert "CSRF_TRUSTED_ORIGINS" not in text
    assert "ALLOWED_HOSTS" not in text
    # SITE_DOMAIN is always emitted (Caddy proxy target), as a bare host
    assert "SITE_DOMAIN=" in text
    assert "SITE_DOMAIN=http://" not in text

