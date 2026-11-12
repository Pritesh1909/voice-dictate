"""[model] folders: settings, discovery next to the automatic search, "Add a model file…" (config.toml
edited with its comments kept) and the Windows open-file dialog wrapper (the Win32 call faked)."""
import ctypes
import os
import threading
import types
from pathlib import Path

import pytest

from parakeet_dictate import cfgedit, config, models, winutil
from parakeet_dictate.app import App

from test_models import ORCA, env_for, fake_model, silero, write_gguf


def whisper(d: Path, name="Whisper Small", file="whisper-small-Q8_0.gguf") -> Path:
    return write_gguf(d / file, {"general.architecture": "whisper", "general.name": name,
                                 "general.languages": ["en", "de", "fr"], "stt.variant": "small"})


# --- the setting ------------------------------------------------------------------------------------

def test_folders_setting(monkeypatch):
    assert config.parse("").model_folders == ()
    monkeypatch.setenv("PD_MODELS", "D:\\mine")
    c = config.parse('[model]\nfolders = ["%PD_MODELS%\\\\stt", "  ", "D:\\\\Other", "d:\\\\other\\\\"]')
    assert c.model_folders == ("D:\\mine\\stt", "D:\\Other")      # expanded, blanks dropped, each once
    for bad in ('"D:\\\\models"', "[1]", "true"):
        with pytest.raises(config.ConfigError, match="folders"):
            config.parse(f"[model]\nfolders = {bad}")


def test_default_file_documents_the_setting():
    text = config.DEFAULT_FILE.read_text("utf-8")
    assert "folders = []" in text and "Add a model file" in text


# --- discovery --------------------------------------------------------------------------------------

def test_found_in_addition_to_the_automatic_search(tmp_path):
    home = tmp_path / "user"
    fake_model(home / ORCA / "parakeet-tdt-0.6b-v2-int8")
    w = whisper(tmp_path / "mine" / "speech" / "whisper")        # two folder levels deep
    single = fake_model(tmp_path / "loose", name="Parakeet Custom", file="custom.gguf")
    too_deep = whisper(tmp_path / "mine" / "a" / "b" / "c", name="Too Deep")
    write_gguf(tmp_path / "mine" / "silero-vad.gguf", silero())  # a voice detector isn't offered
    extra = [str(tmp_path / "mine"), str(single), str(tmp_path / "gone"), str(tmp_path / "gone.gguf")]
    found = models.find(include_wsl=False, env=env_for(home), extra=extra)
    by_path = {m.path: m for m in found}
    assert set(by_path) == {home / ORCA / "parakeet-tdt-0.6b-v2-int8" / "parakeet-tdt-0.6b-v2-Q8_0.gguf",
                            w, single}
    assert too_deep not in by_path
    assert by_path[w].source == "your folder (mine)" and by_path[single].source == "your folder (loose)"
    assert found == sorted(found, key=models._rank) and found[-1].path == w   # same ranking: English first


def test_identical_copy_in_your_folder_is_the_one_listed(tmp_path):
    home = tmp_path / "user"
    fake_model(home / ORCA / "parakeet-tdt-0.6b-v2-int8")
    mine = fake_model(tmp_path / "mine")
    found = models.find(include_wsl=False, env=env_for(home), extra=[str(tmp_path / "mine")])
    assert [m.path for m in found] == [mine] and found[0].source == "your folder (mine)"


def test_find_in_and_merge(tmp_path):
    w = whisper(tmp_path / "mine")
    assert [m.path for m in models.find_in([str(tmp_path / "mine"), str(tmp_path / "nothing")])] == [w]
    assert models.find_in([]) == []
    a = models.inspect(fake_model(tmp_path / "a"))
    assert [m.path for m in models.merge([a], models.find_in([str(tmp_path / "mine")]), [a])] == [a.path, w]


def test_wsl_folder_entries_are_copied_first():
    assert models._is_wsl_path(Path(r"\\wsl.localhost\Ubuntu\home\a\models"))
    assert models._is_wsl_path(Path(r"\\wsl$\Ubuntu\models")) and not models._is_wsl_path(Path(r"D:\models"))


