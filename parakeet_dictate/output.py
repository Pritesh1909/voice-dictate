"""Deliver text to the window that has focus: Unicode keystrokes (default) or clipboard paste."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x2, 0x4
VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN, VK_V = 0x11, 0x12, 0x10, 0x5B, 0x5C, 0x56
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x2


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.OpenClipboard.argtypes = [wt.HWND]
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]


def modifiers_down() -> bool:
    return any(user32.GetAsyncKeyState(k) & 0x8000 for k in (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN))


def wait_modifiers_released(timeout: float = 2.0) -> None:
    """Typing while Ctrl/Alt are still held would turn letters into shortcuts."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not modifiers_down():
            return
        time.sleep(0.02)


def _send(events: list[INPUT]) -> None:
    for i in range(0, len(events), 200):
        chunk = events[i:i + 200]
        arr = (INPUT * len(chunk))(*chunk)
        if user32.SendInput(len(chunk), arr, ctypes.sizeof(INPUT)) != len(chunk):
            raise OSError(f"SendInput blocked (error {ctypes.get_last_error()}); is the target window elevated?")
        time.sleep(0.005)


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, u=_U(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)))


def type_text(text: str) -> None:
    data = text.encode("utf-16-le")
    events: list[INPUT] = []
    for i in range(0, len(data), 2):
        unit = int.from_bytes(data[i:i + 2], "little")
        events.append(_key(scan=unit, flags=KEYEVENTF_UNICODE))
        events.append(_key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    _send(events)


def _open_clipboard() -> bool:
    for _ in range(20):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def _get_clipboard_text() -> str | None:
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT) or not _open_clipboard():
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _set_clipboard_text(text: str) -> None:
    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, buf, size)
    kernel32.GlobalUnlock(h)
    if not _open_clipboard():
        kernel32.GlobalFree(h)
        raise OSError("clipboard is busy")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            raise OSError("could not set clipboard")
    finally:
        user32.CloseClipboard()


def paste_text(text: str) -> None:
    previous = _get_clipboard_text()
    _set_clipboard_text(text)
    _send([_key(VK_CONTROL), _key(VK_V), _key(VK_V, flags=KEYEVENTF_KEYUP), _key(VK_CONTROL, flags=KEYEVENTF_KEYUP)])
    time.sleep(0.4)  # let the target read the clipboard before restoring it
    if previous is not None:
        _set_clipboard_text(previous)


def deliver(text: str, method: str) -> None:
    wait_modifiers_released()
    (paste_text if method == "paste" else type_text)(text)


def copy_to_clipboard(text: str) -> None:
    _set_clipboard_text(text)


class PhraseTyper:
    """Types finished phrases in order while the user keeps talking.

    Nothing is typed while a modifier key is held (hold-to-talk keeps Ctrl down, and letters typed then
    would become shortcuts); those phrases wait and go out together once the keys are released.
    Phrases are joined with one space, and each delivery ends with a space when trailing_space is on.
    """

    def __init__(self, clean, method: str = "type", trailing_space: bool = True, send=None, mods=None):
        self._clean = clean
        self.method = method
        self.trailing_space = trailing_space
        self._send = send or deliver
        self._mods = mods or modifiers_down
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._typed: list[str] = []
        self.cancelled = False

    def add(self, raw: str) -> None:
        text = self._clean(raw)
        if text and not self.cancelled:
            with self._lock:
                self._pending.append(text)

    @property
    def pending(self) -> bool:
        return bool(self._pending) and not self.cancelled

    @property
    def text(self) -> str:
        """Everything typed so far."""
        return " ".join(self._typed)

    def pump(self, wait: bool = False) -> None:
        """Type what is waiting. wait=False skips it while a modifier is held; wait=True (end of the
        dictation) gives the keys up to 2 s to come up, as a whole-dictation delivery does."""
        with self._lock:
            if self.cancelled or not self._pending or (not wait and self._mods()):
                return
            chunk = " ".join(self._pending)
            lead = " " if self._typed and not self.trailing_space else ""
            self._send(lead + chunk + (" " if self.trailing_space else ""), self.method)
            self._typed += self._pending
            self._pending = []

    def cancel(self) -> None:
        """Stop typing. Phrases already typed stay where they are."""
        self.cancelled = True
