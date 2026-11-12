"""Dictionary terms per program ([dictionary.apps]) and the foreground-program helper."""
from types import SimpleNamespace as NS

import pytest

from parakeet_dictate import config, winutil
from parakeet_dictate.app import App
from parakeet_dictate.textclean import Cleaner

APPS = '''
[dictionary.apps."Code.exe"]
terms = ["useAudioCapture", "Mail C"]
[dictionary.apps."Code.exe".heard_as]
"kubectl" = ["cube control", "cube cuddle"]
[dictionary.apps.WindowsTerminal]
terms = ["pnpm"]
'''


def test_app_dictionary_settings():
    c = config.parse(APPS)
    apps = dict(c.app_dictionaries)
    assert apps["code.exe"] == (("useAudioCapture", ""), ("Mail C", ""), ("kubectl", ""),
                                ("kubectl", "cube control"), ("kubectl", "cube cuddle"))
    assert apps["windowsterminal.exe"] == (("pnpm", ""),)      # ".exe" added when left out
    assert ("Keycloak", "") in c.dictionary                     # the personal dictionary is unchanged
    assert config.parse("").app_dictionaries == ()


@pytest.mark.parametrize("bad", ['[dictionary]\napps = 3', '[dictionary.apps]\n"code.exe" = ["x"]',
                                 '[dictionary.apps."code.exe"]\nterms = "x"',
                                 '[dictionary.apps."code.exe"]\nheard_as = 1',
                                 '[dictionary.apps."code.exe".heard_as]\n"X" = 3',
                                 '[dictionary.apps." "]\nterms = ["x"]'])
def test_bad_app_dictionaries_are_reported(bad):
    with pytest.raises(config.ConfigError):
        config.parse(bad)


def test_app_key():
    assert config.app_key("Code.exe") == config.app_key("code") == config.app_key(r"C:\VS Code\CODE.EXE") == "code.exe"
    assert config.app_key("") == ""


def test_app_terms_win_over_personal_ones():
    c = config.parse(APPS)
    # The personal heard_as turns "mail c" into Mailzzy; in VS Code "Mail C" is a term of its own.
    assert Cleaner.from_config(c).clean("Open the mail c file.") == "Open the Mailzzy file."
    code = Cleaner.from_config(c, app_terms=dict(c.app_dictionaries)["code.exe"])
    assert code.clean("Open the mail c file with cube control.") == "Open the Mail C file with kubectl."
    assert code.clean("Use audio capture in key cloak.") == "useAudioCapture in Keycloak."


def _app(cfg_text="", languages=("en",)):
    ns = NS(cfg=config.parse(cfg_text), model=NS(languages=languages))
    App._make_cleaner(ns, ns.cfg)
    return ns


def test_cleaner_for_matches_the_program_case_insensitively_and_caches():
    app = _app(APPS)
    code = App.cleaner_for(app, "CODE.exe")
    assert code.clean("cube cuddle get pods") == "kubectl get pods"
    assert App.cleaner_for(app, "code.exe") is code
    other = App.cleaner_for(app, "notepad.exe")
    assert other is not code
    assert other.clean("cube cuddle get pods") == "cube cuddle get pods"
    App._make_cleaner(app, app.cfg)                       # new settings: built again
    assert App.cleaner_for(app, "code.exe") is not code


def test_cleaner_for_uses_the_dictation_language():
    app = _app('[model]\nlanguage = "de"', languages=("de", "en", "fr"))
    assert App.cleaner_for(app, "").languages == ("de",)
    app = _app('[model]\nlanguage = "de"', languages=("en",))
    assert App.cleaner_for(app, "").languages == ("en",)
    app.model = None
    assert App.cleaner_for(app, "").languages == ("de",)


def test_foreground_exe_returns_text():
    assert isinstance(winutil.foreground_exe(), str)


def test_foreground_exe_is_empty_when_windows_says_nothing(monkeypatch):
    monkeypatch.setattr(winutil, "user32", NS(GetForegroundWindow=lambda: None))
    assert winutil.foreground_exe() == ""
    monkeypatch.setattr(winutil, "user32", NS(GetForegroundWindow=lambda: 1234,
                                               GetWindowThreadProcessId=lambda hwnd, pid: 0))
    assert winutil.foreground_exe() == ""                 # no process id came back
