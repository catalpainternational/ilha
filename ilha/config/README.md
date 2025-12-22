# ilha User Guide

This directory contains the ilha configuration for your project. This guide is optimized for coding agents and developers working with ilha environments.

## 🚀 Quick Reference

### Essential Commands

```bash
# Worktree Management
ilha create <branch-name>     # Create new worktree
ilha <branch-name> up -d      # Start worktree environment
ilha <branch-name> down       # Stop worktree environment
ilha <branch-name> remove     # Remove worktree (keep branch)
ilha <branch-name> delete     # Delete worktree and branch

# Docker Compose Integration
ilha <branch-name> exec <service> <command>    # Execute command in container
ilha <branch-name> logs <service>              # View container logs
ilha <branch-name> ps                          # List containers
ilha <branch-name> run <service> <command>     # Run one-off command
```

### For Coding Agents

**Key Pattern**: Use `ilha <worktree-name> <docker-compose-command>` for all container operations.

**Common Tasks**:
```bash
# Django Management
ilha feature-auth exec web python manage.py migrate
ilha feature-auth exec web python manage.py collectstatic
ilha feature-auth exec web python manage.py test

# Database Operations
ilha feature-auth exec web python manage.py shell
ilha feature-auth exec web python manage.py dbshell

# Application Logs
ilha feature-auth logs web
ilha feature-auth logs -f web  # Follow logs

# Container Management
ilha feature-auth ps
ilha feature-auth restart web
ilha feature-auth run --rm web bash
```

## 📁 Directory Structure

```
Project Root/
├── .ilha/
│   ├── README.md                    # This file
│   ├── config.yml                   # Project configuration
│   ├── docker-compose.worktree.yml  # Docker Compose override file
│   └── env.ilha               # Environment variables for worktree
└── worktrees/                       # Worktree directories (configurable)
    ├── feature-auth/
    ├── feature-payments/
    └── test-branch/
```

## 🔧 Configuration Files

### `config.yml`
Main project configuration including:
- Project name
- Caddy network settings
- Service container name templates
- Volume configurations
- Environment variables

### `docker-compose.worktree.yml`
Docker Compose override file that:
- Extends your main `docker-compose.yml`
- Adds worktree-specific configurations
- Sets up isolated volumes and networks
- Configures Caddy routing

### `env.ilha`
Environment variables for each worktree:
- `COMPOSE_PROJECT_NAME`: Unique project name per worktree
- `PROJECT_ROOT`: Path to worktree directory
- `SITE_DOMAIN`: Unique domain for the worktree
- `ALLOWED_HOSTS`: Django allowed hosts configuration

## 🌐 Accessing Your Applications

Each worktree gets its own domain:
- **Pattern**: `{project-name}-{branch-name}.localhost`
- **Example**: `myapp-feature-auth.localhost`

### Common Service Names
- `web` - Main web application (Django, Flask, etc.)
- `db` - Database service (PostgreSQL, MySQL)
- `redis` - Cache service
- `nginx` - Web server (if used)

## 🛠️ Development Workflow

### 1. Create and Start Worktree
```bash
# Create new worktree
ilha create feature-new-auth

# Start the environment
ilha feature-new-auth up -d

# Access the application
open http://myapp-feature-new-auth.localhost
```

### 2. Development Tasks
```bash
# Run migrations
ilha feature-new-auth exec web python manage.py migrate

# Install new packages
ilha feature-new-auth exec web pip install new-package

# Run tests
ilha feature-new-auth run --rm web python manage.py test

# Check logs
ilha feature-new-auth logs web
```

### 3. Clean Up
```bash
# Stop environment
ilha feature-new-auth down

# Remove worktree (keeps git branch)
ilha feature-new-auth remove

# Or delete completely (removes git branch)
ilha feature-new-auth delete
```

## 🔍 Troubleshooting

### Common Issues

**Worktree not found**:
```bash
# Check existing worktrees
ilha list

# Create if needed
ilha create <branch-name>
```

**Container not running**:
```bash
# Check container status
ilha <branch-name> ps

# View logs
ilha <branch-name> logs web

# Restart if needed
ilha <branch-name> restart web
```

**Database issues**:
```bash
# Check database container
ilha <branch-name> exec db psql -U user -d database

# Run migrations
ilha <branch-name> exec web python manage.py migrate
```

### Debug Commands

```bash
# Check worktree status
ilha list

# Check container status
ilha <branch-name> ps

# View all logs
ilha <branch-name> logs

# Check environment variables
ilha <branch-name> exec web env | grep COMPOSE
```

## 📊 Volume Management

Each worktree has isolated volumes:
- `{project}-{branch}_postgres_data` - Database data
- `{project}-{branch}_redis_data` - Cache data
- `{project}-{branch}_media_files` - User uploads

### Volume Operations
```bash
# List volumes
ilha volumes list

# Check volume sizes
ilha volumes size

# Backup volumes
ilha volumes backup <branch-name>

# Clean up volumes
ilha volumes clean <branch-name>
```

## 🚀 Advanced Usage

### Multiple Worktrees
```bash
# Work on multiple features simultaneously
ilha create feature-auth
ilha create feature-payments
ilha create feature-notifications

# Start all
ilha feature-auth up -d
ilha feature-payments up -d
ilha feature-notifications up -d

# Access each independently
open http://myapp-feature-auth.localhost
open http://myapp-feature-payments.localhost
open http://myapp-feature-notifications.localhost
```

### Bulk Operations
```bash
# Remove all worktrees (keeps branches)
ilha remove-all

# Delete all worktrees and branches
ilha delete-all --force

# Remove by pattern
ilha remove test-*
ilha delete feature-*
```

### Custom Environment
```bash
# Edit worktree environment
vim worktrees/<branch-name>/.ilha/env.ilha

# Restart to apply changes
ilha <branch-name> down
ilha <branch-name> up -d
```

## 🎯 For Coding Agents

### Key Patterns to Remember

1. **Always use worktree name first**: `ilha <worktree-name> <command>`
2. **Docker compose commands work directly**: `ilha <worktree-name> exec web <command>`
3. **Each worktree is isolated**: Changes in one worktree don't affect others
4. **Use `exec` for interactive commands**: `ilha <worktree-name> exec web bash`
5. **Use `run` for one-off commands**: `ilha <worktree-name> run --rm web <command>`

### Common Django Tasks
```bash
# Database operations
ilha <branch> exec web python manage.py migrate
ilha <branch> exec web python manage.py makemigrations
ilha <branch> exec web python manage.py shell

# Static files
ilha <branch> exec web python manage.py collectstatic

# Testing
ilha <branch> run --rm web python manage.py test
ilha <branch> run --rm web python manage.py test app.tests

# Package management
ilha <branch> exec web pip install package-name
ilha <branch> exec web pip freeze > requirements.txt
```

### Service Discovery
```bash
# List available services
ilha <branch> ps

# Common service names: web, db, redis, nginx
# Use these in exec/run commands
```

---

**Need Help?** Run `ilha --help` for full command reference.
