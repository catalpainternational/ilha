# Bash to Python Migration: Zero-Downtime Deploy & Server Prep

## Overview

This document describes the migration from Bash scripts to pure Python, eliminating Bash script dependencies while maintaining feature parity and improving code quality with DRY principles and type safety.

## Migration Summary

### Before: Bash Scripts
- `ilha/scripts/zd_deploy.sh` (280 lines) — Remote zero-downtime deployment
- `ilha/scripts/server_prep.sh` (120 lines) — Server preparation
- **Issues**: Non-reusable code, shell-specific behaviors, poor error handling, hard to test, difficult to integrate with Python orchestration

### After: Pure Python
- `ilha/scripts/deploy_utils.py` (380 lines) — Reusable utilities (DRY)
- `ilha/scripts/remote_deploy.py` (400+ lines) — Zero-downtime deployment
- `ilha/scripts/server_prep.py` (350+ lines) — Server preparation
- **Benefits**: Reusable classes, structured subprocess handling, integrated logging, type hints, testable, integrates with Python

---

## Architecture

### deploy_utils.py - Reusable Utilities

Provides **DRY, reusable classes** for subprocess operations:

#### DeployLogger
Simple logging with timestamps (info, success, warning, error).

#### DockerCommand
Wrapper for docker/docker-compose commands:
- `run()` — Execute docker command with error handling
- `compose()` — Docker compose wrapper with env file support
- `ps_json()` — Get docker compose ps as JSON
- `exec()` — Execute command inside container
- `wait_for_postgres()` — Health check with retry loop
- `health_check_http()` — HTTP health check via curl
- `network_connect()` — Connect container to network
- `container_remove/start/stop()` — Container lifecycle

#### CaddyManager
Manages Caddy reverse proxy configuration:
- `get_config()` — Fetch current config via admin API
- `swap_upstream()` — Swap reverse proxy targets with config traversal

#### RemoteSystemCommand
System-level commands (apt, firewall, etc.):
- `run()` — Execute system command
- `apt_update()` — Wait for apt lock and update
- `dpkg_wait()` — Wait for dpkg/apt processes
- `command_exists()` — Check if command in PATH

---

## remote_deploy.py - Zero-Downtime Deployment

### 10-Phase Strategy

```
Phase 1:   Build image (old container still serving)
Phase 1b:  Bring up dependencies, wait for DB readiness
Phase 2:   Start canary container from new image
Phase 3:   Health-check canary
Phase 4:   Pause Caddy monitor
Phase 5:   Swap Caddy upstream to canary
Phase 6:   Recreate compose-managed web service
Phase 7:   Health-check recreated service
Phase 8:   Swap Caddy upstream to new service
Phase 9:   Cleanup (remove canary, restart monitor)
Phase 10:  Recreate worker services (if configured)
```

### Class: RemoteDeployer

**Environment Variables (Required)**:
- `APP_ROOT` — Remote app directory (e.g., `/app`)
- `WEB_SERVICE` — Compose service name for web app
- `WEB_PORT` — Port web app listens on inside container
- `HEALTH_PATH` — HTTP path for health checks
- `DB_SERVICE` — Compose service for database
- `DEPS_SERVICES` — Space-separated dependency services
- `WORKER_SERVICES` — Space-separated worker services (optional)
- `BUILD_WEB` — "1" to build image, "" to skip (optional)

**Key Methods**:
- `run()` — Execute full deployment (returns True/False)
- `_get_service_image()` — Determine image name for service
- `_health_check_http()` — Check service health
- `_get_app_network()` — Get compose network
- `_prepare_canary_env()` — Prepare env file for canary (DNS rewriting)
- `_cleanup()` — Cleanup on exit (registered with atexit)

**Error Handling**:
- If canary health check fails → rollback (exit 1, old container keeps serving)
- If new web health check fails → keep canary alive, exit 1 (loud failure)
- Cleanup is guaranteed via `atexit` handler

### Usage

