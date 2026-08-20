"""
Setup command for ilha CLI.

This module provides the setup command that initializes ilha for a project.
"""

import re
import shutil
import yaml
from pathlib import Path
from typing import Optional, Dict, Any, List

from ..config.settings import ILHA_DIR, get_worktree_dir
from ..utils.logging import log_info, log_success, log_warning, log_error, log_step
from ..utils.validation import check_prerequisites
from ..utils.file_utils import (
    prompt_yes_no,
    add_to_gitignore,
    add_to_cursorignore,
    add_to_cursorindexignore,
    check_gitignore_entry,
    replace_gitignore_entry,
    remove_gitignore_entry,
)



def _django_spa_path_routes(
    project_root: Path, user_compose: Dict[str, Any]
) -> Optional[Dict[str, List[str]]]:
    """Which paths the backend owns, for a Django+SPA project. None otherwise.

    Read from the very same ``deploy:`` block the push pipeline uses, so a
    worktree and the droplet cannot disagree about who serves ``/admin`` or
    ``/slack/*``. Returns None whenever the project is not two-tier or has no
    api_paths — a single routable service needs no split, and inventing one
    would break it.

    Never raises. A project that cannot be introspected gets the old
    unconstrained behaviour rather than a failed setup.
    """
    try:
        from .push.deploy_service_config import load_deploy_service_config

        config = load_deploy_service_config(project_root)
        names = set((user_compose.get("services") or {}).keys())
        if (
            config.frontend_service
            and config.api_paths
            and config.frontend_service in names
            and config.web_service in names
        ):
            return {
                config.web_service: list(config.api_paths),
                # Explicitly empty: the catch-all, and the thing that makes the
                # backend's paths win by being sorted ahead of it.
                config.frontend_service: [],
            }
    except Exception as exc:  # noqa: BLE001 — routing is a nicety, setup is not
        log_warning(f"Could not work out API path routing: {exc}")
    return None


