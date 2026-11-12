"""Microphone capture (only open while recording) and pre-processing for Parakeet."""
from __future__ import annotations

import threading
import time

import numpy as np

SR = 16000


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


def preprocess(x: np.ndarray, sr: int = SR, normalize: bool = True) -> np.ndarray:
    """Return speech ready for the model, or an empty array when there is no speech.

    DC offset removed, leading/trailing silence trimmed (with margin), quiet speech raised,
    then padded with silence so the transducer flushes the last word.
    """
    x = np.asarray(x, dtype=np.float32).ravel()
    if x.size < sr // 10:
        return np.zeros(0, np.float32)
    x = x - float(x.mean())
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


class Recorder:
    """Opens the microphone on start() and closes it on close(); nothing runs between dictations.

    Audio stays at the device rate; drain() hands over what arrived since the last call.
    `level` is the RMS of the latest 50 ms block, for the on-screen meter.
    """

    def __init__(self, device_hint: str = ""):
        self.device_hint = device_hint
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._rate = SR
        self._lock = threading.Lock()
        self.started_at = 0.0
        self.level = 0.0

    @property
    def rate(self) -> int:
        return self._rate

    def _device(self):
        if not self.device_hint:
            return None
        import sounddevice as sd
        hint = self.device_hint.lower()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and hint in d["name"].lower():
                return i
        raise RuntimeError(f"No microphone matching '{self.device_hint}' ([audio] input_device)")

    def _callback(self, indata, frames, t, status):
        block = indata[:, 0].copy()
        self.level = float(np.sqrt(np.mean(block * block))) if block.size else 0.0
        with self._lock:
            self._chunks.append(block)

    def start(self) -> None:
        import sounddevice as sd
        self._chunks = []
        self.level = 0.0
        dev = self._device()
        try:
            self._rate = SR
            self._stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=dev,
                                          blocksize=SR // 20, callback=self._callback)
        except Exception:
            self._rate = int(sd.query_devices(dev, "input")["default_samplerate"])
            self._stream = sd.InputStream(samplerate=self._rate, channels=1, dtype="float32", device=dev,
                                          blocksize=self._rate // 20, callback=self._callback)
        self._stream.start()
        self.started_at = time.monotonic()

    def drain(self) -> np.ndarray:
        """Audio (device rate) captured since the previous drain()."""
        with self._lock:
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, np.float32)

    def close(self) -> None:
        self._close()
        self.level = 0.0

    def _close(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
            finally:
                s.close()

    def cancel(self) -> None:
        self.close()
        with self._lock:
            self._chunks = []

    @property
    def active(self) -> bool:
        return self._stream is not None
