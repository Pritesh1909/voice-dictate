import numpy as np

from parakeet_dictate.segmenter import Segmenter

SR = 16000


def speech(sec, amp=0.2):
    t = np.arange(int(sec * SR)) / SR
    return (amp * np.sin(2 * np.pi * 200 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)


def silence(sec, amp=0.0005):
    return np.random.default_rng(0).normal(0, amp, int(sec * SR)).astype(np.float32)


def feed_in_blocks(seg, x, block=SR // 20):
    out = []
    for i in range(0, x.size, block):
        out += seg.feed(x[i:i + block])
    return out


def test_cuts_at_pause_and_keeps_all_audio():
    x = np.concatenate([speech(3), silence(1.2), speech(2.5), silence(1.0), speech(1)])
    seg = Segmenter(SR)
    parts = feed_in_blocks(seg, x)
    last = seg.finish()
    assert len(parts) == 2
    assert sum(p.size for p in parts) + last.size == x.size       # nothing lost or duplicated
    # first cut lands inside the first pause
    assert 3.0 * SR < parts[0].size < 4.2 * SR


def test_short_pauses_do_not_cut():
    x = np.concatenate([speech(3), silence(0.4), speech(3), silence(0.3), speech(1)])
    seg = Segmenter(SR)
    assert feed_in_blocks(seg, x) == []
    assert seg.finish().size == x.size


def test_needs_enough_speech_before_cutting():
    x = np.concatenate([speech(1.0), silence(1.5), speech(1)])  # only 1 s of speech before the pause
    seg = Segmenter(SR)
    assert feed_in_blocks(seg, x) == []


def test_other_sample_rate():
    r = 48000
    t = np.arange(3 * r) / r
    s = (0.2 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    x = np.concatenate([s, np.zeros(int(1.2 * r), np.float32), s])
    seg = Segmenter(r)
    parts = []
    for i in range(0, x.size, r // 20):
        parts += seg.feed(x[i:i + r // 20])
    assert len(parts) == 1 and 3 * r < parts[0].size < 4.2 * r
