from pathlib import Path

from ilha.core.environment_manager import EnvironmentManager


def test_env_contains_csrf_and_proxy_defaults(tmp_path: Path):
    em = EnvironmentManager(project_root=tmp_path)
    worktree = tmp_path / "worktrees" / "feature-x"
    ok = em.create_worktree_env("feature-x", worktree)
    assert ok
    env_dt = worktree / ".ilha" / "env.ilha"
    text = env_dt.read_text()
    assert "USE_X_FORWARDED_HOST=True" in text
    assert "CSRF_TRUSTED_ORIGINS=http://" in text


def test_domain_overrides_add_csrf_and_proxy(tmp_path: Path):
    em = EnvironmentManager(project_root=tmp_path)
    worktree = tmp_path / "worktrees" / "feature-y"
    ok = em.create_worktree_env("feature-y", worktree, domain="app.example.com")
    assert ok
    env_dt = worktree / ".ilha" / "env.ilha"
    text = env_dt.read_text()
    assert "USE_X_FORWARDED_HOST=True" in text
    assert "SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https" in text
    assert "CSRF_TRUSTED_ORIGINS=https://app.example.com http://app.example.com https://*.example.com" in text


def test_ip_overrides_add_csrf_and_proxy(tmp_path: Path):
    em = EnvironmentManager(project_root=tmp_path)
    worktree = tmp_path / "worktrees" / "feature-z"
    ok = em.create_worktree_env("feature-z", worktree)
    assert ok
    assert em.apply_ip_overrides(worktree, "203.0.113.10")
    env_dt = worktree / ".ilha" / "env.ilha"
    text = env_dt.read_text()
    assert "USE_X_FORWARDED_HOST=True" in text
    assert "SECURE_PROXY_SSL_HEADER" not in text  # no https on IP
    assert "CSRF_TRUSTED_ORIGINS=http://203.0.113.10" in text

