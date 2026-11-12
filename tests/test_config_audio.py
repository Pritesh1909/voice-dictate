import numpy as np
import pytest

from parakeet_dictate import config
from parakeet_dictate.audio import SR, preprocess, resample
from parakeet_dictate.hotkey import HotkeyError, parse


def test_defaults_parse():
    c = config.parse("")
    assert c.hotkey == "ctrl+q"
    assert c.model_dir == "auto"
    assert "%APPDATA%" not in config.parse('[model]\ndir = "%APPDATA%\\\\x"').model_dir
    assert ("Keycloak", "") in c.dictionary and ("Mailzzy", "mail sey") in c.dictionary
    assert c.vad == "auto" and c.type_while_speaking
    assert c.output_method == "type"


def test_user_values_override_and_replacements_replace():
    c = config.parse('[model]\nthreads = 2\n[cleanup.replacements]\n"foo bar" = "FooBar"\n')
    assert c.threads == 2
    assert c.replacements == (("foo bar", "FooBar"),)   # user table replaces the default table
    assert c.fillers                                      # untouched sections keep defaults


def test_dictionary_settings():
    c = config.parse('[dictionary]\nterms = ["pnpm"]\n[dictionary.heard_as]\n"Orca" = "orka"\n"Mailzzy" = ["a", "b"]\n')
    assert c.dictionary == (("pnpm", ""), ("Orca", ""), ("Orca", "orka"), ("Mailzzy", ""), ("Mailzzy", "a"),
                            ("Mailzzy", "b"))   # the user's heard_as table replaces the default one
    assert config.parse('[model]\nvad = "OFF"').vad == "off"


@pytest.mark.parametrize("bad", ['[dictionary.heard_as]\n"X" = 3', '[model]\nthreads = "4"',
                                 "[output]\nmethod = 'shout'", "[hotkey\nkeys=1"])
def test_bad_values_are_reported(bad):
    with pytest.raises(config.ConfigError):
        config.parse(bad)


def test_hotkey_parse():
    assert parse("ctrl+alt+space") == (0x2 | 0x1, 0x20)
    assert parse("Win + Shift + D") == (0x8 | 0x4, ord("D"))
    assert parse("f9") == (0, 0x78)
    for bad in ("space", "ctrl+", "hyper+x", "ctrl+alt+nokey"):
        with pytest.raises(HotkeyError):
            parse(bad)


def _tone(seconds, amp=0.05, f=220):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


def test_preprocess_trims_normalises_and_pads():
    x = np.concatenate([np.zeros(SR * 2, np.float32), _tone(1.0, amp=0.2), np.zeros(SR * 2, np.float32)]) + 0.01
    y = preprocess(x)
    assert 1.5 * SR < y.size <= 2.45 * SR           # 1 s speech + 2x0.3 s margin + 0.8 s padding
    assert 0.85 < np.max(np.abs(y)) <= 0.91        # quiet speech raised
    assert abs(float(np.mean(y))) < 0.01            # DC removed
    very_quiet = preprocess(np.concatenate([np.zeros(SR, np.float32), _tone(1.0, amp=0.02)]))
    assert np.max(np.abs(very_quiet)) == pytest.approx(0.16, abs=0.01)  # gain capped at 8x, noise not blown up


def test_preprocess_silence_is_empty():
    assert preprocess(np.random.default_rng(1).normal(0, 0.001, SR * 3).astype(np.float32)).size == 0
    assert preprocess(np.zeros(100, np.float32)).size == 0


def test_resample_48k():
    x = _tone(1.0, f=440)
    up = np.repeat(x, 3)  # crude 48 kHz signal
    y = resample(up, 48000, SR)
    assert abs(y.size - SR) <= 1


def test_press_enter_setting():
    assert config.parse("").press_enter is False
    assert config.parse("[output]\npress_enter = true").press_enter is True
    with pytest.raises(config.ConfigError):
        config.parse("[output]\npress_enter = 'yes'")
