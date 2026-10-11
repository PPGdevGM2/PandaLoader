"""Detect being started inside another app's file system container.

Programs started by a packaged Windows app (MSIX, e.g. a Store app or a desktop
app installed that way) inherit its container: files written under AppData land
in that app's private folder (AppData\\Local\\Packages\\<app>\\LocalCache\\...)
instead of the user's real one. The program reads and writes there happily, but
the next start from Explorer or the Start Menu sees the real, stale files: the
settings look reset. Directory paths hide this; only a file shows its true
location, so the check writes a probe file and asks Windows where it went.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

RELAUNCH_WINDOW_S = 30.0  # a relaunch marker younger than this means "already tried once"


def _final_path(path: Path) -> Optional[str]:
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    h = k.CreateFileW(str(path), 0, 7, None, 3, 0x02000000, None)  # any access, share all, OPEN_EXISTING, backup semantics
    if h in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = k.GetFinalPathNameByHandleW(h, buf, 32768, 0)
        return buf.value if 0 < n < 32768 else None
    finally:
        k.CloseHandle(h)


def _strip_prefix(p: str) -> str:
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    return p[4:] if p.startswith("\\\\?\\") else p


def redirected_folder(folder) -> Optional[str]:
    """Where files written into `folder` really end up, when that is somewhere else; else None.
    Never use Path.resolve() for the comparison: it follows the redirection itself."""
    if os.name != "nt":
        return None
    folder = Path(os.path.abspath(folder))
    probe = folder / f".pandaloader-probe-{os.getpid()}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        final = _final_path(probe)
    except OSError:
        return None
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    if not final:
        return None
    real = _strip_prefix(final)
    if os.path.normcase(real) == os.path.normcase(str(probe)):
        return None
    return str(Path(real).parent)


def container_name(redirected: str) -> str:
    """'Claude_pzs8sxrjxfjjc' out of ...\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\..., else ''."""
    m = re.search(r"\\Packages\\([^\\]+)\\", redirected, re.IGNORECASE)
    return m.group(1) if m else ""


def relaunch_outside(marker_dir) -> bool:
    """Start this exe again through Explorer, which runs it outside any container.
    Only for the frozen exe without arguments (Explorer cannot pass any), and only
    once: a fresh marker next to the exe means the last attempt did not help."""
    if not getattr(sys, "frozen", False) or len(sys.argv) > 1:
        return False
    marker = Path(marker_dir) / ".pandaloader-relaunch"
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < RELAUNCH_WINDOW_S:
            marker.unlink()
            return False
        marker.write_text(str(time.time()), encoding="ascii")
    except OSError:
        return False
    try:
        subprocess.Popen(["explorer.exe", sys.executable], close_fds=True)
    except OSError:
        return False
    return True
