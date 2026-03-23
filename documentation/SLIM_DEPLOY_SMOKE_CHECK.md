# Slim deploy smoke check: failure mode and fix

## Symptom

After a successful remote **Application services started**, slim deploy fails with:

```text
[ERROR] Smoke check failed: local_app -> http://127.0.0.1:8000/ (curl: (7) Failed to connect to 127.0.0.1 port 8000 ...)
```

## Root cause

In Caddy-fronted slim deploys, the Django/uvicorn process listens on **port 8000 inside the `web` container only** (`expose: ["8000"]`), not on the host’s loopback. A smoke probe that runs plain `curl http://127.0.0.1:8000/...` **on the host** always gets *connection refused* if nothing publishes `8000` to the host.

The intended path is to run curl **inside** the `web` container:

`docker compose exec -T web sh -c 'curl -fsS http://127.0.0.1:8000/...'`

ilha only does that when it believes a `web` service exists. The original check used **`docker compose config --services`** only. That can return a non-zero exit (interpolation, env, Compose version quirks) or omit services that use **profiles**, even when `docker compose up` just started a healthy `web` container. When that happened, ilha fell back to **host** curl and produced the persistent false failure above.

## Fix (ilha)

1. **`remote_compose_service_known`** (`ilha/commands/push/remote_compose_probe.py`): treat the project as having `web` if `docker compose ps -q web` prints a container id; otherwise fall back to `config --services`.
2. **`SlimDeployManager._remote_service_exists`** and **`DeployDoctor._remote_service_exists`** delegate to that helper (DRY).
3. **On smoke failure** (container probe path): ilha runs **`docker compose logs --tail 100 web`**, prints the last 40 lines to the console, and stores a capped excerpt in the deploy report under **`web_logs_tail`** (for JSON / tooling).

## Verifying on a server

SSH to the droplet as usual, then:

```bash
cd /app
docker compose --env-file .env ps -q web | head -1   # should print a container id
docker compose --env-file .env exec -T web sh -c 'curl -fsS --max-time 5 http://127.0.0.1:8000/'
```

If the inner curl fails, the app is not listening on 8000 inside the container (different issue: crashes, migrations, wrong port in CMD).

## Related: merged `.env` on slim deploy

Slim deploy’s `_build_merged_env` now sets **`SITE_DOMAIN` to the bare host** (e.g. `staging.example.com` or the raw IP), not `https://…`, so Django/allauth do not build broken CSRF/origin values. This matches `environment_manager`’s production-domain helpers and `STAGING_DROPLET_FIXES.md`.

## Related docs

- `documentation/ARCHITECTURE.md` — “Slim deploy: smoke checks and hardening”
- `documentation/STAGING_DROPLET_FIXES.md` — CSRF / `PROJECT_ROOT` on droplets (separate from smoke)
