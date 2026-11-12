"""Ties hotkey -> microphone -> Parakeet -> cleanup -> typing together behind a tray icon."""
from __future__ import annotations

import logging
import os
import queue
import threading
import time

import pystray

from . import APP_NAME, __version__
from . import config as cfgmod
from . import models, output, winutil
from .audio import SR, Recorder, preprocess, resample, silence_stop_due, silence_stop_message
from .engine import Engine, EngineError, EngineSpec
from .hotkey import HotkeyError, HotkeyThread, key_down
from .overlay import GREEN, MUTED, RED, Overlay
from .segmenter import Segmenter, VadSegmenter
from .textclean import Cleaner
from .tray import icon_image, write_ico

log = logging.getLogger(__name__)

MIC_LOST = "Microphone disconnected. Dictation stopped."


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
        self.typer = (output.PhraseTyper(app.cleaner.clean, app.cfg.output_method, app.cfg.add_trailing_space)
                      if self.parallel and app.cfg.type_while_speaking else None)
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

    @property
    def writing(self) -> bool:
        """Speech was heard whose text hasn't been typed yet (for the overlay)."""
        return self._busy > 0 or bool(self.typer and self.typer.pending)

    def _recording(self) -> bool:
        return self.app._session == self.id and self.app.state == "recording" and not self._cancelled

    def _take(self, final: bool = False) -> None:
        x = self.app.recorder.drain()
        self.total_samples += x.size
        if not self.parallel:
            if x.size:
                self._buffer.append(x)
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

    def _work(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                if self._cancelled or self._error:
                    continue
                idx, samples, rate = item
                audio = preprocess(resample(samples, rate, SR), normalize=self.normalize)
                if audio.size == 0:
                    self._results[idx] = ""
                    continue
                raw, secs = self.app.engine.transcribe(audio)
                self.decode_seconds += secs
                self._results[idx] = raw.strip()
                if self.typer and raw.strip() and not self._cancelled:
                    self.typer.add(raw.strip())
                    self.typer.pump()
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
        self.cleaner = self._make_cleaner(self.cfg)
        self.recorder = Recorder(self.cfg.input_device)
        self.icon: pystray.Icon | None = None
        self.problem: str | None = None
        self.model: models.Model | None = None
        self.found: list[models.Model] = []   # every model found, for the tray "Model" menu
        self.model_job = ""          # "searching" / "copying 40%" / "downloading 12%"
        self.cfg_model_dir = self.cfg.model_dir
        self.engine = Engine(None, self.cfg.unload_after_seconds, on_change=self._refresh)
        self.vad_path = self._find_vad(self.cfg)
        self._resolve_model(self.cfg)
        self.engine.set_spec(self._engine_spec(self.cfg), self.cfg.unload_after_seconds)
        self.hotkeys = HotkeyThread(self.cfg.hotkey, self._on_hotkey, self._on_cancel)
        self.state = "idle"          # idle | recording | busy
        self.last_text = ""
        self.last_stats = ""
        self._lock = threading.Lock()
        self._session = 0
        self.current: Session | None = None
        self.overlay: Overlay | None = Overlay(self.cfg.overlay_position) if self.cfg.overlay else None

    # --- model ------------------------------------------------------------------------------------

    @property
    def _model_cache(self):
        return self.paths.data_dir / "model_path.txt"

    def _resolve_model(self, c: cfgmod.Config) -> None:
        """Pick the model quickly (settings path, else the one picked last time, else the best one a
        Windows-only scan finds). If that finds nothing, search WSL too in the background."""
        from pathlib import Path
        if c.model_dir != "auto":
            self.model = models.inspect_path(Path(c.model_dir), "your settings")
            self.found = [self.model] if self.model else []
            self.problem = None if self.model else (
                f"No speech model (.gguf) at {c.model_dir}. Fix [model] dir in Settings, or set it to \"auto\".")
            return
        self.found = models.find(include_wsl=False)
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
            found = models.find(include_wsl=True)
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

    # --- setup ------------------------------------------------------------------------------------

    @staticmethod
    def _make_cleaner(c: cfgmod.Config) -> Cleaner:
        return Cleaner.from_config(c)

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
        return EngineSpec.for_model(m, c.threads, device=c.device, vocabulary=c.hotwords,
                                    vad=str(self.vad_path or ""))

    def apply_config(self, c: cfgmod.Config) -> str | None:
        """Switch to new settings. Returns a problem to show, or None."""
        err = self.hotkeys.change(c.hotkey)
        if c.vad != self.cfg.vad:
            self.vad_path = self._find_vad(c)
        self.cfg = c
        self.cleaner = self._make_cleaner(c)
        self.recorder.device_hint = c.input_device
        self.sounds.enabled = c.sounds
        if c.overlay and self.overlay is None:
            self.overlay = Overlay(c.overlay_position)
        elif not c.overlay and self.overlay is not None:
            self.overlay.close()
            self.overlay = None
        if self.overlay:
            self.overlay.position = c.overlay_position
        if c.model_dir != self.cfg_model_dir:
            self.cfg_model_dir = c.model_dir
            self._resolve_model(c)
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
            problem = self.reload_if_changed()
            if problem:
                self._notify(problem)
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
            self.current = Session(self, session, use_vad=self.vad_path is not None)
            self.sounds.play("start")
            if self.cfg.esc_cancels:
                self.hotkeys.escape_on()
            if self.overlay:
                rec, cur = self.recorder, self.current
                self.overlay.listening(lambda: rec.level, rec.started_at, lambda: cur.writing, lambda: rec.clipping)
        self.engine.warm()  # load the model while the user is talking
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
            raw = session.finish()
            waited = time.monotonic() - t_stop
            spoken = session.total_samples / max(1, session.rate)
            if not raw:
                self.last_stats = f"{spoken:.1f}s recorded, no speech detected"
                log.info("dictation: %s", self.last_stats)
                if self.overlay:
                    self.overlay.flash("No speech heard", MUTED, 1200)
                return
            live = session.typer is not None
            text = session.typer.text if live else self.cleaner.clean(raw)   # live: already typed
            self.last_stats = (f"{spoken:.1f}s speech in {session.parts} part(s), "
                               f"{'voice detector' if session.vad_mode else 'loudness'} cuts; "
                               f"text done {waited:.1f}s after you stopped")
            log.info("dictation: %s (model time %.1fs)", self.last_stats, session.decode_seconds)
            if self.cfg.log_transcripts:
                log.info("raw: %r | clean: %r", raw, text)
            if text:
                self.last_text = text
                if not live:
                    output.deliver(text + (" " if self.cfg.add_trailing_space else ""), self.cfg.output_method)
                if self.overlay:
                    self.overlay.flash("Typed", GREEN, 700)
            elif self.overlay:
                self.overlay.flash("Only filler words heard", MUTED, 1200)
        except EngineError as e:
            log.error("engine: %s", e)
            self._notify(f"Transcription failed: {e}")
            self.sounds.play("error")
            if self.overlay:
                self.overlay.flash("Transcription failed", RED, 2000)
        except Exception as e:
            log.exception("transcribe")
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
        return {"recording": "Listening… (hotkey again to stop, Esc cancels)", "busy": "Transcribing…"}.get(
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

    def _menu(self) -> pystray.Menu:
        I, S = pystray.MenuItem, pystray.Menu.SEPARATOR

        def copy_last(icon, item):
            if self.last_text:
                output.copy_to_clipboard(self.last_text)

        def open_settings(icon, item):
            os.startfile(self.paths.config_file)

        def reload(icon, item):
            err = self.reload_if_changed(force=True)
            self._notify(err or "Settings reloaded.")
            self._refresh()

        def unload(icon, item):
            self.engine.unload()

        def toggle_autostart(icon, item):
            lnk = winutil.startup_shortcut()
            try:
                if lnk.exists():
                    lnk.unlink()
                else:
                    winutil.create_shortcut(lnk, self.ico)
            except Exception as e:
                self._notify(f"Could not change start-up setting: {e}")

        def open_log(icon, item):
            if self.paths.log_file.exists():
                os.startfile(self.paths.log_file)

        def quit_app(icon, item):
            self.engine.unload()
            self.hotkeys.stop()
            if self.overlay:
                self.overlay.close()
            icon.stop()

        return pystray.Menu(
            I(lambda _: self._status_text(), None, enabled=False),
            I(lambda _: f"Hotkey: {self.cfg.hotkey}", None, enabled=False),
            I(lambda _: f"Last: {self.last_stats}" if self.last_stats else "Last: —", None, enabled=False),
            I(lambda _: self._model_text(), pystray.Menu(self._model_items)),
            I(lambda _: "Phrases: " + ("Silero voice detector" if self.vad_path else "loudness detector"),
              None, enabled=False),
            S,
            I("Copy last dictation", copy_last, enabled=lambda _: bool(self.last_text)),
            I("Find models again (Windows + WSL)", lambda i, it: self.search_model(),
              enabled=lambda _: not self.model_job),
            I("Download Parakeet v2 model (about 730 MB)", lambda i, it: self.download_model(),
              enabled=lambda _: not self.model_job),
            I("Settings…", open_settings, default=True),
            I("Reload settings", reload),
            I("Free model memory now", unload, enabled=lambda _: self.engine.state != "unloaded"),
            I("Start with Windows", toggle_autostart, checked=lambda _: winutil.startup_shortcut().exists()),
            I("Open log", open_log),
            S,
            I(f"Quit {APP_NAME} {__version__}", quit_app),
        )

    def run(self) -> None:
        self.sounds.enabled = self.cfg.sounds
        self.hotkeys.start()
        hotkey_error = self.hotkeys.wait_started()
        self.icon = pystray.Icon("parakeet_dictate", icon_image(self._visual_state()), APP_NAME, self._menu())

        def setup(icon):
            icon.visible = True
            self._refresh()
            first = hotkey_error or self.problem
            self._notify(first or f"Ready. Press {self.cfg.hotkey} to dictate.")
            winutil.trim_working_set()

        self.icon.run(setup=setup)
