"""Spoken numbers as digits ([cleanup] numbers): only where the meaning is clearly a number."""
import pytest

from parakeet_dictate import config, numbers
from parakeet_dictate.commands import CommandMatcher
from parakeet_dictate.output import PhraseTyper
from parakeet_dictate.textclean import Cleaner

CFG = config.parse("")
CLEANER = Cleaner.from_config(CFG)


@pytest.mark.parametrize("raw, expected", [
    # What Parakeet v2 left as words in the TTS probe (see README "Numbers")
    ("The timeout is five minutes.", "The timeout is 5 minutes."),
    ("It takes three hours to build.", "It takes 3 hours to build."),
    ("Scale it to three replicas.", "Scale it to 3 replicas."),
    ("Run two instances of the worker.", "Run 2 instances of the worker."),
    ("We run Java Twenty-One in production.", "We run Java 21 in production."),
    ("The meeting is at 10.30.", "The meeting is at 10:30."),
    ("Let us meet at 3.15 p.m.", "Let us meet at 3:15 p.m."),
    ("The stand-up is at 9.15.", "The stand-up is at 9:15."),
    ("The temperature fell to minus 5 degrees.", "The temperature fell to -5 degrees."),
    ("The offset is negative 10.", "The offset is -10."),
    ("Fix SEC12 today.", "Fix SEC-12 today."),
    ("Close Net 03 first.", "Close NET-03 first."),
    ("Work on Net12 next.", "Work on NET-12 next."),
    ("See D one eight four for details.", "See D-184 for details."),
    # The same sentences fully in words (a voice the model writes fewer digits for)
    ("CPU usage hit twenty five percent last night.", "CPU usage hit 25% last night."),
    ("About ninety nine point nine percent uptime.", "About 99.9% uptime."),
    ("The file is ten megabytes.", "The file is 10 megabytes."),
    ("Each chunk is five hundred twelve KB.", "Each chunk is 512 KB."),
    ("The request took two hundred fifty ms.", "The request took 250 ms."),
    ("Upgrade to version two point five.", "Upgrade to version 2.5."),
    ("Use v two of the API.", "Use v2 of the API."),
    ("Move to Spring Boot three point two.", "Move to Spring Boot 3.2."),
    ("Install Python three point thirteen.", "Install Python 3.13."),
    ("Start it on port six zero four zero.", "Start it on port 6040."),
    ("Start it on port sixty forty.", "Start it on port 6040."),
    ("The CRM listens on port six oh three zero.", "The CRM listens on port 6030."),
    ("Open port eight thousand eighty.", "Open port 8080."),
    ("Run Ubuntu twenty two point oh four.", "Run Ubuntu 22.04."),
    ("Version two point zero point one is out.", "Version 2.0.1 is out."),
    ("The value is zero point seven five.", "The value is 0.75."),
    ("My number is five five five one two three four.", "My number is 5551234."),
    ("Go to page twelve, line forty two.", "Go to page 12, line 42."),
    ("Give it five hundred and twelve megabytes.", "Give it 512 megabytes."),
    ("Wait two to three minutes.", "Wait 2 to 3 minutes."),
    ("Close NET zero three first.", "Close NET-03 first."),
    ("Look at ticket SEC dash twelve.", "Look at ticket SEC-12."),
    ("The meeting is at ten thirty.", "The meeting is at 10:30."),
    ("Meet at nine oh five tomorrow.", "Meet at 9:05 tomorrow."),
    ("Twenty five percent of users.", "25% of users."),         # no "25% Of users"
    ("Five minutes later it failed.", "5 minutes later it failed."),
    # Spacing of digits the model wrote
    ("It hit 25 %.", "It hit 25%."),
    ("It hit 25 percent.", "It hit 25%."),
    ("The image is 10 MB and the log is 2GB.", "The image is 10 MB and the log is 2GB."),
])
def test_converted(raw, expected):
    assert CLEANER.clean(raw) == expected


@pytest.mark.parametrize("raw", [
    "This is one of the best options.",
    "We need a couple of changes.",
    "No one has checked it yet.",
    "Two things are broken today.",
    "One more thing.",
    "Zero errors today.",
    "The answer is zero.",
    "This is the first step.",
    "He finished twenty-first in the race.",
    "He finished twenty first in the race.",
    "I want to go to the store too.",
    "Send two copies to the team.",
    "This is for the four of us.",
    "Once we merge it, run it once more.",
    "It is a one-to-one mapping.",
    "It is a one to one mapping.",
    "I have two or three ideas.",
    "Retry three times then give up.",
    "Wait one second.",
    "Give me a second.",
    "About a hundred users signed up.",
    "We have one hundred and twenty users.",       # "users" is no unit
    "The first one is fine.",
    "Pick any one of them.",
    "Call me at nine.",
    "It shipped in twenty twenty four.",           # years are left to the model (it writes 2024)
    "Pay within net 30 days.",                     # lower-case "net" is a word
    "Ten minus five is five.",                     # a sum, not a negative number
    "Priced at 1.25 per hour.",                    # no time: "per" follows
    "Use HTTP2 and EC2.",
    "One two, buckle my shoe.",                    # two digits alone: not a code
    "Upgrade to JDK21.",
    # already digits: as the model wrote them
    "It costs about $1,500.",
    "There are 3,500 rows.",
    "Allocate 2.5GB of memory.",
    "Use V2 of the API.",
    "The ticket is SEC-04.",
    "Set the cron to run at 2am.",
    "Wait 2-3 minutes.",
])
def test_left_alone(raw):
    assert CLEANER.clean(raw) == raw


