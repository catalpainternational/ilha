# Archived Bash Deployment Scripts

This directory contains the original Bash deployment scripts that have been migrated to pure Python for improved maintainability, type safety, and DRY principles.

## Migration Summary

| Original | Replacement | Status |
|----------|-------------|--------|
| `zd_deploy.sh` | `../remote_deploy.py` | ✅ Migrated |
| `server_prep.sh` | `../server_prep.py` | ✅ Migrated |
| N/A | `../deploy_utils.py` | ✅ New (shared utilities) |

## Why the Migration?

- **DRY Principles**: Reusable classes in `deploy_utils.py` eliminate duplication
- **Type Safety**: Python type hints and Pydantic validation
- **Testability**: Classes and functions are easier to unit test
- **Maintainability**: Single language throughout the deployment pipeline
- **Error Handling**: Structured exception handling and logging

## Rollback

If needed to revert to bash scripts:

```bash
# Extract the archive
tar -xzf bash_scripts_archived_20260426.tar.gz

# Move scripts back to parent directory
mv *.sh ../

# Update SlimDeployManager to use old bash methods
```

## Python Replacements

### `remote_deploy.py`
- Standalone 10-phase zero-downtime deployment script
- Reads configuration from environment variables (APP_ROOT, WEB_SERVICE, etc.)
- Imports reusable classes from `deploy_utils.py`
- Equivalent functionality to original `zd_deploy.sh`

### `server_prep.py`
- Fresh Ubuntu server preparation (Docker, firewall, swap)
- No dependencies; works on bare servers
- Reads no configuration; safe to run multiple times (idempotent)
- Equivalent functionality to original `server_prep.sh`

### `deploy_utils.py`
- Shared DRY utilities for both deployment scripts
- Classes: `DeployLogger`, `DockerCommand`, `CaddyManager`, `RemoteSystemCommand`
- Used by both `remote_deploy.py` and `server_prep.py`

## Integration Points

SlimDeployManager now uses:
- `_prepare_server_slim()` → executes `server_prep.py` with Python
- `_start_app_remote()` → executes `remote_deploy.py` with Python

Both scripts are uploaded to the remote server with `deploy_utils.py` and executed with `PYTHONPATH` set for import resolution.

## Archive Contents

```
bash_scripts_archived_20260426.tar.gz
├── zd_deploy.sh (11 KB) - 10-phase zero-downtime deployment
└── server_prep.sh (4.9 KB) - Server preparation
```

**Archived**: 2026-04-26  
**Reason**: Migrated to Python for DRY, type-safe deployment pipeline
