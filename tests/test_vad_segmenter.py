import numpy as np

from parakeet_dictate.segmenter import PhraseChunker, VadSegmenter

SR, F = 16000, 512


def scored(pattern: str):
    """Audio + scores from a pattern: '#' = speech frame (0.9), '.' = silence (0.05), '-' = unsure (0.4).
    Each frame's samples hold its index, so tests can check which audio landed where."""
    probs = [{"#": 0.9, ".": 0.05, "-": 0.4}[c] for c in pattern]
    audio = np.concatenate([np.full(F, i, np.float32) for i in range(len(pattern))])
    return audio, probs


def frames(x):
    return sorted(set(int(v) for v in x))


def feed(seg, audio, probs, block=1000):
    """Feed in uneven blocks; each frame's score arrives once all its samples are in (like the detector)."""
    out, sent = [], 0
    for i in range(0, audio.size, block):
        x = audio[i:i + block]
        done = (i + x.size) // F
        out += seg.push(x, probs[sent:done])
        sent = done
    return out, probs[sent:]


def test_two_phrases_split_at_pause_with_preroll():
    # 30 silent, 20 speech, 25 silent (0.8 s > 0.6 s pause), 20 speech, 5 silent
    pat = "." * 30 + "#" * 20 + "." * 25 + "#" * 20 + "." * 5
    audio, probs = scored(pat)
    seg = VadSegmenter(F, pause_seconds=0.6)
    out, rest = feed(seg, audio, probs)
    out += seg.flush(rest)
    assert len(out) == 2
    first = frames(out[0])
    preroll = int(np.ceil(0.4 * SR / F))          # ~13 frames of lead-in kept
    assert first[0] in (30 - preroll, 30 - preroll + 1) and 30 in first and 49 in first
    assert 75 in frames(out[1]) and 94 in frames(out[1])
    assert 5 not in first                          # silence long before speech is dropped


def test_short_pause_and_unsure_frames_do_not_cut():
    pat = "." * 5 + "#" * 20 + "." * 10 + "#" * 10 + "-" * 30 + "#" * 10
    audio, probs = scored(pat)
    seg = VadSegmenter(F, pause_seconds=0.6)
    out, rest = feed(seg, audio, probs)
    assert out == [] and seg.phrase_open
    out = seg.flush(rest)
    assert len(out) == 1 and frames(out[0])[-1] == len(pat) - 1


def test_blip_shorter_than_min_speech_does_not_end_a_phrase_on_its_own():
    pat = "." * 10 + "##" + "." * 40 + "#" * 20 + "." * 25
    audio, probs = scored(pat)
    seg = VadSegmenter(F, pause_seconds=0.6)
    out, rest = feed(seg, audio, probs)
    out += seg.flush(rest)
    assert len(out) == 1                           # the blip joins the real phrase instead of standing alone
    assert 10 in frames(out[0]) and 60 in frames(out[0])


def test_nothing_heard_gives_nothing():
    audio, probs = scored("." * 60)
    seg = VadSegmenter(F)
    out, rest = feed(seg, audio, probs)
    assert out + seg.flush(rest) == []


def test_held_audio_hands_over_everything_pending():
    audio, probs = scored("." * 5 + "#" * 10)
    seg = VadSegmenter(F)
    seg.push(audio, probs[:12])                    # last 3 frames not scored yet
    assert frames(seg.held_audio()) == list(range(0, 15))


def test_long_phrase_is_cut_near_30_s_at_its_quietest_point():
    x = np.full(SR * 35, 0.3, np.float32)
    x[SR * 27: SR * 27 + 2000] = 0.0               # a quiet spot 27 s in
    pc = PhraseChunker(SR)
    out = pc.push(x)
    assert len(out) == 1 and SR * 27 <= out[0].size <= SR * 27 + 2000
    assert out[0].size + pc.flush().size == x.size
