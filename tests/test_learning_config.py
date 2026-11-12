"""The [learning] settings (learn in the background from kept recordings) and the file paths that go
with them. Text only: nothing here reads the real config or touches the real data folder."""
from pathlib import Path

import pytest

from parakeet_dictate import config


def test_learning_defaults():
    c = config.parse("")
    assert c.learning_enabled is True
    assert c.keep_recordings is True
    assert c.keep_days == 14
    assert c.max_recordings_mb == 1500
    assert c.idle_minutes == 3
    assert c.use_claude is True
    assert c.claude_calls_per_day == 20


def test_learning_values_can_be_changed():
    c = config.parse("[learning]\nenabled = false\nkeep_recordings = false\nkeep_days = 365\n"
                     "max_recordings_mb = 100\nidle_minutes = 120\nuse_claude = false\nclaude_calls_per_day = 0")
    assert (c.learning_enabled, c.keep_recordings, c.keep_days) == (False, False, 365)
    assert (c.max_recordings_mb, c.idle_minutes, c.use_claude, c.claude_calls_per_day) == (100, 120, False, 0)


@pytest.mark.parametrize("bad, key", [
    ("keep_days = 0", "keep_days"),
    ("keep_days = 366", "keep_days"),
    ("max_recordings_mb = 99", "max_recordings_mb"),
    ("max_recordings_mb = 20001", "max_recordings_mb"),
    ("idle_minutes = 0", "idle_minutes"),
    ("idle_minutes = 121", "idle_minutes"),
    ("claude_calls_per_day = -1", "claude_calls_per_day"),
    ("claude_calls_per_day = 201", "claude_calls_per_day"),
    ('enabled = "yes"', "enabled"),
    ("keep_days = true", "keep_days"),
])
def test_learning_bad_values(bad, key):
    with pytest.raises(config.ConfigError) as e:
        config.parse("[learning]\n" + bad)
    assert key in str(e.value)


def test_learning_paths():
    p = config.Paths(Path("c"), Path("d"))
    assert p.recordings_dir == Path("d") / "recordings"
    assert p.learning_dir == Path("d") / "learning"
    assert p.teacher_dir == Path("d") / "teacher"
    assert p.learned_auto_file == Path("c") / "learned_auto.toml"
