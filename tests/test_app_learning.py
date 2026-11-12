"""Background learning in the app, with the fake microphone and engine of test_app_flow: saving each finished
dictation (recordings.py), Whisper's download, the idle loop that runs the checker, the hotkey stopping it,
and automatic fixes in the clean-up. Nothing is typed into any window, no model is loaded and nothing is
downloaded; every file goes to a temporary folder."""
import dataclasses
import logging
import sys
import threading
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from test_app_flow import SR, FakeEngine, FakeRecorder, dictate, make_app, out, retry, speech  # noqa: F401

from parakeet_dictate import autolearn, config, idle, learn, models, recordings, teacher, winutil
from parakeet_dictate.app import FAILED_NOTE, App, FailedRecording, Session
from parakeet_dictate.engine import GPU_MARKER, Engine, EngineError, EngineSpec

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


def test_clear_out_runs_hourly_by_default():
    """Hourly, not daily: a laptop that sleeps at night (the timer doesn't run while it sleeps) would
    otherwise keep recordings well past keep_days. The clear-out is cheap and goes by age."""
    assert App.PRUNE_EVERY_SECONDS == 3600


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
    wait_for(lambda: len(calls) == 1)                # once at start, then it waits an hour
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


# --- background learning: Whisper, the idle loop, the hotkey -------------------------------------------

WHISPER = "whisper-large-v3-turbo-Q8_0.gguf"
LEARNING_THREADS = ("background-learning", "background-check", "whisper-setup")


@pytest.fixture
def learner(tmp_path, monkeypatch):
    """`learner(**cfg)`: a learning_app with the background-learning state of a fresh app start, on a PC idle
    for a long time and on mains power. teacher_ready=False: Whisper isn't there yet. Every status written to
    state.json is in app.statuses. Background threads are ended when the test ends."""
    monkeypatch.setattr(idle, "seconds_since_input", lambda: 1e6)
    monkeypatch.setattr(idle, "on_mains_power", lambda: True)
    apps = []

    def make(teacher_ready=True, **cfg_over):
        app = learning_app(tmp_path, FakeEngine(), **cfg_over)
        del app._check_learned                     # the real one, on learned.toml and learned_auto.toml in tmp_path
        app._learned_stamp = None
        app.model_job, app.problem = "", None
        app.context = types.SimpleNamespace(everywhere=[])
        app.reload_if_changed = lambda: None
        app._init_learning()
        app.statuses = []
        real = app.learning_store.set_status
        app.learning_store.set_status = lambda text: (app.statuses.append(text), real(text))[1]
        if teacher_ready:
            app.teacher_path, app._teacher_tried = tmp_path / WHISPER, True
        apps.append(app)
        return app

    yield make
    for app in apps:
        app._quitting.set()
        if app.checker is not None:
            app.checker.stop()
    for t in threading.enumerate():
        if t.name in LEARNING_THREADS:
            t.join(5)


class FakeTeacher:
    """Whisper's engine as the checker uses it. transcribe() returns `text`; with block=True it waits until
    kill() ends the process under it, then raises EngineError like a real engine does."""
    device_type = "gpu"

    def __init__(self, events: list, text: str = "", block: bool = False):
        self.events, self.text, self.block = events, text, block
        self.transcribing, self.killed = threading.Event(), threading.Event()

    def warm(self) -> None:
        pass

    def wait_ready(self, timeout: float = 60.0) -> bool:
        return True

    def transcribe(self, samples):
        self.transcribing.set()
        if not self.block:
            return self.text, 0.01
        self.killed.wait(5)
        raise EngineError("the engine process ended")

    def unload(self) -> None:
        self.events.append(("unload", threading.current_thread().name))

    def kill(self) -> None:
        self.events.append("kill")
        self.killed.set()


def kept(app, raw: str) -> str:
    """A kept recording of `raw`, as a finished dictation leaves it."""
    id = recordings.new_id(datetime.now())
    recordings.save(app.paths.recordings_dir, speech(),
                    recordings.Record(id, "", "notepad.exe", raw, [raw], raw, MODEL, "de"))
    return id


