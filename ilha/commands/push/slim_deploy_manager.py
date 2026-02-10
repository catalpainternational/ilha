"""
Slim deploy manager for ilha.

Deploys a worktree to a remote server using rsync + remote docker compose,
without requiring Python, git, or ilha on the server.

Flow:
  1. Assemble a flat deploy directory locally (code + compose + env + caddy)
  2. Apply domain/IP overrides locally
  3. Backup Docker volumes locally (if any)
  4. rsync the deploy directory to the server
  5. Restore volumes on the server via SSH + docker
  6. Start Caddy and app services via SSH + docker compose
"""

import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, List, Tuple

from ...config.settings import get_script_dir, get_worktree_dir
from ...core.dns_manager import DNSManager, parse_domain, is_domain
from ...core.droplet_manager import DropletInfo
from ...core.environment_manager import EnvironmentManager
from ...utils.caddy_config import ensure_caddy_labels_and_network
from ...utils.logging import log_info, log_success, log_warning, log_error, print_plain
from ...utils.ssh_manager import SSHConnectionManager, SCPTarget
from ...utils.ssh_utils import add_ssh_host_key
from ...utils.streaming import execute_with_streaming
from .server_preparer import ServerPreparer
from .transfer_manager import TransferManager


class SlimDeployManager:
    """Deploys a worktree via rsync + remote docker compose (no ilha on server)."""

    # Remote directory structure
    REMOTE_APP_ROOT = "/app"
    REMOTE_CADDY_DIR = "/app/caddy"
    REMOTE_VOLUMES_DIR = "/app/volumes"

    def __init__(self, project_root: Optional[Path] = None):
        if project_root is None:
            from ...config.settings import get_project_root
            self.project_root = get_project_root()
        else:
            self.project_root = Path(project_root).resolve()

        self.ssh = SSHConnectionManager()
        self.transfer = TransferManager()

    # ──────────────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────────────
    def deploy(
        self,
        branch_name: str,
        scp_target: str,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
        prepare_server: bool = True,
        dns_token: Optional[str] = None,
        skip_dns_check: bool = False,
        build: bool = False,
        debug: bool = False,
        droplet_info: Optional[DropletInfo] = None,
        use_staging_certificates: bool = False,
    ) -> bool:
        """Run the full slim deploy pipeline.

        Returns True on success, False on failure.
        """
        try:
            target = SCPTarget(scp_target)
        except ValueError:
            log_error(f"Invalid SCP target: {scp_target}  (expected user@host:path)")
            return False

        username = target.username
        server = target.server

        log_info("Starting slim deploy (no ilha required on server)...")
        log_info(f"  Branch:  {branch_name}")
        log_info(f"  Server:  {username}@{server}")
        if domain:
            log_info(f"  Domain:  {domain}")
        elif ip:
            log_info(f"  IP:      {ip}")

        # ── 1–3. DNS, prepare server, assemble deploy dir (in parallel) ─
        log_info("Running DNS, server preparation, and deploy assembly in parallel...")
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            if domain and not skip_dns_check:
                futures["dns"] = executor.submit(
                    self._handle_dns, domain, server, dns_token, droplet_info
                )
            if prepare_server:
                futures["prepare"] = executor.submit(
                    self._prepare_server_slim, username, server
                )
            futures["assemble"] = executor.submit(
                self._assemble_deploy_dir,
                branch_name,
                domain=domain,
                ip=ip,
                debug=debug,
                use_staging_certificates=use_staging_certificates,
            )

            dns_ok = futures["dns"].result() if "dns" in futures else True
            prepare_ok = futures["prepare"].result() if "prepare" in futures else True
            deploy_dir = futures["assemble"].result()

        if "dns" in futures and not dns_ok:
            log_warning("DNS management failed, continuing anyway...")
        if prepare_server and not prepare_ok:
            log_error("Slim server preparation failed.")
            return False
        if deploy_dir is None:
            return False

        # ── 4. Backup volumes locally ────────────────────────────────
        volume_tar = self._backup_volumes_locally(branch_name, deploy_dir)

        # ── 5. rsync to server ───────────────────────────────────────
        add_ssh_host_key(server)
        if not self._rsync_to_server(deploy_dir, username, server):
            return False

        # ── 6–8. Restore volumes, start Caddy, build (in parallel) ─────
        log_info("Running volume restore, Caddy start, and build (if requested) in parallel...")
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            if volume_tar:
                futures["restore"] = executor.submit(
                    self._restore_volumes_remote, username, server, branch_name
                )
            futures["caddy"] = executor.submit(
                self._start_caddy_remote, username, server
            )
            if build:
                futures["build"] = executor.submit(
                    self._build_remote, username, server, debug
                )

            for key in futures:
                ok = futures[key].result()
                if key == "caddy" and not ok:
                    log_warning("Caddy start had issues, but continuing with app services...")
                elif key == "build" and not ok:
                    log_warning("Docker build had issues, but continuing with app start...")

        # ── 9. Start app services ────────────────────────────────────
        if not self._start_app_remote(username, server, build=build, debug=debug):
            log_error("Failed to start application services on the server.")
            return False

        # ── 10. Summary ──────────────────────────────────────────────
        url = None
        if domain:
            url = f"https://{domain}"
        elif ip:
            url = f"http://{ip}"
        elif droplet_info and droplet_info.ip_address:
            url = f"http://{droplet_info.ip_address}"

        print_plain("")
        log_success("Slim deploy completed successfully!")
        if url:
            log_success(f"🌐 Your site is available at: {url}")
        print_plain("")

        # Cleanup local temp dir
        try:
            shutil.rmtree(deploy_dir, ignore_errors=True)
        except Exception:
            pass

        return True

    def put(
        self,
        branch_name: str,
        scp_target: str,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
        build: bool = False,
        debug: bool = False,
        use_staging_certificates: bool = False,
        code_only: bool = False,
        db_only: bool = False,
    ) -> bool:
        """Update an existing slim-deployed server.

        Default: backup volumes, sync code + config + volumes, restore volumes, restart Caddy + app.
        code_only: sync code + config only, restart Caddy + app (no volume operations).
        db_only: backup volumes, sync only volumes to server, stop app, restore volumes, start app.
        """
        try:
            target = SCPTarget(scp_target)
        except ValueError:
            log_error(f"Invalid SCP target: {scp_target}  (expected user@host:path)")
            return False

        username = target.username
        server = target.server

        log_info("Starting slim put...")
        log_info(f"  Branch:  {branch_name}")
        log_info(f"  Server:  {username}@{server}")
        if domain:
            log_info(f"  Domain:  {domain}")
        elif ip:
            log_info(f"  IP:      {ip}")
        if code_only:
            log_info("  Mode:    code only (no volumes)")
        elif db_only:
            log_info("  Mode:    db/volumes only (no code sync)")

        def _cleanup_deploy_dir(d: Optional[Path]) -> None:
            if d is not None:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass

        # ── db_only: volumes only ─────────────────────────────────────
        if db_only:
            deploy_dir = Path(tempfile.mkdtemp(prefix=f"ilha-put-volumes-{branch_name}-"))
            try:
                (deploy_dir / "volumes").mkdir()
                volume_tar = self._backup_volumes_locally(branch_name, deploy_dir)
                if not volume_tar:
                    log_warning("No volumes to sync; nothing to do for --db-only")
                    _cleanup_deploy_dir(deploy_dir)
                    return True
                add_ssh_host_key(server)
                if not self._rsync_volumes_to_server(deploy_dir, username, server):
                    _cleanup_deploy_dir(deploy_dir)
                    return False
                if not self._stop_app_remote(username, server):
                    log_warning("App stop had issues, but continuing with volume restore...")
                if not self._restore_volumes_remote(username, server, branch_name):
                    _cleanup_deploy_dir(deploy_dir)
                    return False
                if not self._start_app_remote(username, server, build=False, debug=False):
                    _cleanup_deploy_dir(deploy_dir)
                    log_error("Failed to start application services on the server.")
                    return False
                log_success("Slim put (db-only) completed successfully!")
                return True
            finally:
                _cleanup_deploy_dir(deploy_dir)

        # ── code_only: code + config, no volumes ───────────────────────
        if code_only:
            deploy_dir = self._assemble_deploy_dir(
                branch_name,
                domain=domain,
                ip=ip,
                debug=debug,
                use_staging_certificates=use_staging_certificates,
            )
            if deploy_dir is None:
                return False
            add_ssh_host_key(server)
            if not self._rsync_to_server(deploy_dir, username, server):
                _cleanup_deploy_dir(deploy_dir)
                return False
            if not self._start_caddy_remote(username, server):
                log_warning("Caddy start had issues, but continuing with app services...")
            if not self._start_app_remote(username, server, build=build, debug=debug):
                _cleanup_deploy_dir(deploy_dir)
                log_error("Failed to start application services on the server.")
                return False
            url = f"https://{domain}" if domain else (f"http://{ip}" if ip else None)
            print_plain("")
            log_success("Slim put (code-only) completed successfully!")
            if url:
                log_success(f"🌐 Your site is available at: {url}")
            print_plain("")
            _cleanup_deploy_dir(deploy_dir)
            return True

        # ── Default: full sync (volumes + code + config) ────────────────
        deploy_dir = self._assemble_deploy_dir(
            branch_name,
            domain=domain,
            ip=ip,
            debug=debug,
            use_staging_certificates=use_staging_certificates,
        )
        if deploy_dir is None:
            return False
        volume_tar = self._backup_volumes_locally(branch_name, deploy_dir)
        add_ssh_host_key(server)
        if not self._rsync_to_server(deploy_dir, username, server):
            _cleanup_deploy_dir(deploy_dir)
            return False
        log_info("Running volume restore and Caddy start...")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {}
            if volume_tar:
                futures["restore"] = executor.submit(
                    self._restore_volumes_remote, username, server, branch_name
                )
            futures["caddy"] = executor.submit(self._start_caddy_remote, username, server)
            for key in futures:
                ok = futures[key].result()
                if key == "caddy" and not ok:
                    log_warning("Caddy start had issues, but continuing with app services...")
        if not self._start_app_remote(username, server, build=build, debug=debug):
            _cleanup_deploy_dir(deploy_dir)
            log_error("Failed to start application services on the server.")
            return False
        url = f"https://{domain}" if domain else (f"http://{ip}" if ip else None)
        print_plain("")
        log_success("Slim put completed successfully!")
        if url:
            log_success(f"🌐 Your site is available at: {url}")
        print_plain("")
        _cleanup_deploy_dir(deploy_dir)
        return True

    # ──────────────────────────────────────────────────────────────────
    # Internal: assemble deploy directory
    # ──────────────────────────────────────────────────────────────────
    def _assemble_deploy_dir(
        self,
        branch_name: str,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
        debug: bool = False,
        use_staging_certificates: bool = False,
    ) -> Optional[Path]:
        """Build a flat deploy directory ready to rsync.

        Layout:
          deploy_dir/
          ├── code/              # worktree source code
          ├── caddy/
          │   ├── docker-compose.yml   # global caddy compose
          │   ├── Caddyfile
          │   ├── caddy-docker-monitor.py
          │   └── caddy-dynamic-config.py  (if exists)
          ├── docker-compose.yml # worktree compose (renamed from worktree yml)
          ├── .env               # merged env with overrides applied
          └── volumes/           # (populated later if needed)
        """
        log_info("Assembling deploy directory...")
        deploy_dir = Path(tempfile.mkdtemp(prefix=f"ilha-deploy-{branch_name}-"))

        try:
            # Locate worktree using config (respects fractal worktree_dir setting)
            worktree_dir = get_worktree_dir()
            worktree_path = self.project_root / worktree_dir / branch_name
            if not worktree_path.exists():
                log_error(f"Worktree directory not found: {worktree_path}")
                return None

            worktree_ilha = worktree_path / ".ilha"
            worktree_compose = worktree_ilha / "docker-compose.worktree.yml"
            if not worktree_compose.exists():
                log_error(f"Worktree compose file not found: {worktree_compose}")
                return None

            # ── code/ ────────────────────────────────────────────────
            code_dir = deploy_dir / "code"
            log_info(f"Copying worktree code from {worktree_path}...")
            shutil.copytree(
                worktree_path, code_dir,
                ignore=shutil.ignore_patterns(
                    ".git", "__pycache__", "*.pyc", ".tox",
                    "node_modules", ".venv", "venv",
                ),
            )
            log_success(f"Code copied ({_dir_size_mb(code_dir):.1f} MB)")

            # ── docker-compose.yml (app) ─────────────────────────────
            shutil.copy2(worktree_compose, deploy_dir / "docker-compose.yml")
            log_info("Copied worktree compose file")

            # Apply domain/IP to Caddy labels in compose
            if domain or ip:
                self._apply_compose_overrides(
                    deploy_dir / "docker-compose.yml", domain=domain, ip=ip
                )

            # ── .env (merged) ────────────────────────────────────────
            self._build_merged_env(
                deploy_dir, worktree_path, branch_name,
                domain=domain, ip=ip, debug=debug,
                use_staging_certificates=use_staging_certificates,
            )

            # ── caddy/ ──────────────────────────────────────────────
            caddy_dir = deploy_dir / "caddy"
            caddy_dir.mkdir()
            self._assemble_caddy_files(caddy_dir, deploy_dir / ".env")

            # ── volumes/ (placeholder) ───────────────────────────────
            (deploy_dir / "volumes").mkdir()

            log_success(f"Deploy directory assembled: {deploy_dir}")
            return deploy_dir

        except Exception as exc:
            log_error(f"Failed to assemble deploy directory: {exc}")
            import traceback
            log_error(traceback.format_exc())
            shutil.rmtree(deploy_dir, ignore_errors=True)
            return None

    # ──────────────────────────────────────────────────────────────────
    # Internal: compose overrides (Caddy labels + network)
    # ──────────────────────────────────────────────────────────────────
    def _apply_compose_overrides(
        self, compose_path: Path,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> None:
        """Update Caddy labels and network config in the compose file."""
        try:
            import yaml

            data = yaml.safe_load(compose_path.read_text())
            if not data:
                return
            ensure_caddy_labels_and_network(data, domain=domain, ip=ip)
            compose_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
            log_info(f"Applied {'domain' if domain else 'IP'} override to compose labels")
        except ImportError:
            log_warning("PyYAML not available — skipping compose label override")
        except Exception as exc:
            log_warning(f"Could not update compose labels: {exc}")

    # ──────────────────────────────────────────────────────────────────
    # Internal: merged .env
    # ──────────────────────────────────────────────────────────────────
    def _build_merged_env(
        self,
        deploy_dir: Path,
        worktree_path: Path,
        branch_name: str,
        domain: Optional[str] = None,
        ip: Optional[str] = None,
        debug: bool = False,
        use_staging_certificates: bool = False,
    ) -> None:
        """Build a single .env file by merging project .env + worktree env.ilha,
        then applying domain/IP overrides.

        Critical for fractal compatibility:
        - Sets PROJECT_ROOT to the server-side code path so that
          ``${PROJECT_ROOT}:/app`` bind mounts and ``${PROJECT_ROOT}/.ilha/env.ilha``
          env_file references in the worktree compose file resolve correctly.
        - Also writes the same overrides into the code copy's env.ilha, because
          the compose file loads ``${PROJECT_ROOT}/.ilha/env.ilha`` *after*
          ``${PROJECT_ROOT}/.env``, and the env.ilha values would otherwise
          overwrite the production overrides with localhost defaults.
        """
        from ...utils.env_loader import load_env_file
        from ...utils.path_utils import get_env_compose_file_path

        REMOTE_CODE_DIR = f"{self.REMOTE_APP_ROOT}/code"

        merged: dict = {}

        # Layer 1: project root .env
        root_env = self.project_root / ".env"
        if root_env.exists():
            merged.update(load_env_file(root_env))

        # Layer 2: worktree .ilha/env.ilha (overrides root .env)
        env_ilha = get_env_compose_file_path(worktree_path)
        if env_ilha.exists():
            merged.update(load_env_file(env_ilha))

        # Layer 3: deployment overrides
        # PROJECT_ROOT must point to the code directory on the server so that
        # ${PROJECT_ROOT}:/app bind mounts and ${PROJECT_ROOT}/.env env_file
        # references in docker-compose.worktree.yml resolve correctly.
        merged["PROJECT_ROOT"] = REMOTE_CODE_DIR

        if domain:
            from ...core.dns_manager import get_base_domain
            base_domain = get_base_domain(domain)
            merged["SITE_DOMAIN"] = f"https://{domain}"
            merged["ALLOWED_HOSTS"] = f"localhost,127.0.0.1,{domain},*.{base_domain},web"
            merged["USE_X_FORWARDED_HOST"] = "True"
            merged["SECURE_PROXY_SSL_HEADER"] = "HTTP_X_FORWARDED_PROTO,https"
            # Django settings split CSRF_TRUSTED_ORIGINS by comma, not space
            csrf = f"https://{domain},http://{domain}"
            if base_domain:
                csrf += f",https://*.{base_domain}"
            merged["CSRF_TRUSTED_ORIGINS"] = csrf
            # Vite allowedHosts
            merged["VITE_ALLOWED_HOSTS"] = f"{domain},*.{base_domain},localhost,127.0.0.1"
            if "CADDY_EMAIL" not in merged:
                merged["CADDY_EMAIL"] = f"admin@{domain}"
        elif ip:
            merged["SITE_DOMAIN"] = f"http://{ip}"
            merged["ALLOWED_HOSTS"] = f"localhost,127.0.0.1,{ip},web"
            merged["USE_X_FORWARDED_HOST"] = "True"
            # Comma-separated for Django's split(',')
            merged["CSRF_TRUSTED_ORIGINS"] = f"http://{ip}"

        merged["DEBUG"] = "True" if debug else "False"
        merged["BUILD_MODE"] = "prod"

        if use_staging_certificates:
            merged["USE_STAGING_CERTIFICATES"] = "1"

        # Fallback: worktree compose often expects DATABASE_URL; synthesize from POSTGRES_* if missing
        if "DATABASE_URL" not in merged or not merged["DATABASE_URL"]:
            user = merged.get("POSTGRES_USER", "biuser")
            password = merged.get("POSTGRES_PASSWORD", "bipassword")
            db = merged.get("POSTGRES_DB", "business_intelligence")
            merged["DATABASE_URL"] = f"postgres://{user}:{password}@db:5432/{db}"

        # ── Write top-level .env (used by ``docker compose --env-file`` and sourced by scripts) ──
        # Quote values so shell `source .env` does not treat space-separated parts as commands
        def _env_line(key: str, value: str) -> str:
            safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
            return f'{key}="{safe}"'

        env_path = deploy_dir / ".env"
        lines = [_env_line(k, v) for k, v in merged.items()]
        env_path.write_text("\n".join(lines) + "\n")
        log_info(f"Merged .env written ({len(merged)} vars)")

        # ── Also write .env inside code/ (referenced by compose env_file) ──
        code_env = deploy_dir / "code" / ".env"
        code_env.write_text("\n".join(lines) + "\n")

        # ── Update the env.ilha inside code/.ilha/ (loaded *second* by compose,
        #    so it must not revert production overrides to localhost defaults) ──
        code_env_ilha = deploy_dir / "code" / ".ilha" / "env.ilha"
        if code_env_ilha.exists():
            code_env_ilha.write_text("\n".join(lines) + "\n")
            log_info("Updated code/.ilha/env.ilha with production overrides")

    # ──────────────────────────────────────────────────────────────────
    # Internal: Caddy files
    # ──────────────────────────────────────────────────────────────────
    def _assemble_caddy_files(self, caddy_dir: Path, env_path: Path) -> None:
        """Copy Caddy configuration files and generate a resolved compose file."""
        script_dir = get_script_dir()
        config_dir = script_dir / "config"

        # Caddyfile
        caddyfile_src = config_dir / "Caddyfile.ilha"
        if caddyfile_src.exists():
            shutil.copy2(caddyfile_src, caddy_dir / "Caddyfile")

        # Monitor script
        monitor_src = script_dir / "scripts" / "caddy-docker-monitor.py"
        if monitor_src.exists():
            shutil.copy2(monitor_src, caddy_dir / "caddy-docker-monitor.py")

        # Dynamic config script (optional)
        dynamic_src = script_dir / "scripts" / "caddy-dynamic-config.py"
        if dynamic_src.exists():
            shutil.copy2(dynamic_src, caddy_dir / "caddy-dynamic-config.py")

        # Build a concrete caddy compose (resolve path placeholders)
        caddy_compose_template = config_dir / "docker-compose.global-caddy.yml"
        if caddy_compose_template.exists():
            content = caddy_compose_template.read_text()
            # Replace path placeholders with absolute server-side paths
            content = content.replace(
                "{CADDYFILE_PATH}",
                f"{self.REMOTE_CADDY_DIR}/Caddyfile",
            )
            content = content.replace(
                "{MONITOR_SCRIPT_PATH}",
                f"{self.REMOTE_CADDY_DIR}/caddy-docker-monitor.py",
            )
            (caddy_dir / "docker-compose.yml").write_text(content)
            log_info("Caddy compose file assembled with server-side paths")

    # ──────────────────────────────────────────────────────────────────
    # Internal: volume backup / restore
    # ──────────────────────────────────────────────────────────────────
    def _backup_volumes_locally(self, branch_name: str, deploy_dir: Path) -> Optional[Path]:
        """Backup worktree Docker volumes into deploy_dir/volumes/.

        Returns path to the tar file, or None if no volumes backed up.
        """
        from ...core.docker_manager import DockerManager

        docker_mgr = DockerManager(project_root=self.project_root)
        env_mgr = EnvironmentManager(project_root=self.project_root)
        volume_names = env_mgr.get_worktree_volume_names(branch_name)

        if not volume_names:
            log_info("No volumes to backup")
            return None

        volumes_dir = deploy_dir / "volumes"
        volumes_dir.mkdir(exist_ok=True)
        tar_path = volumes_dir / f"backup_{branch_name}.tar"

        log_info(f"Backing up {len(volume_names)} volume(s)...")
        try:
            backup_path = docker_mgr.backup_volumes(branch_name, volumes_dir)
            if backup_path is not None and backup_path.exists():
                size_mb = backup_path.stat().st_size / 1024 / 1024
                log_success(f"Volumes backed up ({size_mb:.1f} MB)")
                return backup_path
            else:
                log_warning("Volume backup returned no data")
                return None
        except Exception as exc:
            log_warning(f"Volume backup failed: {exc}")
            return None

    def _restore_volumes_remote(self, username: str, server: str, branch_name: str) -> bool:
        """Restore volumes on the remote server using docker run + alpine."""
        log_info("Restoring volumes on remote server...")

        # Backup format: gzipped tar (tar czf) containing volume_name.tar.gz members.
        # Each member is a gzipped tar of that volume's contents.
        script = f"""#!/bin/bash
set -euo pipefail
BACKUP_TAR="{self.REMOTE_VOLUMES_DIR}/backup_{branch_name}.tar"
if [ ! -f "$BACKUP_TAR" ]; then
    echo "No volume backup found at $BACKUP_TAR, skipping restore"
    exit 0
fi
echo "Restoring volumes from $BACKUP_TAR..."
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
tar xzf "$BACKUP_TAR" -C "$TMPDIR"
for f in "$TMPDIR"/*.tar.gz; do
    [ -f "$f" ] || continue
    vol=$(basename "$f" .tar.gz)
    echo "  Restoring volume: $vol"
    docker volume create "$vol" >/dev/null 2>&1 || true
    docker run --rm -v "$vol":/data -v "$TMPDIR":/backup alpine \\
        sh -c "cd /data && tar xzf /backup/$(basename "$f") -C ."
done
echo "Volume restore complete"
"""
        cmd = self.ssh.build_ssh_command(username, server, f"bash -s")
        try:
            result = subprocess.run(
                cmd, input=script, capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                log_success("Volumes restored on server")
                for line in result.stdout.splitlines():
                    if line.strip():
                        log_info(f"  {line}")
                return True
            else:
                log_warning(f"Volume restore returned exit code {result.returncode}")
                if result.stderr:
                    for line in result.stderr.splitlines():
                        log_warning(f"  {line}")
                return False
        except Exception as exc:
            log_warning(f"Volume restore failed: {exc}")
            return False

    # ──────────────────────────────────────────────────────────────────
    # Internal: server prep (slim)
    # ──────────────────────────────────────────────────────────────────
    def _prepare_server_slim(self, username: str, server: str) -> bool:
        """Run the slim server preparation script (Docker-only)."""
        log_info("Running slim server preparation (Docker + firewall only)...")
        add_ssh_host_key(server)

        script_path = get_script_dir() / "scripts" / "server_prep_slim.sh"
        if not script_path.exists():
            log_error(f"Slim prep script not found: {script_path}")
            return False

        script = script_path.read_text()
        exec_cmd = "cat > /tmp/dtprep_slim.sh && chmod +x /tmp/dtprep_slim.sh && /tmp/dtprep_slim.sh && rm -f /tmp/dtprep_slim.sh"
        ssh_cmd = ["ssh", f"{username}@{server}",
                   "-o", "StrictHostKeyChecking=accept-new",
                   "-o", "ConnectTimeout=10",
                   "bash", "-lc", exec_cmd]

        success, stdout_lines, stderr_lines = execute_with_streaming(
            ssh_cmd, script=script, timeout=None, progress_interval=15, prefix="  "
        )

        if not success:
            log_error("Slim server preparation failed")
            if stderr_lines:
                for line in stderr_lines:
                    log_error(f"  {line}")
            return False

        log_success("Slim server preparation complete")
        return True

    # ──────────────────────────────────────────────────────────────────
    # Internal: rsync
    # ──────────────────────────────────────────────────────────────────
    def _rsync_to_server(self, deploy_dir: Path, username: str, server: str) -> bool:
        """rsync the deploy directory to the server."""
        log_info(f"Transferring deploy directory to {server}:{self.REMOTE_APP_ROOT}/ ...")

        # Ensure remote directory exists
        mkdir_cmd = self.ssh.build_ssh_command(
            username, server, f"mkdir -p {self.REMOTE_APP_ROOT}"
        )
        subprocess.run(mkdir_cmd, capture_output=True, timeout=15)

        # rsync with compression
        cmd = [
            "rsync", "-avz", "--progress", "--delete",
            "-e", "ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10",
            f"{deploy_dir}/",
            f"{username}@{server}:{self.REMOTE_APP_ROOT}/",
        ]
        log_info(f"Running rsync ({_dir_size_mb(deploy_dir):.1f} MB)...")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for line in iter(process.stdout.readline, ""):
            line = line.rstrip()
            if "%" in line or "speedup" in line.lower():
                log_info(f"  {line}")
        process.wait()

        if process.returncode != 0:
            log_error(f"rsync failed (exit {process.returncode})")
            return False

        log_success("Transfer complete")
        return True

    def _rsync_volumes_to_server(self, deploy_dir: Path, username: str, server: str) -> bool:
        """Rsync only deploy_dir/volumes/ to REMOTE_VOLUMES_DIR on the server."""
        volumes_src = deploy_dir / "volumes"
        if not volumes_src.exists():
            log_warning("No volumes directory to sync")
            return True
        log_info(f"Transferring volumes to {server}:{self.REMOTE_VOLUMES_DIR}/ ...")
        mkdir_cmd = self.ssh.build_ssh_command(
            username, server, f"mkdir -p {self.REMOTE_VOLUMES_DIR}"
        )
        subprocess.run(mkdir_cmd, capture_output=True, timeout=15)
        cmd = [
            "rsync", "-avz", "--progress",
            "-e", "ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10",
            f"{volumes_src}/",
            f"{username}@{server}:{self.REMOTE_VOLUMES_DIR}/",
        ]
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if process.returncode != 0:
            log_error(f"rsync volumes failed (exit {process.returncode})")
            if process.stderr:
                for line in process.stderr.splitlines():
                    if line.strip():
                        log_warning(f"  {line}")
            return False
        log_success("Volumes transfer complete")
        return True

    # ──────────────────────────────────────────────────────────────────
    # Internal: remote start commands
    # ──────────────────────────────────────────────────────────────────
    def _stop_app_remote(self, username: str, server: str) -> bool:
        """Stop application services on the server so volumes can be safely restored."""
        log_info("Stopping application services on server...")
        self.ssh.ensure_host_key(server)
        script = f"""#!/bin/bash
set -euo pipefail
cd {self.REMOTE_APP_ROOT}
docker compose --env-file .env down --remove-orphans 2>&1 || true
echo "App services stopped"
"""
        exec_cmd = "cat > /tmp/stop_app.sh && chmod +x /tmp/stop_app.sh && /tmp/stop_app.sh && rm -f /tmp/stop_app.sh"
        ssh_cmd = self.ssh.build_ssh_command(username, server, exec_cmd)
        success, _, stderr_lines = execute_with_streaming(
            ssh_cmd, script=script, timeout=120, progress_interval=15, prefix="  "
        )
        if success:
            log_success("App services stopped on server")
            return True
        log_warning("App stop had issues")
        for line in (stderr_lines or [])[-10:]:
            if line.strip():
                log_warning(f"  {line}")
        return False

    def _start_caddy_remote(self, username: str, server: str) -> bool:
        """Start global Caddy proxy on the server."""
        log_info("Starting Caddy proxy on server...")
        self.ssh.ensure_host_key(server)
        script = f"""#!/bin/bash
set -euo pipefail
cd {self.REMOTE_CADDY_DIR}

# Pass env vars from app .env for CADDY_EMAIL, USE_STAGING_CERTIFICATES
set -a
[ -f {self.REMOTE_APP_ROOT}/.env ] && source {self.REMOTE_APP_ROOT}/.env
set +a

docker compose -p ilha-proxy up -d --force-recreate 2>&1
echo "Caddy proxy started"
"""
        exec_cmd = "cat > /tmp/start_caddy.sh && chmod +x /tmp/start_caddy.sh && /tmp/start_caddy.sh && rm -f /tmp/start_caddy.sh"
        ssh_cmd = self.ssh.build_ssh_command(username, server, exec_cmd)
        success, stdout_lines, stderr_lines = execute_with_streaming(
            ssh_cmd, script=script, timeout=120, progress_interval=15, prefix="  "
        )
        if success:
            log_success("Caddy proxy started on server")
            return True
        else:
            log_warning("Caddy start failed")
            for line in stderr_lines[-20:]:
                if line.strip():
                    log_warning(f"  {line}")
            return False

    def _run_nohup_remote(
        self,
        username: str,
        server: str,
        script: str,
        script_path: str,
        log_file: str,
        label: str,
        success_markers: List[str],
        max_wait: int = 1800,
        debug: bool = False,
    ) -> bool:
        """Upload script, launch via nohup, poll until done. Two-step pattern avoids stdin+nohup failure."""
        # Step 1: upload script (synchronous, no nohup — reliable)
        upload_cmd = self.ssh.build_ssh_command(
            username, server, f"cat > {script_path} && chmod +x {script_path}"
        )
        if debug:
            log_info(f"  [debug] {label} upload SSH: {' '.join(upload_cmd)}")
        try:
            up = subprocess.run(
                upload_cmd,
                input=script,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            log_error(f"Failed to upload {label} script (SSH timeout)")
            return False
        if up.returncode != 0:
            log_error(f"Failed to upload {label} script: {up.stderr}")
            return False
        # Step 2: launch via nohup (no stdin)
        launch_cmd = self.ssh.build_ssh_command(
            username, server,
            f"nohup bash -c '{script_path}; echo EXIT_CODE=$?' >> {log_file} 2>&1 & echo $!",
        )
        if debug:
            log_info(f"  [debug] {label} launch SSH: {' '.join(launch_cmd)}")
        try:
            launch = subprocess.run(
                launch_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            log_error(f"Failed to start remote {label} (SSH timeout)")
            return False
        if launch.returncode != 0:
            log_error(f"Failed to start remote {label}: {launch.stderr}")
            return False
        pid = launch.stdout.strip()
        if not pid.isdigit():
            log_error(f"Failed to get PID from remote command: {launch.stdout}")
            return False
        log_info(f"{label} running on server (PID {pid}), log: {log_file}")
        log_info("Monitoring progress (this may take several minutes)...")
        # Step 3: poll
        poll_interval = 5
        elapsed = 0
        last_line_count = 0
        grep_parts = " || ".join(f"grep -q '{m}' {log_file}" for m in success_markers)
        success_check = f"({grep_parts}) && echo SUCCESS || echo FAILED"
        is_build = "Build" in label
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval
            check_cmd = self.ssh.build_ssh_command(
                username, server,
                f"ps -p {pid} > /dev/null 2>&1 && echo RUNNING || echo FINISHED",
            )
            check_result = subprocess.run(
                check_cmd, capture_output=True, text=True, timeout=10,
            )
            process_status = (check_result.stdout or "").strip()
            wc_cmd = self.ssh.build_ssh_command(
                username, server,
                f"wc -l {log_file} 2>/dev/null | awk '{{print $1}}' || echo 0",
            )
            wc_result = subprocess.run(wc_cmd, capture_output=True, text=True, timeout=10)
            try:
                current_line_count = int((wc_result.stdout or "0").strip().split()[0])
            except (ValueError, IndexError):
                current_line_count = 0
            if current_line_count > last_line_count:
                lines_to_show = current_line_count - last_line_count
                tail_cmd = self.ssh.build_ssh_command(
                    username, server, f"tail -n {lines_to_show} {log_file}",
                )
                tail_result = subprocess.run(tail_cmd, capture_output=True, text=True, timeout=10)
                for line in (tail_result.stdout or "").splitlines():
                    s = line.strip()
                    if s == "EXIT_CODE=0":
                        log_info(f"  {label} script completed successfully.")
                    elif s.startswith("EXIT_CODE="):
                        log_info(f"  {label} script exited with code {s.split('=', 1)[1]}.")
                    elif s:
                        log_info(f"  {line}")
                last_line_count = current_line_count
            if process_status == "FINISHED":
                log_info(f"{label} process finished")
                success_cmd = self.ssh.build_ssh_command(username, server, success_check)
                success_result = subprocess.run(
                    success_cmd, capture_output=True, text=True, timeout=10,
                )
                if (success_result.stdout or "").strip() == "SUCCESS":
                    if is_build:
                        log_success("Docker images built on server")
                    return True
                if is_build:
                    log_warning(f"{label} failed - check log for details")
                else:
                    log_error(f"{label} failed - check log for details")
                self._log_tail_and_return_false(username, server, log_file, label)
                return False
            if elapsed % 60 == 0:
                log_info(f"Still running... ({elapsed}s elapsed)")
        if is_build:
            log_warning(f"{label} timed out after {max_wait}s")
        else:
            log_error(f"{label} timed out after {max_wait}s")
        log_error(f"Check progress: ssh {username}@{server} 'tail -f {log_file}'")
        self._log_tail_and_return_false(username, server, log_file, label)
        return False

    def _build_remote(self, username: str, server: str, debug: bool = False) -> bool:
        """Build Docker images on the server via nohup + poll (avoids SSH timeout on long builds)."""
        log_info("Building Docker images on server (this may take a while)...")
        self.ssh.ensure_host_key(server)
        script = f"""#!/bin/bash
set -euo pipefail
cd {self.REMOTE_APP_ROOT}

# Load env vars
set -a
[ -f .env ] && source .env
set +a

docker compose --env-file .env build 2>&1
echo "Build complete"
"""
        return self._run_nohup_remote(
            username, server, script,
            script_path="/tmp/ilha-build.sh",
            log_file="/tmp/ilha-build.log",
            label="Build",
            success_markers=["Build complete", "EXIT_CODE=0"],
            max_wait=1800,
            debug=debug,
        )

    def _start_app_remote(self, username: str, server: str, build: bool = False, debug: bool = False) -> bool:
        """Start application services on the server via nohup + poll.

        Script is uploaded then launched (two-step pattern). When build=True, uses
        docker compose up -d --build; otherwise up -d only.
        """
        log_info("Starting application services on server...")
        self.ssh.ensure_host_key(server)
        app_root = self.REMOTE_APP_ROOT
        up_build = "docker compose --env-file .env up -d --build 2>&1" if build else "docker compose --env-file .env up -d 2>&1"
        script = f"""#!/bin/bash
set -euo pipefail
cd {app_root}

docker compose --env-file .env down --remove-orphans 2>&1 || true
{up_build}
WEB_CID=$(docker compose --env-file .env ps -q web 2>/dev/null | head -1)
[ -n "$WEB_CID" ] && docker network connect ilha_caddy_proxy "$WEB_CID" 2>/dev/null || true
echo "Application services started"
docker compose --env-file .env ps 2>&1
"""
        ok = self._run_nohup_remote(
            username, server, script,
            script_path="/tmp/ilha-start-app.sh",
            log_file="/tmp/ilha-start-app.log",
            label="App start",
            success_markers=["EXIT_CODE=0"],
            max_wait=1800,
            debug=debug,
        )
        if not ok:
            return False
        # Verify at least one app container is running
        count_cmd = self.ssh.build_ssh_command(
            username, server,
            f"cd {app_root} && docker compose --env-file .env ps --status running -q 2>/dev/null | wc -l",
        )
        count_result = subprocess.run(count_cmd, capture_output=True, text=True, timeout=10)
        try:
            running_count = int((count_result.stdout or "0").strip().split()[0])
        except (ValueError, IndexError):
            running_count = 0
        if running_count < 1:
            log_error("App start reported success but no app containers are running")
            self._log_tail_and_return_false(username, server, "/tmp/ilha-start-app.log", "App start")
            return False
        log_success("Application services started on server")
        return True

    def _log_tail_and_return_false(
        self, username: str, server: str, log_file: str, label: str
    ) -> None:
        """Log last 30 lines of remote log file (DRY for app start / build failure paths)."""
        final_cmd = self.ssh.build_ssh_command(username, server, f"tail -n 30 {log_file}")
        final_result = subprocess.run(final_cmd, capture_output=True, text=True, timeout=10)
        for line in (final_result.stdout or "").splitlines():
            s = line.strip()
            if s == "EXIT_CODE=0":
                log_error(f"  {label} script completed successfully (see log for details).")
            elif s.startswith("EXIT_CODE="):
                log_error(f"  {label} script exited with code {s.split('=', 1)[1]}.")
            elif s:
                log_error(f"  {line}")

    # ──────────────────────────────────────────────────────────────────
    # Internal: DNS
    # ──────────────────────────────────────────────────────────────────
    def _handle_dns(
        self, domain: str, server: str,
        dns_token: Optional[str] = None,
        droplet_info: Optional[DropletInfo] = None,
    ) -> bool:
        """Create / update DNS record for the domain."""
        try:
            if not is_domain(domain):
                log_error(f"Invalid domain: {domain}")
                return False

            subdomain, base_domain = parse_domain(domain)
            api_token = DNSManager.resolve_dns_token(dns_token)
            if not api_token:
                log_error("DNS API token not found")
                return False

            provider = DNSManager.create_provider("digitalocean", api_token)
            if not provider:
                return False

            # Resolve target IP
            import re, socket
            target_ip = server
            if droplet_info and droplet_info.ip_address:
                target_ip = droplet_info.ip_address
            elif not re.match(r"^\d+\.\d+\.\d+\.\d+$", server):
                try:
                    target_ip = socket.gethostbyname(server)
                except socket.gaierror:
                    pass

            exists, current_ip = provider.check_domain_exists(subdomain, base_domain)
            if exists and current_ip == target_ip:
                log_success(f"DNS record already correct: {domain} -> {target_ip}")
                return True
            elif exists:
                log_info(f"Updating DNS: {domain} -> {target_ip}")
                return provider.update_subdomain(subdomain, base_domain, target_ip)
            else:
                log_info(f"Creating DNS: {domain} -> {target_ip}")
                return provider.create_subdomain(subdomain, base_domain, target_ip)
        except Exception as exc:
            log_error(f"DNS management error: {exc}")
            return False

    # ──────────────────────────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────────────────────────
    def cleanup(self):
        """Release SSH connections."""
        self.ssh.cleanup()
        self.transfer.cleanup()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _dir_size_mb(path: Path) -> float:
    """Rough size of a directory in megabytes."""
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / 1024 / 1024
