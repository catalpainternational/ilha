#!/bin/bash
# cloud-init user data for ephemeral ilha worker droplets.
#
# Intentionally secret-free: DigitalOcean stores user_data in metadata in
# plaintext. Compose YAML (image refs, service names) is not a secret.
# App env and the registry token are delivered over SSH after the droplet
# is up — see WorkerManager.bootstrap_worker.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# A transient mirror/network failure here would otherwise strand a droplet
# that is tagged (and billed, and counted as fleet capacity) but runs nothing.
apt_retry() {
  local attempt
  for attempt in 1 2 3; do
    if "$@"; then
      return 0
    fi
    echo "apt step failed (attempt $attempt/3): $*" >&2
    sleep 10
  done
  return 1
}

apt_retry apt-get update
apt_retry apt-get install -y ca-certificates curl gnupg lsb-release

if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt_retry apt-get update
  apt_retry apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

systemctl enable --now docker

mkdir -p /app
chmod 700 /app
umask 077
echo '{{COMPOSE_B64}}' | base64 -d > /app/docker-compose.yml
chmod 600 /app/docker-compose.yml
touch /app/.ilha-docker-ready
