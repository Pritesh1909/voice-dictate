"""Moving the pill: position maths, the saved file, move mode on the overlay (Win32 and Tk faked: no
window is shown) and the app's idle -> moving -> idle state machine."""
import json
import threading
import types

import pytest

from parakeet_dictate import overlay as ov
from parakeet_dictate import pillpos
from parakeet_dictate.app import App

WORK = (0, 0, 1920, 1040)                 # 1920x1080 with a 40 px taskbar
RIGHT = (1920, 0, 4480, 1400)             # a 2560x1440 monitor to its right, taskbar 40 px


# --- maths ------------------------------------------------------------------------------------------

def test_relative_and_back_is_the_same_spot():
    pos = pillpos.to_relative("\\\\.\\DISPLAY1", 842, 940, 236, 44, WORK)
    assert (pos.x, pos.y) == (842, 940)
    assert pos.fx == pytest.approx(842 / (1920 - 236), abs=1e-5) and pos.fy == pytest.approx(940 / 996, abs=1e-5)
    assert pillpos.to_absolute(pos, 236, 44, WORK) == (842, 940)


def test_fractions_keep_the_spot_when_resolution_or_scaling_changes():
    pos = pillpos.to_relative("D", 1920 - 236, 0, 236, 44, WORK)      # top-right corner
    assert (pos.fx, pos.fy) == (1.0, 0.0)
    # same monitor at 150 %: the pill is bigger, the work area is now 2560x1400
    assert pillpos.to_absolute(pos, 354, 66, (0, 0, 2560, 1400)) == (2560 - 354, 0)
    mid = pillpos.to_relative("D", (1920 - 236) // 2, 498, 236, 44, WORK)
    x, y = pillpos.to_absolute(mid, 236, 44, (0, 0, 1280, 680))
    assert abs(x - (1280 - 236) / 2) <= 1 and abs(y - (680 - 44) / 2) <= 1


def test_clamped_fully_inside_the_work_area():
    assert pillpos.clamp(-50, 2000, 236, 44, WORK) == (0, 1040 - 44)
    assert pillpos.clamp(5000, -9, 236, 44, RIGHT) == (4480 - 236, 0)
    pos = pillpos.to_relative("D", 3000, 3000, 236, 44, WORK)           # dropped off the screen
    assert (pos.fx, pos.fy) == (1.0, 1.0) and (pos.x, pos.y) == (1920 - 236, 1040 - 44)
    assert pillpos.clamp(10, 10, 3000, 3000, WORK) == (0, 0)            # pill bigger than the screen
    assert pillpos.to_absolute(pillpos.SavedPosition("D", 0.5, 0.5, 0, 0), 3000, 3000, WORK) == (0, 0)


def test_placement_on_the_saved_monitor_or_none():
    mons = [pillpos.Monitor("\\\\.\\DISPLAY1", WORK), pillpos.Monitor("\\\\.\\DISPLAY2", RIGHT)]
    pos = pillpos.to_relative("\\\\.\\DISPLAY2", 1920 + 100, 50, 236, 44, RIGHT)
    assert pillpos.placement(pos, mons, 236, 44) == (2020, 50)
    assert pillpos.placement(pos, mons[:1], 236, 44) is None               # that monitor was unplugged
    assert pillpos.placement(None, mons, 236, 44) is None
    lower = pillpos.SavedPosition("\\\\.\\display2", 0.0, 0.0, 0, 0)        # device names: any capitals
    assert pillpos.placement(lower, mons, 236, 44) == (1920, 0)


# --- the file -----------------------------------------------------------------------------------------

def test_save_and_load(tmp_path):
    f = tmp_path / "sub" / "pill_position.json"
    pos = pillpos.to_relative("\\\\.\\DISPLAY2", 2020, 50, 236, 44, RIGHT)
    pillpos.save(f, pos, RIGHT)
    d = json.loads(f.read_text("utf-8"))
    assert d["device"] == "\\\\.\\DISPLAY2" and d["x"] == 100 and d["work_area"] == list(RIGHT)
    assert pillpos.load(f) == pos
    assert not (tmp_path / "sub" / "pill_position.json.tmp").exists()
    assert pillpos.reset(f) and not f.exists() and not pillpos.reset(f)


@pytest.mark.parametrize("text", ["", "{", "[]", "null", '{"device": "D"}', '{"device": 3, "fx": 0, "fy": 0}',
                                  '{"device": "", "fx": 0, "fy": 0}', '{"device": "D", "fx": "a", "fy": 0}',
                                  '{"device": "D", "fx": true, "fy": 0}', '{"device": "D", "fx": NaN, "fy": 0}',
                                  "\xff\xfe garbage"])
def test_broken_file_is_ignored(tmp_path, text):
    f = tmp_path / "pill_position.json"
    f.write_text(text, "latin-1")
    assert pillpos.load(f) is None
    assert pillpos.load(tmp_path / "missing.json") is None


def test_out_of_range_fractions_are_clamped(tmp_path):
    f = tmp_path / "p.json"
    f.write_text('{"device": "D", "fx": 7, "fy": -1}', "utf-8")
    assert pillpos.load(f) == pillpos.SavedPosition("D", 1.0, 0.0, 0, 0)


def test_hint_fits_the_pill():
    measure = lambda t: 6 * len(t)        # noqa: E731
    assert ov.fit_text(ov.MOVE_HINTS, measure, 400) == ov.MOVE_HINTS[0]
    assert ov.fit_text(ov.MOVE_HINTS, measure, 200) == ov.MOVE_HINTS[1]
    assert ov.fit_text(ov.MOVE_HINTS, measure, 10) == ov.MOVE_HINTS[-1]


# --- move mode on the overlay (no window) ---------------------------------------------------------

HWND, OTHER = 4242, 777


class FakeUser32:
    def __init__(self, foreground=OTHER):
        self.fg = foreground
        self.styles: list[int] = []
        self.moves: list[tuple] = []
        self.focused: list[int] = []

    def GetForegroundWindow(self):
        return self.fg

    def SetForegroundWindow(self, h):
        self.focused.append(h)
        self.fg = h
        return 1

    def IsWindow(self, h):
        return 1

    def SetWindowLongPtrW(self, h, idx, value):
        assert (h, idx) == (HWND, ov.GWL_EXSTYLE)
        self.styles.append(value)
        return 0

    def SetWindowPos(self, h, after, x, y, cx, cy, flags):
        assert flags & ov.SWP_NOACTIVATE           # the pill never activates itself
        self.moves.append((x, y, flags))
        return 1

    def MonitorFromWindow(self, *a):
        return 0

    def GetMonitorInfoW(self, *a):
        return 0


class FakeRoot:
    def __init__(self):
        self.alpha = 0.0
        self.jobs = []
        self.focus_forced = 0

    def after(self, ms, fn):
        self.jobs.append((ms, fn))
        return f"job{len(self.jobs)}"

    def after_cancel(self, job):
        pass

    def attributes(self, name, value):
        if name == "-alpha":
            self.alpha = value

    def focus_force(self):
        self.focus_forced += 1


class FakeCanvas:
    def __getattr__(self, name):
        return lambda *a, **k: None


START_EX = 0x100 | ov.PILL_EX               # the pill's styles, plus whatever Tk had set


@pytest.fixture
def pill(tmp_path, monkeypatch):
    u = FakeUser32()
    monkeypatch.setattr(ov, "user32", u)
    monkeypatch.setattr(pillpos, "monitors", lambda: [pillpos.Monitor("\\\\.\\DISPLAY1", WORK),
                                                      pillpos.Monitor("\\\\.\\DISPLAY2", RIGHT)])
    o = ov.Overlay.__new__(ov.Overlay)      # no Tk thread, no window
    o.position, o.wide, o.margin = "bottom", False, -1
    o.pos_file = tmp_path / "pill_position.json"
    o._saved, o._saved_loaded, o._on_move_end, o._prev_fg, o._grab = None, False, None, None, None
    o.hwnd, o._ex, o.s, o.w, o.h = HWND, START_EX, 1.0, 236, 44
    o._root, o.canvas = FakeRoot(), FakeCanvas()
    o._mode, o._hide_job, o._anim_job, o._tick, o._kw = "hidden", None, None, 0, {}
    o.u = u
    return o


def _ended(o):
    got, done = [], threading.Event()

    def on_end(saved):
        assert threading.current_thread().name == "pill-moved"   # never on the Tk thread
        got.append(saved)
        done.set()
    return on_end, got, done


def test_move_mode_takes_the_mouse_and_gives_the_exact_styles_back(pill, monkeypatch):
    on_end, got, done = _ended(pill)
    pill._begin_move(on_end)
    assert pill.moving and pill._root.alpha > 0.9 and pill._root.focus_forced == 1
    assert pill.u.styles[-1] == START_EX & ~(ov.WS_EX_TRANSPARENT | ov.WS_EX_NOACTIVATE)
    assert pill.u.styles[-1] & ov.WS_EX_TOOLWINDOW and pill.u.styles[-1] & ov.WS_EX_LAYERED
    pill._show("listening")                 # nothing else may take the pill over meanwhile
    pill._hide()
    assert pill.moving
    # drag: grab the pill 30 px from its left edge, move the mouse onto the second monitor
    monkeypatch.setattr(pillpos, "window_rect", lambda h: (842, 940, 842 + 236, 940 + 44))
    monkeypatch.setattr(pillpos, "cursor_pos", lambda: (872, 950))
    pill._on_press()
    monkeypatch.setattr(pillpos, "cursor_pos", lambda: (2100, 70))
    pill._on_drag()
    assert pill.u.moves[-1][:2] == (2070, 60)
    pill._on_release()
    # Enter saves: the position of the window, on the monitor that shows it
    monkeypatch.setattr(pillpos, "window_rect", lambda h: (2070, 60, 2070 + 236, 60 + 44))
    monkeypatch.setattr(pillpos, "monitor_of_window", lambda h: pillpos.Monitor("\\\\.\\DISPLAY2", RIGHT))
    pill.u.fg = HWND                         # the user clicked the pill, so it had the focus
    pill._end_move(True)
    assert done.wait(2) and got == [True]
    assert pill.u.styles[-1] == START_EX     # click-through and never-focused again
    assert pill.u.focused == [OTHER]         # the window that was in front gets the focus back
    saved = pillpos.load(pill.pos_file)
    assert saved.device == "\\\\.\\DISPLAY2" and (saved.x, saved.y) == (150, 60)
    assert pill._mode == "flash"             # "Position saved", shown at the saved place
    assert pill.u.moves[-1][:2] == (2070, 60)
    assert not pill.moving


def test_esc_cancels_without_saving(pill):
    on_end, got, done = _ended(pill)
    pill._begin_move(on_end)
    pill._end_move(False)
    assert done.wait(2) and got == [False]
    assert pill.u.styles[-1] == START_EX and not pill.pos_file.exists()
    assert pill._mode == "hidden" and pill._root.alpha == 0.0
    assert pill.u.focused == []              # the pill never had the focus: nothing to give back
    pill._end_move(True)                     # a second Enter / double-click does nothing
    assert got == [False]


def test_shown_at_the_saved_place_else_the_usual_one(pill):
    pillpos.save(pill.pos_file, pillpos.to_relative("\\\\.\\DISPLAY2", 3000, 700, 236, 44, RIGHT))
    pill._show("working", text="x")
    assert pill.u.moves[-1][:2] == (3000, 700)
    pill._hide()
    pill.forget_position()                   # tray → Reset pill position
    pill._show("working", text="x")
    assert pill.u.moves[-1][:2] != (3000, 700)


def test_saved_monitor_gone_falls_back(pill, monkeypatch):
    pillpos.save(pill.pos_file, pillpos.to_relative("\\\\.\\DISPLAY9", 3000, 700, 236, 44, RIGHT))
    pill._show("working", text="x")
    x, y, flags = pill.u.moves[-1]
    assert (x, y) != (3000, 700) and flags & ov.SWP_SHOWWINDOW


# --- the app's state machine ------------------------------------------------------------------------

class FakePill:
    def __init__(self, auto_end=True):
        self.on_end = None
        self.ends = []
        self.foreground = False

    def start_move(self, on_end):
        self.on_end = on_end
        return True

    def end_move(self, save):
        self.ends.append(save)
        threading.Thread(target=self.on_end, args=(save,), name="pill-moved").start()
        return True

    def is_foreground(self):
        return self.foreground


def make_app(tmp_path):
    app = App.__new__(App)
    app.paths = types.SimpleNamespace(data_dir=tmp_path)
    app.state, app._lock, app.icon = "idle", threading.Lock(), None
    app.overlay = FakePill()
    app.cfg = types.SimpleNamespace(hotkey="ctrl+q")
    app.sounds = types.SimpleNamespace(play=lambda name: app.sounds_played.append(name))
    app.sounds_played, app.notes = [], []
    app._notify = app.notes.append
    app.failed = None
    app.recorder = types.SimpleNamespace(start=lambda: pytest.fail("a dictation must not start"))
    return app


def wait_idle(app):
    for _ in range(200):
        if app.state == "idle":
            return True
        threading.Event().wait(0.01)
    return False


def test_move_start_save_and_cancel(tmp_path):
    app = make_app(tmp_path)
    app.move_pill()
    assert app.state == "moving" and app.overlay.on_end is not None
    app.move_pill()                          # tray → "Done moving the pill" saves
    assert app.overlay.ends == [True] and wait_idle(app)
    app.move_pill()
    app.overlay.on_end(False)                # Esc on the pill
    assert app.state == "idle"


def test_hotkey_while_moving_ends_move_mode_and_does_not_dictate(tmp_path):
    app = make_app(tmp_path)
    app.move_pill()
    app._on_hotkey()                         # recorder.start would fail the test
    assert app.overlay.ends == [True] and wait_idle(app)
    assert "again to dictate" in app.notes[-1]


def test_no_dictation_while_the_pill_has_the_focus(tmp_path):
    app = make_app(tmp_path)
    app.overlay.foreground = True
    app._on_hotkey()
    assert app.sounds_played == ["error"] and app.state == "idle"


def test_move_only_from_idle_and_retry_blocked_while_moving(tmp_path):
    app = make_app(tmp_path)
    app.state = "recording"
    app.move_pill()
    assert app.state == "recording" and app.overlay.on_end is None and app.sounds_played == ["error"]
    app.state = "idle"
    app.move_pill()
    app.failed = object()
    assert not app._can_retry()
    app.retry_failed()                       # would set state "busy" if it ran
    assert app.state == "moving"
    app.overlay = None                       # [overlay] enabled = false: the item is greyed out
    app.state = "idle"
    app.move_pill()
    assert app.state == "idle"


def test_start_failure_goes_back_to_idle(tmp_path):
    app = make_app(tmp_path)
    app.overlay.start_move = lambda on_end: False
    app.move_pill()
    assert app.state == "idle" and app.notes
    app.overlay = FakePill()
    app.move_pill()
    app.overlay.end_move = lambda save: False     # the pill's window went away meanwhile
    app._on_hotkey()
    assert app.state == "idle"


def test_reset_deletes_the_file(tmp_path):
    app = make_app(tmp_path)
    forgot = []
    app.overlay.forget_position = lambda: forgot.append(1)
    f = tmp_path / "pill_position.json"
    f.write_text("{}", "utf-8")
    app.reset_pill_position()
    assert not f.exists() and forgot == [1]
    app.reset_pill_position()                # nothing saved: still fine
