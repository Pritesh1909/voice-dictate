"""Small always-on-top pill that shows Listening / Transcribing / Typed.

It never takes focus (WS_EX_NOACTIVATE), clicks go straight through it (WS_EX_TRANSPARENT), and it
appears on the monitor of the window you are typing into, or where the user dragged it to (see
pillpos.py: then it stays on that monitor). When hidden it is fully transparent and parked off-screen,
and no timers run, so it costs nothing between dictations.

The look follows the "Listening pill" design (dark only): a 60 px tall, fully rounded dark pill with a
soft drop shadow, a pulsing red dot, "Listening", a 9-bar level wave, the words heard so far (wide
pill), the timer and a stop mark. Each frame is drawn with Pillow (Painter, no window needed) and shown
as a per-pixel-alpha layered window (UpdateLayeredWindow), so the edges are anti-aliased and the
shadow is real. Tk only runs the window and its message loop; it draws nothing.

Move mode (tray → "Move the pill…") is the one exception: for as long as it lasts, the pill takes the
mouse and the keyboard (WS_EX_TRANSPARENT and WS_EX_NOACTIVATE cleared) so it can be dragged, and
Enter / a double-click saves, Esc cancels. Afterwards the exact original styles are put back and the
window that was in front gets the focus again. The app never starts a dictation while it lasts.

style="button" shows a round 56 px mic button instead ("Button states" design, ButtonPainter): Ready
(idle), Listening (red halo and pulse ring that follow the voice), Typing (blue halo, hopping dots),
Processing (blue arc turning round it), Done (light circle with a check). It takes clicks whenever the
mouse is on the circle (the same hover-only trick as the pill's stop button, never the focus) and calls
on_click(); with always_visible it shows Ready instead of hiding. Ready and the results are still
pictures: only the cursor is polled (cheaply) for the hover look, nothing is redrawn until it changes.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import math
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

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
user32.LockSetForegroundWindow.argtypes = [wt.UINT]
LSFW_LOCK, LSFW_UNLOCK = 1, 2
user32.GetDC.restype = wt.HANDLE
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HANDLE]
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
gdi32.CreateCompatibleDC.restype = wt.HANDLE
gdi32.CreateCompatibleDC.argtypes = [wt.HANDLE]
gdi32.CreateDIBSection.restype = wt.HANDLE
gdi32.CreateDIBSection.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.UINT, ctypes.c_void_p, wt.HANDLE, wt.DWORD]
gdi32.SelectObject.restype = wt.HANDLE
gdi32.SelectObject.argtypes = [wt.HANDLE, wt.HANDLE]
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.DeleteDC.argtypes = [wt.HANDLE]

GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
PILL_EX = WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_TOPMOST
MOVE_CLEARS = WS_EX_TRANSPARENT | WS_EX_NOACTIVATE      # only while the user moves the pill
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x20, 0x40
MONITOR_DEFAULTTONEAREST = 2
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
SHOWN_ALPHA = 255           # the whole window's opacity while shown (0 = hidden)

# Move-mode hint, longest first: the first that fits the pill is shown.
MOVE_HINTS = ("Drag me · Enter to save · Esc to cancel", "Drag · Enter saves · Esc cancels",
              "Drag · Enter · Esc")

# --- the look (CSS px at 100 % scaling; from the design) ------------------------------------------
BODY = "#16171b"
FG = "#f5f6f8"              # labels, stop mark
LIVE = "#b7bdcb"            # words heard but not typed yet
TIMER = "#c5cad6"
BAR = "#e8ebf2"
MUTED = "#8a909c"           # quiet bars, "Cancelled" / "No speech heard"
RED, AMBER, GREEN = "#ff5a5f", "#ffb547", "#3ecf8e"
SHADOW = (18, 20, 28)

PILL_H, WIDE_W = 60, 580
PAD_L, PAD_R, GAP = 24, 8, 16
DOT, LABEL_GAP = 10, 10                     # the dot's centre is PAD_L + DOT / 2 = 29 px in
BARS, BAR_W, BAR_GAP, BAR_MAX = 9, 3, 3, 22
BAR_OPACITY = (.45, .65, .85, 1, 1, 1, .85, .65, .45)
BAR_DELAY = (-1.0, -.8, -.6, -.4, -.2, -.7, -.5, -.3, -.9)
WAVE_PERIOD, RING_PERIOD, CARET_PERIOD = 1.2, 1.8, 1.0
TIMER_MIN_W, STOP_D, STOP_MARK = 34, 44, 12
ICON_D = 18                                 # flash icon circle
TEXT_END = 24                               # right padding of the short pills (Transcribing, Typed, ...)
FADE_W = 20                                 # the live words fade in over their first 20 px when cut
SHADOW_PAD = (40, 22, 40, 58)               # room for the drop shadow: left, top, right, bottom
LABELS = ("Listening", "Too loud")
INTER = Path(__file__).with_name("fonts") / "Inter-latin.woff2"     # the design's font (SIL OFL, see OFL.txt)
# The characters in INTER (Google Fonts' "latin" range); other text is drawn in Segoe UI.
INTER_RANGES = ((0x0000, 0x00FF), (0x0131, 0x0131), (0x0152, 0x0153), (0x02BB, 0x02BC), (0x02C6, 0x02C6),
                (0x02DA, 0x02DA), (0x02DC, 0x02DC), (0x0304, 0x0304), (0x0308, 0x0308), (0x0329, 0x0329),
                (0x2000, 0x206F), (0x20AC, 0x20AC), (0x2122, 0x2122), (0x2191, 0x2191), (0x2193, 0x2193),
                (0x2212, 0x2212), (0x2215, 0x2215), (0xFEFF, 0xFEFF), (0xFFFD, 0xFFFD))

POSITIONS = ("top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right")
CORNER_MARGIN = 24                          # default distance from both edges in a corner

# --- the round button ("Button states" design; CSS px at 100 %) ---------------------------------------
BTN_D = 56                                  # the circle
BTN_PAD = 52                                # room around it for the halo, the pulse ring and the shadow
BTN_CORE = "#17181c"
BTN_HOVER = .07                             # white over the dark core while the mouse is on it
WORK = "#7aa2ff"                            # Typing halo, Processing spinner, Done pulse
INK = "#16171b"                             # the glyph on a filled (Done / Cancelled / error) core
BTN_SHADOWS = ((10, 24, .28), (2, 6, .20))  # box-shadow: y offset, blur, opacity
SPIN_PERIOD, SPIN_ARC = 1.1, 48 / (2 * math.pi * 27) * 360     # 1.1 s a turn; dasharray 48 of r 27
HOP_PERIOD, HOP_DELAY, DOT_D, DOT_STEP = 1.0, .15, 5, 9         # Typing dots: 5 px, gap 4
TYPING_LVL = .22                            # the Typing halo's least size (the design's value)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class UPDATELAYEREDWINDOWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hdcDst", wt.HANDLE), ("pptDst", ctypes.POINTER(wt.POINT)),
                ("psize", ctypes.POINTER(wt.SIZE)), ("hdcSrc", wt.HANDLE), ("pptSrc", ctypes.POINTER(wt.POINT)),
                ("crKey", wt.DWORD), ("pblend", ctypes.POINTER(BLENDFUNCTION)), ("dwFlags", wt.DWORD),
                ("prcDirty", ctypes.POINTER(wt.RECT))]


user32.UpdateLayeredWindowIndirect.argtypes = [wt.HWND, ctypes.POINTER(UPDATELAYEREDWINDOWINFO)]


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


def preset_xy(work: tuple[int, int, int, int], w: int, h: int, position: str, margin: int,
              scale: float) -> tuple[int, int]:
    """Screen position of a w×h pill or button in the work area (left, top, right, bottom) for an
    [overlay] position: "top" / "bottom" are centred, `margin` px (at 100 %) from that edge (defaults
    24 / 56, see pill_y); the corners ("top-left", "bottom-right", ...) are `margin` px from both edges
    (default 24). Unknown positions mean "bottom". Always fully inside the work area."""
    left, top, right, bottom = work
    vert, _, horiz = (position if position in POSITIONS else "bottom").partition("-")
    if horiz:
        m = int((CORNER_MARGIN if margin < 0 else margin) * scale)
        x = left + m if horiz == "left" else right - w - m
        y = top + m if vert == "top" else bottom - h - m
    else:
        x, y = (left + right - w) // 2, pill_y(top, bottom, h, vert, margin, scale)
    return pillpos.clamp(x, y, w, h, work)


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


def fit_end(text: str, measure, room: float) -> str:
    """`text`, cut at the end with "…" until its measured width fits in `room`."""
    if measure(text) <= room:
        return text
    for n in range(len(text) - 1, 0, -1):
        t = text[:n].rstrip() + "…"
        if measure(t) <= room:
            return t
    return "…"


def inter_covers(text: str) -> bool:
    """True when every character of `text` is in the bundled Inter (its Latin part)."""
    return all(any(a <= ord(ch) <= b for a, b in INTER_RANGES) for ch in text)


def wave(p: float) -> float:
    """The CSS bar wave (scaleY .25 → 1 → .25, ease-in-out) as 0..1 at phase p (any real number)."""
    return (1 - math.cos(2 * math.pi * (p % 1.0))) / 2


def bar_scales(t: float, level: float) -> list[float]:
    """Each bar's height as a share of BAR_MAX at time t: the design's wave, as tall as the mic level
    allows (level 1 = the design's full wave, 0 = all bars at their .25 floor)."""
    v = min(1.0, max(0.0, level))
    return [0.25 + 0.75 * v * wave((t - d) / WAVE_PERIOD) for d in BAR_DELAY]


def ring_look(t: float) -> tuple[float, float]:
    """(scale, opacity) of the dot's pulse ring at time t: 1 → 2.6 and .55 → 0 over 1.8 s, ease-out."""
    p = (t % RING_PERIOD) / RING_PERIOD
    e = 1 - (1 - p) ** 2
    return 1 + 1.6 * e, 0.55 * (1 - e)


def caret_on(t: float) -> bool:
    """The live-words caret blinks: on for the first half of each second."""
    return (t % CARET_PERIOD) < CARET_PERIOD / 2


def flash_icon(color: str) -> str:
    """The glyph in a flash message's circle: ✓ for done, ✕ for errors, – for nothing happened."""
    return {GREEN: "check", RED: "cross", MUTED: "dash"}.get(str(color).lower(), "bang")


def clock(secs: float) -> str:
    s = max(0, int(secs))
    return f"{s // 60}:{s % 60:02d}"


# --- the round button's look (pure, tested) ------------------------------------------------------------

def button_state(mode: str, writing: bool = False, color: str = GREEN) -> str:
    """Which of the button's looks an overlay mode shows: ready, listening, typing, processing, done,
    muted (Cancelled, nothing heard), error, alert (other flash colours) or move."""
    if mode == "listening":
        return "typing" if writing else "listening"
    if mode == "working":
        return "processing"
    if mode == "flash":
        return {GREEN: "done", MUTED: "muted", RED: "error"}.get(str(color).lower(), "alert")
    return mode if mode in ("ready", "move") else "ready"


def flash_core(state: str) -> tuple[str, str]:
    """(core fill, glyph) of a flash state: the design's Done is a light core with a dark check; the
    other results follow it with the pill's flash colours (grey dash, red cross, amber "!")."""
    return {"done": (FG, "check"), "muted": (MUTED, "dash"), "error": (RED, "cross")}.get(state, (AMBER, "bang"))


def halo_look(level: float) -> tuple[float, float, float]:
    """(scale, opacity, blur px) of the soft halo behind the button at mic level 0..1 (the design's
    1 + lvl·.75, .12 + lvl·.4, 2 + lvl·6 px)."""
    v = min(1.0, max(0.0, level))
    return 1 + .75 * v, .12 + .4 * v, 2 + 6 * v


def pulse_period(level: float) -> float:
    """Seconds per pulse ring: 1.8 when quiet, down to .9 at full level."""
    return 1.8 - .9 * min(1.0, max(0.0, level))


def pulse_look(phase: float, level: float) -> tuple[float, float]:
    """(scale, opacity) of the pulse ring at `phase` (0..1 through one ring): it grows from the
    button's edge to 1.5 + lvl·.6 and fades from .7 to 0, ease-out."""
    p = min(1.0, max(0.0, phase))
    e = 1 - (1 - p) ** 2
    return 1 + (.5 + .6 * min(1.0, max(0.0, level))) * e, .7 * (1 - e)


RIPPLE_LIFE = 1.5           # seconds a ripple takes to spread out and fade
RIPPLE_MAX = 7              # rings drawn at most at once


def ripple_rate(level: float) -> float:
    """Ripples per second: a slow 0.6 when it is quiet (so it still shows it is listening), up to 4.5
    when you speak loudly."""
    return .6 + 3.9 * min(1.0, max(0.0, level))


def ripple_look(age: float, strength: float) -> tuple[float, float, float]:
    """(scale, opacity, line width px at 100 %) of a ripple `age` s old that was sent out at voice
    `strength` 0..1: like a ring on water it spreads from the button's edge (further when louder:
    1.7 to 2.55 times the button) and fades, a louder one brighter and thicker."""
    p = min(1.0, max(0.0, age / RIPPLE_LIFE))
    e = 1 - (1 - p) ** 2.2                       # fast at first, then slowing, like a water ring
    s = min(1.0, max(0.0, strength))
    scale = 1 + (.7 + .85 * s) * e
    opacity = (.22 + .6 * s) * (1 - p) ** 1.4
    width = 1.2 + 1.8 * s * (1 - .5 * p)
    return scale, opacity, width


class RippleEmitter:
    """Sends out ripples as the voice comes and goes. Louder means more of them and stronger ones; a
    sudden rise (the start of a word) sends one straight away, so the rings follow the rhythm of speech.
    Pure state, no drawing: step() returns what to draw."""

    def __init__(self):
        self.rings: list[tuple[float, float]] = []      # (born at, strength)
        self._credit = 0.0
        self._last_t: float | None = None
        self._floor = 0.0                                # recent quiet level, for "a word starts"
        self._last_burst = -1.0

    def step(self, t: float, level: float) -> list[tuple[float, float, float]]:
        v = min(1.0, max(0.0, level))
        first = self._last_t is None
        dt = 0.0 if first else min(.25, max(0.0, t - self._last_t))
        self._last_t = t
        if first:
            self._emit(t, max(v, .15))                   # one right away when listening starts
        self._credit += dt * ripple_rate(v)
        if v - self._floor > .18 and t - self._last_burst > .18:
            self._emit(t, v)                             # a word starts: a ring now
            self._last_burst = t
            self._credit = min(self._credit, .5)
        while self._credit >= 1.0:
            self._credit -= 1.0
            self._emit(t, max(v, .15))
        self._floor = min(v, self._floor + dt * .8)      # follows quiet down at once, rises slowly
        self.rings = [(b, s) for b, s in self.rings if t - b < RIPPLE_LIFE][-RIPPLE_MAX:]
        return [ripple_look(t - b, s) for b, s in self.rings]

    def _emit(self, t: float, strength: float) -> None:
        self.rings.append((t, strength))


def core_scale(level: float) -> float:
    """The button grows a little with the voice: 1 + lvl·.06."""
    return 1 + .06 * min(1.0, max(0.0, level))


def smooth_level(prev: float, level: float) -> float:
    """The halo's level: follows the meter up quickly and down slowly, so it breathes instead of flickering."""
    v = min(1.0, max(0.0, level))
    return prev + (v - prev) * (.6 if v > prev else .2)


def _ease_in_out(x: float) -> float:
    return x * x * (3 - 2 * x)


def hop(t: float, i: int) -> tuple[float, float]:
    """(lift px, opacity) of Typing dot i (0..2) at time t: each hops 4 px and brightens .5 → 1 at
    40 % of a 1 s cycle, the next one .15 s later."""
    p = ((t - i * HOP_DELAY) % HOP_PERIOD) / HOP_PERIOD
    f = _ease_in_out(p / .4) if p < .4 else 1 - _ease_in_out((p - .4) / .6)
    return 4 * f, .5 + .5 * f


def spinner_angle(t: float) -> float:
    """Where the Processing arc starts (degrees clockwise from 3 o'clock): a turn every 1.1 s."""
    return (t % SPIN_PERIOD) / SPIN_PERIOD * 360


def button_animates(state: str, since: float) -> bool:
    """Does the button need a frame every 70 ms? Ready / the flash results are still pictures (redrawn
    only when the hover look changes), except Done's one pulse ring while it runs."""
    return state in ("listening", "typing", "processing") or (state == "done" and since < pulse_period(0))


def button_hit(x: float, y: float, win_left: float, win_top: float, pad: tuple, d: float,
               slack: float = 2) -> bool:
    """Is screen point (x, y) on the round button? The window starts at (win_left, win_top), the
    button (d screen px across) sits `pad` px inside it."""
    cx, cy = win_left + pad[0] + d / 2, win_top + pad[1] + d / 2
    r = d / 2 + slack
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


@dataclass(frozen=True)
class Layout:
    """Where things go on the Listening pill, in px at 100 % from the pill's left edge (x centres for
    the dot, the divider / amber mark and the stop button)."""
    width: float
    dot_cx: float
    label_x: float
    bars_x: float
    mark_cx: float
    text_x: float           # live words (wide pill only)
    timer_right: float
    stop_cx: float

    def text_right(self, timer_w: float) -> float:
        return self.timer_right - max(TIMER_MIN_W, timer_w) - GAP


def listening_layout(label_w: float, wide: bool) -> Layout:
    """The design's flex row: [dot 10, gap 10, label] gap 16 [9 bars] gap 16 [divider 1] gap 16
    [live words, flexible] gap 16 [timer, min 34] gap 16 [stop 44], padding 24 left and 8 right.
    The wide pill is 580 px like the design; the narrow one (no live words) is as long as it needs.
    label_w is the wider of the two labels, so the bars don't jump when "Too loud" shows."""
    dot_cx = PAD_L + DOT / 2
    label_x = PAD_L + DOT + LABEL_GAP
    bars_x = label_x + label_w + GAP
    bars_end = bars_x + BARS * BAR_W + (BARS - 1) * BAR_GAP
    mark_cx = bars_end + GAP + 0.5
    if wide:
        width = WIDE_W
    else:
        width = mark_cx + 0.5 + GAP + TIMER_MIN_W + GAP + STOP_D + PAD_R
    stop_cx = width - PAD_R - STOP_D / 2
    return Layout(width, dot_cx, label_x, bars_x, mark_cx, mark_cx + 0.5 + GAP, stop_cx - STOP_D / 2 - GAP, stop_cx)


def stop_hit(x: float, y: float, win_left: float, win_top: float, pad: tuple, stop_cx: float,
             pill_h: float, s: float, slack: float = 2) -> bool:
    """Is screen point (x, y) on the stop button? The window starts at (win_left, win_top); the pill
    sits `pad` px inside it (room for the shadow); stop_cx is the button's centre in px at 100 %."""
    cx = win_left + pad[0] + stop_cx * s
    cy = win_top + pad[1] + pill_h / 2
    r = (STOP_D / 2 + slack) * s
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def compact_width(text_x: float, text_w: float, max_w: float) -> float:
    """Width of a short pill (Transcribing / Typed / errors): its text plus the right padding, at most
    the full pill's width."""
    return min(max_w, text_x + text_w + TEXT_END)


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _over(color: str, white: float) -> tuple[int, int, int]:
    """`color` with white at `white` opacity on top (the design's rgba(255,255,255,a) layers)."""
    return tuple(round(v + (255 - v) * white) for v in _rgb(color))


class _Sprites:
    """Cached anti-aliased pieces (text, circles, shapes) drawn with Pillow at display scale `s`; shared by
    the pill's and the round button's painters."""

    SS = 4                  # supersampling for anti-aliased shapes

    def __init__(self, s: float):
        self.s = s
        self._fonts: dict = {}
        self._cache: dict = {}

    # --- pieces ---

    def _font(self, weight: int, css_px: float, text: str = ""):
        """The design's font (Inter, bundled), or Segoe UI when `text` has letters Inter's Latin part
        lacks or the file can't be read."""
        inter = inter_covers(text)
        key = (weight, css_px, inter)
        f = self._fonts.get(key)
        if f is None:
            px = max(1, round(css_px * self.s))
            f = None
            if inter:
                try:
                    f = ImageFont.truetype(str(INTER), px)
                    f.set_variation_by_axes([weight])
                except Exception:
                    log.warning("pill font %s unusable, using Segoe UI", INTER, exc_info=True)
                    f = None
            if f is None:
                try:
                    f = ImageFont.truetype("SegUIVar.ttf", px)          # Segoe UI Variable (Windows 11)
                    f.set_variation_by_axes([weight, css_px * 0.75])
                except Exception:
                    try:
                        f = ImageFont.truetype("seguisb.ttf" if weight >= 600 else "segoeui.ttf", px)
                    except OSError:
                        f = ImageFont.load_default(px)
            self._fonts[key] = f
        return f

    def measure(self, text: str, weight: int = 400, css_px: float = 14) -> float:
        """Width of `text` in screen pixels."""
        return self._font(weight, css_px, text).getlength(text)

    def _cached(self, key, make):
        img = self._cache.get(key)
        if img is None:
            if len(self._cache) > 2000:     # a long dictation's words and timer values: start over
                self._cache.clear()
            img = self._cache[key] = make()
        return img

    def _text(self, text: str, weight: int, css_px: float, color: str, anchor: str = "lm"):
        """(image, dx, dy): the text drawn in `color`; paste it at (x + dx, y + dy) to put its anchor
        point (left/right, middle) at (x, y)."""
        def make():
            f = self._font(weight, css_px, text)
            x0, y0, x1, y1 = f.getbbox(text, anchor=anchor)
            m = Image.new("L", (max(1, math.ceil(x1 - x0)), max(1, math.ceil(y1 - y0))), 0)
            ImageDraw.Draw(m).text((-x0, -y0), text, font=f, fill=255, anchor=anchor)
            img = Image.new("RGBA", m.size, _rgb(color) + (255,))
            img.putalpha(m)
            return img, x0, y0
        return self._cached(("text", text, weight, css_px, color, anchor), make)

    def _shape(self, key, w: float, h: float, draw):
        """An anti-aliased sprite of w x h screen px: draw(d, k) paints it at k times the size."""
        def make():
            k = self.SS
            W, H = max(1, math.ceil(w)), max(1, math.ceil(h))
            big = Image.new("RGBA", (W * k, H * k), (0, 0, 0, 0))
            draw(ImageDraw.Draw(big), k, W, H)
            return big.reduce(k)
        return self._cached(("shape",) + key, make)

    def _circle(self, d: float, color, alpha: float = 1.0):
        rgba = _rgb(color) + (round(255 * alpha),)
        q = round(d * 4) / 4
        return self._shape(("circle", q, rgba), q + 2, q + 2,
                           lambda dr, k, W, H: dr.ellipse(((W * k - q * k) / 2, (H * k - q * k) / 2,
                                                           (W * k + q * k) / 2 - 1, (H * k + q * k) / 2 - 1),
                                                          fill=rgba))

    def _bar(self, height: float, color: str, opacity: float):
        s, k = self.s, self.SS
        q = round(height * 2) / 2
        rgba = _rgb(color) + (round(255 * opacity),)
        bw, box = BAR_W * s, BAR_MAX * s + 2

        def draw(dr, k, W, H):
            top = (H * k - q * k) / 2
            dr.rounded_rectangle((0, top, bw * k - 1, top + q * k - 1), radius=min(2 * s * k, bw * k / 2, q * k / 2),
                                 fill=rgba)
        return self._shape(("bar", q, rgba), bw, box, draw)

    def _paste(self, frame, sprite, cx: float, cy: float) -> None:
        """Paste a sprite with its centre at (cx, cy)."""
        frame.alpha_composite(sprite, (max(0, round(cx - sprite.width / 2)), max(0, round(cy - sprite.height / 2))))

    def _put_text(self, frame, text, weight, css_px, color, x, y, anchor="lm") -> int:
        img, dx, dy = self._text(text, weight, css_px, color, anchor)
        frame.alpha_composite(img, (max(0, round(x + dx)), max(0, round(y + dy))))
        return img.width

    def _move_mark(self):
        s, d = self.s, ICON_D * self.s
        rgba = _rgb(AMBER) + (255,)

        def draw(dr, k, W, H):
            c, u, lw = W * k / 2, s * k, max(1, round(1.6 * s * k))
            a = 7.5 * u
            dr.line([(c - a + u, c), (c + a - u, c)], fill=rgba, width=lw)
            dr.line([(c, c - a + u), (c, c + a - u)], fill=rgba, width=lw)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                tip = (c + dx * a, c + dy * a)
                px, py = -dy, dx
                back = (c + dx * (a - 3.2 * u), c + dy * (a - 3.2 * u))
                dr.polygon([tip, (back[0] + px * 2.8 * u, back[1] + py * 2.8 * u),
                            (back[0] - px * 2.8 * u, back[1] - py * 2.8 * u)], fill=rgba)
        return self._shape(("move",), d + 2, d + 2, draw)


class Painter(_Sprites):
    """Draws the pill as RGBA images at display scale `s`. Needs no window, so tests can draw with it.

    The window is the pill plus room for its shadow (`size`). The frame methods (listening, working,
    flash, move) return only the band of rows the pill covers (window width x pill height, `band`),
    on top of `shell`: the window-sized picture of the current pill shape with its shadow, which
    changes only when the shape does. full() puts a band on its shell. Everything that doesn't move
    is drawn once and kept (shells, text, bars at each height, ring steps), so a frame is a copy of
    the band and a few small pastes."""

    def __init__(self, s: float, wide: bool):
        super().__init__(s)
        self.wide = wide
        self._shells: dict = {}
        label_w = max(self._font(600, 14).getlength(t) for t in LABELS) / s
        self.layout = listening_layout(math.ceil(label_w), wide)
        self.w, self.h = round(self.layout.width * s), round(PILL_H * s)
        self.pad = tuple(round(p * s) for p in SHADOW_PAD)
        self.size = (self.w + self.pad[0] + self.pad[2], self.h + self.pad[1] + self.pad[3])
        self.band = (self.pad[1], self.pad[1] + self.h)    # the rows the pill covers in the window
        self.cy = self.h / 2                                # its middle, in the band
        self.shell: Image.Image | None = None               # the last frame's pill shape and shadow

    def full(self, band):
        """The whole window's picture: `band` (from the last frame method) on its shell."""
        img = self.shell.copy()
        img.paste(band, (0, self.band[0]))
        return img

    def _start(self, bw: int, stop: bool):
        """A new frame's band for a pill `bw` px wide (stop button or not)."""
        self.shell = self._shell(bw, stop)
        return self.shell.crop((0, self.band[0], self.size[0], self.band[1]))

    def _shell(self, bw: int, stop: bool):
        """The pill body `bw` px wide (centred), border, top highlight and drop shadow, on a clear
        window-sized image; with the stop button when `stop`."""
        key = (bw, stop)
        img = self._shells.get(key)
        if img is not None:
            return img
        if len(self._shells) > 8:
            self._shells.clear()
        s, k, h = self.s, self.SS, self.h
        W, H = self.size
        x0, y0 = self.pad[0] + (self.w - bw) // 2, self.pad[1]
        b = max(1, round(s))                                  # 1 px border
        big = Image.new("RGBA", (bw * k, h * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        r = h * k / 2
        d.rounded_rectangle((0, 0, bw * k - 1, h * k - 1), radius=r, fill=_over(BODY, .08))       # border
        d.rounded_rectangle((b * k, b * k, bw * k - 1 - b * k, h * k - 1 - b * k), radius=r - b * k,
                            fill=_over(BODY, .06))                                                 # inset top light
        d.rounded_rectangle((b * k, 2 * b * k, bw * k - 1 - b * k, h * k - 1 - b * k), radius=r - b * k,
                            fill=_rgb(BODY))
        body = big.reduce(k)
        mask = body.getchannel("A")
        # box-shadow: 0 18px 40px rgba(18,20,28,.22), 0 2px 6px rgba(18,20,28,.16)
        alphas = []
        for dy, blur, a in ((18, 40, .22), (2, 6, .16)):
            m = Image.new("L", (W, H), 0)
            m.paste(mask, (x0, y0 + round(dy * s)))
            m = m.filter(ImageFilter.GaussianBlur(blur * s / 2))
            alphas.append(m.point(lambda v, a=a: round(v * a)))
        shadow = Image.new("RGBA", (W, H), SHADOW + (0,))
        shadow.putalpha(ImageChops.screen(*alphas))
        hole = Image.new("L", (W, H), 255)                     # no shadow under the (opaque) body
        hole.paste(ImageChops.invert(mask), (x0, y0))
        shadow.putalpha(ImageChops.multiply(shadow.getchannel("A"), hole))
        shadow.alpha_composite(body, (x0, y0))
        if stop:        # True, or "hover" while the mouse is on it (a lighter circle)
            cx, cy = x0 + self.layout.stop_cx * s, y0 + h / 2
            self._paste(shadow, self._circle(STOP_D * s, "#ffffff", .18 if stop == "hover" else .08), cx, cy)
            m = STOP_MARK * s
            sq = self._shape(("stop", m), m, m, lambda dr, k, W, H: dr.rounded_rectangle(
                (0, 0, m * k - 1, m * k - 1), radius=3 * s * k, fill=_rgb(FG) + (255,)))
            self._paste(shadow, sq, cx, cy)
        self._shells[key] = shadow
        return shadow

    def _left(self, bw: int) -> int:
        return self.pad[0] + (self.w - bw) // 2

    # --- frames ---

    def listening(self, t: float, level: float, clipping: bool, writing: bool, preview: str, secs: float,
                  hover: bool = False):
        s, L, cy = self.s, self.layout, self.cy
        frame = self._start(self.w, "hover" if hover else True)
        x0 = self._left(self.w)
        X = lambda v: x0 + v * s                                            # noqa: E731
        label, label_col, bar_col, v = listening_look(level, clipping)
        bar_col = BAR if bar_col == FG else bar_col           # the design's bars are a touch greyer
        scale, op = ring_look(t)
        if op > 0.01:
            self._paste(frame, self._circle(DOT * s * round(scale * 16) / 16, RED, round(op * 32) / 32), X(L.dot_cx), cy)
        self._paste(frame, self._circle(DOT * s, RED), X(L.dot_cx), cy)
        self._put_text(frame, label, 600, 14, label_col, X(L.label_x), cy)
        for i, sc in enumerate(bar_scales(t, v)):
            self._paste(frame, self._bar(BAR_MAX * s * sc, bar_col, BAR_OPACITY[i]),
                        X(L.bars_x + i * (BAR_W + BAR_GAP) + BAR_W / 2), cy)
        if writing:     # a phrase is being transcribed / waiting to be typed
            d = (5 + 2 * abs(math.sin(t * 2.6))) * s
            self._paste(frame, self._circle(d, AMBER), X(L.mark_cx), cy)
        elif self.wide:
            dw = max(1, round(s))
            self._paste(frame, self._shape(("div", dw), dw, BAR_MAX * s, lambda dr, k, W, H: dr.rectangle(
                (0, 0, W * k - 1, H * k - 1), fill=(255, 255, 255, 31))), X(L.mark_cx), cy)
        stamp = clock(secs)
        img, dx, dy = self._text(stamp, 500, 13, TIMER, "rm")
        frame.alpha_composite(img, (round(X(L.timer_right) + dx), round(cy + dy)))
        if self.wide:
            self._live_words(frame, X(L.text_x), X(L.text_right(img.width / s)), cy, preview, t)
        return frame

    def _live_words(self, frame, left: float, right: float, cy: float, preview: str, t: float) -> None:
        s = self.s
        caret_w, caret_gap = 2 * s, 3 * s
        room = int(right - left - caret_gap - caret_w)
        text = fit_tail(preview, 400)
        shown = 0
        if text and room > 0:
            def make():
                img, dx, dy = self._text(text, 400, 14, LIVE)
                if img.width <= room:
                    return img, dx, dy
                cut = img.crop((img.width - room, 0, img.width, img.height))
                fw = min(room, round(FADE_W * s))
                ramp = Image.frombytes("L", (fw, 1), bytes(round(255 * i / max(1, fw - 1)) for i in range(fw)))
                a = cut.getchannel("A")
                a.paste(ImageChops.multiply(a.crop((0, 0, fw, cut.height)), ramp.resize((fw, cut.height))), (0, 0))
                cut.putalpha(a)
                return cut, 0, dy
            img, dx, dy = self._cached(("live", text, room), make)
            frame.alpha_composite(img, (round(left + dx), round(cy + dy)))
            shown = img.width + dx
        if caret_on(t):
            ch = 15 * s
            caret = self._shape(("caret", round(ch)), caret_w, ch, lambda dr, k, W, H: dr.rounded_rectangle(
                (0, 0, caret_w * k - 1, ch * k - 1), radius=s * k, fill=_rgb(RED) + (255,)))
            self._paste(frame, caret, left + shown + caret_gap + caret_w / 2, cy)

    def _short(self, text: str, text_x: float):
        """A short pill: (frame, its left edge, the text as it fits)."""
        s = self.s
        room = (self.layout.width - text_x - TEXT_END) * s
        text = fit_end(text, lambda t: self.measure(t, 600), room)
        bw = round(compact_width(text_x, self.measure(text, 600) / s, self.layout.width) * s)
        frame = self._start(bw, False)
        return frame, self._left(bw), text

    def working(self, t: float, text: str):
        s, cy = self.s, self.cy
        text_x = PAD_L + DOT + LABEL_GAP
        frame, x0, text = self._short(text, text_x)
        cx = x0 + (PAD_L + DOT / 2) * s
        scale, op = ring_look(t)
        if op > 0.01:
            self._paste(frame, self._circle(DOT * s * round(scale * 16) / 16, AMBER, round(op * 32) / 32), cx, cy)
        self._paste(frame, self._circle(DOT * s, AMBER), cx, cy)
        self._put_text(frame, text, 600, 14, FG, x0 + text_x * s, cy)
        return frame

    def flash(self, text: str, color: str):
        s, cy = self.s, self.cy
        text_x = PAD_L + DOT / 2 + ICON_D / 2 + LABEL_GAP
        frame, x0, text = self._short(text, text_x)
        cx = x0 + (PAD_L + DOT / 2) * s
        self._paste(frame, self._icon(flash_icon(color), color), cx, cy)
        self._put_text(frame, text, 600, 14, FG, x0 + text_x * s, cy)
        return frame

    def _icon(self, kind: str, color: str):
        s, d = self.s, ICON_D * self.s
        fill, ink = _rgb(color) + (255,), _rgb(BODY) + (255,)

        def draw(dr, k, W, H):
            o = (W * k - d * k) / 2
            dr.ellipse((o, o, o + d * k - 1, o + d * k - 1), fill=fill)
            c, u, lw = W * k / 2, s * k, max(1, round(2 * s * k))
            if kind == "check":
                dr.line([(c - 4.2 * u, c + 0.2 * u), (c - 1.3 * u, c + 3 * u), (c + 4.2 * u, c - 3 * u)],
                        fill=ink, width=lw, joint="curve")
            elif kind == "cross":
                for sx in (-1, 1):
                    dr.line([(c - 3.2 * u, c - 3.2 * u * sx), (c + 3.2 * u, c + 3.2 * u * sx)], fill=ink, width=lw)
            elif kind == "dash":
                dr.line([(c - 4 * u, c), (c + 4 * u, c)], fill=ink, width=lw)
            else:
                dr.line([(c, c - 4.5 * u), (c, c + 1 * u)], fill=ink, width=lw)
                dr.ellipse((c - 1.3 * u, c + 2.6 * u, c + 1.3 * u, c + 5.2 * u), fill=ink)
        return self._shape(("icon", kind, color), d + 2, d + 2, draw)

    def move(self):
        """Move mode: the full-size pill (so the user sees what they place), a move mark and the hint."""
        s, cy = self.s, self.cy
        frame = self._start(self.w, False)
        x0 = self._left(self.w)
        text_x = PAD_L + DOT / 2 + ICON_D / 2 + LABEL_GAP
        self._paste(frame, self._move_mark(), x0 + (PAD_L + DOT / 2) * s, cy)
        hint = fit_text(MOVE_HINTS, lambda t: self.measure(t, 500, 13), (self.layout.width - text_x - TEXT_END) * s)
        self._put_text(frame, hint, 500, 13, FG, x0 + text_x * s, cy)
        return frame


class ButtonPainter(_Sprites):
    """Draws the round button ("Button states" design) as window-sized RGBA images at display scale `s`;
    needs no window, so tests can draw with it.

    It has the Painter's interface for the Overlay: `w` x `h` is the button itself (what is placed and
    saved), `size` the window (the button plus BTN_PAD on every side for the halo, the pulse ring and
    the drop shadow), `pad`, and `band` / `shell` / full(), here simply the whole window. The layers
    are cached per quantised level (halo, ring, core with its shadow, glyphs, spinner steps), so a
    frame is a few alpha composites."""

    def __init__(self, s: float):
        super().__init__(s)
        self.w = self.h = round(BTN_D * s)
        p = round(BTN_PAD * s)
        self.pad = (p, p, p, p)
        self.size = (self.w + 2 * p, self.h + 2 * p)
        self.band = (0, self.size[1])
        self.shell = self.size              # never changes: every frame is the whole window
        self.c = p + self.w / 2             # the button's centre in the window (both ways)
        self._phase, self._last_t = 0.0, None
        self._ripples = RippleEmitter()
        self._ripple_shapes: dict = {}

    def full(self, band):
        return band

    # --- layers ---

    def _blank(self):
        return Image.new("RGBA", self.size, (0, 0, 0, 0))

    def _region(self, key, half: float, draw):
        """(sprite, offset): the square of half-size `half` px around the centre, drawn supersampled by
        draw(dr, k, X), X(v) being window coordinate v in the supersampled sprite (same both ways)."""
        def make():
            k = self.SS
            o = math.floor(self.c - half)
            n = math.ceil(self.c + half) - o
            big = Image.new("RGBA", (n * k, n * k), (0, 0, 0, 0))
            draw(ImageDraw.Draw(big), k, lambda v: (v - o) * k)
            return big.reduce(k), o
        return self._cached(("btn",) + key, make)

    @staticmethod
    def _put(frame, sprite) -> None:
        img, o = sprite
        frame.alpha_composite(img, (o, o))

    def _core(self, scale: float, fill: str, hover: bool):
        """The circle (1 px light border) with its drop shadow, window-sized; CSS transform: scale()
        grows the shadow with it."""
        q = round(scale * 200) / 200

        def make():
            s, W = self.s, self.size
            base = _over(fill, BTN_HOVER) if hover else _rgb(fill)
            edge = tuple(round(v + (255 - v) * .1) for v in base)
            r = BTN_D * s * q / 2
            b = max(1.0, s) * q

            def draw(dr, k, X):
                c, R = X(self.c), r * k
                dr.ellipse((c - R, c - R, c + R - 1, c + R - 1), fill=edge + (255,))
                R -= b * k
                dr.ellipse((c - R, c - R, c + R - 1, c + R - 1), fill=base + (255,))
            body, o = self._region(("body", q, base), r + 2, draw)
            mask = Image.new("L", W, 0)
            mask.paste(body.getchannel("A"), (o, o))
            alphas = []
            for dy, blur, a in BTN_SHADOWS:
                m = Image.new("L", W, 0)
                m.paste(mask, (0, round(dy * s * q)))
                m = m.filter(ImageFilter.GaussianBlur(blur * s * q / 2))
                alphas.append(m.point(lambda v, a=a: round(v * a)))
            img = Image.new("RGBA", W, SHADOW + (0,))
            img.putalpha(ImageChops.multiply(ImageChops.screen(*alphas), ImageChops.invert(mask)))
            img.alpha_composite(body, (o, o))
            return img
        return self._cached(("btn-core", q, fill, hover), make)

    def _halo(self, color: str, level: float):
        """The soft glow behind the button (window-sized)."""
        lq = round(min(1.0, max(0.0, level)) * 24) / 24

        def make():
            sc, op, blur = halo_look(lq)
            r = BTN_D * self.s * sc / 2
            m = Image.new("L", self.size, 0)
            ImageDraw.Draw(m).ellipse((self.c - r, self.c - r, self.c + r - 1, self.c + r - 1), fill=round(255 * op))
            img = Image.new("RGBA", self.size, _rgb(color) + (0,))
            img.putalpha(m.filter(ImageFilter.GaussianBlur(blur * self.s)))
            return img
        return self._cached(("btn-halo", color, lq), make)

    def _ring(self, color: str, scale: float, opacity: float, width: float | None = None):
        """A ring round the button: by default the design's 1.5 px pulse line the size of the button,
        scaled (line and all); with `width` (px at 100 %) a ripple line of that width."""
        sq, oq = round(scale * 40) / 40, round(opacity * 32) / 32
        wq = None if width is None else round(width * 2) / 2
        if wq is not None:
            # Ripples: the ring is drawn once per size and width at full strength (kept only while
            # listening, see _ripple_shapes) and faded per frame by scaling its alpha.
            sq = round(scale * 16) / 16
            oq = round(opacity * 16) / 16
            base_img, o = self._ring_shape(color, sq, wq)
            return self._faded(base_img, oq), o
        r = BTN_D * self.s * sq / 2
        bw = 1.5 * self.s * sq
        rgba = _rgb(color) + (round(255 * oq),)

        def draw(dr, k, X):
            c, R = X(self.c), r * k
            dr.ellipse((c - R, c - R, c + R - 1, c + R - 1), outline=rgba, width=max(1, round(bw * k)))
        return self._region(("ring", color, sq, oq, wq), r + 2, draw)

    def _ring_shape(self, color: str, sq: float, wq: float):
        """A ripple ring at full opacity, kept per size and line width in _ripple_shapes, which is
        emptied when the dictation ends (drop_ripples), so the memory is only used while listening."""
        key = (color, sq, wq)
        hit = self._ripple_shapes.get(key)
        if hit is not None:
            return hit
        r, bw = BTN_D * self.s * sq / 2, wq * self.s
        rgba = _rgb(color) + (255,)
        k = self.SS
        o = math.floor(self.c - r - 2)
        n = math.ceil(self.c + r + 2) - o
        big = Image.new("RGBA", (n * k, n * k), (0, 0, 0, 0))
        c, R = (self.c - o) * k, r * k
        ImageDraw.Draw(big).ellipse((c - R, c - R, c + R - 1, c + R - 1), outline=rgba, width=max(1, round(bw * k)))
        hit = self._ripple_shapes[key] = (big.reduce(k), o)
        return hit

    def drop_ripples(self) -> None:
        """Forget the ripple rings (called when listening ends)."""
        self._ripple_shapes.clear()
        self._ripples = RippleEmitter()

    @staticmethod
    def _faded(img, opacity: float):
        out = img.copy()
        out.putalpha(img.getchannel("A").point(lambda v, f=opacity: round(v * f)))
        return out

    def _spinner(self, angle: float):
        """Processing: a 48-unit arc of the r 27 circle (stroke 2, round ends) starting at `angle`."""
        aq = round(angle / 5) * 5 % 360
        s = self.s
        rc, sw = 27 * s, 2 * s
        rgba = _rgb(WORK) + (255,)

        def draw(dr, k, X):
            c, R = X(self.c), (rc + sw / 2) * k
            dr.arc((c - R, c - R, c + R - 1, c + R - 1), aq, aq + SPIN_ARC, fill=rgba, width=max(1, round(sw * k)))
            cr = sw * k / 2
            for a in (aq, aq + SPIN_ARC):
                x, y = c + rc * k * math.cos(math.radians(a)), c + rc * k * math.sin(math.radians(a))
                dr.ellipse((x - cr, y - cr, x + cr, y + cr), fill=rgba)
        return self._region(("spin", aq), rc + sw + 2, draw)

    def _glyph(self, kind: str, color: str, opacity: float = 1.0, scale: float = 1.0):
        """The design's 22 px icons (24-unit box, round caps): mic (stroke 1.8), check (2.4); and in
        the check's style the dash, cross and "!" of the other results."""
        sq = round(scale * 50) / 50
        u = 22 / 24 * self.s * sq
        rgba = _rgb(color) + (round(255 * opacity),)

        def draw(dr, k, X):
            c = X(self.c)
            P = lambda x, y: (c + (x - 12) * u * k, c + (y - 12) * u * k)       # noqa: E731

            def dot(x, y, d):
                cr = d * u * k / 2
                dr.ellipse((x - cr, y - cr, x + cr, y + cr), fill=rgba)

            def line(pts, sw):
                pts = [P(*p) for p in pts]
                dr.line(pts, fill=rgba, width=max(1, round(sw * u * k)), joint="curve")
                for x, y in (pts[0], pts[-1]):
                    dot(x, y, sw)

            if kind == "mic":
                sw, h = 1.8, .9
                lw = max(1, round(sw * u * k))
                dr.rounded_rectangle(P(9 - h, 3 - h) + P(15 + h, 14 + h), radius=(3 + h) * u * k, outline=rgba, width=lw)
                dr.arc(P(5 - h, 4 - h) + P(19 + h, 18 + h), 0, 180, fill=rgba, width=lw)     # centre 12,11 r 7
                for x in (5, 19):
                    dot(*P(x, 11), sw)
                line([(12, 18), (12, 21)], sw)
            elif kind == "check":
                line([(5, 12.5), (9.5, 17), (19, 7.5)], 2.4)
            elif kind == "cross":
                line([(7.5, 7.5), (16.5, 16.5)], 2.4)
                line([(16.5, 7.5), (7.5, 16.5)], 2.4)
            elif kind == "dash":
                line([(7, 12), (17, 12)], 2.4)
            else:
                line([(12, 6.5), (12, 13)], 2.4)
                dot(*P(12, 17.5), 3)
        return self._region(("glyph", kind, color, round(opacity * 32), sq), 14 * self.s * sq, draw)

    def _dots(self, frame, t: float, scale: float) -> None:
        s = self.s
        for i in range(3):
            lift, op = hop(t, i)
            self._paste(frame, self._circle(DOT_D * s * scale, FG, round(op * 16) / 16),
                        self.c + (i - 1) * DOT_STEP * s * scale, self.c - lift * s)

    def _advance(self, t: float, level: float) -> float:
        """The pulse ring's phase: moved on by the time since the last frame at the current level's
        speed (so a changing level never makes the ring jump)."""
        last, self._last_t = self._last_t, t
        if last is not None:
            self._phase = (self._phase + min(.25, max(0.0, t - last)) / pulse_period(level)) % 1.0
        return self._phase

    # --- frames ---

    def ready(self, hover: bool = False):
        """Idle: the dark circle with the mic (a lighter circle while the mouse is on it)."""
        if self._ripple_shapes:
            self.drop_ripples()
        frame = self._blank()
        frame.alpha_composite(self._core(1.0, BTN_CORE, hover))
        self._put(frame, self._glyph("mic", FG))
        return frame

    def listening(self, t: float, level: float, clipping: bool = False, writing: bool = False,
                  hover: bool = False):
        """Listening: a red halo and pulse ring that grow and speed up with the voice (amber while the
        input clips); Typing (writing): a blue halo and three hopping dots instead of the mic."""
        color = WORK if writing else (AMBER if clipping else RED)
        lvl = max(level, TYPING_LVL) if writing else min(1.0, max(0.0, level))
        frame = self._blank()
        frame.alpha_composite(self._halo(color, lvl))
        if self._last_t is None or t - self._last_t > .5:      # a new dictation: start the ripples afresh
            self._ripples = RippleEmitter()
        self._advance(t, lvl)
        if not writing:
            # Voice ripples: rings spread from the button like on water, more and stronger when louder.
            for sc, op, width in self._ripples.step(t, level):
                if op > .01:
                    self._put(frame, self._ring(color, sc, op, width))
        cs = core_scale(lvl)
        frame.alpha_composite(self._core(cs, BTN_CORE, hover))
        if writing:
            self._dots(frame, t, cs)
        else:
            self._put(frame, self._glyph("mic", FG, 1.0, cs))
        return frame

    def working(self, t: float, hover: bool = False):
        """Processing: the mic dimmed to .45 and a blue arc turning round the edge."""
        if self._ripple_shapes:                # listening is over: the ripple rings aren't needed
            self.drop_ripples()
        frame = self._blank()
        frame.alpha_composite(self._core(1.0, BTN_CORE, hover))
        self._put(frame, self._glyph("mic", FG, .45))
        self._put(frame, self._spinner(spinner_angle(t)))
        return frame

    def flash(self, color: str, since: float = 1e9, hover: bool = False):
        """A result, `since` seconds after it showed: Done (light circle, dark check, one blue pulse
        ring), or the other flash colours' circle and glyph."""
        if self._ripple_shapes:
            self.drop_ripples()
        state = button_state("flash", color=color)
        fill, glyph = flash_core(state)
        frame = self._blank()
        if state == "done" and since < pulse_period(0):
            sc, op = pulse_look(since / pulse_period(0), 0)
            if op > .01:
                self._put(frame, self._ring(WORK, sc, op))
        frame.alpha_composite(self._core(1.0, fill, False))
        self._put(frame, self._glyph(glyph, INK))
        return frame

    def move(self):
        """Move mode: the button with the amber move mark and a small hint label under it."""
        s = self.s
        frame = self._blank()
        frame.alpha_composite(self._core(1.0, BTN_CORE, False))
        self._paste(frame, self._move_mark(), self.c, self.c)
        hint = fit_text(MOVE_HINTS, lambda t: self.measure(t, 500, 12), self.size[0] - 24 * s)
        lw, lh = self.measure(hint, 500, 12) + 20 * s, 22 * s
        cy = self.pad[1] + self.h + 8 * s + lh / 2
        label = self._shape(("hint", round(lw), round(lh)), lw, lh, lambda dr, k, W, H: dr.rounded_rectangle(
            (0, 0, lw * k - 1, lh * k - 1), radius=lh * k / 2, fill=_rgb(BODY) + (235,),
            outline=_over(BODY, .1) + (255,), width=max(1, round(s * k))))
        self._paste(frame, label, self.c, cy)
        self._put_text(frame, hint, 500, 12, FG, self.c, cy, anchor="mm")
        return frame


class _Surface:
    """A per-pixel-alpha layered window's picture (UpdateLayeredWindow). Tk thread only."""

    def __init__(self, hwnd: int, size: tuple[int, int]):
        self.hwnd, self.size = hwnd, size
        w, h = size
        self._dc = gdi32.CreateCompatibleDC(None)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down
        self._bits = ctypes.c_void_p()
        self._bmp = gdi32.CreateDIBSection(self._dc, ctypes.byref(bmi), 0, ctypes.byref(self._bits), None, 0)
        if not self._bmp or not self._bits:
            raise OSError("CreateDIBSection failed")
        self._old = gdi32.SelectObject(self._dc, self._bmp)
        self._failed = False

    def present(self, frame: Image.Image | None, alpha: int, top: int = 0) -> None:
        """Show the window at `alpha` 0..255 with `frame` (RGBA, the window's width) replacing its rows
        from `top` down; None keeps the last picture."""
        if frame is not None:
            w, h = self.size
            if frame.width != w or not 0 <= top <= h - frame.height:
                raise ValueError(f"frame {frame.size} at row {top} doesn't fit the window {self.size}")
            raw = frame.tobytes("raw", "BGRa")          # premultiplied BGRA, as the window wants it
            ctypes.memmove(self._bits.value + top * w * 4, raw, len(raw))
        size, src = wt.SIZE(*self.size), wt.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, max(0, min(255, int(alpha))), AC_SRC_ALPHA)
        dirty = wt.RECT(0, top, self.size[0], top + frame.height) if frame is not None else wt.RECT(0, 0, 0, 0)
        info = UPDATELAYEREDWINDOWINFO(ctypes.sizeof(UPDATELAYEREDWINDOWINFO), None, None, ctypes.pointer(size),
                                       self._dc, ctypes.pointer(src), 0, ctypes.pointer(blend), ULW_ALPHA,
                                       ctypes.pointer(dirty) if frame is not None and top else None)
        ok = user32.UpdateLayeredWindowIndirect(self.hwnd, ctypes.byref(info))
        if not ok and not self._failed:
            self._failed = True
            log.error("UpdateLayeredWindow failed: %s", ctypes.get_last_error())

    def close(self) -> None:
        gdi32.SelectObject(self._dc, self._old)
        gdi32.DeleteObject(self._bmp)
        gdi32.DeleteDC(self._dc)


class Overlay:
    on_stop = None              # the stop button's callback (see __init__)
    on_click = None             # the round button's callback (see __init__)
    style = "pill"
    always_visible = False
    _clickable = False          # the mouse is over the stop button (the round button), so it takes clicks
    _near = False               # button: the mouse is close, so the hover check runs more often
    _pad = (0, 0, 0, 0)         # shadow room around the pill in the window: left, top, right, bottom
    _surface: _Surface | None = None
    _painter: Painter | ButtonPainter | None = None
    _alpha = 0
    _shown_shell = None         # the Painter shell the window's picture was last fully drawn with
    _placed_for = None          # button: (position, margin, saved position) it was last placed for
    _shown_at = 0.0
    _lvl = 0.0                  # button: the smoothed mic level the halo follows

    def __init__(self, position: str = "bottom", wide: bool = False, pos_file: Path | None = None,
                 on_stop=None, *, style: str = "pill", always_visible: bool = False, on_click=None):
        # on_stop(): the stop button was clicked while listening (called in a thread of its own).
        # The pill is click-through everywhere except over the stop button, and only while the mouse
        # is on it (checked every frame); it never takes focus, so the text cursor stays where it was.
        # style "button": the round button instead of the pill (wide and the live words are ignored).
        # on_click(): the button was clicked, in any state (in a thread of its own; the app decides what
        # it means). always_visible: the button shows Ready wherever it would otherwise hide.
        self.on_stop = on_stop
        self.style = "button" if style == "button" else "pill"
        self.always_visible = bool(always_visible)
        self.on_click = on_click
        self._clickable = False
        self.position = position
        self.wide = wide            # live streaming: room for the words heard but not typed yet
        self.margin = -1            # pixels from the screen edge(s) (at 100% scaling); -1 = 24 at the top, 56 at the
        #                             bottom, 24 from both edges in a corner (see preset_xy)
        self.pos_file = pos_file    # pill_position.json / button_position.json (None = never moved by the user)
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
        self._kw: dict = {}
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
        """Hide the pill; an always-visible button shows Ready instead (and moves to its place again when
        position, margin or the saved spot changed: call this after changing them while idle)."""
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
        painter = ButtonPainter(s) if self.style == "button" else Painter(s, self.wide)
        self.w, self.h = painter.w, painter.h       # the pill itself: what is placed and saved
        self._pad = painter.pad
        W, H = painter.size                         # the window: the pill plus room for its shadow
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)           # (no -alpha / -transparentcolor: they would stop
        root.geometry(f"{W}x{H}+-4000+-4000")       #  the per-pixel-alpha picture from working)
        self.canvas = tk.Canvas(root, width=W, height=H, bg=BODY, highlightthickness=0, bd=0)
        self.canvas.pack()
        # Move mode only: with WS_EX_TRANSPARENT / WS_EX_NOACTIVATE set these never fire.
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        for key in ("<Return>", "<KP_Enter>"):
            root.bind(key, lambda e: self._end_move(True))
        root.bind("<Escape>", lambda e: self._end_move(False))
        root.update_idletasks()
        self.hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        self._ex = ex | PILL_EX              # the pill's styles: restored exactly after move mode
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, self._ex)
        try:
            self._painter = painter
            self._surface = _Surface(self.hwnd, (W, H))
            self._surface.present(Image.new("RGBA", (W, H), (0, 0, 0, 0)), 0)    # clear and invisible
        except Exception:
            log.exception("pill window picture")
            self._surface = None
        # Tk's deiconify also activates the window ("raised and given the focus") when this process may
        # take the foreground (just after start), WS_EX_NOACTIVATE or not. Lock foreground changes for
        # that moment (only works while this process is in front, the one case it matters) and hand the
        # focus back if it got through anyway.
        prev_fg = user32.GetForegroundWindow()
        locked = user32.LockSetForegroundWindow(LSFW_LOCK)
        try:
            root.deiconify()                 # shown once, invisible and off-screen; never re-activated
            root.update()
        except tk.TclError:
            pass
        finally:
            if locked:
                user32.LockSetForegroundWindow(LSFW_UNLOCK)
        if user32.GetForegroundWindow() == self.hwnd and prev_fg and prev_fg != self.hwnd and user32.IsWindow(prev_fg):
            user32.SetForegroundWindow(prev_fg)
        self._root = root
        if self._keep_shown():
            root.after(0, self._hide)        # an always-visible button: Ready from the start
        self._ready.set()
        try:
            root.mainloop()
        finally:
            if self._surface is not None:
                self._surface.close()
                self._surface = None

    def _saved_position(self) -> pillpos.SavedPosition | None:
        if not self._saved_loaded:
            self._saved = pillpos.load(self.pos_file) if self.pos_file else None
            self._saved_loaded = True
        return self._saved

    def _put_window(self, x: int, y: int, flags: int) -> None:
        """Put the pill (not its shadow) at screen x, y."""
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x - self._pad[0], y - self._pad[1], 0, 0,
                            flags | SWP_NOSIZE | SWP_NOACTIVATE)

    def _place_key(self):
        try:
            saved = self._saved_position()
        except Exception:
            saved = None
        return self.position, self.margin, saved

    def _place(self) -> None:
        self._placed_for = self._place_key()
        try:
            saved = self._saved_position()
            xy = pillpos.placement(saved, pillpos.monitors(), self.w, self.h) if saved else None
        except Exception:                    # never let a position problem keep the pill from showing
            log.exception("saved pill position")
            xy = None
        if xy is not None:                   # where the user put it (stays on that monitor)
            self._put_window(xy[0], xy[1], SWP_SHOWWINDOW)
            return
        mon = user32.MonitorFromWindow(user32.GetForegroundWindow(), MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        user32.GetMonitorInfoW(mon, ctypes.byref(mi))
        wa = mi.rcWork
        x, y = preset_xy((wa.left, wa.top, wa.right, wa.bottom), self.w, self.h, self.position, self.margin, self.s)
        self._put_window(x, y, SWP_SHOWWINDOW)

    def _keep_shown(self) -> bool:
        """An always-visible button: Ready instead of hidden."""
        return self.style == "button" and self.always_visible

    def _set_alpha(self, alpha: int) -> None:
        self._alpha = alpha
        if self._surface is not None:
            self._surface.present(None, alpha)

    def _show(self, mode: str, **kw) -> None:
        if self._mode == "move":             # move mode owns the pill until it ends
            return
        if self._hide_job:
            self._root.after_cancel(self._hide_job)
            self._hide_job = None
        if self._mode == "hidden" or (mode == "ready" and self._place_key() != self._placed_for):
            self._place()
        button = self.style == "button"
        if mode != "listening" and not button:
            self._set_clickable(False)       # click-through everywhere again once listening ends
        if mode == "listening" and self._mode != "listening":
            self._lvl = 0.0
        self._mode = mode
        self._kw = kw
        self._shown_at = time.monotonic()
        if mode == "listening":
            self._level_fn, self._started, self._writing_fn = kw["level_fn"], kw["started"], kw["writing_fn"]
            self._clipping_fn, self._preview_fn = kw["clipping_fn"], kw["preview_fn"]
        self._alpha = SHOWN_ALPHA
        self._draw()                         # the new picture and the opacity in one step
        if mode == "flash":
            self._hide_job = self._root.after(kw["ms"], self._hide)
        if button:                           # every state: the hover check (and the animation, if any)
            if self._anim_job is not None:
                self._root.after_cancel(self._anim_job)
            self._anim_job = self._root.after(0, self._animate)
        elif mode != "flash" and self._anim_job is None:
            self._animate()

    def _hide(self, force: bool = False) -> None:
        if self._mode == "move" and not force:
            return
        if self._keep_shown() and self._mode not in ("move", "hidden"):
            self._show("ready")              # never hidden: straight to Ready, where it is
            return
        self._set_clickable(False)
        self._mode = "hidden"
        self._hide_job = None
        if self._anim_job:
            self._root.after_cancel(self._anim_job)
            self._anim_job = None
        self._set_alpha(0)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, -4000, -4000, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
        if self._keep_shown():               # after move mode (or at start): Ready at its (new) place
            self._show("ready")

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
        self._set_clickable(False)
        self._mode = "move"
        self._on_move_end = on_end
        self._grab = None
        self._set_styles(self._ex & ~MOVE_CLEARS)
        self._alpha = SHOWN_ALPHA
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
        left, top = left + self._pad[0], top + self._pad[1]        # the pill, not its shadow
        mon = pillpos.monitor_of_window(self.hwnd)
        if mon is None:
            return False
        pos = pillpos.to_relative(mon.device, left, top, self.w, self.h, mon.work)
        pillpos.save(self.pos_file, pos, mon.work)
        self._saved, self._saved_loaded = pos, True
        return True

    # --- the stop button (Tk thread) ---------------------------------------------------------------

    def _over_stop(self) -> bool:
        if self._mode != "listening" or self.on_stop is None:
            return False
        try:
            cx, cy = pillpos.cursor_pos()
            left, top, _, _ = pillpos.window_rect(self.hwnd)
        except Exception:
            return False
        return stop_hit(cx, cy, left, top, self._pad, self._painter.layout.stop_cx, self.h, self.s)

    def _set_clickable(self, on: bool) -> None:
        """Take the mouse (never the focus) only while it is over the stop button."""
        if on == self._clickable:
            return
        self._clickable = on
        self._set_styles(self._ex & ~WS_EX_TRANSPARENT if on else self._ex)
        try:
            self.canvas.configure(cursor="hand2" if on else "")
        except (tk.TclError, AttributeError):
            pass

    def _stop_clicked(self) -> None:
        self._set_clickable(False)
        on_stop = self.on_stop
        if on_stop is not None:
            threading.Thread(target=on_stop, daemon=True, name="pill-stop").start()

    # --- the round button (Tk thread) -------------------------------------------------------------

    def _over_button(self) -> bool:
        """Is the mouse on the round button (while it shows and has a callback)? Also notes whether it
        is near (then the hover check runs every 50 ms, else every 200 ms)."""
        self._near = False
        if self._mode in ("hidden", "move") or self.on_click is None:
            return False
        try:
            cx, cy = pillpos.cursor_pos()
            left, top, _, _ = pillpos.window_rect(self.hwnd)
        except Exception:
            return False
        self._near = button_hit(cx, cy, left, top, self._pad, self.w, slack=3 * self.w)
        return button_hit(cx, cy, left, top, self._pad, self.w, slack=2 * self.s)

    def _button_clicked(self) -> None:
        on_click = self.on_click
        if on_click is not None:
            threading.Thread(target=on_click, daemon=True, name="button-click").start()

    def _on_double(self, _event=None) -> None:
        # Tk sends a quick second click as <Double-Button-1> instead of <ButtonPress-1>: in move mode it
        # saves; on the round button it is just another click (start, then stop right away).
        if self._mode == "move":
            self._end_move(True)
        elif self.style == "button":
            self._on_press()

    def _on_press(self, _event=None) -> None:
        if self.style == "button" and self._mode not in ("move", "hidden"):
            if self._clickable and self._over_button():
                self._button_clicked()
            return
        if self._mode == "listening":
            if self._clickable and self._over_stop():
                self._stop_clicked()
            return
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
        if self.style == "button":
            self._animate_button()
            return
        if self._mode not in ("listening", "working"):
            self._anim_job = None
            return
        self._tick += 1
        self._set_clickable(self._over_stop())
        self._draw()
        self._anim_job = self._root.after(70, self._animate)

    def _animate_button(self) -> None:
        """The button's loop while it shows: the hover check every time (it takes clicks only while the
        mouse is on it); a new frame every 70 ms only while something moves, else only when the hover
        look changes."""
        if self._mode in ("hidden", "move"):
            self._anim_job = None
            return
        hover = self._over_button()
        changed = hover != self._clickable
        self._set_clickable(hover)
        moving = button_animates(button_state(self._mode, False, self._kw.get("color", GREEN)),
                                 time.monotonic() - self._shown_at)
        if moving or changed:
            self._draw()
        self._anim_job = self._root.after(70 if moving else 50 if self._near else 200, self._animate)

    # --- drawing ----------------------------------------------------------------------------------

    def _button_frame(self, now: float):
        p, kw, hover = self._painter, self._kw, self._clickable
        if self._mode == "listening":
            try:
                level, clipping = float(self._level_fn()), bool(self._clipping_fn())
            except Exception:
                level, clipping = 0.0, False
            try:
                writing = bool(self._writing_fn())
            except Exception:
                writing = False
            self._lvl = smooth_level(self._lvl, level)
            return p.listening(now, self._lvl, clipping, writing, hover)
        if self._mode == "working":
            return p.working(now, hover)
        if self._mode == "flash":
            return p.flash(kw.get("color", GREEN), now - self._shown_at, hover)
        if self._mode == "ready":
            return p.ready(hover)
        if self._mode == "move":
            return p.move()
        return None

    def _frame(self, now: float):
        """This moment's picture of the pill (None when hidden)."""
        if self.style == "button":
            return self._button_frame(now)
        p, kw = self._painter, self._kw
        if self._mode == "listening":
            try:
                level, clipping = float(self._level_fn()), bool(self._clipping_fn())
            except Exception:
                level, clipping = 0.0, False
            try:
                writing = bool(self._writing_fn())
            except Exception:
                writing = False
            preview = ""
            if self.wide:
                try:
                    preview = str(self._preview_fn() or "")
                except Exception:
                    preview = ""
            return p.listening(now, level, clipping, writing, preview, now - self._started, self._clickable)
        if self._mode == "working":
            return p.working(now, kw.get("text", "Transcribing…"))
        if self._mode == "flash":
            return p.flash(kw.get("text", ""), kw.get("color", GREEN))
        if self._mode == "move":
            return p.move()
        return None

    def _draw(self) -> None:
        if self._surface is None or self._painter is None:
            return
        p = self._painter
        try:
            band = self._frame(time.monotonic())
            if band is not None and p.shell is not self._shown_shell:    # a new shape: the whole window
                self._surface.present(p.full(band), self._alpha)
                self._shown_shell = p.shell
            else:                                                           # just the pill's rows
                self._surface.present(band, self._alpha, p.band[0])
        except Exception:                    # a drawing problem must never take the app down
            log.exception("drawing the pill")
