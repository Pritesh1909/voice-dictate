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


def test_transcript_spacing_matches_plain_typing():
    t = Transcript(trailing_space=True)
    t.add_text("Hello.")
    assert t.flush() == (0, "Hello. ")
    t.add_text("World.")
    t.add_text("Again.")                      # held back by a modifier: sent together
    assert t.flush() == (0, "World. Again. ")
    off = Transcript(trailing_space=False)
    off.add_text("One.")
    assert off.flush() == (0, "One.")
    off.add_text("Two.")
    assert off.flush() == (0, " Two.")
    assert t.text == "Hello. World. Again." and off.text == "One. Two."


def test_line_break_takes_back_the_trailing_space_when_safe():
    t = Transcript(trailing_space=True)
    t.add_text("Hello.")
    t.flush()
    t.add_break()
    assert t.flush(can_backspace=True) == (1, "\n")      # no space left at the end of the line
    t.add_text("Next.")
    assert t.flush() == (0, "Next. ")
    assert t.text == "Hello.\nNext."
    paste = Transcript(trailing_space=True)
    paste.add_text("Hello.")
    paste.flush()
    paste.add_break(2)
    assert paste.flush(can_backspace=False) == (0, "\n\n")   # can't backspace: the space stays
    same_chunk = Transcript(trailing_space=True)
    same_chunk.add_text("A.")
    same_chunk.add_break()
    same_chunk.add_text("B.")
    assert same_chunk.flush() == (0, "A.\nB. ")


def test_undo_counts_exactly_what_was_sent():
    t = Transcript()
    t.add_text("Deploy core.")
    t.flush()
    t.add_text("Then CRM.")
    t.flush()
    assert t.undo(can_backspace=True) == len("Then CRM. ")
    t.add_text("Then the ALB.")
    assert t.flush() == (0, "Then the ALB. ")
    assert t.text == "Deploy core. Then the ALB."
    t.add_break()
    t.flush(can_backspace=True)                # took back the space after "ALB."
    assert t.undo(True) == 1                   # a line break is one Backspace
    t.add_text("More.")
    assert t.flush() == (0, " More. ")         # the space taken back is put in front again
    off = Transcript(trailing_space=False)
    off.add_text("A.")
    off.flush()
    off.add_text("B.")
    off.flush()
    assert off.undo(True) == len(" B.")


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
    assert t.flush() == (0, "") and t.text == "Sent."
    assert Transcript().undo(True) == 0                # nothing to undo


def test_plan_commit_and_take_unsent():
    t = Transcript()
    t.add_text("One.")
    t.flush()
    t.add_text("Two.")
    t.add_break()
    bs, out, shares = t.plan(True)
    assert (bs, out) == (0, "Two.\n") and t.pending     # not recorded as sent until commit()
    assert t.take_unsent() == [("text", "Two."), ("break", "\n")]
    assert not t.pending and t.text == "One." and t.render(sent_only=True) == "One."


def test_compose_whole_dictation(m):
    phrases = ["First point.", "New line.", "Second point.", "Wrong bit.", "Scratch that.", "Third.",
               "New paragraph.", "Done.", "Stop listening.", "Never typed."]
    text, stop = compose(phrases, m)
    assert text == "First point.\nSecond point. Third.\n\nDone."
    assert stop
    assert compose(["Just text."], m) == ("Just text.", False)
    assert compose(["Scratch that."], m) == ("", False)
