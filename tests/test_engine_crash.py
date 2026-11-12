"""Engine process failures, with a fake engine running in a thread instead of a real child process
(no model, no GPU): GPU crashes are remembered, timeouts unload the engine."""
import json
import multiprocessing
import threading
import time

import numpy as np
import pytest

from parakeet_dictate import engine as eng
from parakeet_dictate.engine import Engine, EngineError, EngineSpec, choose_device, read_gpu_marker

CRASH = 3221225477          # 0xC0000005, what a driver crash leaves as the exit code
AUDIO = np.zeros(16000, np.float32)


class _ChildEnd:
    """The child's pipe end. The parent closes its copy after starting a real process; here there is only
    one copy, which the fake child keeps using."""
    def __init__(self, conn):
        self.conn = conn

    def close(self):
        pass


class _ThreadProcess:
    def __init__(self, target, args, daemon=True, name=""):
        child, spec = args
        self.exitcode = None
        self._t = threading.Thread(target=self._run, args=(target, child.conn, spec), daemon=True)

    def _run(self, target, conn, spec):
        code = target(conn, spec)
        self.exitcode = 0 if code is None else code

    def start(self):
        self._t.start()

    def is_alive(self):
        return self._t.is_alive()

    def join(self, timeout=None):
        self._t.join(timeout)

    def terminate(self):
        pass


class _Ctx:
    Process = _ThreadProcess

    @staticmethod
    def Pipe():
        a, b = multiprocessing.Pipe()
        return a, _ChildEnd(b)


def fake_worker(devices, crash_load=(), crash_decode=(), hang=""):
    """A stand-in for engine._worker. Records the device each process was asked for."""
    def worker(conn, spec):
        devices.append(spec.device)
        dev = ("cpu", "Fake CPU") if spec.device == "cpu" else ("gpu", "Fake RTX")
        conn.send(("loading", *dev))
        if spec.device in crash_load:
            conn.close()
            return CRASH
        if hang == "load":
            conn.poll(10)       # until the parent gives up and says quit
            return 0
        conn.send(("ready", 0.1, f"on {dev[1]}", 0, dev, ""))
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                return 0
            if msg[0] == "quit":
                return 0
            if msg[0] == "decode":
                if spec.device in crash_decode:
                    conn.close()
                    return CRASH
                if hang == "decode":
                    conn.poll(10)
                    return 0
                conn.send(("text", f"hello from {dev[1]}", 0.01))
    return worker


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(eng.mp, "get_context", lambda kind: _Ctx)
    marker = tmp_path / "gpu-failed.json"

    def make(device="auto", **kw):
        devices = []
        monkeypatch.setattr(eng, "_worker", fake_worker(devices, **kw))
        e = Engine(EngineSpec("m.gguf", 4, device=device, gpu_marker=str(marker)), unload_after=-1)
        return e, devices, marker
    yield make


def test_gpu_crash_while_loading_is_remembered_and_retried_on_cpu(setup):
    e, devices, marker = setup(crash_load=("auto",))
    assert e.transcribe(AUDIO) == ("hello from Fake CPU", 0.01)
    assert devices == ["auto", "cpu"]
    m = json.loads(marker.read_text("utf-8"))
    assert m["device"] == "Fake RTX" and m["during"] == "loading" and m["exit_code"] == CRASH and m["time"]
    assert "CPU only" in e.note and "gpu-failed.json" in e.note
    e.unload()


def test_gpu_crash_while_decoding_is_remembered_and_retried_on_cpu(setup):
    e, devices, marker = setup(crash_decode=("auto",))
    assert e.transcribe(AUDIO)[0] == "hello from Fake CPU"
    assert devices == ["auto", "cpu"]
    assert json.loads(marker.read_text("utf-8"))["during"] == "decoding"
    e.unload()


