"""The Settings window: saving into config.toml, the shortcut recorder, the lists it offers, and a smoke
test that builds every page on a hidden Tk root (never shown, no main loop)."""
import types
from pathlib import Path

import pytest

from parakeet_dictate import config, hotkey
from parakeet_dictate import settings_store as ss
from test_models import fake_model


@pytest.fixture
def paths(tmp_path):
    p = config.Paths(tmp_path / "cfg", tmp_path / "data")
    p.config_dir.mkdir()
    p.data_dir.mkdir()
    return p


def default_copy(paths):
    paths.config_file.write_text(config.DEFAULT_FILE.read_text("utf-8"), "utf-8")
    return paths.config_file.read_text("utf-8")


# --- saving ----------------------------------------------------------------------------------------

def test_unchanged_save_keeps_the_file_byte_for_byte(paths):
    default_copy(paths)
    before = paths.config_file.read_bytes()
    s = ss.Store(paths)
    assert s.render({}) == s.user_text
    s.save({})
    assert paths.config_file.read_bytes() == before


def test_changing_one_value_keeps_every_comment_and_line(paths):
    original = default_copy(paths).splitlines()
    s = ss.Store(paths)
    s.save({("hotkey", "hold_threshold"): 0.7, ("context", "folders"): ["D:\\Notes", "%USERPROFILE%\\vault"]})
    after = paths.config_file.read_text("utf-8").splitlines()
    assert len(after) == len(original)
    diff = [(a, b) for a, b in zip(original, after) if a != b]
    assert diff == [("hold_threshold = 0.4", "hold_threshold = 0.7"),
                    ('folders = ["D:\\\\Obsidian\\\\Knowledgebase"]', "folders = ['D:\\Notes', '%USERPROFILE%\\vault']")]
    c = config.load(paths)
    assert c.hold_threshold == 0.7
    assert c.context_folders[0] == "D:\\Notes" and "%USERPROFILE%" not in c.context_folders[1]
    assert not list(paths.config_dir.glob("*.tmp"))


def test_missing_and_nested_sections_are_created(paths):
    paths.config_file.write_text('# mine\n[hotkey]\nkeys = "ctrl+alt+d"   # my key\n', "utf-8")
    s = ss.Store(paths)
    assert s.value(("hotkey", "keys")) == "ctrl+alt+d"
    assert s.value(("overlay", "position")) == "bottom"            # effective = defaults under the user's file
    s.save({("overlay", "margin"): 70,
            ("dictionary", "heard_as"): {"Orca": ["orka", "or ca"]},
            ("commands", "phrases"): {"next line": "newline", "full stop": "stop"},
            ("model", "folders"): ["D:\\models"]})
    text = paths.config_file.read_text("utf-8")
    assert text.startswith('# mine\n[hotkey]\nkeys = "ctrl+alt+d"   # my key\n')
    c = config.load(paths)
    assert c.overlay_margin == 70 and c.hotkey == "ctrl+alt+d"
    assert ("Orca", "orka") in c.dictionary and ("Mailzzy", "mail sey") not in c.dictionary   # table replaced
    assert c.commands == (("next line", "newline"), ("full stop", "stop"))
    assert c.model_folders == ("D:\\models",)


def test_existing_table_is_refilled_in_place(paths):
    default_copy(paths)
    s = ss.Store(paths)
    s.save({("dictionary", "heard_as"): {"Mailzzy": ["mail sey"], "Orca": ["orka"]}})
    text = paths.config_file.read_text("utf-8")
    assert text.count("[dictionary.heard_as]") == 1
    head = text.index("[dictionary.heard_as]")
    assert text.index('"Heard as": what the model writes') < head < text.index('Orca = ["orka"]') < text.index("[dictionary.apps]")
    assert config.load(paths).dictionary[-1] == ("Orca", "orka")


def test_invalid_result_is_not_written(paths):
    default_copy(paths)
    before = paths.config_file.read_bytes()
    s = ss.Store(paths)
    with pytest.raises(config.ConfigError, match="threads"):
        s.save({("model", "threads"): 99})
    with pytest.raises(config.ConfigError, match="hotkey"):
        s.save({("hotkey", "keys"): "ctrl+nosuchkey"})
    with pytest.raises(config.ConfigError, match="silence_stop_seconds"):
        s.save({("audio", "silence_stop_seconds"): 2})
    assert paths.config_file.read_bytes() == before
    assert not list(paths.config_dir.glob("*.tmp"))


