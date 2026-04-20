# Zero-downtime deploy for `ilha droplet put`

## How it works

`_start_app_remote()` uses a canary container + Caddy admin API to avoid dropping
requests during a deploy. The old web container serves traffic throughout the build
and canary validation phases.

### Sequence

```
old web serving ─────────────────────────────────────────────────┐
                                                                 │
1. docker compose build web         (minutes — old still serves) │
1b. docker compose up -d db redis   (or db only if no redis svc) │
    wait until pg_isready inside db (canary needs Compose DNS)   │
2. docker run canary                (SKIP_MIGRATIONS=true)       │
3. Health check canary              (curl inside container)      │
   └─ If FAIL → remove canary, abort. Old still serves.         │
4. docker stop caddy_monitor        (pause drift detection)      │
5. Caddy admin API: swap upstream   (old-web → canary)           │
                    ┌── canary serves traffic ───────────────────┤
6. docker compose up -d web         (stops old, starts new)      │
7. Connect new web to Caddy network                              │
8. Health check new web                                          │
9. Caddy admin API: swap upstream   (canary → new-web)           │
                    ┌── new web serves traffic ──────────────────┤
10. Remove canary, restart monitor                               │
11. Restart workers                                              │
```

Zero dropped requests: old serves until step 5, canary bridges steps 5–9,
new web takes over at step 9.

## Why pause caddy_monitor

The caddy-docker-monitor polls every 5 seconds and has drift detection that
auto-reconfigures Caddy when the config doesn't match container labels. If we
swap the upstream via the admin API without pausing the monitor, it will
overwrite our change within 5 seconds.

The monitor is stopped before the swap and restarted after the new compose web
container is running with the correct labels — at that point, the monitor's
label-based config matches reality.

A cleanup trap ensures the monitor is restarted even if the script exits early.

## Canary container

The canary is a lightweight `docker run` from the built image:

- **No Caddy labels** — the monitor won't try to route to it
- **`SKIP_MIGRATIONS=true`** — only validates the app boots and serves HTTP
- **`STARTUP_PERFORMANCE_MODE=true`** — fast startup
- **Connected to `ilha_caddy_proxy` network** — so Caddy can reach it when we swap
- **Connected to app network** — so it can reach db/redis for the health check

### Dependencies before the canary

The canary is started with `docker run --network ilha_caddy_proxy` first, then
`docker network connect` joins the Compose project network (discovered from the
running **db** container). Docker internal DNS names such as `db` and `redis`
only resolve on that Compose network.

Before starting the canary, the start script runs `docker compose up -d db redis`
(falling back to `db` only if `db redis` fails, e.g. no redis service). It then
waits until `pg_isready` succeeds inside the db container. That covers cold
droplets, `docker compose down` + `--db-only` restore, or a stopped db container
— in those cases `docker compose ps -q db` was empty and the canary previously
never joined the app network, causing `could not translate host name "db"`.

If the app network cannot be resolved from the db container, or
`docker network connect` fails, the script exits with an explicit error instead
of continuing into a failing health check.

The canary runs `start.sh` (the normal entrypoint) but skips migrations and just
boots uvicorn. If `/health-check/` responds within 60 seconds, the canary is
healthy and we proceed with the swap.

## Rollback

If the canary fails its health check, the script exits immediately. The old web
container is still running and serving traffic — no disruption occurs. The EXIT
trap cleans up the canary container and restarts the monitor.

## Limitations

- **Backwards-incompatible migrations**: The canary skips migrations, but the new
  compose web container will run them. If a migration drops a column the old code
  needs, there's a brief window during the compose recreate. For these cases,
  deploy the migration separately first using `--db-only`.

- **Caddy admin API must be reachable**: The deploy script hits `localhost:2019`.
  If Caddy is down, the swap steps are skipped and the deploy falls back to a
  standard compose recreate (with a brief gap).

## Debugging

If a deploy fails mid-swap:

1. Check the deploy log on the server: `ls /tmp/ilha-start-app.log-*` then
   `tail` the latest session file (ilha names logs `/tmp/ilha-start-app.log-<session_id>`).
2. Check if the canary is still running: `docker ps | grep web_zd_canary`
3. Check if the monitor is stopped: `docker ps | grep caddy_monitor`

### `db` hostname resolution / empty app network

If logs show `could not translate host name "db"` or an error about determining
the Compose app network:

- Run `cd /app && docker compose --env-file .env ps -a` and ensure **db** is
  **Up** (not only **Exited** — `docker compose ps -q db` lists running
  containers only).
- Ensure the Postgres service in `docker-compose.yml` is named **`db`** (or
  adjust the deploy script for your project).
- Manually recover: `docker compose --env-file .env up -d db redis` (or `db`
  only), then re-run `ilha droplet put`.

### Manual recovery

```bash
# Remove canary and restart monitor
docker rm -f web_zd_canary
docker start caddy_monitor

# If Caddy is routing to the canary (which no longer exists):
# The monitor will auto-reconfigure within 5 seconds of being restarted.
# Or force it by restarting the monitor:
docker restart caddy_monitor
```

## Related

- [SLIM_DEPLOY_SMOKE_CHECK.md](SLIM_DEPLOY_SMOKE_CHECK.md) — smoke check details
- [STAGING_DROPLET_FIXES.md](STAGING_DROPLET_FIXES.md) — CSRF/env fixes for production domains
