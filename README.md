# ilha - Git Worktrees with Isolated Docker Environments

[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-passing-green.svg)](tests/)
[![Status](https://img.shields.io/badge/status-production%20ready-brightgreen.svg)]()

A comprehensive Python CLI tool that creates isolated development environments using Git worktrees, Docker Compose, and Caddy reverse proxy. Each feature branch gets its own complete environment with isolated databases, Redis, and media storage.

## 🚀 Quick Start

### Installation

```bash
# Install via pip
pip install ilha

# Or install from source
git clone https://github.com/catalpainternational/ilha.git
cd ilha
pip install -e .
```

### Basic Usage

```bash
# Initialize ilha in your project
ilha setup

# Start global Caddy proxy
ilha start-proxy

# Create and start a worktree
ilha create feature-auth
ilha feature-auth up -d

# Access your isolated environment (note: domain includes project name)
# If your project is named "myapp", the URL will be:
open http://myapp-feature-auth.localhost

# Export environment as shareable package (includes code by default)
ilha packages export feature-auth

# Import environment from package
ilha packages import myapp-feature-auth-2024-01-15.ilha-package.tar.gz

# Stop and clean up
ilha feature-auth down
ilha remove feature-auth
```

## 🏗️ Architecture Overview

ilha CLI provides complete environment isolation through:

- **Git Worktrees**: Isolated working directories for each branch
- **Docker Compose**: Isolated containers with branch-specific volumes
- **Caddy Proxy**: Dynamic routing based on branch names
- **Volume Isolation**: Branch-specific databases, Redis, and media storage
- **Package Management**: Export/import complete environments as shareable packages

### System Architecture

```text
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Global Caddy  │    │  Worktree A      │    │  Worktree B     │
│   (Port 80)     │    │  feature-auth    │    │  feature-pay    │
│                 │    │                  │    │                 │
│  Routes to:     │───▶│  • PostgreSQL    │    │  • PostgreSQL   │
│  • *.localhost  │    │  • Redis         │    │  • Redis        │
│                 │    │  • Web App       │    │  • Web App      │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

## 📋 Command Reference

**Important**: ilha uses a **worktree-name-first pattern**. For `up` and `down` commands, you can use:
- `ilha <worktree_name> up` and `ilha <worktree_name> down` (explicit worktree name)
- `ilha up` and `ilha down` (auto-detected from current directory or current git branch)

The shorthand `ilha up` and `ilha down` automatically resolve the worktree from your current directory (if you're in a worktree) or from your current git branch (if you're at the project root).

### Setup Commands

| Command | Description | Example |
|---------|-------------|---------|
| `setup` | Initialize ilha for this project | `ilha setup [--monkey-patch]` |

### Global Caddy Management

| Command | Description | Example |
|---------|-------------|---------|
| `start-proxy` | Start global Caddy proxy container | `ilha start-proxy` |
| `stop-proxy` | Stop global Caddy proxy container | `ilha stop-proxy` |
| `start` | Alias for start-proxy | `ilha start` |
| `stop` | Alias for stop-proxy | `ilha stop` |

### Command Aliases

| Alias | Full Command | Description |
|-------|--------------|-------------|
| `-D` | `delete` | Delete worktree and branch completely |
| `-r` | `remove` | Remove worktree but keep git branch |

### Worktree Lifecycle

| Command | Description | Example |
|---------|-------------|---------|
| `create <branch>` | Create worktree | `ilha create feature-auth` |
| `<branch> up -d` | Start worktree environment | `ilha feature-auth up -d` |
| `up` | Start worktree environment (auto-detected) | `ilha up` (from worktree directory or project root) |
| `<branch> down` | Stop worktree environment | `ilha feature-auth down` |
| `down` | Stop worktree environment (auto-detected) | `ilha down` (from worktree directory or project root) |
| `remove <branch>` | Remove worktree (keep branch) | `ilha remove feature-auth` |
| `delete <branch>` | Delete worktree and branch | `ilha delete feature-auth` |

**Note**: The `ilha up` and `ilha down` commands automatically detect the worktree from:
- Your current directory (if you're inside a worktree directory)
- Your current git branch (if you're at the project root and a worktree exists for that branch)

**Important**: Branch names must match exactly. The `delete` and `remove` commands check worktrees, branches, and docker volumes for exact matches only. If no exact match is found, an error message will show what was checked.

### Docker Compose Commands

ilha supports direct passthrough to Docker Compose commands using the pattern: `ilha <worktree_name> <compose-command>`

| Command Pattern | Description | Example |
|-----------------|-------------|---------|
| `<branch> exec <service> <cmd>` | Execute command in container | `ilha feature-auth exec web python manage.py migrate` |
| `<branch> logs <service>` | View container logs | `ilha feature-auth logs web` |
| `<branch> ps` | List containers | `ilha feature-auth ps` |
| `<branch> run <service> <cmd>` | Run one-off command | `ilha feature-auth run --rm web python manage.py test` |
| `<branch> build` | Build services | `ilha feature-auth build` |
| `<branch> restart <service>` | Restart service | `ilha feature-auth restart web` |
| `<branch> <compose-cmd>` | Any docker compose command | `ilha feature-auth pull`, `ilha feature-auth config` |

**Supported Docker Compose Commands:**
- `exec`, `logs`, `ps`, `run`, `build`, `pull`, `push`, `restart`
- `start`, `stop`, `up`, `down`, `config`, `images`, `port`
- `top`, `events`, `kill`, `pause`, `unpause`, `scale`

### Wildcard Operations

| Command | Description | Example |
|---------|-------------|---------|
| `remove <pattern>` | Remove worktrees matching pattern | `ilha remove test-*` |
| `delete <pattern>` | Delete worktrees and branches matching pattern | `ilha delete feature-*` |

**Wildcard Patterns:**
- `*` - Matches any characters (e.g., `test-*` matches `test-feature`, `test-bugfix`)
- `?` - Matches single character (e.g., `test-?` matches `test-1`, `test-a`)
- `[abc]` - Matches any character in brackets (e.g., `test-[abc]` matches `test-a`, `test-b`, `test-c`)
- Case-insensitive matching (e.g., `test-*` matches `Test-Feature`, `TEST-FEATURE`)

### Bulk Operations
| Command | Description | Example |
|---------|-------------|---------|
| `remove-all` | Remove all worktrees (keep branches) | `ilha remove-all` |
| `delete-all` | Delete all worktrees and branches | `ilha delete-all --force` |

### Utility Commands
| Command | Description | Example |
|---------|-------------|---------|
| `list` | List active worktrees | `ilha list` |
| `prune` | Remove prunable worktrees | `ilha prune` |
| `help` | Show help information | `ilha help` |
| `clean-legacy` | Clean legacy ilha elements | `ilha clean-legacy` |

### Volume Management
| Command | Description | Example |
|---------|-------------|---------|
| `volumes list` | List all worktree volumes | `ilha volumes list` |
| `volumes size` | Show volume sizes | `ilha volumes size` |
| `volumes backup <branch>` | Backup worktree volumes | `ilha volumes backup feature-auth` |
| `volumes restore <branch> <file>` | Restore from backup | `ilha volumes restore feature-auth backup.tar` |
| `volumes clean <branch>` | Clean up volumes | `ilha volumes clean feature-auth` |

### Droplet Management
| Command | Description | Example |
|---------|-------------|---------|
| `droplet create [branch_name]` | Create a new Digital Ocean droplet and push environment (default). Droplet name auto-detected from domain subdomain or branch name | `ilha droplet create` or `ilha droplet create test` |
| `droplet create [branch_name] --create-only` | Create a droplet only, do not push | `ilha droplet create test --create-only` |
| `droplet create --domain app.example.com` | Create droplet using subdomain from domain as name | `ilha droplet create --domain app.example.com` |
| `droplet list` | List all droplets (formatted table) | `ilha droplet list` |
| `droplet list --as-json` | List droplets as JSON | `ilha droplet list --as-json` |
| `droplet list --as-csv` | List droplets as CSV | `ilha droplet list --as-csv` |
| `droplet regions` | List available Digital Ocean regions (table) | `ilha droplet regions` |
| `droplet regions --show-all` | Include unavailable regions in region list | `ilha droplet regions --show-all` |
| `droplet regions --as-json` | List regions as JSON | `ilha droplet regions --as-json` |
| `droplet regions --as-csv` | List regions as CSV | `ilha droplet regions --as-csv` |
| `droplet info <id>` | Get droplet information | `ilha droplet info 12345678` |
| `droplet destroy <id>` | Destroy a droplet (supports ID or name, comma-separated) | `ilha droplet destroy 12345678` or `ilha droplet destroy my-droplet` or `ilha droplet destroy 123,456,789` |
| `droplet push [<branch>] <scp_target_or_droplet>` | Push ilha package to remote server (supports progressive SCP patterns) | `ilha droplet push feature-auth user@server:/path` or `ilha droplet push feature-auth my-app` |

**Droplet Creation Options:**
- `branch_name` (optional argument) - Branch/worktree name (auto-detected from current directory if not provided)
- `--region <region>` - Droplet region (e.g. nyc1, sfo3). Use `ilha droplet regions` to list available regions (defaults from env or nyc1)
- `--size <size>` - Droplet size (defaults from env or s-1vcpu-1gb)
- `--image <image>` - Droplet image (defaults from env or ubuntu-22-04-x64)
- `--ssh-keys <key>` - SSH key IDs or fingerprints (can be specified multiple times)
- `--tags <tag>` - Tags for the droplet (can be specified multiple times)
- `--wait` - Wait for droplet to be ready
- `--api-token <token>` - Digital Ocean API token (or use DIGITALOCEAN_API_TOKEN env var)
- `--create-only` - Only create droplet, do not push environment (default: creates and pushes)
- `--scp-target <target>` - SCP target (optional, defaults to root@<droplet-ip>:/root)
- `--vpc-uuid <uuid>` - VPC UUID for the droplet (if not provided, uses default VPC for the region)
- `--central-droplet-name <name>` - Name of central droplet to reuse VPC UUID from (for worker deployments)
- Push options: `--no-auto-import` (opt-out), `--prepare-server`, `--domain`, `--ip`, `--dns-token`, `--skip-dns-check`, `--resume`, `--code-only`

**Droplet Name Auto-Detection:**
- If `--domain` is provided: uses subdomain as droplet name (e.g., `app.example.com` → `app`)
- Otherwise: uses branch/worktree name as droplet name (auto-detected if not provided)

**Droplet List Options:**
- `--as-json` or `--json` - Output as JSON format
- `--as-csv` - Output as CSV format
- `--api-token <token>` - Digital Ocean API token (or use DIGITALOCEAN_API_TOKEN env var)

**Droplet Regions Options:**
- `--show-all` - Include unavailable regions
- `--as-json` or `--json` - Output as JSON format
- `--as-csv` - Output as CSV format
- `--api-token <token>` - Digital Ocean API token (or use DIGITALOCEAN_API_TOKEN env var)

**Droplet Destroy Options:**
- `<id>` - Single droplet ID or name, or comma-separated list of IDs/names (e.g., `123,456,789` or `my-droplet,another-droplet,123`)
- `--force` - Skip confirmation (destroys without typing droplet name)
- `--only-droplet` - Only destroy droplet, skip DNS deletion
- `--only-domain` - Only destroy DNS records, skip droplet deletion
- `--domain <domain>` - Domain name for DNS deletion (optional, auto-detects if not provided)
- `--api-token <token>` - Digital Ocean API token (or use DIGITALOCEAN_API_TOKEN env var)
- `--dns-token <token>` - DNS API token (if different from droplet token)
- `--json` - Output as JSON format

**Droplet Destroy Behavior:**
- **Default**: Destroys droplet only (backward compatible)
- **Multiple Droplets**: Accepts comma-separated IDs or names (e.g., `123,456,789` or `my-droplet,another-droplet,123`) and processes all droplets sequentially
- **Name Resolution**: Automatically resolves droplet names to IDs. If multiple droplets share the same name, use the droplet ID instead.
- **Error Handling**: Continues destroying remaining droplets even if one fails
- **Summary Output**: Shows summary of destroyed/failed droplets when processing multiple IDs
- **Confirmation**: Requires typing the exact droplet name to confirm (unless `--force`) - each droplet requires confirmation separately
- **DNS Auto-detection**: When destroying droplet, automatically finds and deletes DNS records pointing to droplet IP
- **DNS-only mode**: Use `--only-domain` to delete DNS records without destroying the droplet
- **Domain confirmation**: When deleting DNS records, requires typing the full domain name (e.g., "app.example.com") unless `--force`
- **Domain override**: Use `--domain <domain>` to limit DNS search to specific domain

### Package Management
| Command | Description | Example |
|---------|-------------|---------|
| `packages export <branch>` | Export worktree as shareable package (includes code by default) | `ilha packages export feature-auth` |
| `packages import <file>` | Import environment from package (auto-detects standalone mode) | `ilha packages import my-package.tar.gz` |
| `packages import <file> --domain <sub.domain.tld>` | Import with domain override (HTTPS via Caddy) | `ilha packages import pkg.tar.gz --domain myapp.example.com` |
| `packages import <file> --ip <x.x.x.x>` | Import with IP override (HTTP-only) | `ilha packages import pkg.tar.gz --ip 203.0.113.10` |
| `packages import <file> --standalone` | Force standalone import (create new project) | `ilha packages import my-package.tar.gz --standalone --target-dir ./myproject` |
| `packages list` | List available packages | `ilha packages list` |
| `packages validate <file>` | Validate package integrity | `ilha packages validate my-package.tar.gz` |

#### Standalone Package Import

ilha intelligently detects whether you're in an existing project and automatically switches between normal and standalone import modes.

**Automatic Detection (Default)**:
```bash
# In an empty directory - automatically creates new project
cd /path/to/new-location
ilha packages import my-package.tar.gz
# Auto-detects: No git repo → standalone mode
# Creates: ./myproject-standalone/

# In existing ilha project - automatically imports as worktree
cd /path/to/existing-project
ilha packages import my-package.tar.gz
# Auto-detects: Existing .ilha → normal mode
# Creates: new worktree in project
```

**Explicit Standalone Mode**:
```bash
# Force standalone import with custom directory
ilha packages import my-package.tar.gz --standalone --target-dir ./my-new-project

# Force standalone even if in existing project
ilha packages import my-package.tar.gz --standalone
```

**Requirements**:
- Standalone imports require packages exported with code (`--include-code`, which is the default)
- Normal imports work with or without code

**What Gets Created in Standalone Mode**:
1. Complete project structure extracted from package
2. Project root `.ilha/` configuration
3. Worktree with its own `.ilha/` (fractal structure)
4. All code files
5. Docker volumes (if `--restore-data`)
6. Ready-to-use isolated environment

**Note**: Standalone import is simple - it extracts the project tar and applies domain/IP overrides. No git initialization or additional setup needed since the package contains the complete fractal structure.

### Shell Completion Management
| Command | Description | Example |
|---------|-------------|---------|
| `completion install [shell]` | Install shell completion | `ilha completion install` |
| `completion uninstall` | Remove shell completion | `ilha completion uninstall` |
| `completion status` | Show completion status | `ilha completion status` |

## 🔧 Configuration
### Django Compatibility Checks

During `ilha setup`, if a Django project is detected (`manage.py` present and a `settings.py` found), ilha validates your settings are environment-driven for reverse proxy use:

- ALLOWED_HOSTS (comma-separated)
- CSRF_TRUSTED_ORIGINS (space-separated)
- USE_X_FORWARDED_HOST (boolean)
- SECURE_PROXY_SSL_HEADER (tuple as `HTTP_X_FORWARDED_PROTO,https`)

If any are missing, ilha prints a nicely formatted guidance block and, when `--monkey-patch` is supplied, appends a safe snippet to `settings.py` to read these values from environment variables.

Suggested snippet (auto-added with `--monkey-patch`):
```python
import os
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = os.getenv("CSRF_TRUSTED_ORIGINS", "").split()
USE_X_FORWARDED_HOST = os.getenv("USE_X_FORWARDED_HOST", "False") == "True"
_hdr = os.getenv("SECURE_PROXY_SSL_HEADER")
if _hdr:
    SECURE_PROXY_SSL_HEADER = tuple(_hdr.split(",", 1))
```

### SQLite Persistence (Automatic)

ilha automatically configures SQLite persistence when you run `ilha setup`:

**Automatic Configuration:**
- ✅ Creates `sqlite_data` Docker volume
- ✅ Mounts volume at `/data` in web containers
- ✅ Copies SQLite databases between worktrees
- ✅ Includes SQLite in export/import packages

**Django Configuration:**
Set your database path to use the volume:

```python
# settings.py
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.getenv('SQLITE_PATH', '/data/db.sqlite3'),  # Use volume path
    }
}
```

**Migration:**
If your database is currently in the source tree (e.g., `db/db.sqlite3`), migrate it:

```bash
# Copy to Docker volume
docker compose cp db/db.sqlite3 web:/data/db.sqlite3

