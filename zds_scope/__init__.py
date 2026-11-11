"""
ZLG ZDS3000 / ZDS4000 Plus 示波器专业上位机
"""

from .protocol import (
    WfmHeadInfo,
    WfmItemInfo,
    WfmFile,
    ChannelWaveformData,
    parse_wfm,
    pack_wfm,
    unpack_screen_image,
    ScopeCommands
)
from .driver import ZDScopeDriver, ScopeCommunicationError
from .storage import ScopeStorage

__version__ = "1.0.0"