**From SlimDeployManager**:
```python
import subprocess
import os

env_vars = {
    "APP_ROOT": "/app",
    "WEB_SERVICE": "web",
    "WEB_PORT": "8000",
    "HEALTH_PATH": "/health/",
    "DB_SERVICE": "db",
    "DEPS_SERVICES": "db redis",
    "WORKER_SERVICES": "worker-1 worker-2",
    "BUILD_WEB": "1",
}

# Upload remote_deploy.py to remote server
# ssh_upload(local_path, remote_path)

# Execute on remote
result = subprocess.run(
    ["ssh", "user@host", "python3 /tmp/remote_deploy.py"],
    env=env_vars,
)
sys.exit(result.returncode)
```

---

## server_prep.py - Server Preparation

### Steps

```
Step 0:  Wait for apt/dpkg lock (unattended-upgrades on first boot)
Step 1:  Configure firewall (UFW or iptables)
Step 2:  Install Docker Engine and Compose v2
Step 3:  Create Docker network 'ilha_caddy_proxy'
Step 4:  Create Caddy Docker volumes
Step 5:  Setup 512MB swap (for memory-constrained droplets)
```

### Class: ServerPrep

**No environment variables required** (works on fresh Ubuntu).

**Key Methods**:
- `run()` — Execute full preparation (returns True/False)
- `_setup_firewall()` — Configure UFW or iptables
- `_install_docker()` — Install Docker + Compose via get.docker.com
- `_create_docker_network()` — Create ilha_caddy_proxy network
- `_create_caddy_volumes()` — Create Caddy volumes for persistence
- `_setup_swap()` — Create and enable swapfile

**Error Handling**:
- **UFW SSH rule critical**: If SSH rule fails, abort firewall setup to avoid lockout
- If swap setup fails, continue (not blocking)
- If network/volume already exists, skip creation (idempotent)
- Waits up to 300s for apt lock before failing

### Usage

**From SlimDeployManager**:
```python
import subprocess

# Upload server_prep.py to new server
# ssh_upload(local_path, "/tmp/server_prep.py")

# Execute on remote
result = subprocess.run(
    ["ssh", "root@host", "python3 /tmp/server_prep.py"],
)
sys.exit(result.returncode)
```

---

## DRY Principles Applied

### 1. Reusable Utilities (deploy_utils.py)

**Before**: Bash script with inline docker/curl commands scattered throughout
**After**: 
- `DockerCommand` class with methods for common operations
- `CaddyManager` handles Caddy API interactions
- `RemoteSystemCommand` handles system-level operations
- Both scripts use these utilities (no duplication)

**Example - Health Checking**:
```python
# Before (bash): Manual curl + retry loop
for i in $(seq 1 30); do
  if docker exec web_zd_canary curl -fsS ... >/dev/null; then
    echo "OK"
    break
  fi
  sleep 2
done

# After (Python): Reusable method
docker.health_check_http("web_zd_canary", 8000, "/health/", max_attempts=30)
# Used by both remote_deploy.py and other scripts
```

### 2. Unified Logging

All scripts use `DeployLogger` for consistent output format:
```
[2024-04-26 10:30:45] ✓    Docker installed
[2024-04-26 10:30:46] INFO Waiting for Postgres...
[2024-04-26 10:30:48] ✗    Health check failed
```

### 3. Error Handling Consistency

All subprocess calls go through `DockerCommand.run()` or `RemoteSystemCommand.run()`:
- Consistent error logging
- Configurable `check` behavior
- Optional output capture
- No duplicated error handling logic

### 4. Configuration via Environment Variables

Both scripts accept configuration as environment variables (set by Python caller):
- Type-safe in Python before passing to subprocess
- Scripts validate required vars on startup
- No hardcoded values in scripts

---

## Integration with SlimDeployManager

### Current Integration (Bash)

```python
# In SlimDeployManager._run_nohup_deploy():
deploy_script = open(scripts_dir / "zd_deploy.sh").read()
# ssh: "nohup bash -s <<'ILHA_SCRIPT'\n{deploy_script}\nILHA_SCRIPT"
```

### New Integration (Python)

