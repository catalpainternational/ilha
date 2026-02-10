#!/bin/bash
# Server preparation script for ilha
# This script installs required dependencies on remote servers before ilha can be used
# Must remain as Bash because it runs before Python is installed

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "[PREP] Detecting distribution..."
if [ -f /etc/os-release ]; then . /etc/os-release; else ID=unknown; fi

# Choose package manager commands
if command -v apt-get >/dev/null 2>&1; then
  PKG_UPDATE='apt-get -y -qq update'
  PKG_INSTALL='apt-get -y -qq install'
  USE_APT=true
elif command -v dnf >/dev/null 2>&1; then
  PKG_UPDATE='dnf -y makecache'
  PKG_INSTALL='dnf install -y -q'
  USE_APT=false
elif command -v yum >/dev/null 2>&1; then
  PKG_UPDATE='yum -y makecache'
  PKG_INSTALL='yum install -y -q'
  USE_APT=false
else
  echo "[PREP] Unsupported distro: cannot find apt/dnf/yum" >&2
  exit 1
fi

# Function to wait for apt lock release (max 60s)
wait_for_apt_lock() {
  if [ "$USE_APT" != "true" ]; then
    return 0
  fi
  
  local max_wait=60
  local waited=0
  local check_interval=2
  
  # Check if unattended-upgrades is running
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet unattended-upgrades 2>/dev/null; then
      echo "[PREP] unattended-upgrades is running, waiting up to ${max_wait}s for it to complete..." >&2
      while [ $waited -lt $max_wait ]; do
        if ! systemctl is-active --quiet unattended-upgrades 2>/dev/null; then
          echo "[PREP] unattended-upgrades completed" >&2
          break
        fi
        sleep $check_interval
        waited=$((waited + check_interval))
      done
    fi
  fi
  
  # Wait for apt locks to be released
  while [ $waited -lt $max_wait ]; do
    if lsof /var/lib/apt/lists/lock >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || [ -f /var/lib/apt/lists/lock ]; then
      sleep $check_interval
      waited=$((waited + check_interval))
    else
      break
    fi
  done
  
  if [ $waited -ge $max_wait ]; then
    echo "[PREP] Warning: Apt lock still present after ${max_wait}s, proceeding anyway..." >&2
  fi
}

# Retry function for apt operations with improved lock detection
apt_retry() {
  local cmd="$1"
  local max_attempts=5
  local attempt=1
  local wait_times=(2 5 10 20 30)  # Reduced wait times
  
  while [ $attempt -le $max_attempts ]; do
    # Try to run the command first
    if sh -lc "$cmd" 2>/dev/null; then
      return 0
    fi
    
    # Only check for locks on actual failures
    if [ $attempt -lt $max_attempts ]; then
      if [ "$USE_APT" = "true" ]; then
        # Check if failure was due to lock
        if lsof /var/lib/apt/lists/lock >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || [ -f /var/lib/apt/lists/lock ]; then
          local wait_time=${wait_times[$((attempt-1))]}
          echo "[PREP] Apt lock detected after command failure, waiting ${wait_time}s before retry (attempt ${attempt}/${max_attempts})..." >&2
          sleep $wait_time
        else
          # No lock, but command failed - wait shorter time
          local wait_time=${wait_times[$((attempt-1))]}
          echo "[PREP] Command failed, waiting ${wait_time}s before retry (attempt ${attempt}/${max_attempts})..." >&2
          sleep $wait_time
        fi
      else
        # Non-apt system, just wait
        local wait_time=${wait_times[$((attempt-1))]}
        echo "[PREP] Command failed, waiting ${wait_time}s before retry (attempt ${attempt}/${max_attempts})..." >&2
        sleep $wait_time
      fi
      attempt=$((attempt+1))
    else
      echo "[PREP] Command failed after ${max_attempts} attempts" >&2
      return 1
    fi
  done
}

