"""Unit tests for project footprint detection."""

from pathlib import Path

import pytest

from ilha.utils.project_footprint import detect_project_footprint


def _write_compose(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_static_caddy_only_project(tmp_path: Path) -> None:
    """A Caddy-only project reports static_service / port without Django/Postgres."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        "services:\n  caddy:\n    image: caddy:2-alpine\n    ports: ['8080:80']\n",
    )

    fp = detect_project_footprint(tmp_path)

    assert fp.has_django is False
    assert fp.has_postgres is False
    assert fp.has_static_service is True
    assert fp.primary_static_service == "caddy"
    assert fp.primary_static_port == 80


def test_static_service_demoted_when_django_present(tmp_path: Path) -> None:
    """A Caddy sidecar in a Django project is not treated as the primary static service."""
    (tmp_path / "manage.py").write_text("")
    _write_compose(
        tmp_path / "docker-compose.yml",
        (
            "services:\n"
            "  web:\n    image: django-app\n"
            "  caddy:\n    image: caddy:2-alpine\n"
        ),
    )

    fp = detect_project_footprint(tmp_path)

    assert fp.has_django is True
    assert fp.has_static_service is True  # caddy is still present in compose
    assert fp.primary_static_service is None  # but it is not the primary app
    assert fp.primary_static_port is None


def test_postgres_image_detected(tmp_path: Path) -> None:
    """An explicit Postgres image flips has_postgres."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        (
            "services:\n"
            "  web:\n    image: my-app\n"
            "  db:\n    image: postgres:16\n"
        ),
    )

    fp = detect_project_footprint(tmp_path)

    assert fp.has_postgres is True


def test_db_service_without_postgres_image_does_not_match(tmp_path: Path) -> None:
    """`db` alone (no Postgres image, no POSTGRES_* env) is not Postgres."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        "services:\n  db:\n    image: mysql:8\n",
    )

    fp = detect_project_footprint(tmp_path)

    assert fp.has_postgres is False


def test_db_service_with_postgres_env_is_postgres(tmp_path: Path) -> None:
    """`db` with POSTGRES_* env vars counts as Postgres even without an image tag we recognize."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        (
            "services:\n"
            "  db:\n"
            "    image: pgvector/pgvector:0.7\n"
            "    environment:\n"
            "      POSTGRES_PASSWORD: x\n"
        ),
    )

    fp = detect_project_footprint(tmp_path)

    assert fp.has_postgres is True


def test_pyproject_with_django_dependency_marks_django(tmp_path: Path) -> None:
    """pyproject.toml mentioning django flips has_django even without manage.py."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['Django>=5']\n"
    )

    fp = detect_project_footprint(tmp_path)

    assert fp.has_django is True


def test_base_compose_is_authoritative_for_footprint(tmp_path: Path) -> None:
    """The user's docker-compose.yml is authoritative for footprint detection.

    The overlay is a thin delta and is consulted only when the user's compose
    is missing (e.g. tests that drop the overlay alone).
    """
    _write_compose(
        tmp_path / "docker-compose.yml",
        "services:\n"
        "  caddy:\n    image: caddy:2-alpine\n"
        "  db:\n    image: postgres:16\n",
    )

    fp = detect_project_footprint(tmp_path)

    assert fp.has_postgres is True
    # postgres is a backing service; the primary static is still caddy.
    assert fp.has_static_service is True
    assert fp.primary_static_service == "caddy"


def test_overlay_only_used_when_base_missing(tmp_path: Path) -> None:
    """If only the overlay exists (e.g. before the user wrote a compose),
    footprint detection falls back to it."""
    _write_compose(
        tmp_path / ".ilha" / "docker-compose.overlay.yml",
        "services:\n  caddy:\n    image: caddy:2-alpine\n",
    )

    fp = detect_project_footprint(tmp_path)

    assert fp.has_static_service is True
    assert fp.primary_static_service == "caddy"


def test_no_compose_file(tmp_path: Path) -> None:
    fp = detect_project_footprint(tmp_path)
    assert fp.has_django is False
    assert fp.has_postgres is False
    assert fp.has_static_service is False


# ── service classification (db / dependency / worker) ──────────────────────

def test_classify_uses_web_depends_on_for_dependency_services(tmp_path: Path) -> None:
    """When `web.depends_on` is declared, dependency_services follows it
    verbatim (filtered to known, non-worker, non-web services)."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  web:
    build: .
    depends_on: [db, redis]
  db:
    image: postgres:16-alpine
  redis:
    image: redis:7-alpine
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.db_service == "db"
    assert fp.web_dependency_services == ("db", "redis")
    assert fp.worker_service_names == frozenset()


