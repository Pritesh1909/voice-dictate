"""A dictation with a model that streams (Parakeet Unified EN): each phrase is fed to it while it is spoken
and the words it is sure of are typed before the pause; the final read of the phrase still decides the text.
Models that can't stream (or run on the CPU), and a stream that fails or falls behind, re-read phrases as
before. A fake microphone, voice detector and engine; nothing is typed into any window."""
import time

import numpy as np
import pytest

from parakeet_dictate import app as appmod
from parakeet_dictate.app import Session

from test_app_flow import SR, FakeEngine, make_app, out, speech  # noqa: F401

PIECE = SR // 4   # what the microphone hands over per pump (0.25 s)


class TrickleRecorder:
    """Hands out the audio a quarter second per drain, like a microphone."""

    def __init__(self, audio):
        self.audio, self.rate, self.pos = audio, SR, 0

    def drain(self):
        x = self.audio[self.pos:self.pos + PIECE]
        self.pos += x.size
        return x

    @property
    def done(self):
        return self.pos >= self.audio.size

    def cancel(self):
        pass


class StreamEngine(FakeEngine):
    """Final reads come from `finals`. A loud 512-sample frame scores 1 for the voice detector. stream()
    answers each phrase with that phrase's (sure, maybe) from `partials`."""

    vad_frame = 512

    def __init__(self, finals, partials, can_stream=True, secs=0.01, fail=False):
        super().__init__(*finals)
        self.partials, self.can_stream, self.secs, self.fail = partials, can_stream, secs, fail
        self.feeds, self.previews, self._phrase, self._left = [], 0, -1, np.zeros(0, np.float32)

    def vad(self, x, new_stream=False, flush=False):
        buf = np.concatenate([np.zeros(0, np.float32) if new_stream else self._left, x])
        n = buf.size // 512
        if flush and buf.size % 512:
            buf, n = np.concatenate([buf, np.zeros(512 - buf.size % 512, np.float32)]), n + 1
        frames = buf[:n * 512].reshape(n, 512)
        self._left = buf[n * 512:]
        return [1.0 if np.sqrt(np.mean(f ** 2)) > 0.01 else 0.0 for f in frames]

    def stream(self, samples, new):
        self.feeds.append((samples.size, new))
        if self.fail:
            return None
        self._phrase += new
        sure, maybe = self.partials[min(self._phrase, len(self.partials) - 1)]
        return sure, maybe, self.secs


@pytest.fixture
def fast_pump(monkeypatch):
    """The pump waits 0.25 s between drains; here a few milliseconds (the audio still comes 0.25 s at a time)."""
    real = time.sleep

    class Clock:
        def __getattr__(self, name):
            return getattr(time, name)

        @staticmethod
        def sleep(s):
            real(min(s, 0.005))

    monkeypatch.setattr(appmod, "time", Clock())


def run(engine, monkeypatch):
    app = make_app(engine, live=True)
    app.recorder = TrickleRecorder(speech())
    previews = []
    real_preview = Session._preview

    def preview(self, part, samples):
        previews.append(part)
        real_preview(self, part, samples)

    monkeypatch.setattr(Session, "_preview", preview)
    app._session += 1
    app.state = "recording"
    s = Session(app, app._session, use_vad=True)
    end = time.monotonic() + 10
    while not app.recorder.done and time.monotonic() < end:
        time.sleep(0.005)
    app.state = "busy"
    app._complete(s)
    return s, previews


def test_words_are_typed_before_the_pause_and_the_final_read_completes_them(out, fast_pump, monkeypatch):
    e = StreamEngine(["Deploy the core service first.", "Then check the token."],
                     [("Deploy the core", " service"), ("Then check", " the")])
    s, previews = run(e, monkeypatch)
    assert s._native and s.vad_mode
    assert e.feeds and e.feeds[0][1] is True                      # the first piece starts a phrase
    assert sum(new for _, new in e.feeds) == 2                    # one stream per phrase
    assert out.typed[0] == "Deploy the core"                      # typed while the phrase was spoken
    assert "".join(out.typed) == "Deploy the core service first. Then check the token. "
    assert previews == []                                          # no re-reading
    assert s.stream_seconds > 0


def test_models_that_cant_stream_and_the_cpu_re_read_as_before(out, fast_pump, monkeypatch):
    # can_stream is False for a model that can't stream and for one running on the CPU (test_engine.py)
    e = StreamEngine(["Deploy the core service first."] * 60, [("x", "y")], can_stream=False)
    s, previews = run(e, monkeypatch)
    assert s._native is False and e.feeds == []
    assert previews                                                # the open phrase was re-read
    assert "".join(out.typed) == "Deploy the core service first. " * 2


@pytest.mark.parametrize("trouble", ["fails", "too slow"])
def test_a_stream_that_fails_or_falls_behind_goes_back_to_re_reading(out, fast_pump, monkeypatch, trouble):
    # every read (final or re-read) hears the same sentence, so the order of reads doesn't matter
    e = StreamEngine(["Deploy the core service first."] * 60, [("Deploy the core", " service")],
                     fail=trouble == "fails", secs=5.0 if trouble == "too slow" else 0.01)
    s, previews = run(e, monkeypatch)
    assert s._native is False
    assert len(e.feeds) == 1                                       # stopped after the first piece
    assert previews                                                # and re-read the open phrase instead
    assert "".join(out.typed) == "Deploy the core service first. " * 2
