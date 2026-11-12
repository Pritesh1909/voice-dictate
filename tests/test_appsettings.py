import pytest

from parakeet_dictate import config
from parakeet_dictate.appsettings import settings_for
from parakeet_dictate.commands import STOP

APPS = """
[output]
method = "type"
add_trailing_space = true
[commands]
enabled = true
[apps."WindowsTerminal.exe"]
commands = false
add_trailing_space = false
[apps."idea64"]
method = "paste"
type_while_speaking = false
press_enter = true
"""


def test_defaults_for_new_sections():
    c = config.parse("")
    assert c.commands_enabled and ("stop listening", STOP) in c.commands
    assert c.context_enabled and c.context_window_title and c.context_max_terms == 300
    assert c.context_refresh_minutes == 10 and c.context_folders
    assert c.apps == ()


def test_user_command_table_replaces_the_default_one():
    c = config.parse('[commands.phrases]\n"Next Line!" = "newline"\n')
    assert c.commands == (("next line", "newline"),)


def test_context_settings():
    c = config.parse('[context]\nenabled = false\nfolders = ["%APPDATA%\\\\notes"]\nmax_terms = 0\n')
    assert not c.context_enabled and c.context_max_terms == 0 and "%APPDATA%" not in c.context_folders[0]


def test_settings_for_app_overrides():
    c = config.parse(APPS)
    term = settings_for("windowsterminal.EXE", c)
    assert (term.method, term.add_trailing_space, term.commands, term.app) == ("type", False, False, "windowsterminal")
    assert term.type_while_speaking == c.type_while_speaking
    idea = settings_for("IDEA64.exe", c)
    assert (idea.method, idea.type_while_speaking, idea.press_enter, idea.commands) == ("paste", False, True, True)
    other = settings_for("notepad.exe", c)
    assert (other.method, other.add_trailing_space, other.commands, other.app) == ("type", True, True, "")
    assert settings_for("", c) == other


@pytest.mark.parametrize("bad", [
    '[commands.phrases]\n"new line" = "teleport"',
    '[commands.phrases]\n"..." = "newline"',
    '[commands]\nenabled = "yes"',
    '[context]\nmax_terms = 501',
    '[context]\nrefresh_minutes = 0',
    '[context]\nfolders = "D:\\\\notes"',
    '[apps."x.exe"]\nmethod = "shout"',
    '[apps."x.exe"]\nfont = "big"',
    '[apps."x.exe"]\ncommands = 1',
    '[apps]\n"x.exe" = 3',
])
def test_bad_new_values_are_reported(bad):
    with pytest.raises(config.ConfigError):
        config.parse(bad)


def test_paths_for_new_files(tmp_path):
    p = config.Paths(tmp_path / "r", tmp_path / "l")
    assert p.learned_file == tmp_path / "r" / "learned.toml"
    assert p.context_cache == tmp_path / "l" / "context_terms.json"
