# Project Setup Guide for ilha

This guide provides best practices for configuring your project to work seamlessly with ilha. Follow these patterns to ensure your application works correctly in isolated worktree environments.

## 📋 Table of Contents

1. [Docker Compose Configuration](#1-docker-compose-configuration)
2. [Backend Framework Configuration](#2-backend-framework-configuration)
3. [Frontend Configuration](#3-frontend-configuration)
4. [Environment Variable Patterns](#4-environment-variable-patterns)
5. [Volume Configuration](#5-volume-configuration)
6. [Network Configuration](#6-network-configuration)
7. [Complete Example](#7-complete-example-docker-composeyml)
8. [Quick Checklist](#8-quick-checklist)
9. [Testing Your Setup](#9-testing-your-setup)

## 1. Docker Compose Configuration

### Use `expose` instead of `ports`

ilha uses Caddy as a reverse proxy, so services should expose ports internally rather than binding to host ports:

```yaml
services:
  backend:
    expose:
      - "8000"  # ✅ Use expose for internal networking
    # ports:  # ❌ Don't use ports - Caddy handles external routing
    #   - "8000:8000"
```

**Why**: Caddy handles all external routing. Services communicate internally via Docker networks, and Caddy routes external traffic to the appropriate containers.

### Environment Variable Substitution Pattern

Use environment variable substitution with defaults so `env.ilha` files can override values:

```yaml
services:
  backend:
    environment:
      # ✅ Pattern: ${VAR_NAME:-default_value}
      - ALLOWED_HOSTS=${ALLOWED_HOSTS:-localhost,127.0.0.1}
      - DEBUG=${DEBUG:-True}
      - SITE_DOMAIN=${SITE_DOMAIN:-http://localhost:8000}
```

**Why**: ilha generates `env.ilha` files for each worktree with domain-specific values. Using substitution allows these files to override defaults while maintaining backward compatibility.

### Caddy Labels for Reverse Proxy

Add Caddy labels to services that need external routing:

```yaml
services:
  backend:
    labels:
      - "caddy.proxy=${COMPOSE_PROJECT_NAME:-myapp}.localhost"
      - "caddy.proxy.path=/api/*"
      - "caddy.proxy.reverse_proxy=${COMPOSE_PROJECT_NAME:-myapp}-backend:8000"
    networks:
      - default  # For database/redis access
      - ilha_caddy_proxy  # For Caddy routing
```

**Why**: Caddy automatically discovers containers with these labels and routes traffic accordingly. The `${COMPOSE_PROJECT_NAME}` variable ensures each worktree gets its own subdomain.

### Multi-Stage Docker Builds

Support both development and production modes:

```dockerfile
# Dockerfile example for frontend
FROM node:18-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build

FROM node:18-alpine AS dev
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev"]

FROM node:18-alpine AS prod
RUN npm install -g serve
COPY --from=builder /app/dist ./dist
EXPOSE 5173
CMD ["serve", "-s", "dist", "-l", "5173"]
```

```yaml
# docker-compose.yml
services:
  frontend:
    build:
      context: ./frontend
      target: ${BUILD_MODE:-dev}  # Selects dev or prod stage
    environment:
      - BUILD_MODE=${BUILD_MODE:-dev}
```

**Why**: Allows switching between development (hot reload) and production (static files) modes via the `BUILD_MODE` environment variable.

## 2. Backend Framework Configuration

### Django Settings (Environment-Driven)

Make settings read from environment variables:

```python
# settings.py
import os
from decouple import config

# ✅ Environment-driven configuration
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1').split(',')
CSRF_TRUSTED_ORIGINS = config('CSRF_TRUSTED_ORIGINS', default='').split()
USE_X_FORWARDED_HOST = config('USE_X_FORWARDED_HOST', default='False') == 'True'

# Reverse proxy SSL header (if using HTTPS)
SECURE_PROXY_SSL_HEADER = config('SECURE_PROXY_SSL_HEADER', default=None)
if SECURE_PROXY_SSL_HEADER:
    SECURE_PROXY_SSL_HEADER = tuple(SECURE_PROXY_SSL_HEADER.split(',', 1))

# CORS configuration
CORS_ALLOWED_ORIGINS = config('CORS_ALLOWED_ORIGINS', default='').split(',')
CORS_ALLOW_CREDENTIALS = True
```

**Why**: ilha sets these values in `env.ilha` files based on the worktree domain. Environment-driven configuration ensures each worktree has correct settings.

### WebAuthn/Authentication Configuration

Derive WebAuthn settings from `SITE_DOMAIN` in your application code. ilha sets `SITE_DOMAIN` for each worktree, so use it as the single source of truth:

```python
# settings.py
import os
from urllib.parse import urlparse
from decouple import config

# SITE_DOMAIN is set by ilha for each worktree
SITE_DOMAIN = config('SITE_DOMAIN', default='http://localhost:8000')

def get_webauthn_rp_id():
    """Get WebAuthn RP ID, deriving from SITE_DOMAIN if not explicitly set"""
    explicit_rp_id = config('WEBAUTHN_RP_ID', default=None)
    if explicit_rp_id:
        return explicit_rp_id
    
    # Derive from SITE_DOMAIN (ilha sets this for each worktree)
    if SITE_DOMAIN:
        parsed = urlparse(SITE_DOMAIN)
        # Extract domain without protocol (e.g., 'test.example.com' from 'https://test.example.com')
        return parsed.netloc or parsed.path
    
    return 'localhost'  # Fallback for local development

def get_webauthn_origin():
    """Get WebAuthn Origin, deriving from SITE_DOMAIN if not explicitly set"""
    explicit_origin = config('WEBAUTHN_ORIGIN', default=None)
    if explicit_origin:
        return explicit_origin
    
    # Use SITE_DOMAIN directly (already includes protocol)
    if SITE_DOMAIN:
        return SITE_DOMAIN
    
    return config('FRONTEND_URL', default='http://localhost:5173')

WEBAUTHN_RP_ID = get_webauthn_rp_id()
WEBAUTHN_ORIGIN = get_webauthn_origin()
WEBAUTHN_ALLOWED_ORIGINS = config('WEBAUTHN_ALLOWED_ORIGINS', default=WEBAUTHN_ORIGIN)
```

**Why**: 
- **Single Source of Truth**: `SITE_DOMAIN` is the only domain variable ilha sets
- **Separation of Concerns**: ilha handles infrastructure, your app handles authentication logic
- **Flexibility**: Projects can implement their own derivation logic or use explicit overrides
- **DRY Principle**: No duplication - derivation logic exists only in your application code

**Generic Python Example** (for non-Django projects):

```python
import os
from urllib.parse import urlparse

SITE_DOMAIN = os.getenv('SITE_DOMAIN', 'http://localhost:8000')
parsed = urlparse(SITE_DOMAIN)

# Extract domain without protocol for RP_ID
WEBAUTHN_RP_ID = os.getenv('WEBAUTHN_RP_ID', parsed.netloc or parsed.path)
WEBAUTHN_ORIGIN = os.getenv('WEBAUTHN_ORIGIN', SITE_DOMAIN)
WEBAUTHN_ALLOWED_ORIGINS = os.getenv('WEBAUTHN_ALLOWED_ORIGINS', SITE_DOMAIN)
```

### Other Framework Considerations

**Flask/FastAPI/Express**: Apply the same principles:
- Read configuration from environment variables
- Use `ALLOWED_HOSTS` or equivalent for domain validation
- Configure CORS to allow worktree domains
- Set reverse proxy headers when behind Caddy

## 3. Frontend Configuration

### API Client Configuration

Use `window.location.origin/api` for production, with Vite proxy for local development:

```javascript
// src/services/api.js
const viteApiUrl = import.meta.env.VITE_API_URL;

// Use environment variable only if it's a localhost URL (for Vite proxy)
// In production or ilha deployments, use same origin
const API_BASE_URL = (viteApiUrl && typeof window !== 'undefined' &&
                      (viteApiUrl.includes('localhost') || viteApiUrl.includes('127.0.0.1')))
  ? viteApiUrl
  : (typeof window !== 'undefined' ? `${window.location.origin}/api` : 'http://localhost:8000/api');

const api = axios.create({
  baseURL: API_BASE_URL,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true, // Include cookies for session-based auth
});
```

**Why**: In ilha worktrees, Caddy routes `/api/*` to the backend. Using `window.location.origin/api` ensures API calls go through Caddy's routing, which handles authentication cookies correctly.

### Vite Configuration

Configure Vite to proxy API requests and allow ilha subdomains:

```javascript
// vite.config.js
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    // Allow requests from ilha subdomains
    allowedHosts: process.env.VITE_ALLOWED_HOSTS?.split(',').map(h => h.trim()).filter(Boolean) 
      || ['localhost', '127.0.0.1', '*.localhost'],
    proxy: {
      '/api': {
        target: 'http://backend:8000', // Use Docker service name
        changeOrigin: true,
        secure: false,
      },
    },
  },
})
```

**Why**: 
- `allowedHosts` with `*.localhost` allows Vite dev server to accept requests from ilha subdomains
- Proxy configuration enables local development without Caddy
- Docker service names (`backend`) work within the Docker network

### Other Frontend Frameworks

**Next.js, Nuxt, SvelteKit**: Similar principles apply:
- Configure API base URL to use `window.location.origin` in production
- Allow ilha subdomains in development server configuration
- Use environment variables for API URLs

## 4. Environment Variable Patterns

### Base `docker-compose.yml` Pattern

```yaml
services:
  backend:
    environment:
      # ✅ Use substitution with sensible defaults
      - VAR_NAME=${VAR_NAME:-default_value}
      
      # ✅ For lists, provide comma-separated defaults
      - ALLOWED_HOSTS=${ALLOWED_HOSTS:-localhost,127.0.0.1}
      
      # ✅ For booleans, use string comparison in code
      - DEBUG=${DEBUG:-True}
```

### ilha Auto-Generation

ilha automatically generates `env.ilha` files that override these defaults:

- **`SITE_DOMAIN`** - Set to `http://{project}-{branch}.localhost` for local worktrees (or production domain/IP when deployed)
- **`COMPOSE_PROJECT_NAME`** - Set to `{project}-{branch}`
- **`ALLOWED_HOSTS`** - Includes worktree domain and wildcards
- **`USE_X_FORWARDED_HOST`** - Set to `True` for reverse proxy support
- **`BUILD_MODE`** - Set to `prod` for production deployments

**Example `env.ilha` file**:
```bash
COMPOSE_PROJECT_NAME=myapp-feature-auth
SITE_DOMAIN=http://myapp-feature-auth.localhost
ALLOWED_HOSTS=localhost,127.0.0.1,myapp-feature-auth.localhost,*.localhost
USE_X_FORWARDED_HOST=True
DEBUG=True
```

**Note**: Projects should derive domain-dependent variables (like WebAuthn settings) from `SITE_DOMAIN` in their application code. See [WebAuthn Configuration](#webauthnauthentication-configuration) for examples.

## 5. Volume Configuration

### Named Volumes for Data Persistence

Use named Docker volumes (not bind mounts) for data that should be included in exports:

```yaml
services:
  web:
    volumes:
      - sqlite_data:/data  # ✅ Named volume - included in exports
      # - ./db.sqlite3:/app/db.sqlite3  # ❌ Bind mount - NOT included

volumes:
  sqlite_data:  # ✅ Will be backed up and restored
```

**Why**: ilha's export/import functionality captures Docker volumes. Bind mounts are not part of Docker volumes and won't be included in backups.

### SQLite Best Practice

For SQLite databases, mount a named volume and configure your app to use it:

```yaml
services:
  web:
    volumes:
      - sqlite_data:/data
    environment:
      - SQLITE_PATH=/data/db.sqlite3
```

```python
# settings.py
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.getenv('SQLITE_PATH', '/app/db.sqlite3'),
    }
}
```

## 6. Network Configuration

### Service Networks

Web services need both networks:
- `default` - For database/redis access
- `ilha_caddy_proxy` - For Caddy reverse proxy routing

```yaml
services:
  backend:
    networks:
      - default
      - ilha_caddy_proxy
```

**Why**: 
- `default` network enables communication with database/redis containers
- `ilha_caddy_proxy` is the external network that Caddy monitors for routing

### Database/Redis Services

Database and Redis services typically only need the `default` network:

```yaml
services:
  db:
    # No networks specified - uses default network
    expose:
      - '5432'
```

## 7. Complete Example: docker-compose.yml

Here's a complete example combining all best practices:

```yaml
version: '3.8'

services:
  db:
    image: postgres:15-alpine
    volumes:
      - postgres_data:/var/lib/postgresql/data
    expose:
      - '5432'
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5

  backend:
    build: ./backend
    command: sh -c "python manage.py migrate && uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --reload"
    volumes:
      - ./backend:/app
    expose:
      - "8000"
    labels:
      - "caddy.proxy=${COMPOSE_PROJECT_NAME:-myapp}.localhost"
      - "caddy.proxy.path=/api/*"
      - "caddy.proxy.reverse_proxy=${COMPOSE_PROJECT_NAME:-myapp}-backend:8000"
    networks:
      - default
      - ilha_caddy_proxy
    environment:
      - DEBUG=${DEBUG:-True}
      - DB_HOST=db
      - DB_PORT=5432
      - ALLOWED_HOSTS=${ALLOWED_HOSTS:-localhost,127.0.0.1}
      - CORS_ALLOWED_ORIGINS=${CORS_ALLOWED_ORIGINS:-http://localhost:5173}
      - SITE_DOMAIN=${SITE_DOMAIN:-http://localhost:8000}
      - USE_X_FORWARDED_HOST=${USE_X_FORWARDED_HOST:-False}
    depends_on:
      db:
        condition: service_healthy

  frontend:
    build:
      context: ./frontend
      target: ${BUILD_MODE:-dev}
    volumes:
      - ./frontend:/app
      - /app/node_modules
    expose:
      - "5173"
    labels:
      - "caddy.proxy=${COMPOSE_PROJECT_NAME:-myapp}.localhost"
      - "caddy.proxy.reverse_proxy=${COMPOSE_PROJECT_NAME:-myapp}-frontend:5173"
    networks:
      - default
      - ilha_caddy_proxy
    environment:
      - BUILD_MODE=${BUILD_MODE:-dev}
      - VITE_API_URL=${VITE_API_URL:-}
      - VITE_ALLOWED_HOSTS=${VITE_ALLOWED_HOSTS:-localhost,127.0.0.1,*.localhost}
    depends_on:
      - backend

volumes:
  postgres_data:
```

## 8. Quick Checklist

Before running `ilha setup`, ensure:

- [ ] Services use `expose` instead of `ports` (except for health checks)
- [ ] Environment variables use `${VAR:-default}` substitution pattern
- [ ] Caddy labels added to web services needing external routing
- [ ] Web services connected to both `default` and `ilha_caddy_proxy` networks
- [ ] Backend settings read from environment variables (ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, etc.)
- [ ] Frontend API client uses `window.location.origin/api` for production
- [ ] Vite configured to allow ilha subdomains (`*.localhost`)
- [ ] Named volumes used for data persistence (not bind mounts for data files)
- [ ] Multi-stage builds support `BUILD_MODE` environment variable
- [ ] Reverse proxy headers configured (`USE_X_FORWARDED_HOST`, `SECURE_PROXY_SSL_HEADER`)

## 9. Testing Your Setup

After setup, verify your configuration:

```bash
# 1. Run setup
ilha setup

# 2. Start proxy
ilha start-proxy

# 3. Create and start worktree
ilha create test-branch
ilha test-branch up -d

# 4. Check environment variables are set correctly
ilha test-branch exec backend env | grep SITE_DOMAIN
ilha test-branch exec backend env | grep ALLOWED_HOSTS

# 5. Check Caddy labels
ilha test-branch exec backend env | grep COMPOSE_PROJECT_NAME

# 6. Access application
open http://myproject-test-branch.localhost

# 7. Test API endpoints
curl http://myproject-test-branch.localhost/api/health/

# 8. Check logs for errors
ilha test-branch logs backend
ilha test-branch logs frontend
```

### Common Issues and Solutions

**Issue**: `DisallowedHost` error in Django
- **Solution**: Ensure `ALLOWED_HOSTS` uses environment variable substitution and includes worktree domain

**Issue**: CORS errors in browser
- **Solution**: Configure `CORS_ALLOWED_ORIGINS` to include worktree domain

**Issue**: WebAuthn authentication fails
- **Solution**: Derive WebAuthn variables from `SITE_DOMAIN` in your application code. See [WebAuthn Configuration](#webauthnauthentication-configuration) for implementation examples.

**Issue**: API calls fail with 404
- **Solution**: Verify Caddy labels are correct and backend service is on `ilha_caddy_proxy` network

**Issue**: Frontend can't connect to backend
- **Solution**: Use `window.location.origin/api` instead of hardcoded localhost URLs

## Additional Resources

- [Architecture Guide](ARCHITECTURE.md) - Detailed system architecture
- [README](../README.md) - Quick start and command reference
- [Troubleshooting Guide](TROUBLESHOOTING.md) - Common issues and solutions

---

*This guide is based on real-world experience configuring projects for ilha. If you encounter issues not covered here, please open an issue or contribute improvements.*

