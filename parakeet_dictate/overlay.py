"""Small always-on-top pill that shows Listening / Transcribing / Typed.

It never takes focus (WS_EX_NOACTIVATE), clicks go straight through it (WS_EX_TRANSPARENT), and it
appears on the monitor of the window you are typing into, or where the user dragged it to (see
pillpos.py: then it stays on that monitor). When hidden it is fully transparent and parked off-screen,
and no timers run, so it costs nothing between dictations.

Move mode (tray → "Move the pill…") is the one exception: for as long as it lasts, the pill takes the
mouse and the keyboard (WS_EX_TRANSPARENT and WS_EX_NOACTIVATE cleared) so it can be dragged, and
Enter / a double-click saves, Esc cancels. Afterwards the exact original styles are put back and the
window that was in front gets the focus again. The app never starts a dictation while it lasts.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import math
import threading
import time
import tkinter as tk
from pathlib import Path

from . import pillpos

log = logging.getLogger(__name__)

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
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]

GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
PILL_EX = WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_TOPMOST
MOVE_CLEARS = WS_EX_TRANSPARENT | WS_EX_NOACTIVATE      # only while the user moves the pill
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x20, 0x40
MONITOR_DEFAULTTONEAREST = 2

# Move-mode hint, longest first: the first that fits the pill is shown.
MOVE_HINTS = ("Drag me · Enter to save · Esc to cancel", "Drag · Enter saves · Esc cancels",
              "Drag · Enter · Esc")

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


def pill_y(top: int, bottom: int, height: int, position: str, margin: int, scale: float) -> int:
    """Screen y of the pill: `margin` pixels (at 100% scaling) below the top of the work area or above
    the bottom (the taskbar is outside the work area). margin < 0 keeps the defaults: 24 top, 56 bottom."""
    if position == "top":
        return top + int((24 if margin < 0 else margin) * scale)
    return bottom - height - int((56 if margin < 0 else margin) * scale)


def fit_tail(text: str, max_chars: int) -> str:
    """The end of `text` in at most max_chars characters: the newest words matter most."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return "…" + text[-(max_chars - 1):].lstrip()


def listening_look(level: float, clipping: bool) -> tuple[str, str, str, float]:
    """(label, label colour, bar colour, bar fill 0..1) for the Listening pill.

    `level` is the mic meter's 0..1 level (noise floor already removed). While the input clips the
    pill says so in red, because clipped audio transcribes badly and the fix (move the mic, lower the
    input volume) is the user's to make."""
    v = min(1.0, max(0.0, level))
    if clipping:
        return "Too loud", RED, RED, max(v, 0.8)
    return "Listening", FG, FG if v > 0.05 else MUTED, v


def fit_text(texts, measure, room: float) -> str:
    """The first of `texts` whose measured width fits in `room` (else the last, shortest one)."""
    for t in texts:
        if measure(t) <= room:
            return t
    return texts[-1]


