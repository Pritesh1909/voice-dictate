"""Microphone capture (only open while recording) and pre-processing for Parakeet."""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

SR = 16000
STALL_SECONDS = 2.0        # no audio for this long while recording = the microphone went away
FIRST_BLOCK_SECONDS = 5.0  # ...but allow longer for the first block (Bluetooth headsets switch profile)


def resample(x: np.ndarray, src: int, dst: int = SR) -> np.ndarray:
    if src == dst or x.size == 0:
        return x.astype(np.float32, copy=False)
    if src > dst:  # anti-alias low-pass before decimating (windowed sinc)
        fc = 0.45 * dst / src
        n = np.arange(129) - 64
        h = np.sinc(2 * fc * n) * np.hamming(129)
        x = np.convolve(x, h / h.sum(), mode="same")
    pos = np.arange(0, x.size, src / dst)
    return np.interp(pos, np.arange(x.size), x).astype(np.float32)


def preprocess(x: np.ndarray, sr: int = SR, normalize: bool = True, noise_reduction: str = "off") -> np.ndarray:
    """Return speech ready for the model, or an empty array when there is no speech.

    DC offset removed, steady background noise turned down ([accuracy] noise_reduction: "off", "auto" =
    only when this phrase is noisy, "on"), leading/trailing silence trimmed (with margin), quiet speech
    raised, then padded with silence so the transducer flushes the last word.
    """
    x = np.asarray(x, dtype=np.float32).ravel()
    if x.size < sr // 10:
        return np.zeros(0, np.float32)
    x = x - float(x.mean())
    if noise_reduction != "off":
        from . import denoise
        if denoise.needs_cleaning(x, sr, noise_reduction):
            x = denoise.reduce_noise(x, sr)
    frame = sr // 50  # 20 ms
    n = x.size // frame
    rms = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    floor = float(np.percentile(rms, 20))
    loud = np.flatnonzero(rms > max(0.004, floor * 3.0))
    if loud.size == 0 or (loud[-1] - loud[0] + 1) * frame < sr * 0.15:
        return np.zeros(0, np.float32)
    if normalize:
        margin = int(0.3 * sr)
        x = x[max(0, loud[0] * frame - margin): min(x.size, (loud[-1] + 1) * frame + margin)]
        peak = float(np.max(np.abs(x)))
        if 0 < peak < 0.5:
            x = x * min(0.9 / peak, 8.0)
    pad_start, pad_end = np.zeros(int(0.2 * sr), np.float32), np.zeros(int(0.6 * sr), np.float32)
    return np.concatenate([pad_start, x.astype(np.float32), pad_end])


class LevelMeter:
    """Mic level, speech and clipping from 50 ms blocks (ported from Orca 1.4.256's dictation meter).

    The noise floor follows the room slowly, so a fan or hum reads as silence rather than speech;
    `level` (0..1) rises fast and falls slowly so the bars don't flicker. Clipping stays on for half
    a second after a block that hits full scale, long enough to be seen. feed() runs on the audio
    thread, so it does a handful of float operations and nothing else.
    """

    FLOOR_START, FLOOR_MIN = 0.008, 0.004
    RANGE = 0.16                 # rms above the floor that fills the meter
    RISE, FALL = 0.58, 0.2
    SPEAKING_LEVEL, SPEAKING_PEAK = 0.1, 0.18
    CLIP_PEAK, CLIP_HOLD = 0.98, 0.5

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.level = 0.0
        self.peak = 0.0
        self.speaking = False
        self.noise_floor = self.FLOOR_START
        self.last_speech_at = 0.0      # monotonic seconds; 0 = no speech yet
        self._clipping_until = float("-inf")

    def feed(self, block: np.ndarray, now: float) -> None:
        if block.size:
            rms = float(np.sqrt(np.mean(block * block)))
            peak = min(1.0, float(np.max(np.abs(block))))
        else:
            rms = peak = 0.0
        floor = self.noise_floor
        floor = max(self.FLOOR_MIN, floor * 0.96 + min(rms, floor * 2) * 0.04)
        raw = min(1.0, max(0.0, (rms - floor) / self.RANGE))
        k = self.RISE if raw > self.level else self.FALL
        self.noise_floor = floor
        self.level = self.level + (raw - self.level) * k
        self.peak = peak
        if peak >= self.CLIP_PEAK:
            self._clipping_until = now + self.CLIP_HOLD
        self.speaking = self.level >= self.SPEAKING_LEVEL or peak >= self.SPEAKING_PEAK
        if self.speaking:
            self.last_speech_at = now

    def clipping_at(self, now: float) -> bool:
        """Checked against the clock (not the last block) so it clears even if audio stops."""
        return now <= self._clipping_until