# Or use ilha setup --monkey-patch to auto-configure
ilha setup --monkey-patch
```

**Why This Matters:**
- 📁 Each worktree gets its own isolated database
- 📦 Databases are included in ilha packages
- 🔄 Database state persists across container rebuilds
- ❌ Without volume: all worktrees share the same database file

### Project Setup

When you run `ilha setup`, it creates a `.ilha/` directory with:

- `config.yml` - Project configuration
- `docker-compose.worktree.yml` - Transformed compose file (generated from existing docker-compose.yml)
- `Caddyfile.ilha` - Caddy configuration
- `README.md` - User guide optimized for coding agents
- `worktrees/` - Worktree directories

#### Docker Compose File Usage

**Important**: ilha always uses `worktrees/{branch}/.ilha/docker-compose.worktree.yml` for all runtime operations. ilha never looks for or uses other compose files (such as `docker-compose.yml` in the project root or worktree directory) during runtime operations.

- **Setup Phase**: During `ilha setup`, the base `docker-compose.yml` in the project root is read and transformed into `.ilha/docker-compose.worktree.yml`
- **Runtime Phase**: All commands (`up`, `down`, `exec`, `logs`, etc.) exclusively use `worktrees/{branch}/.ilha/docker-compose.worktree.yml`
- **No Fallback**: There is no backward compatibility or fallback logic - ilha will only use the worktree compose file

### Droplet Configuration

Droplet defaults can be configured in `.env` or `.ilha/env.ilha`:

```bash
# .env or .ilha/env.ilha
DROPLET_DEFAULT_REGION=nyc1
DROPLET_DEFAULT_SIZE=s-1vcpu-1gb
DROPLET_DEFAULT_IMAGE=ubuntu-22-04-x64
# SSH key names (comma-separated, e.g., anders,peter)
# Only key names are supported, not numeric IDs or fingerprints
DROPLET_DEFAULT_SSH_KEYS=anders,peter
DIGITALOCEAN_API_TOKEN=your_token_here
```

### Configuration File

The `.ilha/config.yml` file contains:

```yaml
project_name: myproject
caddy_network: ilha_caddy_proxy
worktree_dir: worktrees
services:
  web:
    container_name_template: ${COMPOSE_PROJECT_NAME}-web
  db:
    container_name_template: ${COMPOSE_PROJECT_NAME}-db
  redis:
    container_name_template: ${COMPOSE_PROJECT_NAME}-redis
