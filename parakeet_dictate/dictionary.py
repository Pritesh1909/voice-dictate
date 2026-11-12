"""Spell names and code terms your way, whatever spacing or case the model heard them with.

Ported from Orca 1.4.256 (shared/dictation-dictionary.js). Each term is matched by its "key": the
lower-case letters and digits with spaces, hyphens, dots and underscores dropped. So "Keycloak" also
fixes "key cloak", "Key-Cloak" and "KEYCLOAK", and "useAudioCapture" fixes "use audio capture".
A "heard as" spelling covers what the model gets wrong in a way the key can't ("mail sey" -> "Mailzzy").
At each word the longest run of joined words (up to 8) that keys like a term becomes that term.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable, Iterable

MAX_TERMS = 500
MAX_TERM_LENGTH = 100
MAX_WORDS_PER_TERM = 8

# Letters, digits and the symbols that belong to a name ("C#", "C++", "@orca").
_WORD_RE = re.compile(r"(?:[^\W_]|[#+@$&])+")
# What may sit between the words of one term: spaces, or one joiner as in "on-change" or "stt-worker.ts".
_JOINING_GAP_RE = re.compile(r"(?:\s+|[-_./])")
# A term's own leading or trailing joiners, as in ".NET", taken along when the text already has them.
_EDGE_JOINERS_RE = re.compile(r"^([-_./]*).*?([-_./]*)$", re.S)
# Scripts written without spaces, where a phrase can only be found inside a longer run of letters.
_UNSPACED_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿฀-໿ក-៿က-႟]")


def word_key(text: str) -> str:
    return "".join(_WORD_RE.findall(unicodedata.normalize("NFKC", text).lower()))


def normalize_terms(entries: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """(term, heard_as) pairs, trimmed, capped and without duplicates. heard_as may be ""."""
    out, seen = [], set()
    for term, heard in entries:
        term = (term or "").strip()[:MAX_TERM_LENGTH]
        heard = (heard or "").strip()[:MAX_TERM_LENGTH]
        if not term or (term, heard) in seen:
            continue
        seen.add((term, heard))
        out.append((term, heard))
    return out[:MAX_TERMS]


def _spelling(term: str) -> tuple[str, str, str]:
    m = _EDGE_JOINERS_RE.match(term)
    return term, (m.group(1) if m else ""), (m.group(2) if m else "")


def build(entries: Iterable[tuple[str, str]],
          ahead: Iterable[tuple[str, str]] = ()) -> Callable[[str], str] | None:
    """A function that respells dictated text, or None when there are no terms. Entries in `ahead` (one
    program's own terms) win over all of `entries`, even over a "heard as" there that keys the same."""
    by_key: dict[str, tuple[str, str, str]] = {}
    unspaced: list[tuple[str, str]] = []
    for terms in (normalize_terms(ahead), normalize_terms(entries)):
        # A "heard as" is what the user asked for, so it beats another term that keys the same.
        for term, heard in terms:
            if heard and _UNSPACED_RE.search(heard):
                unspaced.append((heard, term))
            elif heard:
                by_key.setdefault(word_key(heard), _spelling(term))
        for term, _ in terms:
            k = word_key(term)
            if k:
                by_key.setdefault(k, _spelling(term))
    by_key.pop("", None)
    if not by_key and not unspaced:
        return None
    longest = max((len(k) for k in by_key), default=0)

    def apply(text: str) -> str:
        for heard, term in unspaced:
            text = text.replace(heard, term)
        return _replace_runs(text, by_key, longest) if by_key else text
    return apply


def _replace_runs(text: str, by_key: dict, longest: int) -> str:
    words = [(m.start(), m.end(), word_key(m.group(0))) for m in _WORD_RE.finditer(text)]
    joins = [i + 1 < len(words) and bool(_JOINING_GAP_RE.fullmatch(text[w[1]:words[i + 1][0]]))
             for i, w in enumerate(words)]
    out, cursor, start = [], 0, 0
    while start < len(words):
        key, match = "", None
        end = start
        while end < len(words) and end - start < MAX_WORDS_PER_TERM:
            if end > start and not joins[end - 1]:
                break
            key += words[end][2]
            if len(key) > longest:
                break
            if key in by_key:
                match = (end, by_key[key])
            end += 1
        if match is None:
            start += 1
            continue
        end, (term, lead, trail) = match
        frm, to = words[start][0], words[end][1]
        if lead and frm - len(lead) >= cursor and text[frm - len(lead):frm] == lead:
            frm -= len(lead)
        if trail and text.startswith(trail, to):
            to += len(trail)
        out.append(text[cursor:frm] + term)
        cursor = to
        start = end + 1
    return "".join(out) + text[cursor:]
