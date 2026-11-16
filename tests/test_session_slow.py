"""A whole dictation session with the real model and Silero voice detector, fed TTS speech at real-time pace.
Keystrokes are captured, never sent: nothing is typed into any window."""
import threading
import time
import types

import numpy as np
import pytest

from parakeet_dictate import config, models, output
from parakeet_dictate.app import Session
from parakeet_dictate.engine import Engine, EngineSpec
from parakeet_dictate.textclean import Cleaner

from test_engine_slow import _model, _tts

pytestmark = pytest.mark.slow
SR = 16000


class FakeRecorder:
    """Hands out the audio as a microphone would: only what has 'arrived' since the last drain."""

    def __init__(self, audio: np.ndarray):
        self.audio, self.rate, self.pos, self.t0 = audio, SR, 0, time.monotonic()

    def drain(self) -> np.ndarray:
        upto = min(self.audio.size, int((time.monotonic() - self.t0) * SR))
        x, self.pos = self.audio[self.pos:upto], max(self.pos, upto)
        return x

    @property
    def done(self) -> bool:
        return self.pos >= self.audio.size


@pytest.fixture(scope="module")
def vad_engine():
    vad = models.find_vad("auto")
    if vad is None:
        pytest.skip("Silero VAD not on this PC")
    c = config.parse("")
    e = Engine(EngineSpec.for_model(_model(), c.threads, device=c.device, vad=str(vad)), unload_after=-1)
    e.warm()
    assert e.wait_ready() and e.vad_frame == 512, e.note
    yield e
    e.unload()


def run_session(engine, audio, monkeypatch, use_vad=True, screen=None, **cfg_over):
    """screen: a list [text] that typed chunks are applied to, Backspace included (and the window is
    treated as unchanged, so streaming may correct itself)."""
    sent = []

    def deliver(text, method):
        sent.append((time.monotonic(), text))
        if screen is not None:
            screen[0] += text

    def backspace(n):
        sent.append((time.monotonic(), f"<bs {n}>"))
        screen[0] = screen[0][:-n] if n else screen[0]

    monkeypatch.setattr(output, "deliver", deliver)
    monkeypatch.setattr(output, "backspace", backspace)
    monkeypatch.setattr(output, "modifiers_down", lambda: False)
    if screen is not None:
        from parakeet_dictate import winutil
        monkeypatch.setattr(winutil, "foreground_window", lambda: 1)
    cfg_over.setdefault("stream_while_speaking", False)
    cfg = config.parse("")
    cfg = config.Config(**{**cfg.__dict__, **cfg_over})
    app = types.SimpleNamespace(cfg=cfg, recorder=FakeRecorder(audio), engine=engine,
                                cleaner_for=lambda exe: Cleaner.from_config(cfg), target_exe="",
                                target_hwnd=1 if screen is not None else 0, target_terms=[],
                                _session=1, state="recording")
    s = Session(app, 1, use_vad=use_vad)
    while not app.recorder.done:
        time.sleep(0.05)
    stopped = time.monotonic()
    app.state = "busy"
    raw = s.finish()
    return s, raw, sent, stopped


def speech(tmp_path):
    a = _tts("Uh, I think we should, um, deploy the core service first.", tmp_path / "s1.wav")
    b = _tts("Then check the key cloak token on code commit.", tmp_path / "s2.wav")
    c = _tts("And send the mail sey campaign.", tmp_path / "s3.wav")
    gap = lambda sec: np.random.default_rng(1).normal(0, 0.0005, int(sec * SR)).astype(np.float32)
    return np.concatenate([gap(0.5), a, gap(1.2), b, gap(1.2), c, gap(0.3)])


def test_phrases_are_typed_while_speaking(vad_engine, monkeypatch, tmp_path):
    s, raw, sent, stopped = run_session(vad_engine, speech(tmp_path), monkeypatch)
    typed = "".join(t for _, t in sent)
    print(f"\nraw:   {raw}\ntyped: {typed!r}\nchunks: {[(round(t - stopped, 2), x) for t, x in sent]}")
    assert s.vad_mode and s.parts >= 3
    assert typed == ("I think we should deploy the core service first. Then check the Keycloak token "
                     "on CodeCommit. And send a Mailzzy campaign. ")
    assert sum(1 for t, _ in sent if t < stopped) >= 2      # the first phrases landed before the end


