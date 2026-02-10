#!/bin/bash
# Slim server preparation script for ilha deploy.
# Installs ONLY Docker + Docker Compose. No Python, git, or ilha needed.
set -euo pipefail

log()         { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }
log_success() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $*" >&2; }
log_warning() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ $*" >&2; }
log_error()   { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✗ $*" >&2; }

log "=== ilha Slim Server Preparation ==="
log "Installing: Docker Engine + Compose v2 + firewall"

# ─── 0. Wait for apt/dpkg lock (unattended-upgrades on first boot) ─────
log "--- Waiting for apt to be ready ---"
apt_wait_max=300
apt_waited=0
while ! (apt-get -qq update 2>/dev/null); do
    if [ "$apt_waited" -ge "$apt_wait_max" ]; then
        log_error "Timed out waiting for apt lock after ${apt_wait_max}s"
        exit 1
    fi
    log "apt/dpkg lock held (e.g. unattended-upgrades), waiting 10s..."
    sleep 10
    apt_waited=$((apt_waited + 10))
done
log_success "apt ready (waited ${apt_waited}s)"

# Wait for dpkg/apt processes to finish so get.docker.com can acquire the lock
dpkg_wait_max=120
dpkg_waited=0
while [ -n "$(fuser /var/lib/dpkg/lock-frontend 2>/dev/null)" ] || pgrep -x apt-get >/dev/null 2>&1 || pgrep -x dpkg >/dev/null 2>&1; do
    if [ "$dpkg_waited" -ge "$dpkg_wait_max" ]; then
        log_error "Timed out waiting for dpkg/apt processes after ${dpkg_wait_max}s"
        exit 1
    fi
    log "dpkg/apt still in use, waiting 5s..."
    sleep 5
    dpkg_waited=$((dpkg_waited + 5))
done
[ "$dpkg_waited" -gt 0 ] && log_success "dpkg idle (waited ${dpkg_waited}s)"

# ─── 1. Firewall ──────────────────────────────────────────────────────
log "--- Configuring firewall ---"
if command -v ufw >/dev/null 2>&1; then
    ufw allow 22/tcp   >/dev/null 2>&1 || true
    ufw allow 80/tcp   >/dev/null 2>&1 || true
    ufw allow 443/tcp  >/dev/null 2>&1 || true
    ufw --force enable >/dev/null 2>&1 || true
    log_success "UFW configured (22, 80, 443 open)"
elif command -v iptables >/dev/null 2>&1; then
    for port in 22 80 443; do
        iptables -A INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || true
    done
    log_success "iptables rules added (22, 80, 443 open)"
else
    log_warning "No firewall tool found, skipping"
fi

# ─── 2. Docker Engine + Compose v2 ───────────────────────────────────
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log_success "Docker and Docker Compose already installed"
    docker --version >&2
    docker compose version >&2
else
    log "Installing Docker Engine and Compose v2..."
    export DEBIAN_FRONTEND=noninteractive
    curl -fsSL https://get.docker.com | sh >&2 2>&1
    systemctl enable docker >/dev/null 2>&1 || true
    systemctl start docker  >/dev/null 2>&1 || true
    log_success "Docker installed"
    docker --version >&2
    docker compose version >&2
fi

# ─── 3. Create ilha Docker network (needed for Caddy) ────────────────
if docker network inspect ilha_caddy_proxy >/dev/null 2>&1; then
    log_success "Docker network 'ilha_caddy_proxy' already exists"
else
    docker network create ilha_caddy_proxy >/dev/null 2>&1
    log_success "Docker network 'ilha_caddy_proxy' created"
fi

# ─── 4. Create Caddy volumes (needed for global Caddy compose) ───────
for vol in ilha_caddy_data ilha_caddy_config; do
    if docker volume inspect "$vol" >/dev/null 2>&1; then
        log_success "Docker volume '$vol' already exists"
    else
        docker volume create "$vol" >/dev/null 2>&1
        log_success "Docker volume '$vol' created"
    fi
done

log_success "=== Slim server preparation complete ==="