def test_why_a_file_is_refused(tmp_path):
    assert "voice detector" in models.why_not_speech(write_gguf(tmp_path / "vad.gguf", silero()))
    llm = write_gguf(tmp_path / "llama.gguf", {"general.architecture": "llama", "general.name": "Llama"})
    assert "LLM" in models.why_not_speech(llm)
    (tmp_path / "x.onnx").write_bytes(b"x")
    assert ".gguf" in models.why_not_speech(tmp_path / "x.onnx")
    (tmp_path / "bad.gguf").write_bytes(b"nope")
    assert "damaged" in models.why_not_speech(tmp_path / "bad.gguf")
    assert "isn't there" in models.why_not_speech(tmp_path / "missing.gguf")


# --- the app picks them ------------------------------------------------------------------------------

def make_app(tmp_path, monkeypatch, cfg_text=""):
    """An App with real settings files in tmp_path but no tray, hotkey, microphone, overlay or model process;
    the automatic search sees only tmp_path."""
    home, real = tmp_path / "user", models.windows_places
    monkeypatch.setattr(models, "windows_places", lambda env=None: real(env_for(home)))
    paths = config.Paths(tmp_path / "cfg", tmp_path / "data")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(cfg_text or config.DEFAULT_FILE.read_text("utf-8"), "utf-8")
    app = App.__new__(App)
    app.paths, app.cfg = paths, config.load(paths)
    app.cfg_model_dir, app.cfg_model_folders = app.cfg.model_dir, app.cfg.model_folders
    app.model, app.found, app.problem, app.model_job, app.icon, app.overlay = None, [], None, "", None, None
    app.state, app._lock, app.learned, app.vad_path = "idle", threading.Lock(), [], None
    app.hotkeys = types.SimpleNamespace(change=lambda keys: None, set_extras=lambda wanted: {})
    app.context = types.SimpleNamespace(configure=lambda *a: None, everywhere=())
    app.recorder = types.SimpleNamespace(device_hint="")
    app.sounds = types.SimpleNamespace(enabled=True, play=lambda name: None)
    app.specs = []
    app.engine = types.SimpleNamespace(set_spec=lambda spec, secs: app.specs.append(spec))
    app._engine_spec = lambda c: app.model
    app.notes = []
    app._notify = app.notes.append
    app.quiet = []
    app._note = app.quiet.append
    app.search_model = lambda: app.notes.append("searching WSL")
    app._adding_model = False
    # never a real pill window: a settings reload would otherwise create one
    app._new_overlay = lambda c: types.SimpleNamespace(wide=c.stream_while_speaking, position=None, margin=None,
                                                       close=lambda: None)
    return app


def test_a_model_you_picked_stays_picked(tmp_path, monkeypatch):
    fake_model(tmp_path / "user" / ORCA / "parakeet-tdt-0.6b-v2-int8")
    w = whisper(tmp_path / "Mine")
    app = make_app(tmp_path, monkeypatch, f"[model]\nfolders = ['{tmp_path / 'Mine'}']\n")
    app._resolve_model(app.cfg)
    assert app.model.name == "Parakeet TDT 0.6B v2" and w in [m.path for m in app.found]
    app._use_model(next(m for m in app.found if m.path == w), apply=False)    # picked in the tray menu
    # model_path.txt, written in other capitals (Windows paths aren't case-sensitive)
    app._model_cache.write_text(str(w).upper(), "utf-8")
    app.model = None
    app._resolve_model(app.cfg)                                               # next start
    assert app.model.path == w and app.model.source == "your folder (Mine)"


def test_folder_gone_means_not_listed(tmp_path, monkeypatch):
    fake_model(tmp_path / "user" / ORCA / "parakeet-tdt-0.6b-v2-int8")
    w = whisper(tmp_path / "usb")
    app = make_app(tmp_path, monkeypatch, f"[model]\nfolders = ['{tmp_path / 'usb'}']\n")
    app._resolve_model(app.cfg)
    app._use_model(next(m for m in app.found if m.path == w), apply=False)
    w.unlink()
    (tmp_path / "usb").rmdir()                                                # the USB stick was pulled
    app._resolve_model(app.cfg)
    assert app.model.name == "Parakeet TDT 0.6B v2" and len(app.found) == 1 and not app.notes