def fake_checker(result="nothing to check", runs=None, release=None):
    """A Checker stand-in: run() notes should_stop() and returns `result` (after `release` is set, if given)."""
    def run(should_stop):
        if runs is not None:
            runs.append(should_stop())
        if release is not None:
            release.wait(5)
        return result
    return types.SimpleNamespace(run=run, stop=release.set if release else lambda: None)


def write_auto(app, *entries) -> None:
    """learned_auto.toml with these automatic fixes (the Store writes it in the shape of learned.toml)."""
    app.paths.learned_auto_file.parent.mkdir(parents=True, exist_ok=True)
    app.paths.learned_auto_file.write_text(learn.dumps(entries), "utf-8")


def add_learned(app, *entries) -> None:
    app.paths.learned_file.parent.mkdir(parents=True, exist_ok=True)
    learn.add(app.paths.learned_file, list(entries))


def finish_check(app) -> None:
    app._check_thread.join(5)
    assert not app._check_thread.is_alive()


# --- the hotkey --------------------------------------------------------------------------------------

def test_hotkey_during_check_stops_teacher_first(learner):
    """The hotkey while Whisper is in the middle of a recording: its process is killed at once, before the
    microphone opens, and the hotkey never waits for that Whisper call (nor calls unload(), which would)."""
    app = learner()
    kept(app, "send the mail t campaign")
    events = []
    whisper = FakeTeacher(events, block=True)
    app._new_teacher_engine = lambda spec: whisper
    app._learning_tick()
    assert whisper.transcribing.wait(5)                   # the check is waiting for Whisper
    stop = app.checker.stop
    app.checker.stop = lambda: (events.append("stop"), stop())

    def start():
        events.append("microphone")
        raise OSError("test: no microphone")              # the dictation ends here: nothing more to fake

    app.recorder = types.SimpleNamespace(start=start)
    me = threading.current_thread().name
    t0 = time.monotonic()
    app._on_hotkey()
    assert time.monotonic() - t0 < 1
    finish_check(app)
    assert [e for e in events if isinstance(e, str)] == ["stop", "kill", "microphone"]
    assert ("unload", "background-check") in events and ("unload", me) not in events   # the checker's own thread
    assert app.learning_paused is None                    # stopped by the user: not a crash, learning goes on


def test_check_starting_just_as_the_hotkey_is_pressed_stops_too(learner):
    """Checker.run begins by clearing an earlier stop(), so a check that starts right after the hotkey must
    still see the user is back, also before the app's state says "recording"."""
    app = learner()
    assert app._should_stop_check() is False
    app.recorder = types.SimpleNamespace(start=lambda: (_ for _ in ()).throw(OSError("test: no microphone")))
    app._on_hotkey()                                      # no check running: nothing to stop, no error
    assert app.state == "idle" and app._should_stop_check() is True


def test_retry_during_check_stops_teacher_first(learner, out):
    """Tray → Retry while Whisper is in the middle of a recording: like the hotkey, its process is killed
    before Parakeet transcribes (both on the 4 GB card would not fit), and Retry never waits for the check."""
    app = learner()
    kept(app, "send the mail t campaign")
    events = []
    whisper = FakeTeacher(events, block=True)
    app._new_teacher_engine = lambda spec: whisper
    app._learning_tick()
    assert whisper.transcribing.wait(5)                   # the check is waiting for Whisper
    stop = app.checker.stop
    app.checker.stop = lambda: (events.append("stop"), stop())
    app.failed = FailedRecording(speech(), SR, False)
    app.engine = FakeEngine("Hello again.")
    transcribe = app.engine.transcribe
    app.engine.transcribe = lambda audio: (events.append("transcribe"), transcribe(audio))[1]
    t0 = time.monotonic()
    retry(app)
    assert time.monotonic() - t0 < 1
    finish_check(app)
    assert [e for e in events if isinstance(e, str)] == ["stop", "kill", "transcribe"]
    assert out.typed == ["Hello again. "] and app.failed is None
    assert app.learning_paused is None                    # stopped by the user: not a crash


def test_a_checker_bug_never_breaks_the_hotkey(learner, caplog):
    app = learner()

    def broken():
        raise RuntimeError("checker bug")

    app.checker = types.SimpleNamespace(stop=broken)
    opened = []
    app.recorder = types.SimpleNamespace(start=lambda: (opened.append(1), (_ for _ in ()).throw(OSError("test: no mic"))))
    with caplog.at_level(logging.ERROR, logger="parakeet_dictate.app"):
        app._on_hotkey()
    assert opened == [1] and logged(caplog, "checker bug")   # the microphone was still opened
    app.checker = None                                    # the fixture's clean-up calls stop() again