volumes:
  - postgres_data
  - redis_data
  - media_files
environment:
  DEBUG: "True"
  ALLOWED_HOSTS: "localhost,127.0.0.1,*.localhost,web"
```

### Volume Naming Convention
Worktree-specific volumes follow the pattern: `{branch_name}_{volume_type}`

- `feature-auth_postgres_data` - Database data
- `feature-auth_redis_data` - Cache data  
- `feature-auth_media_files` - User uploads

### Network Configuration
- **Global Network**: `ilha_caddy_proxy` (external)
- **Worktree Networks**: `{branch_name}_internal`, `{branch_name}_web`

## 🛠️ Development Workflows

### Feature Development
```bash
# 1. Initialize ilha in your project
ilha setup

# 2. Start global infrastructure
ilha start-proxy

# 3. Create feature branch environment
ilha create feature-new-auth
ilha feature-new-auth up -d

# 4. Develop and test
open http://feature-new-auth.localhost
# Make changes, test database migrations, etc.

# 5. Clean up when done
ilha feature-new-auth down
ilha remove feature-new-auth
```

### Multiple Feature Branches
```bash
# Work on multiple features simultaneously
ilha create feature-auth
ilha create feature-payments
ilha create feature-notifications

# Start all environments
ilha feature-auth up -d
ilha feature-payments up -d
ilha feature-notifications up -d

