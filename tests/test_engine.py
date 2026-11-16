"""Device choice for the speech engine (no model or GPU needed)."""
from types import SimpleNamespace as D

import pytest

from parakeet_dictate import config
from parakeet_dictate.engine import device_order

# The order transcribe.cpp lists them on this laptop.
DEVS = [D(device_type="igpu", name="Vulkan0"), D(device_type="gpu", name="Vulkan1"), D(device_type="cpu", name="CPU")]


def names(want, devs=DEVS):
    return [d.name for d in device_order(devs, want)]


def test_auto_prefers_discrete_gpu_then_integrated_then_cpu():
    assert names("auto") == ["Vulkan1", "Vulkan0", "CPU"]


def test_explicit_choices_always_end_with_cpu():
    assert names("gpu") == ["Vulkan1", "CPU"]
    assert names("igpu") == ["Vulkan0", "CPU"]
    assert names("cpu") == ["CPU"]


def test_no_gpu_falls_back_to_cpu():
    assert names("gpu", [D(device_type="cpu", name="CPU")]) == ["CPU"]


def test_device_setting():
    assert config.parse("").device == "auto"
    assert config.parse('[model]\ndevice = "cpu"').device == "cpu"
    with pytest.raises(config.ConfigError):
        config.parse('[model]\ndevice = "tpu"')


def test_old_settings_file_with_hotwords_score_still_loads():
    assert config.parse("[accuracy]\nhotwords_score = 2.0").hotwords


# --- the Whisper teacher: an initial prompt, and which device it loaded on --------------------------

class _Session:
    def __init__(self):
        self.kwargs = []

    def run(self, samples, **kw):
        self.kwargs.append(kw)
        return D(text="hello")


class _Conn:
    """The child's pipe end for running _worker in this thread: one decode, then quit."""
    def __init__(self):
        self.sent = []
        self._inbox = [("decode", [0.0] * 16000), ("quit",)]

    def send(self, msg):
        self.sent.append(msg)

    def recv(self):
        return self._inbox.pop(0)


def _run_worker(monkeypatch, spec):
    from parakeet_dictate import engine as eng
    session = _Session()
    monkeypatch.setattr(eng, "_load", lambda s, report: (object(), session, None, None, "on Fake RTX", "",
                                                         ("gpu", "Fake RTX"), False))
    monkeypatch.setattr(eng, "_load_vad", lambda path: (None, None, 0, ""))
    conn = _Conn()
    eng._worker(conn, spec)
    return session.kwargs, conn.sent


def test_worker_passes_the_prompt_only_when_set(monkeypatch):
    from parakeet_dictate.engine import EngineSpec
    [kw], sent = _run_worker(monkeypatch, EngineSpec("w.gguf", 4, prompt="Mailzzy, Keycloak"))
    assert kw["prompt"] == "Mailzzy, Keycloak"
    assert sent[-1][:2] == ("text", "hello")
    [kw], _ = _run_worker(monkeypatch, EngineSpec("p.gguf", 4))
    assert "prompt" not in kw   # Parakeet takes no prompt


def test_device_type_after_ready():
    from parakeet_dictate.engine import Engine, EngineSpec
    e = Engine(EngineSpec("m.gguf", 4), unload_after=-1)
    assert e.device_type is None
    e.device = ("gpu", "NVIDIA GeForce RTX 2050")   # what the "ready" message sets
    assert e.device_type == "gpu"

# --- streaming (models such as Parakeet Unified EN) -------------------------------------------------

class _Stream:
    def __init__(self, log):
        self.log, self.fed = log, 0

    def feed(self, samples):
        self.fed += len(samples)
        self.log.append(("feed", len(samples)))

    def text(self):
        return D(committed="I think", tentative=f" we ({self.fed})")

    def reset(self):
        self.log.append(("reset",))


class _StreamSession(_Session):
    def __init__(self):
        super().__init__()
        self.log = []

    def stream(self, **kw):
        self.log.append(("begin",))
        return _Stream(self.log)

    def run(self, samples, **kw):
        self.log.append(("run",))
        return super().run(samples, **kw)


def test_worker_streams_a_phrase_and_a_decode_ends_it(monkeypatch):
    from parakeet_dictate import engine as eng
    session = _StreamSession()
    monkeypatch.setattr(eng, "_load", lambda s, report: (object(), session, None, None, "on Fake RTX", "",
                                                         ("gpu", "Fake RTX"), True))
    monkeypatch.setattr(eng, "_load_vad", lambda path: (None, None, 0, ""))
    conn = _Conn()
    conn._inbox = [("stream", [0.0] * 4000, True), ("stream", [0.0] * 4000, False), ("decode", [0.0] * 16000),
                   ("stream", [0.0] * 4000, False), ("quit",)]
    eng._worker(conn, eng.EngineSpec("u.gguf", 4))
    ready, first, second, text, orphan = [m for m in conn.sent if m[0] != "loading"]
    assert ready[0] == "ready" and ready[6] is True               # the model says it can stream
    assert first[:3] == ("partial", "I think", " we (4000)")
    assert second[:3] == ("partial", "I think", " we (8000)")    # the same phrase, carried on
    assert text[:2] == ("text", "hello")
    assert orphan[0] == "error" and "no phrase" in orphan[1]       # the decode ended the stream
    assert session.log == [("begin",), ("feed", 4000), ("feed", 4000), ("reset",), ("run",)]


def test_can_stream_only_with_a_streaming_model_on_a_graphics_card():
    from parakeet_dictate.engine import Engine, EngineSpec
    e = Engine(EngineSpec("m.gguf", 4), unload_after=-1)
    for streams, device, want in [(True, ("gpu", "RTX"), True), (True, ("igpu", "Iris Xe"), True),
                                  (True, ("cpu", "i7"), False), (False, ("gpu", "RTX"), False)]:
        e.streams, e.device = streams, device
        assert e.can_stream is want
    assert Engine(EngineSpec("m.gguf", 4), unload_after=-1).can_stream is False   # nothing loaded yet


def test_stream_without_a_streaming_model_returns_none():
    from parakeet_dictate.engine import Engine, EngineSpec
    e = Engine(EngineSpec("m.gguf", 4), unload_after=-1)
    e._ready.set()
    e._proc = object()
    assert e.stream([0.0] * 4000, True) is None    # streams is False: never asks the engine process
