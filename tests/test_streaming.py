"""Live streaming: words typed while the phrase is still spoken, then corrected by the final read.
A fake screen applies every chunk and Backspace, so the tests check exactly what the user would see."""
import numpy as np
import pytest

from parakeet_dictate import config
from parakeet_dictate.commands import CommandMatcher, Transcript, reconcile
from parakeet_dictate.output import PhraseTyper
from parakeet_dictate.overlay import fit_tail
from parakeet_dictate.segmenter import VadSegmenter
from parakeet_dictate.textclean import Cleaner

CFG = config.parse("")
CLEANER = Cleaner.from_config(CFG)


class Screen:
    def __init__(self):
        self.text, self.log = "", []

    def send(self, text, method):
        self.text += text
        self.log.append(("type", text))

    def backspace(self, n):
        self.text = self.text[:-n] if n else self.text
        self.log.append(("bs", n))


def typer(screen, can_backspace=True, mods=lambda: False, cleaner=CLEANER, trailing=True):
    return PhraseTyper(cleaner.clean, "type", trailing, send=screen.send, mods=mods,
                       matcher=CommandMatcher(CFG.commands), backspace_keys=screen.backspace,
                       can_backspace=lambda: can_backspace, stream=True, may_continue=cleaner.may_continue)


def speak(t, previews, final):
    for p in previews:
        t.preview(p)
    t.add(final)
    t.pump()


def test_words_appear_while_speaking_and_final_completes_them():
    s = Screen()
    t = typer(s)
    t.preview("I think.")
    assert s.text == ""                            # one read: nothing agreed yet
    t.preview("I think we.")
    assert s.text == "I think"                     # agreed by two reads ("think." then "think"); "we" is new
    t.preview("I think we should.")
    assert s.text == "I think we"
    assert t.tentative == "should."
    t.preview("I think we should deploy.")
    assert s.text == "I think we should"
    t.add("I think we should deploy the core service first.")
    t.pump()
    assert s.text == "I think we should deploy the core service first. "
    assert all(kind == "type" for kind, _ in s.log)    # no Backspace needed
    assert t.text == "I think we should deploy the core service first."


def test_final_read_corrects_the_end_with_backspace():
    s = Screen()
    t = typer(s)
    speak(t, ["Send it to the.", "Send it to the core.", "Send it to the core team now."], "Send it to the CRM team now.")
    assert s.text == "Send it to the CRM team now. "
    assert ("bs", len(" core")) in s.log or any(k == "bs" for k, _ in s.log)


def test_without_backspace_the_typed_words_stay_and_the_rest_is_added():
    s = Screen()
    t = typer(s, can_backspace=False)
    speak(t, ["Send it to the.", "Send it to the core.", "Send it to the core team."], "Send it to the CRM team now.")
    assert all(k == "type" for k, _ in s.log)
    assert s.text.startswith("Send it to the core") and s.text.endswith("now. ")


def test_second_phrase_gets_one_space_between():
    s = Screen()
    t = typer(s)
    speak(t, ["Deploy core.", "Deploy core first."], "Deploy core first.")
    speak(t, ["Then the.", "Then the CRM.", "Then the CRM service."], "Then the CRM service.")
    assert s.text == "Deploy core first. Then the CRM service. "


def test_a_word_that_may_start_a_dictionary_term_waits():
    s = Screen()
    t = typer(s)
    t.preview("Open the key.")
    t.preview("Open the key cloak.")
    assert s.text == "Open the"                    # "key" held: it might become "Keycloak"
    t.preview("Open the key cloak admin.")
    t.preview("Open the key cloak admin page.")
    assert s.text == "Open the Keycloak admin"
    t.add("Open the key cloak admin page.")
    t.pump()
    assert s.text == "Open the Keycloak admin page. "
    assert all(k == "type" for k, _ in s.log)


