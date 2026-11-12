"""Small always-on-top pill that shows Listening / Transcribing / Typed.

It never takes focus (WS_EX_NOACTIVATE), clicks go straight through it (WS_EX_TRANSPARENT), and it
appears on the monitor of the window you are typing into. When hidden it is fully transparent and
parked off-screen, and no timers run, so it costs nothing between dictations.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import threading
import time
import tkinter as tk

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
user32.GetParent.restype = wt.HWND
user32.GetParent.argtypes = [wt.HWND]
user32.GetForegroundWindow.restype = wt.HWND

GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x10, 0x40
MONITOR_DEFAULTTONEAREST = 2

KEY = "#010203"          # transparent colour key
BG = "#1d1f24"
FG = "#f2f3f5"
MUTED = "#9aa0aa"
RED, AMBER, GREEN = "#ff4b4b", "#ffb020", "#38c172"


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def _dpi_scale() -> float:
    try:
        return user32.GetDpiForSystem() / 96.0
    except AttributeError:
        return 1.0


class Overlay:
    def __init__(self, position: str = "bottom"):
        self.position = position
        self._ready = threading.Event()
        self._root: tk.Tk | None = None
        self._mode = "hidden"
        self._level_fn = lambda: 0.0
        self._writing_fn = lambda: False
        self._started = 0.0
        self._tick = 0
        self._hide_job = None
        self._anim_job = None
        threading.Thread(target=self._run, daemon=True, name="overlay").start()
        self._ready.wait(5)

    # --- public, callable from any thread -------------------------------------------------------

    def listening(self, level_fn, started_at: float, writing_fn=None) -> None:
        """writing_fn() -> True while heard speech is still being turned into text (amber dot)."""
        self._call(self._show, "listening", level_fn=level_fn, started=started_at,
                   writing_fn=writing_fn or (lambda: False))

    def working(self, text: str = "Transcribing…") -> None:
        self._call(self._show, "working", text=text)

    def flash(self, text: str, color: str = GREEN, ms: int = 900) -> None:
        self._call(self._show, "flash", text=text, color=color, ms=ms)

    def hide(self) -> None:
        self._call(self._hide)

    def close(self) -> None:
        self._call(lambda: self._root.destroy())

    # --- Tk thread ------------------------------------------------------------------------------

    def _call(self, fn, *a, **kw) -> None:
        if self._root is not None:
            try:
                self._root.after(0, lambda: fn(*a, **kw))
            except (RuntimeError, tk.TclError):
                pass

    def _run(self) -> None:
        s = _dpi_scale()
        self.s = s
        self.w, self.h = int(236 * s), int(44 * s)
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.configure(bg=KEY)
        root.attributes("-transparentcolor", KEY)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.0)
        root.geometry(f"{self.w}x{self.h}+-4000+-4000")
        self.canvas = tk.Canvas(root, width=self.w, height=self.h, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
        root.update_idletasks()
        self.hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE,
                                 ex | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_TOPMOST)
        root.deiconify()                     # shown once, invisible and off-screen; never re-activated
        self._root = root
        self._ready.set()
        root.mainloop()

    def _place(self) -> None:
        mon = user32.MonitorFromWindow(user32.GetForegroundWindow(), MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        user32.GetMonitorInfoW(mon, ctypes.byref(mi))
        wa = mi.rcWork
        x = (wa.left + wa.right - self.w) // 2
        y = wa.top + int(24 * self.s) if self.position == "top" else wa.bottom - self.h - int(56 * self.s)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _show(self, mode: str, **kw) -> None:
        if self._hide_job:
            self._root.after_cancel(self._hide_job)
            self._hide_job = None
        if self._mode == "hidden":
            self._place()
        self._mode = mode
        self._kw = kw
        if mode == "listening":
            self._level_fn, self._started, self._writing_fn = kw["level_fn"], kw["started"], kw["writing_fn"]
        self._root.attributes("-alpha", 0.96)
        self._draw()
        if mode == "flash":
            self._hide_job = self._root.after(kw["ms"], self._hide)
        elif self._anim_job is None:
            self._animate()

    def _hide(self) -> None:
        self._mode = "hidden"
        self._hide_job = None
        if self._anim_job:
            self._root.after_cancel(self._anim_job)
            self._anim_job = None
        self._root.attributes("-alpha", 0.0)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, -4000, -4000, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)

    def _animate(self) -> None:
        if self._mode not in ("listening", "working"):
            self._anim_job = None
            return
        self._tick += 1
        self._draw()
        self._anim_job = self._root.after(70, self._animate)

    # --- drawing ----------------------------------------------------------------------------------

    def _pill(self) -> None:
        c, s, w, h = self.canvas, self.s, self.w, self.h
        r = h / 2
        c.create_oval(0, 0, h, h, fill=BG, outline=BG)
        c.create_oval(w - h, 0, w, h, fill=BG, outline=BG)
        c.create_rectangle(r, 0, w - r, h, fill=BG, outline=BG)

    def _draw(self) -> None:
        c, s, h = self.canvas, self.s, self.h
        c.delete("all")
        self._pill()
        font = ("Segoe UI Semibold", 10)
        small = ("Segoe UI", 9)
        cy = h / 2
        if self._mode == "listening":
            pulse = 0.75 + 0.25 * math.sin(self._tick / 2.2)
            rr = 6 * s * pulse
            c.create_oval(22 * s - rr, cy - rr, 22 * s + rr, cy + rr, fill=RED, outline=RED)
            c.create_text(36 * s, cy, text="Listening", fill=FG, font=font, anchor="w")
            level = self._level_fn()
            db = 20 * math.log10(max(level, 1e-6))
            v = min(1.0, max(0.0, (db + 55) / 40))
            for i in range(7):
                ph = abs(math.sin(self._tick / 1.6 + i * 0.9))
                bh = (3 + 15 * v * (0.45 + 0.55 * ph)) * s
                x = (120 + i * 7) * s
                c.create_rectangle(x, cy - bh / 2, x + 4 * s, cy + bh / 2, fill=FG if v > 0.05 else MUTED, outline="")
            try:
                writing = bool(self._writing_fn())
            except Exception:
                writing = False
            if writing:  # a phrase is being transcribed / waiting to be typed
                rr = (2.6 + 0.9 * abs(math.sin(self._tick / 2.0))) * s
                c.create_oval(178 * s - rr, cy - rr, 178 * s + rr, cy + rr, fill=AMBER, outline=AMBER)
            secs = int(time.monotonic() - self._started)
            c.create_text(self.w - 18 * s, cy, text=f"{secs // 60}:{secs % 60:02d}", fill=MUTED, font=small, anchor="e")
        elif self._mode == "working":
            for i in range(3):
                on = (self._tick // 3) % 3 == i
                rr = (4.5 if on else 3.2) * s
                x = (16 + i * 11) * s
                col = AMBER if on else "#6b5a35"
                c.create_oval(x - rr, cy - rr, x + rr, cy + rr, fill=col, outline=col)
            c.create_text(46 * s, cy, text=self._kw.get("text", "Transcribing…"), fill=FG, font=font, anchor="w")
        elif self._mode == "flash":
            col = self._kw.get("color", GREEN)
            c.create_oval(14 * s, cy - 7 * s, 28 * s, cy + 7 * s, fill=col, outline=col)
            c.create_text(21 * s, cy, text="✓" if col == GREEN else "!", fill=BG, font=("Segoe UI", 8, "bold"))
            c.create_text(38 * s, cy, text=self._kw.get("text", ""), fill=FG, font=font, anchor="w")
