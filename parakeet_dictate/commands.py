"""Spoken commands ("new line", "scratch that", ...) and the bookkeeping that lets "scratch that" work.

A command is only recognised when a whole phrase is nothing but the command. The voice detector ends a
phrase at each pause, so "new line" said on its own is a command, while "I added a new line to the file"
is ordinary dictation and is typed as said. Case and punctuation are ignored ("New line." matches).

Transcript records exactly which characters each phrase sent, so "scratch that" can take the last one
back with the right number of Backspaces and never more.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

# What a phrase can be mapped to in [commands.phrases].
NEWLINE, PARAGRAPH, UNDO, STOP = "newline", "paragraph", "undo", "stop"
ACTIONS = (NEWLINE, PARAGRAPH, UNDO, STOP)

_NOT_WORD = re.compile(r"[^\w']+")


def normalize_phrase(text: str) -> str:
    """Lower-case words with punctuation dropped: "New line." -> "new line", "Scratch that!" -> "scratch that"."""
    return " ".join(w.strip("'_") for w in _NOT_WORD.split(text.lower()) if w.strip("'_"))


class CommandMatcher:
    """Maps a finished phrase to an action, or None when it is ordinary dictation."""

    def __init__(self, phrases: Iterable[tuple[str, str]] | Mapping[str, str], enabled: bool = True):
        items = phrases.items() if isinstance(phrases, Mapping) else phrases
        self.enabled = enabled
        self._map = {normalize_phrase(p): a for p, a in items if normalize_phrase(p) and a in ACTIONS}

    def match(self, phrase: str) -> str | None:
        if not self.enabled or not phrase:
            return None
        return self._map.get(normalize_phrase(phrase))


@dataclass
class _Piece:
    kind: str            # "text" or "break"
    text: str            # the phrase, or one or two line breaks
    sent: str = ""       # exactly what went to the window for it (leading and trailing space included)
    flushed: bool = False


class Transcript:
    """The pieces one dictation types, in order: phrases of text and line breaks, and exactly which
    characters each one put on screen, so "scratch that" can take the last one back with the right
    number of Backspaces and never more.

    Spacing is the same as typing without commands: each delivery (one or more pieces sent together;
    several wait while a modifier key is held) is followed by a space when trailing_space is on, or
    preceded by one when it is off. A line break arriving after that trailing space takes it back first
    (one Backspace, only when that is safe) so dictated lines don't end in a space.

    Sending is two-step so a failed send loses nothing: plan() says what to send, commit() records it as
    sent, take_unsent() hands the pieces over when the window can't be typed into.
    """

    def __init__(self, trailing_space: bool = True):
        self.trailing_space = trailing_space
        self._pieces: list[_Piece] = []

    def add_text(self, text: str) -> None:
        text = text.strip()
        if text:
            self._pieces.append(_Piece("text", text))

    def add_break(self, lines: int = 1) -> None:
        self._pieces.append(_Piece("break", "\n" * max(1, lines)))

    @property
    def pending(self) -> bool:
        return any(not p.flushed for p in self._pieces)

    def _split(self) -> tuple[_Piece | None, list[_Piece]]:
        unsent = [p for p in self._pieces if not p.flushed]
        sent = [p for p in self._pieces if p.flushed]
        return (sent[-1] if sent else None), unsent

    def plan(self, can_backspace: bool) -> tuple[int, str, list[str]]:
        """(Backspaces, characters, each unsent piece's share of them) for what isn't sent yet."""
        prev, unsent = self._split()
        if not unsent:
            return 0, "", []
        bs, shares = 0, []
        for i, p in enumerate(unsent):
            before = unsent[i - 1] if i else prev
            lead = ""
            if p.kind == "text" and before is not None and before.kind == "text":
                ends_with_space = i == 0 and before.sent.endswith(" ")
                lead = "" if ends_with_space else " "
            elif p.kind == "break" and i == 0 and prev is not None and prev.sent.endswith(" ") and can_backspace:
                bs = 1      # the trailing space typed after the last phrase
            shares.append(lead + p.text)
        if self.trailing_space and unsent[-1].kind == "text":
            shares[-1] += " "
        return bs, "".join(shares), shares

    def commit(self, bs: int, shares: list[str]) -> None:
        prev, unsent = self._split()
        if bs and prev is not None:
            prev.sent = prev.sent[:-bs]
        for p, share in zip(unsent, shares):
            p.sent, p.flushed = share, True

    def flush(self, can_backspace: bool = False) -> tuple[int, str]:
        """Plan and commit in one go: (Backspaces, characters) to send."""
        bs, out, shares = self.plan(can_backspace)
        self.commit(bs, shares)
        return bs, out

    def take_unsent(self) -> list[tuple[str, str]]:
        """Remove the pieces not sent yet and return them as (kind, text)."""
        _, unsent = self._split()
        self._pieces = [p for p in self._pieces if p.flushed]
        return [(p.kind, p.text) for p in unsent]

    def undo(self, can_backspace: bool) -> int:
        """Take back the last piece. Returns how many Backspaces the caller must send (0 = none needed).

        can_backspace is False when the keys can't safely be sent: paste mode, or a different window in
        front than the one the dictation typed into. Then a piece already on screen is left alone (and
        stays in the record); a piece not sent yet is still dropped, as that needs no keys."""
        if not self._pieces:
            return 0
        last = self._pieces[-1]
        if not last.flushed:
            self._pieces.pop()
            return 0
        if not can_backspace:
            return 0
        self._pieces.pop()
        # One Backspace per character as the user sees it (a code point); a line break is one.
        return len(last.sent)

    def render(self, sent_only: bool = False) -> str:
        """The text as it reads: phrases joined by single spaces, line breaks without spaces around them."""
        out = ""
        prev = None
        for p in self._pieces:
            if sent_only and not p.flushed:
                break
            out += (" " if p.kind == "text" and prev == "text" else "") + p.text
            prev = p.kind
        return out

    @property
    def text(self) -> str:
        """Everything in the record (sent or not), as it reads, without the trailing space."""
        return self.render()


def compose(phrases: Iterable[str], matcher: CommandMatcher) -> tuple[str, bool]:
    """Whole-dictation output (nothing typed while speaking): apply the commands to the cleaned phrases.

    Returns (text, stop_heard); the caller adds the trailing space as before. Nothing has been typed yet,
    so "scratch that" simply drops the piece before it; "stop listening" is not typed, and anything
    after it is not either."""
    t = Transcript()
    stop = False
    for phrase in phrases:
        action = matcher.match(phrase)
        if action is None:
            t.add_text(phrase)
        elif action == NEWLINE:
            t.add_break(1)
        elif action == PARAGRAPH:
            t.add_break(2)
        elif action == UNDO:
            t.undo(can_backspace=False)
        elif action == STOP:
            stop = True
            break
    return t.text, stop
