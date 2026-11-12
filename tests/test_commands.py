import pytest

from parakeet_dictate import config
from parakeet_dictate.commands import (NEWLINE, PARAGRAPH, STOP, UNDO, CommandMatcher, Transcript, compose,
                                       normalize_phrase)

DEFAULT = config.parse("").commands


@pytest.fixture
def m():
    return CommandMatcher(DEFAULT)


@pytest.mark.parametrize("phrase, action", [
    ("New line.", NEWLINE), ("new line", NEWLINE), ("NEW LINE!", NEWLINE), ("  New   line , ", NEWLINE),
    ("New paragraph.", PARAGRAPH), ("Scratch that.", UNDO), ("Delete that!", UNDO),
    ("Stop listening.", STOP),
])
def test_whole_phrase_is_a_command(m, phrase, action):
    assert m.match(phrase) == action


@pytest.mark.parametrize("phrase", [
    "I added a new line to the file.", "New line please.", "Scratch that idea.", "", "Newline",
    "stop listening to him",
])
def test_ordinary_dictation_is_not_a_command(m, phrase):
    assert m.match(phrase) is None


def test_disabled_and_unknown_actions():
    assert CommandMatcher(DEFAULT, enabled=False).match("New line.") is None
    assert CommandMatcher({"beam me up": "teleport"}).match("Beam me up.") is None
    assert CommandMatcher({"Next Line": "newline"}).match("next line") == NEWLINE


def test_normalize_phrase():
    assert normalize_phrase("Scratch that!") == "scratch that"
    assert normalize_phrase("don't—stop") == "don't stop"
    assert normalize_phrase("...") == ""


def test_transcript_spaces_go_in_front_and_trailing_space_at_the_end():
    t = Transcript(trailing_space=True)
    t.add_text("Hello.")
    assert t.flush() == "Hello."
    t.add_text("World.")
    t.add_break()
    t.add_text("Next.")
    assert t.flush() == " World.\nNext."
    assert t.finish() == " "
    assert t.finish() == ""                      # only once
    assert t.text == "Hello. World.\nNext."


def test_no_trailing_space_after_a_break_or_when_off():
    t = Transcript(trailing_space=True)
    t.add_text("A.")
    t.add_break(2)
    assert t.finish() == "A.\n\n"
    off = Transcript(trailing_space=False)
    off.add_text("A.")
    assert off.finish() == "A."


def test_undo_counts_exactly_what_was_sent():
    t = Transcript()
    t.add_text("Deploy core.")
    t.add_text("Then CRM.")
    t.flush()
    assert t.undo(can_backspace=True) == len(" Then CRM.")
    t.add_text("Then the ALB.")
    assert t.flush() == " Then the ALB."           # the space before it is worked out again
    assert t.text == "Deploy core. Then the ALB."
    t.add_break()
    t.flush()
    assert t.undo(True) == 1                         # a line break is one Backspace


def test_undo_not_allowed_leaves_typed_text_alone():
    t = Transcript()
    t.add_text("Kept.")
    t.flush()
    assert t.undo(can_backspace=False) == 0 and t.text == "Kept."


def test_undo_of_unsent_piece_needs_no_keys():
    t = Transcript()
    t.add_text("Sent.")
    t.flush()
    t.add_text("Held back while Ctrl was down.")
    assert t.undo(can_backspace=False) == 0
    assert t.flush() == "" and t.text == "Sent."
    assert Transcript().undo(True) == 0                # nothing to undo


def test_first_text_after_undoing_everything_has_no_space():
    t = Transcript()
    t.add_text("Gone.")
    t.flush()
    assert t.undo(True) == 5
    t.add_text("Fresh.")
    assert t.flush() == "Fresh."


def test_compose_whole_dictation(m):
    phrases = ["First point.", "New line.", "Second point.", "Wrong bit.", "Scratch that.", "Third.",
               "New paragraph.", "Done.", "Stop listening.", "Never typed."]
    text, stop = compose(phrases, m)
    assert text == "First point.\nSecond point. Third.\n\nDone."
    assert stop
    assert compose(["Just text."], m) == ("Just text.", False)
    assert compose(["Scratch that."], m) == ("", False)
