"""Extra shortcuts (copy / correct last dictation), the short tray menu, and Settings -> tray actions."""
import json
import threading
import types

import pytest

from parakeet_dictate import config, hotkey, output
from parakeet_dictate import settings_store as ss
from parakeet_dictate.app import App

ALL_FREE = lambda spec: True   # noqa: E731


# --- settings ---------------------------------------------------------------------------------------

def test_extra_shortcut_settings():
    c = config.parse("")
    assert c.copy_last_hotkey == "" and c.correct_last_hotkey == ""
    c = config.parse('[hotkey]\ncopy_last = "ctrl+alt+c"\ncorrect_last = "ctrl+alt+r"')
    assert (c.copy_last_hotkey, c.correct_last_hotkey) == ("ctrl+alt+c", "ctrl+alt+r")
    with pytest.raises(config.ConfigError):
        config.parse('[hotkey]\ncopy_last = "ctrl+nonsense"')


def test_longest_dictation_default_is_six_minutes():
    assert config.parse("").max_seconds == 360


# --- the recorder -----------------------------------------------------------------------------------

def test_optional_shortcut_backspace_removes_it():
    r = ss.record_key(0x08, set(), "", ALL_FREE, default="")
    assert r.action == "set" and r.spec == "" and "No shortcut" in r.message


def test_shortcut_may_not_repeat_another_one():
    r = ss.record_key(0x51, {"ctrl"}, "", ALL_FREE, default="", others={"Dictate": "ctrl+q"})
    assert r.action == "error" and "already the Dictate shortcut" in r.message
    r = ss.record_key(0x43, {"ctrl", "alt"}, "", ALL_FREE, default="", others={"Dictate": "ctrl+q", "x": ""})
    assert r.action == "set" and r.spec == "ctrl+alt+c"


# --- registering the extras ---------------------------------------------------------------------------

def test_extras_register_and_report_clashes(monkeypatch):
    held = {}

    def register(hwnd, i, mods, vk):
        if (mods & ~hotkey.MOD_NOREPEAT, vk) == (hotkey.MOD["ctrl"] | hotkey.MOD["alt"], ord("B")):
            return 0                                   # another program holds Ctrl+Alt+B
        held[i] = (mods, vk)
        return 1

    monkeypatch.setattr(hotkey, "user32", types.SimpleNamespace(
        RegisterHotKey=register, UnregisterHotKey=lambda hwnd, i: held.pop(i, None)))
    pressed = []
    t = hotkey.HotkeyThread("ctrl+q", lambda: None, lambda: None, pressed.append)
    t._register_extras({"copy_last": "ctrl+alt+c", "correct_last": "ctrl+q"})
    assert sorted(t._extras.values()) == ["copy_last"]
    assert "already the dictation shortcut" in t.extra_errors["correct_last"]
    t._register_extras({"copy_last": "ctrl+alt+b", "correct_last": ""})
    assert t._extras == {} and "another program" in t.extra_errors["copy_last"]
    assert held == {}                                  # the earlier registration was released


# --- the app ------------------------------------------------------------------------------------------

def _app(tmp_path, **cfg):
    app = App.__new__(App)
    app.paths = config.Paths(tmp_path / "cfg", tmp_path / "data")
    app.paths.data_dir.mkdir(parents=True)
    app.cfg = config.Config(**{**config.parse("").__dict__, **cfg})
    app.last_text, app.failed, app.state, app.model, app.model_job = "", None, "idle", object(), ""
    app.overlay, app.icon, app.problem = None, None, None
    app.engine = types.SimpleNamespace(state="unloaded", unload=lambda: app.calls.append("unload"))
    app.notes, app.calls, app.quiet, app.sounds_played = [], [], [], []
    app._notify = app.notes.append
    app._note = app.quiet.append
    app.sounds = types.SimpleNamespace(play=app.sounds_played.append)
    app._status_text = lambda: "Ready"
    for name in ("move_pill", "search_model", "reset_pill_position", "download_model"):
        setattr(app, name, (lambda n: lambda: app.calls.append(n))(name))
    return app


def visible_items(menu):
    return [item.text for item in menu.items if item.visible and item.text and not item.text.startswith("- ")]


def test_tray_menu_is_short(tmp_path):
    app = _app(tmp_path)
    assert visible_items(app._menu()) == ["Ready", "Copy last dictation", "Correct last dictation…",
                                          "Settings…", "Quit Parakeet Dictate"]


def test_tray_menu_shows_what_applies_and_the_shortcuts(tmp_path):
    app = _app(tmp_path, copy_last_hotkey="ctrl+alt+c")
    app.failed, app.model, app.state = object(), None, "moving"
    items = visible_items(app._menu())
    assert "Copy last dictation\tCtrl+Alt+C" in items
    for text in ("Retry last recording", "Discard last recording", "Done moving the pill",
                 "Download Parakeet v2 model (about 730 MB)"):
        assert text in items


def test_copy_last_shortcut(tmp_path, monkeypatch):
    copied = []
    monkeypatch.setattr(output, "copy_to_clipboard", copied.append)
    app = _app(tmp_path)
    app.copy_last()
    # nothing to copy: the error sound, no Windows notification
    assert copied == [] and app.notes == [] and app.quiet == ["Nothing dictated yet."]
    assert app.sounds_played == ["error"]
    app.last_text = "Deploy core first."
    app.copy_last()
    assert copied == ["Deploy core first."]


def test_extra_shortcut_runs_on_its_own_thread(tmp_path, monkeypatch):
    done = threading.Event()
    monkeypatch.setattr(output, "copy_to_clipboard", lambda text: done.set())
    app = _app(tmp_path)
    app.last_text = "x"
    app._on_extra_hotkey("copy_last")
    assert done.wait(2)


def test_settings_window_requests_run_after_it_closes(tmp_path):
    app = _app(tmp_path)
    f = app._settings_request
    f.write_text(json.dumps({"actions": ["move_pill", "free_memory", "reset_pill", 7]}), "utf-8")
    actions = app._settings_actions()
    assert actions == ["move_pill", "free_memory", "reset_pill"] and not f.exists()
    for a in actions:
        app._run_settings_action(a)
    assert "unload" in app.calls and "reset_pill_position" in app.calls
    assert any("Turn the pill on" in n for n in app.notes)   # no pill: nothing to move
    f.write_text("{broken", "utf-8")
    assert app._settings_actions() == [] and not f.exists()