def test_marker_makes_auto_use_the_cpu_until_deleted(setup):
    e, devices, marker = setup()
    marker.write_text(json.dumps({"device": "Fake RTX", "time": "2026-11-12 10:00:00"}), "utf-8")
    assert e.transcribe(AUDIO)[0] == "hello from Fake CPU"
    e.unload()
    marker.unlink()
    assert e.transcribe(AUDIO)[0] == "hello from Fake RTX"
    assert devices == ["cpu", "auto"]
    e.unload()


def test_explicit_gpu_ignores_the_marker(setup):
    e, devices, marker = setup(device="gpu")
    marker.write_text("{}", "utf-8")
    assert e.transcribe(AUDIO)[0] == "hello from Fake RTX"
    assert devices == ["gpu"]
    e.unload()


def test_normal_use_and_unload_never_write_the_marker(setup):
    e, devices, marker = setup()
    e.warm()
    assert e.wait_ready(5)
    e.transcribe(AUDIO)
    e.unload()
    e.warm()
    e.unload()          # unloaded while (possibly) still loading
    assert not marker.exists()


def test_cpu_crash_is_not_a_gpu_failure(setup):
    e, devices, marker = setup(device="cpu", crash_decode=("cpu",))
    with pytest.raises(EngineError, match="stopped"):
        e.transcribe(AUDIO)
    assert not marker.exists() and devices == ["cpu"] and e.state == "unloaded"


def test_explicit_gpu_crash_is_recorded_but_not_retried(setup):
    e, devices, marker = setup(device="gpu", crash_decode=("gpu",))
    with pytest.raises(EngineError):
        e.transcribe(AUDIO)
    assert marker.exists() and devices == ["gpu"]


def test_decode_timeout_unloads(setup, monkeypatch):
    monkeypatch.setattr(eng, "DECODE_TIMEOUT", 0.3)
    e, devices, marker = setup(hang="decode")
    with pytest.raises(EngineError, match="Transcription took longer than a minute."):
        e.transcribe(AUDIO)
    assert e.state == "unloaded"        # so a late reply can't be read as the next request's answer
    assert not marker.exists()


def test_load_timeout_unloads(setup, monkeypatch):
    monkeypatch.setattr(eng, "LOAD_TIMEOUT", 0.3)
    e, devices, marker = setup(hang="load")
    with pytest.raises(EngineError, match="The speech model took longer than a minute to load."):
        e.transcribe(AUDIO)
    assert e.state == "unloaded" and not marker.exists()


def test_time_limits_are_a_minute():
    assert eng.LOAD_TIMEOUT == eng.DECODE_TIMEOUT == 60.0


def test_choose_device():
    assert choose_device("auto", None) == ("auto", "")
    dev, why = choose_device("auto", {"device": "NVIDIA RTX 2050", "time": "2026-11-12 10:00:00"})
    assert dev == "cpu" and "NVIDIA RTX 2050" in why and "2026-11-12" in why
    assert choose_device("auto", {})[0] == "cpu"            # unreadable marker still counts
    assert choose_device("gpu", {}) == ("gpu", "")
    assert choose_device("igpu", {}) == ("igpu", "")
    assert choose_device("cpu", {}) == ("cpu", "")


def test_read_gpu_marker(tmp_path):
    p = tmp_path / "gpu-failed.json"
    assert read_gpu_marker("") is None and read_gpu_marker(str(p)) is None
    p.write_text("not json", "utf-8")
    assert read_gpu_marker(str(p)) == {}
    p.write_text('{"device": "X"}', "utf-8")
    assert read_gpu_marker(str(p)) == {"device": "X"}


# --- kill(): the hotkey ends the Whisper teacher at once ----------------------------------------------

class _KillableChild:
    """The child's pipe end plus what terminate() does to a real process: `killed` is set."""
    def __init__(self, conn):
        self.conn = conn
        self.killed = threading.Event()

    def close(self):
        pass