# Wait for droplet initialization intelligently
if [ "$USE_APT" = "true" ]; then
  echo "[PREP] Checking droplet initialization status..."
  # Wait for apt locks and unattended-upgrades if needed
  wait_for_apt_lock
  echo "[PREP] Droplet initialization check complete"
fi

echo "[PREP] Installing base tools (curl git)..."
apt_retry "$PKG_UPDATE" || true
apt_retry "$PKG_INSTALL curl git" || true

# Configure firewall to allow HTTP, HTTPS, and SSH
echo "[PREP] Configuring firewall..."
if command -v ufw >/dev/null 2>&1; then
  # UFW is available
  if ufw status | grep -q "Status: active"; then
    echo "[PREP] UFW is active, ensuring ports are open..."
    ufw allow 22/tcp comment 'SSH' || true
    ufw allow 80/tcp comment 'HTTP' || true
    ufw allow 443/tcp comment 'HTTPS' || true
  else
    echo "[PREP] UFW is inactive, enabling and configuring..."
    ufw --force enable || true
    ufw default allow outgoing || true
    ufw default deny incoming || true
    ufw allow 22/tcp comment 'SSH' || true
    ufw allow 80/tcp comment 'HTTP' || true
    ufw allow 443/tcp comment 'HTTPS' || true
  fi
  echo "[PREP] UFW status:"
  ufw status verbose || true
elif command -v iptables >/dev/null 2>&1; then
  # Fallback to iptables if ufw not available
  echo "[PREP] Configuring iptables for HTTP/HTTPS/SSH..."
  # Allow SSH (port 22)
  iptables -C INPUT -p tcp --dport 22 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 22 -j ACCEPT || true
  # Allow HTTP (port 80)
  iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 80 -j ACCEPT || true
  # Allow HTTPS (port 443)
  iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 443 -j ACCEPT || true
  # Save iptables rules if iptables-persistent is available
  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save || true
  elif command -v iptables-save >/dev/null 2>&1 && [ -w /etc/iptables/rules.v4 ]; then
    iptables-save > /etc/iptables/rules.v4 || true
  fi
  echo "[PREP] iptables rules configured"
else
  echo "[PREP] Warning: No firewall tool (ufw/iptables) found. Ports may need manual configuration." >&2
fi

# Install Python 3.11+ (required for ilha)
echo "[PREP] Checking Python version..."
PYTHON_CMD=""
if command -v python3.11 >/dev/null 2>&1; then
  PYTHON_CMD="python3.11"
  echo "[PREP] Python 3.11 already installed"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON_CMD="python3.12"
  echo "[PREP] Python 3.12 already installed"
elif command -v python3.13 >/dev/null 2>&1; then
  PYTHON_CMD="python3.13"
  echo "[PREP] Python 3.13 already installed"