class SetupManager:
    """Manages project setup for ilha CLI."""
    
    def __init__(self, project_root: Optional[Path] = None):
        """Initialize setup manager.
        
        Args:
            project_root: Project root directory. If None, uses current working directory.
        """
        self.project_root = project_root or Path.cwd()
        self.ilha_dir = self.project_root / ILHA_DIR
    
    def _get_examples_dir(self) -> Path:
        """Get path to examples directory in current project.
        
        Returns:
            Path to .ilha directory where example files are stored
        """
        # Example files are stored directly in .ilha directory
        return self.ilha_dir
    
    def _generate_config_dict(
        self,
        project_name: str,
        *,
        compose_file: Optional[Path] = None,
        compose_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate config dictionary from project name and compose file.
        
        Extracts the logic currently in _create_config_file() to make it reusable.
        This method is called by both template-based and from-scratch creation paths.
        
        Args:
            project_name: Project name
            compose_file: Path to docker-compose.yml (use when no ``compose_data``)
            compose_data: Parsed compose dict (e.g. Ilha default static template)
            
        Returns:
            Dictionary with complete config structure
        """
        if compose_data is None:
            if compose_file is None:
                raise ValueError("compose_file or compose_data is required")
            with open(compose_file) as f:
                compose_data = yaml.safe_load(f)
        
        services = {}
        volumes = []
        
        if 'services' in compose_data:
            for service_name in compose_data['services'].keys():
                services[service_name] = {
                    "container_name_template": f"${{COMPOSE_PROJECT_NAME}}-{service_name}"
                }
        
        if 'volumes' in compose_data:
            volumes = list(compose_data['volumes'].keys())
        
        config = {
            'project_name': project_name,
            'caddy_network': 'ilha_caddy_proxy',
            'worktree_dir': 'worktrees',
            'services': services,
            'volumes': volumes,
            'environment': {
                'DEBUG': 'True',
                'ALLOWED_HOSTS': 'localhost,127.0.0.1,*.localhost,web',
            }
        }
        
        return config
    
    def _generate_env_ilha_content(self, branch_name: Optional[str] = None) -> str:
        """Generate env.ilha content for project root.

        Reuses generate_env_compose_content() from settings.py for consistency.
        Detects Django footprint against ``self.project_root`` so Django-only
        siblings (ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, USE_X_FORWARDED_HOST)
        are emitted iff the project actually uses Django.
        """
        from ..config.settings import generate_env_compose_content, resolve_default_branch
        from ..utils.project_footprint import detect_project_footprint
        if branch_name is None:
            branch_name = resolve_default_branch(self.project_root)
        has_django = detect_project_footprint(self.project_root).has_django
        return generate_env_compose_content(branch_name, has_django=has_django)

    def setup_project(self, project_name: Optional[str] = None, domain: Optional[str] = None, ip: Optional[str] = None, non_interactive: bool = False, monkey_patch: bool = False, force: bool = False) -> bool:
        """Initialize ilha for a project.
        
        Args:
            project_name: Optional project name (defaults to directory name)
            domain: Optional domain override (subdomain.domain.tld) for production/staging
            ip: Optional IP address for HTTP-only deployments
            non_interactive: Run in non-interactive mode (auto-accept defaults)
            monkey_patch: Auto-patch Django settings.py if detected
            force: Force overwrite existing .ilha configuration files
        """
        log_step("Setting up ilha for this project")
        if domain:
            log_info(f"Using domain override: {domain}")

        # Fractal dispatch: when invoked inside an existing worktree, configure
        # against the parent project rather than treating cwd as a new project
        # root. Preserves the fractal contract (worktree inherits parent
        # identity; env.ilha reflects the actual git branch).
        from ..utils.path_utils import detect_execution_context
        _, _, is_worktree = detect_execution_context()
        if is_worktree:
            return self._setup_worktree(force=force, non_interactive=non_interactive)

        # 0. Check if .ilha already exists and prompt for overwrite
        force_overwrite = force
        if self.ilha_dir.exists():
            if force:
                log_info("--force flag set, overwriting existing .ilha configuration...")
                force_overwrite = True
            elif non_interactive:
                # Don't bail: refresh the ilha-owned overlay from the (possibly
                # hand-edited) manifest while preserving the user-owned config.yml
                # and env.ilha. This makes `ilha setup` idempotent and lets a
                # manifest edit take effect without an interactive prompt. Use
                # --force to also re-derive the manifest from detection.
                log_info(
                    ".ilha already exists; refreshing the overlay from the existing "
                    "manifest and keeping config.yml / env.ilha (use --force to reset)."
                )
                force_overwrite = False
            else:
                log_warning(f"The directory {self.ilha_dir} already exists.")
                if not prompt_yes_no("Overwrite existing .ilha config (config.yml) and env override (env.ilha)?", default=False):
                    log_info("Keeping existing config.yml and env.ilha; updating worktree compose only.")
                    force_overwrite = False
                else:
                    force_overwrite = True
                    log_info("Overwriting .ilha config and env.ilha...")
        
        # 0. Check prerequisites first
        check_prerequisites(project_root=self.project_root)
        
        # 0.5. Validate PROJECT_ROOT environment variable
        if not self._validate_project_root():
            return False
        
        # 1. Create .ilha directory structure
        if not self._create_ilha_directory():
            return False

        # 2. Require an existing docker-compose.yml. Per ilha's charter, the
        # user owns their compose; ilha never generates or mutates it.
        compose_file = self.detect_docker_compose()
        if not compose_file:
            log_error(
                "No Docker Compose file found in project root. "
                "ilha requires an existing docker-compose.yml (and Dockerfile, "
                "if your services build from source). Add one and re-run `ilha setup`."
            )
            return False

        # 2.5 Ratify the project manifest (.ilha/ilha.yml). This is the single
        # authority for which services are web-routable (and their port), the db,
        # and the volumes to clone. Detection proposes it; the user ratifies/edits
        # it; the overlay is generated from it (no silent per-command heuristics).
        manifest = self._ratify_manifest(
            compose_file, project_name, force=force_overwrite, non_interactive=non_interactive
        )
        if manifest is None:
            # No routable service and the user declined to proceed.
            return False

        # 3. Generate the compose overlay (.ilha/docker-compose.overlay.yml).
        if not self._write_compose_overlay(
            compose_file, routable=manifest.routable_ports(), domain=domain, ip=ip
        ):
            return False

        # 4. Create config.yml
        if not self._create_config_file(
            project_name,
            compose_file=compose_file,
            compose_data=None,
            overwrite=force_overwrite,
        ):
            return False
        
        # 5. Create template env.ilha for project root
        if not self._create_template_env_ilha(overwrite=force_overwrite):
            log_warning("Failed to create template env.ilha")
            # Don't fail setup, this is optional
        
        if non_interactive:
            # Non-interactive: silently ensure worktrees/ in .gitignore, skip completion
            try:
                from ..utils.file_utils import append_line_to_file
                gitignore_path = self.project_root / '.gitignore'
                append_line_to_file(gitignore_path, 'worktrees/')
                log_success("Added worktrees/ to .gitignore")
            except Exception:
                log_warning("Failed to update .gitignore silently in non-interactive mode")
        else:
            # 7. Ask user about adding .ilha to .gitignore
            if not self._handle_gitignore_setup():
                log_warning("Gitignore setup was skipped or failed")
            
            # 8. Ask user about installing shell completion
            if not self._handle_completion_setup():
                log_warning("Completion setup was skipped or failed")
        
        # 9. Django-specific guidance and optional patching
        try:
            self._django_post_setup_checks(monkey_patch=monkey_patch, non_interactive=non_interactive)
        except Exception as e:
            log_warning(f"Django compatibility checks skipped: {e}")
        
        log_success("ilha setup completed successfully!")
        log_info(f"Configuration: {self.ilha_dir}/config.yml")
        log_info("Next: ilha start")
        return True

    def _setup_worktree(self, *, force: bool, non_interactive: bool) -> bool:
        """Configure the current worktree as a fractal child of its parent.

        Inherits identity (config.yml, compose overlay, Caddyfile, README) from
        the parent's ``.ilha/`` via the same primitives ``ilha up`` uses, then
        regenerates ``env.ilha`` for the worktree's actual git branch. This
        keeps every fractal-write path in agreement with the parent and avoids
        the silent ``cwd().name`` and hardcoded-``master`` defaults that bite
        when ``ilha setup`` is re-run inside a worktree.
        """
        import shutil
        from ..utils.path_utils import get_parent_project_root, get_worktree_branch_name
        from ..core.worktree_orchestrator import WorktreeOrchestrator
        from ..core.environment_manager import EnvironmentManager

        worktree_path = self.project_root  # cwd, since SetupManager defaults to it

        parent_root = get_parent_project_root()
        if not parent_root:
            log_error(
                f"No parent ilha project found above {worktree_path}. "
                f"Run `ilha setup` at the parent project root first."
            )
            return False

        parent_config = parent_root / ILHA_DIR / "config.yml"
        if not parent_config.exists():
            log_error(
                f"Parent at {parent_root} has no .ilha/config.yml. "
                f"Run `ilha setup` there first, then re-run from this worktree."
            )
            return False

        branch_name = get_worktree_branch_name(worktree_path)
        if not branch_name:
            log_error(f"Could not determine git branch for worktree at {worktree_path}")
            return False

        log_info(f"Configuring worktree '{branch_name}' as fractal child of {parent_root}")

        target_ilha = worktree_path / ILHA_DIR
        existing_env_ilha = target_ilha / "env.ilha"

        # Snapshot the worktree's own env.ilha (if any) before any rmtree, so
        # branch-specific user overrides (DROPLET_DOMAIN, PUSH_*, branch-local
        # secrets) survive a --force re-copy from parent.
        from ..utils.env_loader import load_env_file
        worktree_env_snapshot: Dict[str, str] = (
            load_env_file(existing_env_ilha) if existing_env_ilha.exists() else {}
        )

        if target_ilha.exists():
            if force:
                log_info("--force: removing existing worktree .ilha before re-copying from parent")
                shutil.rmtree(target_ilha)
            elif non_interactive:
                log_info(f"{target_ilha} exists; keeping it (refreshing env.ilha only)")
            else:
                if prompt_yes_no(
                    f"Overwrite {target_ilha} with a fresh copy from {parent_root}?",
                    default=False,
                ):
                    shutil.rmtree(target_ilha)
                else:
                    log_info("Keeping existing .ilha; refreshing env.ilha only")

        orchestrator = WorktreeOrchestrator(project_root=parent_root)
        if not target_ilha.exists():
            if not orchestrator._copy_ilha_to_worktree(worktree_path):
                log_error("Failed to copy parent .ilha into worktree")
                return False
            log_success(f"Copied parent .ilha to {target_ilha}")

        # Refresh env.ilha for the actual branch.
        # Merge order (later wins):
        #   1. parent's env.ilha    — shared baseline (API keys, global secrets)
        #   2. worktree's snapshot  — branch-specific user overrides
        #   3. ilha-managed keys    — COMPOSE_PROJECT_NAME, SITE_DOMAIN, ALLOWED_HOSTS, ...
        env_manager = EnvironmentManager(project_root=parent_root)
        new_content = env_manager._generate_env_compose_content(branch_name)

        merged_env: Dict[str, str] = {}
        if existing_env_ilha.exists():
            # After _copy_ilha_to_worktree, this file is the parent's env.ilha.
            merged_env.update(load_env_file(existing_env_ilha))
        merged_env.update(worktree_env_snapshot)
        ilha_managed = self._parse_env_content_to_dict(new_content)
        merged_env.update(ilha_managed)

        header_comment = next(
            (line.strip() for line in new_content.split("\n") if line.strip().startswith("#")),
            None,
        )
        try:
            self._write_env_dict_to_file(merged_env, existing_env_ilha, header_comment=header_comment)
            log_success(f"Refreshed {existing_env_ilha} ({len(merged_env)} vars)")
        except Exception as e:
            log_error(f"Failed to write worktree env.ilha: {e}")
            return False

        log_success("Worktree setup complete")
        log_info(f"Branch: {branch_name}")
        log_info(f"Parent: {parent_root}")
        log_info(f"Configuration: {target_ilha}/config.yml")
        return True

    def _django_post_setup_checks(self, monkey_patch: bool, non_interactive: bool) -> None:
        """If this looks like a Django project, validate env-driven settings and guide user.

        Checks for: ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, USE_X_FORWARDED_HOST, SECURE_PROXY_SSL_HEADER.
        Also checks SQLite configuration for Docker volume persistence.
        Optionally monkey-patches settings.py to read these from env vars.
        """
        project_root = self.project_root
        manage_py = project_root / "manage.py"
        if not manage_py.exists():
            return
        settings_path = self._locate_django_settings(project_root)
        if not settings_path:
            return

        # Existing proxy checks
        missing = self._find_missing_env_config(settings_path)
        if not missing:
            log_success("✓ Django settings appear to read required values from environment variables")
        else:
            log_warning("Django settings are not fully environment-driven for reverse proxy/Caddy:")
            for item in missing:
                log_warning(f"  - {item}")

            self._print_django_guidance()

            if monkey_patch:
                ok = self._monkey_patch_settings(settings_path)
                if ok:
                    log_success(f"Applied monkey patch to: {settings_path}")
                else:
                    log_warning("Monkey patch failed; please update settings manually using the guidance above.")
        
        # NEW: SQLite configuration check
        sqlite_config = self._detect_sqlite_configuration(settings_path)
        if sqlite_config["is_sqlite"]:
            self._check_sqlite_persistence(sqlite_config, settings_path, monkey_patch)

    def _locate_django_settings(self, root: Path) -> Optional[Path]:
        """Heuristically find Django settings.py."""
        # common locations: project_name/settings.py; any */settings.py near manage.py
        candidates = list(root.glob("**/settings.py"))
        # Prefer a settings.py in same tree as manage.py
        for cand in candidates:
            if (cand.parent / "__init__.py").exists():
                return cand
        return candidates[0] if candidates else None

    def _find_missing_env_config(self, settings_py: Path) -> list[str]:
        """Naively scan settings.py for env-driven configuration of required keys."""
        text = settings_py.read_text()
        missing: list[str] = []
        def lacks(patterns: list[str]) -> bool:
            return not any(p in text for p in patterns)

        # ALLOWED_HOSTS from env (comma-split or similar)
        if lacks(["ALLOWED_HOSTS = os.getenv", "ALLOWED_HOSTS=os.getenv", "env('ALLOWED_HOSTS'", "split(", "] for parsing"]):
            missing.append("ALLOWED_HOSTS from env (comma-separated)")
        # CSRF_TRUSTED_ORIGINS from env (space-separated)
        if lacks(["CSRF_TRUSTED_ORIGINS = os.getenv", "env('CSRF_TRUSTED_ORIGINS'", "split("]):
            missing.append("CSRF_TRUSTED_ORIGINS from env (space-separated)")
        # USE_X_FORWARDED_HOST from env
        if lacks(["USE_X_FORWARDED_HOST = os.getenv", "env('USE_X_FORWARDED_HOST'"]):
            missing.append("USE_X_FORWARDED_HOST from env (True/False)")
        # SECURE_PROXY_SSL_HEADER from env (tuple encoded as 'HTTP_X_FORWARDED_PROTO,https')
        if lacks(["SECURE_PROXY_SSL_HEADER =", "SECURE_PROXY_SSL_HEADER = tuple("]):
            missing.append("SECURE_PROXY_SSL_HEADER from env ('HTTP_X_FORWARDED_PROTO,https')")
        return missing

    def _print_django_guidance(self) -> None:
        log_info("")
        log_info("Guidance for Django behind Caddy (copy into settings.py):")
        log_info("""
import os

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = os.getenv("CSRF_TRUSTED_ORIGINS", "").split()

USE_X_FORWARDED_HOST = os.getenv("USE_X_FORWARDED_HOST", "False") == "True"
_hdr = os.getenv("SECURE_PROXY_SSL_HEADER")
if _hdr:
    SECURE_PROXY_SSL_HEADER = tuple(_hdr.split(",", 1))
        """)

    def _monkey_patch_settings(self, settings_py: Path) -> bool:
        try:
            text = settings_py.read_text()
            block = (
                "\n\n# ilha auto-added settings for reverse proxy and env-driven config\n"
                "import os\n"
                "ALLOWED_HOSTS = os.getenv(\"ALLOWED_HOSTS\", \"localhost,127.0.0.1\").split(\",\")\n"
                "CSRF_TRUSTED_ORIGINS = os.getenv(\"CSRF_TRUSTED_ORIGINS\", \"\").split()\n"
                "USE_X_FORWARDED_HOST = os.getenv(\"USE_X_FORWARDED_HOST\", \"False\") == \"True\"\n"
                "_hdr = os.getenv(\"SECURE_PROXY_SSL_HEADER\")\n"
                "if _hdr:\n    SECURE_PROXY_SSL_HEADER = tuple(_hdr.split(\",\", 1))\n"
            )
            if "ilha auto-added settings" in text:
                return True
            settings_py.write_text(text + block)
            return True
        except Exception as e:
            log_warning(f"Failed to patch settings.py: {e}")
            return False
    
    def _detect_sqlite_configuration(self, settings_path: Path) -> dict:
        """Detect SQLite database configuration in Django settings.
        
        Returns:
            dict with keys:
            - is_sqlite: bool - True if SQLite is detected
            - db_path: str - Database file path from settings (if found)
            - on_volume: bool - True if path appears to be on /data volume
            - needs_migration: bool - True if db is in source tree (bad)
        """
        try:
            with open(settings_path, 'r') as f:
                content = f.read()
            
            # Look for DATABASES configuration
            # Pattern 1: sqlite3 with ENGINE field
            sqlite_pattern = r"['\"]ENGINE['\"]:\s*['\"].*sqlite3['\"]"
            if not re.search(sqlite_pattern, content, re.IGNORECASE):
                return {"is_sqlite": False}
            
            # Pattern 2: Extract NAME field
            # Matches: 'NAME': 'path' or 'NAME': Path('path') or "NAME": os.path.join(...)
            name_patterns = [
                r"['\"]NAME['\"]:\s*['\"]([^'\"]+)['\"]",  # Simple string
                r"['\"]NAME['\"]:\s*Path\(['\"]([^'\"]+)['\"]",  # Path object
                r"['\"]NAME['\"]:\s*BASE_DIR\s*/\s*['\"]([^'\"]+)['\"]",  # BASE_DIR / 'path'
            ]
            
            db_path = None
            for pattern in name_patterns:
                match = re.search(pattern, content)
                if match:
                    db_path = match.group(1)
                    break
            
            # Check if path is on volume mount
            on_volume = False
            needs_migration = True
            
            if db_path:
                # Volume-mounted path patterns: /data/*, /var/lib/*, /db/*
                if db_path.startswith(('/data/', '/var/lib/', '/db/')):
                    on_volume = True
                    needs_migration = False
                # Relative paths or paths in BASE_DIR are bind-mounted (bad)
                elif db_path.startswith(('db/', 'database/', './', '../')) or 'BASE_DIR' in content:
                    on_volume = False
                    needs_migration = True
            
            return {
                "is_sqlite": True,
                "db_path": db_path,
                "on_volume": on_volume,
                "needs_migration": needs_migration
            }
        
        except Exception as e:
            log_warning(f"Failed to analyze SQLite configuration: {e}")
            return {"is_sqlite": False}
    
    def _check_sqlite_persistence(self, config: dict, settings_path: Path, monkey_patch: bool) -> None:
        """Check and guide SQLite persistence configuration."""
        if config["on_volume"]:
            log_success(f"✓ SQLite database is configured on Docker volume: {config['db_path']}")
            return
        
        if config["needs_migration"]:
            log_warning("\n" + "="*70)
            log_warning("SQLite Database Configuration Issue")
            log_warning("="*70)
            log_warning(f"Database path: {config.get('db_path', 'not detected')}")
            log_warning("")
            log_warning("⚠️  Your SQLite database is in the source code directory (bind mount).")
            log_warning("   This will cause problems with ilha worktrees:")
            log_warning("   • Database won't be copied to new worktrees")
            log_warning("   • Won't be included in ilha export/import packages")
            log_warning("   • All worktrees will share the same database file")
            log_warning("")
            log_warning("✅ Recommended Fix:")
            log_warning("   1. Store SQLite on a Docker volume (ilha already created sqlite_data volume)")
            log_warning("   2. Update Django settings:")
            log_warning("")
            log_warning("      # In settings.py")
            log_warning("      DATABASES = {")
            log_warning("          'default': {")
            log_warning("              'ENGINE': 'django.db.backends.sqlite3',")
            log_warning("              'NAME': os.getenv('SQLITE_PATH', '/data/db.sqlite3'),")
            log_warning("          }")
            log_warning("      }")
            log_warning("")
            log_warning("   3. docker-compose.yml mounts sqlite_data to /data automatically")
            log_warning("   4. Migrate your existing database:")
            log_warning("      cp db/db.sqlite3 /path/to/volume/db.sqlite3")
            log_warning("="*70 + "\n")
            
            if monkey_patch:
                log_info("Attempting to fix SQLite configuration...")
                self._monkey_patch_sqlite_settings(settings_path)
    
    def _monkey_patch_sqlite_settings(self, settings_path: Path) -> bool:
        """Monkey-patch settings.py to use environment-driven SQLite path on volume."""
        try:
            with open(settings_path, 'r') as f:
                content = f.read()
            
            # Check if already has environment-driven SQLite config
            if "os.getenv('SQLITE_PATH'" in content or 'os.environ.get("SQLITE_PATH"' in content:
                log_info("SQLite configuration already uses environment variables")
                return True
            
            patch = '''

# ilha: SQLite persistence on Docker volume
import os
from pathlib import Path
if 'sqlite3' in DATABASES.get('default', {}).get('ENGINE', ''):
    DATABASES['default']['NAME'] = os.getenv('SQLITE_PATH', '/data/db.sqlite3')
'''
            
            with open(settings_path, 'a') as f:
                f.write(patch)
            
            log_success("Applied SQLite volume configuration to settings.py")
            log_info("Set SQLITE_PATH=/data/db.sqlite3 in .env to use Docker volume")
            return True
        
        except Exception as e:
            log_warning(f"Failed to patch SQLite settings: {e}")
            return False
    
    def _create_ilha_directory(self) -> bool:
        """Create .ilha directory structure."""
        try:
            self.ilha_dir.mkdir(exist_ok=True)
            log_info(f"Created directory: {self.ilha_dir}")
            
            # Create worktree directory (default: ./worktrees)
            worktree_dir = get_worktree_dir()
            worktree_path = self.project_root / worktree_dir
            worktree_path.mkdir(exist_ok=True)
            log_info(f"Created worktree directory: {worktree_path}")
            
            return True
        except Exception as e:
            log_error(f"Failed to create directories: {e}")
            return False
    
    def detect_docker_compose(self) -> Optional[Path]:
        """Find compose files in project, supporting both legacy and v2 naming conventions."""
        from ..utils.file_utils import find_compose_files, prompt_compose_file_choice
        
        compose_files = find_compose_files(self.project_root)
        
        if not compose_files:
            return None
        
        if len(compose_files) == 1:
            compose_file = compose_files[0]
            log_info(f"Found {compose_file.name}: {compose_file}")
            return compose_file
        
        # Multiple files found, let user choose
        selected_file = prompt_compose_file_choice(compose_files)
        if selected_file:
            log_info(f"Using {selected_file.name}: {selected_file}")
            return selected_file
        
        return None
    
    def _ratify_manifest(
        self,
        compose_file: Path,
        project_name: Optional[str],
        *,
        force: bool,
        non_interactive: bool,
    ):
        """Load or propose the project manifest (.ilha/ilha.yml) and show it.

        Respects a hand-edited manifest: re-running setup without --force keeps
        the existing manifest rather than clobbering the user's edits. Returns
        the manifest, or None if there is no routable service and the user
        declines to proceed.
        """
        import yaml

        from ..config.manifest import load_manifest, write_manifest, manifest_from_detection
        from ..utils.project_footprint import detect_project_footprint

        try:
            user_compose = yaml.safe_load(compose_file.read_text()) or {}
        except Exception as e:
            log_error(f"Failed to read compose file: {e}")
            return None

        try:
            existing = load_manifest(self.project_root)
        except Exception as e:
            log_warning(f"Existing .ilha/ilha.yml is invalid ({e}); regenerating from detection.")
            existing = None

        if existing is not None and not force:
            log_info("Using existing .ilha/ilha.yml (edit it to change routing/services).")
            manifest = existing
        else:
            resolved_name = project_name or self.project_root.name
            footprint = detect_project_footprint(self.project_root)
            manifest = manifest_from_detection(user_compose, footprint, resolved_name)
            write_manifest(self.project_root, manifest)
            log_success("Wrote project manifest: .ilha/ilha.yml")
            self._display_manifest(manifest)

        if not manifest.web:
            log_warning(
                "No web-routable service was detected. Nothing will be reachable via a "
                "domain until you add a service under `web:` in .ilha/ilha.yml."
            )
            if not non_interactive:
                if not prompt_yes_no("Continue setup with no routable service?", default=False):
                    log_info("Setup stopped. Edit .ilha/ilha.yml `web:` and re-run `ilha setup`.")
                    return None
        return manifest

    def _display_manifest(self, manifest) -> None:
        """Print the ratification summary — what ilha understood about the project."""
        log_info("ilha will run this project as:")
        if manifest.web:
            for w in manifest.web:
                dom = w.domain or "<project>-<branch>.localhost"
                log_info(f"  - web: {w.service} (port {w.port}) -> {dom}")
        else:
            log_info("  - web: (none detected)")
        if manifest.db:
            log_info(f"  - db:  {manifest.db.service} ({manifest.db.engine})")
        if manifest.volumes:
            log_info(f"  - volumes cloned per worktree: {', '.join(manifest.volumes)}")

    def _write_compose_overlay(
        self,
        source_compose: Path,
        *,
        routable: Optional[Dict[str, int]] = None,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> bool:
        """Write `.ilha/docker-compose.overlay.yml` from the user's compose.

        The user's compose is read but never mutated. The overlay carries the
        deltas ilha owns (Caddy labels, ilha_caddy_proxy network, container_name
        templates, env_file wiring, host-port isolation for routable services).
        """
        try:
            with open(source_compose) as f:
                user_compose = yaml.safe_load(f) or {}
        except Exception as e:
            log_error(f"Failed to read compose file: {e}")
            return False
        if not isinstance(user_compose, dict) or "services" not in user_compose:
            log_error("Invalid compose file: missing top-level 'services' key.")
            return False

        from ..utils.overlay_generator import (
            detect_conflicting_services,
            generate_overlay,
        )

        conflicts = detect_conflicting_services(user_compose)
        for name in conflicts:
            log_warning(
                f"Service '{name}' in your docker-compose.yml conflicts with ilha's "
                f"global proxy. Remove it from docker-compose.yml; ilha provides the "
                f"Caddy proxy via the `ilha-proxy` project."
            )

        # Same split deploy performs, so a worktree routes the way production
        # will. Without it both `web` and `frontend` claim the whole domain and
        # Caddy picks by container start order — which silently served Slack's
        # webhook endpoints the SPA's index.html, a 200 that Slack reads as
        # success while the backend never sees the event.
        path_routes = _django_spa_path_routes(self.project_root, user_compose)
        overlay = generate_overlay(
            user_compose, routable=routable, domain=domain, ip=ip,
            path_routes=path_routes,
        )

        target = self.ilha_dir / "docker-compose.overlay.yml"
        target.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# Generated by ilha. Re-run `ilha setup` to regenerate; do not edit by hand.\n"
            "# Used at runtime as: docker compose -f docker-compose.yml -f .ilha/docker-compose.overlay.yml ...\n"
        )
        try:
            with open(target, "w") as f:
                f.write(header)
                yaml.dump(overlay, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            log_error(f"Failed to write {target}: {e}")
            return False

        log_success(f"Created compose overlay: {target}")
        if not conflicts:
            # Make the contract loud the first time around.
            log_info(
                "ilha never modifies your docker-compose.yml. Compose runs both files: "
                "`docker compose -f docker-compose.yml -f .ilha/docker-compose.overlay.yml ...`"
            )
        return True

    def _create_config_file(
        self,
        project_name: Optional[str],
        compose_file: Optional[Path] = None,
        compose_data: Optional[Dict[str, Any]] = None,
        overwrite: bool = False,
    ) -> bool:
        """Create config.yml file.
        
        If .ilha/config.yml does not exist, checks for example template first.
        If example exists, copies and edits it. Otherwise creates from scratch.
        
        Args:
            project_name: Optional project name
            compose_file: Path to docker-compose.yml (optional when ``compose_data`` is set)
            compose_data: Parsed compose (Ilha default static template when no user compose)
            overwrite: If True, overwrite existing config.yml file
        """
        try:
            config_file = self.ilha_dir / "config.yml"
            
            # Don't overwrite if it already exists (unless overwrite flag is set)
            if config_file.exists() and not overwrite:
                log_info("config.yml already exists, skipping creation")
                return True
            
            # Auto-detect project name if not provided
            if not project_name:
                from ..config.settings import resolve_project_name
                project_name = resolve_project_name(self.project_root)
            
            # Generate config dict (single source of truth)
            config = self._generate_config_dict(
                project_name,
                compose_file=compose_file,
                compose_data=compose_data,
            )
            
            # Check if example template exists
            examples_dir = self._get_examples_dir()
            example_config = examples_dir / "example.config.yml"
            
            if example_config.exists():
                # Copy example template and edit it
                # Copy example to target location
                shutil.copy2(example_config, config_file)
                log_info(f"Copied example template from {example_config}")
                
                # Read the copied file
                content = config_file.read_text()
                
                # Uncomment and set project_name
                content = re.sub(
                    r'^# project_name:.*',
                    f'project_name: {project_name}',
                    content,
                    flags=re.MULTILINE
                )
                
                # Uncomment and set caddy_network
                content = re.sub(
                    r'^# caddy_network:.*',
                    f"caddy_network: {config['caddy_network']}",
                    content,
                    flags=re.MULTILINE
                )
                
                # Uncomment and set worktree_dir
                content = re.sub(
                    r'^# worktree_dir:.*',
                    f"worktree_dir: {config['worktree_dir']}",
                    content,
                    flags=re.MULTILINE
                )
                
                # Uncomment services section and add detected services
                # Replace the entire services section with actual services
                services_yaml = yaml.dump({'services': config['services']}, default_flow_style=False, sort_keys=False)
                # Find services section (from "services:" to next "# ==" or end of section)
                services_match = re.search(r'^services:.*?(?=^# =|^volumes:|$)', content, re.MULTILINE | re.DOTALL)
                if services_match:
                    # Replace the matched section
                    content = content[:services_match.start()] + services_yaml + content[services_match.end():]
                else:
                    # If not found, just replace "services:" line
                    content = re.sub(r'^services:', services_yaml, content, flags=re.MULTILINE)
                
                # Uncomment and set volumes
                volumes_yaml = yaml.dump({'volumes': config['volumes']}, default_flow_style=False, sort_keys=False)
                volumes_match = re.search(r'^volumes:.*?(?=^# =|^environment:|$)', content, re.MULTILINE | re.DOTALL)
                if volumes_match:
                    content = content[:volumes_match.start()] + volumes_yaml + content[volumes_match.end():]
                else:
                    content = re.sub(r'^volumes:', volumes_yaml, content, flags=re.MULTILINE)
                
                # Uncomment and set environment variables
                env_yaml = yaml.dump({'environment': config['environment']}, default_flow_style=False, sort_keys=False)
                env_match = re.search(r'^environment:.*?(?=^# =|$)', content, re.MULTILINE | re.DOTALL)
                if env_match:
                    content = content[:env_match.start()] + env_yaml + content[env_match.end():]
                else:
                    content = re.sub(r'^environment:', env_yaml, content, flags=re.MULTILINE)
                
                # Write edited content
                config_file.write_text(content)
                log_success(f"Created config file from template: {config_file}")
            else:
                # No example template, create from scratch (existing behavior)
                with open(config_file, 'w') as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
                log_success(f"Created config file: {config_file}")
            
            # Copy README.md to .ilha directory
            if not self._copy_readme_file():
                log_warning("Failed to copy README.md to .ilha directory")
            
            return True
            
        except Exception as e:
            log_error(f"Failed to create config file: {e}")
            return False
    
    def _copy_readme_file(self) -> bool:
        """Copy README.md from package to .ilha directory."""
        try:
            from ..config.settings import get_script_dir
            
            # Get source README.md from package
            script_dir = get_script_dir()
            source_readme = script_dir / "config" / "README.md"
            
            if not source_readme.exists():
                log_warning(f"Source README.md not found: {source_readme}")
                return False
            
            # Copy to .ilha directory
            target_readme = self.ilha_dir / "README.md"
            import shutil
            shutil.copy2(source_readme, target_readme)
            
            log_success(f"Copied README.md to: {target_readme}")
            return True
            
        except Exception as e:
            log_error(f"Failed to copy README.md: {e}")
            return False
    
    def _parse_env_content_to_dict(self, content: str) -> Dict[str, str]:
        """Parse env file content (string) into key-value dictionary.
        
        Handles comments, empty lines, and KEY=VALUE format.
        Similar to load_env_file but works on string content instead of file path.
        
        Args:
            content: Environment file content as string
            
        Returns:
            Dictionary of key-value pairs
        """
        env_vars = {}
        
        if not content:
            return env_vars
        
        for line in content.split('\n'):
            # Strip whitespace
            line = line.strip()
            
            # Skip empty lines
            if not line:
                continue
            
            # Skip comment lines
            if line.startswith('#'):
                continue
            
            # Parse KEY=VALUE format
            if '=' in line:
                # Split on first = to handle values that might contain =
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                
                # Skip if key is empty
                if key:
                    env_vars[key] = value
        
        return env_vars
    
    def _write_env_dict_to_file(self, env_vars: Dict[str, str], file_path: Path, header_comment: Optional[str] = None) -> None:
        """Write dictionary to env file in clean format.
        
        Args:
            env_vars: Dictionary of key-value pairs to write
            file_path: Path to write the env file
            header_comment: Optional header comment to include at the top
        """
        lines = []
        
        # Add header comment if provided
        if header_comment:
            lines.append(header_comment)
            if not header_comment.endswith('\n'):
                lines.append('')
        
        # Write all key-value pairs, sorted for consistency
        for key in sorted(env_vars.keys()):
            value = env_vars[key]
            lines.append(f"{key}={value}")
        
        # Write to file
        content = '\n'.join(lines)
        if content and not content.endswith('\n'):
            content += '\n'
        
        file_path.write_text(content, encoding='utf-8')
    
    def _create_template_env_ilha(self, overwrite: bool = False) -> bool:
        """Create template env.ilha for project root (enables master branch usage).
        
        Intelligently merges environment variables from multiple sources:
        - .env file (if exists) - base values
        - existing env.ilha (if exists and overwrite=True) - overrides .env
        - ilha-specific settings - always applied last (highest precedence)
        
        Ensures no duplicate keys in the final output.
        
        Args:
            overwrite: If True, overwrite existing env.ilha file
        """
        try:
            from ..utils.env_loader import load_env_file
            
            env_ilha_path = self.ilha_dir / "env.ilha"
            
            # Don't overwrite if it already exists (unless overwrite flag is set)
            if env_ilha_path.exists() and not overwrite:
                log_info("env.ilha already exists, skipping creation")
                return True
            
            # Generate ilha-specific env content (reuses settings.py logic).
            # branch_name=None lets _generate_env_ilha_content resolve the
            # actual default branch from git instead of hardcoding "master".
            env_content = self._generate_env_ilha_content()
            
            # Extract header comment from ilha-specific content (first comment line)
            header_comment = None
            for line in env_content.split('\n'):
                if line.strip().startswith('#'):
                    header_comment = line.strip()
                    break
            
            # Start with empty dict
            merged_env = {}
            
            # 1. Load .env file (if exists) - base values
            dot_env_path = self.project_root / ".env"
            if dot_env_path.exists():
                dot_env_vars = load_env_file(dot_env_path)
                merged_env.update(dot_env_vars)
                log_info(f"Loaded {len(dot_env_vars)} variables from .env file")
            
            # 2. Load existing env.ilha (if exists and overwriting) - overrides .env
            if env_ilha_path.exists() and overwrite:
                existing_env_vars = load_env_file(env_ilha_path)
                merged_env.update(existing_env_vars)
                log_info(f"Loaded {len(existing_env_vars)} variables from existing env.ilha")
            
            # 3. Parse ilha-specific content and merge (highest precedence)
            ilha_env_vars = self._parse_env_content_to_dict(env_content)
            merged_env.update(ilha_env_vars)
            log_info(f"Applied {len(ilha_env_vars)} ilha-specific variables")
            
            # Write merged result
            self._write_env_dict_to_file(merged_env, env_ilha_path, header_comment=header_comment)
            
            log_success(f"Created/updated env.ilha: {env_ilha_path}")
            log_info(f"Total variables: {len(merged_env)} (no duplicates)")
            if dot_env_path.exists():
                log_info("env.ilha contains your .env variables plus ilha-specific overrides")
                log_info("Edit env.ilha to customize values (never edit .env files)")
            
            log_info("This enables using ilha commands with the master branch")
            return True
            
        except Exception as e:
            log_error(f"Failed to create template env.ilha: {e}")
            return False
    
    def _regenerate_example_files(self) -> bool:
        """Regenerate example.config.yml and example.env.ilha in examples/ directory.
        
        Uses same structure as active config generation but with all settings commented.
        Ensures example files stay in sync with actual config structure.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            examples_dir = self._get_examples_dir()
            examples_dir.mkdir(parents=True, exist_ok=True)
            
            # Generate example.config.yml
            example_config_path = examples_dir / "example.config.yml"
            
            # Read the existing example file if it exists to preserve structure
            # Otherwise generate from scratch
            if example_config_path.exists():
                log_info("Regenerating example.config.yml (preserving existing structure)")
            else:
                log_info("Creating example.config.yml")
            
            # For now, we'll write a comprehensive template
            # In the future, this could parse existing configs to generate examples
            config_template = """# ilha Project Configuration Template
# This file serves as a comprehensive template with all possible settings
# Uncomment and modify settings as needed for your project
# 
# When running `ilha setup`, if .ilha/config.yml does not exist,
# this template will be copied and automatically configured based on your
# docker-compose.yml file.

# ============================================================================
# Project Identification
# ============================================================================

# Project name (used in container names, volumes, and domains)
# Default: directory name
# Automatically set during setup based on project directory name
# project_name: myproject

# ============================================================================
# Network Configuration
# ============================================================================

# Docker network name for Caddy proxy
# Default: ilha_caddy_proxy
# This network is shared across all worktrees for routing
# caddy_network: ilha_caddy_proxy

# ============================================================================
# Directory Configuration
# ============================================================================

# Directory where worktrees are stored
# Default: worktrees
# Worktrees will be created in {project_root}/{worktree_dir}/{branch_name}/
# worktree_dir: worktrees

# ============================================================================
# Service Configuration
# ============================================================================

# Service configuration
# Automatically detected from docker-compose.yml during setup
# Each service can have a container_name_template
# The ${COMPOSE_PROJECT_NAME} variable is automatically set per worktree
# Format: {project_name}-{branch_name}
services:
  # Example web service
  # web:
  #   container_name_template: ${COMPOSE_PROJECT_NAME}-web
  
  # Example database service
  # db:
  #   container_name_template: ${COMPOSE_PROJECT_NAME}-db
  
  # Example Redis service
  # redis:
  #   container_name_template: ${COMPOSE_PROJECT_NAME}-redis
  
  # Add more services as needed:
  # api:
  #   container_name_template: ${COMPOSE_PROJECT_NAME}-api
  # worker:
  #   container_name_template: ${COMPOSE_PROJECT_NAME}-worker

# ============================================================================
# Volume Configuration
# ============================================================================

# Volume names (automatically detected from docker-compose.yml)
# These will be prefixed with {project_name}-{branch_name}_ for each worktree
# Example: myproject-feature-auth_postgres_data
volumes:
  # Example PostgreSQL volume
  # - postgres_data
  
  # Example Redis volume
  # - redis_data
  
  # Example media files volume
  # - media_files
  
  # Add more volumes as needed:
  # - sqlite_data
  # - static_files
  # - uploads

# ============================================================================
# Default Environment Variables
# ============================================================================

# Default environment variables for all worktrees
# These can be overridden in worktree-specific env.ilha files
# These values are used as defaults when creating new worktrees
environment:
  # Debug mode (True for development, False for production)
  # DEBUG: "True"
  
  # Allowed hosts for Django/Flask applications
  # Automatically includes localhost, 127.0.0.1, worktree domain, *.localhost, and web
  # ALLOWED_HOSTS: "localhost,127.0.0.1,*.localhost,web"
  
  # Add more default environment variables:
  # POSTGRES_USER: "user"
  # POSTGRES_PASSWORD: "password"
  # POSTGRES_DB: "database"
  # DJANGO_SECRET_KEY: "django-insecure-secret-key"
  # SITE_DOMAIN: "localhost:8000"
  # CADDY_EMAIL: "admin@example.com"

# ============================================================================
# Deployment Configuration (Optional)
# ============================================================================

# Deployment configuration
# Used by 'ilha droplet deploy' and 'ilha droplet put' commands
# These settings provide defaults for deployment operations
# deployment:
  # Default server for push operations
  # Format: username@hostname:/path/to/packages
  # Example: user@server.example.com:/var/ilha/packages
  # default_server: user@server.example.com:/var/ilha/packages
  
  # Default domain for HTTPS deployments
  # Used when --domain flag is not provided to push command
  # Example: myapp.example.com
  # default_domain: myapp.example.com
  
  # Default IP for HTTP-only deployments
  # Used when --ip flag is not provided to push command
  # Note: IP-only deployments are HTTP-only (no TLS)
  # Example: 203.0.113.10
  # default_ip: 203.0.113.10
  
  # SSH key path for deployment
  # Used for SCP operations during push
  # Example: ~/.ssh/deploy_key
  # ssh_key: ~/.ssh/deploy_key

# ============================================================================
# DNS Configuration (Optional)
# ============================================================================

# DNS configuration
# Used for automatic DNS management during deployment
# Currently supports Digital Ocean DNS API
# dns:
  # DNS provider (currently only 'digitalocean' is supported)
  # provider: digitalocean
  
  # API token for DNS provider
  # Can use environment variable: ${DIGITALOCEAN_API_TOKEN}
  # Priority: CLI flag > shell environment > .env file > this config file
  # api_token: ${DIGITALOCEAN_API_TOKEN}
  
  # Default domain for DNS operations
  # Used when creating subdomains during deployment
  # Example: example.com
  # default_domain: example.com
"""
            
            example_config_path.write_text(config_template)
            log_success(f"Regenerated example.config.yml: {example_config_path}")
            
            # Generate example.env.ilha
            example_env_path = examples_dir / "example.env.ilha"
            
            if example_env_path.exists():
                log_info("Regenerating example.env.ilha (preserving existing structure)")
            else:
                log_info("Creating example.env.ilha")
            
            # Read the existing example file content (we already created it, so use that)
            # But for regeneration, we'll write the comprehensive template
            env_template = """# ilha Environment Configuration Template
# This file serves as a comprehensive template with all possible environment variables
# Uncomment and modify variables as needed for your project
#
# This file is automatically sourced by Docker Compose
# Variables here override those in .env files
#
# When running `ilha setup`, if .ilha/env.ilha does not exist,
# this template will be copied and automatically configured for the project root.

# ============================================================================
# Project Identification
# ============================================================================

# Docker Compose project name (automatically set per worktree)
# Format: {project_name}-{branch_name}
# For project root, uses: {project_name}-master
# COMPOSE_PROJECT_NAME=myproject-master

# Project root directory (automatically set)
# Points to the worktree directory
# PROJECT_ROOT=${PWD}

# ============================================================================
# Domain and Host Configuration
# ============================================================================

# Site domain for the worktree
# Local development: {project_name}-{branch_name}.localhost
# Production: https://subdomain.example.com
# IP-only: http://203.0.113.10
# SITE_DOMAIN=myproject-master.localhost

# Allowed hosts for Django/Flask applications
# Automatically includes localhost, 127.0.0.1, worktree domain, *.localhost, and web
# Format: comma-separated list
# ALLOWED_HOSTS=localhost,127.0.0.1,myproject-master.localhost,*.localhost,web

# ============================================================================
# Django Configuration
# ============================================================================

# Debug mode (True for development, False for production)
# DEBUG=True

# Django secret key (use a secure random key in production)
# Generate with: python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
# DJANGO_SECRET_KEY=django-insecure-secret-key

# Enable X-Forwarded-Host header support (required for reverse proxy)
# Set to True when using Caddy or other reverse proxies
# USE_X_FORWARDED_HOST=True

# CSRF trusted origins (space-separated URLs)
# For localhost: http://{project_name}-{branch_name}.localhost
# For domains: https://subdomain.example.com http://subdomain.example.com
# CSRF_TRUSTED_ORIGINS=http://myproject-master.localhost

# Secure proxy SSL header (tuple format for Django)
# Format: HTTP_X_FORWARDED_PROTO,https
# Used to detect HTTPS when behind a reverse proxy
# SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https

# Secure cookie settings (automatically set by ilha)
# For HTTPS domain deployments: USE_SECURE_COOKIES=True
# For HTTP localhost/IP deployments: USE_SECURE_COOKIES=False
# USE_SECURE_COOKIES=True

# ============================================================================
# Database Configuration (PostgreSQL)
# ============================================================================

# PostgreSQL user
# POSTGRES_USER=user

# PostgreSQL password
# POSTGRES_PASSWORD=password

# PostgreSQL database name
# POSTGRES_DB=database

# PostgreSQL host (automatically set to worktree-specific container)
# Format: ${COMPOSE_PROJECT_NAME}-db
# POSTGRES_HOST=${COMPOSE_PROJECT_NAME}-db

# PostgreSQL port
# POSTGRES_PORT=5432

# Database URL (automatically constructed)
# Format: postgres://user:password@host:port/database
# DATABASE_URL=postgres://user:password@${COMPOSE_PROJECT_NAME}-db:5432/database

# ============================================================================
# Redis Configuration
# ============================================================================

# Redis host (automatically set to worktree-specific container)
# Format: ${COMPOSE_PROJECT_NAME}-redis
# REDIS_HOST=${COMPOSE_PROJECT_NAME}-redis

# Redis port
# REDIS_PORT=6379

# Redis database number
# REDIS_DB=0

# Redis URL (automatically constructed)
# Format: redis://host:port/db
# REDIS_URL=redis://${COMPOSE_PROJECT_NAME}-redis:6379/0

# ============================================================================
# Caddy Proxy Configuration
# ============================================================================

# Email for Let's Encrypt certificate notifications
# Used when deploying with HTTPS via Caddy
# CADDY_EMAIL=admin@example.com

# ============================================================================
# DNS Provider Configuration (for deployment)
# ============================================================================

# Digital Ocean API token (for automatic DNS management)
# Can also be set via environment variable: DIGITALOCEAN_API_TOKEN
# Priority: CLI flag > shell env > .env file > this file
# DIGITALOCEAN_API_TOKEN=your_token_here


# ============================================================================
# Droplet/Server Configuration (for deployment)
# ============================================================================

# Default region for droplet creation
# Used with ilha droplet deploy command
# Example: nyc1, sfo3, lon1
# DROPLET_DEFAULT_REGION=nyc1

# Default size for droplet creation
# Used with ilha droplet deploy command
# Example: s-1vcpu-1gb, s-2vcpu-4gb
# DROPLET_DEFAULT_SIZE=s-1vcpu-1gb

# Default image for droplet creation
# Used with ilha droplet deploy command
# Example: ubuntu-22-04-x64, ubuntu-20-04-x64
# DROPLET_DEFAULT_IMAGE=ubuntu-22-04-x64

# Default SSH keys for droplet creation (comma-separated)
# Used with ilha droplet deploy and droplet put commands
# SSH key names (comma-separated, e.g., anders,peter)
# Only key names are supported, not numeric IDs or fingerprints
# DROPLET_DEFAULT_SSH_KEYS=anders,peter

# ============================================================================
# Application-Specific Configuration
# ============================================================================

# Add your application-specific environment variables below:
# CUSTOM_SETTING=value
# API_KEY=your_api_key
# EXTERNAL_SERVICE_URL=https://api.example.com
# LOG_LEVEL=INFO
# SENTRY_DSN=https://your-sentry-dsn@sentry.io/project-id
"""
            
            example_env_path.write_text(env_template)
            log_success(f"Regenerated example.env.ilha: {example_env_path}")
            
            return True
            
        except Exception as e:
            log_error(f"Failed to regenerate example files: {e}")
            return False
    
    def _validate_project_root(self) -> bool:
        """Validate that PROJECT_ROOT environment variable is set correctly."""
        import os
        
        project_root = os.environ.get('PROJECT_ROOT')
        if not project_root:
            log_warning("PROJECT_ROOT environment variable not set")
            log_info("Setting PROJECT_ROOT to current project root...")
            os.environ['PROJECT_ROOT'] = str(self.project_root)
            project_root = str(self.project_root)
        
        # Validate that PROJECT_ROOT points to a valid directory
        project_root_path = Path(project_root)
        if not project_root_path.exists():
            log_error(f"PROJECT_ROOT path does not exist: {project_root}")
            return False
        
        if not project_root_path.is_dir():
            log_error(f"PROJECT_ROOT path is not a directory: {project_root}")
            return False
        
        # Check if it contains typical project files
        project_files = ['manage.py', 'requirements.txt', 'pyproject.toml', 'package.json', 'Dockerfile']
        has_project_files = any((project_root_path / file).exists() for file in project_files)
        
        if not has_project_files:
            log_warning(f"PROJECT_ROOT directory may not be a valid project root: {project_root}")
            log_info("Continuing anyway - this may be intentional for custom setups")
        
        log_success(f"PROJECT_ROOT validated: {project_root}")
        return True
    
    def _handle_gitignore_setup(self) -> bool:
        """Handle ignore file setup for ilha directories."""
        try:
            # Get worktree directory from config (defaults to 'worktrees')
            worktree_dir = get_worktree_dir()
            worktrees_entry = f"{worktree_dir}/"
            old_entry = f"{worktree_dir}/"  # For migration from old path references
            
            success = True
            
            # Handle .gitignore
            log_info("The .ilha directory contains configuration files that should be committed to version control.")
            log_info(f"However, {worktrees_entry} contains worktree directories that should NOT be committed.")
            
            # Check if old entry exists (migration case)
            if check_gitignore_entry(self.project_root, old_entry):
                log_info(f"Found old entry '{old_entry}' in .gitignore")
                log_info(f"Migrating to new worktree directory: {worktrees_entry}")
                
                if prompt_yes_no(f"Would you like to update .gitignore to use {worktrees_entry}?", default=True):
                    if replace_gitignore_entry(self.project_root, old_entry, worktrees_entry):
                        log_success(f"Updated .gitignore: now ignoring {worktrees_entry}")
                    else:
                        log_warning("Failed to update .gitignore")
                        success = False
                else:
                    log_info("Skipped updating .gitignore")
            # Check if correct entry already exists
            elif check_gitignore_entry(self.project_root, worktrees_entry):
                log_info(f"{worktrees_entry} is already in .gitignore")
            # New setup case
            else:
                if prompt_yes_no(f"Would you like to add {worktrees_entry} to your .gitignore file?", default=True):
                    if add_to_gitignore(self.project_root, worktrees_entry):
                        log_success(f"Added {worktrees_entry} to .gitignore")
                        log_info("✓ .ilha/ directory will be committed")
                        log_info(f"✓ {worktrees_entry} will be ignored")
                    else:
                        log_warning(f"Failed to add {worktrees_entry} to .gitignore")
                        success = False
                else:
                    log_info("Skipped adding to .gitignore")
                    log_warning(f"Note: You may want to manually add {worktrees_entry} to .gitignore")
            
            # Handle .cursorignore (only if file exists)
            cursorignore_path = self.project_root / ".cursorignore"
            if cursorignore_path.exists():
                if prompt_yes_no(f"Would you like to add {worktrees_entry} to your .cursorignore file?", default=True):
                    if add_to_cursorignore(self.project_root, worktrees_entry):
                        log_success(f"Added {worktrees_entry} to .cursorignore")
                    else:
                        log_warning(f"Failed to add {worktrees_entry} to .cursorignore")
                        success = False
                else:
                    log_info("Skipped adding to .cursorignore")
            
            # Handle .cursorindexignore (only if file exists)
            cursorindexignore_path = self.project_root / ".cursorindexignore"
            if cursorindexignore_path.exists():
                if prompt_yes_no(f"Would you like to add {worktrees_entry} to your .cursorindexignore file?", default=True):
                    if add_to_cursorindexignore(self.project_root, worktrees_entry):
                        log_success(f"Added {worktrees_entry} to .cursorindexignore")
                    else:
                        log_warning(f"Failed to add {worktrees_entry} to .cursorindexignore")
                        success = False
                else:
                    log_info("Skipped adding to .cursorindexignore")
            
            return success
                
        except Exception as e:
            log_error(f"Failed to handle ignore file setup: {e}")
            return False
    
    def _handle_completion_setup(self) -> bool:
        """Handle shell completion setup for ilha."""
        try:
            from .completion import CompletionManager
            
            completion_manager = CompletionManager()
            return completion_manager.prompt_install_completion()
            
        except Exception as e:
            log_error(f"Failed to handle completion setup: {e}")
            return False
    
    def is_setup_complete(self) -> bool:
        """Check if ilha is already set up for this project."""
        config_file = self.ilha_dir / "config.yml"
        overlay_file = self.ilha_dir / "docker-compose.overlay.yml"

        return all(f.exists() for f in [config_file, overlay_file])

    def get_setup_status(self) -> Dict[str, Any]:
        """Get status of ilha setup."""
        return {
            "ilha_dir_exists": self.ilha_dir.exists(),
            "config_file_exists": (self.ilha_dir / "config.yml").exists(),
            "compose_file_exists": (self.ilha_dir / "docker-compose.overlay.yml").exists(),
            "is_complete": self.is_setup_complete(),
            "project_root": str(self.project_root),
            "ilha_dir": str(self.ilha_dir)
        }
