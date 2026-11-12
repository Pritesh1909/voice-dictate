"""Ties hotkey -> microphone -> Parakeet -> cleanup -> typing together behind a tray icon."""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np
import pystray

from . import APP_NAME, __version__
from . import config as cfgmod
from . import commands, context, correct, learn, models, output, winutil
from .appsettings import settings_for
from .audio import SR, Recorder, preprocess, resample, silence_stop_due, silence_stop_message
from .engine import GPU_MARKER, Engine, EngineError, EngineSpec
from .hotkey import HotkeyError, HotkeyThread, key_down
from .hotkey import display as hotkey_display
from .overlay import GREEN, MUTED, RED, Overlay
from .segmenter import Segmenter, VadSegmenter
from .textclean import Cleaner
from .tray import icon_image, write_ico

log = logging.getLogger(__name__)

MIC_LOST = "Microphone disconnected. Dictation stopped."

# Live streaming: re-read the phrase being spoken at most this often (slower on the CPU, see
# Session._preview), once it holds this much audio.
PREVIEW_EVERY = 0.5
PREVIEW_MIN_SECONDS = 0.6

FAILED_NOTE = "Couldn't transcribe. The recording was kept: tray menu → Retry last recording."
DOUBLED_NOTE = ("Retry couldn't tell which words were already typed, so it inserted them all. "
                "Some words may now be doubled.")


@dataclass(frozen=True, eq=False)
class FailedRecording:
    """A dictation whose recognition failed. Held in memory only, never written to disk: dictation can be
    sensitive. partly_typed: live typing had already typed some of it before the failure."""
    audio: np.ndarray
    rate: int
    partly_typed: bool


