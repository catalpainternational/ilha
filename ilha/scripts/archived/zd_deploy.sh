#!/bin/bash
# Zero-downtime application start for ilha slim deploy.
#
# Runs on the TARGET server (not the developer's machine). Renders nothing
# — all configuration comes from environment variables injected by the
# Python caller (SlimDeployManager._start_app_remote):
#
#   APP_ROOT          - remote app directory (e.g. /app)
#   WEB_SERVICE       - compose service name for the primary web app
#   WEB_PORT          - port the web app listens on inside the container
#   HEALTH_PATH       - HTTP path probed during canary / new-web health checks
#   DB_SERVICE        - compose service used for pg_isready + network probe
#   DEPS_SERVICES     - space-separated list passed to `docker compose up -d`
#   WORKER_SERVICES   - space-separated list of worker services to recreate
#                       (empty string ⇒ Phase 10 is a no-op)
#   BUILD_WEB         - "1" to run `docker compose build <WEB_SERVICE>` in Phase 1
#
# Strategy:
#   1. Build new image while old container keeps serving traffic
#   2. Bring up dependency services; wait for Postgres readiness
#   3. Start a canary container from the new image and health-check it
#   4. If canary fails: abort (trap cleans up); old container still serves
#   5. Pause caddy_monitor to prevent drift correction during swap
#   6. Swap Caddy upstream to canary via admin API
#   7. Recreate the compose-managed web service
#   8. Health-check the recreated service. If it fails: LEAVE Caddy on canary
#      (disarm trap, keep canary alive) and exit non-zero — loud failure
#      instead of swapping traffic onto a broken upstream.
#   9. Cleanup canary + resume monitor
#   10. Recreate worker services (if any configured)

set -euo pipefail

: "${APP_ROOT:?APP_ROOT not set}"
: "${WEB_SERVICE:?WEB_SERVICE not set}"
: "${WEB_PORT:?WEB_PORT not set}"
: "${HEALTH_PATH:?HEALTH_PATH not set}"
: "${DB_SERVICE:?DB_SERVICE not set}"
: "${DEPS_SERVICES:?DEPS_SERVICES not set}"
WORKER_SERVICES="${WORKER_SERVICES:-}"
BUILD_WEB="${BUILD_WEB:-0}"

cd "$APP_ROOT"

# ── Helper: write Caddy upstream swap script ──
cat > /tmp/ilha_caddy_swap.py << 'PYEOF'
import sys, json
config = json.load(sys.stdin)
old_target, new_target = sys.argv[1], sys.argv[2]
routes = config.get("apps",{}).get("http",{}).get("servers",{}).get("srv0",{}).get("routes",[])
for route in routes:
  for handler in route.get("handle",[]):
    if handler.get("handler") == "reverse_proxy":
      for upstream in handler.get("upstreams",[]):
        if upstream.get("dial") == old_target:
          upstream["dial"] = new_target
    for subroute in handler.get("routes",[]):
      for sh in subroute.get("handle",[]):
        if sh.get("handler") == "reverse_proxy":
          for upstream in sh.get("upstreams",[]):
            if upstream.get("dial") == old_target:
              upstream["dial"] = new_target
json.dump(config, sys.stdout)
PYEOF