def test_classify_falls_back_to_image_only_services_without_depends_on(tmp_path: Path) -> None:
    """Without `depends_on`, dependency_services is every image-only,
    non-worker, non-web service (the infra tier)."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  web:
    build: .
  db:
    image: postgres:16-alpine
  redis:
    image: redis:7-alpine
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert set(fp.web_dependency_services) == {"db", "redis"}
    assert fp.db_service == "db"


def test_classify_excludes_workers_from_dependency_services(tmp_path: Path) -> None:
    """A worker service (matched by name OR command) must not be listed as
    a dependency, even when web.depends_on includes it."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  web:
    build: .
    depends_on: [db, redis, rq-worker-1]
  db:
    image: postgres:16-alpine
  redis:
    image: redis:7-alpine
  rq-worker-1:
    build: .
    command: rq worker default
  celery-worker:
    build: .
    command: celery -A app worker -l info
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.db_service == "db"
    assert fp.web_dependency_services == ("db", "redis")  # workers stripped
    assert fp.worker_service_names == frozenset({"rq-worker-1", "celery-worker"})


def test_classify_no_db_when_no_postgres_service(tmp_path: Path) -> None:
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  web:
    build: .
  redis:
    image: redis:7-alpine
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.db_service is None
    assert "redis" in fp.web_dependency_services
    assert fp.worker_service_names == frozenset()


def test_classify_handles_depends_on_long_form(tmp_path: Path) -> None:
    """Compose long-form `depends_on: {svc: {condition: ...}}` is still parsed."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  web:
    build: .
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_started
  db:
    image: postgres:16-alpine
  redis:
    image: redis:7-alpine
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert set(fp.web_dependency_services) == {"db", "redis"}


# ── archetype detection ────────────────────────────────────────────────────

from ilha.utils.project_footprint import ProjectArchetype


def test_archetype_django_spa_when_python_backend_and_node_frontend(tmp_path: Path) -> None:
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  web:
    build: .
    command: daphne -b 0.0.0.0 -p 8000 config.asgi:application
    depends_on: [db]
  frontend:
    image: node:20-alpine
    command: sh -c "npm install && npm run dev -- --host 0.0.0.0"
  db:
    image: postgres:16-alpine
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.archetype == ProjectArchetype.DJANGO_SPA
    assert fp.backend_service == "web"
    assert fp.frontend_service == "frontend"


def test_archetype_solo_django_when_no_frontend(tmp_path: Path) -> None:
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  web:
    build: .
    command: gunicorn config.wsgi
  db:
    image: postgres:16-alpine
  redis:
    image: redis:7-alpine
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.archetype == ProjectArchetype.SOLO_DJANGO
    assert fp.backend_service == "web"
    assert fp.frontend_service is None


def test_archetype_static_only_for_caddy_image(tmp_path: Path) -> None:
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  caddy:
    image: caddy:2-alpine
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.archetype == ProjectArchetype.STATIC_ONLY
    assert fp.backend_service is None


def test_archetype_uses_dockerfile_to_classify_backend(tmp_path: Path) -> None:
    """A built service with no python-ish image/command but a python Dockerfile
    should still be detected as a backend."""
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-slim\nWORKDIR /app\n")
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  api:
    build: .
    command: ./entrypoint.sh
  ui:
    image: node:20-alpine
    command: npm run dev
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.archetype == ProjectArchetype.DJANGO_SPA
    assert fp.backend_service == "api"
    assert fp.frontend_service == "ui"


def test_backend_is_asgi_when_command_uses_daphne(tmp_path: Path) -> None:
    """Daphne/Uvicorn/Hypercorn/Granian → backend is ASGI; ``/ws/*`` should
    later route to the backend by default."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  web:
    build: .
    command: daphne -b 0.0.0.0 -p 8000 config.asgi:application
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.backend_is_asgi is True


def test_backend_is_not_asgi_for_wsgi_gunicorn(tmp_path: Path) -> None:
    """Gunicorn isn't necessarily ASGI; treat as non-ASGI by default
    (conservative — avoids routing /ws/* away from a frontend that might
    actually want it)."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  web:
    build: .
    command: gunicorn config.wsgi:application
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.backend_is_asgi is False


def test_backend_is_asgi_for_uvicorn(tmp_path: Path) -> None:
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  api:
    build: .
    command: uvicorn app.asgi:app --host 0.0.0.0
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.backend_is_asgi is True


def test_archetype_workers_excluded_from_backend(tmp_path: Path) -> None:
    """A worker-style service must not steal the backend slot."""
    _write_compose(
        tmp_path / "docker-compose.yml",
        """
services:
  rq-worker-1:
    image: python:3.13-slim
    command: rq worker default
  web:
    build: .
    command: daphne config.asgi:application
""".lstrip(),
    )
    fp = detect_project_footprint(tmp_path)
    assert fp.backend_service == "web"  # not rq-worker-1
    assert fp.archetype == ProjectArchetype.SOLO_DJANGO
