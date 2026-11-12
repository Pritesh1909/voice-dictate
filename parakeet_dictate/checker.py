"""The background checker: one idle run over the saved recordings that nobody has checked yet.

Whisper Large v3 Turbo (the "teacher", in its own engine process on the graphics card) listens to each
recording again. Where its words and Parakeet's agree, the recording is done. Where they disagree a
little, the pair is judged: by Claude (text only, a batch of pairs per `claude -p` call, at most
[learning] claude_calls_per_day calls a day), or without Claude only for fixes to terms the user already
has. The fixes in a judged text are counted by autolearn.Store, which activates one after two different
recordings showed it. Where the two texts have little in common (Whisper making up words on noise, "I'm
sorry. I'm sorry."), nothing is learned.

Every checked recording gets a row in checked.jsonl, so it is never checked again. Two kinds are left
for a later run instead: recordings not reached before should_stop() (the user came back), and pairs
waiting for Claude when today's calls are used up (not transcribed again until tomorrow or an app restart).

The teacher never writes gpu-failed.json: a Whisper crash must not move dictation to the CPU. It ends the
run with an "error:" result, which pauses learning until the app restarts.
"""
from __future__ import annotations

import difflib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import judge, learn, recordings
from .audio import preprocess
from .autolearn import Entry, Store
from .dictionary import word_key
from .engine import Engine, EngineError, EngineSpec
from .textclean import Cleaner

log = logging.getLogger(__name__)

MIN_SIMILARITY = 0.5      # word-similarity ratio below which two texts aren't the same speech
EXAMPLE_CHARS = 120       # of the judged text, kept as the fix's example

STATUS_NO_GPU = "Paused: no graphics card"
STATUS_FAILED = "Paused: Whisper didn't load"
STATUS_CRASHED = "Paused: Whisper stopped working"


def teacher_spec(path: str, threads: int, language: str, prompt: str) -> EngineSpec:
    """Whisper on the discrete graphics card. No voice detector, and gpu_marker="" so a crash on the
    card is never written to gpu-failed.json (which would switch dictation to the CPU)."""
    return EngineSpec(path, threads, device="gpu", vad="", language=language, gpu_marker="", prompt=prompt)


def _words(text: str) -> list[str]:
    """The words of a text, lower case, without punctuation: what "the same words" compares."""
    return [k for k in (word_key(w) for w in text.split()) if k]


def similar(a: str, b: str) -> float:
    """How alike two texts are, 0..1: difflib's ratio over their lower-case words."""
    return difflib.SequenceMatcher(None, _words(a), _words(b), autojunk=False).ratio()


@dataclass
class _Pending:
    rec: recordings.Record
    parakeet: str
    whisper: str


