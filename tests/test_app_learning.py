"""Saving each finished dictation for the background learner (recordings.py), with the fake microphone and
engine of test_app_flow. Nothing is typed into any window; recordings go to a temporary folder."""
import logging
import threading
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from test_app_flow import SR, FakeEngine, FakeRecorder, dictate, make_app, out, retry, speech  # noqa: F401

from parakeet_dictate import config, models, recordings, winutil
from parakeet_dictate.app import FAILED_NOTE, App, Session
from parakeet_dictate.engine import EngineError

MODEL = "parakeet-tdt-0.6b-v3.gguf"


def learning_app(tmp_path, engine, live=False, **cfg_over):
    """A test_app_flow app that keeps recordings (make_app keeps none) in tmp_path."""
    app = make_app(engine, live=live, **{"keep_recordings": True, "language": "de", **cfg_over})
    app.paths = config.Paths(tmp_path / "config", tmp_path / "data")
    app.target_exe = "notepad.exe"
    app._quitting = threading.Event()
    app.model = models.Model(Path("models") / MODEL, "Parakeet v3", "parakeet", ("de", "en"), "test", False)
    return app


def finish_saving() -> None:
    """Wait for the background save threads started so far."""
    for t in threading.enumerate():
        if t.name == "save-recording":
            t.join(5)


def prune_threads() -> list:
    return [t for t in threading.enumerate() if t.name == "prune-recordings"]


def wait_for(condition, seconds: float = 5) -> None:
    """Wait until `condition()` holds (the clear-out runs in a background thread)."""
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        time.sleep(0.005)
    assert condition()


@pytest.fixture
def pruning():
    """`pruning(app)` starts the app's clear-out loop; every loop is stopped again when the test ends."""
    apps = []

    def start(app):
        apps.append(app)
        app._start_prune()

    yield start
    for app in apps:
        app._quitting.set()
    for t in prune_threads():
        t.join(5)


@pytest.fixture
def saves(monkeypatch, out):
    """Each recordings.save call, with what had been typed and copied by then and the thread it ran in."""
    seen = []
    real = recordings.save

    def spy(dir, audio, rec):
        seen.append(types.SimpleNamespace(dir=dir, audio=audio, rec=rec, typed=list(out.typed),
                                          clip=list(out.clip), thread=threading.current_thread()))
        real(dir, audio, rec)

    monkeypatch.setattr(recordings, "save", spy)
    return seen


def logged(caplog, text: str) -> bool:
    """Was `text` logged (in a message or in an exception's traceback)?"""
    return any(text in r.getMessage() or text in str(r.exc_info) for r in caplog.records)


def old_recording(dir: Path, days: int) -> str:
    id = recordings.new_id(datetime.now() - timedelta(days=days))
    recordings.save(dir, np.zeros(1600, np.float32), recordings.Record(id, "", "", "", [], "", "", ""))
    return id


# --- what gets saved ---------------------------------------------------------------------------------

def test_finished_dictation_saved_after_typing(tmp_path, out, saves):
    app = learning_app(tmp_path, FakeEngine("Hello world."))
    started = []                                                    # what was typed when the save began
    start_save = app._save_recording
    app._save_recording = lambda *a: (started.append(list(out.typed)), start_save(*a))
    before = datetime.now()
    dictate(app)
    finish_saving()
    assert out.typed == ["Hello world. "] and started == [["Hello world. "]]
    [s] = saves
    assert s.typed == ["Hello world. "] and s.clip == []          # typing came first, then the save
    [rec] = recordings.list_records(app.paths.recordings_dir)
    assert (rec.raw, rec.phrases, rec.typed) == ("Hello world.", ["Hello world."], "Hello world.")
    assert (rec.exe, rec.model, rec.language) == ("notepad.exe", MODEL, "de")
    when = datetime.fromisoformat(rec.time)                         # local time, like the id
    assert before.replace(microsecond=0) <= when <= datetime.now()
    assert rec.id.startswith(when.strftime("%Y%m%d-%H%M%S-"))
    audio = recordings.load_audio(app.paths.recordings_dir, rec.id)
    assert audio is not None and audio.size == speech().size
    assert app.notes == []                                          # saving never notifies


def test_saved_audio_is_16k_even_from_a_faster_microphone(tmp_path, out, saves):
    app = learning_app(tmp_path, FakeEngine("Hello world."))
    app.recorder = FakeRecorder(np.repeat(speech(), 3))
    app.recorder.rate = 3 * SR
    dictate(app)
    finish_saving()
    [rec] = recordings.list_records(app.paths.recordings_dir)
    audio = recordings.load_audio(app.paths.recordings_dir, rec.id)
    assert audio is not None and abs(audio.size - speech().size) <= 1


