"""A dictation from recording to delivery, with a fake microphone and a fake engine (fast, no model).
Keystrokes and the clipboard are captured, never sent: nothing is typed into any window."""
import threading
import time
import types

import numpy as np
import pytest

from parakeet_dictate import config, output, winutil
from parakeet_dictate.app import DOUBLED_NOTE, FAILED_NOTE, App, Session
from parakeet_dictate.engine import EngineError
from parakeet_dictate.textclean import Cleaner

SR = 16000
ENTER = "<Enter>"


class FakeRecorder:
    """Hands out the whole recording on the first drain."""

    def __init__(self, audio: np.ndarray):
        self.audio, self.rate = audio, SR

    def drain(self) -> np.ndarray:
        x, self.audio = self.audio, np.zeros(0, np.float32)
        return x

    def cancel(self) -> None:
        pass


class FakeEngine:
    """Returns the given texts in order; an Exception in the list is raised instead."""

    vad_frame = 0

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, 0.01

    def vad(self, *a, **k):
        return None

    def done(self) -> None:
        pass


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def phrase() -> np.ndarray:
    """3.5 s of 'syllables': tone bursts with short gaps, so the loudness detector and the clean-up both find speech."""
    return np.concatenate([np.concatenate([tone(0.25), np.zeros(int(0.1 * SR), np.float32)])] * 10)


def speech() -> np.ndarray:
    """Two phrases with a pause between: the loudness detector cuts it into two parts."""
    return np.concatenate([phrase(), np.zeros(int(1.5 * SR), np.float32), phrase()])


@pytest.fixture
def out(monkeypatch):
    """Everything 'typed' (with Enter as <Enter>) and everything put on the clipboard."""
    o = types.SimpleNamespace(typed=[], clip=[], target=True, block_from=None)

    def type_text(text):
        if o.block_from is not None and len(o.typed) >= o.block_from:
            raise OSError("SendInput blocked (error 5); is the target window elevated?")
        o.typed.append(text)

    def no_send(events):
        raise AssertionError("real SendInput reached")

    monkeypatch.setattr(output, "_send", no_send)
    monkeypatch.setattr(output, "type_text", type_text)
    monkeypatch.setattr(output, "press_enter", lambda: o.typed.append(ENTER))
    monkeypatch.setattr(output, "_set_clipboard_text", o.clip.append)
    monkeypatch.setattr(output, "_get_clipboard_text", lambda: None)
    monkeypatch.setattr(output, "modifiers_down", lambda: False)
    monkeypatch.setattr(output, "has_typing_target", lambda: o.target)
    monkeypatch.setattr(winutil, "trim_working_set", lambda: None)
    return o


def make_app(engine, audio=None, live=False, **cfg_over):
    cfg = config.parse("")
    cfg = config.Config(**{**cfg.__dict__, "process_while_speaking": live, "type_while_speaking": live,
                           **cfg_over})
    app = App.__new__(App)          # no tray, hotkey, microphone or model process
    app.cfg, app.cleaner = cfg, Cleaner.from_config(cfg)
    app.engine, app.recorder = engine, FakeRecorder(speech() if audio is None else audio)
    app.overlay = app.icon = None
    app.sounds = types.SimpleNamespace(play=lambda name: None)
    app.hotkeys = types.SimpleNamespace(escape_off=lambda: None)
    app.state, app._session, app._lock, app.current = "idle", 0, threading.Lock(), None
    app.last_text = app.last_stats = ""
    app.failed = None
    app.notes = []
    app._notify = app.notes.append
    return app


def dictate(app, audio=None) -> Session:
    if audio is not None:
        app.recorder = FakeRecorder(audio)
    app._session += 1
    app.state = "recording"
    s = Session(app, app._session, use_vad=False)
    app.state = "busy"
    app._complete(s)
    return s


def retry(app) -> None:
    app.retry_failed()
    end = time.monotonic() + 5
    while app.state != "idle" and time.monotonic() < end:
        time.sleep(0.01)
    assert app.state == "idle"


# --- 10. press Enter -------------------------------------------------------------------------------

def test_enter_once_after_whole_dictation(out):
    dictate(make_app(FakeEngine("Hello world."), press_enter=True))
    assert out.typed == ["Hello world. ", ENTER]


def test_enter_once_after_live_typing(out):
    app = make_app(FakeEngine("First part.", "Second part."), live=True, press_enter=True)
    dictate(app)
    assert out.typed.count(ENTER) == 1 and out.typed[-1] == ENTER
    assert "".join(out.typed[:-1]) == "First part. Second part. "
    assert app.last_text == "First part. Second part."


def test_no_enter_when_off(out):
    dictate(make_app(FakeEngine("Hello world.")))
    assert out.typed == ["Hello world. "]


@pytest.mark.parametrize("live", [False, True])
def test_no_enter_when_nothing_typed(out, live):
    dictate(make_app(FakeEngine("um", "uh"), live=live, press_enter=True))          # fillers only
    dictate(make_app(FakeEngine("", ""), live=live, press_enter=True))              # no words
    dictate(make_app(FakeEngine(), np.zeros(SR * 2, np.float32), live=live, press_enter=True))  # silence
    assert out.typed == []


