from pathlib import Path

from ilha.commands.setup import SetupManager


def write_dummy_django(tmp: Path) -> Path:
    (tmp / "manage.py").write_text("print('ok')\n")
    pkg = tmp / "myproj"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "settings.py").write_text("ALLOWED_HOSTS = ['*']\n")
    return pkg / "settings.py"


def test_detects_django_and_reports_missing(tmp_path: Path, monkeypatch):
    settings_py = write_dummy_django(tmp_path)
    sm = SetupManager(project_root=tmp_path)
    # call internal helpers
    found = sm._locate_django_settings(tmp_path)
    assert found and found == settings_py
    missing = sm._find_missing_env_config(settings_py)
    # Should flag at least ALLOWED_HOSTS env missing
    assert any("ALLOWED_HOSTS" in m for m in missing)


def test_monkey_patch_appends_block(tmp_path: Path):
    settings_py = write_dummy_django(tmp_path)
    sm = SetupManager(project_root=tmp_path)
    assert sm._monkey_patch_settings(settings_py)
    text = settings_py.read_text()
    assert "ilha auto-added settings" in text
    # idempotent
    assert sm._monkey_patch_settings(settings_py)