# --- when the check runs -----------------------------------------------------------------------------

@pytest.mark.parametrize("change", ["recording", "busy", "model job", "user back", "battery", "gpu marker",
                                    "learning off", "quitting"])
def test_check_stops_when_a_condition_goes(learner, monkeypatch, change):
    app = learner()
    assert app._should_stop_check() is False
    if change in ("recording", "busy"):
        app.state = change
    elif change == "model job":
        app.model_job = "downloading 12%"
    elif change == "user back":
        monkeypatch.setattr(idle, "seconds_since_input", lambda: 5.0)
    elif change == "battery":
        monkeypatch.setattr(idle, "on_mains_power", lambda: False)
    elif change == "gpu marker":                          # dictation itself crashed on the graphics card
        app.paths.data_dir.mkdir(parents=True, exist_ok=True)
        (app.paths.data_dir / GPU_MARKER).write_text("{}", "utf-8")
    elif change == "learning off":
        app.cfg = dataclasses.replace(app.cfg, learning_enabled=False)
    else:
        app._quitting.set()
    assert app._should_stop_check() is True


def test_idle_minutes_from_the_settings(learner, monkeypatch):
    app = learner(idle_minutes=5)
    monkeypatch.setattr(idle, "seconds_since_input", lambda: 299.0)
    assert app._should_stop_check() is True
    monkeypatch.setattr(idle, "seconds_since_input", lambda: 301.0)
    assert app._should_stop_check() is False


def test_idle_tick_runs_the_check_only_when_idle_and_says_why_once(learner, monkeypatch):
    app = learner()
    runs = []
    checker = fake_checker(runs=runs)
    app._make_checker = lambda: checker
    monkeypatch.setattr(idle, "seconds_since_input", lambda: 10.0)
    for _ in range(3):
        app._learning_tick()
    assert runs == [] and app.statuses == ["Waits until the PC is idle"]   # written once, not every 30 s
    assert app.learning_store.status() == "Waits until the PC is idle"
    monkeypatch.setattr(idle, "seconds_since_input", lambda: 1e6)
    app._learning_tick()
    finish_check(app)
    app._learning_tick()
    finish_check(app)
    assert runs == [False, False] and app.checker is checker              # one Checker for the app run
    assert app.notes == []


def test_one_check_at_a_time(learner):
    app = learner()
    runs, release = [], threading.Event()
    app._make_checker = lambda: fake_checker(runs=runs, release=release)
    app._learning_tick()
    app._learning_tick()                                  # the first one is still running
    release.set()
    finish_check(app)
    assert runs == [False]


def test_checker_error_pauses_until_restart(learner):
    app = learner()
    runs = []
    make = app._make_checker = lambda: fake_checker("error: teacher crashed", runs=runs)
    app._learning_tick()
    finish_check(app)
    assert app.learning_paused == "error: teacher crashed"
    for _ in range(3):
        app._learning_tick()
    assert runs == [False] and app.statuses == [] and app.notes == []   # the checker's own status stands
    restarted = learner()
    restarted._make_checker = make
    assert restarted.learning_paused is None
    restarted._learning_tick()
    finish_check(restarted)
    assert runs == [False, False]


def test_learning_off_says_so_and_neither_downloads_nor_checks(learner, monkeypatch):
    calls = []
    monkeypatch.setattr(teacher, "has_discrete_gpu", lambda cmd, timeout=30: calls.append(cmd) or True)
    app = learner(teacher_ready=False, learning_enabled=False)
    app._make_checker = lambda: calls.append("check")
    app._learning_tick()
    app._learning_tick()
    assert app.statuses == ["Off"] and app._check_thread is None
    assert not any(t.name == "whisper-setup" for t in threading.enumerate()) and calls == []


def test_status_write_errors_are_only_logged(learner, monkeypatch, caplog):
    app = learner()

    def locked(text):
        raise TimeoutError("the learning state is locked by another process")

    app.learning_store.set_status = locked
    monkeypatch.setattr(idle, "seconds_since_input", lambda: 1.0)
    with caplog.at_level(logging.WARNING, logger="parakeet_dictate.app"):
        app._learning_tick()
    assert logged(caplog, "locked by another process") and app.notes == []