**Option 1: Run directly (simpler)**
```python
from pathlib import Path
from ilha.commands.push.deployment_models import DeploymentReport, PhaseResult, PhaseStatus
import subprocess

env_vars = {
    "APP_ROOT": "/app",
    "WEB_SERVICE": "web",
    "WEB_PORT": "8000",
    "HEALTH_PATH": "/health/",
    "DB_SERVICE": "db",
    "DEPS_SERVICES": "db redis",
    "WORKER_SERVICES": "worker-1 worker-2",
    "BUILD_WEB": "1",
}

# Upload script to remote
remote_script = "/tmp/remote_deploy_" + str(uuid.uuid4()) + ".py"
self.scp_upload(
    local_path=Path(__file__).parent / "remote_deploy.py",
    remote_path=remote_script,
)

# Execute with environment variables
result = subprocess.run(
    ["ssh", f"{user}@{host}", f"cd {app_root} && python3 {remote_script}"],
    env=env_vars,
    capture_output=True,
    text=True,
)

# Parse result and update DeploymentReport
report.add_phase(
    DeploymentPhase.DEPLOY,
    PhaseResult(
        phase=DeploymentPhase.DEPLOY,
        status=PhaseStatus.SUCCESS if result.returncode == 0 else PhaseStatus.FAILED,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.returncode,
    ),
)
```

**Option 2: Structured execution with metrics**
```python
# For more complex orchestration, could parse remote_deploy.py output
# and update DeploymentReport with phase-by-phase metrics

result = subprocess.run(
    [...],
    capture_output=True,
    text=True,
)

# Parse stdout for "[timestamp] ✓ Phase X: ..." patterns
# Update DeploymentReport incrementally as deployment progresses
```

---

## Testing

### Manual Testing

```bash
# Test deploy_utils locally
python3 -c "from deploy_utils import DeployLogger; log = DeployLogger(); log.success('Test')"

# Test remote_deploy.py (with env vars)
cd /tmp/test-app
export APP_ROOT=/tmp/test-app
export WEB_SERVICE=web
export WEB_PORT=8000
export HEALTH_PATH=/health/
export DB_SERVICE=db
export DEPS_SERVICES=db
python3 /path/to/remote_deploy.py

# Test server_prep.py on fresh droplet
python3 /path/to/server_prep.py
```

### Unit Testing (pytest)

```python
import pytest
from deploy_utils import DeployLogger, DockerCommand

def test_docker_command(mocker):
    docker = DockerCommand()
    mocker.patch("subprocess.run", return_value=...)
    result = docker.compose("ps", ...)
    assert result == ...
```

---

## Migration Checklist

- [x] Create `deploy_utils.py` with reusable classes
- [x] Create `remote_deploy.py` as Python equivalent of `zd_deploy.sh`
- [x] Create `server_prep.py` as Python equivalent of `server_prep.sh`
- [ ] Add unit tests for deploy_utils classes
- [ ] Update SlimDeployManager to use remote_deploy.py
- [ ] Update server prep calls to use server_prep.py
- [ ] Test on real deployment (staging droplet)
- [ ] Deprecate and remove bash scripts after verification

---

## Benefits Summary

### Type Safety
- Pydantic models for configuration (if using new config loader)
- Type hints throughout
- Subprocess errors caught at Python level

### Maintainability
- DRY utilities eliminate duplication
- Reusable classes for common operations
- Consistent error handling
- Clear logging

### Testability
- Classes can be mocked/tested independently
- Environment-variable-driven (easy to test with different configs)
- Structured output (easy to parse for assertions)

### Integration
- Natural integration with Python orchestration code
- Can parse output and update DeploymentReport
- Access to deployment_models.py for structured reporting
- No subprocess-based orchestration needed

---

## Backward Compatibility

Old bash scripts (`zd_deploy.sh`, `server_prep.sh`) remain unchanged during migration. New code can use Python scripts while old code continues to work.

To fully migrate:
1. Verify Python scripts work identically on staging
2. Update SlimDeployManager to use Python scripts
3. Archive bash scripts (don't delete, in case rollback needed)
4. Update documentation
