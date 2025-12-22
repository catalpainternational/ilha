# ilha Troubleshooting Guide

## Port Already Allocated (e.g., `Bind for 0.0.0.0:5432 failed`)

### Symptom
- `docker compose up` fails with `driver failed programming external connectivity` or `Bind for 0.0.0.0:5432 failed: port is already allocated`
- Only one ilha worktree starts successfully; additional worktrees fail to start `db`, `redis`, or `web` containers
- Host ports 5432/6379/8000 randomly collide with other local stacks

### Root Cause
Older `docker-compose.worktree.yml` files published fixed host port mappings such as `5432:5432` and `8000:8000`. When multiple worktrees reuse those host ports, Docker cannot bind the second container, producing the `port is already allocated` error. This was masked previously because the compose override relied on `expose` only; once explicit `ports` blocks were added, conflicts surfaced.

### Solution
1. **Use env-driven port mappings**  
   Update `.ilha/docker-compose.worktree.yml` so every service that publishes a host port uses the ilha placeholders:
   ```yaml
   services:
     db:
       expose:
         - "5432"
       ports:
         - "${ilha_DB_HOST_PORT:-0}:5432"
     web:
       expose:
         - "8000"
       ports:
         - "${ilha_WEB_HOST_PORT:-0}:8000"
     redis:
       ports:
         - "${ilha_REDIS_HOST_PORT:-0}:6379"
   ```
   The `:-0` default lets Docker pick a random free port when running against legacy environments that are missing the new variables.

2. **Let ilha assign unique host ports per worktree**  
   The environment manager now writes three new entries to each worktree's `.ilha/env.ilha`:
   ```
   ilha_DB_HOST_PORT=58xxx
   ilha_REDIS_HOST_PORT=57xxx
   ilha_WEB_HOST_PORT=58xxx
   ```
   These values are selected from high-numbered ranges to avoid system services and are guaranteed to be unique across all existing worktrees. When you create a new worktree (`ilha create <branch>`), the CLI auto-assigns and logs the chosen ports.

3. **Retrofit existing worktrees**  
   Delete and recreate the worktree (`ilha delete <branch>` followed by `ilha create <branch>`) so the new env file is generated with unique ports. If recreation is not possible, manually edit `<worktree>/.ilha/env.ilha` and add unique values for `ilha_DB_HOST_PORT`, `ilha_REDIS_HOST_PORT`, and `ilha_WEB_HOST_PORT`.

### Verification
- `docker compose -p <project>-<branch> ps` shows each service running with distinct published ports.
- `docker compose port db 5432` (or `web 8000`, `redis 6379`) matches the values recorded in `.ilha/env.ilha`.
- Bringing up multiple worktrees simultaneously no longer produces `port is already allocated`.

## Workers Not Processing Jobs / Database Connection Failures

### Symptom
- RQ workers fail to start or get stuck in restart loops
- Error messages like: `could not translate host name "db" to address`
- Workers cannot connect to database/Redis on central droplet
- Jobs remain queued in Redis but are never processed

