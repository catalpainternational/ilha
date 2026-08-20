"""Pin compose services to published registry image refs.

Workers and slim deploy both need the same rewrite: drop ``build:`` and set
``image:`` from the branch manifest. Keep that mutation in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence

import yaml

from ilha.commands.image import ImageManager
from ilha.exceptions import ilhaCommandError
from ilha.utils.compose_yaml import register_compose_yaml_tags

register_compose_yaml_tags()


def pin_service_to_image_ref(service_cfg: MutableMapping[str, Any], image_ref: str) -> None:
    """Replace a service's ``build:`` with a pinned registry ``image:``."""
    service_cfg.pop("build", None)
    service_cfg["image"] = image_ref


def pin_compose_mapping(
    compose_data: MutableMapping[str, Any],
    image_refs: Mapping[str, str],
) -> List[str]:
    """Pin services that exist in both *compose_data* and *image_refs*.

    Returns the service names that were rewritten.
    """
    services = compose_data.get("services")
    if not isinstance(services, dict):
        return []
    pinned: List[str] = []
    for name, image_ref in image_refs.items():
        svc = services.get(name)
        if isinstance(svc, dict):
            pin_service_to_image_ref(svc, image_ref)
            pinned.append(name)
    return pinned


def dump_compose_yaml(compose_data: Mapping[str, Any]) -> str:
    """Serialize compose YAML with ilha's usual dump options."""
    data = dict(compose_data)
    if data.get("version") is None:
        data.pop("version", None)
    return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)


def load_compose_yaml(path: Path) -> Dict[str, Any]:
    """Load a compose file without expanding ``${VAR}`` (preserve source form)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ilhaCommandError(f"Failed to parse compose file {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def manifest_image_refs(
    image_manager: ImageManager,
    branch_name: str,
    services: Iterable[str],
    *,
    prefer_digest: bool = True,
) -> Dict[str, str]:
    """Resolve registry refs for *services* from the published manifest."""
    refs: Dict[str, str] = {}
    for service in services:
        name = str(service or "").strip()
        if not name:
            continue
        refs[name] = image_manager.image_ref_for_service(
            branch_name,
            name,
            prefer_digest=prefer_digest,
        )
    return refs


def pin_compose_file(
    compose_path: Path,
    image_refs: Mapping[str, str],
) -> List[str]:
    """Rewrite *compose_path* in place. Returns pinned service names."""
    data = load_compose_yaml(compose_path)
    pinned = pin_compose_mapping(data, image_refs)
    if pinned:
        compose_path.write_text(dump_compose_yaml(data), encoding="utf-8")
    return pinned


def pin_deploy_compose_files(
    compose_paths: Sequence[Path],
    branch_name: str,
    *,
    image_manager: ImageManager,
    services: Sequence[str],
    prefer_digest: bool = True,
) -> Dict[str, str]:
    """Pin every service in *services* across *compose_paths*.

    Raises if a required service is missing from the manifest.
    """
    image_refs = manifest_image_refs(
        image_manager,
        branch_name,
        services,
        prefer_digest=prefer_digest,
    )
    for path in compose_paths:
        if path.exists():
            pin_compose_file(path, image_refs)
    return image_refs
