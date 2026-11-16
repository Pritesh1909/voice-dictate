"""Pause music and videos while you dictate, and play them again afterwards.

Uses the Windows media sessions (the ones the volume flyout shows: Spotify, Amazon Music, a YouTube tab,
Media Player, ...). Only the sessions that were playing when the recording started are paused, and only
those are played again: music you had paused yourself stays paused.

The calls run on one worker thread of their own, in order, so the hotkey never waits for them and a
resume always comes after its pause. Without the WinRT packages (or on an older Windows) it does nothing.
"""
from __future__ import annotations

import logging
import queue
import threading
import time

log = logging.getLogger(__name__)

# A player can take a moment to act on a pause, and may drop a play that comes straight after it (Amazon
# Music does): a resume is never sent sooner than GAP_SECONDS after its pause, waits up to SETTLE_SECONDS
# for the pause to show, and checks for that long that the music plays again, asking once more if not.
# So a quick tap of the hotkey still gets the music back.
GAP_SECONDS = 0.5
SETTLE_SECONDS = 1.0


def _manager():
    """The Windows media session manager, or None when it isn't available here."""
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
        return M.request_async().get()
    except Exception as e:   # ImportError, or no media sessions service
        log.info("media sessions not available, music won't be paused: %s", e)
        return None


def _playing(session) -> bool:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as S
    return session.get_playback_info().playback_status == S.PLAYING


def _paused(session) -> bool:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as S
    return session.get_playback_info().playback_status == S.PAUSED


class MediaPauser:
    """pause() when recording starts, resume() when it ends. Call from any thread; both return at once."""

    def __init__(self, manager=None, playing=_playing, paused=_paused, gap: float = GAP_SECONDS,
                 settle: float = SETTLE_SECONDS):
        self._manager = manager          # tests pass a fake; None = look it up on the worker thread
        self._looked_up = manager is not None
        self._is_playing, self._is_paused = playing, paused
        self._gap, self._settle = gap, settle
        self._ours: list = []            # (session, when) we asked to pause, to play again
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="media").start()

    def pause(self) -> None:
        self._jobs.put(self._pause)

    def resume(self) -> None:
        self._jobs.put(self._resume)

    def wait(self, timeout: float = 5.0) -> None:
        """Block until the calls asked for so far have run (tests, and quitting)."""
        done = threading.Event()
        self._jobs.put(done.set)
        done.wait(timeout)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job()
            except Exception:
                log.exception("media")

    def _wait(self, cond) -> bool:
        end = time.monotonic() + self._settle
        while not cond():
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)
        return True

    def _sessions(self) -> list:
        if not self._looked_up:
            self._manager, self._looked_up = _manager(), True
        return list(self._manager.get_sessions()) if self._manager is not None else []

    def _pause(self) -> None:
        for s in self._sessions():
            try:
                if any(s == o for o, _ in self._ours) or not self._is_playing(s):
                    continue
                # Kept even when the answer is "no": whether it paused shows in its status at resume.
                self._ours.append((s, time.monotonic()))
                ok = s.try_pause_async().get()
                log.info("paused media: %s%s", s.source_app_user_model_id, "" if ok else " (refused)")
            except Exception as e:   # the app closed meanwhile, or doesn't take commands
                log.info("couldn't pause media: %s", e)

    def _resume(self) -> None:
        ours, self._ours = self._ours, []
        for s, at in ours:
            try:
                time.sleep(max(0.0, at + self._gap - time.monotonic()))
                if not self._wait(lambda: not self._is_playing(s)):
                    continue             # still playing: you played it yourself meanwhile, or it refused
                if not self._is_paused(s):
                    continue             # stopped or closed: not started again
                for _ in range(2):       # once more if the player dropped the first one
                    s.try_play_async().get()
                    if self._wait(lambda: self._is_playing(s)):
                        break
                log.info("resumed media: %s%s", s.source_app_user_model_id,
                         "" if self._is_playing(s) else " (it didn't start)")
            except Exception as e:
                log.info("couldn't resume media: %s", e)
