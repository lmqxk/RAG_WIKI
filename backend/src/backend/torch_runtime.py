"""Windows 下为 pip 安装的 PyTorch CUDA wheel 注册 DLL 搜索目录。"""

from __future__ import annotations

import ctypes
import os
import sysconfig
from pathlib import Path

_DLL_HANDLES: list[object] = []


def prepare_torch_runtime() -> None:
    """在导入 torch 前调用，避免 CUDA wheel 的依赖 DLL 加载失败。"""
    if _DLL_HANDLES or not hasattr(os, "add_dll_directory"):
        return
    site_packages = Path(sysconfig.get_paths()["purelib"])
    directories = [site_packages / "torch" / "lib"]
    directories.extend(sorted((site_packages / "nvidia").glob("*/bin")))
    for directory in directories:
        if directory.is_dir():
            _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
    torch_shm = site_packages / "torch" / "lib" / "shm.dll"
    if torch_shm.exists():
        _DLL_HANDLES.append(ctypes.WinDLL(str(torch_shm)))
