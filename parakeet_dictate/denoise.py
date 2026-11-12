"""Background-noise clean-up for one phrase before it goes to the speech model (numpy only).

A Wiener-style suppressor on a short-time spectrum (32 ms frames, 50 % overlap, square-root Hann
windows, so with all gains at 1 the audio comes back unchanged):

* the noise spectrum is estimated from the phrase itself: the power in each frequency band is averaged
  over ~80 ms, and a low percentile over the phrase is taken (speech comes and goes, steady noise does
  not), then scaled up for that percentile's bias;
* each band's gain follows the "decision-directed" a-priori SNR, which smooths it over time and keeps
  the musical-noise twitter down; it is then smoothed over neighbouring bands;
* the gain never drops below a floor (default -10 dB): neural recognisers do worse on over-cleaned,
  hollow-sounding audio than on a little left-over noise;
* bands under ~80 Hz (mains hum, desk rumble) are turned down to that floor too.

Only steady noise (fans, hum, air conditioning, distant traffic) is reduced. Clicks and other voices
change too fast for a noise estimate and are left as they are.
"""
from __future__ import annotations

import numpy as np

SR = 16000
MODES = ("off", "auto", "on")

# "auto" cleans a phrase only when its own noise floor is this close to its speech (dB) and is not
# digital silence. Chosen from the benchmark in the README: phrases mixed at 20 dB (measured here as ~25 dB)
# read as well or better uncleaned; at 5 and 0 dB (measured ~8-14 dB) cleaning was a large win.
AUTO_SNR_DB = 20.0
MIN_NOISE_RMS = 10 ** (-66 / 20)   # quieter background than this is never treated as noise

FLOOR_DB = -10.0      # lowest gain of any band (-15 and -20 dB read worse in the benchmark)
STRENGTH = 1.0        # over-subtraction: the noise estimate is multiplied by this (1.5 and 2 read worse)
HIGHPASS_HZ = 80.0
_DD_ALPHA = 0.96      # decision-directed smoothing of the a-priori SNR
_PERCENTILE = 10
_BIAS = 1.9           # mean / 10th percentile of power averaged over 5 frames (chi-square, 10 dof)
_AVG_FRAMES = 5


def _frame_size(sr: int) -> int:
    return int(2 ** round(np.log2(0.032 * sr)))     # 512 at 16 kHz


def levels(x: np.ndarray, sr: int = SR) -> tuple[float, float]:
    """(noise rms, speech rms) of a phrase: the quiet and the loud 20 ms frames (10th and 95th percentile)."""
    x = np.asarray(x, np.float32).ravel()
    frame = max(1, sr // 50)
    n = x.size // frame
    if n == 0:
        return 0.0, 0.0
    rms = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1))
    lo, hi = np.percentile(rms, (10, 95))
    return float(lo), float(hi)


def snr_db(x: np.ndarray, sr: int = SR) -> float:
    """How far the phrase's speech stands above its background, in dB (inf when there is no background)."""
    noise, speech = levels(x, sr)
    if noise <= 0:
        return float("inf")
    return 20 * float(np.log10(max(speech, 1e-12) / noise))


def needs_cleaning(x: np.ndarray, sr: int = SR, mode: str = "auto") -> bool:
    """Whether [accuracy] noise_reduction `mode` cleans this phrase."""
    if mode == "on":
        return True
    if mode != "auto":
        return False
    noise, speech = levels(x, sr)
    if noise < MIN_NOISE_RMS:
        return False
    return 20 * float(np.log10(max(speech, 1e-12) / noise)) < AUTO_SNR_DB


def _window(n: int) -> np.ndarray:
    # periodic Hann, square-rooted: analysis x synthesis = Hann, which sums to 1 at 50 % overlap
    return np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)).astype(np.float32)


def reduce_noise(x: np.ndarray, sr: int = SR, floor_db: float = FLOOR_DB, strength: float = STRENGTH,
                 highpass_hz: float = HIGHPASS_HZ) -> np.ndarray:
    """The phrase with steady background noise turned down. Same length and dtype float32."""
    x = np.asarray(x, np.float32).ravel()
    n = _frame_size(sr)
    hop = n // 2
    if x.size < 4 * n or not np.all(np.isfinite(x)):
        return x.copy()
    win = _window(n)
    pad_end = hop + (-x.size) % hop
    xp = np.concatenate([np.zeros(hop, np.float32), x, np.zeros(pad_end, np.float32)])
    frames = np.lib.stride_tricks.sliding_window_view(xp, n)[::hop]
    spec = np.fft.rfft(frames * win, axis=1)                       # (T, F)
    power = spec.real ** 2 + spec.imag ** 2
    t_count, bands = power.shape

    noise = _noise_spectrum(power) * strength
    floor = 10 ** (floor_db / 20)
    gains = _wiener(power, noise, floor)

    if highpass_hz > 0:
        f = np.arange(bands) * (sr / n)
        hp = np.clip((f - 0.5 * highpass_hz) / (0.75 * highpass_hz), 0.0, 1.0) ** 2
        gains *= np.maximum(hp, floor).astype(np.float32)

    out = np.fft.irfft(spec * gains, n=n, axis=1).astype(np.float32) * win
    acc = np.zeros((t_count + 1, hop), np.float32)
    acc[:t_count] += out[:, :hop]
    acc[1:] += out[:, hop:]
    return acc.ravel()[hop: hop + x.size].copy()


def _noise_spectrum(power: np.ndarray) -> np.ndarray:
    """Per band: a low percentile over time of the power averaged over a few frames, bias-corrected."""
    k = min(_AVG_FRAMES, power.shape[0])
    c = np.cumsum(power, axis=0, dtype=np.float64)
    avg = np.empty((power.shape[0] - k + 1, power.shape[1]))
    avg[0] = c[k - 1]
    avg[1:] = c[k:] - c[:-k]
    avg /= k
    noise = np.percentile(avg, _PERCENTILE, axis=0) * _BIAS
    noise = np.convolve(np.pad(noise, 1, mode="edge"), (0.25, 0.5, 0.25), mode="valid")
    return np.maximum(noise, 1e-12).astype(np.float32)


def _wiener(power: np.ndarray, noise: np.ndarray, floor: float) -> np.ndarray:
    """Decision-directed Wiener gains, floored, then smoothed over neighbouring bands."""
    post = power / noise                                          # a-posteriori SNR
    ml = np.maximum(post - 1.0, 0.0)
    gains = np.empty_like(power)
    prev = ml[0]
    a = _DD_ALPHA
    for t in range(power.shape[0]):
        xi = a * prev + (1 - a) * ml[t]                           # a-priori SNR
        g = xi / (1.0 + xi)
        gains[t] = g
        prev = g * g * post[t]
    np.maximum(gains, floor, out=gains)
    gains[:, 1:-1] = 0.25 * gains[:, :-2] + 0.5 * gains[:, 1:-1] + 0.25 * gains[:, 2:]
    return gains
