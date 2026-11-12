"""The pill's look: layout maths, animation curves, text fitting and the Painter's frames (Pillow
only; no window is created)."""
import pytest

from parakeet_dictate import overlay as ov


# --- layout -------------------------------------------------------------------------------------------

def test_wide_layout_is_the_design():
    L = ov.listening_layout(61, wide=True)                   # Inter 600 14 px "Listening" is ~61 px
    assert L.width == 580 and L.dot_cx == 29 and L.label_x == 44
    assert L.bars_x == 44 + 61 + 16                           # gap 16 after the label
    assert L.mark_cx == L.bars_x + 9 * 3 + 8 * 3 + 16 + 0.5   # the 1 px divider
    assert L.text_x == L.mark_cx + 0.5 + 16
    assert L.stop_cx == 580 - 8 - 22 and L.timer_right == L.stop_cx - 22 - 16
    assert L.text_right(20) == L.timer_right - 34 - 16       # the timer keeps a 34 px minimum
    assert L.text_right(40) == L.timer_right - 40 - 16


def test_narrow_layout_drops_the_live_words():
    L = ov.listening_layout(61, wide=False)
    assert L.width == L.mark_cx + 0.5 + 16 + 34 + 16 + 44 + 8
    assert L.width < 580 and L.stop_cx == L.width - 30


def test_short_pills_fit_their_text():
    assert ov.compact_width(44, 100, 580) == 44 + 100 + 24
    assert ov.compact_width(44, 900, 580) == 580


def test_fit_end():
    measure = lambda t: 10 * len(t)        # noqa: E731
    assert ov.fit_end("Typed", measure, 100) == "Typed"
    assert ov.fit_end("Something went wrong", measure, 100) == "Something…"
    assert ov.fit_end("abc", measure, 5) == "…"


# --- animation ----------------------------------------------------------------------------------------

def test_bars_follow_the_level():
    assert ov.bar_scales(0.4, 0.0) == [0.25] * 9              # silence: flat at the .25 floor
    full = ov.bar_scales(0.4, 1.0)
    assert len(full) == 9 and all(0.25 <= v <= 1.0 for v in full)
    half = ov.bar_scales(0.4, 0.5)
    assert all(h <= f for h, f in zip(half, full))
    assert ov.bar_scales(0.4, 7.0) == full                    # clamped
    # the design's wave: bar 4 (delay -.4 s) is at its top 0.2 s later, at its floor 0.8 s later
    assert ov.bar_scales(0.2, 1.0)[3] == pytest.approx(1.0)
    assert ov.bar_scales(0.8, 1.0)[3] == pytest.approx(0.25)


def test_ring_and_caret():
    assert ov.ring_look(0.0) == (1.0, 0.55)
    scale, op = ov.ring_look(1.8 - 1e-9)
    assert scale == pytest.approx(2.6) and op == pytest.approx(0.0, abs=1e-6)
    assert ov.ring_look(3.6) == ov.ring_look(0.0)             # repeats every 1.8 s
    assert ov.caret_on(0.1) and not ov.caret_on(0.6) and ov.caret_on(1.2)


def test_flash_icons_and_clock():
    assert ov.flash_icon(ov.GREEN) == "check" and ov.flash_icon(ov.RED) == "cross"
    assert ov.flash_icon(ov.MUTED) == "dash" and ov.flash_icon(ov.MUTED.upper()) == "dash"
    assert ov.flash_icon("#123456") == "bang"
    assert ov.clock(0) == "0:00" and ov.clock(75.9) == "1:15" and ov.clock(-3) == "0:00"


def test_inter_covers_latin_only():
    assert ov.inter_covers("Héllo — “quotes” … €5")
    assert not ov.inter_covers("Привет") and not ov.inter_covers("日本")
    assert ov.inter_covers("")


# --- frames -------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def painters():
    return ov.Painter(1.0, True), ov.Painter(1.5, False)


def _px(img, x, y):
    return img.getpixel((round(x), round(y)))


def test_frames_are_the_window_size_with_room_for_the_shadow(painters):
    wide, narrow = painters
    assert wide.w == 580 and wide.h == 60 and wide.size == (580 + 80, 60 + 80)
    assert narrow.h == 90 and narrow.pad == (60, 33, 60, 87)          # 150 % scaling
    assert narrow.band == (33, 33 + 90) and narrow.cy == 45
    bands = [(wide, wide.listening(0.3, 0.5, False, True, "hello there", 4)), (wide, wide.working(0.1, "Transcribing…")),
             (narrow, narrow.listening(0.3, 0.5, True, False, "", 4)), (narrow, narrow.flash("Typed", ov.GREEN)),
             (narrow, narrow.move())]
    for p, band in bands:                    # frames are the pill's rows only, the full window on request
        assert band.size == (p.size[0], p.h) and band.mode == "RGBA"
        assert p.full(band).size == p.size


def test_listening_frame_pixels(painters):
    wide, _ = painters
    f = wide.full(wide.listening(1.25, 1.0, False, False, "", 3))
    x0, cy = wide.pad[0], wide.pad[1] + wide.cy
    assert _px(f, 0, 0)[3] == 0                                        # outside: clear
    assert 0 < _px(f, x0 + 290, wide.pad[1] + 60 + 20)[3] < 80         # the soft shadow below
    assert _px(f, x0 + 290, wide.pad[1] + 30)[:3] == ov._rgb(ov.BODY)  # the pill body, opaque
    assert _px(f, x0 + 290, wide.pad[1] + 30)[3] == 255
    assert _px(f, x0 + 29, cy)[:3] == ov._rgb(ov.RED)                  # the red dot
    assert _px(f, x0 + 550, cy)[:3] == ov._rgb(ov.FG)                  # the stop mark


def test_short_pills_are_centred_and_narrower(painters):
    wide, _ = painters
    f = wide.flash("Typed", ov.GREEN)
    alpha_row = [f.getpixel((x, round(wide.cy)))[3] for x in range(f.width)]
    assert wide.full(f).getpixel((10, 10))[3] == 0          # the short pill's own, narrower shadow
    body = [x for x, a in enumerate(alpha_row) if a == 255]
    assert body and body[-1] - body[0] < 200
    assert abs((body[0] + body[-1]) / 2 - f.width / 2) <= 1


def test_long_live_words_keep_their_end(painters):
    wide, _ = painters
    text = "word " * 80 + "END"
    f = wide.listening(0.2, 0.3, False, False, text, 9)          # must not raise, words cut at the left
    assert f.size == (wide.size[0], wide.h)


def test_unusual_text_falls_back_to_segoe(painters):
    wide, _ = painters
    assert wide.measure("Привет") > 0
    wide.listening(0.2, 0.3, False, False, "Привет мир", 9)