# Access each independently
open http://feature-auth.localhost
open http://feature-payments.localhost
open http://feature-notifications.localhost
```

### Database Testing
```bash
# Test database migrations in isolation
ilha create test-migration
ilha test-migration up -d

# Run migrations, test data changes
# Each worktree has its own database

# Clean up test environment
ilha delete test-migration
```

### Docker Compose Integration
```bash
# Execute Django management commands
ilha feature-auth exec web python manage.py migrate
ilha feature-auth exec web python manage.py collectstatic

# View application logs
ilha feature-auth logs web
ilha feature-auth logs -f web  # Follow logs

# Run one-off commands
ilha feature-auth run --rm web python manage.py test
ilha feature-auth run --rm web bash

# Check container status
ilha feature-auth ps

# Restart services
ilha feature-auth restart web
```

## 🔍 Troubleshooting

### Common Issues

**Docker not running**
```bash
Error: Docker is not running. Please start Docker and try again.
```
**Solution**: Start Docker Desktop or Docker daemon

**PostgreSQL Database Corruption**
```bash
Error: invalid primary checkpoint record
Error: could not locate a valid checkpoint record
```
**Solution**: This has been fixed! ilha now stops the original database container before copying volumes, then restarts it after copying. This ensures data consistency and prevents corruption.

**Worktree already exists**
```bash
Error: Worktree for branch 'feature-auth' already exists
```
**Solution**: Use `ilha list` to see existing worktrees, or remove the existing one

**Port conflicts**
```bash
Error: Port 80 is already in use
```
**Solution**: Stop other services using port 80, or check if global Caddy is already running

**Volume creation fails**
```bash
Error: Failed to create volume
```
**Solution**: Check Docker disk space, ensure Docker has sufficient permissions

**Branch not found during deletion**
```bash
Error: No exact match found for 'feature-auth'. Checked: worktrees, branches, and docker volumes.
```
**Solution**: Use `ilha list` to see exact branch names. Branch names are case-sensitive and must match exactly.

**HTTPS hangs or certificate acquisition fails (Let's Encrypt rate limit)**
```bash
Error: HTTP 429 urn:ietf:params:acme:error:rateLimited - too many certificates (5) already issued for this exact set of identifiers in the last 168h0m0s
```
**Symptoms**: HTTPS connections hang or timeout, HTTP works fine. Browser shows connection timeout when accessing `https://your-domain.com`.

**Root Cause**: Let's Encrypt limits certificate issuance to 5 certificates per domain per 168 hours (7 days). If you've deployed the same domain multiple times, you may hit this limit.

**Solution**: Use Let's Encrypt staging certificates temporarily until the rate limit expires:

1. **Manual Fix (Immediate)**: SSH to your server and update Caddy configuration:
   ```bash
   # Get current config
   curl -s http://localhost:2019/config/ > /tmp/caddy_config.json
   
   # Edit config to use staging endpoint (add "ca" field to issuer)
   # Update the issuer in apps.tls.automation.policies[0].issuers[0]:
   # Add: "ca": "https://acme-staging-v02.api.letsencrypt.org/directory"
   
   # Reload Caddy
   curl -X POST http://localhost:2019/load -H "Content-Type: application/json" -d @/tmp/caddy_config.json
   ```

2. **Automatic Fix (Long-term)**: ilha's dynamic configuration script (`caddy-dynamic-config.py`) now automatically detects rate limit errors and falls back to staging certificates. The script checks Caddy logs for rate limit patterns and automatically switches to staging when detected.

3. **Force Staging Mode for Testing**: You can force staging certificates for testing without hitting rate limits by setting the `USE_STAGING_CERTIFICATES` environment variable:
   ```bash
   # Option 1: Set in your shell before running ilha commands
   export USE_STAGING_CERTIFICATES=1
   
   # Option 2: Add to your .ilha/env.ilha file (recommended for droplet create/push)
   echo "USE_STAGING_CERTIFICATES=1" >> .ilha/env.ilha
   ```
   **For `droplet create` and `droplet push`**: Add `USE_STAGING_CERTIFICATES=1` to your `.ilha/env.ilha` file before exporting. This ensures the setting is included in the package and will be used on the remote server. This is perfect for testing environments where you don't want to consume your production certificate quota. Staging certificates don't count against Let's Encrypt's rate limits.