def test_fixed_dir_plus_folders(tmp_path, monkeypatch):
    fixed = fake_model(tmp_path / "fixed")
    w = whisper(tmp_path / "mine")
    app = make_app(tmp_path, monkeypatch,
                   f"[model]\ndir = '{tmp_path / 'fixed'}'\nfolders = ['{tmp_path / 'mine'}']\n")
    app._resolve_model(app.cfg)
    assert app.model.path == fixed and [m.path for m in app.found] == [fixed, w]
    app._use_model(app.found[1], apply=False)                                 # picked in the tray menu
    app._resolve_model(app.cfg, keep=app.model)                               # only the folders changed
    assert app.model.path == w


# --- Add a model file… ------------------------------------------------------------------------------

USER_CFG = """# my settings
[hotkey]
keys = "ctrl+q"   # the usual one

[model]
# where my models are
dir = "auto"
language = "auto"

[output]
sounds = false
"""


def test_add_model_file_edits_config_and_switches(tmp_path, monkeypatch):
    fake_model(tmp_path / "user" / ORCA / "parakeet-tdt-0.6b-v2-int8")
    w = whisper(tmp_path / "dl")
    app = make_app(tmp_path, monkeypatch, USER_CFG)
    app._resolve_model(app.cfg)
    assert app.add_model(str(w))
    text = app.paths.config_file.read_text("utf-8")
    assert "# my settings" in text and "# the usual one" in text and "# where my models are" in text
    assert f"folders = ['{w}']" in text and text.index("folders") < text.index("[output]")
    assert config.parse(text).model_folders == (str(w),)
    assert app.model.path == w and app.cfg.model_folders == (str(w),)
    assert app.quiet[-1] == "Now using Whisper Small (3 languages)." and app.notes == []   # log only
    assert w in [m.path for m in app.found] and app._model_cache.read_text("utf-8") == str(w)
    # the same file again: not listed twice
    assert app.add_model(str(w)) and app.paths.config_file.read_text("utf-8").count(str(w)) == 1


@pytest.mark.parametrize("kind,words", [("vad", "voice detector"), ("llm", "LLM"), ("junk", "damaged")])
def test_add_model_refuses_other_files(tmp_path, monkeypatch, kind, words):
    p = tmp_path / f"{kind}.gguf"
    if kind == "vad":
        write_gguf(p, silero())
    elif kind == "llm":
        write_gguf(p, {"general.architecture": "llama", "general.name": "Llama"})
    else:
        p.write_bytes(b"GGUF\x01")
    app = make_app(tmp_path, monkeypatch, USER_CFG)
    assert not app.add_model(str(p))
    assert words in app.notes[-1] and app.paths.config_file.read_text("utf-8") == USER_CFG


def test_add_model_while_dictating_only_adds_it(tmp_path, monkeypatch):
    w = whisper(tmp_path / "dl")
    app = make_app(tmp_path, monkeypatch, USER_CFG)
    app.state = "recording"
    assert app.add_model(str(w)) and app.model is None
    assert "once this dictation is done" in app.notes[-1]
    assert str(w) in app.paths.config_file.read_text("utf-8")


def test_add_model_file_runs_the_dialog_off_the_tray_thread(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, USER_CFG)
    seen, done = [], threading.Event()

    def dialog(title, filters, initial_dir=None):
        seen.append((threading.current_thread().name, filters))
        return str(tmp_path / "picked.gguf")
    monkeypatch.setattr(winutil, "open_file_dialog", dialog)
    app.add_model = lambda p: (seen.append(p), done.set())
    app._refresh = lambda: None
    app.add_model_file()
    assert done.wait(5)
    assert seen[0][0] == "add-model" and ("Speech models (*.gguf)", "*.gguf") in seen[0][1]
    assert seen[1] == str(tmp_path / "picked.gguf") and not app._adding_model


