"""Engine process failures, with a fake engine running in a thread instead of a real child process
(no model, no GPU): GPU crashes are remembered, timeouts unload the engine."""
import json
import multiprocessing
import threading

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