def test_environment_variables_stay_as_written(paths, monkeypatch):
    monkeypatch.setenv("PD_TEST_DIR", "C:\\Expanded")
    paths.config_file.write_text('[model]\ndir = "%PD_TEST_DIR%\\\\model.gguf"\n'
                                 "[context]\nfolders = ['%PD_TEST_DIR%\\notes']\n", "utf-8")
    s = ss.Store(paths)
    assert s.value(("model", "dir")) == "%PD_TEST_DIR%\\model.gguf"          # shown unexpanded
    assert s.value(("context", "folders")) == ["%PD_TEST_DIR%\\notes"]
    s.save({("model", "threads"): 2})
    text = paths.config_file.read_text("utf-8")
    assert 'dir = "%PD_TEST_DIR%\\\\model.gguf"' in text and "['%PD_TEST_DIR%\\notes']" in text
    c = config.load(paths)
    assert c.model_dir == "C:\\Expanded\\model.gguf" and c.context_folders == ("C:\\Expanded\\notes",)


def test_file_changed_on_disk_meanwhile_keeps_both_edits(paths):
    default_copy(paths)
    s = ss.Store(paths)          # window opened
    paths.config_file.write_text(paths.config_file.read_text("utf-8").replace("threads = 4", "threads = 6"), "utf-8")
    s.save({("output", "sounds"): False})
    c = config.load(paths)
    assert c.threads == 6 and c.sounds is False


def test_windows_line_endings_are_kept(paths):
    paths.config_file.write_bytes(b"# mine\r\n[output]\r\nsounds = true\r\n")
    ss.Store(paths).save({("output", "sounds"): False, ("overlay", "enabled"): False})
    data = paths.config_file.read_bytes()
    assert b"sounds = false\r\n" in data and data.count(b"\n") == data.count(b"\r\n")


def test_broken_file_still_opens_and_reports(paths):
    paths.config_file.write_text("[model\n", "utf-8")
    s = ss.Store(paths)
    assert s.config_error and s.value(("model", "threads")) == 4
    with pytest.raises(Exception):
        s.save({("model", "threads"): 2})
    assert paths.config_file.read_text("utf-8") == "[model\n"


def test_folders_setting_defaults_to_empty_and_expands():
    assert config.parse("").model_folders == ()
    assert "%APPDATA%" not in config.parse('[model]\nfolders = ["%APPDATA%\\\\m"]').model_folders[0]
    with pytest.raises(config.ConfigError):
        config.parse("[model]\nfolders = [1]")


# --- shortcut recorder -----------------------------------------------------------------------------

def test_every_recordable_key_converts_to_a_spec_hotkey_parse_accepts():
    keys = [vk for vk in range(256) if hotkey.vk_name(vk) and vk not in hotkey.MODIFIER_VKS]
    assert len(keys) > 80
    for vk in keys:
        spec = hotkey.spec_for({"alt", "ctrl"}, vk)
        assert spec.startswith("ctrl+alt+")
        assert hotkey.parse(spec) == (0x3, vk), spec


@pytest.mark.parametrize("mods,vk,spec,shown", [
    ({"ctrl", "alt"}, 0x20, "ctrl+alt+space", "Ctrl+Alt+Space"),
    ({"win", "shift"}, 0x44, "shift+win+d", "Shift+Win+D"),
    ({"ctrl"}, 0x51, "ctrl+q", "Ctrl+Q"),
    (set(), 0x74, "f5", "F5"),
    ({"ctrl", "shift"}, 0x22, "ctrl+shift+pagedown", "Ctrl+Shift+PageDown"),
    ({"alt"}, 0x63, "alt+num3", "Alt+Num 3"),
    ({"ctrl"}, 0xC0, "ctrl+`", "Ctrl+`"),
])
def test_spec_and_display(mods, vk, spec, shown):
    assert hotkey.spec_for(mods, vk) == spec
    assert hotkey.display(spec) == shown


def test_display_of_hand_written_specs():
    assert hotkey.display("Control + Escape") == "Ctrl+Esc"
    assert hotkey.display("ctrl+backtick") == "Ctrl+`"


ALL_FREE = lambda spec: True          # noqa: E731
ALL_TAKEN = lambda spec: False        # noqa: E731


def test_recorder_listens_cancels_and_clears():
    assert ss.record_key(0x11, {"ctrl"}, "ctrl+q", ALL_FREE).action == "listen"
    assert ss.record_key(0x5B, {"win"}, "ctrl+q", ALL_FREE).action == "listen"
    assert ss.record_key(0x1B, set(), "ctrl+q", ALL_FREE).action == "cancel"
    r = ss.record_key(0x08, set(), "ctrl+alt+d", ALL_TAKEN)
    assert (r.action, r.spec) == ("set", "ctrl+q")
    r = ss.record_key(0x20, {"ctrl", "alt"}, "ctrl+q", ALL_FREE)
    assert (r.action, r.spec, r.message) == ("set", "ctrl+alt+space", "")


