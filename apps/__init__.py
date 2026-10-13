from .base import AppAdapter, OperationCatalog, OperationSpec
from .bilibili import BilibiliAdapter
from .huawei_browser import HuaweiBrowserAdapter
from .wps import WpsAdapter

__all__ = [
    "AppAdapter",
    "BilibiliAdapter",
    "HuaweiBrowserAdapter",
    "OperationCatalog",
    "OperationSpec",
    "WpsAdapter",
]
