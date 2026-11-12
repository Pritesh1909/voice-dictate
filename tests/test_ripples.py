"""Voice ripples round the button: more and stronger rings when louder, one at the start of a word."""
from parakeet_dictate.overlay import (RIPPLE_LIFE, RIPPLE_MAX, ButtonPainter, RippleEmitter, ripple_look,
                                      ripple_rate)


def run(levels, dt=0.07, start=0.0):
    e = RippleEmitter()
    born = []
    emit = e._emit
    e._emit = lambda t, s: (born.append(t), emit(t, s))
    for i, v in enumerate(levels):
        e.step(start + i * dt, v)
    return e, len(born)


def test_louder_means_more_rings():
    _, quiet = run([0.02] * 60)                  # ~4 s of silence
    _, normal = run([0.4] * 60)
    _, loud = run([0.95] * 60)
    assert quiet < normal < loud
    assert quiet <= 4 and loud >= 15


def test_rate_and_look_follow_the_level():
    assert ripple_rate(0) < ripple_rate(.5) < ripple_rate(1)
    soft, strong = ripple_look(.4, .1), ripple_look(.4, .9)
    assert strong[0] > soft[0] and strong[1] > soft[1] and strong[2] > soft[2]   # further, brighter, thicker
    sc0, op0, _ = ripple_look(0, .5)
    sc1, op1, _ = ripple_look(RIPPLE_LIFE, .5)
    assert sc0 == 1.0 and sc1 > sc0 and op1 == 0.0                              # spreads out and fades


def test_a_word_starting_sends_a_ring_at_once():
    e = RippleEmitter()
    t = 0.0
    for _ in range(10):                          # quiet
        e.step(t, .02)
        t += .07
    n = len(e.rings)
    e.step(t, .7)                                # a word starts
    assert len(e.rings) == n + 1 and e.rings[-1][1] >= .7


def test_rings_end_and_stay_few():
    e, _ = run([1.0] * 200)
    assert len(e.rings) <= RIPPLE_MAX
    last = 200 * .07
    e.step(last + RIPPLE_LIFE + .1, 0.0)
    assert all(last + RIPPLE_LIFE + .1 - b < RIPPLE_LIFE for b, _ in e.rings)


def test_ring_memory_only_while_listening():
    p = ButtonPainter(1.0)
    for i in range(40):
        p.listening(i * .07, .8)
    assert p._ripple_shapes
    p.ready()
    assert not p._ripple_shapes
