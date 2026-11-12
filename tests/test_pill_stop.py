"""The pill's stop button: where it can be clicked, and what a click does."""
import threading
import types

from parakeet_dictate import overlay
from parakeet_dictate.app import App
from parakeet_dictate.overlay import STOP_D, Painter, stop_hit


def test_stop_hit_is_the_round_button_only():
    p = Painter(1.0, True)
    L, pad = p.layout, p.pad
    cx, cy = 100 + pad[0] + L.stop_cx, 50 + pad[1] + p.h / 2       # window at (100, 50)
    assert stop_hit(cx, cy, 100, 50, pad, L.stop_cx, p.h, 1.0)
    assert stop_hit(cx + STOP_D / 2, cy, 100, 50, pad, L.stop_cx, p.h, 1.0)          # its edge
    assert not stop_hit(cx + STOP_D / 2 + 4, cy, 100, 50, pad, L.stop_cx, p.h, 1.0)  # just outside
    assert not stop_hit(100 + pad[0] + L.dot_cx, cy, 100, 50, pad, L.stop_cx, p.h, 1.0)  # the red dot


def test_stop_hit_follows_display_scaling():
    p = Painter(1.5, False)
    L, pad = p.layout, p.pad
    cx, cy = pad[0] + L.stop_cx * 1.5, pad[1] + p.h / 2
    assert stop_hit(cx, cy, 0, 0, pad, L.stop_cx, p.h, 1.5)
    assert not stop_hit(cx - STOP_D * 1.5, cy, 0, 0, pad, L.stop_cx, p.h, 1.5)


def test_hovered_button_is_drawn_lighter():
    p = Painter(1.0, True)
    plain = p.full(p.listening(0.0, 0.3, False, False, "", 3.0))
    hover = p.full(p.listening(0.0, 0.3, False, False, "", 3.0, hover=True))
    x, y = round(p.pad[0] + p.layout.stop_cx - STOP_D / 2 + 4), round(p.pad[1] + p.h / 2)
    assert sum(hover.getpixel((x, y))[:3]) > sum(plain.getpixel((x, y))[:3])


def _pill(mode="listening"):
    """An Overlay without its window, for the click logic."""
    o = overlay.Overlay.__new__(overlay.Overlay)
    o._mode, o._clickable, o._ex, o.hwnd = mode, False, overlay.PILL_EX, 1
    o.styles = []
    o._set_styles = o.styles.append
    o.canvas = types.SimpleNamespace(configure=lambda **kw: None)
    o.stopped = threading.Event()
    o.on_stop = o.stopped.set
    return o


def test_clickable_only_while_over_the_button(monkeypatch):
    o = _pill()
    over = [True]
    monkeypatch.setattr(o, "_over_stop", lambda: over[0])
    o._set_clickable(o._over_stop())
    assert o._clickable and not (o.styles[-1] & overlay.WS_EX_TRANSPARENT)
    assert o.styles[-1] & overlay.WS_EX_NOACTIVATE                    # never takes the focus
    over[0] = False
    o._set_clickable(o._over_stop())
    assert not o._clickable and o.styles[-1] == overlay.PILL_EX        # click-through again


def test_click_on_the_button_stops(monkeypatch):
    o = _pill()
    monkeypatch.setattr(o, "_over_stop", lambda: True)
    o._set_clickable(True)
    o._on_press()
    assert o.stopped.wait(2) and not o._clickable


def test_click_elsewhere_does_nothing(monkeypatch):
    o = _pill()
    o._clickable = True
    monkeypatch.setattr(o, "_over_stop", lambda: False)
    o._on_press()
    assert not o.stopped.wait(0.2)


def test_app_stops_only_a_recording():
    calls = []
    app = App.__new__(App)
    app._lock, app.state = threading.Lock(), "recording"
    app._finish_locked = lambda: calls.append("finish")
    app.stop_from_pill()
    app.state = "busy"
    app.stop_from_pill()
    assert calls == ["finish"]
