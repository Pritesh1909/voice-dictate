"""The speech model in a child process that exists only while it is needed.

The tray process never imports transcribe.cpp, so it stays small. The model (~700 MB) lives in a child
process that is started when the hotkey is pressed and ended after a period of inactivity; ending the
process returns every byte to Windows, which unloading inside one long-lived process does not.

transcribe.cpp (the engine Orca uses) runs on the GPU through Vulkan when there is one. Measured on this
laptop with Parakeet v2, 16 s of speech: NVIDIA RTX 2050 0.3 s, CPU (i7-1260P) ~4.5 s; loading ~2 s on
the GPU, ~0.5 s on the CPU. The very first GPU run on a PC compiles shaders once (~20 s); the driver
caches them, so later processes don't pay it again.

If the engine process dies while loading or decoding on a graphics card (a driver crash takes the whole
process down, so no error message arrives), that is written to gpu-failed.json in the data folder, and
device = "auto" uses only the CPU from then on, retrying the failed request there once.

A model that can stream (Parakeet Unified EN, for example) can also be fed a phrase bit by bit while it is
spoken (stream()); it answers with the words it is sure of and the ones it may still change. Models that
can't (Parakeet v2 and v3) only ever read whole phrases.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import multiprocessing as mp
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


DEVICES = ("auto", "gpu", "igpu", "cpu")
GPU_MARKER = "gpu-failed.json"
LOAD_TIMEOUT = 60.0      # the first GPU run compiles shaders (~20 s), which fits
DECODE_TIMEOUT = 60.0
STREAM_TIMEOUT = 10.0    # one piece of a phrase (about 0.25 s of audio) takes ~0.05 s on a graphics card
# Parakeet Unified EN streams in steps of STREAM_CHUNK_MS, looking STREAM_RIGHT_MS ahead. Its own default
# (1 s steps, ~1.1 s ahead) settles words ~1.5 s after they are said; these settle them ~0.45 s after, and
# matched the whole-phrase read nearly as well (measured on six real dictations: 96.1% vs 96.9% of words).
# They take about twice the work: fine on a discrete card (8x faster than speech on an RTX 2050), too
# close to the voice on integrated graphics (Iris Xe: 21 s for 32 s), which keeps the default.
STREAM_CHUNK_MS = 560
STREAM_RIGHT_MS = 160


@dataclass(frozen=True)
class EngineSpec:
    model: str                          # path of the .gguf file
    threads: int                        # CPU threads (only used when running on the CPU)
    device: str = "auto"                # one of DEVICES
    vocabulary: tuple[str, ...] = ()    # boosted words, for models that support it
    vad: str = ""                       # path of the voice-activity detector .gguf ("" = none)
    language: str = "auto"              # [model] language: "auto" or an ISO-639-1 code
    gpu_marker: str = ""                # path of gpu-failed.json ("" = don't remember GPU crashes)
    prompt: str = ""                    # initial prompt (Whisper: the user's terms); "" = none (Parakeet takes none)

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


# --- remembering GPU crashes ----------------------------------------------------------------------

def read_gpu_marker(path: str) -> dict | None:
    """The recorded GPU crash, or None. A marker that exists but can't be read still counts."""
    if not path:
        return None
    try:
        d = json.loads(Path(path).read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}