4. **Staging Certificates**: 
   - Staging certificates work for HTTPS but show browser security warnings
   - Users can proceed after accepting the warning
   - Connection is still encrypted, just not trusted by default
   - Switch back to production certificates after rate limit expires (check Caddy logs for "retry after" timestamp)
   - **Staging certificates don't count against Let's Encrypt rate limits** - perfect for testing!

5. **Monitor Certificate Health**: The `caddy-docker-monitor.py` script monitors certificate health and logs warnings when rate limits are detected.

**Note**: Rate limits expire after 168 hours (7 days) from the first certificate issuance. Check Caddy logs for the exact expiration time.

### Debug Commands

```bash
# Check Docker status
docker ps

# Check worktree status
ilha list

# Check volume status
ilha volumes list

# Check network status
docker network ls | grep ilha_caddy_proxy
```

### Logs and Debugging

```bash
# View container logs
docker logs ilha_caddy_proxy
docker logs {branch-name}-web

# Check worktree directory
ls -la worktrees/{branch-name}/

# Verify environment files
cat worktrees/{branch-name}/.ilha/env.ilha
```

## ⌨️ Shell Completion

ilha CLI provides intelligent tab completion for Bash and Zsh shells, making it faster and easier to use.

### Features

- **Command Completion**: Tab complete all ilha commands and subcommands
- **Worktree Names**: Auto-complete worktree branch names for commands that need them
- **Volume Operations**: Complete worktree names for volume backup/restore/clean operations
- **Flag Completion**: Auto-complete command flags like `--force`, `-d`, `--detach`

### Installation

#### Automatic Installation (Recommended)

Shell completion is automatically offered during project setup:

```bash
ilha setup
# After successful setup, you'll be prompted:
# "Would you like to install bash completion? [Y/n]"
```

#### Manual Installation

```bash
# Install for current shell (auto-detected)
ilha completion install

# Install for specific shell
ilha completion install bash
ilha completion install zsh

# Check installation status
ilha completion status

# Uninstall completion
ilha completion uninstall
```

### Usage Examples

```bash
# Tab complete commands
ilha <TAB>
# Shows: create delete down help list prune remove remove-all setup start-proxy stop-proxy start stop up volumes

# Tab complete worktree names
ilha feature-auth <TAB>
# Shows: up down exec logs ps run build restart

# Tab complete volume operations
ilha volumes backup <TAB>
# Shows: feature-auth feature-payments feature-notifications

# Tab complete flags
ilha delete <TAB>
# Shows: feature-auth --force
```

### Troubleshooting

**Completion not working after installation:**
```bash
# Restart your shell or source the configuration
source ~/.bashrc  # For Bash
source ~/.zshrc   # For Zsh
```

**Check completion status:**
```bash
ilha completion status
```

**Reinstall completion:**
```bash
ilha completion uninstall
ilha completion install
```

## 🚀 Advanced Usage

### Custom Configuration
Create custom environment files for specific worktrees:

```bash
# Create worktree with custom settings
ilha create feature-auth

# Edit environment file
vim worktrees/feature-auth/.ilha/env.ilha

# Start with custom configuration
ilha feature-auth up -d
```

### Volume Management
```bash
# Backup before major changes
ilha volumes backup feature-auth

# Check volume sizes
ilha volumes size

# Clean up old volumes
ilha volumes clean feature-auth
```

### Package Management

**Auto-Detection** (recommended):
```bash
# Automatically chooses correct mode based on current directory
ilha packages import myapp-feature-auth.tar.gz
```

**Standalone Import** (create new project):
```bash
# Create new project from package
mkdir ~/new-project && cd ~/new-project
ilha packages import myapp-feature-auth.tar.gz --standalone

# Or specify target directory
ilha packages import myapp-feature-auth.tar.gz --standalone --target-dir ./myproject
```

**Normal Import** (add to existing project):
```bash
# Import to existing project as new worktree
cd /path/to/existing-project
ilha packages import myapp-feature-auth.tar.gz
```

**Import Options**:
```bash
# Import without data (configuration and code only)
ilha packages import myapp-feature-auth.tar.gz --no-data

# Import to specific branch name
ilha packages import myapp-feature-auth.tar.gz --target-branch new-branch-name

# Domain/IP overrides for deployment
ilha packages import myapp-feature-auth.tar.gz --standalone --domain myapp.example.com  # HTTPS via Caddy
ilha packages import myapp-feature-auth.tar.gz --standalone --ip 203.0.113.10           # HTTP-only (no TLS)
```

**Export Options**:
```bash
# Export complete environment as shareable package (includes code by default)
ilha packages export feature-auth

# Export environment without code (smaller package)
ilha packages export feature-auth --no-code

# Export to specific directory
ilha packages export feature-auth --output-dir ./exports
```

**Package Management**:
```bash
# List available packages
ilha packages list

# Validate package integrity
ilha packages validate myapp-feature-auth.tar.gz
```

### Bulk Operations
```bash
# Remove all worktrees (keeps git branches)
ilha remove-all

# Delete all worktrees and branches (destructive)
ilha delete-all --force

# Clean up prunable worktrees
ilha prune
```

### Wildcard Operations
```bash
# Remove all test branches (keeps git branches)
ilha remove test-*

# Delete all feature branches and their worktrees
ilha delete feature-*

# Remove branches matching pattern with confirmation
ilha remove bugfix-*
# Output: Found 3 matching branch(es): bugfix-auth, bugfix-payment, bugfix-ui
# Remove 3 worktree(s) (keep branches)? [Y/n]: 

# Delete with force flag (skips confirmation)
ilha delete temp-* --force
```

## 📚 Additional Documentation

