"""Where the user dragged the pill to ("Move the pill…" in the tray menu), kept across restarts.

The position is saved in %LOCALAPPDATA%\\ParakeetDictate\\pill_position.json as the monitor's device
name (\\\\.\\DISPLAY2) and the pill's place inside that monitor's work area (the screen without the
taskbar): as fractions of the room the pill can move in (0 = left/top edge, 1 = right/bottom edge), so
a resolution or display-scaling change keeps it at the same spot, and in pixels, for reference. When
that monitor isn't connected, the pill goes back to its usual place ([overlay] position and margin).

Everything here except the few Win32 wrappers at the bottom is plain arithmetic, tested without a screen.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

Rect = tuple[int, int, int, int]      # left, top, right, bottom (right/bottom exclusive, like RECT)

VERSION = 1


@dataclass(frozen=True)
class Monitor:
    device: str                       # MONITORINFOEXW.szDevice, e.g. "\\\\.\\DISPLAY1"
    work: Rect                        # its work area


@dataclass(frozen=True)
class SavedPosition:
    device: str
    fx: float                         # 0..1 across the free room of the work area
    fy: float
    x: int                            # pixels from the work area's left / top (informational)
    y: int


def _clamp01(v: float) -> float:
    return min(1.0, max(0.0, v))


def clamp(x: int, y: int, w: int, h: int, work: Rect) -> tuple[int, int]:
    """(x, y) moved just enough that a w×h pill lies fully inside `work` (its top-left corner when the
    pill is bigger than the work area)."""
    left, top, right, bottom = work
    x = max(left, min(x, right - w))
    y = max(top, min(y, bottom - h))
    return x, y


def to_relative(device: str, x: int, y: int, w: int, h: int, work: Rect) -> SavedPosition:
    """Absolute screen position of the pill's top-left corner -> saved form (clamped into `work` first)."""
    x, y = clamp(x, y, w, h, work)
    left, top, right, bottom = work
    room_x, room_y = right - left - w, bottom - top - h
    fx = (x - left) / room_x if room_x > 0 else 0.0
    fy = (y - top) / room_y if room_y > 0 else 0.0
    return SavedPosition(device, round(_clamp01(fx), 5), round(_clamp01(fy), 5), x - left, y - top)


def to_absolute(pos: SavedPosition, w: int, h: int, work: Rect) -> tuple[int, int]:
    """Saved form -> screen position of the pill's top-left corner, fully inside `work`."""
    left, top, right, bottom = work
    room_x, room_y = max(0, right - left - w), max(0, bottom - top - h)
    x = left + round(_clamp01(pos.fx) * room_x)
    y = top + round(_clamp01(pos.fy) * room_y)
    return clamp(x, y, w, h, work)


def find_monitor(device: str, monitors: Iterable[Monitor]) -> Monitor | None:
    want = device.lower()
    return next((m for m in monitors if m.device.lower() == want), None)


def placement(pos: SavedPosition | None, monitors: Iterable[Monitor], w: int, h: int) -> tuple[int, int] | None:
    """Where to show a w×h pill: the saved spot on the saved monitor, or None (no saved position, or that
    monitor isn't connected) for the usual place."""
    if pos is None:
        return None
    mon = find_monitor(pos.device, monitors)
    if mon is None:
        return None
    return to_absolute(pos, w, h, mon.work)


# --- the file -------------------------------------------------------------------------------------

def load(path: Path) -> SavedPosition | None:
    """The saved position, or None when there is none or the file is broken (then it is just ignored)."""
    try:
        d = json.loads(Path(path).read_text("utf-8"))
        device, fx, fy = d["device"], d["fx"], d["fy"]
        x, y = d.get("x", 0), d.get("y", 0)
        if not (isinstance(device, str) and device):
            return None
        nums = (fx, fy, x, y)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in nums):
            return None
        if not all(v == v and abs(v) != float("inf") for v in nums):   # NaN / infinity
            return None
        return SavedPosition(device, _clamp01(float(fx)), _clamp01(float(fy)), int(x), int(y))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def save(path: Path, pos: SavedPosition, work: Rect | None = None) -> None:
    """Write atomically (a temporary file renamed over the old one), so a crash never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    d = {"version": VERSION, "device": pos.device, "fx": pos.fx, "fy": pos.fy, "x": pos.x, "y": pos.y}
    if work is not None:
        d["work_area"] = list(work)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1), "utf-8")
    os.replace(tmp, path)


def reset(path: Path) -> bool:
    """Forget the saved position. True when there was one."""
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False


# --- Win32 (only used on the overlay's Tk thread) ---------------------------------------------------

def _user32():
    import ctypes
    import ctypes.wintypes as wt
    global _U32
    if _U32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)   # own instance: argtypes don't leak elsewhere
        u.MonitorFromWindow.restype = wt.HANDLE
        u.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        u.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
        u.GetMonitorInfoW.restype = wt.BOOL
        u.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.c_void_p, ctypes.c_void_p, wt.LPARAM]
        u.EnumDisplayMonitors.restype = wt.BOOL
        u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        _U32 = u
    return _U32


_U32 = None
MONITOR_DEFAULTTONEAREST = 2


def _info(hmon) -> Monitor | None:
    import ctypes
    import ctypes.wintypes as wt

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD),
                    ("szDevice", wt.WCHAR * 32)]

    mi = MONITORINFOEXW()
    mi.cbSize = ctypes.sizeof(mi)
    if not hmon or not _user32().GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    r = mi.rcWork
    return Monitor(mi.szDevice, (r.left, r.top, r.right, r.bottom))


def monitors() -> list[Monitor]:
    """Every connected monitor with its work area."""
    import ctypes
    import ctypes.wintypes as wt
    out: list[Monitor] = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def cb(hmon, _hdc, _rect, _lp):
        m = _info(hmon)
        if m:
            out.append(m)
        return True

    fn = proc(cb)                     # kept alive for the whole call
    _user32().EnumDisplayMonitors(None, None, ctypes.cast(fn, ctypes.c_void_p), 0)
    return out


def monitor_of_window(hwnd) -> Monitor | None:
    """The monitor showing most of the window (the nearest one when it is off every screen)."""
    return _info(_user32().MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))


def cursor_pos() -> tuple[int, int]:
    import ctypes
    import ctypes.wintypes as wt
    pt = wt.POINT()
    _user32().GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def window_rect(hwnd) -> Rect:
    import ctypes
    import ctypes.wintypes as wt
    r = wt.RECT()
    _user32().GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom
