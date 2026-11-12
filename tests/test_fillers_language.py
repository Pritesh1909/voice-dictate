"""Filler words per dictation language (ported from Orca 1.4.256) and choosing the dictation language."""
from types import SimpleNamespace as NS

import pytest

from parakeet_dictate import config
from parakeet_dictate.engine import EngineSpec, _run_language
from parakeet_dictate.models import dictated_languages, match_language
from parakeet_dictate.textclean import Cleaner, filler_rules, remove_fillers


def drop(text, languages=(), extra=()):
    return remove_fillers(text, filler_rules(languages, extra))


@pytest.mark.parametrize("lang, raw, expected", [
    ("en", "Um, I think so.", "I think so."),
    ("en", "We should, uh, ship it.", "We should ship it."),
    ("en", "We should, um.", "We should."),
    ("en", "Um.", ""),
    ("en", "So uh... we wait.", "So we wait."),
    ("en", "Hmm, maybe the other one.", "Maybe the other one."),
    ("en", "I said uh-huh to that.", "I said uh-huh to that."),
    ("en", "Open um.txt now.", "Open um.txt now."),            # a dot inside a name joins it
    ("en", "An umbrella, sir.", "An umbrella, sir."),
    ("de", "Ähm, wir sollten das machen.", "Wir sollten das machen."),
    ("de", "Wir treffen uns um acht, äh, oder neun.", "Wir treffen uns um acht oder neun."),  # "um" is German
    ("es", "Ehh, vamos a ver.", "Vamos a ver."),
    ("es", "Este, ehm, proyecto.", "Este proyecto."),
    ("es", "Eh, tú.", "Eh, tú."),                                # "eh" is a word in Spanish
    ("fr", "Euh, je pense que oui.", "Je pense que oui."),
    ("fr", "C'est, heu, compliqué.", "C'est compliqué."),
    ("it", "Ehm, non lo so.", "Non lo so."),
    ("pt", "Hã, não sei.", "Não sei."),
    ("pt", "Eu tenho um carro.", "Eu tenho um carro."),          # "um" is Portuguese
    ("nl", "Uhm, ik weet het niet.", "Ik weet het niet."),
    ("ru", "Эм, я не знаю.", "Я не знаю."),
    ("ru", "Это, хм, сложно.", "Это сложно."),
    ("ja", "えーと、今日は晴れです。", "今日は晴れです。"),
    ("ja", "あのー、すみません。", "すみません。"),
    ("ko", "음, 잘 모르겠어요.", "잘 모르겠어요."),
    ("zh", "嗯，我觉得可以。", "我觉得可以。"),
    ("zh", "呃，这个问题。", "这个问题。"),
    ("zh", "嗯。", "嗯。"),                                      # 嗯 alone is also "yes"
    ("yue", "呃，係咪。", "係咪。"),
    ("zh-CN", "呃，好的。", "好的。"),                            # regional codes use the language's list
])
def test_language_fillers(lang, raw, expected):
    assert drop(raw, [lang]) == expected


def test_unknown_language_removes_only_neutral_sounds():
    assert drop("Uh, the umm thing, hmm.") == "The thing."
    assert drop("We meet um eight, er, nine.") == "We meet um eight, er, nine."   # "um"/"er" are words elsewhere
    assert drop("Эм, привет, uh.") == "Эм, привет, uh."          # non-Latin text in an unknown language: untouched
    assert drop("Эм, привет, uh.", extra=["uh"]) == "Эм, привет."  # ... except the user's own fillers


def test_question_or_call_on_its_own_is_kept():
    assert drop("Hm?", ["en"]) == "Hm?"
    assert drop("Done. Hmm? Really.", ["en"]) == "Done. Hmm? Really."
    assert drop("Is it ready, uh?", ["en"]) == "Is it ready?"   # a filler at the end of a question still goes


def test_all_caps():
    assert drop("Take him to the ER now.", ["en"]) == "Take him to the ER now."
    assert drop("The UM campus, um, is big.", ["en"]) == "The UM campus is big."
    assert drop("UM, HELLO THERE.", ["en"]) == "HELLO THERE."   # a model writing all capitals: still a sound
    assert drop("AHH, the token.", ["en"], extra=["ah"]) == "The token."   # stretched: a sound


def test_capitalises_only_where_a_sentence_began():
    assert drop("Done. Um, next one.", ["en"]) == "Done. Next one."
    assert drop("Done. um, next one.", ["en"]) == "Done. next one."     # lower-case "um": not a sentence start
    assert drop("um, deploy it", ["en"]) == "Deploy it"                 # start of the text


def test_language_fillers_can_be_turned_off():
    assert remove_fillers("Um, hello.", filler_rules(["en"], [], language_fillers=False)) == "Um, hello."
    assert filler_rules(["en"], [], language_fillers=False) is None
    assert remove_fillers("Uhh, hello.", filler_rules(["en"], ["uh"], language_fillers=False)) == "Hello."


def test_cleaner_uses_language_and_user_fillers():
    c = Cleaner(fillers=["ah"], languages=("de",))
    assert c.clean("Ähm, ah, wir gehen um acht.") == "Wir gehen um acht."


# --- choosing the language ---------------------------------------------------------------------------

def test_match_language():
    assert match_language("de", ("en", "de")) == "de"
    assert match_language("DE", ("en", "de")) == "de"
    assert match_language("zh", ("en", "zh-CN", "zh-TW")) == "zh-CN"
    assert match_language("fr", ("en",)) is None
    assert match_language("de", ()) is None                 # a model that lists none can't be told one
    assert match_language("auto", ("en", "de")) is None


def test_dictated_languages():
    assert dictated_languages(("en",), "auto") == ("en",)
    assert dictated_languages(("en",), "de") == ("en",)     # an English-only model always writes English
    assert dictated_languages(("zh", "en"), "auto") == ("zh", "en")
    many = ("bg", "de", "en", "fr", "zh-CN")
    assert dictated_languages(many, "auto") == ()
    assert dictated_languages(many, "fr") == ("fr",)
    assert dictated_languages(many, "zh") == ("zh-CN",)
    assert dictated_languages(many, "ja") == ()             # the model can't take it: language unknown
    assert dictated_languages((), "ja") == ("ja",)          # the header doesn't say: trust the setting


def test_run_language_only_when_the_model_takes_it():
    multi = NS(capabilities=NS(languages=("en", "de", "zh-CN")))
    assert _run_language(multi, "auto") == (None, "")
    assert _run_language(multi, "de") == ("de", "")
    assert _run_language(multi, "zh") == ("zh-CN", "")
    lang, note = _run_language(NS(capabilities=NS(languages=("en",))), "de")
    assert lang is None and "ignored" in note


def test_language_setting():
    assert config.parse("").language == "auto"
    assert config.parse('[model]\nlanguage = " DE "').language == "de"
    assert config.parse('[model]\nlanguage = "yue"').language == "yue"
    for bad in ('"german"', '"d"', "3"):
        with pytest.raises(config.ConfigError):
            config.parse(f"[model]\nlanguage = {bad}")
    assert EngineSpec("m.gguf", 4).language == "auto"


def test_language_fillers_setting():
    assert config.parse("").language_fillers is True
    assert config.parse("[cleanup]\nlanguage_fillers = false").language_fillers is False
    with pytest.raises(config.ConfigError):
        config.parse('[cleanup]\nlanguage_fillers = "no"')