**Agent skills:** This repo includes an [Agent Skill](https://agentskills.io/) at `ilha/skills/ilha/` (inside the ilha package). Symlinks from `.agents/skills/ilha` and `.claude/skills/ilha` point to it so **Cursor**, **Claude Code**, and **Codex** discover the skill when the repo is open; no installation needed.

- **[📖 Full Documentation Site](https://catalpainternational.github.io/ilha)** - Complete documentation with search and navigation
- **[Project Setup Guide](documentation/PROJECT_SETUP.md)** - Best practices for configuring your project for ilha
- **[Architecture Guide](documentation/ARCHITECTURE.md)** - Detailed system architecture and component relationships
- **[Troubleshooting Guide](documentation/TROUBLESHOOTING.md)** - Common issues and solutions
- **[Design Documents](design_documents/)** - Original design and implementation plans
- **[Test Documentation](tests/)** - Comprehensive test suite and coverage
- **[Configuration Reference](config/)** - All configuration files and options

## 🤝 Contributing

The ilha CLI follows a modular architecture designed for easy extension:

- **Core Infrastructure**: `core/` - Docker, Git, and environment management
- **Command Layer**: `commands/` - Business logic for each command
- **Utilities**: `utils/` - Shared functionality and validation
- **Configuration**: `config/` - Settings and Docker Compose files

### Adding New Commands

1. Create command class in `commands/`
2. Add CLI interface in `cli.py`
3. Add tests in `tests/unit/`
4. Update documentation

### Extending Functionality

- **New Volume Types**: Modify `EnvironmentManager.get_volume_names()`
- **Custom Networks**: Update `DockerManager.create_network()`
- **Additional Validation**: Extend `utils/validation.py`

## 📊 Performance

- **Worktree Creation**: ~30 seconds
- **Volume Copying**: ~60 seconds (depending on data size)
- **Container Startup**: ~45 seconds
- **Memory Usage**: ~1GB per worktree
- **Disk Usage**: ~5GB per worktree (varies with data)

## ✅ Status

**Production Ready**: All tests passing, 100% functional compatibility with original bash script.

**Key Features**:
- ✅ Complete environment isolation
- ✅ Dynamic routing with Caddy
- ✅ Volume management and backup
- ✅ Git worktree integration
- ✅ Docker Compose commands
- ✅ Comprehensive error handling
- ✅ Rich console output
- ✅ Type safety throughout
- ✅ Project-agnostic setup
- ✅ User guide in each .ilha directory
- ✅ JSON output mode for programmatic access
 - ✅ Push command for SCP-based deployment with auto-import enabled by default

## 🚀 Deployment (Push)

### Create Droplet and Push (Default)
```bash
# Create a new Digital Ocean droplet and automatically push environment to it
# Branch name auto-detected from current directory, droplet name uses branch name
# Auto-import is enabled by default
ilha droplet create \
  --prepare-server

# With explicit branch name (droplet name = branch name)
ilha droplet create test \
  --prepare-server

# With domain (droplet name = subdomain from domain)
ilha droplet create \
  --domain app.example.com \
  --prepare-server

# With custom droplet configuration
ilha droplet create test \
  --region sfo3 --size s-2vcpu-4gb \
  --prepare-server \
  --domain app.example.com

# Create droplet only, do not push
ilha droplet create test --create-only
```

### Basic Push to Existing Server
```bash
# Export and transfer a package to a remote server via SCP
# Supports progressive SCP target patterns:
ilha droplet push feature-auth user@server:/var/ilha/packages

# Progressive patterns - Username + domain
ilha droplet push feature-auth deploy@example.com

# Progressive patterns - Domain + path
ilha droplet push feature-auth example.com:/var/ilha

# Progressive patterns - Domain only (resolves to IP)
ilha droplet push feature-auth example.com

# Progressive patterns - IP + path
ilha droplet push feature-auth 192.168.1.100:/var/ilha

# Progressive patterns - IP only
ilha droplet push feature-auth 192.168.1.100

# Droplet by name
ilha droplet push feature-auth my-app

# Droplet by ID
ilha droplet push feature-auth 12345678
```

### Auto-Import on Remote with Domain and HTTPS
```bash
# Push with automatic DNS management and HTTPS deployment
# Auto-import is enabled by default
ilha droplet push feature-auth user@server:/var/ilha/packages \
  --prepare-server \
  --domain app.example.com \
  --dns-token $DIGITALOCEAN_API_TOKEN

# Or use environment variable for token
export DIGITALOCEAN_API_TOKEN=your_token_here
ilha droplet push feature-auth user@server:/var/ilha/packages \
  --domain app.example.com

# Or add to .env file in project root (no export needed)
# .env file:
# DIGITALOCEAN_API_TOKEN=your_token_here
ilha droplet push feature-auth user@server:/var/ilha/packages \
  --domain app.example.com

# To skip auto-import (only push, don't import/start):
ilha droplet push feature-auth user@server:/var/ilha/packages \
  --no-auto-import

# Options:
#   --domain myapp.example.com        # Domain for HTTPS deployment
#   --dns-token <token>                 # Digital Ocean API token (or use DIGITALOCEAN_API_TOKEN env var)
#   --skip-dns-check                    # Skip DNS validation
#   --ip 203.0.113.10                   # IP-only HTTP mode (no Let's Encrypt for IPs)
```

### Code-Only Push (Fast Updates)
```bash
# Push code-only update to pre-existing server
# Uses stored push configuration from env.ilha (saved after first push)
ilha droplet push --code-only

# Code-only update with explicit arguments (overrides stored config)
# Works with all progressive SCP patterns:
ilha droplet push feature-auth user@server:/var/ilha/packages --code-only
ilha droplet push feature-auth my-app --code-only
ilha droplet push feature-auth 192.168.1.100 --code-only
ilha droplet push feature-auth deploy@example.com --code-only
ilha droplet push feature-auth example.com:/var/ilha --code-only

# Code-only update with domain/IP override
ilha droplet push --code-only --domain app.example.com
ilha droplet push --code-only --ip 203.0.113.10
```

**How Code-Only Push Works:**
- **Automatic Detection**: ilha automatically detects whether your code is stored in Docker volumes or bind mounts
- **Volume-Based**: If code is in volumes, only code volumes are backed up and transferred
- **Archive-Based**: If code is in bind mounts, a git archive is created and extracted to the worktree
- **Configuration Storage**: Push configuration (scp_target, branch_name, domain, ip) is automatically saved to `.ilha/env.ilha` after successful push
- **Reuse Configuration**: On subsequent pushes, stored configuration is used automatically (CLI arguments override stored config)

**Push Configuration Variables:**
After a successful push (full or code-only), the following variables are saved to `.ilha/env.ilha`:
```
PUSH_SCP_TARGET=user@server:/path/to/packages
PUSH_BRANCH_NAME=feature-auth
PUSH_DOMAIN=app.example.com  # optional
PUSH_IP=203.0.113.10  # optional (mutually exclusive with domain)
```

**When to Use Code-Only Push:**
- Quick code updates on pre-existing servers
- Deploying minor fixes without full environment redeployment
- Faster iteration during development
- When only code changes, not environment configuration

**When to Use Full Push:**
- Initial deployment
- Environment configuration changes
- Database schema migrations
- Volume data updates

### Progressive SCP Target Patterns

The `droplet push` command supports progressive SCP target patterns, making it easier to deploy without manually constructing SCP targets:

**Supported Patterns:**
- **Full SCP target**: `user@server:/path` → use as-is (backward compatible)
- **Username + domain**: `user@example.com` → resolves domain to IP, constructs `user@<ip>:/root`
- **Domain + path**: `example.com:/path` → resolves domain to IP, constructs `root@<ip>:<path>`
- **Domain only**: `example.com` → resolves domain to IP, constructs `root@<ip>:/root`
- **IP + path**: `192.168.1.100:/path` → constructs `root@192.168.1.100:/path`
- **IP only**: `192.168.1.100` → constructs `root@192.168.1.100:/root`
- **Droplet ID**: `12345678` → resolves to IP via API, constructs `root@<ip>:/root`
- **Droplet name**: `my-app` → resolves to IP via API, constructs `root@<ip>:/root`

**Resolution Priority:**
1. If already an IP address → use directly
2. Try DNS resolution (domain lookup) → use resolved IP
3. Try droplet ID/name lookup → use droplet IP

**Domain Setup Fix:**
When using progressive patterns with `--domain`, the resolved IP from the SCP target is automatically used for DNS management, ensuring DNS records point to the correct server.

### Multi-Project Deployments

ilha supports deploying **multiple independent projects** to the same server, each with their own unique URL. This enables efficient resource usage while maintaining complete isolation between projects.

#### How It Works

**Global Caddy Proxy:**
- A single global Caddy container (`ilha_caddy_proxy`) runs on the server
- Monitors **all Docker containers** on the host via `/var/run/docker.sock`
- Automatically discovers containers with `caddy.proxy` labels from **any project**
- Routes traffic based on domain/IP specified in container labels

**Container Discovery:**
- Caddy dynamically scans all containers on the Docker host
- Finds containers with `caddy.proxy` labels regardless of which project directory they belong to
- Each project's containers are automatically discovered and routed

**Shared Network:**
- All project containers join the `ilha_caddy_proxy` network (external)
- This enables the global Caddy to route to containers from any project
- Projects remain isolated but share the routing infrastructure

#### Deployment Example

```bash
# Deploy Project 1 (e.g., Django app) to server
cd /var/ilha/project1
ilha droplet push feature-auth user@server:/var/ilha/packages \
  --domain app1.example.com

# Deploy Project 2 (e.g., Flask API) to same server
cd /var/ilha/project2  
ilha droplet push api-v2 user@server:/var/ilha/packages \
  --domain api.example.com

# Deploy Project 3 (e.g., Node.js app) to same server
cd /var/ilha/project3
ilha droplet push main user@server:/var/ilha/packages \
  --domain app3.example.com
```

All three projects will:
- ✅ Run independently with isolated containers and volumes
- ✅ Be accessible via their own unique domains
- ✅ Share the same global Caddy proxy for routing
- ✅ Not interfere with each other

#### Requirements

1. **Global Caddy Must Be Running**: Ensure `ilha_caddy_proxy` container is running on the server
   ```bash
   # On the server, start global Caddy if not already running
   ilha start-proxy
   ```

2. **Unique Domains/IPs**: Each project must have a unique domain or IP address
   - ✅ `app1.example.com`, `app2.example.com`, `api.example.com` (all different)
   - ❌ Cannot use the same domain for multiple projects

3. **Project Isolation**: Projects can be in completely different directories
   - Standalone mode works perfectly for multi-project deployments
   - Each project maintains its own `.ilha/` configuration

4. **Automatic Label Configuration**: ilha automatically adds `caddy.proxy` labels to containers
   - Labels are set based on `--domain` or `--ip` flags during push
   - No manual configuration needed

#### Benefits

- **Resource Efficiency**: One reverse proxy handles all projects
- **Complete Isolation**: Each project has its own containers, volumes, and networks
- **Simple Management**: Deploy each project independently
- **Flexible**: Mix domain-based HTTPS and IP-based HTTP deployments
- **Scalable**: Add new projects without affecting existing ones

#### Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│              Global Caddy Proxy                         │
│         (ilha_caddy_proxy)                        │
│         Ports: 80, 443, 2019                           │
│                                                         │
│  Monitors ALL containers via Docker socket              │
│  Routes based on caddy.proxy labels                     │
└─────────────────────────────────────────────────────────┘
                    │
        ┌───────────┼───────────┐
        │           │           │
┌───────▼──────┐ ┌──▼──────┐ ┌──▼──────┐
│  Project 1   │ │Project 2│ │Project 3│
│              │ │         │ │         │
│ app1.example │ │api.exam │ │app3.exam│
│     .com     │ │  ple.com│ │  ple.com│
│              │ │         │ │         │
│ • PostgreSQL │ │• MongoDB│ │• Redis  │
│ • Redis      │ │• Web    │ │• Web    │
│ • Web App    │ │• API    │ │• App    │
└──────────────┘ └─────────┘ └─────────┘
```

### DNS Management
ilha can automatically manage DNS records via Digital Ocean DNS API:

- **Automatic Domain Creation**: If a subdomain doesn't exist, ilha will automatically create it when `--domain` is provided
- **Domain Validation**: Checks if DNS records already exist and point to the correct server
- **Supported Provider**: Digital Ocean DNS
- **API Token Configuration**: Multiple options supported (priority order):
  1. CLI flag: `--dns-token <token>`
  2. Shell environment: `export DIGITALOCEAN_API_TOKEN=token`
  3. `.env` file: Add `DIGITALOCEAN_API_TOKEN=token` to project root `.env` file
  4. Global config: Add `DIGITALOCEAN_API_TOKEN=token` to `~/.ilha/env.ilha` file

### DNS Provider Setup

#### Digital Ocean
1. Generate a personal access token from https://cloud.digitalocean.com/account/api/tokens
2. Configure token using one of these methods:
   - **Shell environment** (recommended for CI/CD): `export DIGITALOCEAN_API_TOKEN=your_token`
   - **Project `.env` file** (recommended for project-specific tokens): Add `DIGITALOCEAN_API_TOKEN=your_token` to your project root `.env` file
   - **Global config** (recommended for personal tokens): Add `DIGITALOCEAN_API_TOKEN=your_token` to `~/.ilha/env.ilha` file
   - **CLI flag**: Use `--dns-token <token>` when pushing

### DNS Propagation

When ilha creates a DNS record, it is immediately available on Digital Ocean's authoritative nameservers, but it takes time to propagate to all DNS resolvers worldwide.

**What is DNS Propagation?**
DNS propagation is the time it takes for DNS record changes to spread across all DNS servers on the internet. When you create a new DNS record, it's immediately available on the authoritative nameservers (Digital Ocean's in this case), but other DNS resolvers (like Google's 8.8.8.8 or Cloudflare's 1.1.1.1) cache DNS records and may take time to update.

**Expected Propagation Times:**
- **Authoritative nameservers**: Immediate (Digital Ocean nameservers)
- **Public resolvers**: 5-60 minutes typically
- **Global propagation**: Up to 48 hours in rare cases

**Verifying DNS Records:**

You can verify DNS records in several ways:

1. **Check on Digital Ocean nameservers** (immediate):
   ```bash
   dig your-domain.com A @ns1.digitalocean.com
   dig your-domain.com A @ns2.digitalocean.com
   dig your-domain.com A @ns3.digitalocean.com
   ```

2. **Check on public resolvers** (may take time):
   ```bash
   dig your-domain.com A @8.8.8.8      # Google DNS
   dig your-domain.com A @1.1.1.1      # Cloudflare DNS
   ```

3. **Check from your local machine**:
   ```bash
   dig your-domain.com A
   nslookup your-domain.com
   ```

**Troubleshooting:**
- If the record exists on Digital Ocean nameservers but not on public resolvers, wait a few more minutes
- If the record doesn't exist on any nameserver, check that the DNS record was created successfully
- Browser DNS caches may need to be cleared or wait for TTL expiration

### VPC Deployments (DigitalOcean)

ilha supports VPC (Virtual Private Cloud) deployments for secure private networking between droplets:

**Central and Worker Droplet Setup:**

```bash
# 1. Create central server droplet with db/redis services
ilha droplet create test \
  --domain central.example.com \
  --containers test.db,test.redis,test.web \
  --prepare-server

# 2. Create worker droplet in same VPC (reuses VPC UUID from central)
ilha droplet create test \
  --central-droplet-name central \
  --containers test.rq-worker-1 \
  --exclude-deps db,redis \
  --prepare-server
```

**VPC Features:**
- **Automatic VPC Detection**: Extracts private IP addresses and VPC UUID from droplets
- **VPC UUID Reuse**: Use `--central-droplet-name` to automatically reuse VPC UUID from central droplet
- **Port Binding**: Configure `.ilha/config.yml` to automatically bind ports for VPC-accessible services:
  ```yaml
  vpc:
    auto_bind_ports: true  # Enable automatic port binding (default: false)
    bind_to_private_ip: true  # Bind to private IP instead of 0.0.0.0 (default: true, recommended for security)
  ```
- **Security**: Redis and database ports are automatically bound to private IP (not public) when available, preventing public internet exposure
- **Firewall Configuration**: Optional automatic UFW firewall rules to restrict access to VPC network only
- **Worker Environment Configuration**: When deploying workers with `--exclude-deps`, environment variables are automatically configured to point to central server's private IP
- **VPC Metadata**: Package metadata includes VPC deployment information for automatic configuration

**VPC Configuration Options:**
- `--vpc-uuid <uuid>` - Explicitly specify VPC UUID
- `--central-droplet-name <name>` - Reuse VPC UUID from central droplet (for worker deployments)
- `--exclude-deps <services>` - Exclude services from dependency resolution (indicates worker deployment)

**Security Best Practices for VPC Deployments:**

When deploying with `--central-server` and VPC networking, ilha implements multiple security layers:

1. **Private IP Binding** (Default: Enabled)
   - Redis and database ports bind to private IP address (e.g., `10.x.x.x:6379`) instead of `0.0.0.0`
   - Prevents public internet exposure while maintaining VPC accessibility
   - Configure via `.ilha/config.yml`:
     ```yaml
     vpc:
       bind_to_private_ip: true  # Default: true
     ```

2. **Firewall Rules** (Optional: Opt-in)
   - Automatically configures UFW firewall rules to restrict access to VPC network (10.0.0.0/8)
   - Only allows connections from within the VPC
   - Configure via `.ilha/config.yml`:
     ```yaml
     vpc:
       auto_configure_firewall: true  # Default: false (opt-in)
     ```

3. **Verification**
   After deployment, verify security:
   ```bash
   # On central server, check port bindings
   docker ps --format "{{.Names}}\t{{.Ports}}" | grep -E "(redis|db)"
   # Should show: 10.x.x.x:6379->6379/tcp (not 0.0.0.0:6379)
   
   # Verify public access is blocked
   telnet <public-ip> 6379  # Should fail
   
   # Verify VPC access works
   telnet <private-ip> 6379  # Should succeed from worker droplet
   ```

### Notes
- When using `--domain`, ilha automatically enables HTTPS via Caddy's Let's Encrypt integration
- When using `--ip`, deployments are HTTP-only. Certificate authorities do not issue certificates for IP addresses; use a domain for HTTPS.
- DNS records are automatically created when `--domain` is provided (use `--skip-dns-check` to skip DNS management)
- You can add defaults in `.ilha/config.yml` (optional):
```yaml
deployment:
  default_server: user@server:/var/ilha/packages
  default_domain: myapp.example.com
  default_ip: 203.0.113.10
  ssh_key: ~/.ssh/deploy_key
vpc:
  auto_bind_ports: true  # Enable automatic port binding for VPC-accessible services
  bind_to_private_ip: true  # Bind to private IP only (default: true, recommended for security)
  auto_configure_firewall: false  # Auto-configure UFW rules (default: false, opt-in)
```

---

*For detailed architecture information, see [documentation/ARCHITECTURE.md](documentation/ARCHITECTURE.md)*

Additional Push Details:
- Auto-import uses a robust remote script with strict mode and consistent quoting to avoid empty variables and broken chains.
- The remote script resolves the ilha binary (prefers `/opt/ilha-venv/bin/ilha`, falls back to `ilha`).
- Import runs with `--non-interactive`; older ilha versions safely ignore unknown flags.
- If an existing ilha project is detected on the server, a normal import is used; otherwise standalone import is performed.