def test_idle_check_every_30_seconds_by_default():
    assert App.IDLE_EVERY_SECONDS == 30


def test_idle_loop_ticks_on_its_timer_and_ends_on_quit(learner, monkeypatch):
    monkeypatch.setattr(App, "IDLE_EVERY_SECONDS", 0.01)
    app = learner()
    ticks = []

    def tick():
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("tick failed")             # logged; the loop carries on

    app._learning_tick = tick
    app._start_learning()
    wait_for(lambda: len(ticks) >= 3)
    app._quitting.set()
    wait_for(lambda: not any(t.name == "background-learning" for t in threading.enumerate()))


def test_quit_kills_a_running_check(learner):
    app = learner()
    stopped = []
    app.checker = types.SimpleNamespace(stop=lambda: stopped.append(1))
    app.engine = types.SimpleNamespace(unload=lambda: None, state="unloaded")
    app.hotkeys = types.SimpleNamespace(stop=lambda: None)
    [quit_item] = [i for i in app._menu().items if isinstance(i.text, str) and i.text.startswith("Quit")]
    quit_item(types.SimpleNamespace(stop=lambda: None))
    assert stopped == [1] and app._quitting.is_set()


# --- Whisper's download ------------------------------------------------------------------------------

@pytest.mark.parametrize("frozen", [False, True])
def test_no_gpu_skips_download(learner, monkeypatch, frozen):
    asked, ensured = [], []
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(teacher, "has_discrete_gpu", lambda cmd, timeout=30: asked.append(cmd) or False)
    monkeypatch.setattr(teacher, "ensure", lambda *a, **k: ensured.append(a))
    app = learner(teacher_ready=False)
    app._setup_teacher()
    assert asked == [[sys.executable] if frozen else [sys.executable, "-m", "parakeet_dictate"]]
    assert ensured == [] and app.teacher_path is None
    assert app.statuses == ["Waiting for a graphics card"] and app.notes == []