else
  # Try installing Python with uv (fastest method)
  echo "[PREP] Attempting to install Python 3.11 using uv..."
  UV_INSTALLED=false
  UV_CMD=""
  
  # Install uv if not available
  if ! command -v uv >/dev/null 2>&1; then
    echo "[PREP] Installing uv..."
    UV_INSTALL_OUTPUT=$(curl -LsSf https://astral.sh/uv/install.sh 2>&1 | sh 2>&1)
    UV_INSTALL_EXIT=$?
    
    # Update PATH - uv installer may add to different locations
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    
    # Check common uv installation locations
    if [ -x "$HOME/.cargo/bin/uv" ]; then
      UV_CMD="$HOME/.cargo/bin/uv"
      UV_INSTALLED=true
      echo "[PREP] uv installed successfully (found in ~/.cargo/bin)"
    elif [ -x "$HOME/.local/bin/uv" ]; then
      UV_CMD="$HOME/.local/bin/uv"
      UV_INSTALLED=true
      echo "[PREP] uv installed successfully (found in ~/.local/bin)"
    elif command -v uv >/dev/null 2>&1; then
      UV_CMD="uv"
      UV_INSTALLED=true
      echo "[PREP] uv installed successfully (found in PATH)"
    else
      echo "[PREP] uv installation may have failed (exit code: $UV_INSTALL_EXIT)"
      if [ -n "$UV_INSTALL_OUTPUT" ]; then
        echo "[PREP] uv installer output: $UV_INSTALL_OUTPUT" >&2
      fi
    fi
  else
    UV_CMD="uv"
    UV_INSTALLED=true
    echo "[PREP] uv already available"
  fi
  
  # Try to install Python with uv
  if [ "$UV_INSTALLED" = "true" ] && [ -n "$UV_CMD" ]; then
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    echo "[PREP] Installing Python 3.11 using uv..."
    UV_PYTHON_OUTPUT=$("$UV_CMD" python install 3.11 2>&1)
    UV_PYTHON_EXIT=$?
    
    if [ $UV_PYTHON_EXIT -eq 0 ]; then
      # Find the installed Python - uv stores it in ~/.local/bin or ~/.uv/python
      if [ -x "$HOME/.local/bin/python3.11" ]; then
        PYTHON_CMD="$HOME/.local/bin/python3.11"
        echo "[PREP] Python 3.11 installed successfully using uv (found in ~/.local/bin)"
      else
        # Try uv python find
        UV_PYTHON=$("$UV_CMD" python find 3.11 2>/dev/null | head -1)
        if [ -n "$UV_PYTHON" ] && [ -x "$UV_PYTHON" ]; then
          PYTHON_CMD="$UV_PYTHON"
          echo "[PREP] Python 3.11 installed successfully using uv (found via 'uv python find')"
        else
          # Search in common uv locations
          if [ -d "$HOME/.uv/python" ]; then
            UV_PYTHON_PATH=$(find "$HOME/.uv/python" -name "python3" -type f -executable 2>/dev/null | grep -E "3\.11" | head -1)
            if [ -n "$UV_PYTHON_PATH" ] && [ -x "$UV_PYTHON_PATH" ]; then
              PYTHON_CMD="$UV_PYTHON_PATH"
              echo "[PREP] Python 3.11 installed successfully using uv (found in ~/.uv/python)"
            fi
          fi
        fi
      fi
      
      # If we still don't have Python, show error
      if [ -z "$PYTHON_CMD" ]; then
        echo "[PREP] Python 3.11 installed via uv but could not locate binary" >&2
        if [ -n "$UV_PYTHON_OUTPUT" ]; then
          echo "[PREP] uv python install output: $UV_PYTHON_OUTPUT" >&2
        fi
      fi
    else
      echo "[PREP] uv python install failed (exit code: $UV_PYTHON_EXIT)" >&2
      if [ -n "$UV_PYTHON_OUTPUT" ]; then
        echo "[PREP] uv python install output: $UV_PYTHON_OUTPUT" >&2
      fi
    fi
  fi
  
  # Fallback to deadsnakes PPA if uv failed
  if [ -z "$PYTHON_CMD" ]; then
    echo "[PREP] uv installation failed or unavailable, falling back to deadsnakes PPA..."
    if command -v apt-get >/dev/null 2>&1; then
      apt_retry "$PKG_INSTALL software-properties-common" || true
      add-apt-repository -y ppa:deadsnakes/ppa || true
      apt_retry "$PKG_UPDATE" || true
      apt_retry "$PKG_INSTALL python3.11 python3.11-venv python3.11-dev" || true
      PYTHON_CMD="python3.11"
    else
      echo "[PREP] Warning: Cannot install Python 3.11+ on non-Debian/Ubuntu system" >&2
      echo "[PREP] Attempting to use system Python 3.11+ if available..." >&2
      # Try to find any Python 3.11+ in PATH
      for py in python3.11 python3.12 python3.13; do
        if command -v "$py" >/dev/null 2>&1; then
          PYTHON_CMD="$py"
          break
        fi
      done
      if [ -z "$PYTHON_CMD" ]; then
        echo "[PREP] Error: Python 3.11+ not found and cannot be installed automatically" >&2
        exit 1
      fi
    fi
  fi
fi

if [ -z "$PYTHON_CMD" ]; then
  echo "[PREP] Error: Could not determine Python 3.11+ command" >&2
  exit 1
fi

echo "[PREP] Using Python: $PYTHON_CMD"
$PYTHON_CMD --version || true

echo "[PREP] Installing Docker (Engine + Compose v2) via get.docker.com..."
curl -fsSL https://get.docker.com | sh

# Enable/start docker if systemd present
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable docker || true
  systemctl start docker || true
else
  service docker start || true
fi

echo "[PREP] Setting up ilha in dedicated venv..."
VENV_DIR=/opt/ilha-venv
mkdir -p "$VENV_DIR"

# Check if uv is available (preferred method for venv creation)
# Note: When Python is installed via uv, it's marked as "externally-managed" (PEP 668)
# which prevents python -m venv from using ensurepip. Using uv venv handles this correctly.
if command -v uv >/dev/null 2>&1; then
  # Use uv venv which handles pip installation properly, especially for uv-installed Python
  echo "[PREP] Using uv venv (uv is available)..."
  uv venv "$VENV_DIR" --python "$PYTHON_CMD" || {
    echo "[PREP] uv venv failed, falling back to standard venv..." >&2
    # Fallback to standard venv
    $PYTHON_CMD -m venv "$VENV_DIR" || {
      echo "[PREP] Standard venv failed, trying --without-pip..." >&2
      # If venv fails (e.g., externally-managed), try without pip
      $PYTHON_CMD -m venv --without-pip "$VENV_DIR" || {
        echo "[PREP] venv creation failed" >&2
        exit 1
      }
      # Install pip using get-pip.py
      curl -sSL https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python" || {
        echo "[PREP] pip installation failed" >&2
        exit 1
      }
    }
  }
