"""Deliver text to the window that has focus: Unicode keystrokes (default) or clipboard paste.
When it can't be typed (no window has focus, or Windows blocks the keystrokes) the caller is told,
so it can put the text on the clipboard instead of losing it."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading
import time

from . import commands

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x2, 0x4
VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN, VK_V = 0x11, 0x12, 0x10, 0x5B, 0x5C, 0x56
VK_RETURN, VK_BACK = 0x0D, 0x08
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
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]


# The desktop and the taskbar: focus sits there when no app window has it, and keystrokes sent there are lost.
_NOT_TARGETS = ("Progman", "WorkerW", "Shell_TrayWnd")
NO_TARGET_NOTE = "No window had focus, so the text was copied to the clipboard."
BLOCKED_NOTE = "Couldn't type into that window, so the text was copied to the clipboard."


class DeliveryError(OSError):
    """The text didn't reach a window. no_target: nothing to type into (rather than typing being blocked)."""

    def __init__(self, message: str, no_target: bool = False):
        super().__init__(message)
        self.no_target = no_target


def _foreground() -> tuple[int, int, str]:
    """(handle, owning process id, window class) of the window that has focus; handle 0 if none."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0, 0, ""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return hwnd, pid.value, buf.value


def has_typing_target() -> bool:
    """False only when typing surely goes nowhere: no focused window, the desktop or taskbar has focus,
    or one of our own windows does (after a tray menu click the tray's hidden window is in front)."""
    hwnd, pid, cls = _foreground()
    return bool(hwnd) and pid != os.getpid() and cls not in _NOT_TARGETS


def wait_for_target(timeout: float) -> bool:
    """Give the user a moment to click back into a window (used after a tray menu click)."""
    end = time.monotonic() + timeout
    while not has_typing_target():
        if time.monotonic() >= end:
            return False
        time.sleep(0.1)
    return True


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
    """Type or paste text where the cursor is. Raises DeliveryError when it can't get there."""
    wait_modifiers_released()
    if not has_typing_target():
        raise DeliveryError("no window to type into", no_target=True)
    try:
        (paste_text if method == "paste" else type_text)(text)
    except OSError as e:
        raise DeliveryError(str(e)) from e


def copy_instead(text: str, err: OSError) -> str:
    """Typing failed: put the text on the clipboard so it isn't lost. Returns the note to show the user."""
    _set_clipboard_text(text)
    return NO_TARGET_NOTE if getattr(err, "no_target", False) else BLOCKED_NOTE


def press_enter() -> None:
    """One Enter after the text (e.g. to send a chat message), with the same wait for Ctrl/Alt as the text."""
    wait_modifiers_released()
    _send([_key(VK_RETURN), _key(VK_RETURN, flags=KEYEVENTF_KEYUP)])


def backspace(count: int) -> None:
    """Press Backspace `count` times ("scratch that"). Only called in type mode, into the same window."""
    if count <= 0:
        return
    wait_modifiers_released()
    if not has_typing_target():
        raise DeliveryError("no window to type into", no_target=True)
    try:
        _send([_key(VK_BACK), _key(VK_BACK, flags=KEYEVENTF_KEYUP)] * count)
    except OSError as e:
        raise DeliveryError(str(e)) from e


def copy_to_clipboard(text: str) -> None:
    _set_clipboard_text(text)


def _bare(word: str) -> str:
    """A word without the punctuation the model puts after it, for comparing two provisional reads."""
    return word.rstrip(".,;:!?…")


def _agree(a: list[str], b: list[str]) -> int:
    """How many words at the start of a and b are the same ("admin." at the end of one read is "admin"
    in the next)."""
    n = 0
    for x, y in zip(a, b):
        if _bare(x) != _bare(y):
            break
        n += 1
    return n


class PhraseTyper:
    """Types finished phrases in order while the user keeps talking.

    Nothing is typed while a modifier key is held (hold-to-talk keeps Ctrl down, and letters typed then
    would become shortcuts); those phrases wait and go out together once the keys are released.
    Phrases are joined with one space, and each delivery ends with a space when trailing_space is on.

    With a command matcher, a phrase that is just a spoken command is carried out instead of typed:
    line breaks are typed as newline characters, "scratch that" takes back the previous phrase with
    Backspace (only when can_backspace() says the same window is in front and the method is "type";
    otherwise a phrase already typed is left alone), and "stop listening" calls on_stop. What each phrase
    put on screen is kept in a commands.Transcript, so the Backspace count is exact.

    If typing fails (no window to type into, or Windows blocks it), what was typed stays typed and this
    phrase and every later one are collected in `untyped` instead, for the app to put on the clipboard.
    """

    def __init__(self, clean, method: str = "type", trailing_space: bool = True, send=None, mods=None,
                 matcher=None, backspace_keys=None, can_backspace=None, on_stop=None,
                 stream: bool = False, may_continue=None):
        self._clean = clean
        # Live streaming: words of the phrase still being spoken are typed once two provisional reads
        # agree on them (see preview()). Only when typing keystrokes, never when pasting.
        self.stream = stream and method == "type"
        self._may_continue = may_continue or (lambda text: False)
        self._live = ""                 # characters of the open phrase already on screen
        self._live_words: list[str] = []
        self._prev_words: list[str] = []
        self.tentative = ""             # the open phrase's words not typed yet (for the overlay)
        self.method = method
        self.trailing_space = trailing_space
        self._send = send or deliver
        self._mods = mods or modifiers_down
        self._matcher = matcher
        self._backspace = backspace_keys or backspace
        self._can_backspace = can_backspace or (lambda: False)
        self._on_stop = on_stop or (lambda: None)
        self._lock = threading.Lock()
        self._typed = commands.Transcript(trailing_space)
        self._rest = commands.Transcript(trailing_space)   # what couldn't be typed, for the clipboard
        self._backspaces = 0                                # owed by "scratch that", sent before more text
        self.failed: OSError | None = None
        self.cancelled = False
        self.stopped = False

    def _backspace_ok(self) -> bool:
        return self.method == "type" and self.failed is None and bool(self._can_backspace())

    def preview(self, raw: str, stable: str | None = None) -> None:
        """A provisional read of the phrase still being spoken (live streaming).

        Parakeet v2 and v3 read a whole phrase at a time, so the app re-reads the open phrase about twice a
        second. A word is typed once two reads in a row agree on it and it isn't the last word (the
        last one is still being said). A model that streams says itself which words it won't change
        (`stable`, the start of `raw`): those are typed without waiting for a second read, and so is
        any word two updates in a row agree on (the model settles words a second or two late). Words that could still become part of a dictionary term, and a
        phrase that could be a spoken command, wait. Words typed are never taken back here: when the
        phrase ends, add() corrects them against the final read."""
        if not self.stream or self.cancelled or self.stopped or self.failed is not None:
            return
        words = self._clean(raw).split()
        with self._lock:
            agree = _agree(self._prev_words, words)
            if stable is not None:
                agree = max(agree, _agree(self._clean(stable).split(), words))
            self._prev_words = words
            n = min(agree, len(words) - 1)
            if self._matcher and self._matcher.could_start(" ".join(words)):
                n = 0
            while n > len(self._live_words) and self._may_continue(" ".join(words[:n])):
                n -= 1
            done = len(self._live_words)
            if [_bare(w) for w in words[:done]] != [_bare(w) for w in self._live_words]:
                n = done            # a typed word changed: the final read will fix it
            self.tentative = " ".join(words[done:])
            if n <= done or self._typed.pending or self._backspaces or self._mods():
                return
            new = words[done:n]
            chunk = (" " if done else self._typed.lead_for_text()) + " ".join(new)
            try:
                self._send(chunk, self.method)
            except OSError as e:
                self._fail(e)
                return
            self._live += chunk
            self._live_words += new
            self.tentative = " ".join(words[n:])

    def _end_live(self) -> str:
        live = self._live
        self._live, self._live_words, self._prev_words, self.tentative = "", [], [], ""
        return live

    def add(self, raw: str) -> None:
        text = self._clean(raw)
        with self._lock:
            live = self._end_live()
            if live and (not text or self.cancelled or self.failed is not None
                         or (self._matcher and self._matcher.match(text))):
                # Streamed words that turned out to be nothing (or a command): take them back if safe.
                if self._backspace_ok() and not self.cancelled:
                    self._backspaces += len(live)
                live = ""
        if not text or self.cancelled or self.stopped:
            return
        action = self._matcher.match(text) if self._matcher else None
        if action == commands.STOP:
            self.stopped = True
            self._on_stop()
            return
        with self._lock:
            target = self._typed if self.failed is None else self._rest
            if action is None:
                target.add_text(text, live=live if target is self._typed else "")
            elif action == commands.NEWLINE:
                target.add_break(1)
            elif action == commands.PARAGRAPH:
                target.add_break(2)
            elif action == commands.UNDO:
                self._backspaces += target.undo(target is self._typed and self._backspace_ok())

    @property
    def pending(self) -> bool:
        return not self.cancelled and (self._typed.pending or self._backspaces > 0)

    @property
    def text(self) -> str:
        """Everything typed so far."""
        return self._typed.render(sent_only=True)

    @property
    def untyped(self) -> str:
        """Phrases that couldn't be typed (empty unless typing failed)."""
        return self._rest.text

    def _fail(self, e: OSError) -> None:
        log.warning("typing failed, the rest of this dictation goes to the clipboard: %s", e)
        self.failed = e
        self._backspaces = 0
        for kind, text in self._typed.take_unsent():
            if kind == "text":
                self._rest.add_text(text)
            else:
                self._rest.add_break(len(text))

    def pump(self, wait: bool = False) -> None:
        """Type what is waiting. wait=False skips it while a modifier is held; wait=True (end of the
        dictation) gives the keys up to 2 s to come up, as a whole-dictation delivery does."""
        with self._lock:
            if self.cancelled or self.failed is not None or not self.pending:
                return
            if not wait and self._mods():
                return
            bs, chunk, shares = self._typed.plan(self._backspace_ok())
            try:
                if self._backspaces + bs:
                    self._backspace(self._backspaces + bs)
                self._backspaces = 0
                if chunk:
                    self._send(chunk, self.method)
            except OSError as e:
                self._fail(e)
                return
            self._typed.commit(bs, shares)

    def cancel(self) -> None:
        """Stop typing. Phrases already typed stay where they are."""
        self.cancelled = True