class Session:
    """One dictation. While recording, a pump thread pulls audio from the microphone every 0.25 s and
    (with process_while_speaking) hands each finished phrase to a worker that transcribes it in the
    background, in order. With type_while_speaking each phrase is typed as soon as it is transcribed.
    finish() adds the last phrase and returns the joined raw text.

    Phrases are found by the Silero voice detector, which runs in the engine process next to the model.
    Without one (not found, "off", or it fails) the loudness-based Segmenter does the job instead."""

    def __init__(self, app: "App", sid: int, use_vad: bool):
        self.app, self.id = app, sid
        self.rate = app.recorder.rate
        self.parallel = app.cfg.process_while_speaking
        self.pause = app.cfg.pause_seconds
        self.vad_mode = self.parallel and use_vad
        self.vad_seg: VadSegmenter | None = None
        self._vad_new = True
        self.segmenter = Segmenter(self.rate, self.pause)
        self.seg_rate = self.rate
        self.normalize = app.cfg.normalize_audio
        self.noise = app.cfg.noise_reduction
        self.out = settings_for(app.target_exe, app.cfg)    # [output] with that program's [apps] overrides
        self.cleaner = app.cleaner_for(app.target_exe)   # the program dictated into: its terms come first
        if app.target_terms:                              # context words: lowest rank, this dictation only
            self.cleaner = self.cleaner.with_extra(app.target_terms)
        self.matcher = commands.CommandMatcher(app.cfg.commands, self.out.commands)
        self.hwnd = app.target_hwnd                       # "scratch that" only backspaces in this window
        live = self.parallel and self.out.type_while_speaking
        # Live streaming: re-read the phrase being spoken and type its words before the pause.
        self.stream = (live and self.vad_mode and app.cfg.stream_while_speaking and self.out.method == "type")
        self.typer = (output.PhraseTyper(self.cleaner.clean, self.out.method, self.out.add_trailing_space,
                                         matcher=self.matcher, can_backspace=self._same_window,
                                         on_stop=self._voice_stop, stream=self.stream,
                                         may_continue=self.cleaner.may_continue)
                      if live else None)
        self._preview_pending = False
        self._preview_at = 0.0           # when the last provisional read was asked for
        self._preview_gap = PREVIEW_EVERY
        self._preview_part = -1          # which phrase it was for, and how much audio it had
        self._preview_len = 0
        self.preview_seconds = 0.0
        self.total_samples = 0
        self.parts = 0
        self.decode_seconds = 0.0
        self._busy = 0               # phrases queued or being transcribed
        self._buffer: list = []
        self._results: dict[int, str] = {}
        self._error: Exception | None = None
        self._cancelled = False
        self._q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._work, daemon=True, name=f"stt-{sid}")
        self._worker.start()
        self._pump_thread = threading.Thread(target=self._pump, daemon=True, name=f"pump-{sid}")
        self._pump_thread.start()

    def _same_window(self) -> bool:
        """Backspace is only safe when typing (never when pasting) into the window the dictation began in."""
        return self.out.method == "type" and bool(self.hwnd) and winutil.foreground_window() == self.hwnd

    def _voice_stop(self) -> None:
        self.app.stop_from_voice(self.id)

    @property
    def writing(self) -> bool:
        """Speech was heard whose text hasn't been typed yet (for the overlay)."""
        return self._busy > 0 or bool(self.typer and self.typer.pending)

    def _recording(self) -> bool:
        return self.app._session == self.id and self.app.state == "recording" and not self._cancelled

    def _take(self, final: bool = False) -> None:
        x = self.app.recorder.drain()
        self.total_samples += x.size
        if x.size:
            self._buffer.append(x)  # the whole recording: kept if recognition fails
        if not self.parallel:
            return
        if self.vad_mode:
            x16 = resample(x, self.rate, SR)
            probs = self.app.engine.vad(x16, new_stream=self._vad_new, flush=final)
            if probs is not None:
                self._vad_new = False
                if self.vad_seg is None:
                    self.vad_seg = VadSegmenter(self.app.engine.vad_frame, SR, self.pause)
                parts = self.vad_seg.push(x16, probs) if not final else self.vad_seg.flush_with(x16, probs)
                for part in parts:
                    self._enqueue(part, SR)
                if self.stream and not final:
                    self._maybe_preview()
                return
            self._fall_back(x16)
        else:
            for part in self.segmenter.feed(x):
                self._enqueue(part, self.seg_rate)
        if final:
            self._enqueue(self.segmenter.finish(), self.seg_rate)

    def _fall_back(self, x16) -> None:
        """The voice detector isn't available: carry on with the loudness detector (on 16 kHz audio)."""
        import numpy as np
        log.warning("voice detector not available; using the loudness detector for this dictation")
        held = self.vad_seg.held_audio() if self.vad_seg else np.zeros(0, np.float32)
        self.vad_mode, self.vad_seg = False, None
        self.segmenter, self.seg_rate = Segmenter(SR, self.pause), SR
        for part in self.segmenter.feed(np.concatenate([held, x16])):
            self._enqueue(part, SR)

    def _pump(self) -> None:
        while self._recording():
            time.sleep(0.25)
            try:
                self._take()
                if self.typer:
                    self.typer.pump()   # phrases held back while Ctrl was down go out once it's up
            except Exception as e:      # reported by finish(); stop typing so nothing goes out of order
                log.exception("while recording")
                self._error = self._error or e
                if self.typer:
                    self.typer.cancel()
                return

    def _enqueue(self, samples, rate: int) -> None:
        self._busy += 1
        self._q.put((self.parts, samples, rate))
        self.parts += 1

    def _maybe_preview(self) -> None:
        """Ask for a provisional read of the phrase being spoken, when the worker has nothing else to do,
        at most every _preview_gap seconds, and only when the phrase grew by a quarter second or more."""
        seg = self.vad_seg
        if (seg is None or not seg.phrase_open or self._busy or self._preview_pending
                or time.monotonic() - self._preview_at < self._preview_gap):
            return
        audio = seg.open_audio()
        grown = audio.size - (self._preview_len if self._preview_part == self.parts else 0)
        if audio.size < PREVIEW_MIN_SECONDS * SR or grown < 0.25 * SR:
            return
        self._preview_pending, self._preview_at = True, time.monotonic()
        self._preview_part, self._preview_len = self.parts, audio.size
        self._q.put(("preview", self.parts, audio))

    def _preview(self, part: int, samples) -> None:
        """Worker: read the open phrase provisionally, unless it has ended meanwhile (its final read is
        queued, and that one counts) or other work is waiting."""
        try:
            if part != self.parts or not self._q.empty() or self._cancelled or self._error:
                return
            audio = preprocess(samples, normalize=self.normalize, noise_reduction=self.noise)
            if audio.size == 0:
                return
            raw, secs = self.app.engine.transcribe(audio)
            self.preview_seconds += secs
            # Slower reads (on the CPU) are asked for less often, so final reads never queue up behind them.
            self._preview_gap = max(PREVIEW_EVERY, 2.0 * secs)
            if part == self.parts and raw.strip() and self.typer:
                self.typer.preview(raw.strip())
        except Exception as e:       # a failed provisional read is skipped; the final read still runs
            log.warning("provisional read failed: %s", e)
        finally:
            self._preview_pending = False

    @property
    def tentative(self) -> str:
        """Words heard in the phrase being spoken that aren't typed yet (for the overlay)."""
        return self.typer.tentative if self.typer and self.stream else ""

    def _work(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            if item[0] == "preview":
                self._preview(item[1], item[2])
                continue
            try:
                if self._cancelled or self._error:
                    continue
                idx, samples, rate = item
                audio = preprocess(resample(samples, rate, SR), normalize=self.normalize,
                                   noise_reduction=self.noise)
                if audio.size == 0:
                    self._results[idx] = ""
                    if self.typer:
                        self.typer.add("")            # ends any words streamed for this phrase
                    continue
                raw, secs = self.app.engine.transcribe(audio)
                self.decode_seconds += secs
                self._results[idx] = raw.strip()
                if self.typer and not self._cancelled and (raw.strip() or self.stream):
                    self.typer.add(raw.strip())       # a spoken "stop listening" calls _voice_stop
                    self.typer.pump()
                elif (self.parallel and self.matcher.enabled and raw.strip()
                      and self.matcher.match(self.cleaner.clean(raw)) == commands.STOP):
                    self._voice_stop()
            except Exception as e:  # reported by finish()
                self._error = self._error or e
                if self.typer:
                    self.typer.cancel()
            finally:
                self._busy -= 1

    def finish(self) -> str:
        self._pump_thread.join()
        if self._error is None:
            self._take(final=True)  # whatever arrived after the last pump, and the last phrase
        if not self.parallel:
            import numpy as np
            self._enqueue(np.concatenate(self._buffer) if self._buffer else np.zeros(0, np.float32), self.rate)
        self._q.put(None)
        self._worker.join()
        if self._error:
            raise self._error
        if self.typer:
            self.typer.pump(wait=True)
        return " ".join(t for _, t in sorted(self._results.items()) if t)

    def phrases(self) -> list[str]:
        """The raw text of each phrase, in order (after finish())."""
        return [t for _, t in sorted(self._results.items()) if t]

    def text(self, raw: str) -> str:
        """Whole-dictation output: cleaned, with any spoken commands carried out (nothing typed yet)."""
        if self.matcher.enabled:
            cleaned = [self.cleaner.clean(p) for p in self.phrases()]
            if any(self.matcher.match(c) for c in cleaned):
                return commands.compose(cleaned, self.matcher)[0]
        return self.cleaner.clean(raw)

    def recording(self) -> np.ndarray:
        """Everything recorded so far, at the microphone's rate."""
        return np.concatenate(self._buffer) if self._buffer else np.zeros(0, np.float32)

    def cancel(self) -> None:
        self._cancelled = True
        if self.typer:
            self.typer.cancel()
        self._q.put(None)


class App:
    def __init__(self, paths: cfgmod.Paths):
        self.paths = paths
        self.cfg = cfgmod.load(paths)
        self.sounds = winutil.Sounds(paths.data_dir)
        self.ico = write_ico(paths.data_dir / "icon.ico")
        self.learned: list[tuple[str, str]] = []   # learned.toml ("Correct last dictation…")
        self._learned_stamp: int | None = None
        self.cleaner = self._make_cleaner(self.cfg)
        self._check_learned()
        self.context = context.ContextScanner(paths.context_cache, *self._context_settings(self.cfg))
        self._vocab = self._vocabulary(self.cfg)      # notes' terms boosted in models that support it
        self.recorder = Recorder(self.cfg.input_device)
        self.target_exe = ""         # the program in front when the current dictation started
        self.target_hwnd = 0         # ... and its window, for "scratch that"
        self.target_terms: list[tuple[str, str]] = []   # context words for the current dictation
        self.icon: pystray.Icon | None = None
        self.problem: str | None = None
        self.model: models.Model | None = None
        self.found: list[models.Model] = []   # every model found, for the tray "Model" menu
        self.model_job = ""          # "searching" / "copying 40%" / "downloading 12%"
        self.cfg_model_dir = self.cfg.model_dir
        self.cfg_model_folders = self.cfg.model_folders
        self.engine = Engine(None, self.cfg.unload_after_seconds, on_change=self._refresh)
        self.vad_path = self._find_vad(self.cfg)
        self._resolve_model(self.cfg)
        self.engine.set_spec(self._engine_spec(self.cfg), self.cfg.unload_after_seconds)
        self.hotkeys = HotkeyThread(self.cfg.hotkey, self._on_hotkey, self._on_cancel, self._on_extra_hotkey)
        self.state = "idle"          # idle | recording | busy
        self.last_text = ""
        self.last_stats = ""
        self.failed: FailedRecording | None = None   # the last dictation that couldn't be transcribed
        self._lock = threading.Lock()
        self._session = 0
        self.current: Session | None = None
        self._adding_model = False   # the "Add a model file…" dialog is open
        self.overlay: Overlay | None = self._new_overlay(self.cfg) if self.cfg.overlay else None
        if self.overlay:
            self.overlay.margin = self.cfg.overlay_margin

    @property
    def _pill_file(self):
        return self.paths.data_dir / "pill_position.json"

    def _new_overlay(self, c: cfgmod.Config) -> Overlay:
        return Overlay(c.overlay_position, wide=c.stream_while_speaking, pos_file=self._pill_file)

    # --- model ------------------------------------------------------------------------------------

    @property
    def _model_cache(self):
        return self.paths.data_dir / "model_path.txt"

    def _resolve_model(self, c: cfgmod.Config, keep: "models.Model | None" = None) -> None:
        """Pick the model quickly (settings path, else the one picked last time, else the best one a
        Windows-only scan finds). If that finds nothing, search WSL too in the background.
        [model] folders are searched as well, either way. `keep`: the model in use stays picked when it
        is still found (only the list of folders changed)."""
        from pathlib import Path
        if c.model_dir != "auto":
            fixed = models.inspect_path(Path(c.model_dir), "your settings")
            self.found = models.merge([fixed] if fixed else [], models.find_in(c.model_folders))
            if keep is not None and any(x.path == keep.path for x in self.found):
                self._use_model(keep, apply=False)
                return
            self.model = fixed
            self.problem = None if self.model else (
                f"No speech model (.gguf) at {c.model_dir}. Fix [model] dir in Settings, or set it to \"auto\".")
            return
        self.found = models.find(include_wsl=False, extra=c.model_folders)
        if keep is not None and any(x.path == keep.path for x in self.found):
            self._use_model(keep, apply=False)
            return
        try:
            # Older versions stored a folder here; inspect_path takes a folder or a file.
            cached = Path(self._model_cache.read_text("utf-8").strip())
            m = next((x for x in self.found if x.path == cached), None) or models.inspect_path(cached, "last used")
        except OSError:
            m = None
        if m is None and self.found:
            m = self.found[0]
        if m is not None:
            self._use_model(m, apply=False)
        else:
            self.model = None
            self.problem = "Looking for a speech model (Windows and WSL)…"
            self.search_model()

    def _use_model(self, m: "models.Model", apply: bool = True) -> None:
        self.model = m
        self.problem = None
        if all(x.path != m.path for x in self.found):
            self.found = sorted([*self.found, m], key=models._rank)
        try:
            self._model_cache.write_text(str(m.path), "utf-8")
        except OSError:
            pass
        log.info("model: %s", m.describe())
        if apply:
            self.engine.set_spec(self._engine_spec(self.cfg), self.cfg.unload_after_seconds)
            self._refresh()

    def _progress(self, verb: str):
        last = [-1]

        def cb(done: int, total: int) -> None:
            pct = int(done * 100 / max(total, 1))
            if pct != last[0] and pct % 5 == 0:
                last[0] = pct
                self.model_job = f"{verb} {pct}%"
                self._refresh()
        return cb

    def _model_thread(self, job) -> None:
        if self.model_job:
            return
        self.model_job = "starting"

        def run():
            try:
                job()
            except Exception as e:
                log.exception("model job")
                self.problem = f"Model setup failed: {e}"
                self._notify(self.problem)
            finally:
                self.model_job = ""
                self._refresh()
        threading.Thread(target=run, daemon=True, name="model-job").start()

    def search_model(self) -> None:
        """Look again everywhere (Windows + WSL). Keeps the current model if it is still there."""
        def job():
            self.model_job = "searching Windows and WSL"
            self._refresh()
            found = models.find(include_wsl=True, extra=self.cfg.model_folders)
            self.found = found
            if not found:
                self.model = None
                self.problem = ("No speech model found on Windows or in WSL. "
                                "Tray menu → \"Download Parakeet v2 model\" (about 730 MB).")
                self._notify(self.problem)
                return
            keep = self.model is not None and any(x.path == self.model.path for x in found)
            if keep:
                self._notify(f"Found {len(found)} speech model(s). Still using {self.model.name}; "
                             "pick another under Model in the tray menu.")
                return
            self._switch_to(found[0], announce=True)
        self._model_thread(job)

    def _switch_to(self, m: "models.Model", announce: bool = False) -> None:
        """Use `m`, copying it to this PC first if it lives inside WSL. Runs in a model job."""
        if m.in_wsl:
            mb = m.size // (1 << 20)
            self._notify(f"Copying {m.name} from {m.source} to this PC once (about {mb} MB)…")
            m = models.import_copy(m, self.paths.data_dir / "models", self._progress("copying from WSL"))
        self._use_model(m)
        if announce:
            self._notify(f"{m.name} ready ({m.source}). Press {self.cfg.hotkey} to dictate.")

    def choose_model(self, m: "models.Model") -> None:
        if self.model is not None and m.path == self.model.path:
            return
        if m.in_wsl:
            self._model_thread(lambda: self._switch_to(m, announce=True))
        else:
            self._switch_to(m)
            self._notify(f"Now using {m.name} ({m.languages_label()}).")

    def download_model(self) -> None:
        def job():
            self._notify("Downloading Parakeet v2 (about 730 MB, checksum verified)…")
            m = models.download(self.paths.data_dir / "models", self._progress("downloading"))
            self._use_model(m)
            self._notify(f"Parakeet v2 downloaded. Press {self.cfg.hotkey} to dictate.")
        self._model_thread(job)

    def add_model_file(self) -> None:
        """Tray → Add a model file…: a Windows open-file dialog for a .gguf, in a thread of its own (the
        tray thread has to keep answering the menu)."""
        if self._adding_model:
            return
        self._adding_model = True

        def run():
            try:
                path = winutil.open_file_dialog(
                    "Add a speech model (.gguf)",
                    [("Speech models (*.gguf)", "*.gguf"), ("All files (*.*)", "*.*")],
                    initial_dir=winutil.downloads_folder())
            except OSError as e:
                log.exception("open-file dialog")
                self._notify(f"Couldn't open the file dialog: {e}")
                return
            finally:
                self._adding_model = False
                self._refresh()
            if path:
                self.add_model(path)
        threading.Thread(target=run, daemon=True, name="add-model").start()

    def add_model(self, path) -> bool:
        """Check that `path` is a speech-to-text .gguf, add it to [model] folders in config.toml (comments
        kept) and switch to it. True when it was added (or was already listed)."""
        from pathlib import Path
        from . import cfgedit
        p = Path(path)
        m = models.inspect(p, models.user_source(p))
        if m is None:
            self._notify(f"{p.name} {models.why_not_speech(p)}. Pick a speech-to-text model "
                         "(Parakeet, Whisper, …) .gguf file.")
            return False
        try:
            cfgedit.add_model_folder(self.paths.config_file, str(p))
        except (cfgmod.ConfigError, OSError) as e:
            self._notify(f"Couldn't add {p.name} to your settings: {e}")
            return False
        with self._lock:
            idle = self.state == "idle" and not self.model_job
        if not idle:   # picked up (and listed under Model) at the next dictation
            self._notify(f"Added {m.name}. Pick it under Model in the tray menu once this dictation is done.")
            return True
        if m.in_wsl:
            err = self.reload_if_changed(force=True)
            if err:
                self._notify(err)
            self._model_thread(lambda: self._switch_to(m, announce=True))
            return True
        self._use_model(m, apply=False)      # remembered, so the reload below keeps it picked
        err = self.reload_if_changed(force=True)
        self._notify(err or f"Now using {m.name} ({m.languages_label()}).")
        self._refresh()
        return True

    # --- setup ------------------------------------------------------------------------------------

    def _make_cleaner(self, c: cfgmod.Config) -> Cleaner:
        self._cleaners: dict[tuple, Cleaner] = {}   # (program, languages) -> Cleaner, for these settings
        return Cleaner.from_config(c, learned=self.learned)

    def cleaner_for(self, exe: str) -> Cleaner:
        """The clean-up for dictating into `exe` ("Code.exe"; "" = unknown): that program's
        [dictionary.apps] terms before your own, and the filler words of the dictation language."""
        langs = models.dictated_languages(self.model.languages if self.model else (), self.cfg.language)
        key = (cfgmod.app_key(exe), langs)
        cleaner = self._cleaners.get(key)
        if cleaner is None:
            terms = [t for name, entry in self.cfg.app_dictionaries if name == key[0] for t in entry]
            log.info("clean-up for %s: %d program term(s), fillers for %s", exe or "an unknown program",
                     len(terms), "/".join(langs) or "an unknown language")
            cleaner = self._cleaners[key] = Cleaner.from_config(self.cfg, languages=langs, app_terms=terms,
                                                                learned=self.learned)
        return cleaner

    def _check_learned(self) -> None:
        """Pick up learned.toml when the correction window has saved to it (checked at each dictation)."""
        try:
            stamp = self.paths.learned_file.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp == self._learned_stamp:
            return
        self._learned_stamp = stamp
        self.learned = learn.load(self.paths.learned_file)
        self.cleaner = self._make_cleaner(self.cfg)
        log.info("learned words: %d entr%s", len(self.learned), "y" if len(self.learned) == 1 else "ies")

    @staticmethod
    def _context_settings(c: cfgmod.Config) -> tuple:
        return (c.context_folders if c.context_enabled else ()), c.context_max_terms, c.context_refresh_minutes

    def _vocabulary(self, c: cfgmod.Config) -> tuple[str, ...]:
        """Notes' terms safe in every program, for models that support boosting (Parakeet doesn't)."""
        return tuple(self.context.everywhere) if c.context_enabled else ()

    def _sync_vocabulary(self) -> None:
        """A changed term list goes into the model spec only while the model isn't loaded: a spec change
        unloads it, and new terms aren't worth a reload in the middle of the user's work."""
        vocab = self._vocabulary(self.cfg)
        if vocab != self._vocab and self.engine.state == "unloaded":
            self._vocab = vocab
            self.engine.set_spec(self._engine_spec(self.cfg), self.cfg.unload_after_seconds)

    def _context_entries(self, exe: str, hwnd: int) -> list[tuple[str, str]]:
        """Context words for one dictation: the window title's, then the notes' (code-only ones only in
        [context] code_apps). Never logged beyond counts: a window title can be private."""
        c = self.cfg
        if not c.context_enabled:
            return []
        code = bool(exe) and cfgmod.app_key(exe) in c.context_code_apps
        window: list[str] = []
        if c.context_window_title and hwnd:
            try:
                title = winutil.window_title(hwnd)
            except OSError:
                title = ""
            window = context.title_terms(title, exe, self.context.is_ordinary, code_app=code)
        notes = self.context.terms_for(code)
        log.info("context words: %d from the window, %d from notes%s", len(window), len(notes),
                 " (code app)" if code else "")
        return context.dictionary_entries(window, notes)

    @staticmethod
    def _find_vad(c: cfgmod.Config):
        try:
            p = models.find_vad(c.vad)
        except OSError:
            p = None
        log.info("voice detector: %s", p or f"none ({c.vad}); using the loudness detector")
        return p

    def _engine_spec(self, c: cfgmod.Config) -> EngineSpec | None:
        m = self.model
        if m is None:
            return None
        vocab = tuple(c.hotwords) + tuple(t for t in self._vocab if t not in c.hotwords)
        return EngineSpec.for_model(m, c.threads, device=c.device, vocabulary=vocab,
                                    vad=str(self.vad_path or ""), language=c.language,
                                    gpu_marker=str(self.paths.data_dir / GPU_MARKER))

    def apply_config(self, c: cfgmod.Config) -> str | None:
        """Switch to new settings. Returns a problem to show, or None."""
        err = self.hotkeys.change(c.hotkey)
        err = err or self._extras_problem(self.hotkeys.set_extras(self._extra_hotkeys(c)))
        if c.vad != self.cfg.vad:
            self.vad_path = self._find_vad(c)
        self.cfg = c
        self.cleaner = self._make_cleaner(c)
        self.context.configure(*self._context_settings(c))
        self._vocab = self._vocabulary(c)
        self.recorder.device_hint = c.input_device
        self.sounds.enabled = c.sounds
        if c.overlay and self.overlay is None:
            self.overlay = self._new_overlay(c)
        elif self.overlay is not None and (not c.overlay or self.overlay.wide != c.stream_while_speaking):
            if self.state == "moving":     # the pill being moved goes away: move mode ends with it
                self.state = "idle"
            self.overlay.close()
            self.overlay = self._new_overlay(c) if c.overlay else None
        if self.overlay:
            self.overlay.position, self.overlay.margin = c.overlay_position, c.overlay_margin
        if c.model_dir != self.cfg_model_dir or c.model_folders != self.cfg_model_folders:
            keep = self.model if c.model_dir == self.cfg_model_dir else None   # only the folders changed
            self.cfg_model_dir, self.cfg_model_folders = c.model_dir, c.model_folders
            self._resolve_model(c, keep=keep)
        self.engine.set_spec(self._engine_spec(c), c.unload_after_seconds)
        return err or self.problem

    def reload_if_changed(self, force: bool = False) -> str | None:
        try:
            mtime = self.paths.config_file.stat().st_mtime
            if not force and mtime == self.cfg.source_mtime:
                return None
            new = cfgmod.load(self.paths)
        except (cfgmod.ConfigError, HotkeyError, OSError) as e:
            return f"Settings not applied: {e}"
        try:
            err = self.apply_config(new)
        except HotkeyError as e:
            return f"Settings not applied: {e}"
        log.info("settings reloaded")
        return err

    # --- extra shortcuts ---------------------------------------------------------------------------

    @staticmethod
    def _extra_hotkeys(c: cfgmod.Config) -> dict[str, str]:
        return {"copy_last": c.copy_last_hotkey, "correct_last": c.correct_last_hotkey}

    @staticmethod
    def _extras_problem(errors: dict[str, str]) -> str | None:
        names = {"copy_last": "Copy last dictation", "correct_last": "Correct last dictation"}
        return "; ".join(f"{names.get(k, k)} shortcut not set: {v}" for k, v in errors.items()) or None

    def _on_extra_hotkey(self, name: str) -> None:  # hotkey thread: hand the work to a thread of its own
        log.info("shortcut: %s", name)
        target = {"copy_last": self.copy_last, "correct_last": self.correct_last}.get(name)
        if target:
            threading.Thread(target=target, daemon=True, name=name).start()

    def copy_last(self) -> None:
        if not self.last_text:
            self._notify("Nothing dictated yet.")
            return
        try:
            output.copy_to_clipboard(self.last_text)
        except OSError as e:
            self._notify(f"Couldn't copy: {e}")
            return
        if self.overlay and self.state == "idle":
            self.overlay.flash("Copied to clipboard", GREEN, 900)

    def correct_last(self) -> None:
        if not self.last_text:
            self._notify("Nothing dictated yet.")
            return
        try:
            correct.launch(self.last_text, self.paths.data_dir)
        except Exception as e:
            log.exception("correction window")
            self._notify(f"Couldn't open the correction window: {e}")

    # --- the Settings window ------------------------------------------------------------------------

    _settings_proc = None   # the open Settings window's process

    @property
    def _settings_request(self):
        """Where the Settings window leaves things for the tray to do once it closes (move the pill...)."""
        return self.paths.data_dir / "settings_request.json"

    def _settings_actions(self) -> list[str]:
        import json
        f = self._settings_request
        try:
            actions = json.loads(f.read_text("utf-8")).get("actions", [])
        except (OSError, ValueError, AttributeError):
            return []
        finally:
            try:
                f.unlink()
            except OSError:
                pass
        return [a for a in actions if isinstance(a, str)]

    def _run_settings_action(self, action: str) -> None:
        log.info("settings window asked for: %s", action)
        if action == "move_pill":
            if self.overlay is None:
                self._notify("Turn the pill on first (Settings → General → Pill).")
            elif self.state == "idle":
                self.move_pill()
        elif action == "reset_pill":
            self.reset_pill_position()
        elif action == "search_models":
            self.search_model()
        elif action == "free_memory":
            self.engine.unload()
            self._notify("Model memory freed.")
        elif action == "download_model":
            self.download_model()

    def open_settings_window(self) -> None:
        """The Settings window, its own process and only one at a time; settings reload when it closes."""
        from . import settings_ui
        p = self._settings_proc
        if p is not None and p.poll() is None:
            return
        self._settings_actions()   # drop a request a crashed window may have left behind
        try:
            before = self.paths.config_file.stat().st_mtime
        except OSError:
            before = None
        try:
            p = self._settings_proc = settings_ui.launch()
        except Exception as e:
            log.exception("settings window")
            self._notify(f"Couldn't open the Settings window: {e}")
            return

        def wait() -> None:
            p.wait()
            try:
                changed = self.paths.config_file.stat().st_mtime != before
            except OSError:
                changed = False
            if changed:
                err = self.reload_if_changed(force=True)
                self._notify(err or "Settings saved.")
                self._refresh()
            for action in self._settings_actions():
                self._run_settings_action(action)

        threading.Thread(target=wait, daemon=True, name="settings-window").start()

    # --- dictation flow -----------------------------------------------------------------------------

    def _on_hotkey(self) -> None:  # hotkey thread
        log.info("hotkey pressed (state=%s)", self.state)
        with self._lock:
            if self.state == "recording":
                self._finish_locked()
                return
            if self.state == "busy":
                self.sounds.play("error")
                return
            if self.state == "moving":       # never dictate while the pill can take the focus
                self._end_move_locked(save=True)
                self._notify(f"Pill position saved. Press {self.cfg.hotkey} again to dictate.")
                return
            if self.overlay is not None and self.overlay.is_foreground():
                self._notify("Click the window you want to type into, then press the hotkey again.")
                self.sounds.play("error")
                return
            problem = self.reload_if_changed()
            if problem:
                self._notify(problem)
            self._check_learned()
            if self.problem:
                self._notify(self.problem)
                self.sounds.play("error")
                return
            try:
                self.recorder.start()
            except Exception as e:
                log.exception("microphone")
                self._notify(f"Microphone could not be opened: {e}")
                self.sounds.play("error")
                return
            if self.recorder.warning:   # chosen microphone missing; recording from the default instead
                self._notify(self.recorder.warning)
            self.state = "recording"
            log.info("recording started at %d Hz", self.recorder.rate)
            self._session += 1
            session = self._session
            self.target_exe = winutil.foreground_exe()   # for its [dictionary.apps] terms and [apps] settings
            self.target_hwnd = winutil.foreground_window()
            self.target_terms = self._context_entries(self.target_exe, self.target_hwnd)
            self._sync_vocabulary()
            self.current = Session(self, session, use_vad=self.vad_path is not None)
            self.sounds.play("start")
            if self.cfg.esc_cancels:
                self.hotkeys.escape_on()
            if self.overlay:
                rec, cur = self.recorder, self.current
                self.overlay.listening(lambda: rec.level, rec.started_at, lambda: cur.writing, lambda: rec.clipping,
                                       lambda: cur.tentative)
        self.engine.warm()  # load the model while the user is talking
        self.context.refresh()   # notes changed since the last look are read again, in the background
        self._refresh()
        threading.Thread(target=self._watch_release, args=(session,), daemon=True).start()

    def _watch_release(self, session: int) -> None:
        """Hold-to-talk: if the key is held past the threshold, releasing it stops the recording.

        Also stops it, like pressing the hotkey, when the microphone goes away, at max_seconds, and in
        tap mode after [audio] silence_stop_seconds without speech (holding the keys means you're
        still dictating, so silence never stops a hold)."""
        t0 = time.monotonic()
        deadline = t0 + self.cfg.max_seconds
        vk = self.hotkeys.vk
        rec = self.recorder
        reason = ""
        while key_down(vk) and time.monotonic() < deadline:
            time.sleep(0.02)
            if self._session != session or self.state != "recording":
                return
            if rec.lost:
                reason = MIC_LOST
                break
        held = time.monotonic() - t0
        if not reason:
            with self._lock:
                if held >= self.cfg.hold_threshold and self._session == session and self.state == "recording":
                    self._finish_locked()
                    return
        # Toggle mode: stop for a lost microphone, silence, or the maximum length.
        while not reason:
            now = time.monotonic()
            if now >= deadline:
                reason = f"Stopped after {self.cfg.max_seconds} s ([output] max_seconds)."
            elif rec.lost:
                reason = MIC_LOST
            elif silence_stop_due(rec.meter.last_speech_at, rec.started_at, now, self.cfg.silence_stop_seconds):
                reason = silence_stop_message(self.cfg.silence_stop_seconds)
            else:
                time.sleep(0.25)
                if self._session != session or self.state != "recording":
                    return
        with self._lock:
            if self._session == session and self.state == "recording":
                log.info("auto stop: %s", reason)
                self._notify(reason)
                self._finish_locked()

    def stop_from_voice(self, sid: int) -> None:
        """A spoken "stop listening": end the recording like the hotkey. Called from the transcription
        worker, which finishing waits for, so the stop runs on its own thread."""
        def run():
            with self._lock:
                if self._session == sid and self.state == "recording":
                    log.info("recording stopped by voice command")
                    self._finish_locked()
        threading.Thread(target=run, daemon=True, name="voice-stop").start()

    def _on_cancel(self) -> None:  # hotkey thread (Esc)
        with self._lock:
            if self.state != "recording":
                return
            self.recorder.cancel()
            self.hotkeys.escape_off()
            self.state = "idle"
            self._session += 1
            if self.current:
                self.current.cancel()
                self.current = None
            log.info("recording cancelled (Esc)")
        self.sounds.play("cancel")
        if self.overlay:
            self.overlay.flash("Cancelled", MUTED, 700)
        self.engine.done()
        self._refresh()

    def _finish_locked(self) -> None:
        log.info("recording stopped after %.1fs", time.monotonic() - self.recorder.started_at)
        self.recorder.close()
        self.hotkeys.escape_off()
        self.state = "busy"
        self._session += 1
        self.sounds.play("stop")
        if self.overlay:
            self.overlay.working()
        session, self.current = self.current, None
        threading.Thread(target=self._complete, args=(session,), daemon=True).start()
        self._refresh()

    def _complete(self, session: "Session") -> None:
        t_stop = time.monotonic()
        try:
            try:
                raw = session.finish()
            except Exception as e:
                self._recognition_failed(e, session.recording(), session.rate,
                                         bool(session.typer and session.typer.text))
                return
            waited = time.monotonic() - t_stop
            spoken = session.total_samples / max(1, session.rate)
            if not raw:
                self.last_stats = f"{spoken:.1f}s recorded, no speech detected"
                log.info("dictation: %s", self.last_stats)
                if self.overlay:
                    self.overlay.flash("No speech heard", MUTED, 1200)
                return
            typer = session.typer
            # Live: the phrases are already typed, apart from any that couldn't be (they go to the clipboard).
            text = (" ".join(t for t in (typer.text, typer.untyped) if t) if typer
                    else session.text(raw))
            self.last_stats = (f"{spoken:.1f}s speech in {session.parts} part(s), "
                               f"{'voice detector' if session.vad_mode else 'loudness'} cuts; "
                               f"text done {waited:.1f}s after you stopped")
            log.info("dictation: %s (model time %.1fs)", self.last_stats, session.decode_seconds)
            if self.cfg.log_transcripts:
                log.info("raw: %r | clean: %r", raw, text)
            if not text:
                if self.overlay:
                    self.overlay.flash("Only filler words heard", MUTED, 1200)
                return
            self.last_text = text
            if typer is None:
                typed = self._land(text, session.out)
            elif typer.failed is not None:
                self._notify(output.copy_instead(typer.untyped, typer.failed))
                typed = False
            else:
                typed = True
            if typed:
                self._press_enter(session.out)
            if self.overlay:
                self.overlay.flash("Typed" if typed else "Copied to clipboard", GREEN, 700)
        except Exception as e:
            log.exception("deliver")
            self._notify(f"Something went wrong: {e}")
            self.sounds.play("error")
            if self.overlay:
                self.overlay.flash("Something went wrong", RED, 2000)
        finally:
            self.engine.done()  # start the "free memory" countdown from the end of the dictation
            with self._lock:
                self.state = "idle"
            self._refresh()
            winutil.trim_working_set()

    def _land(self, text: str, out=None) -> bool:
        """Type the text where the cursor is. If it can't be typed, put it on the clipboard and say so.
        True when it was typed. `out`: the program's output settings (None = [output] as set)."""
        out = out or settings_for("", self.cfg)
        try:
            output.deliver(text + (" " if out.add_trailing_space else ""), out.method)
            return True
        except output.DeliveryError as e:
            log.warning("could not type: %s", e)
            self._notify(output.copy_instead(text, e))
            return False

    def _press_enter(self, out=None) -> None:
        if (out or settings_for("", self.cfg)).press_enter:
            try:
                output.press_enter()
            except OSError as e:
                log.warning("could not press Enter: %s", e)

    def _recognition_failed(self, e: Exception, audio: np.ndarray, rate: int, partly_typed: bool) -> None:
        """Keep the recording (replacing an older failed one) so tray → Retry can transcribe it again."""
        if isinstance(e, EngineError):
            log.error("engine: %s", e)
        else:
            log.error("transcribe", exc_info=e)
        if audio.size:
            self.failed = FailedRecording(audio, rate, partly_typed)
            self._notify(FAILED_NOTE)
        else:
            self._notify(f"Transcription failed: {e}")
        self.sounds.play("error")
        if self.overlay:
            self.overlay.flash("Transcription failed", RED, 2000)

    def retry_failed(self) -> None:
        """Tray → Retry last recording: transcribe the kept recording in one go and type it."""
        with self._lock:
            kept = self.failed
            if kept is None or self.state != "idle":
                if kept is not None:
                    self.sounds.play("error")   # a dictation is running; retry once it's done
                return
            self.state = "busy"
        if self.overlay:
            self.overlay.working()
        self._refresh()
        threading.Thread(target=self._retry, args=(kept,), daemon=True, name="retry").start()

    def _retry(self, kept: FailedRecording) -> None:
        try:
            try:
                audio = preprocess(resample(kept.audio, kept.rate, SR), normalize=self.cfg.normalize_audio,
                                   noise_reduction=self.cfg.noise_reduction)
                raw = self.engine.transcribe(audio)[0].strip() if audio.size else ""
            except Exception as e:
                self._recognition_failed(e, kept.audio, kept.rate, kept.partly_typed)
                return
            if self.failed is kept:
                self.failed = None
            self._check_learned()
            text = self.cleaner.clean(raw)
            if self.cfg.log_transcripts:
                log.info("retry raw: %r | clean: %r", raw, text)
            if not text:
                self._notify("Retried: no speech heard in the kept recording.")
                if self.overlay:
                    self.overlay.flash("No speech heard", MUTED, 1200)
                return
            self.last_text = text
            if not output.has_typing_target():
                # Picking the tray menu item leaves focus on the tray; let the user click back into a window.
                if self.overlay:
                    self.overlay.working("Click where the text should go…")
                output.wait_for_target(5.0)
            typed = self._land(text)
            if kept.partly_typed:
                self._notify(DOUBLED_NOTE)
            elif typed:
                self._press_enter()
            if self.overlay:
                self.overlay.flash("Typed" if typed else "Copied to clipboard", GREEN, 700)
        except Exception as e:
            log.exception("retry")
            self._notify(f"Something went wrong: {e}")
            self.sounds.play("error")
            if self.overlay:
                self.overlay.flash("Something went wrong", RED, 2000)
        finally:
            self.engine.done()
            with self._lock:
                self.state = "idle"
            self._refresh()
            winutil.trim_working_set()

    def discard_failed(self) -> None:
        with self._lock:
            if self.state == "idle":
                self.failed = None
        self._refresh()

    # --- moving the pill ----------------------------------------------------------------------------

    def move_pill(self) -> None:
        """Tray → Move the pill… (state idle -> moving), or Done moving the pill (saves). While moving,
        no dictation or retry starts: they need state idle, and the hotkey ends move mode instead."""
        with self._lock:
            if self.state == "moving":
                self._end_move_locked(save=True)
                return
            ov = self.overlay
            if self.state != "idle" or ov is None:
                self.sounds.play("error")
                return
            self.state = "moving"
        if not ov.start_move(self._move_ended):
            with self._lock:
                if self.state == "moving":
                    self.state = "idle"
            self._notify("The pill isn't running, so it can't be moved. Check [overlay] enabled in Settings.")
        self._refresh()

    def _end_move_locked(self, save: bool) -> None:
        """Ask the pill to end move mode; _move_ended sets the state back to idle once its styles are back."""
        if self.overlay is None or not self.overlay.end_move(save):
            self.state = "idle"      # no pill window (any more): nothing can hold the focus

    def _move_ended(self, saved: bool) -> None:  # the overlay's "pill-moved" thread
        with self._lock:
            if self.state == "moving":
                self.state = "idle"
        log.info("pill move %s", "saved" if saved else "cancelled")
        self._refresh()

    def reset_pill_position(self) -> None:
        """Tray → Reset pill position: back to [overlay] position / margin on the screen you're working on."""
        from . import pillpos
        try:
            pillpos.reset(self._pill_file)
        except OSError as e:
            self._notify(f"Couldn't reset the pill position: {e}")
            return
        if self.overlay is not None:
            self.overlay.forget_position()
        self._notify("The pill is back at its usual place.")
        self._refresh()

    # --- tray -------------------------------------------------------------------------------------

    def _visual_state(self) -> str:
        if self.model_job:
            return "working"
        if self.problem:
            return "error"
        if self.state == "recording":
            return "recording"
        if self.state == "busy" or self.engine.state == "loading":
            return "working"
        return "ready" if self.engine.state == "ready" else "idle"

    def _status_text(self) -> str:
        if self.model_job:
            return f"Model: {self.model_job}…"
        if self.problem:
            return self.problem[:90]
        return {"recording": "Listening… (hotkey again to stop, Esc cancels)", "busy": "Transcribing…",
                "moving": "Moving the pill (Enter saves, Esc cancels)"}.get(
            self.state, {"ready": "Ready (model loaded)", "loading": "Loading model…"}.get(
                self.engine.state, "Ready (model loads on first use)"))

    def _refresh(self) -> None:
        if self.icon is None:
            return
        self.icon.icon = icon_image(self._visual_state())
        self.icon.title = f"{APP_NAME}: {self._status_text()}\nHotkey: {self.cfg.hotkey}"[:127]
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _notify(self, message: str) -> None:
        log.warning(message)
        if self.icon is not None:
            try:
                self.icon.notify(message[:255], APP_NAME)
            except Exception:
                pass

    def _model_text(self) -> str:
        if self.model is None:
            return "Model: not found yet"
        where = f", {self.engine.note}" if self.engine.state == "ready" and self.engine.note else ""
        return f"Model: {self.model.name} ({self.model.languages_label()}{where})"[:120]

    def _model_items(self):
        """The "Model" submenu: every model found, the one in use ticked. Rebuilt each time it opens."""
        if not self.found:
            yield pystray.MenuItem("No speech models found", None, enabled=False)
            return
        # pystray counts every lambda parameter (defaults too), so bind `m` through factories instead.
        def pick(m):
            return lambda icon, item: self.choose_model(m)

        def is_current(m):
            return lambda item: self.model is not None and m.path == self.model.path

        for m in self.found:
            yield pystray.MenuItem(m.label(), pick(m), checked=is_current(m), radio=True,
                                   enabled=lambda item: not self.model_job)

    def _can_retry(self) -> bool:
        return self.failed is not None and self.state == "idle"

    def _menu(self) -> pystray.Menu:
        I, S = pystray.MenuItem, pystray.Menu.SEPARATOR

        def quit_app(icon, item):
            self.engine.unload()
            self.hotkeys.stop()
            if self.overlay:
                self.overlay.close()
            icon.stop()

        def with_key(text: str, spec: str) -> str:
            return f"{text}\t{hotkey_display(spec)}" if spec else text

        # Only what is used day to day; everything else lives in Settings. Items that only sometimes
        # apply (a failed recording, moving the pill, no model yet) appear only then.
        return pystray.Menu(
            I(lambda _: self._status_text(), None, enabled=False),
            S,
            I(lambda _: with_key("Copy last dictation", self.cfg.copy_last_hotkey),
              lambda i, it: self.copy_last(), enabled=lambda _: bool(self.last_text)),
            I(lambda _: with_key("Correct last dictation…", self.cfg.correct_last_hotkey),
              lambda i, it: self.correct_last(), enabled=lambda _: bool(self.last_text)),
            I("Retry last recording", lambda i, it: self.retry_failed(), visible=lambda _: self.failed is not None,
              enabled=lambda _: self._can_retry()),
            I("Discard last recording", lambda i, it: self.discard_failed(),
              visible=lambda _: self.failed is not None, enabled=lambda _: self._can_retry()),
            I("Done moving the pill", lambda i, it: self.move_pill(), visible=lambda _: self.state == "moving"),
            I("Download Parakeet v2 model (about 730 MB)", lambda i, it: self.download_model(),
              visible=lambda _: self.model is None, enabled=lambda _: not self.model_job),
            S,
            I("Settings…", lambda i, it: self.open_settings_window(), default=True),
            I(f"Quit {APP_NAME}", quit_app),
        )

    def run(self) -> None:
        self.sounds.enabled = self.cfg.sounds
        self.hotkeys.start()
        hotkey_error = self.hotkeys.wait_started()
        hotkey_error = hotkey_error or self._extras_problem(self.hotkeys.set_extras(self._extra_hotkeys(self.cfg)))
        self.icon = pystray.Icon("parakeet_dictate", icon_image(self._visual_state()), APP_NAME, self._menu())

        def setup(icon):
            icon.visible = True
            self._refresh()
            first = hotkey_error or self.problem
            self._notify(first or f"Ready. Press {self.cfg.hotkey} to dictate.")
            self.context.refresh()   # first look at the [context] folders, in the background
            winutil.trim_working_set()

        self.icon.run(setup=setup)
