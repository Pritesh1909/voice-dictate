"""Turn raw Parakeet output into clean dictation text.

Everything here is deterministic word-list logic: it only removes fillers, collapses stutters and applies
the user's exact replacements and dictionary spellings. It never rewords anything else (no AI rewrite step).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from . import dictionary

# token = leading openers + core word + trailing punctuation
_SPLIT = re.compile(r"^([\"'(\[{“‘]*)(.*?)([.,!?;:…\"')\]}”’\-]*)$", re.S)
_ENDS_SENTENCE = re.compile(r"[.!?…][\"')\]}”’]*$")
_TERMINAL = re.compile(r"[.!?…]")
_WORD = re.compile(r"^[A-Za-z]+(?:'[A-Za-z]+)?$")


def _split(tok: str) -> tuple[str, str, str]:
    m = _SPLIT.match(tok)
    return (m.group(1), m.group(2), m.group(3)) if m else ("", tok, "")


def filler_regex(words: Iterable[str]) -> re.Pattern | None:
    """Build one case-insensitive full-word pattern from base filler spellings.

    Each run of letters may be stretched but not shortened: "uh" matches uh/uhh/uuhh, "ah" matches AHH,
    "mm" matches mm/mmm but never a lone "M", "hm" matches hm/hmm.
    """
    alts = set()
    for w in words:
        w = w.strip().lower()
        if not w:
            continue
        alts.add("".join(f"{re.escape(run[0])}{{{len(run)},}}" for run in
                         (m.group(0) for m in re.finditer(r"(.)\1*", w))))
    if not alts:
        return None
    return re.compile("(?:" + "|".join(sorted(alts, key=len, reverse=True)) + ")", re.I)


def _cap_first(tok: str) -> str:
    for i, ch in enumerate(tok):
        if ch.isalpha():
            return tok[:i] + ch.upper() + tok[i + 1:]
    return tok


def remove_fillers(text: str, pattern: re.Pattern | None) -> str:
    if pattern is None:
        return text
    out: list[str] = []
    cap_next = False
    for tok in text.split():
        lead, core, trail = _split(tok)
        if core and pattern.fullmatch(core):
            at_start = not out or bool(_ENDS_SENTENCE.search(out[-1]))
            terms = _TERMINAL.findall(trail)
            if terms:
                # "and then, uh." -> "and then."
                if out and not at_start:
                    out[-1] = out[-1].rstrip(",;:-") + terms[-1]
                cap_next = True
            else:
                # "we should, um, deploy" -> "we should deploy" (the commas only marked the pause)
                if out and not at_start and "," in trail and out[-1].endswith(","):
                    out[-1] = out[-1][:-1]
                if at_start:
                    cap_next = True
            continue
        if cap_next:
            tok = _cap_first(tok)
            cap_next = False
        out.append(tok)
    return " ".join(out)


def remove_repeats(text: str, allowed: Iterable[str] = ()) -> str:
    """Collapse stutters like "the the" / "I, I" / "to- to". Words in `allowed` may legitimately repeat."""
    keep = {w.lower() for w in allowed}
    out: list[str] = []
    for tok in text.split():
        if out:
            pl, pc, pt = _split(out[-1])
            _, c, t = _split(tok)
            if (pc and _WORD.match(pc) and pc.lower() == c.lower() and pc.lower() not in keep
                    and pt in ("", ",", "-")):
                out[-1] = pl + pc + t
                continue
        out.append(tok)
    return " ".join(out)


def compile_replacements(pairs: Iterable[tuple[str, str]]) -> list[tuple[re.Pattern, str]]:
    compiled = []
    for src, dst in pairs:
        words = src.split()
        if not words:
            continue
        rx = r"(?<![\w'])" + r"[\s\-]+".join(re.escape(w) for w in words) + r"(?![\w'])"
        compiled.append((re.compile(rx, re.I), dst))
    return compiled


def apply_replacements(text: str, compiled: list[tuple[re.Pattern, str]]) -> str:
    for rx, dst in compiled:
        text = rx.sub(lambda _m, d=dst: d, text)
    return text


def tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.!?;:…])", r"\1", text)      # no space before punctuation
    text = re.sub(r",+(?=[.!?;:…])", "", text)          # ",." -> "."
    text = re.sub(r",{2,}", ",", text)
    text = re.sub(r"^[,;:\-\s]+", "", text)             # nothing dangling at the start
    return text


@dataclass
class Cleaner:
    fillers: list[str] = field(default_factory=list)
    remove_repeated_words: bool = True
    repeated_words_allowed: list[str] = field(default_factory=list)
    replacements: list[tuple[str, str]] = field(default_factory=list)
    dictionary: list[tuple[str, str]] = field(default_factory=list)   # (term, heard_as or "")

    def __post_init__(self) -> None:
        self._fillers = filler_regex(self.fillers)
        self._repl = compile_replacements(self.replacements)
        self._spell = dictionary.build(self.dictionary)

    @classmethod
    def from_config(cls, c) -> "Cleaner":
        return cls(fillers=list(c.fillers), remove_repeated_words=c.remove_repeated_words,
                   repeated_words_allowed=list(c.repeated_words_allowed), replacements=list(c.replacements),
                   dictionary=list(c.dictionary))

    def clean(self, raw: str) -> str:
        text = remove_fillers(raw.replace("\n", " "), self._fillers)
        if self.remove_repeated_words:
            text = remove_repeats(text, self.repeated_words_allowed)
        text = apply_replacements(text, self._repl)
        text = tidy(text)
        if raw[:1].isupper() and text:
            text = _cap_first(text)
        # Terms last, so a lower-case term at the start ("pnpm") keeps its spelling.
        return self._spell(text) if self._spell else text