def test_no_model_saves_an_empty_model_name(tmp_path, out, saves):
    app = learning_app(tmp_path, FakeEngine("Hello world."))
    app.model = None
    dictate(app)
    finish_saving()
    [s] = saves
    assert s.rec.model == ""


def test_text_on_the_clipboard_is_saved_too(tmp_path, out, saves):
    out.target = False
    app = learning_app(tmp_path, FakeEngine("Hello world."))
    dictate(app)
    finish_saving()
    [s] = saves
    assert s.typed == [] and s.clip == ["Hello world."] and s.rec.typed == "Hello world."


def test_live_dictation_is_saved_with_its_phrases(tmp_path, out, saves):
    app = learning_app(tmp_path, FakeEngine("First part.", "Second part."), live=True)
    dictate(app)
    finish_saving()
    [s] = saves
    assert s.typed == ["First part. ", "Second part. "]
    assert s.rec.phrases == ["First part.", "Second part."] and s.rec.raw == "First part. Second part."
    assert s.rec.typed == "First part. Second part."


def test_save_runs_on_its_own_daemon_thread(tmp_path, out, saves):
    app = learning_app(tmp_path, FakeEngine("Hello world."))
    dictate(app)
    finish_saving()
    [s] = saves
    assert s.thread is not threading.current_thread() and s.thread.daemon


# --- what is not saved -------------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["cancelled", "silence", "no words", "fillers only", "failed"])
def test_cancelled_empty_failed_not_saved(tmp_path, out, saves, case):
    engine = {"cancelled": FakeEngine("Dropped."), "silence": FakeEngine(), "no words": FakeEngine("", ""),
              "fillers only": FakeEngine("um", "uh"), "failed": FakeEngine(EngineError("x"))}[case]
    app = learning_app(tmp_path, engine)
    if case == "cancelled":
        app._session, app.state = 1, "recording"
        app.current = s = Session(app, 1, use_vad=False)
        app._on_cancel()                    # Esc: a cancelled dictation is never completed
        s._worker.join(2)
    else:
        dictate(app, np.zeros(SR * 2, np.float32) if case == "silence" else None)
    finish_saving()
    assert saves == [] and not app.paths.recordings_dir.exists()
    assert out.typed == [] and out.clip == []


def test_failed_retry_not_saved(tmp_path, out, saves):
    app = learning_app(tmp_path, FakeEngine(EngineError("x"), EngineError("y")))
    dictate(app)
    retry(app)
    finish_saving()
    assert saves == []


@pytest.mark.parametrize("setting", ["learning_enabled", "keep_recordings"])
def test_learning_or_keep_recordings_off_not_saved(tmp_path, out, saves, setting):
    app = learning_app(tmp_path, FakeEngine("Hello world."), **{setting: False})
    dictate(app)
    finish_saving()
    assert out.typed == ["Hello world. "] and saves == [] and not app.paths.recordings_dir.exists()


# --- trouble -----------------------------------------------------------------------------------------

def test_save_error_logged_not_raised(tmp_path, out, monkeypatch, caplog):
    def broken(dir, audio, rec):
        raise OSError("disk full")

    monkeypatch.setattr(recordings, "save", broken)
    app = learning_app(tmp_path, FakeEngine("Hello world."))
    with caplog.at_level(logging.WARNING, logger="parakeet_dictate.app"):
        session = dictate(app)
        finish_saving()
        assert out.typed == ["Hello world. "] and app.state == "idle" and app.notes == []   # no pop-up
        assert logged(caplog, "disk full")
        caplog.clear()
        app._save_recording(session, "Hello world.", "Hello world.")   # called straight, it still does not raise
        finish_saving()
        assert logged(caplog, "disk full") and app.notes == []


# --- Retry -------------------------------------------------------------------------------------------

