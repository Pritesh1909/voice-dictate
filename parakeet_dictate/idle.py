"""When the PC is idle: the conditions that let background learning run, and the Windows calls behind them."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass

# Own DLL instances, like winutil: the argtypes set here don't affect other modules.
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wt.BOOL
kernel32.GetTickCount64.argtypes = []
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
kernel32.GetSystemPowerStatus.restype = wt.BOOL

_TICK_MASK = 0xFFFFFFFF            # GetLastInputInfo's tick is the 32-bit one and wraps every 49.7 days
_AC_OFFLINE = 0                    # battery; 1 = plugged in, 255 = unknown


def seconds_since_input() -> float:
    """Seconds since the last key press or mouse movement, anywhere on this desktop session. 0.0 if Windows
    can't say, so an unknown never looks like a long idle."""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    elapsed_ms = ((kernel32.GetTickCount64() & _TICK_MASK) - info.dwTime) & _TICK_MASK
    return elapsed_ms / 1000.0


def on_mains_power() -> bool:
    """True when plugged in. Unknown (a desktop with no battery, or the call failing) counts as mains."""
    status = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return True
    return status.ACLineStatus != _AC_OFFLINE


@dataclass
class Conditions:
    enabled: bool          # learning is switched on in Settings
    teacher_ready: bool    # the Whisper download has finished
    app_idle: bool         # no dictation or other work going on inside the app
    idle_seconds: float    # seconds since the user's last input
    idle_needed: float     # seconds of no input required before the checker may run
    mains: bool
    gpu_failed: bool       # a gpu-failed.json marker exists
    teacher_on_cpu: bool   # Whisper loaded on the CPU instead of a discrete graphics card


def why_not(c: Conditions) -> str | None:
    """None when the checker may run; otherwise a short reason for the Settings status line (the first one
    that applies)."""
    if not c.enabled:
        return "Off"
    if not c.teacher_ready:
        return "Waiting for Whisper"
    if c.gpu_failed or c.teacher_on_cpu:
        return "Paused: no graphics card"
    if not c.mains:
        return "Paused: on battery"
    if not c.app_idle or c.idle_seconds < c.idle_needed:
        return "Waits until the PC is idle"
    return None
