"""Split live audio at natural pauses so finished parts can be transcribed while the user keeps talking.

Parakeet v2 is an offline (whole-utterance) model, so instead of streaming we cut the recording at
pauses of at least `pause_seconds` after at least `min_speech_seconds` of speech. Each cut lands in the
middle of the pause, so no word is split. Whatever is left when recording stops is the final segment.
"""
from __future__ import annotations

import numpy as np


class Segmenter:
    def __init__(self, rate: int, pause_seconds: float = 0.8, min_speech_seconds: float = 2.0):
        self.rate = rate
        self.frame = max(1, rate // 50)                       # 20 ms analysis frames
        self.pause_frames = max(1, int(pause_seconds * 50))
        self.min_speech_frames = max(1, int(min_speech_seconds * 50))
        self._pending: list[np.ndarray] = []
        self._floor: float | None = None

    def _threshold(self, rms: np.ndarray) -> float:
        if rms.size:
            f = float(np.percentile(rms, 20))
            self._floor = f if self._floor is None else min(self._floor, max(f, 1e-4))
        return max(0.004, (self._floor or 0.0) * 3.0)

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        """Add audio; return any segments that are complete (ended by a long enough pause)."""
        if samples.size:
            self._pending.append(np.asarray(samples, np.float32))
        out: list[np.ndarray] = []
        if not self._pending:
            return out
        x = np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
        while True:
            n = x.size // self.frame
            if n < self.min_speech_frames + self.pause_frames:
                break
            rms = np.sqrt(np.mean(x[: n * self.frame].reshape(n, self.frame) ** 2, axis=1) + 1e-12)
            loud = rms > self._threshold(rms)
            cut = self._find_cut(loud)
            if cut is None:
                break
            out.append(x[: cut * self.frame])
            x = x[cut * self.frame:]
        self._pending = [x] if x.size else []
        return out

    def _find_cut(self, loud: np.ndarray) -> int | None:
        speech = 0
        run = 0
        for i, is_loud in enumerate(loud):
            if is_loud:
                speech += 1
                run = 0
            else:
                run += 1
                if run >= self.pause_frames and speech >= self.min_speech_frames:
                    return i - run // 2          # middle of the pause
        return None

    def finish(self) -> np.ndarray:
        """Everything not yet emitted (the final segment)."""
        x = np.concatenate(self._pending) if self._pending else np.zeros(0, np.float32)
        self._pending = []
        return x


class PhraseChunker:
    """Collects one phrase; a phrase longer than 30 s is cut at its quietest 0.1 s in the last 5 s,
    so the model never gets more audio than it handles well."""

    def __init__(self, rate: int, limit_seconds: float = 30.0):
        self.limit = max(1, round(limit_seconds * rate))
        self.search = round(5 * rate)
        self.window = max(1, round(0.1 * rate))
        self._parts: list[np.ndarray] = []
        self.held = 0

    def push(self, x: np.ndarray) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        if not x.size:
            return out
        self._parts.append(x)
        self.held += x.size
        while self.held >= self.limit:
            buf = self._combined()
            cut = self._quiet_split(buf)
            out.append(buf[:cut])
            rest = buf[cut:]
            self._parts, self.held = ([rest] if rest.size else []), rest.size
        return out

    def flush(self) -> np.ndarray | None:
        if not self.held:
            return None
        buf = self._combined()
        self._parts, self.held = [], 0
        return buf

    def _combined(self) -> np.ndarray:
        return self._parts[0] if len(self._parts) == 1 else np.concatenate(self._parts)

    def _quiet_split(self, x: np.ndarray) -> int:
        end = min(self.limit, x.size)
        n, step = self.window, max(1, self.window // 2)
        best, at = np.inf, end
        for s in range(max(0, end - self.search), end - n + 1, step):
            e = float(np.dot(x[s:s + n], x[s:s + n]))
            if e < best:
                best, at = e, s + n // 2
        return max(1, at)


class VadSegmenter:
    """Cuts live 16 kHz audio into phrases using a voice detector's per-frame speech probabilities.

    Ported from Orca 1.4.256 (main/stt-worker.js). A phrase starts at a frame scoring >= 0.5 and ends
    after `pause_seconds` of frames scoring < 0.35, once it holds at least 0.25 s of speech. The 0.4 s
    before a phrase starts is kept so its first word is not clipped; other silence is dropped.
    The detector itself runs in the engine process; this class only routes audio by its scores.
    """

    START, STOP = 0.5, 0.35

    def __init__(self, frame_samples: int, rate: int = 16000, pause_seconds: float = 0.6,
                 preroll_seconds: float = 0.4, min_speech_seconds: float = 0.25):
        self.frame = frame_samples
        self.pause_frames = max(1, int(np.ceil(pause_seconds * rate / frame_samples)))
        self.min_speech_frames = max(1, int(np.ceil(min_speech_seconds * rate / frame_samples)))
        self.preroll_samples = round(preroll_seconds * rate)
        self.phrase = PhraseChunker(rate)
        self._unscored = np.zeros(0, np.float32)
        self._preroll: list[np.ndarray] = []
        self._preroll_len = 0
        self.in_speech = False
        self._speech = 0
        self._silent = 0

    @property
    def phrase_open(self) -> bool:
        """Speech has been heard that is not in a finished phrase yet."""
        return self.in_speech and self._speech >= self.min_speech_frames

    def push(self, x: np.ndarray, probs) -> list[np.ndarray]:
        """Add audio and the scores of the frames the detector completed (it scores whole frames only,
        so a partial frame's score comes with a later call). Returns finished phrases."""
        self._unscored = np.concatenate([self._unscored, np.asarray(x, np.float32)])
        return self._route(probs)

    def flush(self, probs=()) -> list[np.ndarray]:
        """End of recording: route the last scores, then return the open phrase (if speech)."""
        out = self._route(probs, final=True)
        last = self.phrase.flush() if self.in_speech else None
        self.in_speech, self._preroll, self._preroll_len = False, [], 0
        return out + ([last] if last is not None else [])

    def flush_with(self, x: np.ndarray, probs) -> list[np.ndarray]:
        """The last audio and the scores for it (including the zero-padded partial frame), then flush."""
        self._unscored = np.concatenate([self._unscored, np.asarray(x, np.float32)])
        return self.flush(probs)

    def held_audio(self) -> np.ndarray:
        """Audio not yet in a finished phrase, for handing over to another segmenter."""
        parts = ([self.phrase.flush()] if self.in_speech else self._preroll) + [self._unscored]
        parts = [p for p in parts if p is not None and p.size]
        self._preroll, self._preroll_len, self._unscored = [], 0, np.zeros(0, np.float32)
        self.in_speech = False
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)

    def _route(self, probs, final: bool = False) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        pos = 0
        for p in probs:
            frame = self._unscored[pos:pos + self.frame]
            pos += frame.size
            if not self.in_speech and p >= self.START:
                self.in_speech, self._speech, self._silent = True, 0, 0
                for chunk in self._preroll:
                    out += self.phrase.push(chunk)
                self._preroll, self._preroll_len = [], 0
            if not self.in_speech:
                self._hold_preroll(frame)
                continue
            out += self.phrase.push(frame)
            self._speech += p >= self.START
            self._silent = self._silent + 1 if p < self.STOP else 0
            if self._silent >= self.pause_frames and self._speech >= self.min_speech_frames:
                self.in_speech = False
                done = self.phrase.flush()
                if done is not None:
                    out.append(done)
        self._unscored = np.zeros(0, np.float32) if final else self._unscored[pos:].copy()
        return out

    def _hold_preroll(self, frame: np.ndarray) -> None:
        if not frame.size:
            return
        self._preroll.append(frame.copy())
        self._preroll_len += frame.size
        while len(self._preroll) > 1 and self._preroll_len - self._preroll[0].size >= self.preroll_samples:
            self._preroll_len -= self._preroll.pop(0).size
