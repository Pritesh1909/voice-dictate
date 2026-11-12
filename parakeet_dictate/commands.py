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
    kind: str        # "text" or "break"
    sent: str        # exactly what goes (or went) to the window for it, leading space included
    flushed: bool = False


class Transcript:
    """The pieces one dictation types, in order: phrases of text and line breaks.

    The space between phrases is sent in front of the next phrase rather than after the last one, so the
    cursor always sits right after the last word: a line break then starts cleanly at the end of the
    line, and undoing a phrase removes its leading space with it. The trailing space of the whole
    dictation ([output] add_trailing_space) is only added by finish().

    flush() returns what hasn't been sent yet (it may wait while a modifier key is held, so several
    pieces can go out together). undo() removes the last piece: one not sent yet is just dropped; one
    already sent costs Backspaces, which the caller sends only when that is safe.
    """

    def __init__(self, trailing_space: bool = True):
        self.trailing_space = trailing_space
        self._pieces: list[_Piece] = []
        self.finished = False

    def add_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        lead = " " if self._pieces and self._pieces[-1].kind == "text" else ""
        self._pieces.append(_Piece("text", lead + text))

    def add_break(self, lines: int = 1) -> None:
        self._pieces.append(_Piece("break", "\n" * max(1, lines)))

    @property
    def pending(self) -> bool:
        return any(not p.flushed for p in self._pieces)

    def flush(self) -> str:
        """Characters not sent yet; they count as sent from now on."""
        out = "".join(p.sent for p in self._pieces if not p.flushed)
        for p in self._pieces:
            p.flushed = True
        return out

    def finish(self) -> str:
        """Whatever is still unsent, plus the dictation's trailing space when it ends with text."""
        out = self.flush()
        if self.trailing_space and self._pieces and self._pieces[-1].kind == "text" and not self.finished:
            out += " "
        self.finished = True
        return out

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

    @property
    def text(self) -> str:
        """The dictation as it reads (for "Copy last dictation"), without the trailing space."""
        return "".join(p.sent for p in self._pieces)


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
