"""Mic meter, silence stop, missing-mic fallback and unplug detection, all without a real microphone."""
import sys
import threading
import time
import types

import numpy as np
import pytest

from parakeet_dictate import app as appmod
from parakeet_dictate import audio
from parakeet_dictate.audio import SR, LevelMeter, Recorder, silence_stop_due, silence_stop_message
from parakeet_dictate.overlay import FG, MUTED, RED, listening_look

BLOCK = SR // 20  # 50 ms


def tone(amp, f=220.0):
    t = np.arange(BLOCK) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


def feed(meter, block, start, n):
    """Feed n copies of block at 50 ms steps; returns the time after the last one."""
    t = start
    for _ in range(n):
        meter.feed(block, t)
        t += 0.05
    return t


# --- LevelMeter ---------------------------------------------------------------------------------


def test_silence_is_not_speech():
    m = LevelMeter()
    noise = np.random.default_rng(1).normal(0, 0.002, BLOCK).astype(np.float32)
    feed(m, noise, 1.0, 40)
    assert not m.speaking and m.level < 0.05 and m.last_speech_at == 0.0


def test_speech_is_speech_and_level_rises_fast():
    m = LevelMeter()
    m.feed(tone(0.1), 1.0)            # rms 0.07: one block is enough (fast attack)
    assert m.speaking and m.level > 0.1 and m.last_speech_at == 1.0
    feed(m, tone(0.1), 1.05, 10)
    assert m.level == pytest.approx((0.0707 - m.noise_floor) / 0.16, abs=0.05)


def test_level_falls_slowly():
    m = LevelMeter()
    feed(m, tone(0.2), 1.0, 10)
    high = m.level
    m.feed(np.zeros(BLOCK, np.float32), 1.5)
    assert high * 0.75 < m.level < high      # 0.2 release: one quiet block drops only a fifth


def test_clipping_holds_half_a_second_then_clears():
    m = LevelMeter()
    m.feed(np.full(BLOCK, 0.99, np.float32), 10.0)
    assert m.clipping_at(10.0) and m.clipping_at(10.49)
    feed(m, tone(0.05), 10.05, 5)
    assert m.clipping_at(10.5) and not m.clipping_at(10.51)
    m.feed(tone(0.97), 11.0)                # loud but not full scale
    assert not m.clipping_at(11.0)


def test_noise_floor_follows_a_steady_hum():
    m = LevelMeter()
    hum = tone(0.04, f=100)                 # rms 0.028, peak 0.04: a fan, not a voice
    feed(m, hum, 1.0, 200)
    assert 0.024 < m.noise_floor < 0.03
    assert not m.speaking and m.level < 0.05
    feed(m, np.zeros(BLOCK, np.float32), 20.0, 400)
    assert m.noise_floor == pytest.approx(0.004)   # never below the minimum


def test_peak_alone_counts_as_speech():
    m = LevelMeter()
    click = np.zeros(BLOCK, np.float32)
    click[10] = 0.3
    m.feed(click, 2.0)
    assert m.level < 0.1 and m.speaking


# --- silence stop -------------------------------------------------------------------------------


def test_silence_stop_decision():
    assert not silence_stop_due(0.0, 100.0, 1e9, 0)          # 0 = off
    assert not silence_stop_due(0.0, 100.0, 159.9, 60)       # nothing said: counted from the start
    assert silence_stop_due(0.0, 100.0, 160.0, 60)
    assert not silence_stop_due(130.0, 100.0, 189.0, 60)     # counted from the last speech
    assert silence_stop_due(130.0, 100.0, 190.0, 60)


def test_silence_stop_message():
    assert silence_stop_message(60) == "Stopped after a minute of silence."
    assert silence_stop_message(120) == "Stopped after 2 minutes of silence."
    assert silence_stop_message(45) == "Stopped after 45 seconds of silence."


# --- Recorder with a fake sounddevice -----------------------------------------------------------