class _KillableProcess:
    """A fake engine process that terminate() really ends: its thread stops and closes its pipe end, as
    Windows does for a terminated process (so the parent's poll/recv see the pipe end, not a reply)."""
    def __init__(self, target, args, daemon=True, name=""):
        self.child, spec = args
        self.exitcode = None
        self._t = threading.Thread(target=self._run, args=(target, spec), daemon=True)

    def _run(self, target, spec):
        try:
            target(self.child, spec)
        finally:
            self.child.conn.close()
            self.exitcode = 1 if self.child.killed.is_set() else 0

    def start(self):
        _KillCtx.started += 1
        self._t.start()

    def is_alive(self):
        return self._t.is_alive()

    def join(self, timeout=None):
        self._t.join(timeout)

    def terminate(self):
        self.child.killed.set()


class _KillCtx:
    Process = _KillableProcess
    started = 0

    @staticmethod
    def Pipe():
        a, b = multiprocessing.Pipe()
        return a, _KillableChild(b)


def killable_worker(hang=""):
    """A fake engine._worker on a graphics card that hangs while loading or decoding until terminated."""
    def worker(child, spec):
        conn = child.conn
        conn.send(("loading", "gpu", "Fake RTX"))
        if hang == "load":
            child.killed.wait(10)
            return
        conn.send(("ready", 0.1, "on Fake RTX", 0, ("gpu", "Fake RTX"), ""))
        while not child.killed.is_set():
            if not conn.poll(0.02):
                continue
            msg = conn.recv()
            if msg[0] == "quit":
                return
            if msg[0] == "decode":
                if hang == "decode":
                    child.killed.wait(10)
                    return
                conn.send(("text", "hello from Fake RTX", 0.01))
    return worker


@pytest.fixture
def killable(monkeypatch, tmp_path):
    """An Engine whose fake process can be killed; returns (engine, marker path)."""
    monkeypatch.setattr(eng.mp, "get_context", lambda kind: _KillCtx)
    monkeypatch.setattr(_KillCtx, "started", 0)
    marker = tmp_path / "gpu-failed.json"

    def make(hang="", device="auto"):
        monkeypatch.setattr(eng, "_worker", killable_worker(hang))
        return Engine(EngineSpec("m.gguf", 4, device=device, gpu_marker=str(marker)), unload_after=-1), marker
    return make


def _kill_soon(e, after=0.2):
    t = threading.Timer(after, e.kill)
    t.start()
    return t


def test_kill_ends_a_long_transcribe_at_once(killable):
    e, marker = killable(hang="decode")
    e.warm()
    assert e.wait_ready(5)
    _kill_soon(e)
    t0 = time.monotonic()
    with pytest.raises(EngineError, match="stopped"):
        e.transcribe(AUDIO)
    assert time.monotonic() - t0 < 1.5
    assert not marker.exists()          # a kill is not a GPU crash: dictation stays on the card
    assert _KillCtx.started == 1        # and not retried on the CPU
    e.unload()


def test_a_killed_engine_never_loads_again(killable):
    e, marker = killable()
    assert e.transcribe(AUDIO)[0] == "hello from Fake RTX"
    e.kill()
    with pytest.raises(EngineError, match="stopped"):
        e.transcribe(AUDIO)
    with pytest.raises(EngineError, match="stopped"):
        e.warm()
    e.unload()
    with pytest.raises(EngineError, match="stopped"):
        e.transcribe(AUDIO)
    assert _KillCtx.started == 1 and not marker.exists()


def test_kill_while_loading_ends_the_wait(killable):
    e, marker = killable(hang="load")
    e.warm()
    _kill_soon(e)
    t0 = time.monotonic()
    assert e.wait_ready(10) is False
    assert time.monotonic() - t0 < 1.5
    time.sleep(0.2)                     # the loading thread sees the pipe end: no CPU retry, no marker
    assert _KillCtx.started == 1 and not marker.exists()
    e.unload()


def test_kill_is_harmless_when_nothing_runs(killable):
    e, marker = killable()
    e.kill()
    e.kill()
    with pytest.raises(EngineError):
        e.warm()
    assert _KillCtx.started == 0