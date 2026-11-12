"""Spoken commands, per-program settings, context words and learned words in a whole dictation, with
the fake microphone and engine of test_app_flow. Keystrokes, Backspace and the clipboard are captured,
never sent."""
import threading
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

from parakeet_dictate import config, learn, output, winutil
from parakeet_dictate.app import App, Session
from test_app_flow import ENTER, FakeEngine, dictate, make_app, out, phrase, speech  # noqa: F401  (out: fixture)

SR = 16000
WINDOW = 4242


def three_phrases() -> np.ndarray:
    gap = np.zeros(int(1.5 * SR), np.float32)
    return np.concatenate([phrase(), gap, phrase(), gap, phrase()])


def four_phrases() -> np.ndarray:
    gap = np.zeros(int(1.5 * SR), np.float32)
    return np.concatenate([phrase(), gap, phrase(), gap, phrase(), gap, phrase()])


@pytest.fixture
def keys(out, monkeypatch):
    """Backspace presses recorded as "<BS n>" in out.typed; the dictation's window stays in front."""
    monkeypatch.setattr(output, "backspace", lambda n: out.typed.append(f"<BS {n}>"))
    front = NS(hwnd=WINDOW)
    monkeypatch.setattr(winutil, "foreground_window", lambda: front.hwnd)
    out.front = front
    return out


def live_app(*results, **over):
    app = make_app(FakeEngine(*results), live=True, **over)
    app.target_hwnd = WINDOW
    return app


# --- spoken commands while typing ---------------------------------------------------------------------

def test_new_line_is_typed_as_a_newline_without_a_space_before_it(keys):
    app = live_app("First part.", "New line.", "Second part.")
    dictate(app, three_phrases())
    assert keys.typed == ["First part. ", "<BS 1>", "\n", "Second part. "]
    assert app.last_text == "First part.\nSecond part."


def test_scratch_that_removes_exactly_the_last_phrase(keys):
    app = live_app("Keep this.", "Drop this one.", "Scratch that.", "Then this.")
    dictate(app, four_phrases())
    assert keys.typed == ["Keep this. ", "Drop this one. ", f"<BS {len('Drop this one. ')}>", "Then this. "]
    assert app.last_text == "Keep this. Then this."


def test_scratch_that_in_another_window_sends_no_backspace(keys):
    app = live_app("Keep this.", "Drop this one.", "Scratch that.", "Then this.")
    app.target_hwnd = WINDOW + 1                 # the user switched windows meanwhile
    dictate(app, four_phrases())
    assert not any(t.startswith("<BS") for t in keys.typed)
    assert keys.typed == ["Keep this. ", "Drop this one. ", "Then this. "]


def test_scratch_that_never_backspaces_when_pasting(keys, monkeypatch):
    pasted = []
    monkeypatch.setattr(output, "paste_text", pasted.append)
    app = live_app("Keep this.", "Drop this one.", "Scratch that.", "Then this.", output_method="paste")
    dictate(app, four_phrases())
    assert not any(t.startswith("<BS") for t in keys.typed) and keys.typed == []
    assert pasted == ["Keep this. ", "Drop this one. ", "Then this. "]


def test_commands_off_for_one_program_types_them(keys):
    app = live_app("First part.", "New line.", "Second part.",
                   apps=config.parse('[apps."windowsterminal.exe"]\ncommands = false').apps)
    app.target_exe = "WindowsTerminal.exe"
    dictate(app, three_phrases())
    assert keys.typed == ["First part. ", "New line. ", "Second part. "]


def test_whole_dictation_applies_commands(keys):
    app = make_app(FakeEngine("One.", "New paragraph.", "Two."), process_while_speaking=True,
                   type_while_speaking=False)
    dictate(app, three_phrases())
    assert keys.typed == ["One.\n\nTwo. "] and app.last_text == "One.\n\nTwo."


def test_whole_dictation_without_commands_is_unchanged(keys):
    app = make_app(FakeEngine("One.", "Two."), process_while_speaking=True, type_while_speaking=False)
    dictate(app, speech())
    assert keys.typed == ["One. Two. "]


# --- "stop listening" -------------------------------------------------------------------------------