# --- config.toml edits --------------------------------------------------------------------------------

def test_edit_appends_and_keeps_everything_else(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("# top\n[model]\n# the list\nfolders = ['D:\\a']  # mine\ndevice = \"cpu\"\n", "utf-8")
    assert cfgedit.add_model_folder(f, "E:\\b\\m.gguf")
    text = f.read_text("utf-8")
    assert text == "# top\n[model]\n# the list\nfolders = ['D:\\a', 'E:\\b\\m.gguf']  # mine\ndevice = \"cpu\"\n"
    assert not cfgedit.add_model_folder(f, "d:\\A\\")          # already there (capitals, trailing slash)


def test_edit_creates_the_model_table(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("[output]\nsounds = false\n", "utf-8")
    cfgedit.add_model_folder(f, "D:\\it's here")                  # a quote: a basic string instead
    c = config.parse(f.read_text("utf-8"))
    assert c.model_folders == ("D:\\it's here",) and not c.sounds


def test_edit_keeps_line_endings_and_bom(tmp_path):
    f = tmp_path / "config.toml"
    f.write_bytes(b"\xef\xbb\xbf# hi\r\n[model]\r\ndir = \"auto\"\r\n")
    cfgedit.add_model_folder(f, "D:\\m")
    raw = f.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf# hi\r\n") and b"\n" not in raw.replace(b"\r\n", b"")
    assert config.parse(raw.decode("utf-8-sig")).model_folders == ("D:\\m",)


@pytest.mark.parametrize("text", ["[model\n", "[model]\nfolders = 'D:\\x'\n", "[model]\nthreads = 99\n"])
def test_edit_never_writes_a_broken_file(tmp_path, text):
    f = tmp_path / "config.toml"
    f.write_text(text, "utf-8")
    with pytest.raises(config.ConfigError):
        cfgedit.add_model_folder(f, "D:\\m")
    assert f.read_text("utf-8") == text and not (tmp_path / "config.toml.tmp").exists()


# --- the dialog wrapper -------------------------------------------------------------------------------

def test_dialog_returns_the_picked_file():
    got = {}

    def fake(pofn):
        o = pofn.contents
        got["flags"], got["size"] = o.Flags, o.lStructSize
        got["filter"] = ctypes.wstring_at(o.lpstrFilter, 31)
        got["title"] = ctypes.wstring_at(o.lpstrTitle)
        got["dir"] = ctypes.wstring_at(o.lpstrInitialDir)
        path = ctypes.create_unicode_buffer("C:\\Users\\me\\Downloads\\whisper.gguf")
        assert o.nMaxFile >= 260
        ctypes.memmove(o.lpstrFile, path, ctypes.sizeof(path))
        return 0

    p = winutil.open_file_dialog("Add a model", [("Speech models (*.gguf)", "*.gguf")], "C:\\dl", call=fake)
    assert p == "C:\\Users\\me\\Downloads\\whisper.gguf"
    assert got["flags"] & winutil.OFN_FILEMUSTEXIST and got["flags"] & winutil.OFN_NOCHANGEDIR
    assert got["size"] == ctypes.sizeof(winutil.OPENFILENAMEW)
    assert got["filter"] == "Speech models (*.gguf)\0*.gguf\0\0"
    assert got["title"] == "Add a model" and got["dir"] == "C:\\dl"


def test_dialog_cancel_and_error():
    assert winutil.open_file_dialog("t", [("a", "*.gguf")], call=lambda p: -1) is None
    with pytest.raises(OSError, match="0x3003"):
        winutil.open_file_dialog("t", [("a", "*.gguf")], call=lambda p: 0x3003)


def test_openfilename_matches_the_windows_layout():
    assert ctypes.sizeof(winutil.OPENFILENAMEW) == (152 if ctypes.sizeof(ctypes.c_void_p) == 8 else 88)
    assert os.name == "nt"