# ── Helper: swap Caddy upstream via admin API ──
caddy_swap() {
  local old_target="$1" new_target="$2"
  local current_config updated_config
  current_config=$(curl -sf http://localhost:2019/config/ 2>/dev/null) || return 1
  updated_config=$(echo "$current_config" | python3 /tmp/ilha_caddy_swap.py "$old_target" "$new_target") || return 1
  curl -sf -X POST http://localhost:2019/load \
    -H "Content-Type: application/json" \
    -d "$updated_config" >/dev/null 2>&1
}

# ── Cleanup trap: ensure monitor restarts and canary is removed on any exit ──
cleanup() {
  docker rm -f web_zd_canary 2>/dev/null || true
  docker start caddy_monitor 2>/dev/null || true
  rm -f /tmp/ilha_canary.env /tmp/ilha_caddy_swap.py 2>/dev/null || true
}
trap cleanup EXIT

# ── Phase 1: Build new image (old container keeps serving traffic) ──
echo "Phase 1: Building image..."
if [ "$BUILD_WEB" = "1" ]; then
  docker compose --env-file .env build "$WEB_SERVICE" 2>&1
else
  echo "Skipping build (using existing image)"
fi

# ── Phase 1b: Ensure dependency services are running (canary needs Compose DNS) ──
echo "Phase 1b: Ensuring dependency services are up: $DEPS_SERVICES"
# shellcheck disable=SC2086 -- DEPS_SERVICES is an intentional word-split list of compose services
if ! docker compose --env-file .env up -d $DEPS_SERVICES 2>&1; then
  echo "Phase 1b: dependency services up failed, trying $DB_SERVICE only..."
  docker compose --env-file .env up -d "$DB_SERVICE" 2>&1
fi
echo "Phase 1b: Waiting for Postgres ($DB_SERVICE) to accept connections..."
DB_READY=0
for i in $(seq 1 60); do
  DB_CID_READY=$(docker compose --env-file .env ps -q "$DB_SERVICE" 2>/dev/null | head -1)
  if [ -n "${DB_CID_READY:-}" ] && docker exec "$DB_CID_READY" pg_isready -q 2>/dev/null; then
    DB_READY=1
    echo "Postgres ready (attempt $i)"
    break
  fi
  sleep 2
done
if [ "$DB_READY" != "1" ]; then
  echo "ERROR: $DB_SERVICE not reachable after compose up. Check: docker compose --env-file .env logs $DB_SERVICE"
  exit 1
fi

# ── Phase 2: Start canary for validation ──
echo "Phase 2: Starting canary container for service '$WEB_SERVICE'..."
IMAGE=$(docker compose --env-file .env images "$WEB_SERVICE" --format json 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d[0]['Repository']}:{d[0]['Tag']}\")" 2>/dev/null \
  || docker compose --env-file .env config --format json 2>/dev/null \
  | python3 -c "import sys,json,os; c=json.load(sys.stdin); print(c['services'][os.environ['WEB_SERVICE']]['image'])" 2>/dev/null \
  || echo "")
if [ -z "$IMAGE" ]; then
  echo "ERROR: Could not determine $WEB_SERVICE service image. Check docker-compose.yml."
  exit 1
fi

# docker run --env-file does not strip quotes the way docker compose does,
# so we create a cleaned copy that strips surrounding quotes from values.
sed 's/^\([^=]*\)="\(.*\)"$/\1=\2/; s/^\([^=]*\)='"'"'\(.*\)'"'"'$/\1=\2/' .env > /tmp/ilha_canary.env

# Canary runs as standalone docker run (not compose), so Compose service-name
# DNS aliases (e.g. "db", "redis") won't resolve. Rewrite env vars to use
# the full container names that Docker DNS always publishes.
_CPN=$(grep '^COMPOSE_PROJECT_NAME=' /tmp/ilha_canary.env | head -1 | cut -d= -f2-)
if [ -n "${_CPN:-}" ]; then
  sed -i \
    -e "s|@db:|@${_CPN}-db:|g" \
    -e "s|@redis:|@${_CPN}-redis:|g" \
    -e "s|^DB_HOST=db$|DB_HOST=${_CPN}-db|" \
    -e "s|^REDIS_HOST=redis$|REDIS_HOST=${_CPN}-redis|" \
    /tmp/ilha_canary.env
fi

docker rm -f web_zd_canary 2>/dev/null || true
docker run -d \
  --name web_zd_canary \
  --env-file /tmp/ilha_canary.env \
  -e SKIP_MIGRATIONS=true \
  -e STARTUP_PERFORMANCE_MODE=true \
  --network ilha_caddy_proxy \
  "$IMAGE"

# Connect canary to the Compose app network so it can reach dependency services.
DB_CID=$(docker compose --env-file .env ps -q "$DB_SERVICE" 2>/dev/null | head -1)
APP_NETWORK=""
if [ -n "${DB_CID:-}" ]; then
  APP_NETWORK=$(docker inspect "$DB_CID" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null \
    | tr ' ' '\n' | grep -v ilha_caddy | head -1)
fi
if [ -z "${APP_NETWORK:-}" ]; then
  echo "ERROR: Could not determine Compose app network from $DB_SERVICE container. Configure deploy.db_service in .ilha/config.yml."
  exit 1
fi
if ! docker network connect "$APP_NETWORK" web_zd_canary; then
  echo "ERROR: docker network connect $APP_NETWORK web_zd_canary failed"
  exit 1
fi

# ── Phase 3: Health check canary ──
echo "Phase 3: Health checking canary on :${WEB_PORT}${HEALTH_PATH} ..."
CANARY_HEALTHY=0
for i in $(seq 1 30); do
  if docker exec web_zd_canary curl -fsS --max-time 5 "http://127.0.0.1:${WEB_PORT}${HEALTH_PATH}" >/dev/null 2>&1; then
    CANARY_HEALTHY=1
    echo "Canary health check passed (attempt $i)"
    break
  fi
  sleep 2
done

