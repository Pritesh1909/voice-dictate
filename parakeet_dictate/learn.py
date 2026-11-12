"""Learn dictionary entries from the user's corrections of a dictation.

The user fixes the last dictated text by hand; a word-level diff of what was typed against the fixed
text finds the runs of heard words that were replaced by a different spelling. Each becomes a proposed
dictionary entry, shown with a checkbox before anything is saved:

- "key cloak" -> "Keycloak": the dictionary already matches spacing and case by key, so only the term
  is needed ("Keycloak" = term, no heard_as).
- "mail sey" -> "Mailzzy": the key differs, so the heard words are saved as a heard_as spelling.

Ignored: punctuation-only changes, a capital added or removed at a sentence start, inserted or deleted
words, and rewrites longer than MAX_RUN words on either side (those are edits, not spellings).

Learned entries live in their own file, %APPDATA%\\ParakeetDictate\\learned.toml, in the same
terms / heard_as shape as [dictionary], so the user's commented config.toml is never rewritten.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .dictionary import MAX_TERM_LENGTH, normalize_terms, word_key

log = logging.getLogger(__name__)

MAX_RUN = 4   # longest run of words (either side) still treated as a spelling fix, not a rewrite

_EDGE = re.compile(r"^[\"'(\[{“‘]*(.*?)[.,!?;:…\"')\]}”’]*$", re.S)
_ENDS_SENTENCE = re.compile(r"[.!?…][\"')\]}”’]*$")
_PLAIN_LOWER = re.compile(r"^[a-z]+(?:'[a-z]+)?$")


@dataclass(frozen=True)
class Proposal:
    term: str          # the spelling the user wants
    heard_as: str      # what the model wrote, or "" when the term's key already matches it
    checked: bool      # ticked by default in the dialog
    note: str          # why, in plain words, for the dialog

    @property
    def entry(self) -> tuple[str, str]:
        return self.term, self.heard_as


def _core(tok: str) -> str:
    m = _EDGE.match(tok)
    return m.group(1) if m else tok


def _starts_sentence(tokens: list[str], i: int) -> bool:
    return i == 0 or bool(_ENDS_SENTENCE.search(tokens[i - 1]))


def _plain_lower(words: list[str], at_start: bool = False) -> bool:
    """Ordinary lower-case words; a capital on the first word is ignored at a sentence start."""
    if at_start and words:
        words = [words[0][:1].lower() + words[0][1:]] + words[1:]
    return all(_PLAIN_LOWER.match(w) for w in words)


def _trim_plain_edges(heard: list[str], fixed: list[str]) -> tuple[list[str], list[str]]:
    """Drop ordinary words the diff glued onto a fix: "mail sey" -> "Mailzzy now" is the term "Mailzzy"
    plus an added "now". Only done while a non-plain word (the actual term) is left."""
    while len(fixed) > 1 and _PLAIN_LOWER.match(fixed[-1]) and not _plain_lower(fixed[:-1]):
        if len(heard) > 1 and heard[-1] == fixed[-1]:
            heard = heard[:-1]
        fixed = fixed[:-1]
    while len(fixed) > 1 and _PLAIN_LOWER.match(fixed[0]) and not _plain_lower(fixed[1:]):
        if len(heard) > 1 and heard[0] == fixed[0]:
            heard = heard[1:]
        fixed = fixed[1:]
    return heard, fixed


def _proposal(heard: list[str], fixed: list[str], heard_at_start: bool, fixed_at_start: bool) -> Proposal | None:
    if word_key(" ".join(heard)) != word_key(" ".join(fixed)):
        heard, fixed = _trim_plain_edges(heard, fixed)
    heard_text, term = " ".join(heard), " ".join(fixed)
    if not word_key(term) or len(term) > MAX_TERM_LENGTH or heard_text == term:
        return None
    same_key = word_key(heard_text) == word_key(term)
    if same_key and heard_text.lower() == term.lower() and (heard_at_start or fixed_at_start):
        return None   # only a capital added or removed at the start of a sentence
    if same_key:
        if _plain_lower(fixed):
            # "The" -> "the" mid-sentence: a lower-case "term" would also lower-case sentence starts.
            return Proposal(term, "", False, "lower-case spelling (would also change sentence starts)")
        return Proposal(term, "", True, "spelling and spacing")
    if _plain_lower(fixed):
        # "their" -> "there": swapping one ordinary word for another everywhere would break normal speech.
        return None
    if fixed_at_start and len(heard) == len(fixed) and _plain_lower(fixed, at_start=True):
        # "Their" -> "There" or "Maylzy" -> "Mailzzy" at a sentence start: can't tell a name from a word.
        return Proposal(term, heard_text, False, f'heard as "{heard_text}"; tick only if it is a name')
    return Proposal(term, heard_text, True, f'heard as "{heard_text}"')


def propose(original: str, corrected: str) -> list[Proposal]:
    """Candidate dictionary entries from a hand correction of `original`, in text order, no duplicates."""
    a, b = original.split(), corrected.split()
    ca, cb = [_core(t) for t in a], [_core(t) for t in b]
    out: list[Proposal] = []
    seen: set[tuple[str, str]] = set()
    sm = difflib.SequenceMatcher(None, ca, cb, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or i2 - i1 > MAX_RUN or j2 - j1 > MAX_RUN:
            continue
        heard = [w for w in ca[i1:i2] if w]
        fixed = [w for w in cb[j1:j2] if w]
        if not heard or not fixed:
            continue
        p = _proposal(heard, fixed, _starts_sentence(a, i1), _starts_sentence(b, j1))
        if p and p.entry not in seen:
            seen.add(p.entry)
            out.append(p)
    return out


# --- learned.toml ---------------------------------------------------------------------------------

HEADER = ("# Words learned from \"Correct last dictation…\" in the Parakeet Dictate tray menu.\n"
          "# Same shape as [dictionary] in config.toml, which wins when both spell a word differently.\n"
          "# Edit or delete lines freely; this file is rewritten when you save a correction.\n")


def load(path: Path) -> list[tuple[str, str]]:
    """(term, heard_as) pairs from learned.toml, or [] if it is missing or unreadable (never fatal)."""
    try:
        d = tomllib.loads(path.read_text("utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        log.warning("learned words not loaded (%s): %s", path.name, e)
        return []
    sec = d.get("dictionary", {})
    if not isinstance(sec, dict):
        return []
    terms = sec.get("terms", [])
    out = [(t, "") for t in terms if isinstance(t, str)] if isinstance(terms, list) else []
    heard = sec.get("heard_as", {})
    if isinstance(heard, dict):
        for term, v in heard.items():
            spellings = [v] if isinstance(v, str) else v if isinstance(v, list) else []
            out.append((term, ""))
            out += [(term, x) for x in spellings if isinstance(x, str)]
    return normalize_terms(out)


def merge(existing: Iterable[tuple[str, str]], new: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Existing entries first, then the new ones not already there."""
    return normalize_terms(list(existing) + list(new))


def _q(s: str) -> str:
    """A TOML basic string."""
    out = []
    for ch in s:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def dumps(entries: Iterable[tuple[str, str]]) -> str:
    entries = normalize_terms(entries)
    terms = [t for t, h in entries if not h and not any(t2 == t and h2 for t2, h2 in entries)]
    heard: dict[str, list[str]] = {}
    for t, h in entries:
        if h:
            heard.setdefault(t, []).append(h)
    lines = [HEADER, "[dictionary]", "terms = [" + ", ".join(_q(t) for t in terms) + "]", "",
             "[dictionary.heard_as]"]
    lines += [f"{_q(t)} = [" + ", ".join(_q(h) for h in hs) + "]" for t, hs in heard.items()]
    return "\n".join(lines) + "\n"


def save(path: Path, entries: Iterable[tuple[str, str]]) -> None:
    """Write learned.toml atomically (a half-written file would lose every learned word)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps(entries), "utf-8")
    os.replace(tmp, path)


def add(path: Path, new: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge new entries into learned.toml and return everything it now holds."""
    entries = merge(load(path), new)
    save(path, entries)
    return entries