def test_successful_retry_is_saved_after_typing(tmp_path, out, saves, monkeypatch):
    monkeypatch.setattr(winutil, "foreground_exe", lambda: "Outlook.exe")
    audio = speech()
    app = learning_app(tmp_path, FakeEngine(EngineError("x"), "Hello again."))
    dictate(app, audio)
    assert saves == []                       # the failed dictation itself is not saved
    retry(app)
    finish_saving()
    [s] = saves
    assert out.typed == ["Hello again. "] and s.typed == ["Hello again. "]
    assert (s.rec.raw, s.rec.phrases, s.rec.typed) == ("Hello again.", ["Hello again."], "Hello again.")
    assert (s.rec.exe, s.rec.model, s.rec.language) == ("Outlook.exe", MODEL, "de")
    assert s.audio.size == audio.size and app.notes == [FAILED_NOTE]   # only the first failure was announced


def test_successful_retry_not_saved_when_keeping_is_off(tmp_path, out, saves):
    app = learning_app(tmp_path, FakeEngine(EngineError("x"), "Hello again."), keep_recordings=False)
    dictate(app)
    retry(app)
    finish_saving()
    assert out.typed == ["Hello again. "] and saves == []


# --- clearing out old recordings ---------------------------------------------------------------------

def test_old_recordings_are_pruned_in_the_background_at_start(tmp_path, pruning):
    app = learning_app(tmp_path, FakeEngine())
    old, recent = old_recording(app.paths.recordings_dir, 30), old_recording(app.paths.recordings_dir, 1)
    pruning(app)
    wait_for(lambda: [r.id for r in recordings.list_records(app.paths.recordings_dir)] == [recent])
    assert old != recent and app.notes == []


@pytest.mark.parametrize("setting", ["learning_enabled", "keep_recordings"])
def test_old_recordings_are_pruned_even_when_keeping_is_off(tmp_path, pruning, setting):
    app = learning_app(tmp_path, FakeEngine(), **{setting: False})   # recordings made before it was turned off
    old, recent = old_recording(app.paths.recordings_dir, 30), old_recording(app.paths.recordings_dir, 1)
    pruning(app)
    wait_for(lambda: [r.id for r in recordings.list_records(app.paths.recordings_dir)] == [recent])
    assert old != recent


def test_prune_uses_the_settings_and_local_time(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(recordings, "prune", lambda *a: calls.append(a) or 0)
    app = learning_app(tmp_path, FakeEngine(), keep_days=3, max_recordings_mb=200)
    app._prune_recordings()
    [(dir, days, mb, now)] = calls
    assert (dir, days, mb) == (app.paths.recordings_dir, 3, 200)
    assert now.tzinfo is None and abs(now - datetime.now()) < timedelta(seconds=5)


def test_clear_out_runs_daily_by_default():
    assert App.PRUNE_EVERY_SECONDS == 24 * 3600


def test_clear_out_repeats_on_its_own_timer(tmp_path, monkeypatch, pruning):
    calls = []
    monkeypatch.setattr(recordings, "prune", lambda *a: calls.append(a) or 0)
    monkeypatch.setattr(App, "PRUNE_EVERY_SECONDS", 0.01)
    app = learning_app(tmp_path, FakeEngine())
    pruning(app)
    wait_for(lambda: len(calls) >= 3)                # no dictation, no save: it still comes round again


def test_saving_does_not_prune(tmp_path, out, monkeypatch):
    calls = []
    monkeypatch.setattr(recordings, "prune", lambda *a: calls.append(a) or 0)
    app = learning_app(tmp_path, FakeEngine("One.", "Two."))
    dictate(app)
    dictate(app, speech())
    finish_saving()
    assert len(recordings.list_records(app.paths.recordings_dir)) == 2 and calls == []


def test_quitting_stops_the_clear_out(tmp_path, monkeypatch, pruning):
    calls = []
    monkeypatch.setattr(recordings, "prune", lambda *a: calls.append(a) or 0)
    app = learning_app(tmp_path, FakeEngine())
    pruning(app)
    wait_for(lambda: len(calls) == 1)                # once at start, then it waits a day
    app._quitting.set()
    wait_for(lambda: not prune_threads())
    assert len(calls) == 1


def test_prune_error_logged_not_raised_and_the_clear_out_carries_on(tmp_path, monkeypatch, caplog, pruning):
    calls = []

    def broken(*a):
        calls.append(a)
        raise OSError("folder locked")

    monkeypatch.setattr(recordings, "prune", broken)
    monkeypatch.setattr(App, "PRUNE_EVERY_SECONDS", 0.01)
    app = learning_app(tmp_path, FakeEngine())
    with caplog.at_level(logging.WARNING, logger="parakeet_dictate.app"):
        pruning(app)
        wait_for(lambda: len(calls) >= 2)
    assert app.notes == [] and logged(caplog, "folder locked")
