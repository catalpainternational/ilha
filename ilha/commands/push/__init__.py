"""Deploy commands for ilha. Slim-deploy is the only remaining pipeline.

Imports are lazy so ``ilha.commands.push.remote_layout`` (and other light
modules) can be used without pulling SlimDeployManager into the import graph.
"""

from __future__ import annotations

from typing import Any

__all__ = ["RemoteDiskManager", "TransferManager", "SlimDeployManager"]


def __getattr__(name: str) -> Any:
    if name == "SlimDeployManager":
        from .slim_deploy_manager import SlimDeployManager

        return SlimDeployManager
    if name == "RemoteDiskManager":
        from .remote_disk import RemoteDiskManager

        return RemoteDiskManager
    if name == "TransferManager":
        from .transfer_manager import TransferManager

        return TransferManager
    raise AttributeError(f"module {__name__!r} has no attribute {name}")
