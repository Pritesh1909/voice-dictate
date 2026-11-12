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
    "apps": 0x5D, "numlock": 0x90, "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E,
    "divide": 0x6F, "oem102": 0xE2, **{f"num{i}": 0x60 + i for i in range(10)},
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


# --- naming keys for the Settings window's shortcut recorder ---------------------------------------

MOD_ORDER = ("ctrl", "alt", "shift", "win")
MODIFIER_VKS = frozenset({0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})
_CANONICAL = {"escape": "esc", "backtick": "`", "control": "ctrl"}
_VK_NAME = {vk: name for name, vk in _NAMED.items() if name not in _CANONICAL}
_DISPLAY = {"pageup": "PageUp", "pagedown": "PageDown", "scrolllock": "ScrollLock", "capslock": "CapsLock",
            "numlock": "NumLock", "apps": "Menu", "oem102": "<>", "multiply": "Num *", "add": "Num +",
            "subtract": "Num -", "decimal": "Num .", "divide": "Num /", "esc": "Esc",
            **{f"num{i}": f"Num {i}" for i in range(10)}}


def vk_name(vk: int) -> str | None:
    """The spec name of a virtual key ("space", "q", "f5"), or None for keys a hotkey can't use."""
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk).lower()
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x6F}"
    return _VK_NAME.get(vk)


def spec_for(mods, vk: int) -> str:
    """Modifier names + a virtual key -> the [hotkey] keys spec, e.g. ({"alt", "ctrl"}, 0x20) -> "ctrl+alt+space"."""
    name = vk_name(vk)
    if name is None:
        raise HotkeyError(f"that key (code {vk}) can't be used in a shortcut")
    mods = {_CANONICAL.get(m, m) for m in mods}
    return "+".join([m for m in MOD_ORDER if m in mods] + [name])


def display(spec: str) -> str:
    """"ctrl+alt+space" -> "Ctrl+Alt+Space" (the spec itself when it can't be read)."""
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        return spec
    out = [_CANONICAL.get(p, p).capitalize() for p in parts[:-1]]
    key = _CANONICAL.get(parts[-1], parts[-1])
    out.append(_DISPLAY.get(key) or (key.upper() if len(key) == 1 or key[1:].isdigit() else key.capitalize()))
    return "+".join(out)


# Combinations Windows keeps for itself, or that would break everyday typing if taken over everywhere.
RESERVED = {
    "alt+tab": "switches windows", "alt+shift+tab": "switches windows", "alt+esc": "switches windows",
    "alt+f4": "closes windows", "alt+space": "opens the window menu", "ctrl+esc": "opens Start",
    "ctrl+shift+esc": "opens Task Manager", "ctrl+alt+delete": "is the security screen",
    "f12": "is kept for debuggers", "win+l": "locks the PC", "win+d": "shows the desktop",
    "win+e": "opens File Explorer", "win+r": "opens Run", "win+tab": "opens Task View",
    "win+x": "opens the Start menu's context menu", "win+i": "opens Settings", "win+a": "opens Quick Settings",
    "win+s": "opens Search", "win+v": "opens the clipboard history", "win+.": "opens the emoji panel",
    "win+space": "switches the keyboard layout", "win+m": "minimises all windows", "win+p": "switches displays",
    "win+h": "starts voice typing", "win+g": "opens the Game Bar", "win+k": "opens Cast",
    "win+u": "opens Accessibility", "win+n": "opens notifications",
    "ctrl+c": "copies", "ctrl+v": "pastes", "ctrl+x": "cuts", "ctrl+z": "undoes", "ctrl+y": "redoes",
    "ctrl+a": "selects all", "ctrl+s": "saves",
}


def check_combo(spec: str) -> str | None:
    """Why `spec` can't be the dictation shortcut, or None when it is fine (availability not checked)."""
    try:
        mods, vk = parse(spec)
    except HotkeyError as e:
        return str(e)
    canon = spec_for({m for m, bit in MOD.items() if mods & bit}, vk)
    if canon in RESERVED:
        return f"{display(canon)} {RESERVED[canon]} in Windows. Pick another shortcut."
    if mods == MOD["shift"] and not (0x70 <= vk <= 0x87 or vk in (0x13, 0x91)):
        return f"{display(canon)} types a character. Add Ctrl, Alt or Win."
    return None


def available(spec: str) -> bool:
    """True if no other program holds `spec` (tries to register it on this thread, then lets it go)."""
    mods, vk = parse(spec)
    probe = 0xBFFE
    if not user32.RegisterHotKey(None, probe, mods | MOD_NOREPEAT, vk):
        return False
    user32.UnregisterHotKey(None, probe)
    return True


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