def test_download_shows_progress_then_ready(learner, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(teacher, "has_discrete_gpu", lambda cmd, timeout=30: True)
    calls = []

    def ensure(teacher_dir, orca_dir, progress, *, learning_dir=None):
        calls.append((teacher_dir, orca_dir, learning_dir))
        for done in (0, 100, 410, 412, 1000):
            progress(done, 1000)
        return tmp_path / WHISPER

    monkeypatch.setattr(teacher, "ensure", ensure)
    app = learner(teacher_ready=False)
    app._setup_teacher()
    assert calls == [(app.paths.teacher_dir, tmp_path / "roaming" / "orca" / "speech-models", app.paths.learning_dir)]
    assert app.statuses == ["Downloading Whisper 0%", "Downloading Whisper 10%", "Downloading Whisper 41%",
                            "Whisper ready"]
    assert app.teacher_path == tmp_path / WHISPER and app.notes == []


def test_whisper_setup_starts_once_in_the_background(learner, monkeypatch):
    calls, release = [], threading.Event()

    def gpu(cmd, timeout=30):
        calls.append(threading.current_thread().name)
        release.wait(5)
        return False

    monkeypatch.setattr(teacher, "has_discrete_gpu", gpu)
    app = learner(teacher_ready=False)
    app._learning_tick()
    wait_for(lambda: calls)
    app._learning_tick()                                  # while it still runs
    release.set()
    wait_for(lambda: app.statuses == ["Waiting for a graphics card"])
    app._learning_tick()                                  # done: not again in this app run
    assert calls == ["whisper-setup"] and app._check_thread is None


def test_download_failure_is_a_status_and_retried_at_the_next_start(learner, monkeypatch):
    monkeypatch.setattr(teacher, "has_discrete_gpu", lambda cmd, timeout=30: True)
    tries = []

    def ensure(*a, **k):
        tries.append(1)
        raise OSError("no network")

    monkeypatch.setattr(teacher, "ensure", ensure)
    app = learner(teacher_ready=False)
    app._learning_tick()
    wait_for(lambda: app.statuses == ["Download failed: no network"])
    app._learning_tick()
    assert tries == [1] and app.teacher_path is None and app.notes == []
    restarted = learner(teacher_ready=False)
    restarted._learning_tick()
    wait_for(lambda: tries == [1, 1])


def test_quitting_ends_the_download(learner, monkeypatch):
    monkeypatch.setattr(teacher, "has_discrete_gpu", lambda cmd, timeout=30: True)
    app = learner(teacher_ready=False)

    def ensure(teacher_dir, orca_dir, progress, **k):
        progress(10, 1000)
        app._quitting.set()
        progress(20, 1000)                                # the download ends here
        raise AssertionError("not reached")

    monkeypatch.setattr(teacher, "ensure", ensure)
    app._setup_teacher()
    assert app.statuses == ["Downloading Whisper 1%"] and app.teacher_path is None


def test_turning_learning_off_stops_the_download(learner, monkeypatch):
    """The progress callback ends the download (its .part stays, see test_teacher), the status says "Off",
    and turning learning on again in the same app run starts the set-up again, so the download resumes."""
    monkeypatch.setattr(teacher, "has_discrete_gpu", lambda cmd, timeout=30: True)
    app = learner(teacher_ready=False)
    calls = []

    def ensure(teacher_dir, orca_dir, progress, **k):
        calls.append(1)
        progress(10, 1000)
        app.cfg = dataclasses.replace(app.cfg, learning_enabled=False)   # Settings saved, the tray reloaded
        progress(20, 1000)                                # the download ends here
        raise AssertionError("not reached")

    monkeypatch.setattr(teacher, "ensure", ensure)
    app._teacher_tried = True                             # what the idle loop does before it starts the set-up
    app._setup_teacher()
    assert app.statuses == ["Downloading Whisper 1%", "Off"] and app.teacher_path is None
    assert app.learning_store.status() == "Off" and app.notes == []
    app._learning_tick()                                  # still off: nothing starts
    assert calls == [1]
    app.cfg = dataclasses.replace(app.cfg, learning_enabled=True)
    monkeypatch.setattr(teacher, "ensure", lambda *a, **k: calls.append(2) or app.paths.teacher_dir / WHISPER)
    app._learning_tick()
    wait_for(lambda: app.teacher_path is not None)
    assert calls == [1, 2]


def test_gpu_marker_skips_download(learner, monkeypatch):
    """Dictation already crashed the graphics card (gpu-failed.json): learning can never run, so the 886 MB
    are not downloaded, and the card isn't even asked about."""
    asked, ensured = [], []
    monkeypatch.setattr(teacher, "has_discrete_gpu", lambda cmd, timeout=30: asked.append(cmd) or True)
    monkeypatch.setattr(teacher, "ensure", lambda *a, **k: ensured.append(a))
    app = learner(teacher_ready=False)
    app.paths.data_dir.mkdir(parents=True, exist_ok=True)
    (app.paths.data_dir / GPU_MARKER).write_text("{}", "utf-8")
    app._setup_teacher()
    assert asked == [] and ensured == [] and app.teacher_path is None
    assert app.statuses == ["Paused: no graphics card"] and app.notes == []


# --- no voice kept where learning can never run -------------------------------------------------------

def test_no_discrete_gpu_keeps_no_recordings(learner, out, saves, monkeypatch, pruning):
    monkeypatch.setattr(teacher, "has_discrete_gpu", lambda cmd, timeout=30: False)
    app = learner(teacher_ready=False)
    old, recent = old_recording(app.paths.recordings_dir, 30), old_recording(app.paths.recordings_dir, 1)
    app._setup_teacher()
    app.engine = FakeEngine("Hello world.")
    dictate(app)
    finish_saving()
    assert out.typed == ["Hello world. "] and [s.rec.id for s in saves] == [old, recent]   # only the two above
    pruning(app)                                          # recordings kept earlier still age out
    wait_for(lambda: [r.id for r in recordings.list_records(app.paths.recordings_dir)] == [recent])


def test_gpu_marker_keeps_no_recordings(learner, out, saves):
    app = learner()
    app.paths.data_dir.mkdir(parents=True, exist_ok=True)
    (app.paths.data_dir / GPU_MARKER).write_text("{}", "utf-8")
    app.engine = FakeEngine("Hello world.")
    dictate(app)
    finish_saving()
    assert out.typed == ["Hello world. "] and saves == []


def test_recordings_kept_when_the_pc_has_a_graphics_card(learner, out, saves, monkeypatch):
    monkeypatch.setattr(teacher, "has_discrete_gpu", lambda cmd, timeout=30: True)
    app = learner(teacher_ready=False)
    monkeypatch.setattr(teacher, "ensure", lambda *a, **k: app.paths.teacher_dir / WHISPER)
    app._setup_teacher()
    app.engine = FakeEngine("Hello world.")
    dictate(app)
    finish_saving()
    [s] = saves
    assert s.rec.raw == "Hello world."


# --- what the checker is given -----------------------------------------------------------------------

@pytest.mark.parametrize("setting, model_languages, expected",
                         [("auto", ("en",), "en"), ("auto", ("de", "en"), "auto"), ("de", ("de", "en"), "de")])
def test_teacher_spec_language_terms_and_live_settings(learner, setting, model_languages, expected):
    app = learner(dictionary=(("Keycloak", ""),), context_enabled=True)
    app.context = types.SimpleNamespace(everywhere=["ss-core-service"])
    add_learned(app, ("Zanjo", "zan jo"))
    write_auto(app, ("Mailzzy", "MailT"))                 # automatic fixes are not "your" terms
    app._check_learned()
    app.model = dataclasses.replace(app.model, languages=model_languages)
    specs = []
    app._new_teacher_engine = lambda spec: specs.append(spec) or FakeTeacher([], text="hello there")
    checker = app._make_checker()
    app.cfg = dataclasses.replace(app.cfg, language=setting, threads=3)   # Settings changed after it was made
    kept(app, "hello there")
    assert checker.run(lambda: False) == "Checked 1 dictation"
    [spec] = specs
    assert (spec.model, spec.language, spec.threads, spec.gpu_marker) == (str(app.teacher_path), expected, 3, "")
    assert spec.prompt == "Keycloak, Zanjo, ss-core-service"


def test_user_entries_are_yours_and_learned_toml_not_the_automatic_ones(learner):
    app = learner(dictionary=(("Keycloak", ""), ("Mailzzy", "mail sey")))
    add_learned(app, ("Zanjo", "zan jo"))
    write_auto(app, ("Mailtrap", "mail t"))
    app._check_learned()
    assert app.learned and app.learned_auto
    assert app._user_entries() == [("Keycloak", ""), ("Mailzzy", "mail sey"), *app.learned]


def test_each_check_gets_a_new_teacher_engine(learner):
    from parakeet_dictate.checker import teacher_spec
    app = learner()
    spec = teacher_spec(str(app.teacher_path), 4, "en", "")
    a, b = app._new_teacher_engine(spec), app._new_teacher_engine(spec)
    assert isinstance(a, Engine) and a is not b and a.state == "unloaded"   # a killed engine stays closed


# --- automatic fixes in the clean-up -----------------------------------------------------------------

def test_auto_fixes_used_by_cleaner(learner):
    app = learner()
    write_auto(app, ("Mailzzy", "MailT"))
    app._check_learned()
    assert "Mailzzy" in app.cleaner_for("x").clean("send the MailT campaign")
    assert "Mailzzy" in app.cleaner.clean("send the MailT campaign")


def test_a_new_auto_fix_is_picked_up_at_the_next_dictation(learner):
    app = learner()
    app._check_learned()
    assert "Mailzzy" not in app.cleaner_for("x").clean("send the MailT campaign")
    write_auto(app, ("Mailzzy", "MailT"))
    app._check_learned()                                  # what each dictation does first
    assert "Mailzzy" in app.cleaner_for("x").clean("send the MailT campaign")


def test_user_learned_beats_auto_on_clash(learner):
    app = learner(dictionary=(("Mailchimp", "mail c"),))
    add_learned(app, ("Mailtrap", "MailT"))
    write_auto(app, ("Mailzzy", "MailT"), ("Mailzzy", "mail c"))
    app._check_learned()
    cleaner = app.cleaner_for("x")
    assert "Mailtrap" in cleaner.clean("send the MailT campaign")          # learned.toml beats the automatic fix
    assert "Mailchimp" in cleaner.clean("open the mail c account")        # and so does your dictionary
    assert "Mailzzy" not in cleaner.clean("send the MailT campaign via mail c")