def test_no_enter_when_cancelled(out):
    app = make_app(FakeEngine("Kept.", "Dropped."), live=True, press_enter=True)
    app._session, app.state = 1, "recording"
    app.current = s = Session(app, 1, use_vad=False)
    app._on_cancel()                   # Esc: the app never completes a cancelled session
    s._worker.join(2)
    assert ENTER not in out.typed


def test_no_enter_when_text_went_to_clipboard(out):
    out.target = False
    dictate(make_app(FakeEngine("Hello world."), press_enter=True))
    assert out.typed == [] and out.clip == ["Hello world."]


# --- 11. keep the recording when recognition fails ---------------------------------------------------

def test_failed_recording_is_kept_in_memory(out):
    audio = speech()
    app = make_app(FakeEngine(EngineError("speech engine stopped")))
    dictate(app, audio)
    assert app.notes == [FAILED_NOTE] and out.typed == [] and out.clip == []
    assert app.failed is not None and np.array_equal(app.failed.audio, audio) and app.failed.rate == SR
    assert not app.failed.partly_typed and app._can_retry()


def test_any_error_keeps_the_recording(out):
    app = make_app(FakeEngine(RuntimeError("boom")))
    dictate(app)
    assert app.failed is not None and app.notes == [FAILED_NOTE]


def test_retry_success_types_it_and_clears_it(out):
    app = make_app(FakeEngine(EngineError("x"), "Hello again."), press_enter=True)
    dictate(app)
    retry(app)
    assert out.typed == ["Hello again. ", ENTER]
    assert app.failed is None and app.last_text == "Hello again." and not app._can_retry()


def test_retry_failure_keeps_it_and_says_so_again(out):
    app = make_app(FakeEngine(EngineError("x"), EngineError("y")))
    dictate(app)
    kept = app.failed
    retry(app)
    assert app.failed is not None and np.array_equal(app.failed.audio, kept.audio)
    assert app.notes == [FAILED_NOTE, FAILED_NOTE] and out.typed == []


def test_discard_clears_it(out):
    app = make_app(FakeEngine(EngineError("x")))
    dictate(app)
    app.discard_failed()
    assert app.failed is None and not app._can_retry()


def test_retry_does_not_run_during_a_dictation(out):
    engine = FakeEngine(EngineError("x"))
    app = make_app(engine)
    dictate(app)
    for state in ("recording", "busy"):
        app.state = state
        assert not app._can_retry()
        app.retry_failed()
        app.discard_failed()
        assert app.state == state and app.failed is not None
    assert engine.calls == 1


def test_success_keeps_and_second_failure_replaces_the_kept_recording(out):
    a, b = speech(), speech() * 0.5
    app = make_app(FakeEngine(EngineError("x"), "Fine.", EngineError("y")))
    dictate(app, a)
    first = app.failed
    dictate(app, speech())
    assert app.failed is first and out.typed == ["Fine. "]
    dictate(app, b)
    assert app.failed is not first and np.array_equal(app.failed.audio, b)


def test_retry_after_partly_typed_live_dictation_types_all_and_warns(out):
    app = make_app(FakeEngine("First part.", EngineError("x"), "First part. Second part."),
                   live=True, press_enter=True)
    dictate(app)
    assert out.typed == ["First part. "] and app.failed.partly_typed
    retry(app)
    assert out.typed == ["First part. ", "First part. Second part. "]   # no Enter after doubled words
    assert app.notes == [FAILED_NOTE, DOUBLED_NOTE] and app.failed is None


# --- 12a. clipboard fallback -------------------------------------------------------------------------

def test_blocked_typing_goes_to_clipboard(out):
    out.block_from = 0
    app = make_app(FakeEngine("Hello world."))
    dictate(app)
    assert out.typed == [] and out.clip == ["Hello world."]
    assert app.notes == [output.BLOCKED_NOTE] and app.last_text == "Hello world."


def test_no_window_goes_to_clipboard(out):
    out.target = False
    app = make_app(FakeEngine("Hello world."))
    dictate(app)
    assert out.clip == ["Hello world."] and app.notes == [output.NO_TARGET_NOTE]


def test_live_typing_failure_keeps_typed_and_copies_the_rest(out):
    out.block_from = 1                  # the first phrase gets through, then the window blocks typing
    app = make_app(FakeEngine("First part.", "Second part."), live=True, press_enter=True)
    dictate(app)
    assert out.typed == ["First part. "] and out.clip == ["Second part."]
    assert app.notes == [output.BLOCKED_NOTE] and app.failed is None
    assert app.last_text == "First part. Second part."


def test_live_with_no_window_copies_everything(out):
    out.target = False
    app = make_app(FakeEngine("First part.", "Second part."), live=True)
    dictate(app)
    assert out.typed == [] and out.clip == ["First part. Second part."]
    assert app.notes == [output.NO_TARGET_NOTE]
