"""Pausing music while dictating: only what was playing is paused, and only that is played again.
Fake media sessions: no real music is touched."""
import threading
import types

from parakeet_dictate import config
from parakeet_dictate.media import MediaPauser


class Op:
    def __init__(self, result=True):
        self.result = result

    def get(self):
        return self.result


class FakeSession:
    def __init__(self, name, status="playing", takes_commands=True, delay=0.0):
        self.source_app_user_model_id, self.status, self.takes = name, status, takes_commands
        self.delay = delay      # like a real app: the pause shows in the status a moment later
        self.calls = []
        self.drop_plays = 0

    def try_pause_async(self):
        self.calls.append("pause")
        if self.takes:
            if self.delay:
                threading.Timer(self.delay, setattr, (self, "status", "paused")).start()
            else:
                self.status = "paused"
        return Op(self.takes)

    def try_play_async(self):
        self.calls.append("play")
        if self.drop_plays:
            self.drop_plays -= 1        # accepted, but nothing happens (a play straight after a pause)
        else:
            self.status = "playing"
        return Op(True)


def pauser(*sessions):
    manager = types.SimpleNamespace(get_sessions=lambda: list(sessions))
    return MediaPauser(manager, playing=lambda s: s.status == "playing", paused=lambda s: s.status == "paused",
                       gap=0.05, settle=0.2)


def test_pauses_what_plays_and_plays_it_again():
    music, video = FakeSession("Spotify"), FakeSession("Chrome")
    m = pauser(music, video)
    m.pause()
    m.wait()
    assert music.status == video.status == "paused"
    m.resume()
    m.wait()
    assert music.status == video.status == "playing"


def test_quick_tap_still_gets_the_music_back():
    slow = FakeSession("Amazon.Music", delay=0.1)
    m = pauser(slow)
    m.pause()
    m.resume()      # before the pause shows in its status
    m.wait()
    assert slow.calls == ["pause", "play"] and slow.status == "playing"


def test_play_is_asked_again_when_the_player_drops_it():
    dropper = FakeSession("Amazon.Music")
    dropper.drop_plays = 1
    m = pauser(dropper)
    m.pause()
    m.resume()
    m.wait()
    assert dropper.calls == ["pause", "play", "play"] and dropper.status == "playing"


def test_music_you_paused_yourself_stays_paused():
    mine = FakeSession("Spotify", status="paused")
    m = pauser(mine)
    m.pause()
    m.resume()
    m.wait()
    assert mine.status == "paused" and mine.calls == []


def test_not_played_again_if_you_played_or_stopped_it_meanwhile():
    a, b = FakeSession("A"), FakeSession("B")
    m = pauser(a, b)
    m.pause()
    m.wait()
    a.status = "playing"    # you pressed play during the dictation
    b.status = "stopped"    # or closed the track
    m.resume()
    m.wait()
    assert a.calls == ["pause"] and b.calls == ["pause"] and b.status == "stopped"


def test_a_session_that_refuses_is_not_played_later():
    stubborn = FakeSession("Game", takes_commands=False)
    m = pauser(stubborn)
    m.pause()
    m.resume()
    m.wait()
    assert stubborn.calls == ["pause"]


def test_resume_without_pause_and_errors_do_nothing():
    broken = FakeSession("X")
    broken.try_pause_async = lambda: (_ for _ in ()).throw(OSError("app closed"))
    m = pauser(broken)
    m.resume()
    m.pause()
    m.resume()
    m.wait()
    assert broken.status == "playing"


def test_no_media_sessions_service():
    m = MediaPauser(types.SimpleNamespace(get_sessions=lambda: []))
    m.pause()
    m.resume()
    m.wait()


def test_setting_default_and_off():
    assert config.parse("").pause_media is True
    assert config.parse("[audio]\npause_media = false").pause_media is False