class Overlay:
    def __init__(self, position: str = "bottom", wide: bool = False, pos_file: Path | None = None):
        self.position = position
        self.wide = wide            # live streaming: room for the words heard but not typed yet
        self.margin = -1            # pixels from the screen edge (at 100% scaling); -1 = 24 at the top, 56 at the bottom
        self.pos_file = pos_file    # pill_position.json (None = never moved by the user)
        self._saved: pillpos.SavedPosition | None = None
        self._saved_loaded = False
        self._on_move_end = None    # move mode: called (in its own thread) with True when saved
        self._prev_fg = None        # move mode: the window in front before it, given the focus back after
        self._grab: tuple[int, int] | None = None
        self.hwnd = 0
        self._preview_fn = lambda: ""
        self._ready = threading.Event()
        self._root: tk.Tk | None = None
        self._mode = "hidden"
        self._level_fn = lambda: 0.0
        self._writing_fn = lambda: False
        self._clipping_fn = lambda: False
        self._started = 0.0
        self._tick = 0
        self._hide_job = None
        self._anim_job = None
        threading.Thread(target=self._run, daemon=True, name="overlay").start()
        self._ready.wait(5)

    # --- public, callable from any thread -------------------------------------------------------

    def listening(self, level_fn, started_at: float, writing_fn=None, clipping_fn=None, preview_fn=None) -> None:
        """level_fn() -> mic meter level 0..1; writing_fn() -> True while heard speech is still being
        turned into text (amber dot); clipping_fn() -> True while the input is too loud (red bars);
        preview_fn() -> words heard but not typed yet (live streaming; shown on a wide pill)."""
        self._call(self._show, "listening", level_fn=level_fn, started=started_at,
                   writing_fn=writing_fn or (lambda: False), clipping_fn=clipping_fn or (lambda: False),
                   preview_fn=preview_fn or (lambda: ""))

    def working(self, text: str = "Transcribing…") -> None:
        self._call(self._show, "working", text=text)

    def flash(self, text: str, color: str = GREEN, ms: int = 900) -> None:
        self._call(self._show, "flash", text=text, color=color, ms=ms)

    def hide(self) -> None:
        self._call(self._hide)

    def close(self) -> None:
        self._call(lambda: self._root.destroy())

    @property
    def moving(self) -> bool:
        return self._mode == "move"

    def start_move(self, on_end) -> bool:
        """Show the pill in move mode. on_end(saved: bool) is called once it ends, in a thread of its own
        (never on the Tk thread, so it may take locks that other threads hold while calling into here).
        False when the pill's window isn't running."""
        return self._call(self._begin_move, on_end)

    def end_move(self, save: bool) -> bool:
        """End move mode (save = keep the new spot). False when the pill's window isn't running."""
        return self._call(self._end_move, save)

    def forget_position(self) -> None:
        """The saved position was deleted: use the usual place from the next time the pill shows."""
        self._saved, self._saved_loaded = None, True

    def is_foreground(self) -> bool:
        """True when the pill's window has the focus (it never should outside move mode)."""
        return bool(self.hwnd) and user32.GetForegroundWindow() == self.hwnd

    # --- Tk thread ------------------------------------------------------------------------------

    def _call(self, fn, *a, **kw) -> bool:
        if self._root is not None:
            try:
                self._root.after(0, lambda: fn(*a, **kw))
                return True
            except (RuntimeError, tk.TclError):
                pass
        return False

    def _run(self) -> None:
        s = _dpi_scale()
        self.s = s
        self.w, self.h = int((440 if self.wide else 236) * s), int(44 * s)
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
        # Move mode only: with WS_EX_TRANSPARENT / WS_EX_NOACTIVATE set these never fire.
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", lambda e: self._end_move(True))
        for key in ("<Return>", "<KP_Enter>"):
            root.bind(key, lambda e: self._end_move(True))
        root.bind("<Escape>", lambda e: self._end_move(False))
        root.update_idletasks()
        self.hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        self._ex = ex | PILL_EX              # the pill's styles: restored exactly after move mode
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, self._ex)
        root.deiconify()                     # shown once, invisible and off-screen; never re-activated
        self._root = root
        self._ready.set()
        root.mainloop()

    def _saved_position(self) -> pillpos.SavedPosition | None:
        if not self._saved_loaded:
            self._saved = pillpos.load(self.pos_file) if self.pos_file else None
            self._saved_loaded = True
        return self._saved

    def _place(self) -> None:
        try:
            saved = self._saved_position()
            xy = pillpos.placement(saved, pillpos.monitors(), self.w, self.h) if saved else None
        except Exception:                    # never let a position problem keep the pill from showing
            log.exception("saved pill position")
            xy = None
        if xy is not None:                   # where the user put it (stays on that monitor)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, xy[0], xy[1], 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            return
        mon = user32.MonitorFromWindow(user32.GetForegroundWindow(), MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        user32.GetMonitorInfoW(mon, ctypes.byref(mi))
        wa = mi.rcWork
        x = (wa.left + wa.right - self.w) // 2
        y = pill_y(wa.top, wa.bottom, self.h, self.position, self.margin, self.s)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _show(self, mode: str, **kw) -> None:
        if self._mode == "move":             # move mode owns the pill until it ends
            return
        if self._hide_job:
            self._root.after_cancel(self._hide_job)
            self._hide_job = None
        if self._mode == "hidden":
            self._place()
        self._mode = mode
        self._kw = kw
        if mode == "listening":
            self._level_fn, self._started, self._writing_fn = kw["level_fn"], kw["started"], kw["writing_fn"]
            self._clipping_fn, self._preview_fn = kw["clipping_fn"], kw["preview_fn"]
        self._root.attributes("-alpha", 0.96)
        self._draw()
        if mode == "flash":
            self._hide_job = self._root.after(kw["ms"], self._hide)
        elif self._anim_job is None:
            self._animate()

    def _hide(self, force: bool = False) -> None:
        if self._mode == "move" and not force:
            return
        self._mode = "hidden"
        self._hide_job = None
        if self._anim_job:
            self._root.after_cancel(self._anim_job)
            self._anim_job = None
        self._root.attributes("-alpha", 0.0)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, -4000, -4000, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)

    # --- move mode (Tk thread) ------------------------------------------------------------------

    def _set_styles(self, ex: int) -> None:
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex)
        user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)

    def _begin_move(self, on_end) -> None:
        if self._mode == "move":
            return
        for job in (self._hide_job, self._anim_job):
            if job:
                self._root.after_cancel(job)
        self._hide_job = self._anim_job = None
        self._prev_fg = user32.GetForegroundWindow()
        if self._mode == "hidden":
            self._place()
        self._mode = "move"
        self._on_move_end = on_end
        self._grab = None
        self._set_styles(self._ex & ~MOVE_CLEARS)
        self._root.attributes("-alpha", 0.96)
        self._draw()
        try:
            self._root.focus_force()         # so Enter / Esc reach it; clicking it works too
        except tk.TclError:
            pass

    def _end_move(self, save: bool) -> None:
        if self._mode != "move":
            return
        saved = False
        if save:
            try:
                saved = self._save_position()
            except Exception:
                log.exception("saving the pill position")
        self._set_styles(self._ex)           # click-through and never-focused again, exactly as before
        self._grab = None
        prev = self._prev_fg
        if user32.GetForegroundWindow() == self.hwnd and prev and prev != self.hwnd and user32.IsWindow(prev):
            user32.SetForegroundWindow(prev)  # give the focus back to the window that had it
        self._prev_fg = None
        self._hide(force=True)
        on_end, self._on_move_end = self._on_move_end, None
        if on_end is not None:
            threading.Thread(target=on_end, args=(saved,), daemon=True, name="pill-moved").start()
        if saved:
            self._show("flash", text="Position saved", color=GREEN, ms=900)   # shown at the new place

    def _save_position(self) -> bool:
        if not self.pos_file:
            return False
        left, top, _, _ = pillpos.window_rect(self.hwnd)
        mon = pillpos.monitor_of_window(self.hwnd)
        if mon is None:
            return False
        pos = pillpos.to_relative(mon.device, left, top, self.w, self.h, mon.work)
        pillpos.save(self.pos_file, pos, mon.work)
        self._saved, self._saved_loaded = pos, True
        return True

    def _on_press(self, _event=None) -> None:
        if self._mode != "move":
            return
        cx, cy = pillpos.cursor_pos()
        left, top, _, _ = pillpos.window_rect(self.hwnd)
        self._grab = (cx - left, cy - top)
        try:
            self.canvas.focus_set()
        except (tk.TclError, AttributeError):
            pass

    def _on_drag(self, _event=None) -> None:
        if self._mode != "move" or self._grab is None:
            return
        cx, cy = pillpos.cursor_pos()
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, cx - self._grab[0], cy - self._grab[1], 0, 0,
                            SWP_NOSIZE | SWP_NOACTIVATE)

    def _on_release(self, _event=None) -> None:
        self._grab = None

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
            try:
                level, clipping = float(self._level_fn()), bool(self._clipping_fn())
            except Exception:
                level, clipping = 0.0, False
            label, label_col, bar_col, v = listening_look(level, clipping)
            c.create_text(36 * s, cy, text=label, fill=label_col, font=font, anchor="w")
            for i in range(7):
                ph = abs(math.sin(self._tick / 1.6 + i * 0.9))
                bh = (3 + 15 * v * (0.45 + 0.55 * ph)) * s
                x = (120 + i * 7) * s
                c.create_rectangle(x, cy - bh / 2, x + 4 * s, cy + bh / 2, fill=bar_col, outline="")
            try:
                writing = bool(self._writing_fn())
            except Exception:
                writing = False
            if writing:  # a phrase is being transcribed / waiting to be typed
                rr = (2.6 + 0.9 * abs(math.sin(self._tick / 2.0))) * s
                c.create_oval(178 * s - rr, cy - rr, 178 * s + rr, cy + rr, fill=AMBER, outline=AMBER)
            if self.wide:
                try:
                    preview = str(self._preview_fn() or "")
                except Exception:
                    preview = ""
                room = self.w - (192 + 58) * s           # between the amber dot and the timer
                text = fit_tail(preview, max(4, int(room / (6.6 * s))))
                if text:
                    c.create_text(192 * s, cy, text=text, fill=MUTED, font=small, anchor="w")
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
        elif self._mode == "move":
            c.create_text(22 * s, cy, text="✥", fill=AMBER, font=("Segoe UI Symbol", 12), anchor="center")
            room = self.w - (38 + 16) * s
            try:
                import tkinter.font as tkfont
                f = tkfont.Font(root=self._root, family="Segoe UI", size=9)
                hint = fit_text(MOVE_HINTS, f.measure, room)
            except (tk.TclError, AttributeError):
                hint = MOVE_HINTS[-1]
            c.create_text(38 * s, cy, text=hint, fill=FG, font=small, anchor="w")
        elif self._mode == "flash":
            col = self._kw.get("color", GREEN)
            c.create_oval(14 * s, cy - 7 * s, 28 * s, cy + 7 * s, fill=col, outline=col)
            c.create_text(21 * s, cy, text="✓" if col == GREEN else "!", fill=BG, font=("Segoe UI", 8, "bold"))
            c.create_text(38 * s, cy, text=self._kw.get("text", ""), fill=FG, font=font, anchor="w")