### Root Cause
When using ilha with a central droplet architecture (worker servers connecting to hub server's database/Redis), the worker's `.env` file may contain hardcoded service names (`DB_HOST=db`, `REDIS_HOST=redis`) that override the Docker environment variables set from `.ilha/env.ilha`.

Additionally, if your project's `manage.py` uses `load_dotenv(override=True)`, it will override Docker environment variables with values from the `.env` file.

### Solution

#### 1. Ensure Docker Compose Uses Variable Substitution
In `.ilha/docker-compose.worktree.yml`, use variable substitution instead of hardcoded values:

```yaml
environment:
  DB_HOST: ${DB_HOST:-db}           # Not: DB_HOST: db
  REDIS_HOST: ${REDIS_HOST:-redis}  # Not: REDIS_HOST: redis
  DATABASE_URL: postgres://${POSTGRES_USER:-biuser}:${POSTGRES_PASSWORD:-bipassword}@${DB_HOST:-db}:5432/business_intelligence
```

This allows `.ilha/env.ilha` to override the defaults.

#### 2. Update manage.py to Honor Docker Environment Variables
If your `manage.py` has `override=True`, remove it to preserve Docker environment variables:

**Before:**
```python
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'), override=True)
```

**After:**
```python
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))  # override=False by default
```

This ensures that Docker environment variables (set from `.ilha/env.ilha`) take precedence over `.env` file values.

#### 3. Verify .ilha/env.ilha Contains Correct Values
When `ilha droplet create` is run with `--central-droplet-name`, it should write:

```bash
DB_HOST=10.116.0.13      # VPC IP of central droplet
REDIS_HOST=10.116.0.13   # VPC IP of central droplet
```

Check that these values are present in `.ilha/env.ilha` on the worker server.

### Verification

After making these changes, restart the worker service:

```bash
docker restart <container-name>
```

Check logs to confirm:
- Database connection succeeds
- Workers start successfully
- Jobs begin processing from the queue

### Environment Variable Precedence

Docker Compose loads environment variables in this order (later overrides earlier):
1. `.env` file
2. `.ilha/env.ilha` (overrides `.env`)
3. `environment` section in docker-compose.yml (uses variable substitution from above)

Python's `load_dotenv(override=False)` respects existing environment variables, so Docker-set values are preserved.

## Vite Dev Server Blocking Production Domain (Outstanding Issue - Needs Review)

### Symptom
- When accessing a deployed site via production domain (e.g., `https://boards.are.temporarily.fun`), Vite dev server returns:
  ```
  Blocked request. This host ("boards.are.temporarily.fun") is not allowed.
  To allow this host, add "boards.are.temporarily.fun" to `server.allowedHosts` in vite.config.js.
  ```
- Frontend container is running but requests are blocked by Vite's security feature
- Issue occurs on production deployments when using real domains (not localhost)

### Root Cause
Vite's dev server has a built-in security feature that blocks requests from hosts not explicitly listed in `server.allowedHosts`. By default, Vite only allows `localhost` and `127.0.0.1`. When deploying to a production domain, the domain must be added to the allowed hosts list, otherwise Vite blocks all requests.

This is a security feature to prevent DNS rebinding attacks, but it requires explicit configuration for production deployments.

### Solution (Implemented)

#### 1. Update `vite.config.js` to Read from Environment Variable
Add `allowedHosts` configuration that reads from `VITE_ALLOWED_HOSTS` environment variable:

```javascript
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 8000,
    // Allow requests from domains specified in VITE_ALLOWED_HOSTS env var
    // Format: comma-separated list (e.g., "example.com,*.example.com")
    allowedHosts: process.env.VITE_ALLOWED_HOSTS?.split(',').map(h => h.trim()).filter(Boolean) || [],
    // ... rest of config
  },
})
```

#### 2. Update `docker-compose.worktree.yml` to Pass Environment Variable
Add `VITE_ALLOWED_HOSTS` to the frontend service environment:

```yaml
frontend:
  environment:
    - VITE_API_URL=http://${COMPOSE_PROJECT_NAME}.localhost/api
    - VITE_ALLOWED_HOSTS=${VITE_ALLOWED_HOSTS:-localhost,127.0.0.1,*.localhost}
    # ... other env vars
```

#### 3. ilha Automatically Sets `VITE_ALLOWED_HOSTS` on Deployment
When using `ilha push --domain <domain>`, ilha's `_update_remote_env_file()` function now automatically sets:

```
VITE_ALLOWED_HOSTS=<domain>,*.<base-domain>,localhost,127.0.0.1
```

For example, for domain `boards.are.temporarily.fun`:
```
VITE_ALLOWED_HOSTS=boards.are.temporarily.fun,*.are.temporarily.fun,localhost,127.0.0.1
```

### Verification
1. Check that `VITE_ALLOWED_HOSTS` is set in `.ilha/env.ilha` on the remote server
2. Verify the environment variable is passed to the frontend container:
   ```bash
   docker exec <frontend-container> env | grep VITE_ALLOWED_HOSTS
   ```
3. Confirm `vite.config.js` includes the `allowedHosts` configuration
4. Access the production domain - it should load without the "Blocked request" error

### Outstanding Issues / Needs Review

1. **Backward Compatibility**: The solution defaults to `localhost,127.0.0.1,*.localhost` when `VITE_ALLOWED_HOSTS` is not set, which works for local development. However, this should be verified across all project types.

2. **Production vs Development**: The current solution works for Vite dev server. For production builds, this issue shouldn't occur since production builds don't use the dev server. Consider documenting when this applies (dev mode only).

3. **Wildcard Domain Support**: The solution includes wildcard domains (`*.are.temporarily.fun`), but Vite's `allowedHosts` may have specific requirements for wildcard matching. This needs verification.

4. **Template Updates**: The base `docker-compose.worktree.yml` template in ilha should be updated to include `VITE_ALLOWED_HOSTS` by default for all new projects.

5. **Documentation**: This should be added to the main deployment documentation, not just troubleshooting, as it's a required configuration for production deployments with Vite.

### Related Files Modified
- `/Users/ders/projects/ilha/ilha/commands/push.py` - Added `VITE_ALLOWED_HOSTS` to `_update_remote_env_file()` and import script
- Project `vite.config.js` - Added `allowedHosts` configuration
- Project `.ilha/docker-compose.worktree.yml` - Added `VITE_ALLOWED_HOSTS` environment variable

### Future Improvements
- Consider adding a ilha template/example for Vite projects that includes this configuration
- Add validation/warning if `VITE_ALLOWED_HOSTS` is missing when deploying with a domain
- Consider supporting multiple domains in a single deployment