class FakeStream:
    instances: list = []

    def __init__(self, samplerate, channels, dtype, device, blocksize, callback, finished_callback):
        self.samplerate, self.device, self.callback, self.finished = samplerate, device, callback, finished_callback
        self.active = False
        self.fail_stop = False
        FakeStream.instances.append(self)

    def start(self):
        self.active = True

    def stop(self):
        if self.fail_stop:
            raise RuntimeError("Unanticipated host error")
        self.active = False

    def close(self):
        if self.fail_stop:
            raise RuntimeError("Unanticipated host error")

    def push(self, block):
        self.callback(block.reshape(-1, 1), block.size, None, 0)


DEVICES = [{"name": "Speakers (Realtek)", "max_input_channels": 0, "default_samplerate": 48000.0},
           {"name": "Microphone Array (Intel)", "max_input_channels": 2, "default_samplerate": 48000.0},
           {"name": "Headset Microphone (Jabra)", "max_input_channels": 1, "default_samplerate": 16000.0}]


@pytest.fixture
def fake_sd(monkeypatch):
    devices = list(DEVICES)

    def query_devices(device=None, kind=None):
        if device is None and kind is None:
            return devices
        return {"default_samplerate": 48000.0}

    sd = types.SimpleNamespace(query_devices=query_devices, InputStream=FakeStream, devices=devices)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    FakeStream.instances = []
    return sd


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_chosen_mic_is_used_when_present(fake_sd):
    r = Recorder("headset")
    r.start()
    assert FakeStream.instances[-1].device == 2 and r.warning == ""


def test_missing_mic_falls_back_to_default_with_a_warning(fake_sd):
    r = Recorder("Blue Yeti")
    r.start()
    assert FakeStream.instances[-1].device is None
    assert r.warning == "Microphone 'Blue Yeti' not found. Using the Windows default."
    r.close()
    fake_sd.devices.append({"name": "Blue Yeti", "max_input_channels": 1, "default_samplerate": 48000.0})
    r.start()                                # plugged back in: used again, warning cleared
    assert FakeStream.instances[-1].device == 3 and r.warning == ""


def test_default_mic_failing_still_raises(fake_sd, monkeypatch):
    def broken(**kw):
        raise RuntimeError("Error opening InputStream: Invalid device")
    monkeypatch.setattr(fake_sd, "InputStream", broken)
    with pytest.raises(RuntimeError):
        Recorder("Blue Yeti").start()


def test_callback_feeds_the_meter(fake_sd):
    clock = Clock()
    r = Recorder("", clock=clock)
    r.start()
    s = FakeStream.instances[-1]
    s.push(tone(0.1))
    assert r.level > 0.1 and r.meter.last_speech_at == clock.t and not r.clipping
    s.push(np.full(BLOCK, 1.0, np.float32))
    assert r.clipping
    clock.t += 0.6
    assert not r.clipping                    # clears on the clock even without new audio
    assert r.drain().size == 2 * BLOCK
    r.close()
    assert r.level == 0.0


def test_stalled_stream_is_lost(fake_sd):
    clock = Clock()
    r = Recorder("", clock=clock)
    r.start()
    s = FakeStream.instances[-1]
    clock.t += 4.0
    assert not r.lost                        # first block may take a while (Bluetooth)
    s.push(tone(0.01))
    clock.t += 1.9
    assert not r.lost
    clock.t += 0.2                           # 2.1 s since the last block
    assert r.lost
    clock.t += 1.0
    r.close()
    assert not r.lost and not r.active


def test_no_first_block_is_lost_after_five_seconds(fake_sd):
    clock = Clock()
    r = Recorder("", clock=clock)
    r.start()
    clock.t += 5.1
    assert r.lost


def test_inactive_or_ended_or_erroring_stream_is_lost(fake_sd):
    r = Recorder("", clock=Clock())
    r.start()
    s = FakeStream.instances[-1]
    assert not r.lost
    s.active = False
    assert r.lost

    r.start()
    s = FakeStream.instances[-1]
    s.finished()                             # PortAudio ended the stream on its own
    assert r.lost

    r.start()
    s = FakeStream.instances[-1]
    type(s).active = property(lambda self: (_ for _ in ()).throw(RuntimeError("PortAudio error")))
    try:
        assert r.lost
    finally:
        del type(s).active


