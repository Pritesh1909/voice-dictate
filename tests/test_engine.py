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
                                                         ("gpu", "Fake RTX")))
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