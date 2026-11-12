"""The speech model in a child process that exists only while it is needed.

The tray process never imports transcribe.cpp, so it stays small. The model (~700 MB) lives in a child
process that is started when the hotkey is pressed and ended after a period of inactivity; ending the
process returns every byte to Windows, which unloading inside one long-lived process does not.

transcribe.cpp (the engine Orca uses) runs on the GPU through Vulkan when there is one. Measured on this
laptop with Parakeet v2, 16 s of speech: NVIDIA RTX 2050 0.3 s, CPU (i7-1260P) ~4.5 s; loading ~2 s on
the GPU, ~0.5 s on the CPU. The very first GPU run on a PC compiles shaders once (~20 s); the driver
caches them, so later processes don't pay it again.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


DEVICES = ("auto", "gpu", "igpu", "cpu")


@dataclass(frozen=True)
class EngineSpec:
    model: str                          # path of the .gguf file
    threads: int                        # CPU threads (only used when running on the CPU)
    device: str = "auto"                # one of DEVICES
    vocabulary: tuple[str, ...] = ()    # boosted words, for models that support it
    vad: str = ""                       # path of the voice-activity detector .gguf ("" = none)

    @classmethod
    def for_model(cls, m, threads: int, **kw) -> "EngineSpec":
        return cls(str(m.path), threads, **kw)


def device_order(devices, want: str) -> list:
    """Devices to try, best first. auto = discrete GPU, then integrated GPU, then CPU. The CPU is
    always the last resort, so a broken GPU driver never stops dictation."""
    by = {k: [d for d in devices if d.device_type == k] for k in ("gpu", "igpu", "cpu")}
    order = {"auto": ("gpu", "igpu", "cpu"), "gpu": ("gpu", "cpu"), "igpu": ("igpu", "cpu"),
             "cpu": ("cpu",)}.get(want, ("gpu", "igpu", "cpu"))
    out = [d for k in order for d in by[k]]
    return out or list(devices)


def _load(spec: EngineSpec):
    import transcribe_cpp as tc
    tc.set_log_callback(None)  # silence native info logging (nowhere to show it in a tray app)
    tried = []
    for dev in device_order(tc.backends(), spec.device):
        try:
            model = tc.Model(spec.model, device=dev)
        except Exception as e:
            tried.append(f"{dev.description or dev.name}: {e}")
            continue
        threads = spec.threads if dev.device_type == "cpu" else 0
        session = model.session(n_threads=threads)
        vocab = list(spec.vocabulary) if spec.vocabulary and model.supports("vocabulary") else None
        note = f"on {dev.description or dev.name}"
        if tried:
            note += f" (fell back after: {'; '.join(tried)})"
        return model, session, vocab, note
    raise RuntimeError("could not load the model on any device: " + "; ".join(tried))


def _load_vad(path: str):
    """Silero VAD on the CPU (1 MB, loads in ~5 ms, scores 7 s of audio in ~30 ms). None if unusable."""
    if not path:
        return None, None, 0, ""
    import transcribe_cpp as tc
    try:
        vm = tc.Model(path, backend="cpu")
        info = vm.vad_info
        if info.sample_rate != 16000:
            return None, None, 0, f"voice detector needs {info.sample_rate} Hz"
        return vm, vm.vad_session(n_threads=1), info.frame_samples, ""
    except Exception as e:
        return None, None, 0, f"voice detector unavailable: {type(e).__name__}: {e}"


def _worker(conn, spec: EngineSpec) -> None:  # runs in the child process
    try:
        t = time.perf_counter()
        model, session, vocab, note = _load(spec)  # `model` must stay referenced while the session is used
        vad_model, vad, vad_frame, vad_note = _load_vad(spec.vad)
        if vad_note:
            note += f"; {vad_note}"
        conn.send(("ready", time.perf_counter() - t, note, vad_frame))
    except Exception as e:
        conn.send(("error", f"{type(e).__name__}: {e}"))
        return
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return  # parent went away
        if msg[0] == "quit":
            return
        if msg[0] == "vad":  # ("vad", samples, new_stream, flush) -> ("probs", scores of completed frames)
            try:
                if msg[2]:
                    vad.stream_reset()
                probs = list(vad.stream_feed(msg[1]).probs) if len(msg[1]) else []
                if msg[3]:
                    probs += vad.stream_flush().probs
                conn.send(("probs", probs))
            except Exception as e:
                conn.send(("error", f"{type(e).__name__}: {e}"))
            continue
        if msg[0] == "decode":
            t = time.perf_counter()
            try:
                r = session.run(msg[1], vocabulary=vocab)
                conn.send(("text", r.text, time.perf_counter() - t))
            except Exception as e:
                conn.send(("error", f"{type(e).__name__}: {e}"))


class EngineError(Exception):
    pass


class Engine:
    def __init__(self, spec: EngineSpec | None, unload_after: int, on_change=lambda: None):
        self._spec = spec
        self.unload_after = unload_after
        self._on_change = on_change
        self._lock = threading.RLock()
        self._proc = None
        self._conn = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._timer: threading.Timer | None = None
        self._gen = 0
        self._release_token = 0
        self.note = ""
        self.load_seconds = 0.0
        self.vad_frame = 0          # samples per voice-detector score; 0 = no detector loaded

    @property
    def state(self) -> str:
        with self._lock:
            if self._proc is None:
                return "unloaded"
            return "ready" if self._ready.is_set() and not self._error else "loading"

    def set_spec(self, spec: EngineSpec | None, unload_after: int) -> None:
        with self._lock:
            self.unload_after = unload_after
            if spec != self._spec:
                self._spec = spec
                self.unload()

    def warm(self) -> None:
        """Start loading in the background (called on hotkey press, so loading overlaps speaking)."""
        with self._lock:
            self._cancel_timer()
            self._release_token += 1  # a new dictation: any pending "free memory" no longer applies
            if self._spec is None:
                raise EngineError("no speech model selected yet")
            if self._proc is not None and self._proc.is_alive():
                return
            self._cleanup()
            ctx = mp.get_context("spawn")
            parent, child = ctx.Pipe()
            self._proc = ctx.Process(target=_worker, args=(child, self._spec), daemon=True,
                                     name="parakeet-engine")
            self._proc.start()
            child.close()
            self._conn = parent
            self._ready.clear()
            self._error = None
            self._gen += 1
            threading.Thread(target=self._await_ready, args=(parent, self._gen), daemon=True).start()
        self._on_change()

    def _await_ready(self, conn, gen: int) -> None:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            msg = ("error", "speech engine stopped while loading")
        with self._lock:
            if gen != self._gen or self._conn is not conn:
                return  # unloaded or restarted meanwhile
            if msg[0] == "ready":
                self.load_seconds, self.note, self.vad_frame = msg[1], msg[2], msg[3]
                log.info("model loaded in %.1fs %s", msg[1], msg[2])
            else:
                self._error = msg[1]
                log.error("model failed to load: %s", msg[1])
            self._ready.set()
        self._on_change()

    def transcribe(self, samples, timeout: float = 120.0) -> tuple[str, float]:
        if getattr(samples, "size", len(samples)) < 1600:  # < 0.1 s: the model can't take it, and it holds no words
            return "", 0.0
        self.warm()
        if not self._ready.wait(timeout):
            self.unload()
            raise EngineError("the model took too long to load")
        with self._lock:
            if self._error:
                err = self._error
                self.unload()
                raise EngineError(err)
            conn = self._conn
            try:
                conn.send(("decode", samples))
                if not conn.poll(timeout):
                    raise EngineError("transcription timed out")
                msg = conn.recv()
            except (EOFError, OSError) as e:
                self.unload()
                raise EngineError(f"speech engine stopped: {e}") from None
        if msg[0] != "text":
            raise EngineError(msg[1])
        return msg[1], msg[2]

    def wait_ready(self, timeout: float = 120.0) -> bool:
        """Wait for the model (started by warm()). False if it failed, timed out or was unloaded."""
        return self._ready.wait(timeout) and not self._error and self._proc is not None

    def vad(self, samples, new_stream: bool = False, flush: bool = False) -> list[float] | None:
        """Speech probabilities (one per vad_frame samples of 16 kHz audio) for the frames this audio
        completes. None when there is no detector or it failed; the caller then uses its own detector."""
        if not self.wait_ready() or not self.vad_frame:
            return None
        with self._lock:
            conn = self._conn
            if conn is None:
                return None
            try:
                conn.send(("vad", samples, new_stream, flush))
                if not conn.poll(30):
                    raise EngineError("voice detector timed out")
                msg = conn.recv()
            except (EOFError, OSError, EngineError) as e:
                log.warning("voice detector: %s; restarting the engine", e)
                self.unload()   # a late reply would otherwise be read as the answer to the next request
                return None
        if msg[0] != "probs":
            log.warning("voice detector failed: %s", msg[1])
            return None
        return msg[1]

    def done(self) -> None:
        """A dictation ended (typed, cancelled or nothing heard).

        unload_after == 0: free the model right away, i.e. memory is used only during a dictation.
        A decode still running (cancelled dictation) is allowed to finish first, and if a new dictation
        has started meanwhile the model is kept for it.
        unload_after > 0: free it after that many idle seconds. < 0: keep it loaded.
        """
        with self._lock:
            if self._proc is None:
                return
            if self.unload_after == 0:
                token = self._release_token
                threading.Thread(target=self._release_if_unused, args=(token,), daemon=True).start()
            else:
                self._schedule_unload()

    def _release_if_unused(self, token: int) -> None:
        with self._lock:  # waits for any decode in progress
            if token == self._release_token:
                self.unload()

    def _schedule_unload(self) -> None:
        self._cancel_timer()
        if self.unload_after > 0:
            self._timer = threading.Timer(self.unload_after, self.unload)
            self._timer.daemon = True
            self._timer.start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _cleanup(self) -> None:
        proc, conn = self._proc, self._conn
        self._proc = self._conn = None
        if conn is not None:
            try:
                conn.send(("quit",))
            except (OSError, ValueError):
                pass
        if proc is not None:
            proc.join(3)
            if proc.is_alive():
                proc.terminate()
                proc.join(2)
        if conn is not None:
            conn.close()

    def unload(self) -> None:
        with self._lock:
            self._cancel_timer()
            was_loaded = self._proc is not None
            self._cleanup()
            self._ready.clear()
            self._error = None
        if was_loaded:
            log.info("model unloaded")
            self._on_change()