@pytest.mark.parametrize("mods,vk,why", [
    (set(), 0x51, "alone"),                 # bare Q
    (set(), 0x20, "alone"),                 # bare Space
    (set(), 0x7B, "debuggers"),             # F12
    ({"shift"}, 0x41, "types a character"),
    ({"alt"}, 0x09, "switches windows"),
    ({"alt"}, 0x73, "closes windows"),
    ({"win"}, 0x4C, "locks"),
    ({"ctrl"}, 0x43, "copies"),
    ({"ctrl", "shift"}, 0x1B, "Task Manager"),
    ({"ctrl"}, 0xFF, "can't be used"),
])
def test_recorder_refuses(mods, vk, why):
    r = ss.record_key(vk, mods, "ctrl+q", ALL_FREE)
    assert r.action == "error" and why in r.message, r


def test_function_keys_alone_are_allowed():
    assert ss.record_key(0x77, set(), "ctrl+q", ALL_FREE).spec == "f8"
    assert ss.record_key(0x13, set(), "ctrl+q", ALL_FREE).spec == "pause"


def test_in_use_check_skips_the_current_shortcut():
    r = ss.record_key(0x44, {"ctrl", "alt"}, "ctrl+q", ALL_TAKEN)
    assert r.action == "error" and "in use by another app" in r.message
    asked = []
    r = ss.record_key(0x51, {"ctrl"}, "Ctrl + Q", lambda s: asked.append(s) or False)
    assert (r.action, r.spec, asked) == ("set", "ctrl+q", [])      # the tray app holds it: ours


def test_availability_probe_registers_and_releases():
    # An unusual combination nobody holds: registering it twice in a row works only if it was released.
    assert hotkey.available("ctrl+alt+shift+f23")
    assert hotkey.available("ctrl+alt+shift+f23")


# --- choices ---------------------------------------------------------------------------------------

def _m(path, label):
    return types.SimpleNamespace(path=Path(path), label=lambda: label)


def test_model_choices_merge_found_and_folders():
    found = [_m("C:\\a\\v2.gguf", "Parakeet v2"), _m("C:\\b\\v3.gguf", "Parakeet v3")]
    extra = lambda folders: [_m("C:\\a\\v2.gguf", "dupe"), _m("D:\\m\\w.gguf", "Whisper")]   # noqa: E731
    ch = ss.model_choices(found, ["D:\\m"], "auto", inspect_folders=extra)
    assert ch == [("auto", ss.AUTO_MODEL_LABEL), ("C:\\a\\v2.gguf", "Parakeet v2"),
                  ("C:\\b\\v3.gguf", "Parakeet v3"), ("D:\\m\\w.gguf", "Whisper")]


def test_model_choices_keep_the_current_setting_as_written(monkeypatch):
    monkeypatch.setenv("PD_M", "C:\\b")
    found = [_m("C:\\b\\v3.gguf", "Parakeet v3")]
    ch = ss.model_choices(found, [], "%PD_M%\\v3.gguf", inspect_folders=lambda f: [])
    assert ch[1] == ("%PD_M%\\v3.gguf", "Parakeet v3")                  # matched, raw value kept
    ch = ss.model_choices(found, [], "E:\\gone", inspect_folders=lambda f: [])
    assert ch[-1] == ("E:\\gone", "E:\\gone (from your settings)")


def test_folder_models_reads_files_and_folders(tmp_path):
    fake_model(tmp_path / "one")
    single = fake_model(tmp_path / "two", name="Other", file="other.gguf")
    (tmp_path / "one" / "notes.txt").write_text("x")
    found = ss.folder_models([str(tmp_path / "one"), str(single), str(tmp_path / "missing"), ""])
    assert [m.path for m in found] == [tmp_path / "one" / "parakeet-tdt-0.6b-v2-Q8_0.gguf", single]
    assert all(m.source == "your folders" for m in found)