def test_same_text_as_typing_at_the_end(vad_engine, monkeypatch, tmp_path):
    audio = speech(tmp_path)
    _, raw_live, sent, _ = run_session(vad_engine, audio, monkeypatch)
    s, raw_end, sent_end, _ = run_session(vad_engine, audio, monkeypatch, type_while_speaking=False)
    assert s.typer is None and sent_end == []                # the app types it once, after finish()
    assert Cleaner.from_config(config.parse("")).clean(raw_end) + " " == "".join(t for _, t in sent)


EXPECTED = ("I think we should deploy the core service first. Then check the Keycloak token on CodeCommit. "
            "And send a Mailzzy campaign. ")


def test_streaming_types_words_before_the_pause_and_ends_with_the_same_text(vad_engine, monkeypatch, tmp_path):
    audio = speech(tmp_path)
    screen = [""]
    s, raw, sent, stopped = run_session(vad_engine, audio, monkeypatch, screen=screen, stream_while_speaking=True)
    t0 = stopped - audio.size / SR
    print(f"\nscreen: {screen[0]!r}\npreview reads: {s.preview_seconds:.2f}s of model time")
    for t, x in sent:
        print(f"  {t - t0:6.2f}s  {x!r}")
    assert s.stream
    assert screen[0] == EXPECTED
    # Streaming sent more pieces than the three phrases: words went out before each pause.
    assert len([x for _, x in sent if not x.startswith("<bs")]) > 3
    # The first words of the first sentence (about 0.5 s to 4 s into the audio) appeared before it ended.
    first_end = 0.5 + 3.4
    assert min(t - t0 for t, _ in sent) < first_end


def test_streaming_without_backspace_still_reads_right(vad_engine, monkeypatch, tmp_path):
    """Another window in front (no Backspace allowed): the typed words stay, the rest is added."""
    s, raw, sent, _ = run_session(vad_engine, speech(tmp_path), monkeypatch, stream_while_speaking=True)
    typed = "".join(x for _, x in sent)
    assert not any(x.startswith("<bs") for _, x in sent)
    for words in ("deploy the core service first.", "Keycloak token", "Mailzzy campaign."):
        assert words in typed


def test_falls_back_to_loudness_detector(vad_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(vad_engine, "vad", lambda *a, **k: None)      # detector "fails"
    s, raw, sent, _ = run_session(vad_engine, speech(tmp_path), monkeypatch)
    assert not s.vad_mode
    assert "Mailzzy campaign" in "".join(t for _, t in sent)


@pytest.fixture(scope="module")
def unified_engine():
    """Parakeet Unified EN, a model that streams, on the graphics card (skipped when either is missing)."""
    vad = models.find_vad("auto")
    found = [m for m in models.find(include_wsl=False) if "unified" in m.path.name.lower()]
    if vad is None or not found:
        pytest.skip("Parakeet Unified EN or Silero VAD not on this PC")
    c = config.parse("")
    e = Engine(EngineSpec.for_model(found[0], c.threads, device="auto", vad=str(vad)), unload_after=-1)
    e.warm()
    assert e.wait_ready() and e.streams, e.note
    if not e.can_stream:
        e.unload()
        pytest.skip(f"no graphics card ({e.note})")
    yield e
    e.unload()


def test_a_streaming_model_types_words_before_the_pause(unified_engine, monkeypatch, tmp_path):
    audio = speech(tmp_path)
    screen = [""]
    s, raw, sent, stopped = run_session(unified_engine, audio, monkeypatch, screen=screen,
                                        stream_while_speaking=True)
    t0 = stopped - audio.size / SR
    print(f"\nscreen: {screen[0]!r}\nstreaming: {s.stream_seconds:.2f}s of model time, "
          f"re-reads: {s.preview_seconds:.2f}s")
    for t, x in sent:
        print(f"  {t - t0:6.2f}s  {x!r}")
    assert s._native, "fell back to re-reading"
    assert s.preview_seconds == 0                             # nothing was re-read
    assert s.stream_seconds < 0.5 * audio.size / SR           # well ahead of the voice
    # the same text as reading each phrase whole: the final reads decide it
    assert screen[0] == Cleaner.from_config(config.parse("")).clean(raw) + " "
    assert "deploy the core service first." in screen[0]
    # words went out before the first phrase ended (it ends about 4 s in)
    assert min(t - t0 for t, _ in sent) < 0.5 + 3.4