def test_setting_off_and_validation():
    assert CFG.numbers is True and CFG.id_prefixes == ("SEC", "NET", "D")
    off = Cleaner.from_config(config.parse("[cleanup]\nnumbers = false"))
    assert off.clean("Scale it to three replicas.") == "Scale it to three replicas."
    assert not off.may_continue("Wait twenty")
    assert config.parse('[cleanup]\nid_prefixes = ["ops", "Ops"]').id_prefixes == ("OPS",)
    ops = Cleaner.from_config(config.parse('[cleanup]\nid_prefixes = ["OPS"]'))
    assert ops.clean("Fix OPS seven and SEC twelve.") == "Fix OPS-7 and SEC twelve."
    with pytest.raises(config.ConfigError):
        config.parse("[cleanup]\nnumbers = 'yes'")
    with pytest.raises(config.ConfigError):
        config.parse('[cleanup]\nid_prefixes = ["SEC-"]')


def test_only_for_english():
    assert Cleaner(languages=("de",)).clean("Port six zero four zero.") == "Port six zero four zero."
    assert Cleaner(languages=("en-US",)).clean("Port six zero four zero.") == "Port 6040."


def test_number_words_are_never_stutters():
    c = Cleaner(numbers=False)
    assert c.clean("It shipped in twenty twenty four.") == "It shipped in twenty twenty four."
    assert c.clean("Call five five five.") == "Call five five five."
    assert c.clean("Open the the door.") == "Open the door."


@pytest.mark.parametrize("text, held", [
    ("Wait twenty", True), ("Wait twenty five", True), ("Upgrade to version two", True),
    ("Upgrade to version 2 point", True), ("Hit 25", True), ("Hit 25 per", True), ("It fell to minus", True),
    ("Use v", True), ("Fix SEC", True), ("Fix SEC dash", True), ("port six oh", True), ("two to", True),
    ("Wait twenty,", False), ("Wait twenty.", False), ("Fix sec", False), ("go to", False), ("the dash", False),
    ("open the door", False), ("one of", False), ("", False),
])
def test_may_continue(text, held):
    assert numbers.may_continue(text, CFG.id_prefixes) is held
    assert CLEANER.may_continue(text) is held


class Screen:
    def __init__(self):
        self.text, self.log = "", []

    def send(self, text, method):
        self.text += text
        self.log.append(("type", text))

    def backspace(self, n):
        self.text = self.text[:-n] if n else self.text
        self.log.append(("bs", n))


def _typer(screen):
    return PhraseTyper(CLEANER.clean, "type", True, send=screen.send, mods=lambda: False,
                       matcher=CommandMatcher(CFG.commands), backspace_keys=screen.backspace,
                       can_backspace=lambda: True, stream=True, may_continue=CLEANER.may_continue)


@pytest.mark.parametrize("previews, final, typed", [
    (["Wait twenty.", "Wait twenty five.", "Wait twenty five percent."], "Wait twenty five percent.",
     "Wait 25%. "),
    (["Upgrade to version two.", "Upgrade to version two point.", "Upgrade to version two point five now."],
     "Upgrade to version two point five now.", "Upgrade to version 2.5 now. "),
    (["Fix SEC.", "Fix SEC dash.", "Fix SEC dash twelve.", "Fix SEC dash twelve today."],
     "Fix SEC dash twelve today.", "Fix SEC-12 today. "),
    (["Open port six.", "Open port six zero.", "Open port six zero four zero.", "Open port six zero four zero now."],
     "Open port six zero four zero now.", "Open port 6040 now. "),
    (["It fell to minus.", "It fell to minus five.", "It fell to minus five degrees."],
     "It fell to minus five degrees.", "It fell to -5 degrees. "),
])
def test_streaming_never_types_a_number_word_it_must_take_back(previews, final, typed):
    s = Screen()
    t = _typer(s)
    for p in previews:
        t.preview(p)
    t.add(final)
    t.pump()
    assert s.text == typed
    assert all(kind == "type" for kind, _ in s.log)    # no Backspace needed


# --- end to end with the real model (Windows text-to-speech -> Parakeet v2 -> clean-up) --------------

@pytest.mark.slow
def test_numbers_end_to_end(tmp_path):
    from test_engine_slow import _model, _tts

    from parakeet_dictate.audio import preprocess
    from parakeet_dictate.engine import Engine, EngineSpec
    cases = {
        "Scale it to three replicas.": "Scale it to 3 replicas.",
        "The timeout is five minutes.": "The timeout is 5 minutes.",
        "Start it on port six zero four zero.": "Start it on port 6040.",
        "Fix SEC twelve today.": "Fix SEC-12 today.",
        "This is one of the best options.": "This is one of the best options.",
    }
    e = Engine(EngineSpec.for_model(_model(), CFG.threads, device="auto", vocabulary=CFG.hotwords), unload_after=0)
    try:
        for i, (said, want) in enumerate(cases.items()):
            raw, _ = e.transcribe(preprocess(_tts(said, tmp_path / f"n{i}.wav")))
            got = CLEANER.clean(raw)
            print(f"\nraw:   {raw}\nclean: {got}")
            assert got == want
    finally:
        e.unload()