def write_gpu_marker(path: str, device: str, during: str, exit_code) -> bool:
    try:
        Path(path).write_text(json.dumps({"device": device, "during": during, "exit_code": exit_code,
                                          "time": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1), "utf-8")
        return True
    except OSError as e:
        log.warning("could not record the GPU crash in %s: %s", path, e)
        return False


def choose_device(want: str, marker: dict | None) -> tuple[str, str]:
    """(device to load on, why it isn't `want`). A recorded GPU crash turns "auto" into the CPU;
    "gpu" and "igpu" chosen explicitly ignore it (that is how to try the card again)."""
    if want != "auto" or marker is None:
        return want, ""
    what = marker.get("device") or "the graphics card"
    when = f" on {marker['time']}" if marker.get("time") else ""
    return "cpu", (f"CPU only: {what} crashed the speech engine{when}; delete {GPU_MARKER} "
                   "or set [model] device to \"gpu\" to try it again")


def _on_gpu(device_type: str | None) -> bool:
    return device_type in ("gpu", "igpu")


# --- the child process ----------------------------------------------------------------------------

def _run_language(model, want: str) -> tuple[str | None, str]:
    """(language to pass to run(), a note when the setting is ignored)."""
    if want in ("", "auto"):
        return None, ""
    from .models import match_language
    try:
        langs = model.capabilities.languages
    except Exception:
        langs = ()
    got = match_language(want, langs)
    return got, "" if got else f"[model] language \"{want}\" ignored: this model can't be told the language"


def _load(spec: EngineSpec, report=lambda device_type, name: None):
    import transcribe_cpp as tc
    tc.set_log_callback(None)  # silence native info logging (nowhere to show it in a tray app)
    tried = []
    for dev in device_order(tc.backends(), spec.device):
        name = dev.description or dev.name
        report(dev.device_type, name)  # so the parent knows the device if this crashes the process
        try:
            model = tc.Model(spec.model, device=dev)
        except Exception as e:
            tried.append(f"{name}: {e}")
            continue
        threads = spec.threads if dev.device_type == "cpu" else 0
        session = model.session(n_threads=threads)
        vocab = list(spec.vocabulary) if spec.vocabulary and model.supports("vocabulary") else None
        language, lang_note = _run_language(model, spec.language)
        note = f"on {name}"
        if tried:
            note += f" (fell back after: {'; '.join(tried)})"
        return model, session, vocab, language, note, lang_note, (dev.device_type, name), _can_stream(model)
    raise RuntimeError("could not load the model on any device: " + "; ".join(tried))


def _can_stream(model) -> bool:
    try:
        return bool(model.capabilities.supports_streaming)
    except Exception:
        return False


def _stream_options(model, device_type: str | None):
    """The stream settings for this model on this device (None = the model's own defaults)."""
    if device_type != "gpu":
        return None
    try:
        import transcribe_cpp as tc
        o = tc.ParakeetBufferedStreamOptions(chunk_ms=STREAM_CHUNK_MS, right_ms=STREAM_RIGHT_MS)
        return o if model.accepts(o) else None
    except Exception:
        return None


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
        # `model` must stay referenced while the session is used
        model, session, vocab, language, note, lang_note, device, streams = _load(
            spec, lambda kind, name: conn.send(("loading", kind, name)))
        conn.send(("loading", "cpu", "voice detector"))
        vad_model, vad, vad_frame, vad_note = _load_vad(spec.vad)
        if vad_note:
            note += f"; {vad_note}"
        conn.send(("ready", time.perf_counter() - t, note, vad_frame, device, lang_note, streams))
    except Exception as e:
        conn.send(("error", f"{type(e).__name__}: {e}"))
        return
    extra = {"prompt": spec.prompt} if spec.prompt else {}
    stream = None          # the phrase being streamed, if any (one at a time; a decode ends it)
    stream_opts = _stream_options(model, device[0]) if streams else None

    def end_stream():
        nonlocal stream
        if stream is not None:
            try:
                stream.reset()
            except Exception:
                pass
            stream = None

    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return  # parent went away
        if msg[0] == "quit":
            end_stream()
            return
        if msg[0] == "stream":  # ("stream", samples, new) -> ("partial", committed, tentative, seconds)
            t = time.perf_counter()
            try:
                if msg[2]:
                    end_stream()
                    try:
                        stream = session.stream(vocabulary=vocab, language=language, family=stream_opts, **extra)
                    except Exception:
                        if stream_opts is None:
                            raise
                        log.warning("stream settings refused; using the model's own")
                        stream_opts = None
                        stream = session.stream(vocabulary=vocab, language=language, **extra)
                elif stream is None:
                    raise RuntimeError("no phrase is being streamed")
                stream.feed(msg[1])
                txt = stream.text()
                conn.send(("partial", txt.committed, txt.tentative, time.perf_counter() - t))
            except Exception as e:
                end_stream()
                conn.send(("error", f"{type(e).__name__}: {e}"))
            continue
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
            end_stream()  # the session reads one thing at a time
            t = time.perf_counter()
            try:
                r = session.run(msg[1], vocabulary=vocab, language=language, **extra)
                conn.send(("text", r.text, time.perf_counter() - t))
            except Exception as e:
                conn.send(("error", f"{type(e).__name__}: {e}"))


class EngineError(Exception):
    pass


STOPPED = "speech engine stopped"   # the error after kill()


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
        self.streams = False        # the loaded model can be fed a phrase bit by bit (stream())
        self.device: tuple[str, str] | None = None     # (device type, name) the model is loaded on
        self._loading: tuple[str, str] | None = None   # what the engine process is loading on right now
        self._why_cpu = ""          # why "auto" runs on the CPU this time (a recorded GPU crash)
        self._unsaved_crash: dict | None = None        # a GPU crash that could not be written to the marker
        self._closed = False        # kill() was called: never load again

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
            if self._closed:
                raise EngineError(STOPPED)
            if self._spec is None:
                raise EngineError("no speech model selected yet")
            if self._proc is not None and self._proc.is_alive():
                return
            self._start()
        self._on_change()

    def _start(self) -> None:  # with the lock held
        self._cleanup()
        spec = self._spec
        marker = read_gpu_marker(spec.gpu_marker)
        device, self._why_cpu = choose_device(spec.device, self._unsaved_crash if marker is None else marker)
        if self._why_cpu:
            log.info("speech engine: %s", self._why_cpu)
            spec = dataclasses.replace(spec, device=device)
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        self._proc = ctx.Process(target=_worker, args=(child, spec), daemon=True, name="parakeet-engine")
        self._proc.start()
        if self._closed:            # kill() came while this was starting and saw no process yet
            self._proc.terminate()
        child.close()
        self._conn = parent
        self._ready.clear()
        self._error = None
        self.device = self._loading = None
        self.streams = False
        self._gen += 1
        threading.Thread(target=self._await_ready, args=(parent, self._gen), daemon=True).start()

    def _await_ready(self, conn, gen: int) -> None:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                msg = ("stopped",)
            if msg[0] != "loading":
                break
            with self._lock:
                if gen == self._gen:
                    self._loading = (msg[1], msg[2])
        with self._lock:
            if gen != self._gen or self._conn is not conn:
                return  # unloaded or restarted meanwhile: an ordinary unload, never a crash
            if self._closed:
                self._error = STOPPED   # killed on purpose: not a crash, nothing to retry
            elif msg[0] == "ready":
                self.load_seconds, note, self.vad_frame, self.device, lang_note = msg[1:6]
                self.streams = bool(msg[6]) if len(msg) > 6 else False
                self.note = f"{note}; {self._why_cpu}" if self._why_cpu else note
                log.info("model loaded in %.1fs %s", self.load_seconds, self.note)
                if lang_note:
                    log.info("%s", lang_note)
            elif msg[0] == "stopped":
                if self._gpu_crashed(self._loading, "loading") and self._spec.device == "auto":
                    log.warning("loading the model again on the CPU")
                    self._start()  # transcribe() keeps waiting for this one
                    return
                self._error = "speech engine stopped while loading"
                log.error("model failed to load: %s", self._error)
            else:
                self._error = msg[1]
                log.error("model failed to load: %s", msg[1])
            self._ready.set()
        self._on_change()

    def _gpu_crashed(self, device: tuple[str, str] | None, during: str) -> bool:
        """The engine process went away unexpectedly (lock held). True, and recorded, if it was on a GPU."""
        proc = self._proc
        code = None
        if proc is not None:
            proc.join(1)
            code = proc.exitcode
        log.error("speech engine process ended while %s on %s (exit code %s)",
                  during, device[1] if device else "?", code)
        if code == 0 or device is None or not _on_gpu(device[0]):
            return False
        saved = bool(self._spec.gpu_marker) and write_gpu_marker(self._spec.gpu_marker, device[1], during, code)
        # Not written: remember it until the app restarts, so "auto" doesn't crash the card again.
        self._unsaved_crash = None if saved else {"device": device[1], "time": time.strftime("%Y-%m-%d %H:%M")}
        return True

    def transcribe(self, samples, _retry: bool = True) -> tuple[str, float]:
        if getattr(samples, "size", len(samples)) < 1600:  # < 0.1 s: the model can't take it, and it holds no words
            return "", 0.0
        self.warm()
        if not self._ready.wait(LOAD_TIMEOUT):
            self.unload()
            raise EngineError("The speech model took longer than a minute to load.")
        with self._lock:
            if self._closed:
                raise EngineError(STOPPED)
            if self._error:
                err = self._error
                self.unload()
                raise EngineError(err)
            conn = self._conn
            if conn is None:
                raise EngineError("speech engine stopped")
            try:
                conn.send(("decode", samples))
                if not conn.poll(DECODE_TIMEOUT):
                    log.error("transcription timed out; restarting the engine")
                    self.unload()   # a late reply would otherwise be read as the answer to the next request
                    raise EngineError("Transcription took longer than a minute.")
                msg = conn.recv()
            except (EOFError, OSError) as e:
                if self._closed:
                    raise EngineError(STOPPED) from None   # killed on purpose: not a GPU crash
                on_gpu = self._gpu_crashed(self.device, "decoding")
                self.unload()
                if not (on_gpu and _retry and self._spec is not None and self._spec.device == "auto"):
                    raise EngineError(f"speech engine stopped: {e}") from None
                msg = None
        if msg is None:
            log.warning("transcribing again on the CPU")
            return self.transcribe(samples, _retry=False)
        if msg[0] != "text":
            raise EngineError(msg[1])
        return msg[1], msg[2]

    @property
    def device_type(self) -> str | None:
        """"gpu", "igpu" or "cpu": the kind of device the model loaded on, once wait_ready() is True."""
        return self.device[0] if self.device else None

    @property
    def can_stream(self) -> bool:
        """Worth streaming: the loaded model can stream and runs on a graphics card. On the CPU streaming
        falls behind the voice (measured: 32 s of speech took 34 s on an i7-1260P), so phrases are read
        whole there, as with models that can't stream."""
        return self.streams and _on_gpu(self.device_type)

    def wait_ready(self, timeout: float = LOAD_TIMEOUT) -> bool:
        """Wait for the model (started by warm()). False if it failed, timed out or was unloaded."""
        return self._ready.wait(timeout) and not self._error and self._proc is not None and not self._closed

    def kill(self) -> None:
        """End the engine process now, from any thread, without waiting for a load or decode in progress
        (it takes no lock, so it never waits for transcribe()). A transcribe() or wait_ready() waiting on
        it returns at once (EngineError / False), and the engine stays closed: warm() and transcribe()
        raise EngineError from then on instead of loading the model again. Never recorded as a GPU crash.
        Safe to call more than once; unload() still tidies up afterwards. Used for the Whisper teacher,
        which the hotkey must get off the graphics card before dictation starts."""
        self._closed = True
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()   # the pipe breaks, which wakes a transcribe() waiting for its reply
            except (OSError, ValueError, AttributeError):
                pass               # already ended or closed
        self._ready.set()          # wakes a wait for the model to load

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

    def stream(self, samples, new: bool) -> tuple[str, str, float] | None:
        """Feed the next piece of the phrase being spoken (16 kHz); new = it starts a phrase. Returns
        (words it is sure of, words it may still change, seconds taken), each covering the phrase so far,
        or None when streaming failed (the caller then reads whole phrases). A transcribe() in between
        ends the phrase being streamed."""
        if not self.wait_ready() or not self.streams:
            return None
        with self._lock:
            conn = self._conn
            if conn is None:
                return None
            try:
                conn.send(("stream", samples, new))
                if not conn.poll(STREAM_TIMEOUT):
                    log.warning("streaming timed out; restarting the engine")
                    self.unload()   # a late reply would otherwise be read as the answer to the next request
                    return None
                msg = conn.recv()
            except (EOFError, OSError) as e:
                if not self._closed:
                    self._gpu_crashed(self.device, "streaming")
                log.warning("streaming: %s", e)
                self.unload()
                return None
        if msg[0] != "partial":
            log.warning("streaming failed: %s", msg[1])
            return None
        return msg[1], msg[2], msg[3]

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
