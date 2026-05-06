"""YAML helpers for Docker Compose-specific tags."""

from __future__ import annotations

from typing import Iterable, TypeVar

import yaml


T = TypeVar("T")


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
