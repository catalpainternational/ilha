#!/bin/bash
# cloud-init user data for ephemeral ilha worker droplets.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates curl gnupg lsb-release

if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

systemctl enable --now docker

mkdir -p /app
echo '{{COMPOSE_B64}}' | base64 -d > /app/docker-compose.yml
echo '{{ENV_B64}}' | base64 -d > /app/env.ilha

REGISTRY_TOKEN_B64='__REGISTRY_TOKEN_B64__'
if [ -n "$REGISTRY_TOKEN_B64" ]; then
  REGISTRY_TOKEN="$(echo "$REGISTRY_TOKEN_B64" | base64 -d)"
  echo "$REGISTRY_TOKEN" | docker login registry.digitalocean.com -u "$REGISTRY_TOKEN" --password-stdin
fi

docker pull '{{IMAGE_REF}}'
cd /app
docker compose --env-file env.ilha up -d '{{WORKER_SERVICE}}'