if [ "$CANARY_HEALTHY" != "1" ]; then
  echo "ROLLBACK: canary failed health check, old container still serving"
  docker logs --tail 30 web_zd_canary 2>&1 || true
  exit 1
fi

# ── Phase 4: Pause caddy monitor ──
echo "Phase 4: Pausing caddy monitor..."
docker stop caddy_monitor 2>/dev/null || true

# ── Phase 5: Swap Caddy upstream to canary ──
echo "Phase 5: Swapping traffic to canary..."
WEB_NAME=$(docker compose --env-file .env ps --format json "$WEB_SERVICE" 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['Name'] if isinstance(d,list) else d['Name'])" 2>/dev/null \
  || echo "")

if [ -n "$WEB_NAME" ]; then
  caddy_swap "${WEB_NAME}:${WEB_PORT}" "web_zd_canary:${WEB_PORT}" \
    && echo "Caddy upstream swapped to canary" \
    || echo "WARNING: Caddy swap failed, continuing..."
fi

# ── Phase 6: Replace old web with new compose-managed container ──
echo "Phase 6: Recreating compose $WEB_SERVICE service..."
docker compose --env-file .env up -d --no-deps --force-recreate "$WEB_SERVICE" 2>&1

# Connect new web to Caddy network
for i in 1 2 3; do
  WEB_CID=$(docker compose --env-file .env ps -q "$WEB_SERVICE" 2>/dev/null | head -1)
  if [ -n "$WEB_CID" ]; then
    docker network connect ilha_caddy_proxy "$WEB_CID" 2>/dev/null || true
    break
  fi
  [ "$i" -lt 3 ] && sleep 5
done

# ── Phase 7: Health check new compose web ──
echo "Phase 7: Health checking new $WEB_SERVICE on :${WEB_PORT}${HEALTH_PATH} ..."
NEW_HEALTHY=0
if [ -n "${WEB_CID:-}" ]; then
  for i in $(seq 1 30); do
    if docker exec "$WEB_CID" curl -fsS --max-time 5 "http://127.0.0.1:${WEB_PORT}${HEALTH_PATH}" >/dev/null 2>&1; then
      NEW_HEALTHY=1
      echo "New $WEB_SERVICE health check passed (attempt $i)"
      break
    fi
    sleep 2
  done
fi

# ── Phase 8: Swap Caddy upstream to new compose web ──
# Only swap if the new compose-managed web passed health check. If it did not,
# leave Caddy pointed at the canary (which is still serving traffic) and exit
# non-zero so the deploy loudly fails.
echo "Phase 8: Swapping traffic to new web..."

if [ "$NEW_HEALTHY" != "1" ]; then
  echo "ROLLBACK: new compose web failed health check — leaving Caddy upstream on canary"
  if [ -n "${WEB_CID:-}" ]; then
    echo "Recent logs from unhealthy web container:"
    docker logs --tail 40 "$WEB_CID" 2>&1 || true
  fi
  # Keep canary alive (don't let trap remove it) so traffic keeps flowing.
  trap - EXIT
  docker start caddy_monitor 2>/dev/null || true
  rm -f /tmp/ilha_canary.env /tmp/ilha_caddy_swap.py 2>/dev/null || true
  exit 1
fi

NEW_WEB_NAME=$(docker compose --env-file .env ps --format json "$WEB_SERVICE" 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['Name'] if isinstance(d,list) else d['Name'])" 2>/dev/null \
  || echo "")

if [ -n "$NEW_WEB_NAME" ]; then
  caddy_swap "web_zd_canary:${WEB_PORT}" "${NEW_WEB_NAME}:${WEB_PORT}" \
    && echo "Caddy upstream swapped to new $WEB_SERVICE" \
    || echo "WARNING: Caddy swap failed, continuing..."
fi

# ── Phase 9: Cleanup (trap handles canary removal + monitor restart) ──
echo "Phase 9: Cleaning up..."
docker rm -f web_zd_canary 2>/dev/null || true
docker start caddy_monitor 2>/dev/null || true
rm -f /tmp/ilha_canary.env /tmp/ilha_caddy_swap.py 2>/dev/null || true
# Clear trap since we already cleaned up
trap - EXIT

# ── Phase 10: Restart workers ──
if [ -n "$WORKER_SERVICES" ]; then
  echo "Phase 10: Restarting workers: $WORKER_SERVICES"
  # shellcheck disable=SC2086 -- intentional word-split on service list
  docker compose --env-file .env up -d --no-deps --force-recreate $WORKER_SERVICES 2>&1 || true
else
  echo "Phase 10: No worker services configured, skipping."
fi

echo "Application services started"
docker compose --env-file .env ps 2>&1
