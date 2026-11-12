"""Settings and app wiring for the indicator style (pill or round button) and its position presets."""
import threading
import types

import pytest

from parakeet_dictate import config
from parakeet_dictate.app import App


def test_style_settings():
    c = config.parse("")
    assert (c.overlay_style, c.overlay_always, c.overlay_position) == ("pill", True, "bottom")
    c = config.parse('[overlay]\nstyle = "button"\nbutton_visible = "dictating"\nposition = "bottom-right"')
    assert (c.overlay_style, c.overlay_always, c.overlay_position) == ("button", False, "bottom-right")
    for bad in ('style = "orb"', 'button_visible = "yes"', 'position = "middle"'):
        with pytest.raises(config.ConfigError):
            config.parse("[overlay]\n" + bad)


def _app(tmp_path, **cfg):
    app = App.__new__(App)
    app.paths = config.Paths(tmp_path / "cfg", tmp_path / "data")
    app.cfg = config.Config(**{**config.parse("").__dict__, **cfg})
    app._lock, app.state, app.calls = threading.Lock(), "idle", []
    app._on_hotkey = lambda: app.calls.append("start")
    app._finish_locked = lambda: app.calls.append("stop")
    return app


def test_each_style_keeps_its_own_dragged_position(tmp_path):
    assert _app(tmp_path)._pill_file.name == "pill_position.json"
    assert _app(tmp_path, overlay_style="button")._pill_file.name == "button_position.json"


def test_button_click_starts_and_stops(tmp_path):
    app = _app(tmp_path, overlay_style="button")
    app.click_from_button()                 # idle -> start, like the hotkey
    app.state = "recording"
    app.click_from_button()                 # recording -> stop
    app.state = "busy"
    app.click_from_button()                 # transcribing -> nothing
    assert app.calls == ["start", "stop"]


def test_a_new_indicator_only_when_its_shape_changes(tmp_path):
    app = _app(tmp_path)
    made = []
    app._new_overlay = lambda c: made.append(c) or types.SimpleNamespace(
        shape=App._overlay_shape(c), position=None, margin=None, close=lambda: None)
    app.overlay = app._new_overlay(app.cfg)
    same = config.Config(**{**app.cfg.__dict__, "overlay_margin": 64})
    assert App._overlay_shape(same) == app.overlay.shape                 # margin alone: no new window
    button = config.Config(**{**app.cfg.__dict__, "overlay_style": "button"})
    assert App._overlay_shape(button) != app.overlay.shape               # style: a new window
