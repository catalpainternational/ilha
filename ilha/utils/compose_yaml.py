"""YAML helpers for Docker Compose-specific tags."""

from __future__ import annotations

from typing import Iterable, List, TypeVar

import yaml


T = TypeVar("T")

# Single source of truth for the compose files every ilha deploy targets: the
# user's base compose plus ilha's generated overlay. Previously this pair was
# duplicated in four places (slim_deploy_manager,
# remote_compose_probe, deploy_utils) — and had already drifted into a list in
# one place and a string in the others. Route all of them through here.
COMPOSE_FILES: tuple[str, ...] = ("docker-compose.yml", "docker-compose.overlay.yml")


def compose_flags_list() -> List[str]:
    """Return ``-f <file>`` flags as an argv list (for subprocess list form)."""
    flags: List[str] = []
    for name in COMPOSE_FILES:
        flags.extend(["-f", name])
    return flags


def compose_flags_str() -> str:
    """Return ``-f <file>`` flags as a single string (for remote shell commands)."""
    return " ".join(compose_flags_list())


class ComposeReset(list[T]):
    """Sequence rendered with Docker Compose's ``!reset`` tag."""


def compose_reset(values: Iterable[T] | None = None) -> ComposeReset[T]:
    """Return a list that Docker Compose treats as a reset override."""
    return ComposeReset(values or [])


def _construct_reset(loader: yaml.Loader, node: yaml.Node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


def _represent_reset(dumper: yaml.Dumper, data: ComposeReset):
    return dumper.represent_sequence("!reset", list(data), flow_style=True)


def register_compose_yaml_tags() -> None:
    """Teach PyYAML the Docker Compose tags ilha emits."""
    for loader in (yaml.SafeLoader, yaml.FullLoader, yaml.Loader):
        loader.add_constructor("!reset", _construct_reset)

    for dumper in (yaml.SafeDumper, yaml.Dumper):
        dumper.add_representer(ComposeReset, _represent_reset)


register_compose_yaml_tags()
