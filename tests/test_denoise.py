"""Background-noise clean-up (parakeet_dictate.denoise) and how preprocess uses it."""
import time

import numpy as np
import pytest

from parakeet_dictate import config, denoise, settings_ui
from parakeet_dictate.audio import preprocess

SR = 16000


def _bursts(seconds=5.0, f0=150.0, on=0.35, off=0.25, amp=0.2):
    """Speech-like: harmonic tones that come and go (a steady tone would look like noise)."""
    t = np.arange(int(seconds * SR)) / SR
    tone = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8))
    gate = (t % (on + off)) < on
    gate[: int(0.5 * SR)] = False                      # a quiet lead-in, as a phrase has
    return (amp * tone / np.max(np.abs(tone)) * gate).astype(np.float32)


def _noise(n, level, seed=0):
    return np.random.default_rng(seed).normal(0, level, n).astype(np.float32)


def _snr(clean, x):
    return 10 * np.log10(np.sum(clean ** 2) / np.sum((x - clean) ** 2))


def test_silence_stays_silent():
    assert not np.any(denoise.reduce_noise(np.zeros(SR * 2, np.float32)))
    quiet = _noise(SR * 2, 1e-4)
    out = denoise.reduce_noise(quiet)
    assert np.sqrt(np.mean(out ** 2)) <= np.sqrt(np.mean(quiet ** 2))


def test_tone_in_white_noise_gets_cleaner():
    s = _bursts()
    x = s + _noise(s.size, 0.05)
    before, after = _snr(s, x), _snr(s, denoise.reduce_noise(x))
    assert after > before + 6, (before, after)


def test_steady_hum_is_turned_down():
    s = _bursts()
    t = np.arange(s.size) / SR
    hum = (0.05 * np.sin(2 * np.pi * 50 * t)).astype(np.float32)
    assert _snr(s, denoise.reduce_noise(s + hum)) > _snr(s, s + hum) + 10


def test_speech_energy_kept():
    s = _bursts()
    out = denoise.reduce_noise(s)                       # nothing to remove: nearly unchanged
    assert 0.9 < np.sum(out ** 2) / np.sum(s ** 2) <= 1.01
    noisy = s + _noise(s.size, 0.03)
    out = denoise.reduce_noise(noisy)
    kept = float(np.dot(out, s) / np.dot(s, s))         # how much of the speech itself survives
    assert 0.7 < kept < 1.1, kept


def test_full_reconstruction_when_nothing_is_suppressed():
    x = _noise(12345, 0.1, seed=3)
    out = denoise.reduce_noise(x, floor_db=0.0, highpass_hz=0)
    assert np.max(np.abs(out - x)) < 1e-5


@pytest.mark.parametrize("n", [0, 1, 100, 2047, 4096, 33333])
def test_any_length_comes_back_same_length(n):
    out = denoise.reduce_noise(_noise(n, 0.1))
    assert out.shape == (n,) and out.dtype == np.float32 and np.all(np.isfinite(out))


def test_no_nans_on_odd_input():
    square = np.sign(np.sin(np.arange(SR * 2) / 7)).astype(np.float32)    # full scale
    for x in (square, _noise(SR * 2, 1e-9), np.full(SR * 2, 0.5, np.float32)):
        assert np.all(np.isfinite(denoise.reduce_noise(x)))
    bad = _noise(SR, 0.1)
    bad[100] = np.nan
    assert denoise.reduce_noise(bad).shape == bad.shape     # left alone rather than spreading the NaN


def test_auto_cleans_only_noisy_phrases():
    s = _bursts()
    assert not denoise.needs_cleaning(s, mode="auto")                       # digital silence between words
    assert not denoise.needs_cleaning(s + _noise(s.size, 2e-4), mode="auto")   # a quiet microphone
    assert denoise.needs_cleaning(s + _noise(s.size, 0.03), mode="auto")      # a fan close by
    assert denoise.needs_cleaning(s, mode="on")
    assert not denoise.needs_cleaning(s + _noise(s.size, 0.03), mode="off")
    assert denoise.snr_db(np.zeros(SR, np.float32)) == float("inf")


def test_fast_enough():
    x = _bursts() + _noise(5 * SR, 0.03)
    denoise.reduce_noise(x)
    times = []
    for _ in range(3):
        t = time.perf_counter()
        denoise.reduce_noise(x)
        times.append(time.perf_counter() - t)
    assert min(times) < 0.15, times          # ~15 ms on an i7-1260P; loose for slow CI machines


def test_preprocess_off_is_unchanged_and_on_cleans():
    s = _bursts()
    noisy = s + _noise(s.size, 0.03)
    assert np.array_equal(preprocess(noisy), preprocess(noisy, noise_reduction="off"))
    on = preprocess(noisy, noise_reduction="on")
    assert on.size and not np.array_equal(on, preprocess(noisy))
    assert np.array_equal(preprocess(s, noise_reduction="auto"), preprocess(s))   # clean: untouched
    assert preprocess(np.zeros(SR, np.float32), noise_reduction="on").size == 0


def test_setting_parsed_and_checked():
    assert config.parse("").noise_reduction in denoise.MODES
    assert config.parse('[accuracy]\nnoise_reduction = "on"').noise_reduction == "on"
    with pytest.raises(config.ConfigError):
        config.parse('[accuracy]\nnoise_reduction = "max"')


def test_settings_window_offers_the_choices():
    rows = [s for _, groups in settings_ui.PAGES for _, specs in groups for s in specs
            if s.key == ("accuracy", "noise_reduction")]
    assert len(rows) == 1 and rows[0].kind == "choice"
    assert tuple(v for v, _ in rows[0].choices) == denoise.MODES
