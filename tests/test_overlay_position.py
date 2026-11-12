import pytest

from parakeet_dictate import config
from parakeet_dictate.overlay import pill_y


def test_defaults_unchanged():
    assert pill_y(0, 1000, 44, "top", -1, 1.0) == 24
    assert pill_y(0, 1000, 44, "bottom", -1, 1.0) == 1000 - 44 - 56


def test_margin_moves_it_from_the_edge_and_scales():
    assert pill_y(0, 1000, 44, "top", 64, 1.0) == 64
    assert pill_y(0, 1000, 66, "top", 64, 1.5) == 96        # 150% display scaling
    assert pill_y(40, 1000, 44, "top", 64, 1.0) == 104      # work area starting below a top taskbar
    assert pill_y(0, 1000, 44, "bottom", 10, 1.0) == 946


def test_margin_setting():
    assert config.parse("").overlay_margin == -1
    assert config.parse("[overlay]\nmargin = 64").overlay_margin == 64
    for bad in ("-5", "601", '"64"', "6.4"):
        with pytest.raises(config.ConfigError):
            config.parse(f"[overlay]\nmargin = {bad}")
