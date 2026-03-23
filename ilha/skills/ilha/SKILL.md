---
name: ilha
description: Manages Git worktrees with isolated Docker environments (Caddy, Compose, volumes). Supports setup, worktree lifecycle, packages, and DigitalOcean droplet create/push/destroy. Use when the user mentions ilha, worktrees, worktree isolation, ilha up, ilha down, droplet push, ilha packages, ilha setup, or when running Django or container commands in an ilha-based project.
---

# ilha

Concise guidance for working with the ilha CLI: worktree-isolated Docker environments, commands, and conventions.

## When to use this skill

- User is in the ilha repo (contributing, debugging, adding commands).
- User is in a project that uses ilha (e.g. `ilha <branch> up`, `ilha <branch> exec web ...`) and asks about environments, deploy, or worktrees.
- User mentions: worktree, droplet, push, packages export/import, Caddy, or `.ilha/` config.

## Essential concepts

- **Worktree-name-first:** `ilha <worktree_name> up|down|exec|logs|...`. Shorthand `ilha up` / `ilha down` auto-detect worktree from current directory or current git branch.
- **Paths:** Project root has `.ilha/` (config, compose template). Each worktree lives under `worktrees/<branch>/` with its own `.ilha/` (fractal). Runtime compose is always `worktrees/<branch>/.ilha/docker-compose.worktree.yml` — ilha never uses the root `docker-compose.yml` at runtime.
- **Volume naming:** `{branch_name}_{volume_type}` (e.g. `feature-auth_postgres_data`).

## Command quick reference

| Area | Commands |
|------|----------|
| Setup | `ilha setup [--monkey-patch]` |
| Lifecycle | `ilha create <branch>`, `ilha <branch> up -d`, `ilha <branch> down`, `ilha remove <branch>`, `ilha delete <branch>`, `ilha list` |
| Compose | `ilha <branch> exec <service> <cmd>`, `ilha <branch> logs <service>`, `ilha <branch> ps`, `ilha <branch> run --rm <service> <cmd>` |
| Django | `ilha <branch> exec web python manage.py migrate`, `ilha <branch> exec web python manage.py test`, `ilha <branch> exec web python manage.py shell`, `ilha <branch> exec web python manage.py collectstatic` |
| Droplet | `ilha droplet create [branch] [--create-only]`, `ilha droplet push [branch] <scp_target_or_droplet>`, `ilha droplet list`, `ilha droplet destroy <id>`, `ilha droplet regions` |
| Packages | `ilha packages export <branch>`, `ilha packages import <file> [--standalone] [--domain|--ip]` |
| Proxy | `ilha start-proxy`, `ilha stop-proxy` |

For full options and deployment (push, DNS, VPC, code-only), see [references/commands.md](references/commands.md) or the repo [README](../../../README.md).

## Conventions and pitfalls

- **Exact branch names:** Delete/remove check worktrees, branches, and docker volumes for exact matches only. Names are case-sensitive.
- **Compose file:** Only the worktree compose file is used at runtime; no fallback to root `docker-compose.yml`.
- **Django:** For reverse-proxy settings (ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, etc.), run `ilha setup --monkey-patch` to append a safe snippet to `settings.py`.

## References

- **Full command reference and deployment:** [README](../../../README.md) (repo root).
- **Architecture (agentic coders):** [documentation/ARCHITECTURE.md](../../../documentation/ARCHITECTURE.md).
- **Per-project quick reference:** In a project using ilha, see `.ilha/README.md` or [config/README.md](../../config/README.md).