def test_input_devices_lists_each_microphone_once():
    devs = [{"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0},
            {"name": "Headset (Jabra)", "max_input_channels": 1, "hostapi": 0},
            {"name": "Speakers", "max_input_channels": 0, "hostapi": 0},
            {"name": "Headset (Jabra)", "max_input_channels": 1, "hostapi": 0},
            {"name": "Headset (Jabra Evolve2 65)", "max_input_channels": 1, "hostapi": 2}]
    assert ss.input_devices(devs, hostapi=0) == ["Headset (Jabra)"]
    assert ss.input_devices(devs) == ["Headset (Jabra)", "Headset (Jabra Evolve2 65)"]


def test_model_in_use(paths):
    assert ss.model_in_use(paths.data_dir) == ""
    (paths.data_dir / "model_path.txt").write_text("C:\\m\\v2.gguf\n", "utf-8")
    assert ss.model_in_use(paths.data_dir) == "C:\\m\\v2.gguf"


# --- launching and the tray side ---------------------------------------------------------------------

def test_command_from_source_runs_the_module():
    from parakeet_dictate import settings_ui
    argv, cwd = settings_ui.command()
    assert argv[1:] == ["-m", "parakeet_dictate", "--settings"] and cwd


def test_tray_opens_one_window_and_reloads_when_it_closes(paths, monkeypatch):
    import threading

    from parakeet_dictate import settings_ui
    from parakeet_dictate.app import App
    default_copy(paths)
    gate = threading.Event()
    done = threading.Event()

    class Proc:
        def poll(self):
            return None if not gate.is_set() else 0

        def wait(self):
            gate.wait(5)
            paths.config_file.write_text(paths.config_file.read_text("utf-8") + "\n", "utf-8")
            import os
            st = paths.config_file.stat()
            os.utime(paths.config_file, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            return 0

    launched, notes, reloads = [], [], []
    monkeypatch.setattr(settings_ui, "launch", lambda: launched.append(1) or Proc())
    fake = types.SimpleNamespace(paths=paths, _settings_proc=None, _notify=notes.append,
                                 reload_if_changed=lambda force=False: reloads.append(force),
                                 _refresh=lambda: done.set(), _settings_actions=lambda: [],
                                 _run_settings_action=lambda a: None)
    App.open_settings_window(fake)
    App.open_settings_window(fake)          # still open: no second window
    assert len(launched) == 1
    gate.set()
    assert done.wait(5)
    assert reloads == [True] and notes == ["Settings saved."]


# --- the window, built on a hidden root (never shown) -----------------------------------------------

@pytest.fixture(scope="module")
def hidden_root():
    """One withdrawn Tk root for all window tests (creating many Tk interpreters in one process is flaky)."""
    tk = pytest.importorskip("tkinter")
    from parakeet_dictate import settings_ui
    try:
        root = settings_ui.make_root("dark")
    except tk.TclError as e:
        pytest.skip(f"no Tk: {e}")
    yield root
    root.destroy()


@pytest.fixture
def window(paths, hidden_root):
    from parakeet_dictate import settings_ui
    root = hidden_root
    paths.config_file.write_text(config.DEFAULT_FILE.read_text("utf-8").replace("threads = 4", "threads = 3")
                                 .replace('input_device = ""', 'input_device = "Jabra"'), "utf-8")
    startup = []
    env = settings_ui.Env(found_models=[_m("C:\\a\\v2.gguf", "Parakeet v2")], mics=["Headset (Jabra)"],
                          startup_on=True, set_startup=startup.append, available=ALL_FREE,
                          model_in_use="C:\\a\\v2.gguf", folder_models=lambda f: [])
    opened = []
    closed = []
    win = settings_ui.SettingsWindow(root, ss.Store(paths), env, open_file=opened.append,
                                     close=lambda: closed.append(1))
    win.startup_calls, win.opened, win.closed = startup, opened, closed
    yield win
    win.outer.destroy()


def test_every_page_builds_and_shows_the_effective_values(window, paths):
    from parakeet_dictate import settings_ui
    assert window.root.state() == "withdrawn"
    assert list(window.pages) == [name for name, _ in settings_ui.PAGES]
    store = ss.Store(paths)
    keyed = [f for f in window.fields if f.spec.key]
    assert len(keyed) >= 40
    for f in keyed:
        assert f.control.get() == settings_ui.normalize(f.spec, store.value(f.spec.key)), f.spec.key
    assert window.field(("model", "threads")).control.get() == 3
    assert window.field(("audio", "input_device")).control.get() == "Jabra"
    assert window.field("startup").control.get() is True
    assert window.field(("dictionary", "heard_as")).control.get()["Mailzzy"][0] == "mail te"
    assert not window.dirty() and window.save_btn.instate(["disabled"])
    for name in window.pages:
        window.show(name)


def test_editing_enables_save_and_writes_only_the_changes(window, paths):
    before = paths.config_file.read_text("utf-8")
    window.field(("output", "sounds")).control.set(False)
    window.field(("model", "pause_seconds")).control.var.set("0.8")
    window.field(("audio", "input_device")).control.var.set("Windows default")
    window.field(("dictionary", "heard_as")).control.add_pair("Orca", "orka")
    window.field(("model", "folders")).control.add("D:\\models")
    window.field("startup").control.set(False)
    window.refresh_dirty()
    assert window.save_btn.instate(["!disabled"])
    changes, errors = window.changes()
    assert errors == [] and set(changes) == {("output", "sounds"), ("model", "pause_seconds"),
                                             ("audio", "input_device"), ("dictionary", "heard_as"),
                                             ("model", "folders")}
    assert window.save() is True
    assert window.saved and window.startup_calls == [False] and window.closed == [1]
    c = config.load(paths)
    assert (c.sounds, c.pause_seconds, c.input_device, c.model_folders) == (False, 0.8, "", ("D:\\models",))
    assert ("Orca", "orka") in c.dictionary and ("Mailzzy", "mail te") in c.dictionary
    after = paths.config_file.read_text("utf-8")
    assert after.count("\n#") == before.count("\n#")           # every comment line kept


def test_invalid_input_is_shown_and_nothing_saved(window, paths):
    before = paths.config_file.read_bytes()
    window.field(("model", "threads")).control.var.set("lots")
    assert window.save() is False and "whole number" in window.msg.cget("text")
    window.field(("model", "threads")).control.var.set("64")
    assert window.save() is False and "threads" in window.msg.cget("text")
    assert paths.config_file.read_bytes() == before and not window.saved and not window.closed


def test_shortcut_recorder_in_the_window(window):
    rec = window.field(("hotkey", "keys")).control
    assert rec.shown.cget("text") == "Ctrl+Q"
    rec.start()
    assert rec.listening
    rec.handle(0x11, {"ctrl"})
    assert rec.shown.cget("text") == "Ctrl+…"
    rec.handle(0x41, {"shift"})
    assert rec.listening and "types a character" in rec.status.cget("text")
    rec.handle(0x20, {"ctrl", "alt"})
    assert not rec.listening and rec.get() == "ctrl+alt+space" and rec.shown.cget("text") == "Ctrl+Alt+Space"
    assert window.changes()[0] == {("hotkey", "keys"): "ctrl+alt+space"}
    rec.start()
    rec.handle(0x1B, set())
    assert not rec.listening and rec.get() == "ctrl+alt+space"


def test_model_list_follows_the_custom_folders(window):
    from parakeet_dictate import settings_ui
    window.env.folder_models = lambda folders: [_m(f + "\\x.gguf", "From " + f) for f in folders]
    model = window.field(("model", "dir")).control
    window.field(("model", "folders")).control.add("D:\\more")
    assert ("D:\\more\\x.gguf", "From D:\\more") in model.choices
    assert model.get() == "auto"
    model.var.set("From D:\\more")
    assert model.get() == "D:\\more\\x.gguf"
    assert settings_ui.normalize(window.field(("model", "dir")).spec, "auto") == "auto"


def test_pages_are_grouped(window):
    from parakeet_dictate import settings_ui
    assert [name for name, _ in settings_ui.PAGES] == ["General", "Shortcuts", "Improvements", "Extras"]
    general = dict(settings_ui.PAGES)["General"]
    assert [g for g, _ in general] == ["Start-up", "Microphone", "Speech model", "Typing", "Pill"]


def test_optional_shortcut_recorder(window):
    rec = window.field(("hotkey", "copy_last")).control
    assert rec.shown.cget("text") == "None"
    rec.start()
    rec.handle(0x51, {"ctrl"})                       # Ctrl+Q is Dictate already
    assert rec.listening and "already the Dictate shortcut" in rec.status.cget("text")
    rec.handle(0x43, {"ctrl", "alt"})
    assert not rec.listening and rec.get() == "ctrl+alt+c" and rec.shown.cget("text") == "Ctrl+Alt+C"
    rec.start()
    rec.handle(0x08, set())                          # Backspace: no shortcut
    assert rec.get() == ""


def test_action_buttons_ask_the_tray(window, paths):
    import json
    window.do_action("free_memory")
    window.do_action("reset_pill")
    req = json.loads((paths.data_dir / "settings_request.json").read_text("utf-8"))
    assert req["actions"] == ["free_memory", "reset_pill"] and not window.closed
    window.do_action("move_pill")                    # nothing changed: just closes, tray moves the pill
    assert window.closed and "move_pill" in json.loads(
        (paths.data_dir / "settings_request.json").read_text("utf-8"))["actions"]
    window.do_action("open_log")
    assert "no log" in window.msg.cget("text").lower()


def test_open_settings_file_button(window, paths):
    window.open_settings_file()
    assert window.opened == [paths.config_file]
