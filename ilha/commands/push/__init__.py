"""
Deploy commands for ilha. Slim-deploy is the only remaining pipeline.
"""

from .remote_disk import RemoteDiskManager
from .transfer_manager import TransferManager
from .slim_deploy_manager import SlimDeployManager

__all__ = ['RemoteDiskManager', 'TransferManager', 'SlimDeployManager']