def silence_stop_due(last_speech_at: float, started_at: float, now: float, limit: float) -> bool:
    """True once `limit` seconds pass with no speech (counted from the start if none was heard yet).

    Timed on the clock, not on audio, so a stream that stops delivering still counts as silence.
    limit <= 0 turns it off."""
    return limit > 0 and now - max(started_at, last_speech_at) >= limit


def silence_stop_message(seconds: int) -> str:
    if seconds % 60 == 0:
        mins = seconds // 60
        span = "a minute" if mins == 1 else f"{mins} minutes"
    else:
        span = f"{seconds} seconds"
    return f"Stopped after {span} of silence."


class Recorder:
    """Opens the microphone on start() and closes it on close(); nothing runs between dictations.

    Audio stays at the device rate; drain() hands over what arrived since the last call.
    `meter` follows the latest 50 ms blocks for the on-screen meter, clipping and silence stop.
    `warning` is set by start() when the chosen microphone wasn't found and the default was used;
    `lost` turns True when the microphone goes away mid-recording (unplugged, driver reset).
    """

    def __init__(self, device_hint: str = "", clock=time.monotonic):
        self.device_hint = device_hint
        self._clock = clock
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._rate = SR
        self._lock = threading.Lock()
        self.started_at = 0.0
        self.meter = LevelMeter()
        self.warning = ""
        self._last_block_at = 0.0
        self._got_block = False
        self._ended = False         # PortAudio stopped the stream by itself

    @property
    def rate(self) -> int:
        return self._rate

    @property
    def level(self) -> float:
        """Meter level 0..1 (noise floor removed, smoothed)."""
        return self.meter.level

    @property
    def clipping(self) -> bool:
        return self.meter.clipping_at(self._clock())

    def _device(self):
        """The device index matching [audio] input_device, or None for the Windows default.

        A missing device (headset unplugged, renamed) falls back to the default instead of failing the
        dictation, and leaves a `warning` for the app to show."""
        if not self.device_hint:
            return None
        import sounddevice as sd
        hint = self.device_hint.lower()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and hint in d["name"].lower():
                return i
        self.warning = f"Microphone '{self.device_hint}' not found. Using the Windows default."
        log.warning("no microphone matching %r ([audio] input_device); using the default", self.device_hint)
        return None

    def _callback(self, indata, frames, t, status):
        block = indata[:, 0].copy()
        now = self._clock()
        self._last_block_at = now
        self._got_block = True
        self.meter.feed(block, now)
        with self._lock:
            self._chunks.append(block)

    def _finished(self) -> None:
        """PortAudio calls this when the stream stops, including when the device disappears."""
        self._ended = True

    def _open(self, sd, rate: int, dev):
        return sd.InputStream(samplerate=rate, channels=1, dtype="float32", device=dev, blocksize=rate // 20,
                              callback=self._callback, finished_callback=self._finished)

    def start(self) -> None:
        import sounddevice as sd
        self._chunks = []
        self.meter.reset()
        self.warning = ""
        self._ended = self._got_block = False
        dev = self._device()
        try:
            self._rate = SR
            self._stream = self._open(sd, SR, dev)
        except Exception:
            self._rate = int(sd.query_devices(dev, "input")["default_samplerate"])
            self._stream = self._open(sd, self._rate, dev)
        self._stream.start()
        self.started_at = self._last_block_at = self._clock()

    @property
    def lost(self) -> bool:
        """The open stream has died: PortAudio ended it, it reports inactive, or no audio for 2 s
        (5 s before the first block).

        Overflow flags in the callback are not counted: they mean a busy PC dropped a block, not that
        the microphone is gone."""
        s = self._stream
        if s is None:
            return False
        if self._ended:
            return True
        try:
            if not s.active:
                return True
        except Exception:           # PortAudio error on a dead device
            return True
        limit = STALL_SECONDS if self._got_block else FIRST_BLOCK_SECONDS
        return self._clock() - self._last_block_at > limit

    def drain(self) -> np.ndarray:
        """Audio (device rate) captured since the previous drain()."""
        with self._lock:
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, np.float32)

    def close(self) -> None:
        self._close()
        self.meter.reset()

    def _close(self) -> None:
        """Never raises: stopping a stream whose device was unplugged can fail, and the app must still
        leave the "recording" state."""
        s, self._stream = self._stream, None
        if s is None:
            return
        try:
            s.stop()
        except Exception as e:
            log.warning("could not stop the microphone stream: %s", e)
        try:
            s.close()
        except Exception as e:
            log.warning("could not close the microphone stream: %s", e)

    def cancel(self) -> None:
        self.close()
        with self._lock:
            self._chunks = []

    @property
    def active(self) -> bool:
        return self._stream is not None
