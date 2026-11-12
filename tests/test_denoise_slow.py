"""Noise clean-up with the real model: TTS speech + a synthetic fan/hum -> preprocess -> Parakeet v2.

Clean speech must read exactly as well with the clean-up as without; under a loud fan it must read better.
(The full benchmark with keyboard and babble noise at 20/10/5/0 dB is summarised in the README.)"""
import re

import numpy as np
import pytest

from parakeet_dictate.audio import preprocess

from test_engine_slow import _tts, engine  # noqa: F401  (the module-scoped model fixture)

pytestmark = pytest.mark.slow
SR = 16000
SENTENCES = [
    "Please deploy the core service before the CRM service on the staging cluster.",
    "We need to rotate the database password and update the secrets manager entry.",
    "Merge the feature branch into main after the code review is approved.",
    "The API returns a server error when the access token has expired.",
]


def _words(t):
    return re.sub(r"[^a-z0-9' ]+", " ", t.lower().replace("-", " ")).split()


def _errors(ref, hyp):
    r, h = _words(ref), _words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
    return d[len(h)]


def _fan(n, rng):
    """Pink noise plus 50 Hz mains hum with harmonics, unit power."""
    f = np.fft.rfftfreq(n, 1 / SR)
    pink = np.fft.irfft(np.fft.rfft(rng.normal(size=n)) / np.sqrt(np.maximum(f, 20.0)), n)
    t = np.arange(n) / SR
    hum = sum(a * np.sin(2 * np.pi * h * t) for a, h in ((1, 50), (.6, 100), (.4, 150)))
    x = pink / pink.std() + 0.5 * hum / hum.std()
    return (x / x.std()).astype(np.float32)


def _speech_power(s):
    fr = s[: s.size // 320 * 320].reshape(-1, 320)
    rms = np.sqrt(np.mean(fr ** 2, axis=1))
    return float(np.mean(fr[rms > 0.05 * rms.max()] ** 2))


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("noise")
    out = []
    for i, text in enumerate(SENTENCES):
        s = _tts(text, d / f"{i}.wav")
        out.append((text, np.concatenate([np.zeros(SR // 2, np.float32), s, np.zeros(int(0.8 * SR), np.float32)]),
                    _speech_power(s)))
    return out


def _wer(engine, items, mode):
    errs = words = 0
    for text, x in items:
        audio = preprocess(x, noise_reduction=mode)
        hyp = engine.transcribe(audio)[0] if audio.size else ""
        errs += _errors(text, hyp)
        words += len(_words(text))
    return errs / words


def test_clean_speech_reads_the_same(engine, clips):  # noqa: F811
    items = [(t, x) for t, x, _ in clips]
    off = _wer(engine, items, "off")
    assert _wer(engine, items, "auto") == off           # nothing to clean: left untouched
    assert _wer(engine, items, "on") <= off + 0.02      # even forced on, clean speech isn't harmed
    print(f"\nclean: WER off {off:.1%}")


@pytest.mark.parametrize("snr", [5, 0])
def test_loud_fan_reads_better(engine, clips, snr):  # noqa: F811
    rng = np.random.default_rng(snr)
    items = [(t, x + np.sqrt(p / 10 ** (snr / 10)) * _fan(x.size, rng)) for t, x, p in clips]
    off, auto = _wer(engine, items, "off"), _wer(engine, items, "auto")
    print(f"\nfan {snr} dB: WER off {off:.1%}, auto {auto:.1%}")
    assert auto < off and auto <= 0.1