def test_a_phrase_that_could_be_a_command_is_not_streamed():
    s = Screen()
    t = typer(s)
    speak(t, ["One two three.", "One two three four."], "One two three four.")
    before = s.text
    t.preview("Scratch.")
    t.preview("Scratch that.")
    assert s.text == before                        # nothing of "scratch that" was typed
    t.add("Scratch that.")
    t.pump()
    assert s.text == ""                            # the previous phrase was taken back


def test_nothing_streams_while_a_modifier_is_held():
    s, held = Screen(), [True]
    t = typer(s, mods=lambda: held[0])
    for p in ("Hold to.", "Hold to talk.", "Hold to talk works."):
        t.preview(p)
    assert s.text == ""
    held[0] = False
    t.add("Hold to talk works.")
    t.pump()
    assert s.text == "Hold to talk works. "


def test_streamed_words_with_an_empty_final_read_are_taken_back():
    s = Screen()
    t = typer(s)
    t.preview("Okay so.")
    t.preview("Okay so then.")
    assert s.text == "Okay"
    t.add("")
    t.pump()
    assert s.text == ""


def test_reconcile():
    assert reconcile(" Send it", " Send it now. ", True) == (" Send it now. ", " now. ", 0)
    assert reconcile(" Send the core", " Send the CRM. ", True) == (" Send the CRM. ", "CRM. ", 4)
    assert reconcile(" Send the core", " Send the CRM now. ", False) == (" Send the core now. ", " now. ", 0)


def test_undo_of_a_streamed_phrase_not_yet_final():
    t = Transcript()
    t.add_text("Hello there", live="Hello")
    assert t.undo(can_backspace=True) == len("Hello")


def test_command_prefix_and_dictionary_prefix_checks():
    m = CommandMatcher(CFG.commands)
    assert m.could_start("Scratch") and m.could_start("New") and m.could_start("new line")
    assert not m.could_start("Scratch the surface")
    assert CLEANER.may_continue("open the key") and CLEANER.may_continue("the mail")
    assert not CLEANER.may_continue("open the door") and not CLEANER.may_continue("the key,")


def test_open_phrase_audio_is_readable_without_ending_it():
    seg = VadSegmenter(512)
    x = np.ones(512 * 20, np.float32)
    seg.push(x, [0.9] * 20)
    a = seg.open_audio()
    assert a.size == x.size and seg.in_speech
    assert seg.open_audio().size == x.size        # reading it again changes nothing
    assert VadSegmenter(512).open_audio().size == 0


@pytest.mark.parametrize("text, n, out", [("short", 10, "short"), ("one two three four", 10, "…hree four")])
def test_fit_tail(text, n, out):
    assert fit_tail(text, n) == out


def test_streaming_setting():
    assert CFG.stream_while_speaking is True
    assert config.parse("[model]\nstream_while_speaking = false").stream_while_speaking is False
    with pytest.raises(config.ConfigError):
        config.parse("[model]\nstream_while_speaking = 'yes'")


# --- a model that streams says which words are settled ----------------------------------------------

def test_settled_words_are_typed_at_once():
    s = Screen()
    t = typer(s)
    t.preview("I think we should", stable="I think")
    assert s.text == "I think"                     # no second read needed
    assert t.tentative == "we should"
    t.preview("I think we should deploy the", stable="I think we should deploy")
    assert s.text == "I think we should deploy"
    t.add("I think we should deploy the core service first.")
    t.pump()
    assert s.text == "I think we should deploy the core service first. "
    assert all(k == "type" for k, _ in s.log)


def test_a_settled_part_of_a_word_is_not_typed():
    s = Screen()
    t = typer(s)
    t.preview("the microservices", stable="the micro")   # "micro" is only the start of a word
    assert s.text == "the"
    t.preview("the micro", stable="the micro")            # nothing after it yet: it may still grow
    assert s.text == "the"


def test_settled_number_words_still_wait_for_the_number_to_end():
    s = Screen()
    t = typer(s)
    t.preview("Wait twenty five", stable="Wait twenty")
    t.preview("Wait twenty five percent", stable="Wait twenty five")
    t.add("Wait twenty five percent.")
    t.pump()
    assert s.text == "Wait 25%. "
    assert all(k == "type" for k, _ in s.log)