else
  # Try standard venv first
  $PYTHON_CMD -m venv "$VENV_DIR" || {
    echo "[PREP] Standard venv failed, trying --without-pip..." >&2
    # If venv fails (e.g., externally-managed), try without pip
    $PYTHON_CMD -m venv --without-pip "$VENV_DIR" || {
      echo "[PREP] venv creation failed" >&2
      exit 1
    }
    # Install pip using get-pip.py
    curl -sSL https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python" || {
      echo "[PREP] pip installation failed" >&2
      exit 1
    }
  }
fi

# Ensure pip is available
if [ ! -f "$VENV_DIR/bin/pip" ]; then
  echo "[PREP] pip not found in venv, installing..." >&2
  curl -sSL https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python" || {
    echo "[PREP] pip installation failed" >&2
    exit 1
  }
fi

# Determine ilha version to install (can be set via ILHA_VERSION env var)
# Format: commit hash, tag, or branch name (defaults to master)
ILHA_VERSION="${ILHA_VERSION:-master}"
ILHA_GIT_URL="git+https://github.com/catalpainternational/ilha.git@${ILHA_VERSION}"

echo "[PREP] Installing ilha version: ${ILHA_VERSION}"

"$VENV_DIR/bin/pip" install --upgrade pip wheel
"$VENV_DIR/bin/pip" install --upgrade "$ILHA_GIT_URL" || {
  echo "[PREP] venv install failed, trying pipx/system pip as fallback" >&2
  if command -v pipx >/dev/null 2>&1; then
    pipx install --force "$ILHA_GIT_URL" || true
  else
    $PYTHON_CMD -m pip install --break-system-packages --upgrade "$ILHA_GIT_URL" || true
  fi
}
ln -sf "$VENV_DIR/bin/ilha" /usr/local/bin/ilha || true

echo "[PREP] Versions:"
docker --version || true
docker compose version || true
git --version || true
ilha --version || true

