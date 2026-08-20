"""Image build/push/publish CLI commands."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import click

from ilha.cli.helpers import add_json_option, add_verbose_option, command_wrapper
from ilha.commands.droplets import auto_detect_branch_name
from ilha.commands.image import ImageManager, ImageOptions
from ilha.exceptions import ilhaCommandError
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import log_success, print_plain


def _shared_image_options(func):
    func = click.option("--save", is_flag=True, default=False, help="Persist provided image preferences to .ilha/config.yml")(func)
    func = click.option("--service", "services", multiple=True, help="Compose service to process; can be repeated")(func)
    func = click.option("--platform", help="Build platform (default: linux/amd64)")(func)
    func = click.option("--tag", help="Image tag/template override (default: image.tag_template)")(func)
    func = click.option("--repository", help="Image repository prefix (default: project name)")(func)
    func = click.option("--registry", help="DigitalOcean Container Registry name")(func)
    func = click.option("--provider", help="Registry provider (default: digitalocean)")(func)
    return func


def _options(
    *,
    provider: Optional[str],
    registry: Optional[str],
    repository: Optional[str],
    tag: Optional[str],
    platform: Optional[str],
    services: Tuple[str, ...],
    save: bool,
    no_cache: bool = False,
) -> ImageOptions:
    return ImageOptions(
        provider=provider,
        registry=registry,
        repository=repository,
        tag=tag,
        platform=platform,
        services=services,
        save=save,
        no_cache=no_cache,
    )


def register_commands(cli) -> None:
    @cli.group()
    @add_verbose_option
    def image():
        """Build, publish, list, and destroy registry-backed Docker images."""

    @image.command("build")
    @click.argument("branch_name", required=False)
    @click.option("--no-cache", is_flag=True, default=False, help="Build without Docker layer cache")
    @_shared_image_options
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def image_build(
        branch_name: Optional[str],
        no_cache: bool,
        provider: Optional[str],
        registry: Optional[str],
        repository: Optional[str],
        tag: Optional[str],
        platform: Optional[str],
        services: Tuple[str, ...],
        save: bool,
        json: bool,
    ):
        """Build/tag local images for a worktree."""
        branch = auto_detect_branch_name(branch_name, json)
        if not branch:
            raise ilhaCommandError("Branch name is required")
        result = ImageManager().build(
            branch,
            _options(
                provider=provider,
                registry=registry,
                repository=repository,
                tag=tag,
                platform=platform,
                services=services,
                save=save,
                no_cache=no_cache,
            ),
        )
        if json:
            return JSONOutput.success("Images built", result)
        log_success("Images built")

    @image.command("push")
    @click.argument("branch_name", required=False)
    @_shared_image_options
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def image_push(
        branch_name: Optional[str],
        provider: Optional[str],
        registry: Optional[str],
        repository: Optional[str],
        tag: Optional[str],
        platform: Optional[str],
        services: Tuple[str, ...],
        save: bool,
        json: bool,
    ):
        """Push already-built images to the configured registry."""
        branch = auto_detect_branch_name(branch_name, json)
        if not branch:
            raise ilhaCommandError("Branch name is required")
        result = ImageManager().push(
            branch,
            _options(
                provider=provider,
                registry=registry,
                repository=repository,
                tag=tag,
                platform=platform,
                services=services,
                save=save,
            ),
        )
        if json:
            return JSONOutput.success("Images pushed", result)
        log_success("Images pushed")

    @image.command("publish")
    @click.argument("branch_name", required=False)
    @click.option("--no-cache", is_flag=True, default=False, help="Build without Docker layer cache")
    @_shared_image_options
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def image_publish(
        branch_name: Optional[str],
        no_cache: bool,
        provider: Optional[str],
        registry: Optional[str],
        repository: Optional[str],
        tag: Optional[str],
        platform: Optional[str],
        services: Tuple[str, ...],
        save: bool,
        json: bool,
    ):
        """Build, push, and record an image manifest."""
        branch = auto_detect_branch_name(branch_name, json)
        if not branch:
            raise ilhaCommandError("Branch name is required")
        result = ImageManager().publish(
            branch,
            _options(
                provider=provider,
                registry=registry,
                repository=repository,
                tag=tag,
                platform=platform,
                services=services,
                save=save,
                no_cache=no_cache,
            ),
        )
        if json:
            return JSONOutput.success("Images published", result)
        log_success("Images published")

    @image.command("list")
    @click.argument("branch_name", required=False)
    @click.option(
        "--remote",
        is_flag=True,
        default=False,
        help="Query DOCR via DigitalOcean API (requires DIGITALOCEAN_API_TOKEN)",
    )
    @click.option("--registry", help="DigitalOcean Container Registry name (for --remote)")
    @click.option(
        "--repository",
        help="Repository path prefix (e.g. bower/web); defaults from manifest when listing one branch",
    )
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def image_list(
        branch_name: Optional[str],
        remote: bool,
        registry: Optional[str],
        repository: Optional[str],
        json: bool,
    ):
        """List published image manifests (and optionally remote registry tags).

        \b
        Examples:
          ilha image list
          ilha image list staging --remote --registry catalpa
          ilha image list staging --json
        """
        branch = auto_detect_branch_name(branch_name, json) if branch_name else None
        mgr = ImageManager()
        manifests = mgr.list_manifests(branch)
        payload: Dict[str, Any] = {"manifests": manifests}

        if remote:
            image_cfg = mgr._load_image_config()
            reg = registry or image_cfg.get("registry")
            if not reg and manifests:
                reg = manifests[0].get("registry")
            if not reg:
                raise ilhaCommandError(
                    "Registry name required for --remote. Use --registry or set image.registry in .ilha/config.yml."
                )
            repo = repository
            if not repo and branch and manifests:
                repo_prefix = str(manifests[0].get("repository") or "").strip().strip("/")
                if repo_prefix:
                    repo = repo_prefix
            payload["remote_tags"] = mgr.list_remote_tags(str(reg), repo)

        if json:
            clean_manifests = [{k: v for k, v in m.items() if k != "_manifest_path"} for m in manifests]
            payload["manifests"] = clean_manifests
            payload["rows"] = mgr.flatten_manifest_rows(clean_manifests)
            return JSONOutput.success("Image manifests listed", payload)

        rows = mgr.flatten_manifest_rows(manifests)
        if not rows and not payload.get("remote_tags"):
            print_plain("No published image manifests found.")
            return

        if rows:
            print_plain(f"{'BRANCH':<16} {'PUBLISHED':<22} {'SERVICE':<16} {'IMAGE'}")
            for row in rows:
                published = str(row.get("published_at") or "")[:19]
                digest = row.get("digest") or ""
                image = row.get("image") or ""
                if digest:
                    image = f"{image} ({digest})"
                print_plain(
                    f"{row.get('branch', ''):<16} {published:<22} {row.get('service', ''):<16} {image}"
                )

        if payload.get("remote_tags"):
            print_plain("")
            print_plain("Remote registry tags:")
            for tag in payload["remote_tags"]:
                print_plain(f"  {tag.get('image')}")

    @image.command("destroy")
    @click.argument("branch_names", type=str, metavar="BRANCH[,BRANCH...]")
    @click.option(
        "--service",
        "services",
        multiple=True,
        help="Apply registry deletion to matching compose service(s) only",
    )
    @click.option(
        "--manifest-only",
        is_flag=True,
        default=False,
        help="Remove .ilha/images/<branch>.json only; skip DOCR tag deletion",
    )
    @click.option(
        "--registry-only",
        is_flag=True,
        default=False,
        help="Delete DOCR tags only; keep local manifest",
    )
    @click.option("--force", is_flag=True, default=False, help="Skip confirmation prompts")
    @click.option("--registry", help="Override DigitalOcean registry name for tag deletion")
    @add_json_option
    @add_verbose_option
    @command_wrapper()
    def image_destroy(
        branch_names: str,
        services: Tuple[str, ...],
        manifest_only: bool,
        registry_only: bool,
        force: bool,
        registry: Optional[str],
        json: bool,
    ):
        """Delete published images for one or more branches (comma-separated).

        \b
        Examples:
          ilha image destroy staging
          ilha image destroy staging,feature-x --force
          ilha image destroy staging --service rq-worker-1 --manifest-only
        """
        if manifest_only and registry_only:
            raise ilhaCommandError("Cannot specify both --manifest-only and --registry-only")

        branches = [b.strip() for b in branch_names.split(",") if b.strip()]
        if not branches:
            raise ilhaCommandError("At least one branch name is required")

        result = ImageManager().destroy_branches(
            branches,
            services=services or None,
            manifest_only=manifest_only,
            registry_only=registry_only,
            registry_override=registry,
            force=force,
        )
        if json:
            return JSONOutput.success("Images destroyed", result)
        log_success(f"Destroyed published images for {result['count']} branch(es)")
