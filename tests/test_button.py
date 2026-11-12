"""The round button style of the overlay: placement presets, its looks, the hover hit test,
always-visible behaviour and clicks (Pillow only; no window is created)."""
import threading
import types

import pytest

from parakeet_dictate import overlay
from parakeet_dictate.overlay import (AMBER, GREEN, MUTED, RED, ButtonPainter, button_animates, button_hit,
                                      button_state, flash_core, preset_xy, pill_y)

WORK_AREA = (0, 0, 1920, 1040)


# --- placement ------------------------------------------------------------------------------------------

def test_centred_presets_are_the_pill_ones():
    for pos in ("top", "bottom"):
        for margin in (-1, 64):
            x, y = preset_xy(WORK_AREA, 56, 56, pos, margin, 1.0)
            assert x == (1920 - 56) // 2 and y == pill_y(0, 1040, 56, pos, margin, 1.0)


def test_corners_default_to_24_from_both_edges():
    assert preset_xy(WORK_AREA, 56, 56, "top-left", -1, 1.0) == (24, 24)
    assert preset_xy(WORK_AREA, 56, 56, "top-right", -1, 1.0) == (1920 - 56 - 24, 24)
    assert preset_xy(WORK_AREA, 56, 56, "bottom-left", -1, 1.0) == (24, 1040 - 56 - 24)
    assert preset_xy(WORK_AREA, 56, 56, "bottom-right", -1, 1.0) == (1920 - 80, 1040 - 80)


def test_corner_margin_scales_and_follows_the_monitor():
    assert preset_xy(WORK_AREA, 84, 84, "bottom-right", 10, 1.5) == (1920 - 84 - 15, 1040 - 84 - 15)
    left_monitor = (-1920, 40, 0, 1080)                 # left of the main one, taskbar on top
    assert preset_xy(left_monitor, 56, 56, "top-right", -1, 1.0) == (-56 - 24, 40 + 24)