def test_stop_listening_ends_the_recording_and_is_not_typed(keys):
    app = live_app("Hello there.", "Stop listening.")
    gap = np.zeros(int(1.5 * SR), np.float32)
    app.recorder.audio = np.concatenate([phrase(), gap, phrase(), gap])   # both phrases end while recording
    stopped = threading.Event()
    app._finish_locked = lambda: (setattr(app, "state", "busy"), stopped.set())
    app._session, app.state = 1, "recording"
    s = Session(app, 1, use_vad=False)
    assert stopped.wait(5)                       # the voice stop ran on its own thread
    s.finish()
    assert keys.typed == ["Hello there. "]


def test_stop_listening_does_nothing_once_the_recording_has_ended(keys):
    app = live_app()
    calls = []
    app._finish_locked = lambda: calls.append(1)
    app._session, app.state = 2, "busy"
    app.stop_from_voice(2)
    app.stop_from_voice(1)
    time.sleep(0.2)
    assert calls == []


# --- per-program output settings --------------------------------------------------------------------

def test_app_settings_apply_to_the_program_in_front(keys):
    apps = config.parse('[apps."slack.exe"]\nadd_trailing_space = false\npress_enter = true').apps
    app = make_app(FakeEngine("Hello world.", "Hello world."), apps=apps)
    app.target_exe = "Slack.exe"
    dictate(app)
    assert keys.typed == ["Hello world.", ENTER]
    app.target_exe = "notepad.exe"
    dictate(app, speech())
    assert keys.typed[2:] == ["Hello world. "]


# --- context words and learned words ----------------------------------------------------------------

def test_context_words_apply_to_one_dictation_only(keys):
    app = make_app(FakeEngine("Check the mcp auth filter.", "Check the mcp auth filter."))
    app.target_terms = [("McpAuthFilter", "")]
    dictate(app)
    app.target_terms = []
    dictate(app, speech())
    assert keys.typed == ["Check the McpAuthFilter. ", "Check the mcp auth filter. "]


def test_your_dictionary_beats_learned_beats_context(keys):
    app = make_app(FakeEngine("Open key cloak and mail sey and zan jo."))
    app.learned = [("KeyCloak", ""), ("Zanjo", "zan jo")]     # learned: below your own "Keycloak"
    app.target_terms = [("ZanJo", "")]                         # context: below the learned "Zanjo"
    dictate(app)
    assert keys.typed == ["Open Keycloak and Mailzzy and Zanjo. "]


def test_learned_file_is_picked_up_when_it_changes(tmp_path):
    paths = config.Paths(tmp_path / "r", tmp_path / "l")
    paths.ensure()
    app = NS(paths=paths, cfg=config.parse(""), learned=[], _learned_stamp=None, model=None)
    app._make_cleaner = lambda c: App._make_cleaner(app, c)
    app.cleaner = app._make_cleaner(app.cfg)
    App._check_learned(app)
    assert app.learned == [] and app.cleaner.clean("zan jo") == "zan jo"
    learn.add(paths.learned_file, [("Zanjo", "zan jo")])
    App._check_learned(app)
    assert app.cleaner.clean("zan jo") == "Zanjo"
    assert App.cleaner_for(app, "code.exe").clean("zan jo") == "Zanjo"


def test_vocabulary_changes_only_while_the_model_is_unloaded():
    specs = []
    engine = NS(state="ready", set_spec=lambda spec, after: specs.append(spec))
    app = NS(cfg=config.parse(""), engine=engine, _vocab=(), context=NS(everywhere=["ss-core-service"]))
    app._vocabulary = lambda c: App._vocabulary(app, c)
    app._engine_spec = lambda c: ("spec", app._vocab)
    App._sync_vocabulary(app)
    assert specs == [] and app._vocab == ()            # loaded: no reload for new words
    engine.state = "unloaded"
    App._sync_vocabulary(app)
    App._sync_vocabulary(app)                          # unchanged: no second spec change
    assert specs == [("spec", ("ss-core-service",))]


def test_typing_failure_keeps_commands_in_the_clipboard_text():
    from parakeet_dictate.commands import CommandMatcher
    sent = []

    def send(text, method):
        if sent:
            raise OSError("SendInput blocked")
        sent.append(text)

    t = output.PhraseTyper(lambda raw: raw, "type", True, send=send, mods=lambda: False,
                           matcher=CommandMatcher(config.parse("").commands))
    for p in ("One.", "Two.", "New line.", "Three.", "Scratch that.", "Four."):
        t.add(p)
        t.pump()
    assert sent == ["One. "] and t.text == "One."
    assert t.untyped.split() == ["Two.", "Four."] and t.untyped.count(chr(10)) == 1
