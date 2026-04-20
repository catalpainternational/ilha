# Staging droplet login 403 and DB fixes – what was required

After creating a staging droplet with `ilha droplet deploy` for `staging.bower.bid` on the groq branch, login returned **403** and the DB container failed to start. Below is what was required to fix it and how ilha should be updated so this does not happen again.

## 1. 403 on login (Django CSRF)

**Cause**

- **SITE_DOMAIN** was set with protocol: `https://staging.bower.bid`. Django then appended `https://{SITE_DOMAIN}` to `CSRF_TRUSTED_ORIGINS`, producing `https://https://staging.bower.bid`, which does not match the browser `Origin: https://staging.bower.bid`, so CSRF failed and Django returned 403.
- **CSRF_TRUSTED_ORIGINS** was written **space-separated** (`https://staging.bower.bid http://staging.bower.bid ...`). Django’s settings use `.split(',')`, so the whole string was one invalid entry and the real origin was never trusted.

**Manual fixes applied on droplet**

- In `.ilha/env.ilha` on the server:
  - `SITE_DOMAIN=https://staging.bower.bid` → `SITE_DOMAIN=staging.bower.bid` (bare domain, no protocol).
  - `CSRF_TRUSTED_ORIGINS=https://staging.bower.bid http://staging.bower.bid ...` → `CSRF_TRUSTED_ORIGINS=https://staging.bower.bid,http://staging.bower.bid` (comma-separated).
- Recreated the web container so it picked up the new env.

**Required ilha change**

- When generating or updating env for a **production/staging domain** (deploy, put, domain overrides):
  - Set **SITE_DOMAIN** to the **bare domain** (e.g. `staging.bower.bid`), no `http://` or `https://`.
  - Set **CSRF_TRUSTED_ORIGINS** to a **comma-separated** list (e.g. `https://staging.bower.bid,http://staging.bower.bid`). Use the same list in both the “generate env with domain” path and the “apply domain overrides” path (DRY).

**Update (slim deploy):** `SlimDeployManager._build_merged_env` (ilha) now writes bare **SITE_DOMAIN** for domain and IP deploys, matching the above. See `documentation/SLIM_DEPLOY_SMOKE_CHECK.md`.

## 2. DB container failing (postgresql.conf)

**Cause**

- On the server, **PROJECT_ROOT** in `env.ilha` was set to the **project root** (e.g. `/root/business_intelligence`). The compose file mounts `${PROJECT_ROOT}/postgres-config/...` for the DB. The actual worktree (and its `postgres-config/`) lives at `.../worktrees/groq/`, so the DB was using the wrong path and failed (e.g. “configuration file contains errors” or missing files).

**Manual fix applied on droplet**

- In `.ilha/env.ilha`: `PROJECT_ROOT=/root/business_intelligence` → `PROJECT_ROOT=/root/business_intelligence/worktrees/groq`.
- Recreated the db container so it used the correct config path.

**Required ilha change**

- For **standalone** (server) import, when updating `env.ilha` on the target server, set **PROJECT_ROOT** to the **worktree directory** on the server (e.g. `{target_directory}/worktrees/{branch_name}`), not the project root. That way `${PROJECT_ROOT}/postgres-config` and other paths resolve to the extracted worktree.

## 3. Superuser / login (one-time, optional)

- The existing `admin` user had `is_active=False`; login then failed with “email and/or password … not correct” (not 403). We set `is_active=True` and reset the password via Django shell on the server. This is a one-time fix; ilha does not need to create or activate users.

---

## Summary for ilha

| What | Where | Change |
|------|--------|--------|
| SITE_DOMAIN (production) | `environment_manager.py` | Use bare domain (no protocol) in both `_generate_env_compose_with_domain` and `apply_domain_overrides`. |
| CSRF_TRUSTED_ORIGINS | Same | Use comma-separated list in both code paths (DRY). |
| PROJECT_ROOT (standalone import) | `package_manager.py` | Call `update_project_root(worktree_path, worktree_path)` so PROJECT_ROOT is the worktree path on the server, not `target_directory`. |

Implementing these in ilha ensures the 403 and DB config errors do not recur for new droplet/create or push + server-import deployments.
