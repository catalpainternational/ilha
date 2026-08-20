"""Remote slim-deploy directory layout.

Single source of truth for paths on a slim droplet. ``droplet logs`` previously
hard-coded ``/root/app`` while the deploy pipeline writes ``/app``.
"""

from __future__ import annotations

REMOTE_APP_ROOT = "/app"
REMOTE_CADDY_DIR = "/app/caddy"
REMOTE_VOLUMES_DIR = "/app/volumes"
REMOTE_DEPLOY_IDENTITY = "/app/.ilha/deploy.json"
REMOTE_ENV_FILE = "/app/.env"
