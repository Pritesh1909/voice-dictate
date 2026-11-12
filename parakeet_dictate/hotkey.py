"""Global hotkey via Win32 RegisterHotKey (no keyboard hook, no admin rights, zero CPU while waiting)."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT, WM_APP = 0x0312, 0x0012, 0x8000
VK_ESCAPE = 0x1B
_NAMED = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "pause": 0x13, "scrolllock": 0x91, "capslock": 0x14, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "`": 0xC0, "backtick": 0xC0, ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE,
    "/": 0xBF, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
_ID_MAIN, _ID_ESC = 1, 2
_MSG_ESC_ON, _MSG_ESC_OFF, _MSG_REREGISTER = WM_APP + 1, WM_APP + 2, WM_APP + 3

user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


class HotkeyError(Exception):
    pass


def parse(spec: str) -> tuple[int, int]:
    """'ctrl+alt+space' -> (modifiers, virtual key)."""
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("empty hotkey")
    mods, key = 0, parts[-1]
    for p in parts[:-1]:
        if p not in MOD:
            raise HotkeyError(f"unknown modifier '{p}' in '{spec}' (use ctrl, alt, shift, win)")
        mods |= MOD[p]
    if key in MOD:
        raise HotkeyError(f"'{spec}' has no main key after the modifiers")
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key in _NAMED:
        vk = _NAMED[key]
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    else:
        raise HotkeyError(f"unknown key '{key}' in '{spec}'")
    if mods == 0 and not (0x70 <= vk <= 0x87 or vk in (0x13, 0x91)):
        raise HotkeyError(f"'{spec}' needs a modifier (ctrl/alt/shift/win) unless it is F1-F24, Pause or ScrollLock")
    return mods, vk


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class HotkeyThread(threading.Thread):
    """Owns the registrations and a message loop; callbacks run on this thread, so keep them short."""

    def __init__(self, spec: str, on_press, on_cancel):
        super().__init__(daemon=True, name="hotkey")
        self.mods, self.vk = parse(spec)
        self.spec = spec
        self._on_press, self._on_cancel = on_press, on_cancel
        self._tid = 0
        self._started = threading.Event()
        self.error: str | None = None
        self._pending: tuple[str, int, int] | None = None
        self._reply = threading.Event()

    def _register(self, mods: int, vk: int) -> str | None:
        if user32.RegisterHotKey(None, _ID_MAIN, mods | MOD_NOREPEAT, vk):
            return None
        err = ctypes.get_last_error()
        return (f"The hotkey {self.spec} is already used by another program (error {err}). "
                "Pick another one in config.toml [hotkey] keys.")

    def run(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create the message queue
        self.error = self._register(self.mods, self.vk)
        self._started.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                if msg.wParam == _ID_MAIN:
                    self._on_press()
                elif msg.wParam == _ID_ESC:
                    self._on_cancel()
            elif msg.message == _MSG_ESC_ON:
                user32.RegisterHotKey(None, _ID_ESC, MOD_NOREPEAT, VK_ESCAPE)
            elif msg.message == _MSG_ESC_OFF:
                user32.UnregisterHotKey(None, _ID_ESC)
            elif msg.message == _MSG_REREGISTER and self._pending:
                spec, mods, vk = self._pending
                user32.UnregisterHotKey(None, _ID_MAIN)
                old = (self.spec, self.mods, self.vk)
                self.spec, self.mods, self.vk = spec, mods, vk
                self.error = self._register(mods, vk)
                if self.error:  # keep the old one working
                    self.spec, self.mods, self.vk = old
                    self._register(old[1], old[2])
                self._reply.set()
        user32.UnregisterHotKey(None, _ID_ESC)
        user32.UnregisterHotKey(None, _ID_MAIN)

    def wait_started(self, timeout: float = 5) -> str | None:
        self._started.wait(timeout)
        return self.error

    def _post(self, m: int) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, m, 0, 0)

    def escape_on(self) -> None:
        self._post(_MSG_ESC_ON)

    def escape_off(self) -> None:
        self._post(_MSG_ESC_OFF)

    def change(self, spec: str) -> str | None:
        """Switch to a new hotkey; returns an error and keeps the old one if it can't be registered."""
        if spec == self.spec:
            return None
        mods, vk = parse(spec)
        self._pending = (spec, mods, vk)
        self._reply.clear()
        self._post(_MSG_REREGISTER)
        if threading.get_ident() == self.ident:
            return None  # called from our own callback: applied once the callback returns
        self._reply.wait(3)
        return self.error

    def stop(self) -> None:
        self._post(WM_QUIT)