def test_unknown_position_is_bottom_and_huge_margins_stay_on_screen():
    assert preset_xy(WORK_AREA, 56, 56, "middle", -1, 1.0) == preset_xy(WORK_AREA, 56, 56, "bottom", -1, 1.0)
    assert preset_xy((0, 0, 300, 200), 56, 56, "bottom-right", 600, 1.0) == (0, 0)
    assert preset_xy((0, 0, 300, 200), 56, 56, "top", 600, 1.0) == ((300 - 56) // 2, 200 - 56)


# --- states ---------------------------------------------------------------------------------------------

def test_state_mapping():
    assert button_state("ready") == "ready" and button_state("hidden") == "ready"
    assert button_state("listening") == "listening"
    assert button_state("listening", writing=True) == "typing"
    assert button_state("working") == "processing"
    assert button_state("flash", color=GREEN) == "done"
    assert button_state("flash", color=MUTED.upper()) == "muted"
    assert button_state("flash", color=RED) == "error"
    assert button_state("flash", color=AMBER) == "alert"
    assert button_state("move") == "move"
    assert flash_core("done") == (overlay.FG, "check")
    assert flash_core("error") == (RED, "cross") and flash_core("muted") == (MUTED, "dash")


def test_only_moving_states_animate():
    assert button_animates("listening", 5) and button_animates("typing", 5) and button_animates("processing", 5)
    assert not button_animates("ready", 0) and not button_animates("error", 0) and not button_animates("muted", 0)
    assert button_animates("done", 0.2) and not button_animates("done", 2.0)    # one pulse ring, then still


def test_curves():
    assert overlay.halo_look(0) == (1, .12, 2)
    assert overlay.halo_look(1) == pytest.approx((1.75, .52, 8))
    assert overlay.halo_look(5) == overlay.halo_look(1)
    assert overlay.pulse_period(0) == 1.8 and overlay.pulse_period(1) == pytest.approx(.9)
    assert overlay.pulse_look(0, .5) == (1, .7)
    assert overlay.pulse_look(1, 1) == pytest.approx((2.1, 0))
    assert overlay.hop(0, 0) == (0, .5) and overlay.hop(.4, 0) == (4, 1)
    assert overlay.hop(.55, 1) == pytest.approx((4, 1))                      # the next dot, .15 s later
    assert overlay.spinner_angle(0) == 0 and overlay.spinner_angle(.55) == pytest.approx(180)
    assert overlay.core_scale(1) == pytest.approx(1.06)


def test_level_rises_fast_and_falls_slowly():
    up = overlay.smooth_level(0, 1)
    down = overlay.smooth_level(1, 0)
    assert up > .5 and down > .5 and 0 <= overlay.smooth_level(0, -3) <= overlay.smooth_level(.5, 9) <= 1


# --- the hit test ---------------------------------------------------------------------------------------

def test_hit_is_the_circle_only():
    pad, d = (52, 52, 52, 52), 56
    cx, cy = 100 + 52 + 28, 50 + 52 + 28                  # window at (100, 50)
    assert button_hit(cx, cy, 100, 50, pad, d)
    assert button_hit(cx + 28, cy, 100, 50, pad, d)        # its edge
    assert not button_hit(cx + 28 + 4, cy, 100, 50, pad, d)
    assert not button_hit(cx + 26, cy + 26, 100, 50, pad, d)   # the corner of its square: outside the circle
    assert button_hit(cx + 26, cy + 26, 100, 50, pad, d, slack=20)


# --- the painter ----------------------------------------------------------------------------------------

def test_sizes_follow_display_scaling():
    p = ButtonPainter(1.0)
    assert (p.w, p.h, p.pad, p.size, p.c) == (56, 56, (52,) * 4, (160, 160), 80)
    q = ButtonPainter(1.5)
    assert q.w == 84 and q.size == (84 + 2 * 78,) * 2
    for frame in (p.ready(), p.listening(0, .5), p.listening(0, .5, writing=True), p.working(0),
                  p.flash(GREEN), p.flash(RED), p.move()):
        assert frame.size == p.size and frame.mode == "RGBA"
        assert p.full(frame) is frame


def _px(img, x, y):
    return img.getpixel((round(x), round(y)))


def test_ready_hover_done_and_error_cores():
    p = ButtonPainter(1.0)
    x, y = p.c - 20, p.c                                  # on the circle, beside the mic
    plain, hover = _px(p.ready(), x, y), _px(p.ready(hover=True), x, y)
    assert plain[3] == 255 and sum(hover[:3]) > sum(plain[:3]) and max(plain[:3]) < 40
    assert min(_px(p.flash(GREEN), x, y)[:3]) > 230                      # Done: the light circle
    r, g, b, _ = _px(p.flash(RED), x, y)
    assert r > 200 and g < 120                                            # error: red


def test_halo_grows_with_the_voice_and_changes_colour():
    def at(level, **kw):
        p = ButtonPainter(1.0)                    # a fresh one: the pulse ring starts at the edge
        return _px(p.listening(0, level, **kw), p.c + 40, p.c)
    assert at(.9)[3] > at(.1)[3] + 60
    r, g, b, _ = at(.9)
    assert r > b                                                          # red while listening
    r, g, b, _ = at(.5, writing=True)
    assert b > r                                                          # blue while typing
    r, g, b, _ = at(.9, clipping=True)
    assert r > b and g > b                                                # amber while too loud


def test_processing_draws_the_blue_arc():
    p = ButtonPainter(1.0)
    import math
    frame = p.working(0.0)                        # the arc starts at 3 o'clock and runs ~100° clockwise
    r, g, b, a = _px(frame, p.c + 27 * math.cos(math.radians(50)), p.c + 27 * math.sin(math.radians(50)))
    assert a > 200 and b > r
    r, g, b, a = _px(frame, p.c - 27, p.c)        # not on the other side
    assert b < 100


# --- the window logic (Overlay without its window) --------------------------------------------------------

class _Root:
    def __init__(self):
        self.jobs, self.n = {}, 0

    def after(self, ms, fn):
        self.n += 1
        self.jobs[self.n] = (ms, fn)
        return self.n

    def after_cancel(self, job):
        self.jobs.pop(job, None)


def _button(monkeypatch, mode="hidden", always=False, style="button"):
    o = overlay.Overlay.__new__(overlay.Overlay)
    o.style, o.always_visible = style, always
    o._mode, o._clickable, o._ex, o.hwnd = mode, False, overlay.PILL_EX, 1
    o._root, o._hide_job, o._anim_job, o._kw = _Root(), None, None, {}
    o.position, o.margin, o._saved, o._saved_loaded = "bottom-right", -1, None, True
    o.styles = []
    o._set_styles = o.styles.append
    o.canvas = types.SimpleNamespace(configure=lambda **kw: None)
    o.draws, o.places = [], []
    o._draw = lambda: o.draws.append(o._mode)

    def place():
        o.places.append(o.position)
        o._placed_for = o._place_key()
    o._place = place
    o._set_alpha = lambda a: setattr(o, "_alpha", a)
    o.clicked, o.click_threads = threading.Event(), []

    def click():
        o.click_threads.append(threading.current_thread().name)
        o.clicked.set()
    o.on_click = click
    o.s, o.w, o.h, o._pad = 1.0, 56, 56, (52,) * 4
    monkeypatch.setattr(overlay, "user32", types.SimpleNamespace(SetWindowPos=lambda *a: 1))
    return o


def _run_hide_job(o):
    ms, fn = o._root.jobs[o._hide_job]
    fn()
    return ms


def test_always_visible_shows_ready_instead_of_hiding(monkeypatch):
    o = _button(monkeypatch, always=True)
    o._hide()                                              # at start
    assert o._mode == "ready" and o._alpha == overlay.SHOWN_ALPHA and o.places == ["bottom-right"]
    o._show("flash", text="Typed", color=GREEN, ms=700)
    assert o._mode == "flash" and _run_hide_job(o) == 700
    assert o._mode == "ready" and o._alpha == overlay.SHOWN_ALPHA
    assert o.places == ["bottom-right"]                   # stays where it is


def test_always_visible_moves_when_its_place_changes(monkeypatch):
    o = _button(monkeypatch, always=True)
    o._hide()
    o._hide()
    assert o.places == ["bottom-right"]
    o.position = "top-left"
    o._hide()                                              # the app changed the setting, then hide()
    assert o.places == ["bottom-right", "top-left"] and o._mode == "ready"


def test_without_always_visible_it_hides_like_the_pill(monkeypatch):
    o = _button(monkeypatch)
    o._show("flash", text="Typed", color=GREEN, ms=700)
    _run_hide_job(o)
    assert o._mode == "hidden" and o._alpha == 0
    p = _button(monkeypatch, always=True, style="pill")   # always_visible is for the button only
    p._show("flash", text="Typed", color=GREEN, ms=700)
    _run_hide_job(p)
    assert p._mode == "hidden"


def test_after_move_mode_it_comes_back_as_ready(monkeypatch):
    o = _button(monkeypatch, mode="move", always=True)
    o._hide(force=True)
    assert o._mode == "ready" and o.places


def test_click_on_the_button_calls_on_click_in_its_own_thread(monkeypatch):
    for mode in ("ready", "listening", "working", "flash"):
        o = _button(monkeypatch, mode=mode)
        monkeypatch.setattr(o, "_over_button", lambda: True)
        o._set_clickable(True)
        assert not (o.styles[-1] & overlay.WS_EX_TRANSPARENT)
        assert o.styles[-1] & overlay.WS_EX_NOACTIVATE        # never takes the focus
        o._on_press()
        assert o.clicked.wait(2) and o.click_threads == ["button-click"]
        assert o._clickable                                   # can be clicked again (to stop)


def test_quick_second_click_is_a_click_too(monkeypatch):
    o = _button(monkeypatch, mode="listening")
    monkeypatch.setattr(o, "_over_button", lambda: True)
    o._clickable = True
    o._on_double()
    assert o.clicked.wait(2)


def test_no_click_off_the_button_or_while_hidden(monkeypatch):
    o = _button(monkeypatch, mode="ready")
    o._clickable = True
    monkeypatch.setattr(o, "_over_button", lambda: False)
    o._on_press()
    h = _button(monkeypatch, mode="hidden")
    h._clickable = True
    monkeypatch.setattr(h, "_over_button", lambda: True)
    h._on_press()
    assert not o.clicked.wait(0.2) and not h.clicked.is_set()


def test_over_button_needs_a_callback_and_a_shown_button(monkeypatch):
    o = _button(monkeypatch, mode="ready")
    monkeypatch.setattr(overlay.pillpos, "cursor_pos", lambda: (100 + 80, 50 + 80))
    monkeypatch.setattr(overlay.pillpos, "window_rect", lambda hwnd: (100, 50, 260, 210))
    assert o._over_button() and o._near
    o.on_click = None
    assert not o._over_button()
    o.on_click, o._mode = print, "hidden"
    assert not o._over_button()


def test_move_mode_drags_the_button(monkeypatch):
    o = _button(monkeypatch, mode="move")
    monkeypatch.setattr(overlay.pillpos, "cursor_pos", lambda: (130, 70))
    monkeypatch.setattr(overlay.pillpos, "window_rect", lambda hwnd: (100, 50, 260, 210))
    o._on_press()
    assert o._grab == (30, 20) and not o.clicked.is_set()


def test_ready_is_drawn_once_and_again_only_on_hover_change(monkeypatch):
    o = _button(monkeypatch, mode="ready")
    over = [False]
    monkeypatch.setattr(o, "_over_button", lambda: over[0])
    o._shown_at = 0.0
    o._animate_button()
    assert o.draws == [] and o._root.jobs[o._anim_job][0] == 200
    over[0] = True
    o._animate_button()
    o._animate_button()
    assert o.draws == ["ready"] and o._clickable                  # one redraw: the lighter look
    over[0] = False
    o._animate_button()
    assert o.draws == ["ready", "ready"] and not o._clickable


def test_listening_animates_every_70_ms(monkeypatch):
    o = _button(monkeypatch, mode="listening")
    monkeypatch.setattr(o, "_over_button", lambda: False)
    o._shown_at = 0.0
    o._animate_button()
    o._animate_button()
    assert o.draws == ["listening", "listening"] and o._root.jobs[o._anim_job][0] == 70


def test_the_loop_stops_when_hidden(monkeypatch):
    o = _button(monkeypatch, mode="hidden")
    o._animate_button()
    assert o._anim_job is None and o.draws == []
