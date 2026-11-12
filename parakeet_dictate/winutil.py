"""Small Windows helpers: single instance, sounds, Start-up/Start-menu shortcuts, process memory."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import os
import struct
import subprocess
import sys
import wave
import winsound
from pathlib import Path

from . import APP_ID, APP_NAME

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
ERROR_ALREADY_EXISTS = 183
_mutex = None

PROJECT_DIR = Path(__file__).resolve().parent.parent


def single_instance() -> bool:
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP_ID}")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


# --- sounds ---------------------------------------------------------------------------------------

def _tone_wav(path: Path, freqs: list[float], ms: int = 70, vol: float = 0.25) -> None:
    sr = 22050
    frames = bytearray()
    for f in freqs:
        n = int(sr * ms / 1000)
        for i in range(n):
            env = min(1.0, i / 200, (n - i) / 200)  # click-free fade
            frames += struct.pack("<h", int(32767 * vol * env * math.sin(2 * math.pi * f * i / sr)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


class Sounds:
    def __init__(self, folder: Path):
        self.files = {"start": folder / "start.wav", "stop": folder / "stop.wav", "cancel": folder / "cancel.wav",
                      "error": folder / "error.wav"}
        specs = {"start": [660, 990], "stop": [990, 660], "cancel": [440, 330], "error": [300, 300, 300]}
        for k, p in self.files.items():
            if not p.exists():
                _tone_wav(p, specs[k])
        self.enabled = True

    def play(self, name: str) -> None:
        if self.enabled:
            winsound.PlaySound(str(self.files[name]), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


# --- shortcuts ------------------------------------------------------------------------------------

def _pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def startup_shortcut() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{APP_NAME}.lnk"


def start_menu_shortcut() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"


def create_shortcut(lnk: Path, icon: Path) -> None:
    if getattr(sys, "frozen", False):  # installed build: ParakeetDictate.exe
        target, args, workdir, icon_loc = sys.executable, "", str(Path(sys.executable).parent), sys.executable
    else:                              # source checkout with a venv
        target, args, workdir, icon_loc = _pythonw(), "-m parakeet_dictate", str(PROJECT_DIR), str(icon)
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:PD_LNK);"
        "$s.TargetPath=$env:PD_TARGET;$s.Arguments=$env:PD_ARGS;"
        "$s.WorkingDirectory=$env:PD_DIR;$s.IconLocation=$env:PD_ICON;"
        "$s.Description='Offline dictation with Parakeet v2';$s.Save()"
    )
    env = dict(os.environ, PD_LNK=str(lnk), PD_TARGET=target, PD_ARGS=args, PD_DIR=workdir, PD_ICON=icon_loc)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)


# --- memory ---------------------------------------------------------------------------------------

class _PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
        (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                       "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                       "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


kernel32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]


def working_set_mb() -> float:
    c = _PMC()
    c.cb = ctypes.sizeof(c)
    kernel32.K32GetProcessMemoryInfo(ctypes.c_void_p(-1), ctypes.byref(c), c.cb)
    return c.WorkingSetSize / 2**20


def trim_working_set() -> None:
    """Hand idle pages back to Windows after a dictation (the tray process mostly sleeps)."""
    try:
        ctypes.windll.psapi.EmptyWorkingSet(ctypes.c_void_p(-1))
    except OSError:
        pass


# --- foreground program ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)   # own instance: argtypes here don't affect other modules
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def foreground_exe() -> str:
    """File name of the program whose window is in front ("Code.exe"), or "" if it can't be told
    (no window, or a program running as administrator)."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            size = wt.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
        finally:
            kernel32.CloseHandle(h)
        return buf.value.rsplit("\\", 1)[-1]
    except (OSError, ctypes.ArgumentError):
        return ""