def test_closing_a_dead_stream_never_raises(fake_sd):
    r = Recorder("", clock=Clock())
    r.start()
    FakeStream.instances[-1].fail_stop = True
    r.close()
    assert not r.active
    r.start()
    FakeStream.instances[-1].fail_stop = True
    r.cancel()
    assert not r.active


# --- overlay look -------------------------------------------------------------------------------


def test_listening_look():
    assert listening_look(0.0, False) == ("Listening", FG, MUTED, 0.0)
    assert listening_look(0.5, False) == ("Listening", FG, FG, 0.5)
    assert listening_look(3.0, False)[3] == 1.0
    label, label_col, bar_col, v = listening_look(0.1, True)
    assert label == "Too loud" and label_col == RED and bar_col == RED and v >= 0.8


# --- App._watch_release auto stops --------------------------------------------------------------


def fake_app(monkeypatch, keys_held=0, lost=False, silent_for=0.0, silence_stop=60, max_seconds=180):
    held = [keys_held]

    def key_down(vk):
        held[0] -= 1
        return held[0] >= 0

    monkeypatch.setattr(appmod, "key_down", key_down)
    rec = types.SimpleNamespace(lost=lost, meter=LevelMeter(), started_at=time.monotonic() - silent_for)
    cfg = types.SimpleNamespace(max_seconds=max_seconds, hold_threshold=0.04, silence_stop_seconds=silence_stop)
    a = types.SimpleNamespace(cfg=cfg, recorder=rec, hotkeys=types.SimpleNamespace(vk=0x51), _session=1,
                              state="recording", _lock=threading.Lock(), notes=[], finished=[])
    a._notify = a.notes.append

    def finish():
        a.finished.append(True)
        a.state = "busy"
        a._session += 1

    a._finish_locked = finish
    return a


def test_tap_mode_stops_after_silence(monkeypatch):
    a = fake_app(monkeypatch, silent_for=61)
    appmod.App._watch_release(a, 1)
    assert a.finished and a.notes == ["Stopped after a minute of silence."]


def test_recent_speech_postpones_silence_stop(monkeypatch):
    a = fake_app(monkeypatch, silent_for=61, max_seconds=0.6)
    a.recorder.meter.last_speech_at = time.monotonic()
    appmod.App._watch_release(a, 1)
    assert a.notes == ["Stopped after 0.6 s ([output] max_seconds)."]


def test_silence_stop_off(monkeypatch):
    a = fake_app(monkeypatch, silent_for=1000, silence_stop=0, max_seconds=0.3)
    appmod.App._watch_release(a, 1)
    assert a.notes == ["Stopped after 0.3 s ([output] max_seconds)."]


def test_hold_to_talk_ignores_silence(monkeypatch):
    a = fake_app(monkeypatch, keys_held=5, silent_for=1000)   # ~0.1 s hold, past the 0.04 s threshold
    appmod.App._watch_release(a, 1)
    assert a.finished and a.notes == []                        # stopped by letting go, not by silence


def test_unplugged_mic_stops_in_both_modes(monkeypatch):
    a = fake_app(monkeypatch, lost=True)
    appmod.App._watch_release(a, 1)
    assert a.finished and a.notes == [appmod.MIC_LOST]
    a = fake_app(monkeypatch, keys_held=1000, lost=True)
    appmod.App._watch_release(a, 1)
    assert a.finished and a.notes == [appmod.MIC_LOST]


def test_no_auto_stop_after_the_user_stopped(monkeypatch):
    a = fake_app(monkeypatch, silent_for=0, max_seconds=0.6)
    threading.Timer(0.1, lambda: setattr(a, "state", "busy")).start()
    appmod.App._watch_release(a, 1)
    assert not a.finished and a.notes == []