class Checker:
    def __init__(self, store: Store, recordings_dir: Path, make_engine: Callable[[EngineSpec], Engine],
                 cleaner_for: Callable[[str], Cleaner], terms: Callable[[], list[str]],
                 user_entries: Callable[[], list[Entry]], cfg, teacher_path: str, *,
                 ask: Callable[[list[judge.Pair], list[str]], dict[int, str]] = judge.ask_claude,
                 now: Callable[[], datetime] = datetime.now):
        """cfg: the app's Config (threads, language, use_claude, claude_calls_per_day are used).
        teacher_path: Whisper's .gguf file. ask: judge.ask_claude, replaceable in tests."""
        self._store = store
        self._dir = Path(recordings_dir)
        self._make_engine = make_engine
        self._cleaner_for = cleaner_for
        self._terms = terms
        self._user_entries = user_entries
        self._cfg = cfg
        self._teacher_path = teacher_path
        self._ask = ask
        self._now = now
        self._stop = threading.Event()
        self._engine: Engine | None = None   # the teacher while it is loaded, so stop() can kill it
        self._day = ""
        self._waiting_for_claude: set[str] = set()   # recording ids over today's Claude calls
        self._checked = 0
        self._learned = 0

    def stop(self) -> None:
        """End the current run (the hotkey was pressed). Kills Whisper's process at once, without waiting
        for the recording it is on, so the graphics card is free for dictation; the run then ends at the
        next recording or Claude batch. Call from any thread; returns immediately."""
        self._stop.set()
        engine = self._engine
        if engine is not None:
            engine.kill()

    def run(self, should_stop: Callable[[], bool]) -> str:
        """Check the unchecked recordings, oldest first, until done or stopped. Returns a summary for the
        log; one starting with "error:" means learning should pause until the app restarts."""
        self._stop.clear()

        def stopped() -> bool:
            return self._stop.is_set() or should_stop()

        now = self._now()
        today = now.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._waiting_for_claude = today, set()
        self._checked = self._learned = 0
        try:
            self._store.sync()   # an undo whose learned_auto.toml write failed stops being applied
        except (OSError, TimeoutError) as e:
            log.warning("background check: learned fixes not re-synced: %s", e)
        engine = None
        try:
            todo = self._unchecked()
            if not todo:
                return "nothing to check"   # without loading Whisper: the idle loop asks every 30 s
            if stopped():
                return "stopped"
            terms = list(self._terms())
            engine = self._engine = self._make_engine(
                teacher_spec(self._teacher_path, self._cfg.threads, self._cfg.language, judge.prompt_terms(terms)))
            if stopped():   # stop() came before self._engine was set, so it had nothing to kill
                return "stopped"
            engine.warm()
            ready = engine.wait_ready()   # kill() ends this wait at once
            if stopped():
                return "stopped"
            if not ready:
                self._status(STATUS_FAILED)
                return "error: teacher failed to load"
            if engine.device_type != "gpu":
                log.info("background check paused: Whisper loaded on %s, not a graphics card", engine.device_type)
                self._status(STATUS_NO_GPU)
                return "error: no gpu"
            queue = self._listen(engine, todo, stopped)
            self._unload_teacher(engine)   # off the graphics card while Claude thinks
            self._judge(queue, terms, stopped, today, now.strftime("%Y-%m-%d %H:%M"))
        except EngineError as e:
            if stopped():
                return self._finish(stopped=True)   # stop() killed Whisper mid-call: not a crash
            log.error("background check: Whisper stopped: %s", e)
            self._status(STATUS_CRASHED)
            return "error: teacher crashed"
        except (OSError, TimeoutError) as e:
            log.error("background check: the learning state could not be written: %s", e)
            return f"error: learning state: {e}"
        except Exception as e:   # a background job: never take the tray down
            log.exception("background check failed")
            return f"error: {type(e).__name__}: {e}"
        finally:
            if engine is not None:
                self._unload_teacher(engine)
        return self._finish(stopped=stopped())

    def _unload_teacher(self, engine: Engine) -> None:
        self._engine = None
        engine.unload()

    # --- steps ---------------------------------------------------------------------------------------

    def _unchecked(self) -> list[recordings.Record]:
        done = self._store.checked_ids() | self._waiting_for_claude
        return [r for r in recordings.list_records(self._dir) if r.id not in done]

    def _listen(self, engine: Engine, todo: list[recordings.Record], stopped) -> list[_Pending]:
        """Whisper over each recording; the ones that need judging are returned, the rest are checked."""
        queue: list[_Pending] = []
        for rec in todo:
            if stopped():
                break
            cleaner = self._cleaner_for(rec.exe)
            parakeet = cleaner.clean(rec.raw)   # with today's fixes, so a learned fix makes the texts agree
            audio = recordings.load_audio(self._dir, rec.id)
            if audio is None:
                self._append(rec, parakeet, None, skipped="no audio")
                continue
            samples = preprocess(audio, noise_reduction="auto")
            if stopped():   # again: the noise check in preprocess can take a moment
                break
            whisper = cleaner.clean(engine.transcribe(samples)[0])
            if _words(parakeet) == _words(whisper):
                self._append(rec, parakeet, whisper, matched=True)
            elif similar(parakeet, whisper) < MIN_SIMILARITY:
                self._append(rec, parakeet, whisper, skipped="unrelated")
            else:
                queue.append(_Pending(rec, parakeet, whisper))
        return queue

    def _judge(self, queue: list[_Pending], terms: list[str], stopped, today: str, now: str) -> None:
        cap = self._cfg.claude_calls_per_day
        use_claude = self._cfg.use_claude and cap > 0
        known = set(terms)
        for start in range(0, len(queue), judge.BATCH_SIZE):
            if stopped():
                return
            batch = queue[start:start + judge.BATCH_SIZE]
            answers = None
            if use_claude:
                if self._store.claude_calls_today(today) >= cap:
                    log.info("background check: Claude calls for today used up; %d left for tomorrow",
                             len(queue) - start)
                    self._waiting_for_claude.update(p.rec.id for p in queue[start:])
                    return
                pairs = [judge.Pair(start + i, p.parakeet, p.whisper) for i, p in enumerate(batch)]
                ran = True
                try:
                    answers = self._ask(pairs, terms)
                except judge.JudgeUnavailable as e:
                    log.warning("background check: Claude unavailable, learning only fixes to known terms: %s", e)
                    use_claude, ran = False, e.ran
                if ran:   # a call that never started (claude not installed) uses up nothing
                    self._store.count_claude_call(today)
            for i, p in enumerate(batch):
                if answers is None:
                    self._learn_known(p, known, now)
                else:
                    self._learn_judged(p, answers.get(start + i), now)

    def _learn_judged(self, p: _Pending, answer: str | None, now: str) -> None:
        if answer is None:
            self._append(p.rec, p.parakeet, p.whisper, by="claude", skipped="judge-unsure")
            return
        judged = " ".join(answer.split())
        if max(similar(judged, p.parakeet), similar(judged, p.whisper)) < MIN_SIMILARITY:
            # Not drawn from the two transcripts (the prompt forbids rewording, but only the prompt does).
            self._append(p.rec, p.parakeet, p.whisper, judged, by="claude", skipped="judge-unrelated")
            return
        self._note(p.rec, [x for x in learn.propose(p.parakeet, judged) if x.checked], judged, now)
        self._append(p.rec, p.parakeet, p.whisper, judged, by="claude")

    def _learn_known(self, p: _Pending, known: set[str], now: str) -> None:
        self._note(p.rec, judge.known_term_fixes(p.parakeet, p.whisper, known), p.whisper, now)
        self._append(p.rec, p.parakeet, p.whisper, by="known-terms")

    # --- writing -------------------------------------------------------------------------------------

    def _note(self, rec: recordings.Record, proposals: list[learn.Proposal], text: str, now: str) -> None:
        if proposals:
            became = self._store.note(rec.id, proposals, list(self._user_entries()), text[:EXAMPLE_CHARS], now)
            self._learned += len(became)

    def _append(self, rec: recordings.Record, parakeet: str, whisper: str | None, judged: str | None = None, *,
                matched: bool = False, by: str | None = None, skipped: str | None = None) -> None:
        self._store.append_checked({"id": rec.id, "time": rec.time, "exe": rec.exe, "parakeet": parakeet,
                                    "whisper": whisper, "judged": judged, "matched": matched,
                                    "judge": by, "skipped": skipped})
        self._checked += 1

    def _status(self, text: str) -> None:
        try:
            self._store.set_status(text)
        except (OSError, TimeoutError) as e:
            log.warning("background check: status not saved: %s", e)

    def _finish(self, stopped: bool) -> str:
        n, m = self._checked, self._learned
        summary = f"Checked {n} dictation{'' if n == 1 else 's'}"
        if m:
            summary += f", learned {m} fix{'' if m == 1 else 'es'}"
        if n:
            self._status(summary)
        log.info("background check: %s%s", summary, " (stopped)" if stopped else "")
        return summary + (" (stopped)" if stopped else "")
